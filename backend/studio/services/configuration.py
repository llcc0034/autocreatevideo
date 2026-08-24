from __future__ import annotations

import hashlib
import os
import re
import shutil
from dataclasses import dataclass
from pathlib import Path, PureWindowsPath
from typing import Any, Iterable

from django.conf import settings
from django.core.exceptions import PermissionDenied, ValidationError
from django.db import transaction
from django.db.models import Max
from django.utils import timezone

from studio.models import (
    ConfigDefinition,
    ConfigRevision,
    ConfigValue,
    ModelDeployment,
    ModelProfileVersion,
    ProductionRun,
    SeriesProject,
    WorkflowTemplateVersion,
    SecretBinding,
    SecretVersion,
)
from studio.services.json_contracts import (
    canonical_json_bytes,
    find_sensitive_json_keys,
    json_contract,
    verify_json_contract,
)
from studio.services.provider_registry import credential_reference_digest


class ConfigurationResolutionError(RuntimeError):
    def __init__(self, message: str, *, code: str, missing_keys: Iterable[str] = ()):
        super().__init__(message)
        self.code = code
        self.missing_keys = tuple(sorted(set(missing_keys)))


VIDEO_SHOT_DURATION_CONFIG_KEYS = (
    "production.default_shot_duration_ms",
    "production.video_shot_min_duration_ms",
    "production.video_shot_max_duration_ms",
)


@dataclass(frozen=True)
class ResolvedConfiguration:
    values: dict[str, Any]
    sources: dict[str, dict[str, Any]]
    revision_ids: tuple[str, ...]
    sha256: str

    def snapshot(self) -> dict[str, Any]:
        return {
            "schema_version": 1,
            "values": self.values,
            "sources": self.sources,
            "revision_ids": list(self.revision_ids),
            "sha256": self.sha256,
        }


def video_shot_duration_policy(
    *,
    values: dict[str, Any] | None = None,
    project: SeriesProject | None = None,
    run: ProductionRun | None = None,
) -> dict[str, int]:
    """Resolve and validate the database-owned video duration policy.

    ``values`` is used by ProductionRun callers so compilation consumes the
    immutable stage configuration snapshot instead of re-reading live state.
    Request-time callers omit it and resolve the active system/project policy.
    """

    if values is None:
        values = resolve_configuration(
            project=project,
            run=run,
            required_keys=VIDEO_SHOT_DURATION_CONFIG_KEYS,
        ).values
    missing = [key for key in VIDEO_SHOT_DURATION_CONFIG_KEYS if key not in values]
    if missing:
        raise ConfigurationResolutionError(
            f"缺少必需配置: {', '.join(missing)}",
            code="required_config_missing",
            missing_keys=missing,
        )
    raw_default = values["production.default_shot_duration_ms"]
    raw_minimum = values["production.video_shot_min_duration_ms"]
    raw_maximum = values["production.video_shot_max_duration_ms"]
    if any(type(item) is not int for item in (raw_default, raw_minimum, raw_maximum)):
        raise ConfigurationResolutionError(
            "视频分镜时长配置必须为整数毫秒",
            code="production_config_invalid",
        )
    default = int(raw_default)
    minimum = int(raw_minimum)
    maximum = int(raw_maximum)
    if not 100 <= minimum <= default <= maximum:
        raise ConfigurationResolutionError(
            "视频分镜默认时长必须位于有效的最小/最大时长范围内",
            code="production_config_invalid",
        )
    return {
        "default_duration_ms": default,
        "minimum_duration_ms": minimum,
        "maximum_duration_ms": maximum,
    }


def redact_configuration_snapshot(snapshot: dict[str, Any]) -> dict[str, Any]:
    """Return an API-safe copy without credential binding identifiers."""
    payload = {
        **snapshot,
        "values": dict(snapshot.get("values") or {}),
        "sources": dict(snapshot.get("sources") or {}),
    }
    for key, value in list(payload["values"].items()):
        if isinstance(value, dict) and "credential_binding_sha256" in value:
            payload["values"][key] = {"credential_configured": True}
    return payload


def redact_sensitive_bindings(value: Any) -> Any:
    """Defensively remove credential binding identifiers from API snapshots."""

    if isinstance(value, list):
        return [redact_sensitive_bindings(item) for item in value]
    if not isinstance(value, dict):
        return value
    redacted = {}
    for key, item in value.items():
        normalized = str(key).casefold().replace("-", "_")
        if normalized == "usage" and isinstance(item, dict):
            redacted[key] = {
                field: raw
                for field, raw in item.items()
                if field in {
                    "prompt_tokens", "completion_tokens", "total_tokens",
                    "input_tokens", "output_tokens",
                }
                and isinstance(raw, int)
                and not isinstance(raw, bool)
                and 0 <= raw <= 1_000_000_000
            }
            continue
        if normalized in {"credential_id", "credential_version", "credential_ref"}:
            redacted["credential_configured"] = bool(item)
            continue
        if normalized == "credential_binding_sha256":
            redacted["credential_configured"] = bool(item)
            continue
        if normalized != "credential_configured" and find_sensitive_json_keys({str(key): None}):
            redacted[f"{key}_configured"] = bool(item)
            continue
        redacted[key] = redact_sensitive_bindings(item)
    return redacted


def _allowed_scopes(definition: ConfigDefinition) -> set[str]:
    return {
        item.strip()
        for item in str(definition.allowed_scopes or "").split(",")
        if item.strip()
    }


