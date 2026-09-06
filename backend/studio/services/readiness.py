"""Capability-oriented production readiness checks."""
from __future__ import annotations

from datetime import timedelta
import os
from pathlib import Path
import shutil

from django.conf import settings
from django.contrib.auth import get_user_model
from django.core.exceptions import ValidationError
from django.db import connection, models
from django.db.migrations.executor import MigrationExecutor
from django.utils import timezone

from studio.models import (
    ConfigDefinition,
    HostCapabilitySnapshot,
    ModelDeployment,
    ModelProfileVersion,
    ProductionRecipeVersion,
    ProviderProfile,
    SecretBinding,
    SecretVersion,
    WorkerLease,
    WorkflowTemplateVersion,
)
from studio.services.configuration import ConfigurationResolutionError, resolve_configuration
from studio.services.bible_reference_contract import BIBLE_IMAGE_GENERATION_STAGE_KEY
from studio.services.comfyui_contract import (
    ComfyUIContractError,
    ComfyUIContractUnavailable,
    validate_comfyui_api_contract,
)
from studio.services.distribution import DistributionError, distribution_configuration
from studio.services.provider_registry import (
    ProviderDriverError,
    deployment_supports_script_generation,
    driver_for,
    resolve_credential,
)
from studio.services.secret_store import SecretStoreError, load_keyring, resolve_secret_version
from studio.services.async_media_contract import fresh_smoke_snapshot
from studio.services.runtime_platform import (
    required_workflow_artifacts,
    verified_artifact_availability,
)
from studio.services.script_generation_contract import (
    SCRIPT_GENERATE_OPERATION,
    validate_script_generation_model_profile,
)
from studio.services.script_structure import (
    SCRIPT_STRUCTURE_OPERATION,
    validate_script_structure_model_profile,
)
from studio.services.prompt_normalization import validate_prompt_normalization_model_profile
from studio.services.prompt_normalization_contract import PROMPT_NORMALIZE_OPERATION


from studio.services.production_visual_rules import (
    VISUAL_RULES_OPERATION,
    validate_visual_rules_model_profile,
)
from studio.services.media_capabilities import resolve_conditioning_contract
from studio.services.model_profile_contracts import (
    verified_model_profile_specification,
)
from studio.services.prompt_profiles import (
    compatible_prompt_profile_bindings,
    model_family_for_profile,
    require_prompt_profile_binding,
    validate_bible_image_prompt_profile,
)
from studio.services.security_boundaries import security_boundary_resolution_cache
from studio.services.workflow_versions import (
    declared_workflow_operation,
    verify_published_workflow_capability,
)


READINESS_RUNTIME_KEYS = (
    "runtime.readiness_worker_max_age_seconds",
    "runtime.readiness_min_disk_free_bytes",
    "runtime.readiness_generation_worker_required",
    "runtime.readiness_text_generation_worker_required",
    "runtime.readiness_assembly_worker_required",
)
_RUNTIME_VALUES_UNSET = object()


def _readiness_runtime_values() -> dict[str, object]:
    values = resolve_configuration(required_keys=READINESS_RUNTIME_KEYS).values
    worker_age = values["runtime.readiness_worker_max_age_seconds"]
    disk_free = values["runtime.readiness_min_disk_free_bytes"]
    worker_flags = {
        "generation_worker_required": values[
            "runtime.readiness_generation_worker_required"
        ],
        "text_generation_worker_required": values[
            "runtime.readiness_text_generation_worker_required"
        ],
        "assembly_worker_required": values[
            "runtime.readiness_assembly_worker_required"
        ],
    }
    if type(worker_age) is not int or worker_age < 1:
        raise ConfigurationResolutionError(
            "runtime.readiness_worker_max_age_seconds 必须是正整数",
            code="readiness_config_invalid",
            missing_keys=("runtime.readiness_worker_max_age_seconds",),
        )
    if type(disk_free) is not int or disk_free < 0:
        raise ConfigurationResolutionError(
            "runtime.readiness_min_disk_free_bytes 必须是非负整数",
            code="readiness_config_invalid",
            missing_keys=("runtime.readiness_min_disk_free_bytes",),
        )
    invalid_flags = [
        name for name, value in worker_flags.items()
        if type(value) is not bool
    ]
    if invalid_flags:
        raise ConfigurationResolutionError(
            "readiness worker required 配置必须是布尔值",
            code="readiness_config_invalid",
            missing_keys=tuple(
                f"runtime.readiness_{name}" for name in invalid_flags
            ),
        )
    return {
        "worker_max_age_seconds": worker_age,
        "minimum_disk_free_bytes": disk_free,
        **worker_flags,
    }


def _runtime_values_or_raise(runtime_values: object = _RUNTIME_VALUES_UNSET) -> dict:
    if runtime_values is _RUNTIME_VALUES_UNSET:
        return _readiness_runtime_values()
    if isinstance(runtime_values, BaseException):
        raise runtime_values
    if not isinstance(runtime_values, dict):
        raise ConfigurationResolutionError(
            "readiness 运行配置不可用",
            code="readiness_config_invalid",
            missing_keys=READINESS_RUNTIME_KEYS,
        )
    return runtime_values


User = get_user_model()


