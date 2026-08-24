import hashlib
import json
import uuid

from django.conf import settings
from django.db import migrations, models
import django.db.models.deletion
from django.utils import timezone


NEW_CONFIG = (
    (
        "workflow.smoke_idle_timeout_seconds", "workflow", "integer", 900,
        {"minimum": 30, "maximum": 86400},
        "工作流真实烟测无有效节点或阶段推进时的 idle 超时",
    ),
    (
        "workflow.smoke_hard_timeout_seconds", "workflow", "integer", 21600,
        {"minimum": 60, "maximum": 86400},
        "工作流真实烟测不可被重启延长的 hard 超时",
    ),
    (
        "runtime.task_idle_timeout_seconds", "runtime", "integer", 1800,
        {"minimum": 30, "maximum": 86400},
        "媒体任务无有效进度时的 idle 超时",
    ),
)


def _canonical(value):
    return json.dumps(
        value, ensure_ascii=False, sort_keys=True, separators=(",", ":"),
    ).encode("utf-8")


def _contract(value):
    payload = _canonical(value)
    return len(payload), hashlib.sha256(payload).hexdigest()


def add_deadline_configuration(apps, schema_editor):
    ConfigDefinition = apps.get_model("studio", "ConfigDefinition")
    ConfigRevision = apps.get_model("studio", "ConfigRevision")
    ConfigValue = apps.get_model("studio", "ConfigValue")
    now = timezone.now()
    definitions = {}
    for key, domain, value_type, initial, schema, description in NEW_CONFIG:
        schema_size, schema_sha = _contract(schema)
        initial_size, initial_sha = _contract(initial)
        definition, created = ConfigDefinition.objects.get_or_create(
            key=key,
            defaults={
                "domain": domain,
                "value_type": value_type,
                "description": description,
                "unit": "seconds",
                "impact_modules": ["api", "worker"],
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
            },
        )
        if not created and not definition.is_active:
            definition.is_active = True
            definition.save(update_fields=("is_active", "updated_at"))
        definitions[key] = (definition, initial, initial_size, initial_sha)

    ConfigDefinition.objects.filter(key="workflow.smoke_timeout_seconds").update(
        is_active=False,
        needs_review=False,
        review_reason="0055 replaced by separate persisted idle/hard deadlines",
        updated_at=now,
    )
    active = ConfigRevision.objects.filter(
        scope_type="system", status="published", activation_state="active",
    ).order_by("-revision").first()
    existing = {}
    if active:
        existing = {
            row.definition.key: row
            for row in ConfigValue.objects.filter(revision=active).select_related(
                "definition", "credential_version",
            )
            if row.definition.key != "workflow.smoke_timeout_seconds"
        }
    if all(key in existing for key in definitions):
        return
    next_revision = (
        ConfigRevision.objects.filter(scope_type="system")
        .order_by("-revision").values_list("revision", flat=True).first() or 0
    ) + 1
    revision = ConfigRevision.objects.create(
        scope_type="system",
        revision=next_revision,
        status="published",
        based_on_revision_id=active.pk if active else None,
        activation_state="inactive",
        change_summary="0055 持久化 idle/hard execution deadlines",
        published_at=now,
    )
    digest_values = {}
    rows = []
    for key, row in existing.items():
        rows.append(ConfigValue(
            revision=revision,
            definition_id=row.definition_id,
            value=row.value,
            value_schema_version=row.value_schema_version,
            value_size_bytes=row.value_size_bytes,
            value_sha256=row.value_sha256,
            credential_version_id=row.credential_version_id,
        ))
        if row.credential_version_id:
            identity = f"version:{row.credential_version_id}:{row.credential_version.version}"
            digest_values[key] = {
                "credential_configured": True,
                "credential_binding_sha256": hashlib.sha256(identity.encode()).hexdigest(),
            }
        else:
            digest_values[key] = row.value
    for key, (definition, initial, size, sha) in definitions.items():
        if key in existing:
            continue
        rows.append(ConfigValue(
            revision=revision,
            definition=definition,
            value=initial,
            value_schema_version=1,
            value_size_bytes=size,
            value_sha256=sha,
        ))
        digest_values[key] = initial
    ConfigValue.objects.bulk_create(rows)
    revision.values_sha256 = hashlib.sha256(_canonical(digest_values)).hexdigest()
    revision.save(update_fields=("values_sha256", "updated_at"))
    if active:
        ConfigRevision.objects.filter(pk=active.pk).update(
            status="superseded", activation_state="inactive", updated_at=now,
        )
    ConfigRevision.objects.filter(pk=revision.pk).update(
        activation_state="active", activated_at=now, updated_at=now,
    )