def _allowed_default_sources(definition: ConfigDefinition) -> set[str]:
    return {
        item.strip()
        for item in str(definition.allowed_default_sources or "").split(",")
        if item.strip()
    }


def validate_config_override_values(
    scope_type: str,
    values: dict[str, dict[str, Any] | Any],
) -> dict[str, Any]:
    """Validate write-time overrides without creating a revision.

    ProductionRun creation uses this before inserting its idempotency row, so
    malformed overrides cannot leave a partially initialized run behind.
    """

    if not isinstance(values, dict):
        raise ValidationError({"config_overrides": "必须是对象"})
    definitions = {
        row.key: row
        for row in ConfigDefinition.objects.filter(key__in=values, is_active=True)
    }
    unknown = sorted(set(values) - set(definitions))
    if unknown:
        raise ValidationError({"config_overrides": f"未知或已停用配置: {', '.join(unknown)}"})
    effective: dict[str, Any] = {}
    for key, raw in values.items():
        definition = definitions[key]
        if scope_type not in _allowed_scopes(definition):
            raise ValidationError({key: f"配置不允许在 {scope_type} scope 覆盖"})
        if scope_type != ConfigRevision.ScopeType.SYSTEM and definition.is_secret:
            raise ValidationError({key: "密钥配置只允许 system scope"})
        if scope_type != ConfigRevision.ScopeType.SYSTEM and definition.restart_required:
            raise ValidationError({key: "需要重启的配置只允许 system scope"})
        wrapped = isinstance(raw, dict) and bool(raw) and set(raw) <= {"value", "credential_id"}
        normalized = raw if wrapped else {"value": raw, "credential_id": None}
        value = normalized.get("value")
        credential_version = _credential_version(normalized.get("credential_id"))
        validate_config_value(definition, value, credential_version)
        effective[key] = value
    return effective


def _credential_version(raw: Any) -> SecretVersion | None:
    if raw in {None, ""}:
        return None
    if isinstance(raw, SecretVersion):
        return raw
    if isinstance(raw, SecretBinding):
        binding = SecretBinding.objects.select_related("active_version").get(pk=raw.pk)
    else:
        try:
            binding = SecretBinding.objects.select_related("active_version").get(pk=raw)
        except (SecretBinding.DoesNotExist, ValueError, TypeError) as exc:
            raise ValidationError({"credential_id": "受管凭据不存在"}) from exc
    if not binding.is_active or binding.active_version is None:
        raise ValidationError({"credential_id": "受管凭据未启用或尚无秘密版本"})
    return binding.active_version


def validate_config_value(
    definition: ConfigDefinition,
    value: Any,
    credential_version: SecretVersion | None = None,
    *,
    validate_resource_state: bool = True,
) -> None:
    if definition.is_secret:
        if value is not None:
            raise ValidationError({definition.key: "密钥配置只能绑定受管凭据，不能保存明文 value"})
        if credential_version is None:
            raise ValidationError({definition.key: "密钥配置必须绑定受管凭据"})
        return
    if credential_version is not None:
        raise ValidationError({definition.key: "非密钥配置不能绑定受管凭据"})
    sensitive = find_sensitive_json_keys({definition.key: value})
    if sensitive:
        raise ValidationError({
            definition.key: "非密钥配置不能包含 secret/token/password/api_key 等敏感字段",
        })

    expected = definition.value_type
    valid = {
        ConfigDefinition.ValueType.BOOLEAN: type(value) is bool,
        ConfigDefinition.ValueType.INTEGER: type(value) is int,
        ConfigDefinition.ValueType.NUMBER: type(value) in {int, float},
        ConfigDefinition.ValueType.STRING: type(value) is str,
        ConfigDefinition.ValueType.OBJECT: type(value) is dict,
        ConfigDefinition.ValueType.ARRAY: type(value) is list,
    }.get(expected, False)
    if not valid:
        raise ValidationError({definition.key: f"值类型必须为 {expected}"})

    schema = definition.value_schema if isinstance(definition.value_schema, dict) else {}
    if "enum" in schema and value not in schema["enum"]:
        raise ValidationError({definition.key: "值不在允许枚举中"})
    if type(value) in {int, float}:
        if "minimum" in schema and value < schema["minimum"]:
            raise ValidationError({definition.key: f"值不能小于 {schema['minimum']}"})
        if "maximum" in schema and value > schema["maximum"]:
            raise ValidationError({definition.key: f"值不能大于 {schema['maximum']}"})
    if isinstance(value, str):
        if "minLength" in schema and len(value) < int(schema["minLength"]):
            raise ValidationError({definition.key: "字符串长度不足"})
        if "maxLength" in schema and len(value) > int(schema["maxLength"]):
            raise ValidationError({definition.key: "字符串过长"})
        if schema.get("pattern") and re.fullmatch(str(schema["pattern"]), value) is None:
            raise ValidationError({definition.key: "字符串格式不符合约束"})
        resource_ref = schema.get("resource_ref")
        if resource_ref is not None:
            _validate_config_resource_reference(
                definition=definition,
                value=value,
                resource_ref=resource_ref,
                validate_resource_state=validate_resource_state,
            )
    if isinstance(value, (list, dict)) and "maxItems" in schema:
        if len(value) > int(schema["maxItems"]):
            raise ValidationError({definition.key: "集合元素数量超过上限"})


