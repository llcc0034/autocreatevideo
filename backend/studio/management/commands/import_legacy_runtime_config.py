from __future__ import annotations

import json
import os
import platform
from urllib.parse import urlparse

from django.core.exceptions import ValidationError
from django.core.management.base import BaseCommand, CommandError
from django.db import transaction

from studio.models import (
    ConfigDefinition,
    ConfigRevision,
    ModelDeployment,
    ModelProfileVersion,
    RuntimeHost,
    ProviderProfile,
    SecretBinding,
)
from studio.services.configuration import (
    ConfigurationResolutionError,
    create_config_revision,
    publish_config_revision,
    resolve_configuration,
    validate_config_value,
)
from studio.services.script_generation_contract import (
    SCRIPT_GENERATE_PROFILE_SCHEMA_VERSION,
    validate_script_generation_model_profile,
)
from studio.services.json_contracts import assign_json_contract
from studio.services.provider_registry import driver_for
from studio.services.secret_store import (
    create_secret_binding,
    resolve_secret,
    rotate_secret_binding,
    SecretStoreError,
)


class Command(BaseCommand):
    help = (
        "Preview or import legacy business settings into the versioned database "
        "configuration, media deployments and encrypted credential store. The default "
        "is dry-run and secret values are never printed."
    )

    def add_arguments(self, parser):
        parser.add_argument(
            "--apply",
            action="store_true",
            help="Persist the revision and deployments. Without this flag nothing is written.",
        )
        parser.add_argument(
            "--skip-deployments",
            action="store_true",
            help="Skip model profile/deployment migration; encrypted secrets are controlled separately.",
        )
        parser.add_argument(
            "--skip-secrets",
            action="store_true",
            help="Do not import recognized provider API keys into encrypted database storage.",
        )
        parser.add_argument(
            "--publish-config",
            action="store_true",
            help=(
                "Publish and activate the imported system revision. Without this explicit flag, "
                "--apply creates a review-required draft and never replaces current settings."
            ),
        )

    @staticmethod
    def _strict_boolean(value: object) -> bool:
        normalized = str(value).strip().casefold()
        if normalized in {"1", "true", "yes", "on"}:
            return True
        if normalized in {"0", "false", "no", "off"}:
            return False
        raise ValueError("boolean value must be one of true/false/1/0/yes/no/on/off")

    @staticmethod
    def _revision_values() -> dict:
        truthy = Command._strict_boolean
        mapping = {
            "ALLOW_PUBLIC_REGISTRATION": ("access.public_registration_enabled", truthy),
            "ASSET_UPLOAD_MAX_BYTES": ("artifact.upload_max_bytes", int),
            "ASSET_VIDEO_MAX_BYTES": ("artifact.video_upload_max_bytes", int),
            "ASSET_AUDIO_MAX_BYTES": ("artifact.audio_upload_max_bytes", int),
            "ASSET_TENANT_QUOTA_BYTES": ("artifact.tenant_quota_bytes", int),
            "ASSET_IMAGE_MAX_WIDTH": ("artifact.image_max_width", int),
            "ASSET_IMAGE_MAX_HEIGHT": ("artifact.image_max_height", int),
            "ASSET_IMAGE_MAX_PIXELS": ("artifact.image_max_pixels", int),
            "MEDIA_SIGNED_URL_MAX_AGE_SECONDS": ("artifact.signed_url_ttl_seconds", int),
            "TASK_WORKER_POLL_SECONDS": ("runtime.task_worker_poll_seconds", float),
            "TASK_REMOTE_POLL_SECONDS": ("runtime.task_remote_poll_seconds", float),
            "TASK_HEARTBEAT_SECONDS": ("runtime.task_heartbeat_seconds", float),
            "TASK_HEALTH_CHECK_INTERVAL_SECONDS": ("runtime.task_health_check_interval_seconds", float),
            "TASK_HEALTH_FAILURE_LIMIT": ("runtime.task_health_failure_limit", int),
            "TASK_REMOTE_MISSING_GRACE_SECONDS": ("runtime.task_remote_missing_grace_seconds", float),
            "TASK_CANCEL_GRACE_SECONDS": ("runtime.task_cancel_grace_seconds", float),
            "TASK_STALL_THRESHOLD_SECONDS": ("runtime.task_stall_threshold_seconds", float),
            "TASK_STALE_RECOVERY_INTERVAL_SECONDS": ("runtime.task_stale_recovery_interval_seconds", float),
            "ASSEMBLY_TIMEOUT_SECONDS": ("delivery.assembly_timeout_seconds", float),
            "ASSEMBLY_AI_LABEL_ENABLED": ("delivery.ai_label_enabled", truthy),
            "ASSEMBLY_AI_LABEL_TEXT": ("delivery.ai_label_text", str),
            "SCRIPT_BREAKDOWN_DEFAULT_SHOT_DURATION_MS": ("production.default_shot_duration_ms", int),
            "SCRIPT_BREAKDOWN_DEFAULT_FIRST_FRAME_CONTINUITY": ("production.default_first_frame_continuity", truthy),
            "SCRIPT_BREAKDOWN_DEFAULT_LAST_FRAME_CONTINUITY": ("production.default_last_frame_continuity", truthy),
            "SCRIPT_BREAKDOWN_AUTO_SPLIT_TRIGGER_CHARS": ("production.script_auto_split_trigger_chars", int),
            "SCRIPT_BREAKDOWN_AUTO_SPLIT_TARGET_CHARS": ("production.script_auto_split_target_chars", int),
            "TASK_GENERATION_TIMEOUT_SECONDS": ("production.task_timeout_seconds", int),
            "THROTTLE_ANON_RATE": ("access.throttle_anon_rate", str),
            "THROTTLE_USER_RATE": ("access.throttle_user_rate", str),
            "THROTTLE_REGISTRATION_RATE": ("access.throttle_registration_rate", str),
            "THROTTLE_ASSET_UPLOAD_RATE": ("access.throttle_asset_upload_rate", str),
            "H3_PROMPT_LANGUAGE_GATE": ("production.h3_prompt_language_gate", str),
        }
        known = set(ConfigDefinition.objects.filter(is_active=True).values_list("key", flat=True))
        values = {}
        missing_definitions = []
        for env_key, (config_key, converter) in mapping.items():
            if env_key not in os.environ:
                continue
            if config_key not in known:
                missing_definitions.append(f"{env_key}->{config_key}")
                continue
            try:
                values[config_key] = converter(os.environ[env_key])
            except (TypeError, ValueError) as exc:
                raise CommandError(f"{env_key} 无法转换为 {config_key} 的正式类型") from exc
        aliases = (
            (("TASK_MAX_PENDING", "TASK_RECOMMENDED_MAX_PENDING"),
             "production.recommended_max_pending", int),
            (("TASK_MAX_ACTIVE_PER_USER", "TASK_RECOMMENDED_MAX_ACTIVE_PER_USER"),
             "production.recommended_max_active_per_user", int),
        )
        for env_keys, config_key, converter in aliases:
            supplied = []
            for env_key in env_keys:
                if env_key not in os.environ:
                    continue
                try:
                    supplied.append((env_key, converter(os.environ[env_key])))
                except (TypeError, ValueError) as exc:
                    raise CommandError(f"{env_key} 无法转换为 {config_key} 的正式类型") from exc
            if not supplied:
                continue
            if len({value for _, value in supplied}) != 1:
                raise CommandError(
                    f"{config_key} 的 legacy 与 recommended 环境变量值冲突；请先人工确认"
                )
            if config_key not in known:
                missing_definitions.append(
                    "|".join(key for key, _ in supplied) + f"->{config_key}"
                )
            else:
                values[config_key] = supplied[0][1]
        if missing_definitions:
            raise CommandError(
                "缺少正式 ConfigDefinition；请先应用数据库迁移: "
                + ", ".join(sorted(missing_definitions))
            )
        return values

    @staticmethod
    def _plan_deadline_pair(values: dict) -> tuple[dict, list[dict]]:
        """Keep an imported production hard timeout publishable and reviewable.

        The legacy environment has no independent idle-timeout setting. When
        its hard timeout is lower than the currently active idle timeout, the
        importer must therefore either plan both values together or reject the
        import. The synthesized idle value is returned as an explicit plan row
        and is never hidden from dry-run/apply output.
        """
        hard_key = "production.task_timeout_seconds"
        idle_key = "runtime.task_idle_timeout_seconds"
        if hard_key not in values:
            return dict(values), []
        planned = dict(values)
        try:
            resolved = resolve_configuration(required_keys=(idle_key,))
        except ConfigurationResolutionError as exc:
            raise CommandError(
                f"无法规划 legacy 任务时限：当前 {idle_key} 不可解析"
            ) from exc
        current_idle = resolved.values[idle_key]
        hard_timeout = planned[hard_key]
        if float(current_idle) <= float(hard_timeout):
            return planned, []
        try:
            idle_definition = ConfigDefinition.objects.get(key=idle_key, is_active=True)
            validate_config_value(idle_definition, hard_timeout)
        except (ConfigDefinition.DoesNotExist, ValidationError) as exc:
            raise CommandError(
                f"legacy {hard_key}={hard_timeout} 小于当前 {idle_key}={current_idle}，"
                "且无法生成满足正式 idle 取值范围的受控联动草稿；请先人工调整 legacy hard timeout"
            ) from exc
        planned[idle_key] = hard_timeout
        source = resolved.sources.get(idle_key) or {}
        return planned, [{
            "reason_code": "legacy_hard_timeout_below_current_idle",
            "constraint": f"{idle_key} <= {hard_key}",
            "config_key": idle_key,
            "from": current_idle,
            "to": hard_timeout,
            "source_scope": source.get("scope"),
            "action": "include_in_same_reviewed_revision",
        }]

    @staticmethod
    def _deployment_plan() -> list[dict]:
        base_url = str(os.environ.get("COMFYUI_BASE_URL") or "").strip()
        if not base_url:
            return []
        try:
            timeout_seconds = float(os.environ.get("COMFYUI_TIMEOUT_SECONDS") or "30")
        except ValueError as exc:
            raise CommandError("COMFYUI_TIMEOUT_SECONDS 无法转换为正式类型") from exc
        return [
            {
                "key": "legacy-comfyui-h3",
                "profile_key": "minimax-h3-video",
                "name": "Imported ComfyUI H3",
                "runtime_config": {
                    "base_url": base_url,
                    "timeout_seconds": timeout_seconds,
                },
                "is_default": True,
            },
            {
                "key": "legacy-comfyui-sdxl",
                "profile_key": "sdxl-image",
                "name": "Imported ComfyUI SDXL",
                "runtime_config": {
                    "base_url": base_url,
                    "timeout_seconds": timeout_seconds,
                },
                "is_default": True,
            },
        ]

    @staticmethod
    def _text_profile_requested() -> bool:
        return any(str(os.environ.get(key) or "").strip() for key in (
            "DEEPSEEK_BASE_URL", "DEEPSEEK_MODEL", "DEEPSEEK_API_KEY",
            "DEEPSEEK_TIMEOUT_SECONDS", "DEEPSEEK_SERIES_CONCEPT_TIMEOUT_SECONDS",
            "DEEPSEEK_MAX_TOKENS", "DEEPSEEK_SERIES_CONCEPT_MAX_TOKENS",
        ))

    @staticmethod
    def _deepseek_contract() -> tuple[dict, str, dict]:
        base_url = str(os.environ.get("DEEPSEEK_BASE_URL") or "").strip()
        model_identifier = str(os.environ.get("DEEPSEEK_MODEL") or "").strip()
        if not base_url or not model_identifier:
            raise CommandError(
                "DeepSeek 迁移需要同时提供 DEEPSEEK_BASE_URL 与 DEEPSEEK_MODEL；"
                "禁止用隐式默认值伪造正式 deployment 合同。"
            )
        try:
            timeout_seconds = float(
                os.environ.get("DEEPSEEK_SERIES_CONCEPT_TIMEOUT_SECONDS")
                or os.environ.get("DEEPSEEK_TIMEOUT_SECONDS")
                or "180"
            )
            max_output_tokens = int(os.environ.get("DEEPSEEK_SERIES_CONCEPT_MAX_TOKENS")
                                    or os.environ.get("DEEPSEEK_MAX_TOKENS") or "16384")
        except ValueError as exc:
            raise CommandError("DeepSeek timeout/token 配置无法转换为正式类型") from exc
        if not 0.5 <= timeout_seconds <= 600:
            raise CommandError("DeepSeek 受控 transport timeout 必须在 0.5 到 600 秒之间")
        if max_output_tokens < 1:
            raise CommandError("DeepSeek max_output_tokens 必须大于 0")
        parsed = urlparse(base_url)
        if (
            parsed.scheme not in {"http", "https"} or not parsed.hostname
            or parsed.username or parsed.password or parsed.query or parsed.fragment
        ):
            raise CommandError("DEEPSEEK_BASE_URL 必须是无凭据、无 query/fragment 的 http(s) URL")
        if parsed.scheme != "https" and parsed.hostname not in {"localhost", "127.0.0.1", "::1"}:
            raise CommandError("公网 DeepSeek endpoint 必须使用 HTTPS")
        provider_config = {
            "runtime_source": "openai_compatible_endpoint",
            "base_url": base_url,
            "chat_path": "/v1/chat/completions",
            "timeout_seconds": timeout_seconds,
        }
        policy = resolve_configuration(required_keys=(
            "production.video_shot_min_duration_ms",
            "production.video_shot_max_duration_ms",
        )).values
        specification = {
            "schema_version": SCRIPT_GENERATE_PROFILE_SCHEMA_VERSION,
            "capabilities": ["script.generate"],
            "series_concept": {
                "minimum_shot_duration_ms": int(policy["production.video_shot_min_duration_ms"]),
                "maximum_shot_duration_ms": int(policy["production.video_shot_max_duration_ms"]),
                "max_output_tokens": max_output_tokens,
                "timeout_seconds": timeout_seconds,
            },
        }
        return provider_config, model_identifier, specification

    @staticmethod
    def _ensure_draft_profile(*, provider, profile_key: str, model_identifier: str,
                              specification: dict, modality: str) -> tuple[ModelProfileVersion, bool]:
        for candidate in ModelProfileVersion.objects.filter(
            profile_key=profile_key,
            status=ModelProfileVersion.Status.DRAFT,
        ).order_by("-version"):
            if (
                candidate.provider_id == provider.id
                and candidate.model_identifier == model_identifier
                and candidate.specification == specification
            ):
                return candidate, False
        next_version = (
            ModelProfileVersion.objects.filter(profile_key=profile_key)
            .order_by("-version").values_list("version", flat=True).first() or 0
        ) + 1
        profile = ModelProfileVersion(
            profile_key=profile_key,
            version=next_version,
            provider=provider,
            model_identifier=model_identifier,
            modality=modality,
            status=ModelProfileVersion.Status.DRAFT,
        )
        assign_json_contract(profile, "specification", specification)
        profile.full_clean()
        profile.save()
        return profile, True

    @transaction.atomic
    def handle(self, *args, **options):
        values, controlled_adjustments = self._plan_deadline_pair(self._revision_values())
        plan = [] if options["skip_deployments"] else self._deployment_plan()
        retired_keys = sorted(
            key for key in (
                "COMFYUI_CLIENT_ID",
                "DEEPSEEK_ENABLED",
                "DEEPSEEK_MAX_RETRIES",
                "DEEPSEEK_CREATIVE_BATCH_SHOTS",
                "H3_PROMPT_OPTIMIZER_ENABLED",
                "H3_PROMPT_OPTIMIZER_BASE_URL",
                "H3_PROMPT_OPTIMIZER_API_KEY",
                "H3_PROMPT_OPTIMIZER_MODEL",
                "H3_PROMPT_OPTIMIZER_ALLOW_NON_LOOPBACK",
                "H3_PROMPT_OPTIMIZER_TIMEOUT_SECONDS",
                "H3_PROMPT_OPTIMIZER_FAILURE_MODE",
                "QWEN_TRANSLATION_RUNTIME_PYTHON",
                "QWEN_TRANSLATION_DEVICE",
                "QWEN_CREATIVE_RUNTIME_PYTHON",
                "QWEN_CREATIVE_DEVICE",
                "QWEN_GPU_LAYERS",
            )
            if key in os.environ
        )
        config_changes = False
        if values:
            try:
                current_values = resolve_configuration(required_keys=tuple(values)).values
            except ConfigurationResolutionError:
                current_values = {}
            config_changes = any(
                current_values.get(key) != value for key, value in values.items()
            )
        review_required = []
        if config_changes and not options["publish_config"]:
            review_required.append("system_configuration_draft")
        if controlled_adjustments and not options["publish_config"]:
            review_required.append("deadline_pair_adjustment")
        if self._text_profile_requested() and not options["skip_deployments"]:
            review_required.append("deepseek_profile_and_deployment")
        if str(os.environ.get("MINIMAX_H3_MODEL") or "").strip() and not options["skip_deployments"]:
            review_required.append("minimax_h3_model_profile")
        preview = {
            "schema_version": 1,
            "mode": "apply" if options["apply"] else "dry-run",
            "config_keys": sorted(values),
            "deployments": [
                {
                    "key": item["key"],
                    "profile_key": item["profile_key"],
                    "credential_configured": False,
                    "runtime_config_fields": sorted(item["runtime_config"]),
                }
                for item in plan
            ],
            "manual_setup_required": [
                {
                    "capability": "prompt.normalize",
                    "reason_code": "legacy_text_endpoint_contract_missing",
                    "legacy_provider_types": ["local_qwen", "deepseek_api"],
                    "required_action": (
                        "Create and publish a v2 text profile declaring prompt.normalize, "
                        "bind it to an active loopback OpenAI-compatible deployment, then "
                        "run a real prompt.normalize smoke test."
                    ),
                }
            ],
            "recognized_secret_count": sum(
                1 for key in ("DEEPSEEK_API_KEY",)
                if bool(str(os.environ.get(key) or ""))
            ),
            "draft_model_profiles": (
                ["deepseek-text"]
                if self._text_profile_requested() and not options["skip_deployments"]
                else []
            ),
            "retired_keys": retired_keys,
            "controlled_adjustments": controlled_adjustments,
            "review_required": sorted(set(review_required)),
            # Environment entries are removable only after apply, verification,
            # and any generated drafts/deployments have been reviewed.
            "safe_to_remove": False,
        }
        self.stdout.write(json.dumps(preview, ensure_ascii=False, sort_keys=True))
        if not options["apply"]:
            self.stdout.write(self.style.WARNING("Dry-run only; no database rows were written."))
            return

        revision = None
        if config_changes:
            revision = create_config_revision(
                scope_type=ConfigRevision.ScopeType.SYSTEM,
                values=values,
                created_by=None,
                change_summary="Imported legacy business configuration from deployment environment",
                clone_published=True,
            )
            if options["publish_config"]:
                revision = publish_config_revision(revision, published_by=None)

        imported_secrets = 0
        if not options["skip_secrets"]:
            secret_plan = (("DEEPSEEK_API_KEY", "deepseek", "provider.deepseek.api-key"),)
            for env_key, provider_key, binding_key in secret_plan:
                material = str(os.environ.get(env_key) or "")
                if not material:
                    continue
                provider = ProviderProfile.objects.filter(key=provider_key).first()
                if provider is None:
                    raise CommandError(f"无法迁移 {env_key}：Provider {provider_key} 不存在")
                binding = SecretBinding.objects.filter(key=binding_key).first()
                if binding is None:
                    binding = create_secret_binding(
                        key=binding_key,
                        label=f"{provider.name} API credential",
                        purpose="provider_api",
                        secret_value=material,
                        actor=None,
                        reason="legacy_environment_import",
                    )
                else:
                    try:
                        current = resolve_secret(binding)
                    except SecretStoreError as exc:
                        raise CommandError(
                            f"无法验证已存在的受管凭据 {binding_key}: {exc.code}"
                        ) from exc
                    if current != material:
                        binding = rotate_secret_binding(
                            binding,
                            secret_value=material,
                            actor=None,
                            reason="legacy_environment_import",
                        )
                imported_secrets += 1

        created_draft_profiles = 0
        deepseek_profile = None
        deepseek_runtime_config = None
        if self._text_profile_requested() and not options["skip_deployments"]:
            provider = ProviderProfile.objects.filter(key="deepseek").first()
            if provider is None:
                raise CommandError("无法迁移 DeepSeek 配置：Provider deepseek 不存在")
            provider_config, model_identifier, specification = self._deepseek_contract()
            deepseek_runtime_config = provider_config
            deepseek_profile, created = self._ensure_draft_profile(
                provider=provider,
                profile_key="deepseek-text",
                model_identifier=model_identifier,
                specification=specification,
                modality=ModelProfileVersion.Modality.TEXT,
            )
            try:
                if validate_script_generation_model_profile(deepseek_profile) is None:
                    raise CommandError(
                        "无法迁移 DeepSeek 配置：生成的 profile 未声明 script.generate"
                    )
            except ValidationError as exc:
                raise CommandError("无法迁移 DeepSeek 配置：script.generate 合同无效") from exc
            created_draft_profiles += int(created)

        legacy_h3_model = str(os.environ.get("MINIMAX_H3_MODEL") or "").strip()
        if legacy_h3_model and not options["skip_deployments"]:
            current = ModelProfileVersion.objects.select_related("provider").filter(
                profile_key="minimax-h3-video",
                status=ModelProfileVersion.Status.PUBLISHED,
            ).order_by("-version").first()
            if current is None:
                raise CommandError("无法迁移 MINIMAX_H3_MODEL：缺少已发布 minimax-h3-video")
            if current.model_identifier != legacy_h3_model:
                _, created = self._ensure_draft_profile(
                    provider=current.provider,
                    profile_key=current.profile_key,
                    model_identifier=legacy_h3_model,
                    specification=current.specification,
                    modality=current.modality,
                )
                created_draft_profiles += int(created)

        host = None
        if plan:
            host, _ = RuntimeHost.objects.get_or_create(
                key="local",
                defaults={"name": platform.node() or "Local runtime host"},
            )
        created_deployments = 0
        for item in plan:
            profile = ModelProfileVersion.objects.select_related("provider").filter(
                profile_key=item["profile_key"],
                status=ModelProfileVersion.Status.PUBLISHED,
            ).order_by("-version").first()
            if profile is None:
                raise CommandError(f"Published model profile missing: {item['profile_key']}")
            existing = ModelDeployment.objects.filter(
                runtime_host=host,
                profile_version=profile,
                status=ModelDeployment.Status.ACTIVE,
            ).first()
            if existing is not None:
                if (existing.runtime_config or {}) == item["runtime_config"]:
                    self.stdout.write(
                        self.style.WARNING(
                            f"Verified existing active deployment {existing.key} "
                            f"for {item['profile_key']}."
                        )
                    )
                    continue
                review_key = f"{item['key']}-review"
                review = ModelDeployment.objects.filter(key=review_key).first()
                if review is not None:
                    if not (
                        review.status == ModelDeployment.Status.DISABLED
                        and review.runtime_host_id == host.id
                        and review.profile_version_id == profile.id
                        and review.runtime_config == item["runtime_config"]
                    ):
                        raise CommandError(
                            f"{review_key} 已存在但合同不同；禁止自动覆盖"
                        )
                    continue
                deployment = ModelDeployment(
                    key=review_key,
                    name=f"{item['name']} (review required)",
                    profile_version=profile,
                    runtime_host=host,
                    is_default=False,
                    status=ModelDeployment.Status.DISABLED,
                )
                assign_json_contract(deployment, "runtime_config", item["runtime_config"])
                driver_for(profile.provider).validate_config({
                    **(profile.provider.driver_config or {}),
                    **deployment.runtime_config,
                })
                deployment.full_clean()
                deployment.save()
                created_deployments += 1
                self.stdout.write(self.style.WARNING(
                    f"Active deployment {existing.key} differs; created disabled "
                    f"review candidate {review_key}."
                ))
                continue
            deployment = ModelDeployment(
                key=item["key"],
                name=item["name"],
                profile_version=profile,
                runtime_host=host,
                is_default=item["is_default"],
                status=ModelDeployment.Status.ACTIVE,
            )
            assign_json_contract(deployment, "runtime_config", item["runtime_config"])
            driver_for(profile.provider).validate_config({
                **(profile.provider.driver_config or {}),
                **deployment.runtime_config,
            })
            deployment.full_clean()
            deployment.save()
            created_deployments += 1

        if deepseek_profile is not None and deepseek_runtime_config is not None:
            credential = SecretBinding.objects.filter(key="provider.deepseek.api-key").first()
            existing = ModelDeployment.objects.filter(key="legacy-deepseek-review").first()
            if existing is None:
                deployment = ModelDeployment(
                    key="legacy-deepseek-review",
                    name="Imported DeepSeek (review required)",
                    profile_version=deepseek_profile,
                    credential=credential,
                    is_default=False,
                    status=ModelDeployment.Status.DISABLED,
                )
                assign_json_contract(deployment, "runtime_config", deepseek_runtime_config)
                deployment.full_clean()
                deployment.save()
                created_deployments += 1
            elif not (
                existing.status == ModelDeployment.Status.DISABLED
                and existing.profile_version_id == deepseek_profile.id
                and existing.runtime_config == deepseek_runtime_config
                and existing.credential_id == (credential.id if credential else None)
            ):
                raise CommandError(
                    "legacy-deepseek-review 已存在但合同不同；禁止自动覆盖，请在系统设置中人工处理"
                )

        revision_state = (
            f"{revision.status} revision {revision.revision}" if revision else "unchanged"
        )
        self.stdout.write(self.style.SUCCESS(
            f"System config {revision_state}; "
            f"created {created_deployments} model deployments; "
            f"created {created_draft_profiles} review-required model profile drafts; "
            f"imported {imported_secrets} encrypted credentials."
        ))
