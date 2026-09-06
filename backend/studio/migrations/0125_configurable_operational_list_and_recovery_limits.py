import hashlib
import json

from django.db import migrations
from django.utils import timezone


STANDARD_CONFIG = {
    "runtime.asset_text_submission_grace_seconds": (
        "runtime",
        "integer",
        60,
        "seconds",
        {
            "minimum": 0,
            "security_ceiling_key": "security.runtime.max_interval_seconds",
        },
        (
            "文本素材任务进入陈旧回收前，在已冻结请求超时之外保留的提交宽限时间"
            " / Asset text-submission stale-recovery grace period"
        ),
        ["runtime", "asset_generation", "recovery"],
    ),
    "runtime.asset_uncertain_reconcile_batch_size": (
        "runtime",
        "integer",
        32,
        "items",
        {
            "minimum": 1,
            "security_ceiling_key": "security.runtime.max_reconcile_batch_items",
        },
        (
            "每轮核对状态不确定的 ComfyUI 素材任务数量"
            " / ComfyUI uncertain-asset reconciliation batch size"
        ),
        ["runtime", "asset_generation", "worker"],
    ),
    "api.default_page_size": (
        "api",
        "integer",
        24,
        "items",
        {
            "minimum": 1,
            "security_ceiling_key": "security.api.max_page_size",
        },
        "运营列表默认每页数量 / Default operations-list page size",
        ["api", "pagination", "frontend"],
    ),
    "workflow.node_metadata_default_page_size": (
        "workflow",
        "integer",
        50,
        "items",
        {
            "minimum": 1,
            "security_ceiling_key": "security.api.max_page_size",
        },
        (
            "ComfyUI 节点元数据浏览默认每页数量"
            " / Default ComfyUI node-metadata page size"
        ),
        ["workflow", "comfyui", "metadata"],
    ),
    "workflow.node_metadata_input_preview_limit": (
        "workflow",
        "integer",
        100,
        "items",
        {
            "minimum": 1,
            "security_ceiling_key": "security.comfy.client.list_max_items",
        },
        (
            "节点元数据中 required/optional 输入名称的单组展示数量"
            " / Per-group ComfyUI node-input preview size"
        ),
        ["workflow", "comfyui", "metadata"],
    ),
}


SECURITY_BOUNDARIES = {
    "security.runtime.max_reconcile_batch_items": (
        1000,
        "items",
        (
            "单轮不确定任务核对数量绝对上限"
            " / Absolute uncertain-task reconciliation batch ceiling"
        ),
        ["security", "runtime", "asset_generation"],
    ),
    "security.api.max_search_chars": (
        4096,
        "characters",
        "同步查询搜索文本绝对长度上限 / Absolute synchronous-search length ceiling",
        ["security", "api", "query"],
    ),
}


def _canonical(value):
    return json.dumps(
        value,
        ensure_ascii=False,
        sort_keys=True,
        separators=(",", ":"),
        allow_nan=False,
    ).encode("utf-8")


def _contract(value):
    payload = _canonical(value)
    return len(payload), hashlib.sha256(payload).hexdigest()