def _validate_config_resource_reference(
    *,
    definition: ConfigDefinition,
    value: str,
    resource_ref: Any,
    validate_resource_state: bool,
) -> None:
    """Validate a machine-readable dependency selected by configuration UI.

    The schema extension is intentionally closed and server-owned.  It keeps
    configuration values as ordinary immutable JSON while ensuring a UUID is
    not accepted merely because it has the right shape.
    """

    if not isinstance(resource_ref, dict):
        raise ValidationError({definition.key: "resource_ref 必须是对象"})
    resource = str(resource_ref.get("resource") or "").strip()
    capability = str(resource_ref.get("capability") or "").strip()
    allow_empty = resource_ref.get("allow_empty", False)
    require_ready = resource_ref.get("require_ready", True)
    if type(allow_empty) is not bool or type(require_ready) is not bool:
        raise ValidationError({definition.key: "resource_ref 的布尔约束无效"})
    if resource != "model_deployment" or not capability:
        raise ValidationError({
            definition.key: "当前仅支持声明 capability 的 model_deployment 资源引用",
        })
    if not value:
        if allow_empty:
            return
        raise ValidationError({definition.key: "必须选择一个可用的模型部署"})
    if not validate_resource_state:
        # Revision resolution is an integrity read, not a deployment health
        # gate.  The immutable JSON value and its checksum remain auditable
        # even after the referenced deployment is disabled or its smoke
        # evidence expires.  Consumers re-check the live route before use.
        return
    try:
        deployment = ModelDeployment.objects.select_related(
            "profile_version__provider",
        ).get(id=value)
    except (ModelDeployment.DoesNotExist, ValueError, TypeError):
        raise ValidationError({definition.key: "所选模型部署不存在"})

    if capability == "prompt.normalize":
        from studio.services.prompt_normalization import (
            ROUTE_CONTEXT_DURABLE_ATTEMPT,
            route_state,
        )

        state, reason_code, _route = route_state(
            deployment,
            execution_context=ROUTE_CONTEXT_DURABLE_ATTEMPT,
        )
        if require_ready and state != "ready":
            raise ValidationError({
                definition.key: f"所选模型部署尚不可用于 prompt.normalize: {reason_code}",
            })
        if not require_ready and state == "unavailable":
            raise ValidationError({
                definition.key: f"所选模型部署不兼容 prompt.normalize: {reason_code}",
            })
        return
    raise ValidationError({definition.key: f"尚未注册资源能力: {capability}"})


def config_resource_reference_options(definition: ConfigDefinition) -> list[dict[str, Any]]:
    """Return safe, server-validated choices for a resource_ref definition."""

    schema = definition.value_schema if isinstance(definition.value_schema, dict) else {}
    resource_ref = schema.get("resource_ref")
    if not isinstance(resource_ref, dict):
        return []
    resource = str(resource_ref.get("resource") or "").strip()
    capability = str(resource_ref.get("capability") or "").strip()
    require_ready = resource_ref.get("require_ready", True)
    if resource != "model_deployment" or not capability or type(require_ready) is not bool:
        raise ValidationError({definition.key: "resource_ref 定义无效"})

    if capability != "prompt.normalize":
        raise ValidationError({definition.key: f"尚未注册资源能力: {capability}"})
    from studio.services.prompt_normalization import (
        ROUTE_CONTEXT_DURABLE_ATTEMPT,
        route_state,
    )

    rows: list[dict[str, Any]] = []
    deployments = ModelDeployment.objects.select_related(
        "profile_version__provider",
    ).order_by("name", "key")
    for deployment in deployments:
        state, reason_code, _route = route_state(
            deployment,
            execution_context=ROUTE_CONTEXT_DURABLE_ATTEMPT,
        )
        rows.append({
            "value": str(deployment.id),
            "label": f"{deployment.name} · {deployment.profile_version.model_identifier}",
            "resource": resource,
            "capability": capability,
            "state": state,
            "reason_code": reason_code,
            "is_selectable": state == "ready" if require_ready else state != "unavailable",
        })
    return rows


def _scope_kwargs(
    scope_type: str,
    *,
    project: SeriesProject | None,
    run: ProductionRun | None,
) -> dict[str, Any]:
    if scope_type == ConfigRevision.ScopeType.SYSTEM:
        if project is not None or run is not None:
            raise ValidationError("system scope 不接受 project/run")
        return {"project": None, "run": None}
    if scope_type == ConfigRevision.ScopeType.PROJECT:
        if project is None or run is not None:
            raise ValidationError("project scope 必须且只能指定 project")
        return {"project": project, "run": None}
    if scope_type == ConfigRevision.ScopeType.RUN:
        if run is None or project is not None:
            raise ValidationError("run scope 必须且只能指定 run")
        return {"project": None, "run": run}
    raise ValidationError("未知配置作用域")


def _scope_queryset(scope_type: str, *, project=None, run=None):
    kwargs = _scope_kwargs(scope_type, project=project, run=run)
    return ConfigRevision.objects.filter(scope_type=scope_type, **kwargs)


def _revision_values_map(revision: ConfigRevision | None) -> dict[str, dict[str, Any]]:
    if revision is None:
        return {}
    return {
        row.definition.key: {
            "value": row.value,
            "credential_version": row.credential_version,
        }
        for row in revision.values.select_related(
            "definition", "credential_version__binding",
        ).all()
    }