_TEXT_STAGE_OPERATIONS = {
    "script_generate": SCRIPT_GENERATE_OPERATION,
    "script_structure": SCRIPT_STRUCTURE_OPERATION,
    "visual_rules_generate": VISUAL_RULES_OPERATION,
    "script_normalize": PROMPT_NORMALIZE_OPERATION,
}

_TEXT_PROFILE_CONTRACT_REASON_CODES = {
    SCRIPT_GENERATE_OPERATION: "script_profile_contract_invalid",
    SCRIPT_STRUCTURE_OPERATION: "script_structure_profile_contract_invalid",
    VISUAL_RULES_OPERATION: "visual_rules_profile_contract_invalid",
    PROMPT_NORMALIZE_OPERATION: "prompt_normalize_profile_contract_invalid",
}

_TEXT_SMOKE_REASON_CODES = {
    SCRIPT_GENERATE_OPERATION: "script_smoke_missing_or_stale",
    SCRIPT_STRUCTURE_OPERATION: "script_structure_smoke_missing_or_stale",
    VISUAL_RULES_OPERATION: "visual_rules_smoke_missing_or_stale",
    PROMPT_NORMALIZE_OPERATION: "prompt_normalize_smoke_missing_or_stale",
}


def _result(ok: bool, message: str, **details) -> dict:
    return {"ok": bool(ok), "message": message, **details}


def _database_check() -> dict:
    try:
        with connection.cursor() as cursor:
            cursor.execute("SELECT 1")
            value = cursor.fetchone()
        return _result(value == (1,), "数据库连接正常" if value == (1,) else "数据库响应异常")
    except Exception:
        return _result(False, "数据库不可用")


def _migration_check() -> dict:
    try:
        executor = MigrationExecutor(connection)
        pending = executor.migration_plan(executor.loader.graph.leaf_nodes())
        return _result(
            not pending,
            "数据库迁移已同步" if not pending else "存在未应用的数据库迁移",
            pending_count=len(pending),
        )
    except Exception:
        return _result(False, "无法核对数据库迁移", pending_count=None)


def _configuration_check() -> dict:
    """Reject an incomplete product-default bootstrap instead of inventing values."""
    try:
        required = list(ConfigDefinition.objects.filter(
            is_active=True,
            is_required=True,
        ).values_list("key", flat=True))
        review = list(ConfigDefinition.objects.filter(
            is_active=True,
            needs_review=True,
        ).values_list("key", flat=True))
        if review:
            return _result(
                False,
                "必需配置缺少可验证的产品初始值",
                needs_review=sorted(review),
            )
        resolve_configuration(required_keys=required)
        return _result(True, "系统激活配置完整", needs_review=[])
    except (ConfigurationResolutionError, ValueError, TypeError) as exc:
        return _result(False, "系统激活配置不完整", error=str(exc)[:500])
    except Exception:
        return _result(False, "无法核对系统激活配置")


def _access_control_check() -> dict:
    """A healthy deployment must have a usable governance owner."""

    try:
        candidates = list(User.objects.filter(
            is_active=True,
            is_superuser=True,
        ).only("password"))
    except Exception:
        return _result(False, "无法核对超级管理员", active_superuser_count=None)
    usable = sum(1 for user in candidates if user.has_usable_password())
    return _result(
        usable > 0,
        "存在可登录的活跃超级管理员" if usable else "缺少可登录的活跃超级管理员",
        active_superuser_count=usable,
    )


def _secret_store_check() -> dict:
    """Fail closed only for secrets referenced by currently active facts."""
    try:
        bindings = {}
        for binding in SecretBinding.objects.filter(
            models.Q(provider_profiles__is_active=True)
            | models.Q(model_deployments__status=ModelDeployment.Status.ACTIVE)
        ).distinct():
            bindings[str(binding.id)] = binding
        config_versions = list(SecretVersion.objects.select_related("binding").filter(
            config_values__revision__scope_type="system",
            config_values__revision__status="published",
            config_values__revision__activation_state="active",
        ).distinct())
    except Exception:
        return _result(
            False, "无法核对数据库受管凭据", required=True,
            reference_count=None, failure_count=None,
        )
    try:
        load_keyring()
    except SecretStoreError as exc:
        return _result(
            False, "数据库敏感配置密钥环不可用", required=True,
            reference_count=len(bindings) + len(config_versions),
            failure_count=1, failure_codes=[exc.code],
        )
    if not bindings and not config_versions:
        return _result(
            True, "数据库敏感配置密钥环可用，当前没有活跃凭据引用",
            required=True, reference_count=0, failure_count=0, failure_codes=[],
        )
    failures = {}
    for binding_id, binding in bindings.items():
        try:
            resolve_credential(binding)
        except ProviderDriverError as exc:
            failures[f"binding:{binding_id}"] = exc.code
    for version in config_versions:
        try:
            if not version.binding.is_active:
                raise SecretStoreError("受管凭据已停用", code="credential_inactive")
            resolve_secret_version(version)
        except SecretStoreError as exc:
            failures[f"version:{version.id}"] = exc.code
    return _result(
        not failures,
        "活跃受管凭据均可解密" if not failures else "活跃受管凭据不可解密",
        required=True,
        reference_count=len(bindings) + len(config_versions),
        failure_count=len(failures), failure_codes=sorted(set(failures.values())),
    )


