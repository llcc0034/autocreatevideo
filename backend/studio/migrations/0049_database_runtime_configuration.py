import hashlib
import json

from django.db import migrations
from django.utils import timezone


SPECS = (
    ("access.public_registration_enabled", "access", "boolean", False, {}, "是否允许访客公开注册账号"),
    ("artifact.upload_max_bytes", "artifact", "integer", 10485760, {"minimum": 1, "maximum": 1073741824}, "普通图片/文件单次上传上限"),
    ("artifact.video_upload_max_bytes", "artifact", "integer", 262144000, {"minimum": 1, "maximum": 4294967296}, "视频素材单次上传上限"),
    ("artifact.audio_upload_max_bytes", "artifact", "integer", 52428800, {"minimum": 1, "maximum": 1073741824}, "音频素材单次上传上限"),
    ("artifact.tenant_quota_bytes", "artifact", "integer", 2147483648, {"minimum": 1, "maximum": 1099511627776}, "单租户受管素材总配额"),
    ("artifact.image_max_width", "artifact", "integer", 8192, {"minimum": 32, "maximum": 65536}, "上传图片最大宽度"),
    ("artifact.image_max_height", "artifact", "integer", 8192, {"minimum": 32, "maximum": 65536}, "上传图片最大高度"),
    ("artifact.image_max_pixels", "artifact", "integer", 40000000, {"minimum": 1024, "maximum": 1000000000}, "上传图片最大像素数"),
    ("artifact.signed_url_ttl_seconds", "artifact", "integer", 900, {"minimum": 30, "maximum": 86400}, "受管媒体签名 URL 有效期"),
    ("runtime.task_worker_poll_seconds", "runtime", "number", 2, {"minimum": 0.05, "maximum": 300}, "生成 worker 空闲轮询间隔"),
    ("runtime.task_remote_poll_seconds", "runtime", "number", 1, {"minimum": 0.05, "maximum": 300}, "远端生成状态轮询间隔"),
    ("runtime.task_heartbeat_seconds", "runtime", "number", 5, {"minimum": 0.05, "maximum": 300}, "生成任务心跳间隔"),
    ("runtime.task_health_check_interval_seconds", "runtime", "number", 5, {"minimum": 0, "maximum": 300}, "生成端健康复核间隔"),
    ("runtime.task_health_failure_limit", "runtime", "integer", 3, {"minimum": 1, "maximum": 100}, "连续健康失败上限"),
    ("runtime.task_remote_missing_grace_seconds", "runtime", "number", 5, {"minimum": 0, "maximum": 3600}, "远端任务短暂缺失宽限"),
    ("runtime.task_cancel_grace_seconds", "runtime", "number", 30, {"minimum": 0, "maximum": 3600}, "取消确认宽限"),
    ("runtime.task_stall_threshold_seconds", "runtime", "number", 30, {"minimum": 1, "maximum": 86400}, "任务停滞判定阈值"),
    ("runtime.task_stale_recovery_interval_seconds", "runtime", "number", 5, {"minimum": 0.1, "maximum": 3600}, "陈旧任务恢复扫描间隔"),
    ("delivery.assembly_timeout_seconds", "delivery", "number", 7200, {"minimum": 1, "maximum": 86400}, "逐集组装执行超时"),
    ("delivery.ai_label_enabled", "delivery", "boolean", True, {}, "交付产物是否嵌入 AI 披露标签"),
    ("delivery.ai_label_text", "delivery", "string", "AI-generated", {"minLength": 1, "maxLength": 120}, "交付产物 AI 披露标签文本"),
)


def canonical(value):
    return json.dumps(value, ensure_ascii=False, sort_keys=True, separators=(",", ":")).encode("utf-8")


def contract(value):
    payload = canonical(value)
    return len(payload), hashlib.sha256(payload).hexdigest()