@transaction.atomic
def create_config_revision(
    *,
    scope_type: str,
    values: dict[str, dict[str, Any] | Any],
    created_by,
    project: SeriesProject | None = None,
    run: ProductionRun | None = None,
    change_summary: str = "",
    clone_published: bool = True,
    unset_keys: Iterable[str] = (),
) -> ConfigRevision:
    scope_kwargs = _scope_kwargs(scope_type, project=project, run=run)
    scope_rows = _scope_queryset(scope_type, project=project, run=run).select_for_update()
    current = scope_rows.filter(
        status=ConfigRevision.Status.PUBLISHED,
        activation_state=ConfigRevision.ActivationState.ACTIVE,
    ).first()
    materialized = _revision_values_map(current) if clone_published else {}
    for key, raw in values.items():
        materialized[str(key)] = (
            raw if isinstance(raw, dict) and bool(raw) and set(raw) <= {
                "value", "credential_id", "credential_version"
            }
            else {"value": raw, "credential_version": None}
        )
    requested_unsets = {str(item) for item in unset_keys}
    unset_definitions = {
        item.key: item for item in ConfigDefinition.objects.filter(
            key__in=requested_unsets,
            is_active=True,
        )
    }
    unknown_unsets = sorted(requested_unsets - set(unset_definitions))
    if unknown_unsets:
        raise ValidationError({"unset_keys": f"未知或已停用配置: {', '.join(unknown_unsets)}"})
    for key in requested_unsets:
        definition = unset_definitions[key]
        if scope_type not in _allowed_scopes(definition):
            raise ValidationError({"unset_keys": f"配置 {key} 不允许在 {scope_type} scope 管理"})
        if scope_type != ConfigRevision.ScopeType.SYSTEM and (
            definition.is_secret or definition.restart_required
        ):
            raise ValidationError({"unset_keys": f"配置 {key} 只允许 system scope"})
        materialized.pop(key, None)

    definitions = {
        row.key: row
        for row in ConfigDefinition.objects.filter(
            key__in=materialized,
            is_active=True,
        )
    }
    unknown = sorted(set(materialized) - set(definitions))
    if unknown:
        raise ValidationError({"values": f"未知或已停用配置: {', '.join(unknown)}"})
    next_revision = (scope_rows.aggregate(value=Max("revision"))["value"] or 0) + 1
    revision = ConfigRevision.objects.create(
        scope_type=scope_type,
        revision=next_revision,
        status=ConfigRevision.Status.DRAFT,
        based_on_revision=current,
        created_by=created_by,
        change_summary=change_summary[:500],
        **scope_kwargs,
    )
    digest_values: dict[str, Any] = {}
    rows: list[ConfigValue] = []
    for key in sorted(materialized):
        definition = definitions[key]
        if scope_type not in _allowed_scopes(definition):
            raise ValidationError({key: f"配置不允许在 {scope_type} scope 覆盖"})
        if scope_type != ConfigRevision.ScopeType.SYSTEM and definition.is_secret:
            raise ValidationError({key: "密钥配置只允许 system scope"})
        if scope_type != ConfigRevision.ScopeType.SYSTEM and definition.restart_required:
            raise ValidationError({key: "需要重启的配置只允许 system scope"})
        raw = materialized[key]
        value = raw.get("value")
        credential_version = raw.get("credential_version")
        if credential_version is None:
            credential_version = _credential_version(raw.get("credential_id"))
        validate_config_value(definition, value, credential_version)
        if definition.is_secret:
            contract_size, contract_sha = 0, ""
            digest_values[key] = {
                "credential_configured": True,
                "credential_binding_sha256": credential_reference_digest(credential_version),
            }
        else:
            contract = json_contract(value)
            contract_size, contract_sha = contract.size_bytes, contract.sha256
            digest_values[key] = value
        rows.append(ConfigValue(
            revision=revision,
            definition=definition,
            value=None if definition.is_secret else value,
            value_schema_version=1,
            value_size_bytes=contract_size,
            value_sha256=contract_sha,
            credential_version=credential_version,
        ))
    ConfigValue.objects.bulk_create(rows)
    revision.values_sha256 = hashlib.sha256(
        canonical_json_bytes(digest_values),
    ).hexdigest()
    revision.save(update_fields=("values_sha256", "updated_at"))
    return revision


def _verify_revision(
    revision: ConfigRevision,
    *,
    validate_resource_state: bool = False,
) -> dict[str, tuple[Any, ConfigValue]]:
    """Verify immutable revision contracts without conflating live readiness.

    Historical and active revisions must remain readable when referenced
    resources change lifecycle state.  Lifecycle gates opt into the live
    resource check explicitly; ordinary resolution verifies type/schema,
    stored JSON contracts and the revision checksum only.
    """

    resolved: dict[str, tuple[Any, ConfigValue]] = {}
    digest_values: dict[str, Any] = {}
    for row in revision.values.select_related(
        "definition", "credential_version__binding",
    ).all():
        definition = row.definition
        validate_config_value(
            definition,
            row.value,
            row.credential_version,
            validate_resource_state=validate_resource_state,
        )
        if definition.is_secret:
            value = {
                "credential_configured": True,
                "credential_binding_sha256": credential_reference_digest(row.credential_version),
            }
        else:
            verify_json_contract(
                row.value,
                schema_version=row.value_schema_version,
                size_bytes=row.value_size_bytes,
                sha256=row.value_sha256,
            )
            value = row.value
        resolved[definition.key] = (value, row)
        digest_values[definition.key] = value
    digest = hashlib.sha256(canonical_json_bytes(digest_values)).hexdigest()
    if digest != revision.values_sha256:
        raise ConfigurationResolutionError(
            f"配置 revision {revision.id} 校验和不一致",
            code="config_revision_checksum_mismatch",
        )
    return resolved