def _storage_check(*, runtime_values: object = _RUNTIME_VALUES_UNSET) -> dict:
    media_root = Path(settings.MEDIA_ROOT)
    try:
        minimum = _runtime_values_or_raise(runtime_values)["minimum_disk_free_bytes"]
    except Exception:
        return _result(
            False,
            "无法读取媒体存储就绪配置",
            free_bytes=None,
            minimum_free_bytes=None,
        )
    try:
        if not media_root.is_dir() or not os.access(media_root, os.W_OK):
            return _result(False, "媒体存储目录不可写", free_bytes=None, minimum_free_bytes=minimum)
        free = shutil.disk_usage(media_root).free
        return _result(
            free >= minimum,
            "媒体存储空间正常" if free >= minimum else "媒体存储剩余空间不足",
            free_bytes=free,
            minimum_free_bytes=minimum,
        )
    except (OSError, ValueError):
        return _result(False, "无法核对媒体存储", free_bytes=None, minimum_free_bytes=minimum)


def _binary_available(value: object) -> bool:
    raw = str(value or "").strip()
    if not raw:
        return False
    candidate = Path(raw)
    return candidate.is_file() if candidate.is_absolute() else shutil.which(raw) is not None


def _toolchain_check() -> dict:
    ffmpeg_ok = _binary_available(getattr(settings, "FFMPEG_BINARY", "ffmpeg"))
    ffprobe_ok = _binary_available(getattr(settings, "FFPROBE_BINARY", "ffprobe"))
    try:
        distribution = distribution_configuration()
        disclosure_ok = True
    except (DistributionError, OSError, ValueError):
        distribution, disclosure_ok = {}, False
    return _result(
        ffmpeg_ok and ffprobe_ok and disclosure_ok,
        "FFmpeg/FFprobe 与单镜发行标识工具链可用"
        if ffmpeg_ok and ffprobe_ok and disclosure_ok
        else "FFmpeg/FFprobe 或单镜发行标识工具链不完整",
        ffmpeg=ffmpeg_ok,
        ffprobe=ffprobe_ok,
        task_distribution_disclosure=disclosure_ok,
        distribution_version=distribution.get("version"),
        distribution_font_sha256=distribution.get("font_sha256"),
    )


def _comfyui_output_check(*, runtime_values: object = _RUNTIME_VALUES_UNSET) -> dict:
    configured = str(getattr(settings, "COMFYUI_OUTPUT_DIR", "") or "").strip()
    if not configured:
        return _result(True, "未配置本地 ComfyUI 输出目录；仅影响本地媒体能力", required=False)
    minimum = _runtime_values_or_raise(runtime_values)["minimum_disk_free_bytes"]
    try:
        root = Path(configured)
        available = root.is_dir() and os.access(root, os.R_OK)
        free = shutil.disk_usage(root).free if available else None
    except (OSError, ValueError, TypeError):
        available, free = False, None
    enough = bool(available and free is not None and free >= minimum)
    return _result(
        enough,
        "ComfyUI 输出目录可读且空间充足" if enough else "ComfyUI 输出目录不可读或空间不足",
        required=True,
        free_bytes=free,
        minimum_free_bytes=minimum,
    )


def _worker_check(
    key: str,
    label: str,
    *,
    required: bool,
    runtime_values: object = _RUNTIME_VALUES_UNSET,
) -> dict:
    if not required:
        return _result(True, f"{label}未设为必需", required=False, last_seen_at=None)
    max_age = _runtime_values_or_raise(runtime_values)["worker_max_age_seconds"]
    try:
        last_seen = WorkerLease.objects.filter(pk=key).values_list("updated_at", flat=True).first()
    except Exception:
        return _result(False, f"无法读取{label}心跳", required=True, last_seen_at=None)
    fresh = bool(last_seen and last_seen >= timezone.now() - timedelta(seconds=max_age))
    return _result(
        fresh,
        f"{label}心跳正常" if fresh else f"{label}没有新鲜心跳",
        required=True,
        last_seen_at=last_seen.isoformat() if last_seen else None,
        max_age_seconds=max_age,
    )