def forwards(apps, schema_editor):
    ConfigDefinition = apps.get_model("studio", "ConfigDefinition")
    ConfigRevision = apps.get_model("studio", "ConfigRevision")
    ConfigValue = apps.get_model("studio", "ConfigValue")
    now = timezone.now()
    definitions = {}
    for key, domain, value_type, initial, schema, description in SPECS:
        schema_size, schema_sha = contract(schema)
        initial_size, initial_sha = contract(initial)
        defaults = {
            "domain": domain,
            "value_type": value_type,
            "description": description,
            "unit": "",
            "impact_modules": ["api", "worker"] if domain == "runtime" else ["api"],
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
            "initial_value_size_bytes": initial_size,
            "initial_value_sha256": initial_sha,
        }
        definition, created = ConfigDefinition.objects.get_or_create(
            key=key,
            defaults=defaults,
        )
        if not created:
            immutable = (
                "value_type", "restart_required", "allowed_scopes",
                "allowed_default_sources", "is_required", "is_secret",
                "value_schema", "value_schema_version", "value_schema_size_bytes",
                "value_schema_sha256", "initial_value", "initial_value_schema_version",
                "initial_value_size_bytes", "initial_value_sha256",
            )
            conflicts = [name for name in immutable if getattr(definition, name) != defaults[name]]
            if conflicts or not definition.is_active:
                raise RuntimeError(
                    f"0049 配置定义 {key} 与已存在的审计合同冲突: "
                    f"{', '.join(conflicts or ['is_active'])}"
                )
        definitions[key] = (definition, initial, initial_size, initial_sha)

    active = ConfigRevision.objects.filter(
        scope_type="system", status="published", activation_state="active",
    ).order_by("-revision").first()
    existing = {}
    if active:
        existing = {
            row.definition.key: row
            for row in ConfigValue.objects.filter(revision=active).select_related(
                "definition", "credential_version"
            )
        }
    if all(key in existing for key in definitions):
        return

    next_revision = (
        ConfigRevision.objects.filter(scope_type="system").order_by("-revision")
        .values_list("revision", flat=True).first() or 0
    ) + 1
    new_revision = ConfigRevision.objects.create(
        scope_type="system",
        revision=next_revision,
        status="published",
        based_on_revision_id=active.pk if active else None,
        activation_state="inactive",
        change_summary="0049 将运行期业务配置迁入数据库",
        published_at=now,
    )
    digest_values = {}
    rows = []
    for key, row in existing.items():
        rows.append(ConfigValue(
            revision=new_revision,
            definition_id=row.definition_id,
            value=row.value,
            value_schema_version=row.value_schema_version,
            value_size_bytes=row.value_size_bytes,
            value_sha256=row.value_sha256,
            credential_version_id=row.credential_version_id,
        ))
        if row.credential_version_id:
            version = row.credential_version
            identity = f"version:{version.id}:{version.version}"
            digest_values[key] = {
                "credential_configured": True,
                "credential_binding_sha256": hashlib.sha256(identity.encode("utf-8")).hexdigest(),
            }
        else:
            digest_values[key] = row.value
    for key, (definition, initial, size, sha) in definitions.items():
        if key in existing:
            continue
        rows.append(ConfigValue(
            revision=new_revision,
            definition=definition,
            value=initial,
            value_schema_version=1,
            value_size_bytes=size,
            value_sha256=sha,
        ))
        digest_values[key] = initial
    ConfigValue.objects.bulk_create(rows)
    new_revision.values_sha256 = hashlib.sha256(canonical(digest_values)).hexdigest()
    new_revision.save(update_fields=("values_sha256", "updated_at"))
    if active:
        ConfigRevision.objects.filter(pk=active.pk).update(
            status="superseded", activation_state="inactive", updated_at=now,
        )
    ConfigRevision.objects.filter(pk=new_revision.pk).update(
        activation_state="active", activated_at=now, updated_at=now,
    )


class Migration(migrations.Migration):
    dependencies = [("studio", "0048_access_control_encrypted_configuration")]
    operations = [migrations.RunPython(forwards, migrations.RunPython.noop)]