def _assert_required_system_values(
    revision: ConfigRevision,
    values: dict[str, tuple[Any, ConfigValue]],
) -> None:
    if revision.scope_type != ConfigRevision.ScopeType.SYSTEM:
        return
    required = set(ConfigDefinition.objects.filter(
        is_active=True,
        is_required=True,
    ).values_list("key", flat=True))
    missing = sorted(required - set(values))
    if missing:
        raise ValidationError({
            "values": f"缺少必需系统配置: {', '.join(missing)}",
        })


def _assert_deadline_pairs(values: dict[str, tuple[Any, ConfigValue]]) -> None:
    pairs = (
        (
            "workflow.smoke_idle_timeout_seconds",
            "workflow.smoke_hard_timeout_seconds",
        ),
        (
            "runtime.task_idle_timeout_seconds",
            "production.task_timeout_seconds",
        ),
    )
    for idle_key, hard_key in pairs:
        if idle_key not in values or hard_key not in values:
            continue
        idle = values[idle_key][0]
        hard = values[hard_key][0]
        if isinstance(idle, bool) or isinstance(hard, bool) or float(idle) > float(hard):
            raise ValidationError({idle_key: f"不能大于 {hard_key}"})


def _assert_effective_deadline_pairs(
    revision: ConfigRevision,
    values: dict[str, tuple[Any, ConfigValue]],
) -> None:
    if revision.scope_type == ConfigRevision.ScopeType.SYSTEM:
        _assert_deadline_pairs(values)
        return
    keys = (
        "workflow.smoke_idle_timeout_seconds",
        "workflow.smoke_hard_timeout_seconds",
        "runtime.task_idle_timeout_seconds",
        "production.task_timeout_seconds",
    )
    current = resolve_configuration(
        project=revision.project,
        run=revision.run,
        required_keys=keys,
    ).values
    candidate = dict(current)
    candidate.update({key: value for key, (value, _row) in values.items()})
    for idle_key, hard_key in (
        ("workflow.smoke_idle_timeout_seconds", "workflow.smoke_hard_timeout_seconds"),
        ("runtime.task_idle_timeout_seconds", "production.task_timeout_seconds"),
    ):
        if float(candidate[idle_key]) > float(candidate[hard_key]):
            raise ValidationError({idle_key: f"有效值不能大于 {hard_key}"})


def revision_changes_secret_bindings(
    revision: ConfigRevision,
    *,
    baseline: ConfigRevision | None = None,
) -> bool:
    """Return whether a revision adds, removes, or rotates a secret version.

    Config revisions carry forward immutable ``SecretVersion`` rows.  Staff may
    publish ordinary configuration changes that merely retain those rows, but
    changing the referenced secret material remains a superuser-only action.
    """

    if baseline is None:
        baseline = revision.based_on_revision
    current = {
        row.definition_id: row.credential_version_id
        for row in revision.values.select_related("definition").filter(
            definition__is_secret=True,
        )
    }
    previous = {
        row.definition_id: row.credential_version_id
        for row in baseline.values.select_related("definition").filter(
            definition__is_secret=True,
        )
    } if baseline is not None else {}
    return current != previous


def _assert_secret_revision_actor(
    revision: ConfigRevision,
    *,
    baseline: ConfigRevision | None,
    actor,
) -> None:
    """Keep secret binding changes behind the superuser boundary.

    ``actor=None`` is reserved for controlled migration/bootstrap services. A
    staff user may carry an unchanged immutable SecretVersion through a normal
    configuration revision, but may not add, rotate, or remove one.
    """

    if (
        actor is not None
        and not getattr(actor, "is_superuser", False)
        and revision_changes_secret_bindings(revision, baseline=baseline)
    ):
        raise PermissionDenied("只有超级管理员可以发布或激活敏感配置绑定变更")


def _clear_satisfied_migration_reviews(
    revision: ConfigRevision,
    values: dict[str, tuple[Any, ConfigValue]],
) -> None:
    if revision.scope_type != ConfigRevision.ScopeType.SYSTEM:
        return
    ConfigDefinition.objects.filter(
        key__in=values,
        needs_review=True,
        review_reason__startswith="0046 ",
    ).update(needs_review=False, review_reason="", updated_at=timezone.now())


