import hashlib
import json

from django.core.validators import MinValueValidator
from django.db import migrations, models
from django.utils import timezone


PROJECT_CREATION_POLICY = {
    "schema_version": 1,
    "width": 768,
    "height": 1344,
    "fps": 24,
    "default_shot_duration_ms": 5000,
    "default_episode_count": 1,
    "default_shots_per_episode": 8,
    "default_episode_duration_seconds": 90,
    "default_max_generation_tasks": 20,
    "continuity_seed_policy": {"mode": "random_per_project", "value": 1},
}


STANDARD_CONFIG = {
    "production.project_creation_policy": (
        "production", "object", PROJECT_CREATION_POLICY, "contract",
        {
            "type": "object", "required": sorted(PROJECT_CREATION_POLICY),
            "additionalProperties": False, "closed_contract": True,
            "properties": {
                "schema_version": {"type": "integer", "enum": [1]},
                "width": {"type": "integer", "minimum": 1},
                "height": {"type": "integer", "minimum": 1},
                "fps": {"type": "integer", "minimum": 1},
                "default_shot_duration_ms": {"type": "integer", "minimum": 1},
                "default_episode_count": {"type": "integer", "minimum": 1},
                "default_shots_per_episode": {"type": "integer", "minimum": 1},
                "default_episode_duration_seconds": {"type": "integer", "minimum": 1},
                "default_max_generation_tasks": {"type": "integer", "minimum": 1},
                "continuity_seed_policy": {
                    "type": "object", "required": ["mode", "value"],
                    "additionalProperties": False,
                    "properties": {
                        "mode": {"type": "string", "enum": ["random_per_project", "explicit"]},
                        "value": {"type": "integer", "minimum": 1},
                    },
                },
            },
            "security_ceiling_paths": {
                "width": "security.media.max_dimension",
                "height": "security.media.max_dimension",
                "fps": "security.media.max_fps",
                "default_shot_duration_ms": "security.media.max_duration_ms",
                "default_episode_duration_seconds": "security.media.max_duration_ms",
                "default_episode_count": "security.production.max_episodes",
                "default_shots_per_episode": "security.production.max_shots_per_episode",
                "default_max_generation_tasks": "security.production.max_generation_tasks",
            },
        },
        "项目正式创建的默认画幅、时长、容量建议与连续性种子合同 / Versioned project-creation defaults",
        ["production", "project", "creation"], "system",
    ),
    "workflow.smoke_poll_interval_seconds": (
        "workflow", "number", 0.25, "seconds",
        {"minimum": 0.001, "security_ceiling_key": "security.runtime.max_interval_seconds"},
        "真实 workflow smoke 远端状态轮询间隔 / Workflow-smoke remote polling interval",
        ["workflow", "smoke", "worker"], "system",
    ),
    "runtime.worker_stale_recovery_minutes": (
        "runtime", "number", 30, "minutes",
        {"minimum": 1, "security_ceiling_key": "security.runtime.max_timeout_seconds"},
        "生成和文本 Worker 的陈旧任务回收阈值 / Generation and text worker stale-recovery threshold",
        ["runtime", "worker", "recovery"], "system",
    ),
    "runtime.deferred_generation_retry_backoff_seconds": (
        "runtime", "integer", 30, "seconds",
        {"minimum": 1, "security_ceiling_key": "security.runtime.max_interval_seconds"},
        "延迟生成请求可重试失败的冻结退避间隔 / Deferred-generation retry backoff",
        ["runtime", "deferred_generation", "retry"], "system,project",
    ),
    "delivery.assembly_max_attempts": (
        "delivery", "integer", 3, "attempts",
        {"minimum": 1, "security_ceiling_key": "security.delivery.assembly_max_attempts"},
        "组装任务创建时冻结的最大尝试次数 / Assembly retry limit frozen at enqueue",
        ["delivery", "assembly", "retry"], "system,project",
    ),
}


SECURITY_BOUNDARIES = {
    "security.delivery.assembly_max_attempts": (
        10, "attempts", "组装任务重试次数绝对上限 / Absolute assembly retry ceiling",
    ),
}


def _canonical(value):
    return json.dumps(value, ensure_ascii=False, sort_keys=True, separators=(",", ":"), allow_nan=False).encode("utf-8")


def _contract(value):
    payload = _canonical(value)
    return len(payload), hashlib.sha256(payload).hexdigest()