class Migration(migrations.Migration):
    dependencies = [
        migrations.swappable_dependency(settings.AUTH_USER_MODEL),
        ("studio", "0054_runtimemodelavailability_observed_facts"),
    ]

    operations = [
        migrations.AddField(
            model_name="generationtask", name="deadline_started_at",
            field=models.DateTimeField(blank=True, null=True),
        ),
        migrations.AddField(
            model_name="generationtask", name="hard_deadline_at",
            field=models.DateTimeField(blank=True, db_index=True, null=True),
        ),
        migrations.AddField(
            model_name="generationtask", name="idle_deadline_at",
            field=models.DateTimeField(blank=True, db_index=True, null=True),
        ),
        migrations.AddField(
            model_name="generationtask", name="last_meaningful_progress_at",
            field=models.DateTimeField(blank=True, null=True),
        ),
        migrations.AddField(
            model_name="generationtask", name="progress_marker",
            field=models.CharField(blank=True, max_length=191),
        ),
        migrations.AddField(
            model_name="generationattempt", name="deadline_started_at",
            field=models.DateTimeField(blank=True, null=True),
        ),
        migrations.AddField(
            model_name="generationattempt", name="hard_deadline_at",
            field=models.DateTimeField(blank=True, db_index=True, null=True),
        ),
        migrations.AddField(
            model_name="generationattempt", name="idle_deadline_at",
            field=models.DateTimeField(blank=True, db_index=True, null=True),
        ),
        migrations.AddField(
            model_name="generationattempt", name="last_meaningful_progress_at",
            field=models.DateTimeField(blank=True, null=True),
        ),
        migrations.AddField(
            model_name="generationattempt", name="progress_marker",
            field=models.CharField(blank=True, max_length=191),
        ),
        migrations.AlterField(
            model_name="modeldeploymentsmokeattempt", name="status",
            field=models.CharField(
                choices=[
                    ("queued", "排队中"), ("submitting", "正在提交"),
                    ("submitted", "已提交"), ("cancel_requested", "取消中"),
                    ("succeeded", "成功"), ("failed", "失败"),
                    ("cancelled", "已取消"), ("uncertain", "远端状态不确定"),
                ], max_length=24,
            ),
        ),
        *[
            migrations.AddField(model_name="modeldeploymentsmokeattempt", name=name, field=field)
            for name, field in (
                ("provider_request_id", models.CharField(blank=True, max_length=191)),
                ("idempotency_key", models.CharField(blank=True, max_length=191)),
                ("request_snapshot", models.JSONField(blank=True, default=dict)),
                ("claim_token", models.UUIDField(blank=True, db_index=True, editable=False, null=True)),
                ("worker_id", models.CharField(blank=True, max_length=160)),
                ("claimed_at", models.DateTimeField(blank=True, null=True)),
                ("heartbeat_at", models.DateTimeField(blank=True, null=True)),
                ("submitted_at", models.DateTimeField(blank=True, null=True)),
                ("last_polled_at", models.DateTimeField(blank=True, null=True)),
                ("next_poll_at", models.DateTimeField(blank=True, db_index=True, null=True)),
                ("poll_count", models.PositiveIntegerField(default=0)),
                ("remote_status", models.CharField(blank=True, max_length=120)),
                ("deadline_started_at", models.DateTimeField(blank=True, null=True)),
                ("hard_deadline_at", models.DateTimeField(blank=True, db_index=True, null=True)),
                ("idle_deadline_at", models.DateTimeField(blank=True, db_index=True, null=True)),
                ("last_meaningful_progress_at", models.DateTimeField(blank=True, null=True)),
                ("progress_marker", models.CharField(blank=True, max_length=191)),
                ("cancel_requested_at", models.DateTimeField(blank=True, null=True)),
                ("cancel_submitted_at", models.DateTimeField(blank=True, null=True)),
                ("error_code", models.CharField(blank=True, max_length=100)),
                ("error_message", models.TextField(blank=True)),
            )
        ],
        migrations.CreateModel(
            name="WorkflowSmokeAttempt",
            fields=[
                ("id", models.UUIDField(default=uuid.uuid4, editable=False, primary_key=True, serialize=False)),
                ("created_at", models.DateTimeField(auto_now_add=True)),
                ("updated_at", models.DateTimeField(auto_now=True)),
                ("status", models.CharField(choices=[
                    ("queued", "排队中"), ("submitting", "正在提交"),
                    ("running", "执行中"), ("cancel_requested", "取消中"),
                    ("succeeded", "成功"), ("failed", "失败"),
                    ("cancelled", "已取消"), ("uncertain", "远端状态不确定"),
                ], default="queued", max_length=24)),
                ("submission_token", models.UUIDField(default=uuid.uuid4, editable=False, unique=True)),
                ("provider_request_id", models.CharField(blank=True, max_length=191)),
                ("graph_sha256", models.CharField(max_length=64)),
                ("execution_contract_sha256", models.CharField(max_length=64)),
                ("validation_evidence_sha256", models.CharField(max_length=64)),
                ("target_sha256", models.CharField(max_length=64)),
                ("claim_token", models.UUIDField(blank=True, db_index=True, editable=False, null=True)),
                ("worker_id", models.CharField(blank=True, max_length=160)),
                ("claimed_at", models.DateTimeField(blank=True, null=True)),
                ("heartbeat_at", models.DateTimeField(blank=True, null=True)),
                ("submission_started_at", models.DateTimeField(blank=True, null=True)),
                ("submitted_at", models.DateTimeField(blank=True, null=True)),
                ("last_polled_at", models.DateTimeField(blank=True, null=True)),
                ("next_poll_at", models.DateTimeField(blank=True, db_index=True, null=True)),
                ("poll_count", models.PositiveIntegerField(default=0)),
                ("remote_status", models.CharField(blank=True, max_length=120)),
                ("deadline_started_at", models.DateTimeField()),
                ("hard_deadline_at", models.DateTimeField(db_index=True)),
                ("idle_deadline_at", models.DateTimeField(db_index=True)),
                ("last_meaningful_progress_at", models.DateTimeField()),
                ("progress_marker", models.CharField(blank=True, max_length=191)),
                ("cancel_requested_at", models.DateTimeField(blank=True, null=True)),
                ("cancel_submitted_at", models.DateTimeField(blank=True, null=True)),
                ("finished_at", models.DateTimeField(blank=True, null=True)),
                ("error_code", models.CharField(blank=True, max_length=100)),
                ("error_message", models.TextField(blank=True)),
                ("result", models.JSONField(blank=True, default=dict)),
                ("created_by", models.ForeignKey(on_delete=django.db.models.deletion.PROTECT, related_name="workflow_smoke_attempts", to=settings.AUTH_USER_MODEL)),
                ("deployment", models.ForeignKey(on_delete=django.db.models.deletion.PROTECT, related_name="workflow_smoke_attempts", to="studio.modeldeployment")),
                ("workflow_version", models.ForeignKey(on_delete=django.db.models.deletion.PROTECT, related_name="smoke_attempts", to="studio.workflowtemplateversion")),
            ],
            options={
                "ordering": ("-created_at",),
                "indexes": [models.Index(fields=["status", "next_poll_at"], name="studio_work_status_70a205_idx")],
                "constraints": [models.UniqueConstraint(
                    condition=models.Q(("status__in", ("queued", "submitting", "running", "cancel_requested"))),
                    fields=("workflow_version", "deployment"),
                    name="one_active_workflow_smoke",
                )],
            },
        ),
        migrations.AddConstraint(
            model_name="modeldeploymentsmokeattempt",
            constraint=models.UniqueConstraint(
                condition=models.Q(("status__in", ("queued", "submitting", "submitted", "cancel_requested"))),
                fields=("deployment", "operation"),
                name="one_active_deployment_smoke",
            ),
        ),
        migrations.RunPython(add_deadline_configuration, migrations.RunPython.noop),
    ]