def _workflow_catalog_check() -> dict:
    """Read only published/current database facts; never rebuild code-owned probes."""
    try:
        versions = list(
            WorkflowTemplateVersion.objects.select_related(
                "provider", "model_profile_version", "validated_deployment__profile_version",
            ).prefetch_related("model_requirements__artifact").filter(
                status=WorkflowTemplateVersion.Status.PUBLISHED,
                is_current=True,
            )
        )
        cloud = ModelDeployment.objects.filter(
            runtime_host__isnull=True,
            status=ModelDeployment.Status.ACTIVE,
            profile_version__status="published",
            profile_version__provider__is_active=True,
        ).exists()
    except Exception:
        return _result(False, "无法读取平台能力目录", local_required=False, cloud_available=False)

    local_versions = [
        item for item in versions
        if item.provider_id and item.provider.driver_type == "comfyui"
    ]
    if not local_versions:
        return _result(
            True,
            "没有 production 本地工作流；远程能力独立判定",
            local_required=False,
            cloud_available=cloud,
            production_workflow_count=len(versions),
        )

    failures = {}
    for version in local_versions:
        deployment = version.validated_deployment
        if (
            deployment is None
            or deployment.status != ModelDeployment.Status.ACTIVE
            or deployment.profile_version_id != version.model_profile_version_id
        ):
            failures[str(version.id)] = "validated_deployment_unavailable"
            continue
        try:
            operation = declared_workflow_operation(version)
            verify_published_workflow_capability(
                version,
                deployment,
                operation=operation,
            )
            driver = driver_for(version.provider)
            health = driver.invoke(deployment, "health", {})
            object_info = driver.invoke(deployment, "object_info", {})
            validate_comfyui_api_contract(version.graph, object_info)
            if not isinstance(health, dict):
                raise ProviderDriverError("invalid health response")
        except ValidationError as exc:
            failures[str(version.id)] = str(
                getattr(exc, "code", None) or "workflow_capability_contract_invalid",
            )
        except (
            ProviderDriverError,
            ComfyUIContractError,
            ComfyUIContractUnavailable,
            ValueError,
            TypeError,
        ):
            failures[str(version.id)] = "live_contract_unavailable"
    return _result(
        not failures,
        "数据库 production 工作流能力可用" if not failures else "production 工作流实时能力不可用",
        local_required=True,
        cloud_available=cloud,
        production_workflow_count=len(versions),
        failures=failures,
    )


def _text_generation_worker_required(
    *,
    runtime_values: object = _RUNTIME_VALUES_UNSET,
    requirements: dict[str, object] | None = None,
) -> bool:
    if not _runtime_values_or_raise(runtime_values)["text_generation_worker_required"]:
        return False
    if requirements is not None:
        # The capability collector uses the same published recipe snapshot.
        # Treating any declared AI text route as requiring the worker is at
        # least as strict as the former per-recipe validator, while avoiding a
        # second full validation pass over the same immutable definitions.
        return bool(requirements["text_deployments"])
    from studio.services.production_runs import validate_production_recipe_executability

    for recipe in ProductionRecipeVersion.objects.filter(
        status=ProductionRecipeVersion.Status.PUBLISHED,
    ).iterator():
        try:
            stages = validate_production_recipe_executability(recipe)
        except Exception:
            continue
        if any(
            item["key"] in _TEXT_STAGE_OPERATIONS
            and item["mode"] == "ai"
            for item in stages
        ):
            return True
    return False


def _text_provider_credentials_check() -> dict:
    deployment_ids: set[str] = set()
    for recipe in ProductionRecipeVersion.objects.filter(
        status=ProductionRecipeVersion.Status.PUBLISHED,
    ).iterator():
        definition = recipe.definition if isinstance(recipe.definition, dict) else {}
        for stage in definition.get("stages") or []:
            if not isinstance(stage, dict) or not (
                stage.get("key") in _TEXT_STAGE_OPERATIONS
                and stage.get("mode") == "ai"
            ):
                continue
            routing = stage.get("routing") if isinstance(stage.get("routing"), dict) else {}
            for route in routing.get("routes") or []:
                if isinstance(route, dict) and route.get("model_deployment_id"):
                    deployment_ids.add(str(route["model_deployment_id"]))
    if not deployment_ids:
        return _result(True, "没有已发布 AI 剧本配方需要文本凭据", required=False)
    failures = {}
    deployments = ModelDeployment.objects.select_related(
        "profile_version__provider",
    ).filter(pk__in=deployment_ids)
    found = {str(item.id) for item in deployments}
    for missing in sorted(deployment_ids - found):
        failures[missing] = "deployment_missing"
    for deployment in deployments:
        provider = deployment.profile_version.provider
        reference = deployment.credential or provider.credential
        if provider.driver_type == provider.DriverType.LOCAL_QWEN and not reference:
            continue
        if not reference:
            failures[str(deployment.id)] = "credential_missing"
            continue
        try:
            resolve_credential(reference)
        except ProviderDriverError as exc:
            failures[str(deployment.id)] = exc.code
    return _result(
        not failures,
        "AI 剧本文本凭据可解析" if not failures else "AI 剧本文本凭据后端未就绪",
        required=True,
        failures=failures,
    )


def _requirements_from_stages(stages: list[dict]) -> dict[str, object]:
    requirements: dict[str, object] = {
        "text_deployments": set(),
        "text_operations": {},
        "image_routes": [],
        "video_routes": [],
        "assembly": False,
    }
    for stage in stages:
        if not isinstance(stage, dict) or stage.get("mode") == "skip":
            continue
        key = str(stage.get("key") or "")
        if key == "assembly":
            requirements["assembly"] = True
            continue
        routing = stage.get("routing") if isinstance(stage.get("routing"), dict) else {}
        bible_route_group = object() if key == BIBLE_IMAGE_GENERATION_STAGE_KEY else None
        for route in routing.get("routes") or []:
            if not isinstance(route, dict) or not route.get("model_deployment_id"):
                continue
            if key in _TEXT_STAGE_OPERATIONS:
                deployment_id = str(route["model_deployment_id"])
                requirements["text_deployments"].add(deployment_id)
                operation = _TEXT_STAGE_OPERATIONS[key]
                requirements["text_operations"].setdefault(deployment_id, set()).add(operation)
            elif key == "media_generation":
                requirements["image_routes"].append(route)
                requirements["video_routes"].append(route)
            elif key == BIBLE_IMAGE_GENERATION_STAGE_KEY:
                requirements["image_routes"].append({
                    **route,
                    # Bible routes form one exact-selection group: the UI and
                    # create path require one binding that is valid for every
                    # route in this stage, not merely a generic image profile.
                    "_bible_route_group": bible_route_group,
                })
    return requirements


