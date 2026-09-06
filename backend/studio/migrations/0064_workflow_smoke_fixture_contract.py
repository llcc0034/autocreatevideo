import hashlib
import json
import uuid

import django.core.validators
import django.db.models.deletion
from django.db import migrations, models
from django.utils import timezone


def _contract(value):
    payload = json.dumps(
        value,
        ensure_ascii=False,
        separators=(",", ":"),
        sort_keys=True,
        allow_nan=False,
    ).encode("utf-8")
    return len(payload), hashlib.sha256(payload).hexdigest()


def freeze_existing_facts(apps, schema_editor):
    Source = apps.get_model("studio", "ComfyWorkflowSource")
    Attempt = apps.get_model("studio", "WorkflowSmokeAttempt")
    Version = apps.get_model("studio", "WorkflowTemplateVersion")

    for source in Source.objects.all().iterator():
        report = source.conversion_report if isinstance(source.conversion_report, dict) else {}
        digest = str(report.get("import_contract_sha256") or "").casefold()
        if len(digest) != 64:
            # Legacy rows never froze an import contract.  Use a tagged,
            # deterministic migration identity so the new uniqueness column
            # cannot misrepresent a conversion-report hash as that contract.
            digest = _contract({
                "schema_version": 0,
                "legacy_source_id": str(source.pk),
                "conversion_report_sha256": str(
                    source.conversion_report_sha256 or "",
                ).casefold(),
            })[1]
        source.import_contract_sha256 = digest
        source.save(update_fields=("import_contract_sha256",))

    terminalized_versions = {}
    for attempt in Attempt.objects.select_related("workflow_version").iterator():
        # Old attempts did not freeze the exact values or Asset facts used by
        # their worker.  Even a currently attached immutable version is not
        # proof of what was materialized then, so do not fabricate history.
        values = {}
        value_size, value_sha = _contract(values)
        # Mark the fixture schema as legacy (0). A new successful schema-v1
        # attempt is required to publish.
        fixture_sha = ""
        attempt.fixture_values = values
        attempt.fixture_values_schema_version = 0
        attempt.fixture_values_size_bytes = value_size
        attempt.fixture_values_sha256 = value_sha
        attempt.fixture_sha256 = fixture_sha
        update_fields = [
            "fixture_values", "fixture_values_schema_version",
            "fixture_values_size_bytes", "fixture_values_sha256",
            "fixture_sha256",
        ]
        if attempt.status in {
            "queued", "submitting", "running", "cancel_requested",
        }:
            crossed_submission_boundary = bool(
                attempt.submission_started_at
                or attempt.submitted_at
                or attempt.provider_request_id
            )
            attempt.status = "uncertain" if crossed_submission_boundary else "failed"
            attempt.error_code = (
                "legacy_smoke_fixture_uncertain"
                if crossed_submission_boundary
                else "legacy_smoke_fixture_unavailable"
            )
            attempt.error_message = (
                "迁移前任务已越过远端提交边界，但未冻结可验证 fixture；"
                "必须人工核对远端事实，不能作为发布证据。"
                if crossed_submission_boundary
                else "迁移前排队任务未冻结可验证 fixture，已在远端提交前安全终止。"
            )
            attempt.finished_at = timezone.now()
            attempt.claim_token = None
            attempt.worker_id = ""
            attempt.claimed_at = None
            attempt.next_poll_at = None
            terminalized_versions[attempt.workflow_version_id] = bool(
                terminalized_versions.get(attempt.workflow_version_id)
                or crossed_submission_boundary
            )
            update_fields.extend((
                "status", "error_code", "error_message", "finished_at",
                "claim_token", "worker_id", "claimed_at", "next_poll_at",
            ))
        attempt.save(update_fields=update_fields)

    # Every pre-migration smoke attempt lacks a trustworthy per-attempt
    # fixture.  A mutable draft/validated version must therefore not keep a
    # legacy PASSED badge or evidence that the new publish gate cannot accept.
    # Published/retired rows remain untouched historical facts.
    Version.objects.exclude(status__in=("published", "retired")).filter(
        models.Q(smoke_test_state="passed")
        | ~models.Q(smoke_test_evidence_sha256="")
        | ~models.Q(smoke_test_evidence={})
    ).update(
        smoke_test_state="not_run",
        smoke_test_message=(
            "迁移前 smoke-test 未冻结可验证 fixture；请使用当前合同重新执行真实 smoke-test。"
        ),
        smoke_test_evidence={},
        smoke_test_evidence_sha256="",
    )

    for version_id, remote_uncertain in terminalized_versions.items():
        Version.objects.filter(pk=version_id).update(
            smoke_test_state="failed",
            smoke_test_message=(
                "迁移前 smoke-test 已越过远端提交边界但缺少可验证 fixture；"
                "必须人工核对远端事实并创建新的 schema-v1 attempt。"
                if remote_uncertain
                else "迁移前排队 smoke-test 缺少可验证 fixture，已在提交前安全终止。"
            ),
            smoke_test_evidence={},
            smoke_test_evidence_sha256="",
        )