def publish_config_revision(revision: ConfigRevision, *, published_by) -> ConfigRevision:
    with transaction.atomic():
        revision = ConfigRevision.objects.select_for_update().get(pk=revision.pk)
        if revision.status != ConfigRevision.Status.DRAFT:
            raise ValidationError("只有 draft 配置 revision 可以发布")
        values = _verify_revision(revision, validate_resource_state=True)
        _assert_required_system_values(revision, values)
        _assert_effective_deadline_pairs(revision, values)
        scope_qs = _scope_queryset(
            revision.scope_type,
            project=revision.project,
            run=revision.run,
        ).select_for_update()
        active = scope_qs.filter(
            status=ConfigRevision.Status.PUBLISHED,
            activation_state=ConfigRevision.ActivationState.ACTIVE,
        ).first()
        if revision.based_on_revision_id != getattr(active, "id", None):
            raise ConfigurationResolutionError(
                "配置草稿基于的 effective revision 已变化，请重新读取后再提交",
                code="stale_config_base",
            )
        _assert_secret_revision_actor(
            revision,
            baseline=active,
            actor=published_by,
        )
        unsettled = scope_qs.filter(
            status=ConfigRevision.Status.PUBLISHED,
            activation_state=ConfigRevision.ActivationState.PENDING,
        ).exclude(pk=revision.pk).first()
        if unsettled is not None:
            raise ConfigurationResolutionError(
                "同一作用域已有等待激活的 revision，必须先完成激活后再发布新草稿",
                code="config_activation_pending",
            )
        active_values = _verify_revision(active) if active is not None else {}
        restart_keys = set(ConfigDefinition.objects.filter(
            key__in=set(values) | set(active_values),
            restart_required=True,
        ).values_list("key", flat=True))
        changed_restart_keys = {
            key for key in restart_keys
            if (
                key not in values
                or key not in active_values
                or active_values[key][0] != values[key][0]
            )
        }
        now = timezone.now()
        revision.status = ConfigRevision.Status.PUBLISHED
        revision.created_by = revision.created_by or published_by
        revision.published_at = now
        revision.activation_error = ""
        if revision.scope_type == ConfigRevision.ScopeType.SYSTEM and changed_restart_keys:
            revision.activation_state = ConfigRevision.ActivationState.PENDING
            revision.activated_at = None
            revision.activation_requested_boot_id = str(
                getattr(settings, "AUTOPROMPT_BOOT_ID", "") or ""
            ).strip()[:64]
        else:
            scope_qs.filter(
                activation_state=ConfigRevision.ActivationState.ACTIVE,
            ).exclude(pk=revision.pk).update(
                status=ConfigRevision.Status.SUPERSEDED,
                activation_state=ConfigRevision.ActivationState.INACTIVE,
                updated_at=now,
            )
            revision.activation_state = ConfigRevision.ActivationState.ACTIVE
            revision.activated_at = now
            revision.activation_requested_boot_id = ""
        revision.save(update_fields=(
            "status", "created_by", "published_at", "activation_state",
            "activated_at", "activation_error", "activation_requested_boot_id", "updated_at",
        ))
        scope_qs.filter(
            status=ConfigRevision.Status.PUBLISHED,
            activation_state=ConfigRevision.ActivationState.FAILED,
        ).exclude(pk=revision.pk).update(
            status=ConfigRevision.Status.SUPERSEDED,
            activation_state=ConfigRevision.ActivationState.INACTIVE,
            updated_at=now,
        )
        if revision.activation_state == ConfigRevision.ActivationState.ACTIVE:
            _clear_satisfied_migration_reviews(revision, values)
        return revision


def _validate_activation_value(definition: ConfigDefinition, value: Any) -> None:
    schema = definition.value_schema if isinstance(definition.value_schema, dict) else {}
    value_format = str(schema.get("format") or "")
    if value_format == "safe-relative-path":
        raw = str(value or "").strip()
        candidate = Path(raw)
        windows = PureWindowsPath(raw)
        if (
            not raw
            or candidate.is_absolute()
            or bool(windows.drive)
            or bool(windows.root)
            or ".." in candidate.parts
            or ".." in windows.parts
        ):
            raise ValidationError({definition.key: "必须是部署根目录内的安全相对路径"})
        deployment_root = Path(settings.BASE_DIR).resolve()
        resolved = (deployment_root / candidate).resolve()
        if resolved != deployment_root and deployment_root not in resolved.parents:
            raise ValidationError({definition.key: "解析后的路径超出部署根目录"})
    elif value_format == "executable":
        raw = str(value or "").strip()
        candidate = Path(raw)
        if not raw:
            raise ValidationError({definition.key: "可执行项不能为空"})
        windows = PureWindowsPath(raw)
        if not candidate.is_absolute():
            if windows.drive or windows.root or "/" in raw or "\\" in raw:
                raise ValidationError({definition.key: "相对可执行项只能使用 PATH 中的命令名"})
            discovered = shutil.which(raw)
            if discovered is None:
                raise ValidationError({definition.key: "PATH 中找不到该可执行项"})
            candidate = Path(discovered)
        allowed_roots = [
            Path(item).resolve()
            for item in os.getenv("AUTOPROMPT_ALLOWED_TOOL_ROOTS", "").split(os.pathsep)
            if item.strip()
        ]
        resolved = candidate.resolve()
        if not allowed_roots or not any(resolved == root or root in resolved.parents for root in allowed_roots):
            raise ValidationError({definition.key: "解析后的可执行路径不在部署允许根目录内"})
        if not resolved.is_file():
            raise ValidationError({definition.key: "可执行文件不存在"})


