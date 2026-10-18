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
    ModelDeployment,
    ProductionRecipeVersion,
    SecretBinding,
    SecretVersion,
    WorkerLease,
)
from studio.services.configuration import ConfigurationResolutionError, resolve_configuration
from studio.services.distribution import DistributionError, distribution_configuration
from studio.services.provider_registry import ProviderDriverError, driver_for, resolve_credential
from studio.services.secret_store import SecretStoreError, load_keyring, resolve_secret_version
from studio.services.security_boundaries import security_boundary_resolution_cache
from studio.services.execution_catalog_readiness import OUTPUT_KINDS


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


def _text_generation_worker_required(
    *, runtime_values: object = _RUNTIME_VALUES_UNSET, requirements=None,
) -> bool:
    if not _runtime_values_or_raise(runtime_values)["text_generation_worker_required"]:
        return False
    current = requirements if requirements is not None else _published_recipe_requirements()
    # A damaged recipe cannot be reported as requiring no worker.
    return current.needs_text_worker or bool(current.invalid_recipe_ids)


def _text_provider_credentials_check(requirements=None) -> dict:
    from studio.services.execution_catalog_readiness import worker_kind
    current = requirements if requirements is not None else _published_recipe_requirements()
    failures, checked = {}, set()
    for version, definition in current.versions.values():
        if worker_kind(definition) != "text_generation" or version.deployment_id in checked:
            continue
        deployment = version.deployment
        checked.add(deployment.pk)
        try:
            driver_for(deployment.provider).validate_execution_credentials(deployment)
        except (ValidationError, ProviderDriverError):
            failures[str(deployment.pk)] = "execution_transport_unavailable"
    if current.invalid_recipe_ids:
        failures["recipe"] = "recipe_contract_invalid"
    return _result(not failures, "精确实例文本凭据可解析" if not failures else "精确实例文本凭据或引用不可核验",
        required=bool(checked or current.invalid_recipe_ids), failures=failures)


def _requirements_from_stages(stages):
    from studio.services.execution_catalog_readiness import execution_requirements
    return execution_requirements(stages)


def _published_recipe_requirements():
    from studio.services.execution_catalog_readiness import published_execution_requirements
    return published_execution_requirements()


def _capability_readiness_for_requirements(requirements, *, runtime_values: object = _RUNTIME_VALUES_UNSET):
    from studio.services.execution_catalog_readiness import catalog_capability_readiness
    def worker(kind):
        label = {"generation": "生成 Worker", "text_generation": "文本 Worker",
            "async_media": "异步媒体 Worker", "assembly": "装配 Worker"}[kind]
        return _worker_check(kind.replace("_", "-") + "-worker-heartbeat", label, required=True,
            runtime_values=runtime_values)
    return catalog_capability_readiness(requirements, worker_check=worker, toolchain_check=_toolchain_check)


def collect_capability_readiness(
    *,
    requirements=None,
    runtime_values: object = _RUNTIME_VALUES_UNSET,
) -> dict:
    with security_boundary_resolution_cache():
        return _capability_readiness_for_requirements(
            requirements if requirements is not None else _published_recipe_requirements(),
            runtime_values=runtime_values,
        )


def collect_production_readiness() -> dict:
    with security_boundary_resolution_cache():
        return _collect_production_readiness()


def _collect_production_readiness() -> dict:
    """Redacted config/combination/pipeline report, never a per-call admission."""
    from studio.services.production_runs import _recipe_stages, validate_production_recipe_executability
    from studio.services.production_recipe_contracts import recipe_validation_errors

    recipes = ProductionRecipeVersion.objects.filter(status=ProductionRecipeVersion.Status.PUBLISHED).only(
        "id", "recipe_key", "version", "definition", "definition_schema_version", "definition_size_bytes", "definition_sha256"
    ).order_by("recipe_key", "version")
    rows, overall_reasons = [], set()
    for recipe in recipes.iterator():
        capabilities, reasons, errors = {}, set(), []
        phase = "definition"
        try:
            stages = _recipe_stages(recipe)
            phase = "requirements"
            report = _capability_readiness_for_requirements(_requirements_from_stages(stages))
            capabilities = {key: value for key, value in report.items() if key != "schema_version"}
            reasons.update(code for value in capabilities.values() for code in value["reason_codes"])
            try:
                phase = "pipeline"
                validate_production_recipe_executability(recipe)
            except ValidationError as exc:
                reasons.add("recipe_contract_invalid")
                errors = recipe_validation_errors(exc, phase=phase, stages=stages)
        except ValidationError as exc:
            reasons.add("recipe_contract_invalid")
            errors = recipe_validation_errors(exc, phase=phase, stages=recipe.definition.get("stages", []) if isinstance(recipe.definition, dict) else ())
        except Exception:
            # Configuration/database failure is visible and redacted; do not
            # infer an operation from exception text or fall back to old facts.
            reasons.add("readiness_evaluation_failed")
        overall_reasons.update(reasons)
        rows.append({"id": str(recipe.pk), "recipe_key": recipe.recipe_key, "version": recipe.version,
            "state": "blocked" if reasons else "ready", "reason_codes": sorted(reasons), "capabilities": capabilities,
            "validation_errors": errors})
    return {"schema_version": 2, "request_checked": False,
        "status": "unconfigured" if not rows else "blocked" if overall_reasons else "ready",
        "reason_codes": ["published_recipe_missing"] if not rows else sorted(overall_reasons), "recipes": rows}


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
            "scope": "configuration_only", "request_checked": False,
            "state": "blocked", "required": True,
            "reason_codes": ["database_or_migrations_unavailable"], "warning_codes": [],
            "instance_count": None, "viable_instance_count": None,
        }
        return {
            "status": "not_ready",
            "service": "autoprompt-studio",
            "version": "1.0.0",
            "checks": checks,
            "diagnostics": {},
            "capabilities": {
                "schema_version": 2,
                **{kind: dict(blocked) for kind in (*OUTPUT_KINDS, "assembly")},
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
            lambda: _text_provider_credentials_check(requirements), "无法核对文本凭据",
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
            "schema_version": 2,
            **{
                key: {
                    "scope": "configuration_only", "request_checked": False,
                    "state": "blocked", "required": True,
                    "reason_codes": ["readiness_evaluation_failed"], "warning_codes": [],
                    "instance_count": None, "viable_instance_count": None,
                }
                for key in (*OUTPUT_KINDS, "assembly")
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