def _published_recipe_requirements() -> dict[str, object]:
    requirements: dict[str, object] = {
        "text_deployments": set(),
        "text_operations": {},
        "image_routes": [],
        "video_routes": [],
        "assembly": False,
    }
    for recipe in ProductionRecipeVersion.objects.filter(
        status=ProductionRecipeVersion.Status.PUBLISHED,
    ).iterator():
        definition = recipe.definition if isinstance(recipe.definition, dict) else {}
        current = _requirements_from_stages(definition.get("stages") or [])
        requirements["text_deployments"].update(current["text_deployments"])
        for deployment_id, operations in current["text_operations"].items():
            requirements["text_operations"].setdefault(deployment_id, set()).update(operations)
        requirements["image_routes"].extend(current["image_routes"])
        requirements["video_routes"].extend(current["video_routes"])
        requirements["assembly"] = bool(requirements["assembly"] or current["assembly"])
    return requirements


def _media_capability_readiness(
    routes: list[dict],
    *,
    modality: str,
    runtime_values: object = _RUNTIME_VALUES_UNSET,
) -> dict:
    if not routes:
        return {"state": "not_required", "required": False, "reason_codes": []}
    workflow_ids = {
        str(item.get("workflow_version_id")) for item in routes if item.get("workflow_version_id")
    }
    workflows = {
        str(item.id): item
        for item in WorkflowTemplateVersion.objects.select_related("template").filter(
            pk__in=workflow_ids,
        )
        if item.template.output_kind == modality
    }
    deployment_ids = {str(item.get("model_deployment_id")) for item in routes}
    deployments = {
        str(item.id): item
        for item in ModelDeployment.objects.select_related(
            "runtime_host", "profile_version__provider",
        ).filter(pk__in=deployment_ids)
    }
    async_operation = {
        "image": "image.text_to_image",
        "video": "video.text_to_video",
        "audio": "audio.text_to_audio",
    }.get(modality)
    if async_operation is None:
        return None
    relevant = []
    invalid_model_profile_deployment_ids: set[str] = set()
    for route in routes:
        deployment = deployments.get(str(route.get("model_deployment_id")))
        if deployment and deployment.provider.driver_type == ProviderProfile.DriverType.ASYNC_REST:
            try:
                specification = verified_model_profile_specification(
                    deployment.profile_version,
                )
            except ValidationError:
                invalid_model_profile_deployment_ids.add(str(deployment.id))
                relevant.append(route)
                continue
            capabilities = specification.get("capabilities")
            if isinstance(capabilities, list) and async_operation in capabilities:
                relevant.append(route)
        elif str(route.get("workflow_version_id")) in workflows:
            relevant.append(route)
    if not relevant:
        return {"state": "not_required", "required": False, "reason_codes": []}

    def compatible_choice_ids(
        route: dict,
        deployment: ModelDeployment,
        prompt_mode: str,
    ) -> set[str]:
        try:
            model_family = model_family_for_profile(deployment.profile_version)
        except ValidationError:
            invalid_model_profile_deployment_ids.add(str(deployment.id))
            return set()
        choices = compatible_prompt_profile_bindings(
            operation=f"{modality}.generate",
            model_family=model_family,
            mode=prompt_mode,
            model_profile_version=deployment.profile_version,
        )
        if route.get("_bible_route_group") is None:
            return {str(item.binding.id) for item in choices}
        bible_choices = set()
        for item in choices:
            try:
                validate_bible_image_prompt_profile(item.profile_version)
            except ValidationError:
                continue
            bible_choices.add(str(item.binding.id))
        return bible_choices

    bible_group_choice_ids: dict[int, set[str]] = {}
    for route in relevant:
        group = route.get("_bible_route_group")
        if group is None:
            continue
        group_key = id(group)
        deployment = deployments.get(str(route.get("model_deployment_id")))
        route_choices = (
            compatible_choice_ids(route, deployment, "")
            if deployment is not None
            else set()
        )
        if group_key in bible_group_choice_ids:
            bible_group_choice_ids[group_key].intersection_update(route_choices)
        else:
            bible_group_choice_ids[group_key] = route_choices
    reasons: set[str] = set()
    ready_count = 0
    viable_async_routes = 0
    viable_comfy_routes = 0
    for route in relevant:
        deployment = deployments.get(str(route["model_deployment_id"]))
        if deployment is None:
            reasons.add("deployment_missing")
            continue
        if (
            deployment.status != ModelDeployment.Status.ACTIVE
            or deployment.profile_version.status != ModelProfileVersion.Status.PUBLISHED
            or not deployment.profile_version.provider.is_active
        ):
            reasons.add("deployment_inactive")
            continue
        try:
            model_specification = verified_model_profile_specification(
                deployment.profile_version,
            )
        except ValidationError:
            invalid_model_profile_deployment_ids.add(str(deployment.id))
            reasons.add("model_profile_contract_invalid")
            continue
        workflow = workflows.get(str(route.get("workflow_version_id")))
        prompt_modes = [""]
        if modality == "video":
            try:
                conditioning_contract = resolve_conditioning_contract(
                    workflow_input_contract=(
                        workflow.input_contract if workflow is not None else None
                    ),
                    model_specification=model_specification,
                    output_kind="video",
                    required=True,
                )
            except ValidationError:
                reasons.add("conditioning_contract_invalid")
                continue
            prompt_modes = [
                str(item["key"])
                for item in conditioning_contract["modes"]
            ]
        bible_group = route.get("_bible_route_group")
        prompt_choices_ready = (
            bool(bible_group_choice_ids.get(id(bible_group), set()))
            if bible_group is not None
            else all(
                bool(compatible_choice_ids(route, deployment, prompt_mode))
                for prompt_mode in prompt_modes
            )
        )
        if not prompt_choices_ready:
            reasons.add(
                "model_profile_contract_invalid"
                if str(deployment.id) in invalid_model_profile_deployment_ids
                else "prompt_profile_binding_missing_or_invalid"
            )
            continue
        if deployment.provider.driver_type == ProviderProfile.DriverType.ASYNC_REST:
            if fresh_smoke_snapshot(deployment, async_operation) is None:
                reasons.add("async_smoke_missing_or_stale")
                continue
            ready_count += 1
            viable_async_routes += 1
            continue
        if workflow is None:
            reasons.add("workflow_missing")
            continue
        try:
            workflow_operation = declared_workflow_operation(workflow)
            verify_published_workflow_capability(
                workflow,
                deployment,
                operation=workflow_operation,
            )
        except ValidationError as exc:
            reasons.add(str(
                getattr(exc, "code", None)
                or "workflow_capability_contract_invalid",
            ))
            continue
        if deployment.runtime_host_id is None:
            ready_count += 1
            viable_comfy_routes += 1
            continue
        snapshot = HostCapabilitySnapshot.objects.filter(
            host=deployment.runtime_host,
        ).order_by("-captured_at").first()
        if snapshot is None:
            reasons.add("host_snapshot_missing")
            continue
        if snapshot.expires_at and snapshot.expires_at <= timezone.now():
            reasons.add("host_snapshot_stale")
            continue
        try:
            artifacts = required_workflow_artifacts(workflow)
        except ValidationError:
            reasons.add("workflow_artifact_requirements_invalid")
            continue
        if any(item.status != item.Status.AVAILABLE for item in artifacts):
            reasons.add("artifact_not_verified")
            continue
        try:
            for artifact in artifacts:
                verified_artifact_availability(
                    snapshot=snapshot,
                    deployment=deployment,
                    artifact=artifact,
                )
        except ValidationError:
            reasons.add("artifact_observation_unverified")
            continue
        ready_count += 1
        viable_comfy_routes += 1
    comfy_worker = _worker_check(
        "generation-worker-heartbeat",
        "生成 Worker",
        required=viable_comfy_routes > 0,
        runtime_values=runtime_values,
    )
    async_worker = _worker_check(
        "async-media-worker-heartbeat",
        "异步媒体 Worker",
        required=viable_async_routes > 0,
        runtime_values=runtime_values,
    )
    if viable_comfy_routes and not comfy_worker["ok"]:
        reasons.add("generation_worker_unavailable")
    if viable_async_routes and not async_worker["ok"]:
        reasons.add("async_media_worker_unavailable")
    operational = bool(
        (viable_comfy_routes and comfy_worker["ok"])
        or (viable_async_routes and async_worker["ok"])
    )
    return {
        "state": "ready" if ready_count and operational else "blocked",
        "required": True,
        "route_count": len(relevant),
        "viable_route_count": ready_count,
        "reason_codes": sorted(reasons),
    }