def activate_config_revision(revision: ConfigRevision, *, activated_by) -> ConfigRevision:
    try:
        verified = _verify_revision(revision, validate_resource_state=True)
        _assert_required_system_values(revision, verified)
        _assert_deadline_pairs(verified)
        for value, row in verified.values():
            if row.definition.restart_required and not row.definition.is_secret:
                _validate_activation_value(row.definition, value)
    except (ConfigurationResolutionError, ValidationError) as exc:
        ConfigRevision.objects.filter(
            pk=revision.pk,
            status=ConfigRevision.Status.PUBLISHED,
            activation_state__in=(
                ConfigRevision.ActivationState.PENDING,
                ConfigRevision.ActivationState.FAILED,
            ),
        ).update(
            activation_state=ConfigRevision.ActivationState.FAILED,
            activation_error=str(exc)[:500],
            updated_at=timezone.now(),
        )
        raise
    stale_error = ""
    with transaction.atomic():
        locked = ConfigRevision.objects.select_for_update().get(pk=revision.pk)
        if (
            locked.status != ConfigRevision.Status.PUBLISHED
            or locked.activation_state not in {
                ConfigRevision.ActivationState.PENDING,
                ConfigRevision.ActivationState.FAILED,
            }
        ):
            raise ValidationError("只有 pending/failed 的已发布 revision 可以激活")
        current_boot_id = str(getattr(settings, "AUTOPROMPT_BOOT_ID", "") or "").strip()
        if not current_boot_id:
            raise ValidationError("当前进程没有受控 boot id；保持 pending，必须由受控启动入口重启后激活")
        if current_boot_id == locked.activation_requested_boot_id:
            raise ValidationError("restart_required 配置必须在发布后的新进程中激活")
        scope_qs = _scope_queryset(
            locked.scope_type,
            project=locked.project,
            run=locked.run,
        ).select_for_update()
        active = scope_qs.filter(
            status=ConfigRevision.Status.PUBLISHED,
            activation_state=ConfigRevision.ActivationState.ACTIVE,
        ).first()
        if locked.based_on_revision_id != getattr(active, "id", None):
            locked.activation_state = ConfigRevision.ActivationState.FAILED
            locked.activation_error = "配置基线已变化，请基于当前 effective revision 重新创建草稿"
            locked.save(update_fields=("activation_state", "activation_error", "updated_at"))
            stale_error = locked.activation_error
        else:
            _assert_secret_revision_actor(
                locked,
                baseline=active,
                actor=activated_by,
            )
            now = timezone.now()
            scope_qs.filter(
                activation_state=ConfigRevision.ActivationState.ACTIVE,
            ).exclude(pk=locked.pk).update(
                status=ConfigRevision.Status.SUPERSEDED,
                activation_state=ConfigRevision.ActivationState.INACTIVE,
                updated_at=now,
            )
            locked.activation_state = ConfigRevision.ActivationState.ACTIVE
            locked.activation_error = ""
            locked.activated_at = now
            locked.created_by = locked.created_by or activated_by
            locked.save(update_fields=(
                "activation_state", "activation_error", "activated_at", "created_by", "updated_at",
            ))
            _clear_satisfied_migration_reviews(locked, verified)
    if stale_error:
        raise ConfigurationResolutionError(stale_error, code="stale_config_base")
    return locked


def _version_defaults(
    *,
    model_profile_version: ModelProfileVersion | None,
    workflow_version: WorkflowTemplateVersion | None,
) -> tuple[dict[str, Any], dict[str, dict[str, Any]]]:
    values: dict[str, Any] = {}
    sources: dict[str, dict[str, Any]] = {}
    entries: list[tuple[str, object, dict[str, Any], str, int, str]] = []
    if model_profile_version is not None:
        verify_json_contract(
            model_profile_version.specification,
            schema_version=model_profile_version.specification_schema_version,
            size_bytes=model_profile_version.specification_size_bytes,
            sha256=model_profile_version.specification_sha256,
        )
        specification = model_profile_version.specification or {}
        entries.append((
            "model", model_profile_version,
            specification.get("config_defaults") or {},
            str(model_profile_version.id), model_profile_version.version,
            model_profile_version.specification_sha256,
        ))
    if workflow_version is not None:
        verify_json_contract(
            workflow_version.input_contract,
            schema_version=workflow_version.input_contract_schema_version,
            size_bytes=workflow_version.input_contract_size_bytes,
            sha256=workflow_version.input_contract_sha256,
        )
        contract = workflow_version.input_contract or {}
        entries.append((
            "workflow", workflow_version,
            contract.get("config_defaults") or {},
            str(workflow_version.id), workflow_version.version,
            workflow_version.input_contract_sha256,
        ))
    for source_type, _version, defaults, source_id, version, digest in entries:
        if not isinstance(defaults, dict):
            raise ConfigurationResolutionError(
                f"{source_type} config_defaults 必须是对象",
                code="version_config_defaults_invalid",
            )
        definitions = {
            item.key: item
            for item in ConfigDefinition.objects.filter(key__in=defaults, is_active=True)
        }
        unknown = sorted(set(defaults) - set(definitions))
        if unknown:
            raise ConfigurationResolutionError(
                f"{source_type} defaults 含未知或停用配置: {', '.join(unknown)}",
                code="version_config_definition_missing",
                missing_keys=unknown,
            )
        for key, value in defaults.items():
            definition = definitions[key]
            if source_type not in _allowed_default_sources(definition):
                raise ConfigurationResolutionError(
                    f"配置 {key} 不允许由 {source_type} 提供默认值",
                    code="version_config_source_forbidden",
                )
            if definition.is_secret or definition.restart_required:
                raise ConfigurationResolutionError(
                    f"配置 {key} 不能作为模型/工作流默认值",
                    code="version_config_source_forbidden",
                )
            try:
                validate_config_value(definition, value)
            except ValidationError as exc:
                raise ConfigurationResolutionError(
                    f"{source_type} 默认值 {key} 无效: {exc}",
                    code="version_config_value_invalid",
                ) from exc
            values[key] = value
            sources[key] = {
                "source": source_type,
                "id": source_id,
                "scope": source_type,
                "source_type": source_type,
                "source_id": source_id,
                "version": version,
                "hash": digest,
                "sha256": digest,
            }
    return values, sources