def configure_runtime_execution_policy(apps, schema_editor):
    Definition = apps.get_model("studio", "ConfigDefinition")
    Revision = apps.get_model("studio", "ConfigRevision")
    Value = apps.get_model("studio", "ConfigValue")
    definitions = {}
    boundary_schema = {"minimum": 1}
    boundary_schema_size, boundary_schema_sha = _contract(boundary_schema)
    for key, (initial, unit, description) in SECURITY_BOUNDARIES.items():
        value_size, value_sha = _contract(initial)
        definition, created = Definition.objects.get_or_create(key=key, defaults={
            "domain": "security", "configuration_class": "security_boundary",
            "value_type": "integer", "description": description + "。命中后 fail closed；只有超级管理员可修改。",
            "unit": unit, "impact_modules": ["security", "delivery", "assembly"],
            "restart_required": False, "allowed_scopes": "system", "allowed_default_sources": "",
            "is_required": True, "is_secret": False, "is_active": True, "needs_review": False,
            "review_reason": "", "value_schema": boundary_schema, "value_schema_version": 1,
            "value_schema_size_bytes": boundary_schema_size, "value_schema_sha256": boundary_schema_sha,
            "initial_value": initial, "initial_value_schema_version": 1,
            "initial_value_size_bytes": value_size, "initial_value_sha256": value_sha,
        })
        if not created and (definition.configuration_class != "security_boundary" or definition.domain != "security" or definition.allowed_scopes != "system"):
            raise RuntimeError(f"0120 安全边界定义 {key} 与既有合同冲突")
        definitions[key] = definition
    for key, (domain, value_type, initial, unit, schema, description, modules, scopes) in STANDARD_CONFIG.items():
        schema_size, schema_sha = _contract(schema)
        value_size, value_sha = _contract(initial)
        definition, created = Definition.objects.get_or_create(key=key, defaults={
            "domain": domain, "configuration_class": "standard", "value_type": value_type,
            "description": description, "unit": unit, "impact_modules": modules,
            "restart_required": False, "allowed_scopes": scopes, "allowed_default_sources": "",
            "is_required": True, "is_secret": False, "is_active": True, "needs_review": False,
            "review_reason": "", "value_schema": schema, "value_schema_version": 1,
            "value_schema_size_bytes": schema_size, "value_schema_sha256": schema_sha,
            "initial_value": initial, "initial_value_schema_version": 1,
            "initial_value_size_bytes": value_size, "initial_value_sha256": value_sha,
        })
        if not created and (definition.configuration_class != "standard" or definition.domain != domain or definition.value_type != value_type or definition.allowed_scopes != scopes):
            raise RuntimeError(f"0120 标准配置定义 {key} 与既有合同冲突")
        definitions[key] = definition
    active = Revision.objects.filter(scope_type="system", status="published", activation_state="active").order_by("-revision").first()
    if active is None:
        raise RuntimeError("0120 运行期执行策略缺少 active system revision")
    existing = list(Value.objects.filter(revision=active).select_related("definition", "credential_version"))
    initial_values = {**{key: value[0] for key, value in SECURITY_BOUNDARIES.items()}, **{key: value[2] for key, value in STANDARD_CONFIG.items()}}
    missing = sorted(set(initial_values) - {row.definition.key for row in existing})
    if not missing:
        return
    now = timezone.now()
    next_revision = (Revision.objects.filter(scope_type="system").order_by("-revision").values_list("revision", flat=True).first() or 0) + 1
    revision = Revision.objects.create(scope_type="system", revision=next_revision, status="published", based_on_revision_id=active.id, activation_state="inactive", change_summary="0120 收敛项目创建、smoke 轮询、Worker 回收与组装重试运行策略", published_at=now)
    rows, digest_values = [], {}
    for row in existing:
        rows.append(Value(revision=revision, definition_id=row.definition_id, value=row.value, value_schema_version=row.value_schema_version, value_size_bytes=row.value_size_bytes, value_sha256=row.value_sha256, credential_version_id=row.credential_version_id))
        digest_values[row.definition.key] = row.value if not row.credential_version_id else {"credential_configured": True, "credential_binding_sha256": hashlib.sha256(f"version:{row.credential_version_id}:{row.credential_version.version}".encode("utf-8")).hexdigest()}
    for key in missing:
        value_size, value_sha = _contract(initial_values[key])
        rows.append(Value(revision=revision, definition=definitions[key], value=initial_values[key], value_schema_version=1, value_size_bytes=value_size, value_sha256=value_sha))
        digest_values[key] = initial_values[key]
    Value.objects.bulk_create(rows)
    revision.values_sha256 = hashlib.sha256(_canonical(digest_values)).hexdigest()
    revision.save(update_fields=("values_sha256", "updated_at"))
    Revision.objects.filter(pk=active.pk).update(status="superseded", activation_state="inactive", updated_at=now)
    Revision.objects.filter(pk=revision.pk).update(activation_state="active", activated_at=now, updated_at=now)


class Migration(migrations.Migration):
    dependencies = [("studio", "0119_content_contract_policy")]

    operations = [
        migrations.AddField(
            model_name="deferredgenerationrequest", name="retry_backoff_seconds",
            field=models.PositiveIntegerField(default=30, validators=[MinValueValidator(1)]),
            preserve_default=False,
        ),
        migrations.AlterField(
            model_name="assemblyjob", name="max_attempts",
            field=models.PositiveSmallIntegerField(validators=[MinValueValidator(1)]),
        ),
        migrations.AlterField(
            model_name="seriesproject", name="width",
            field=models.PositiveIntegerField(),
        ),
        migrations.AlterField(
            model_name="seriesproject", name="height",
            field=models.PositiveIntegerField(),
        ),
        migrations.AlterField(
            model_name="seriesproject", name="fps",
            field=models.PositiveSmallIntegerField(validators=[MinValueValidator(1)]),
        ),
        migrations.AlterField(
            model_name="seriesproject", name="default_shot_duration_ms",
            field=models.PositiveIntegerField(validators=[MinValueValidator(1)]),
        ),
        migrations.AlterField(
            model_name="seriesproject", name="continuity_seed",
            field=models.BigIntegerField(),
        ),
        migrations.AlterField(
            model_name="generationtask", name="max_attempts",
            field=models.PositiveSmallIntegerField(validators=[MinValueValidator(1)]),
        ),
        migrations.AlterField(
            model_name="deferredgenerationrequest", name="max_attempts",
            field=models.PositiveSmallIntegerField(validators=[MinValueValidator(1)]),
        ),
        migrations.RunPython(configure_runtime_execution_policy, migrations.RunPython.noop),
    ]