def _capability_readiness_for_requirements(
    requirements: dict[str, object],
    *,
    runtime_values: object = _RUNTIME_VALUES_UNSET,
) -> dict:
    text_ids = requirements["text_deployments"]
    text_operations = requirements.get("text_operations") or {}
    if text_ids:
        text_worker = _worker_check(
            "text-generation-worker-heartbeat",
            "文本生成 Worker",
            required=True,
            runtime_values=runtime_values,
        )
        deployments = {
            str(item.id): item
            for item in ModelDeployment.objects.select_related(
                "profile_version__provider", "credential", "profile_version__provider__credential",
            ).filter(pk__in=text_ids)
        }
        text_reasons: set[str] = set()
        viable_text = 0
        for deployment_id in sorted(text_ids):
            deployment = deployments.get(str(deployment_id))
            if deployment is None:
                text_reasons.add("deployment_missing")
                continue
            if (
                deployment.status != ModelDeployment.Status.ACTIVE
                or deployment.profile_version.status != ModelProfileVersion.Status.PUBLISHED
                or not deployment.profile_version.provider.is_active
            ):
                text_reasons.add("deployment_unavailable")
                continue
            required_operations = set(text_operations.get(deployment_id) or {
                SCRIPT_GENERATE_OPERATION,
            })
            operation_contract_invalid = False
            for operation in sorted(required_operations):
                try:
                    policy = (
                        validate_script_structure_model_profile(deployment.profile_version)
                        if operation == SCRIPT_STRUCTURE_OPERATION
                        else validate_visual_rules_model_profile(deployment.profile_version)
                        if operation == VISUAL_RULES_OPERATION
                        else validate_prompt_normalization_model_profile(deployment.profile_version)
                        if operation == PROMPT_NORMALIZE_OPERATION
                        else validate_script_generation_model_profile(deployment.profile_version)
                        if operation == SCRIPT_GENERATE_OPERATION
                        else None
                    )
                except ValidationError:
                    policy = None
                if policy is None:
                    text_reasons.add(_TEXT_PROFILE_CONTRACT_REASON_CODES.get(
                        operation,
                        "text_profile_contract_invalid",
                    ))
                    operation_contract_invalid = True
                try:
                    require_prompt_profile_binding(
                        operation=operation,
                        model_family=model_family_for_profile(deployment.profile_version),
                        model_profile_version=deployment.profile_version,
                    )
                except ValidationError:
                    text_reasons.add("prompt_profile_binding_missing_or_invalid")
                    operation_contract_invalid = True
            if operation_contract_invalid or not deployment_supports_script_generation(deployment):
                if not operation_contract_invalid:
                    text_reasons.add("script_profile_contract_invalid")
                continue
            reference = deployment.credential or deployment.profile_version.provider.credential
            if not (
                deployment.profile_version.provider.driver_type
                == ProviderProfile.DriverType.LOCAL_QWEN and reference is None
            ):
                try:
                    if reference is None:
                        raise ProviderDriverError("credential missing", code="credential_missing")
                    resolve_credential(reference)
                except ProviderDriverError:
                    text_reasons.add("credential_unavailable")
                    continue
            missing_smoke = [
                operation for operation in sorted(required_operations)
                if fresh_smoke_snapshot(deployment, operation) is None
            ]
            if missing_smoke:
                for operation in missing_smoke:
                    text_reasons.add(_TEXT_SMOKE_REASON_CODES.get(
                        operation,
                        "text_smoke_missing_or_stale",
                    ))
                continue
            viable_text += 1
        if not text_worker["ok"]:
            text_reasons.add("text_worker_unavailable")
        text = {
            "state": "ready" if viable_text == len(text_ids) and not text_reasons else "blocked",
            "required": True,
            "deployment_count": len(text_ids),
            "viable_deployment_count": viable_text,
            "reason_codes": sorted(text_reasons),
        }
    else:
        text = {"state": "not_required", "required": False, "reason_codes": []}
    image = _media_capability_readiness(
        requirements["image_routes"],
        modality="image",
        runtime_values=runtime_values,
    )
    video = _media_capability_readiness(
        requirements["video_routes"],
        modality="video",
        runtime_values=runtime_values,
    )
    if requirements["assembly"]:
        toolchain = _toolchain_check()
        worker = _worker_check(
            "assembly-worker-heartbeat",
            "组装 Worker",
            required=True,
            runtime_values=runtime_values,
        )
        assembly_reasons = []
        if not toolchain["ok"]:
            assembly_reasons.append("toolchain_unavailable")
        if not worker["ok"]:
            assembly_reasons.append("assembly_worker_unavailable")
        assembly = {
            "state": "ready" if not assembly_reasons else "blocked",
            "required": True,
            "reason_codes": assembly_reasons,
        }
    else:
        assembly = {"state": "not_required", "required": False, "reason_codes": []}
    return {
        "schema_version": 1,
        "text": text,
        "image": image,
        "video": video,
        "assembly": assembly,
    }