def validate_version_configuration_defaults(
    *,
    model_profile_version: ModelProfileVersion | None = None,
    workflow_version: WorkflowTemplateVersion | None = None,
) -> None:
    _version_defaults(
        model_profile_version=model_profile_version,
        workflow_version=workflow_version,
    )


def resolve_configuration_from_snapshot(
    base_snapshot: dict[str, Any],
    *,
    model_profile_version: ModelProfileVersion | None = None,
    workflow_version: WorkflowTemplateVersion | None = None,
    required_keys: Iterable[str] | None = None,
) -> ResolvedConfiguration:
    defaults, default_sources = _version_defaults(
        model_profile_version=model_profile_version,
        workflow_version=workflow_version,
    )
    base_values = base_snapshot.get("values") if isinstance(base_snapshot, dict) else None
    base_sources = base_snapshot.get("sources") if isinstance(base_snapshot, dict) else None
    if not isinstance(base_values, dict) or not isinstance(base_sources, dict):
        raise ConfigurationResolutionError("冻结基础配置无效", code="base_config_snapshot_invalid")
    values = {**defaults, **base_values}
    sources = {**default_sources, **base_sources}
    required = {str(item) for item in (required_keys or ())}
    missing = sorted(required - set(values))
    if missing:
        raise ConfigurationResolutionError(
            f"缺少必需配置: {', '.join(missing)}",
            code="required_config_missing",
            missing_keys=missing,
        )
    revision_ids = tuple(str(item) for item in (base_snapshot.get("revision_ids") or []))
    digest = hashlib.sha256(canonical_json_bytes({
        "values": values,
        "sources": sources,
        "revision_ids": list(revision_ids),
    })).hexdigest()
    return ResolvedConfiguration(values, sources, revision_ids, digest)


def resolve_configuration(
    *,
    project: SeriesProject | None = None,
    run: ProductionRun | None = None,
    model_profile_version: ModelProfileVersion | None = None,
    workflow_version: WorkflowTemplateVersion | None = None,
    required_keys: Iterable[str] | None = None,
) -> ResolvedConfiguration:
    if run is not None:
        if project is not None and run.project_id != project.id:
            raise ConfigurationResolutionError(
                "run 与 project 不匹配",
                code="config_scope_mismatch",
            )
        project = run.project
    revisions: list[ConfigRevision] = []
    system = ConfigRevision.objects.filter(
        scope_type=ConfigRevision.ScopeType.SYSTEM,
        status=ConfigRevision.Status.PUBLISHED,
        activation_state=ConfigRevision.ActivationState.ACTIVE,
    ).first()
    if system is not None:
        revisions.append(system)
    if project is not None:
        project_revision = ConfigRevision.objects.filter(
            scope_type=ConfigRevision.ScopeType.PROJECT,
            project=project,
            status=ConfigRevision.Status.PUBLISHED,
            activation_state=ConfigRevision.ActivationState.ACTIVE,
        ).first()
        if project_revision is not None:
            revisions.append(project_revision)
    if run is not None:
        run_revision = ConfigRevision.objects.filter(
            scope_type=ConfigRevision.ScopeType.RUN,
            run=run,
            status=ConfigRevision.Status.PUBLISHED,
            activation_state=ConfigRevision.ActivationState.ACTIVE,
        ).first()
        if run_revision is not None:
            revisions.append(run_revision)

    values, sources = _version_defaults(
        model_profile_version=model_profile_version,
        workflow_version=workflow_version,
    )
    for revision in revisions:
        for key, (value, _row) in _verify_revision(revision).items():
            values[key] = value
            sources[key] = {
                "source": revision.scope_type,
                "id": str(revision.id),
                "scope": revision.scope_type,
                "source_type": revision.scope_type,
                "source_id": str(revision.id),
                "version": revision.revision,
                "revision": revision.revision,
                "revision_id": str(revision.id),
                "hash": revision.values_sha256,
                "sha256": revision.values_sha256,
            }
    if required_keys is None:
        required = set(ConfigDefinition.objects.filter(
            is_active=True,
            is_required=True,
        ).values_list("key", flat=True))
    else:
        required = {str(item) for item in required_keys}
    missing = sorted(required - set(values))
    if missing:
        raise ConfigurationResolutionError(
            f"缺少必需配置: {', '.join(missing)}",
            code="required_config_missing",
            missing_keys=missing,
        )
    digest_payload = {
        "values": values,
        "sources": sources,
        "revision_ids": [str(item.id) for item in revisions],
    }
    digest = hashlib.sha256(canonical_json_bytes(digest_payload)).hexdigest()
    return ResolvedConfiguration(
        values=values,
        sources=sources,
        revision_ids=tuple(str(item.id) for item in revisions),
        sha256=digest,
    )