def configure_operational_list_and_recovery_limits(apps, schema_editor):
    Definition = apps.get_model("studio", "ConfigDefinition")
    Revision = apps.get_model("studio", "ConfigRevision")
    Value = apps.get_model("studio", "ConfigValue")

    definitions = {}
    boundary_schema = {"minimum": 1}
    boundary_schema_size, boundary_schema_sha = _contract(boundary_schema)
    for key, (initial, unit, description, modules) in SECURITY_BOUNDARIES.items():
        value_size, value_sha = _contract(initial)
        definition, created = Definition.objects.get_or_create(
            key=key,
            defaults={
                "domain": "security",
                "configuration_class": "security_boundary",
                "value_type": "integer",
                "description": (
                    description
                    + "。命中后 fail closed；只有超级管理员可以修改。"
                ),
                "unit": unit,
                "impact_modules": modules,
                "restart_required": False,
                "allowed_scopes": "system",
                "allowed_default_sources": "",
                "is_required": True,
                "is_secret": False,
                "is_active": True,
                "needs_review": False,
                "review_reason": "",
                "value_schema": boundary_schema,
                "value_schema_version": 1,
                "value_schema_size_bytes": boundary_schema_size,
                "value_schema_sha256": boundary_schema_sha,
                "initial_value": initial,
                "initial_value_schema_version": 1,
                "initial_value_size_bytes": value_size,
                "initial_value_sha256": value_sha,
            },
        )
        if not created and (
            definition.configuration_class != "security_boundary"
            or definition.domain != "security"
            or definition.value_type != "integer"
            or definition.allowed_scopes != "system"
            or definition.allowed_default_sources
            or not definition.is_required
            or definition.is_secret
            or not definition.is_active
        ):
            raise RuntimeError(f"0125 安全边界定义 {key} 与既有合同冲突")
        definitions[key] = definition

    for key, (
        domain,
        value_type,
        initial,
        unit,
        schema,
        description,
        modules,
    ) in STANDARD_CONFIG.items():
        schema_size, schema_sha = _contract(schema)
        value_size, value_sha = _contract(initial)
        definition, created = Definition.objects.get_or_create(
            key=key,
            defaults={
                "domain": domain,
                "configuration_class": "standard",
                "value_type": value_type,
                "description": description,
                "unit": unit,
                "impact_modules": modules,
                "restart_required": False,
                "allowed_scopes": "system",
                "allowed_default_sources": "",
                "is_required": True,
                "is_secret": False,
                "is_active": True,
                "needs_review": False,
                "review_reason": "",
                "value_schema": schema,
                "value_schema_version": 1,
                "value_schema_size_bytes": schema_size,
                "value_schema_sha256": schema_sha,
                "initial_value": initial,
                "initial_value_schema_version": 1,
                "initial_value_size_bytes": value_size,
                "initial_value_sha256": value_sha,
            },
        )
        if not created and (
            definition.configuration_class != "standard"
            or definition.domain != domain
            or definition.value_type != value_type
            or definition.allowed_scopes != "system"
            or definition.allowed_default_sources
            or not definition.is_required
            or definition.is_secret
            or not definition.is_active
        ):
            raise RuntimeError(f"0125 标准配置定义 {key} 与既有合同冲突")
        definitions[key] = definition

    active = (
        Revision.objects.filter(
            scope_type="system",
            status="published",
            activation_state="active",
        )
        .order_by("-revision")
        .first()
    )
    if active is None:
        raise RuntimeError("0125 缺少 active system revision")
    existing = list(
        Value.objects.filter(revision=active).select_related(
            "definition", "credential_version",
        )
    )
    initial_values = {
        **{key: value[0] for key, value in SECURITY_BOUNDARIES.items()},
        **{key: value[2] for key, value in STANDARD_CONFIG.items()},
    }
    missing = sorted(
        set(initial_values) - {row.definition.key for row in existing}
    )
    if not missing:
        return

    now = timezone.now()
    next_revision = (
        Revision.objects.filter(scope_type="system")
        .order_by("-revision")
        .values_list("revision", flat=True)
        .first()
        or 0
    ) + 1
    revision = Revision.objects.create(
        scope_type="system",
        revision=next_revision,
        status="published",
        based_on_revision_id=active.id,
        activation_state="inactive",
        change_summary=(
            "0125 配置化资产恢复、运营分页与 ComfyUI 节点元数据浏览限制"
        ),
        published_at=now,
    )
    rows = []
    digest_values = {}
    for row in existing:
        rows.append(
            Value(
                revision=revision,
                definition_id=row.definition_id,
                value=row.value,
                value_schema_version=row.value_schema_version,
                value_size_bytes=row.value_size_bytes,
                value_sha256=row.value_sha256,
                credential_version_id=row.credential_version_id,
            )
        )
        digest_values[row.definition.key] = (
            row.value
            if not row.credential_version_id
            else {
                "credential_configured": True,
                "credential_binding_sha256": hashlib.sha256(
                    (
                        f"version:{row.credential_version_id}:"
                        f"{row.credential_version.version}"
                    ).encode("utf-8")
                ).hexdigest(),
            }
        )
    for key in missing:
        value = initial_values[key]
        value_size, value_sha = _contract(value)
        rows.append(
            Value(
                revision=revision,
                definition=definitions[key],
                value=value,
                value_schema_version=1,
                value_size_bytes=value_size,
                value_sha256=value_sha,
            )
        )
        digest_values[key] = value
    Value.objects.bulk_create(rows)
    revision.values_sha256 = hashlib.sha256(_canonical(digest_values)).hexdigest()
    revision.save(update_fields=("values_sha256", "updated_at"))
    Revision.objects.filter(pk=active.pk).update(
        status="superseded",
        activation_state="inactive",
        updated_at=now,
    )
    Revision.objects.filter(pk=revision.pk).update(
        activation_state="active",
        activated_at=now,
        updated_at=now,
    )


class Migration(migrations.Migration):
    dependencies = [("studio", "0124_remove_prompt_artifact_business_defaults")]

    operations = [
        migrations.RunPython(
            configure_operational_list_and_recovery_limits,
            migrations.RunPython.noop,
        ),
    ]
