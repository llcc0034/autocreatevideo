import hashlib
import json

from django.db import migrations


def _contract(value):
    payload = json.dumps(
        value,
        ensure_ascii=False,
        sort_keys=True,
        separators=(",", ":"),
        allow_nan=False,
    ).encode("utf-8")
    return len(payload), hashlib.sha256(payload).hexdigest()


def add_runtime_product_definitions(apps, schema_editor):
    ConfigDefinition = apps.get_model("studio", "ConfigDefinition")
    ConfigRevision = apps.get_model("studio", "ConfigRevision")
    ConfigValue = apps.get_model("studio", "ConfigValue")
    inherited_definitions = (
        (
            "production.default_resolution_width",
            "新媒体任务的默认输出宽度",
            {"minimum": 64, "maximum": 8192},
            768,
            "pixels",
        ),
        (
            "production.default_resolution_height",
            "新媒体任务的默认输出高度",
            {"minimum": 64, "maximum": 8192},
            1344,
            "pixels",
        ),
        (
            "production.default_concurrency",
            "新媒体任务池的业务并发上限",
            {"minimum": 1, "maximum": 64},
            1,
            "workers",
        ),
    )
    for key, description, schema, initial, unit in inherited_definitions:
        schema_size, schema_sha = _contract(schema)
        initial_size, initial_sha = _contract(initial)
        ConfigDefinition.objects.update_or_create(
            key=key,
            defaults={
                "domain": "production",
                "value_type": "integer",
                "description": description,
                "unit": unit,
                "impact_modules": ["production", "recommendation"],
                "restart_required": False,
                "allowed_scopes": "system,project,run",
                "allowed_default_sources": "model,workflow",
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
                "initial_value_size_bytes": initial_size,
                "initial_value_sha256": initial_sha,
            },
        )

    definitions = (
        (
            "production.video_shot_min_duration_ms",
            "视频镜头业务建议的最小时长",
            "integer",
            {"minimum": 100, "maximum": 600000},
            5000,
            "milliseconds",
            ["production", "storyboard"],
        ),
        (
            "production.video_shot_max_duration_ms",
            "视频镜头业务建议的最大时长",
            "integer",
            {"minimum": 100, "maximum": 600000},
            15000,
            "milliseconds",
            ["production", "storyboard"],
        ),
        (
            "production.script_auto_split_trigger_chars",
            "无分镜标记剧本触发自动分段的字符数",
            "integer",
            {"minimum": 1, "maximum": 100000},
            320,
            "characters",
            ["production", "storyboard"],
        ),
        (
            "production.script_auto_split_target_chars",
            "自动分段的目标字符数",
            "integer",
            {"minimum": 1, "maximum": 100000},
            220,
            "characters",
            ["production", "storyboard"],
        ),
        (
            "production.task_timeout_seconds",
            "单个媒体任务的业务等待超时",
            "integer",
            {"minimum": 5, "maximum": 86400},
            21600,
            "seconds",
            ["production", "queue"],
        ),
        (
            "production.recommended_max_pending",
            "待处理任务数量的确认建议阈值",
            "integer",
            {"minimum": 1, "maximum": 100000},
            20,
            "tasks",
            ["production", "queue"],
        ),
        (
            "production.recommended_max_active_per_user",
            "单个用户活跃任务数量的确认建议阈值",
            "integer",
            {"minimum": 1, "maximum": 100000},
            5,
            "tasks",
            ["production", "queue"],
        ),
        (
            "production.prompt_normalization_deployment_id",
            "提示词翻译/标准化的默认 ModelDeployment UUID；空值表示必须人工选择",
            "string",
            {"maxLength": 36, "pattern": "^$|^[0-9a-fA-F-]{36}$"},
            "",
            "",
            ["production", "translation"],
        ),
    )
    for key, description, value_type, schema, initial, unit, modules in definitions:
        schema_size, schema_sha = _contract(schema)
        initial_size, initial_sha = _contract(initial)
        ConfigDefinition.objects.update_or_create(
            key=key,
            defaults={
                "domain": "production",
                "value_type": value_type,
                "description": description,
                "unit": unit,
                "impact_modules": modules,
                "restart_required": False,
                "allowed_scopes": "system,project,run",
                "allowed_default_sources": "model,workflow",
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
                "initial_value_size_bytes": initial_size,
                "initial_value_sha256": initial_sha,
            },
        )

    # 0042 introduced these definitions after a fresh install had already
    # published its first system revision. They must be materialized in a
    # formal immutable revision as well; ConfigDefinition.initial_value is
    # bootstrap metadata, never a resolver fallback.
    inherited_keys = tuple(item[0] for item in inherited_definitions)
    target_keys = inherited_keys + tuple(item[0] for item in definitions)
    targets = {
        item.key: item
        for item in ConfigDefinition.objects.filter(key__in=target_keys)
    }
    missing_definitions = sorted(set(target_keys) - set(targets))
    if missing_definitions:
        raise RuntimeError(
            "0046 缺少配置定义: " + ", ".join(missing_definitions)
        )

    unsettled = ConfigRevision.objects.filter(
        scope_type="system",
        activation_state="pending_activation",
    ).exists()
    active = ConfigRevision.objects.filter(
        scope_type="system",
        status="published",
        activation_state="active",
    ).order_by("-revision").first()
    if active is None or unsettled:
        ConfigDefinition.objects.filter(key__in=target_keys).update(
            needs_review=True,
            review_reason=(
                "0046 未找到唯一稳定的 active system revision；"
                "请管理员发布包含生产能力默认值的完整新 revision"
            ),
        )
        return
    current_rows = list(active.values.select_related("definition")) if active else []
    present = {row.definition.key for row in current_rows}
    missing_values = [key for key in target_keys if key not in present]
    if missing_values:
        # Existing installations may have intentionally tuned the legacy env
        # values that these keys replace. A data migration cannot read that
        # deployment state truthfully, so it must never activate product sample
        # values over an existing revision. Fresh databases already received
        # every key in 0031; upgrades are explicitly not-ready until an admin
        # publishes a reviewed revision (or runs the legacy import workflow).
        ConfigDefinition.objects.filter(key__in=missing_values).update(
            needs_review=True,
            review_reason=(
                "0046 未猜测旧部署的有效运行值；请管理员审核旧环境并发布包含该键的完整新 revision"
            ),
        )


class Migration(migrations.Migration):
    dependencies = [
        ("studio", "0045_generatedartifact_comparison_parameters"),
    ]

    operations = [
        migrations.RunPython(add_runtime_product_definitions, migrations.RunPython.noop),
    ]