class Migration(migrations.Migration):

    dependencies = [
        ("studio", "0063_runtime_probe_attestation_contract"),
    ]

    operations = [
        migrations.RemoveConstraint(
            model_name="comfyworkflowsource",
            name="unique_comfy_workflow_source",
        ),
        migrations.AddField(
            model_name="comfyworkflowsource",
            name="import_contract_sha256",
            field=models.CharField(default="", max_length=64),
            preserve_default=False,
        ),
        migrations.AddField(
            model_name="workflowsmokeattempt",
            name="fixture_sha256",
            field=models.CharField(blank=True, default="", max_length=64),
            preserve_default=False,
        ),
        migrations.AddField(
            model_name="workflowsmokeattempt",
            name="fixture_values",
            field=models.JSONField(blank=True, default=dict),
        ),
        migrations.AddField(
            model_name="workflowsmokeattempt",
            name="fixture_values_schema_version",
            field=models.PositiveIntegerField(default=1),
        ),
        migrations.AddField(
            model_name="workflowsmokeattempt",
            name="fixture_values_sha256",
            field=models.CharField(blank=True, default="", max_length=64),
            preserve_default=False,
        ),
        migrations.AddField(
            model_name="workflowsmokeattempt",
            name="fixture_values_size_bytes",
            field=models.PositiveIntegerField(default=2),
        ),
        migrations.CreateModel(
            name="WorkflowSmokeAssetBinding",
            fields=[
                ("id", models.UUIDField(default=uuid.uuid4, editable=False, primary_key=True, serialize=False)),
                ("created_at", models.DateTimeField(auto_now_add=True)),
                ("updated_at", models.DateTimeField(auto_now=True)),
                ("semantic_key", models.CharField(max_length=120)),
                ("input_slot", models.PositiveIntegerField()),
                ("media_kind", models.CharField(max_length=12)),
                ("category", models.CharField(max_length=80)),
                ("checksum", models.CharField(max_length=64, validators=[django.core.validators.RegexValidator("^[0-9a-f]{64}$")])),
                ("mime_type", models.CharField(max_length=120)),
                ("size_bytes", models.PositiveBigIntegerField()),
                ("asset", models.ForeignKey(on_delete=django.db.models.deletion.PROTECT, related_name="workflow_smoke_bindings", to="studio.asset")),
                ("attempt", models.ForeignKey(on_delete=django.db.models.deletion.PROTECT, related_name="asset_bindings", to="studio.workflowsmokeattempt")),
            ],
            options={
                "ordering": ("attempt", "media_kind", "input_slot", "semantic_key"),
            },
        ),
        # Frozen smoke/import hashes and terminalized remote-submission facts
        # are historical evidence.  A schema rollback may remove the new
        # columns, but must not fabricate the mutable values that preceded the
        # migration; keep those facts unchanged while allowing migration-test
        # executors to traverse the schema backwards.
        migrations.RunPython(freeze_existing_facts, migrations.RunPython.noop),
        migrations.AddConstraint(
            model_name="comfyworkflowsource",
            constraint=models.UniqueConstraint(
                fields=("source_deployment", "source_path_key", "source_sha256", "import_contract_sha256"),
                name="unique_comfy_workflow_source",
            ),
        ),
        migrations.AddConstraint(
            model_name="workflowsmokeassetbinding",
            constraint=models.UniqueConstraint(
                fields=("attempt", "semantic_key"),
                name="unique_workflow_smoke_semantic",
            ),
        ),
        migrations.AddConstraint(
            model_name="workflowsmokeassetbinding",
            constraint=models.UniqueConstraint(
                fields=("attempt", "media_kind", "input_slot"),
                name="unique_workflow_smoke_slot",
            ),
        ),
        migrations.AddConstraint(
            model_name="workflowsmokeassetbinding",
            constraint=models.CheckConstraint(
                condition=models.Q(("input_slot__gte", 1)),
                name="workflow_smoke_slot_positive",
            ),
        ),
        migrations.AddConstraint(
            model_name="workflowsmokeassetbinding",
            constraint=models.CheckConstraint(
                condition=models.Q(("size_bytes__gte", 1)),
                name="workflow_smoke_asset_size_positive",
            ),
        ),
        migrations.AddConstraint(
            model_name="workflowsmokeassetbinding",
            constraint=models.CheckConstraint(
                condition=models.Q(("media_kind__in", ("image", "video", "audio"))),
                name="workflow_smoke_media_kind_valid",
            ),
        ),
    ]