def collect_capability_readiness(
    *,
    requirements: dict[str, object] | None = None,
    runtime_values: object = _RUNTIME_VALUES_UNSET,
) -> dict:
    with security_boundary_resolution_cache():
        return _capability_readiness_for_requirements(
            requirements or _published_recipe_requirements(),
            runtime_values=runtime_values,
        )


def collect_production_readiness() -> dict:
    with security_boundary_resolution_cache():
        return _collect_production_readiness()


def _collect_production_readiness() -> dict:
    """Return a redacted, per-published-recipe production closure report."""

    from studio.services.production_runs import validate_production_recipe_executability

    recipes = list(ProductionRecipeVersion.objects.filter(
        status=ProductionRecipeVersion.Status.PUBLISHED,
    ).order_by("recipe_key", "version"))
    if not recipes:
        return {
            "schema_version": 1,
            "status": "unconfigured",
            "reason_codes": ["published_recipe_missing"],
            "recipes": [],
        }
    rows = []
    overall_reasons: set[str] = set()
    for recipe in recipes:
        try:
            stages = validate_production_recipe_executability(
                recipe,
                require_delivery_closure=True,
            )
        except Exception as exc:
            text = str(exc)
            reason = (
                "visual_rules_contract_invalid"
                if "visual_rules_generate" in text or "visual_rules.generate" in text
                else "script_structure_contract_invalid"
                if "script_structure" in text or "script.structure" in text
                else "script_generate_contract_invalid"
                if "script_generate" in text or "script.generate" in text
                else "recipe_contract_invalid"
            )
            capabilities = {}
            state = "blocked"
            reasons = [reason]
        else:
            capability_report = _capability_readiness_for_requirements(
                _requirements_from_stages(stages),
            )
            capabilities = {
                key: {
                    "state": value["state"],
                    "required": bool(value["required"]),
                    "reason_codes": list(value.get("reason_codes") or []),
                }
                for key, value in capability_report.items()
                if key != "schema_version"
            }
            blocked = [
                value for value in capabilities.values()
                if value["required"] and value["state"] != "ready"
            ]
            state = "blocked" if blocked else "ready"
            reasons = sorted({
                reason
                for value in blocked
                for reason in value["reason_codes"]
            })
            if blocked and not reasons:
                reasons = ["required_capability_unavailable"]
        overall_reasons.update(reasons)
        rows.append({
            "id": str(recipe.id),
            "recipe_key": recipe.recipe_key,
            "version": recipe.version,
            "state": state,
            "reason_codes": reasons,
            "capabilities": capabilities,
        })
    status = "ready" if all(item["state"] == "ready" for item in rows) else "blocked"
    return {
        "schema_version": 1,
        "status": status,
        "reason_codes": sorted(overall_reasons),
        "recipes": rows,
    }


def _safe_runtime_diagnostic(callback, message: str) -> dict:
    try:
        return callback()
    except Exception:
        return _result(False, message)


def collect_readiness() -> dict:
    with security_boundary_resolution_cache():
        return _collect_readiness()


def _collect_readiness() -> dict:
    database = _database_check()
    migrations = _migration_check()
    try:
        runtime_values: object = _readiness_runtime_values()
    except Exception as exc:
        # Preserve the original fail-closed diagnostics while ensuring every
        # downstream check observes the same resolution failure rather than
        # re-querying a broken configuration repeatedly.
        runtime_values = exc
    checks = {
        "database": database,
        "migrations": migrations,
        "storage": _storage_check(runtime_values=runtime_values),
    }
    if not database["ok"] or not migrations["ok"]:
        checks.update({
            "configuration": _result(False, "数据库状态未就绪，未执行配置核对"),
            "access_control": _result(False, "数据库状态未就绪，未执行权限治理核对"),
            "secret_store": _result(False, "数据库状态未就绪，未执行受管凭据核对"),
        })
        blocked = {
            "state": "blocked", "required": True,
            "reason_codes": ["database_or_migrations_unavailable"],
        }
        return {
            "status": "not_ready",
            "service": "autoprompt-studio",
            "version": "1.0.0",
            "checks": checks,
            "diagnostics": {},
            "capabilities": {
                "schema_version": 1,
                "text": dict(blocked), "image": dict(blocked),
                "video": dict(blocked), "assembly": dict(blocked),
            },
        }
    checks.update({
        "configuration": _configuration_check(),
        "access_control": _access_control_check(),
        "secret_store": _secret_store_check(),
    })
    try:
        requirements = _published_recipe_requirements()
    except Exception:
        requirements = None
    diagnostics = {
        "toolchain": _safe_runtime_diagnostic(_toolchain_check, "无法核对媒体工具链"),
        "comfyui_output": _safe_runtime_diagnostic(
            lambda: _comfyui_output_check(runtime_values=runtime_values),
            "无法核对 ComfyUI 输出目录",
        ),
        "generation_worker": _safe_runtime_diagnostic(
            lambda: _worker_check(
                "generation-worker-heartbeat",
                "生成 Worker",
                required=_runtime_values_or_raise(runtime_values)[
                    "generation_worker_required"
                ],
                runtime_values=runtime_values,
            ),
            "无法核对生成 Worker",
        ),
        "text_generation_worker": _safe_runtime_diagnostic(
            lambda: _worker_check(
                "text-generation-worker-heartbeat",
                "文本生成 Worker",
                required=_text_generation_worker_required(
                    runtime_values=runtime_values,
                    requirements=requirements,
                ),
                runtime_values=runtime_values,
            ),
            "无法核对文本生成 Worker",
        ),
        "text_provider_credentials": _safe_runtime_diagnostic(
            _text_provider_credentials_check, "无法核对文本凭据",
        ),
        "assembly_worker": _safe_runtime_diagnostic(
            lambda: _worker_check(
                "assembly-worker-heartbeat",
                "组装 Worker",
                required=_runtime_values_or_raise(runtime_values)[
                    "assembly_worker_required"
                ],
                runtime_values=runtime_values,
            ),
            "无法核对组装 Worker",
        ),
    }
    ready = all(item["ok"] for item in checks.values())
    try:
        capabilities = collect_capability_readiness(
            requirements=requirements,
            runtime_values=runtime_values,
        )
    except Exception:
        capabilities = {
            "schema_version": 1,
            **{
                key: {
                    "state": "blocked", "required": True,
                    "reason_codes": ["readiness_evaluation_failed"],
                }
                for key in ("text", "image", "video", "assembly")
            },
        }
    return {
        "status": "ready" if ready else "not_ready",
        "service": "autoprompt-studio",
        "version": "1.0.0",
        "checks": checks,
        "diagnostics": diagnostics,
        "capabilities": capabilities,
    }
