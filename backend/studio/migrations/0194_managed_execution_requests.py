"""Move exact retained request bytes, not recompiled requests, to private storage.

Run under the normal maintenance/backup window. A failed transaction may leave
unreferenced files; never remove original database facts or auto-delete files.
No runtime dual-read path remains after this migration.
"""
import hashlib
import json

import django.core.validators
from django.core.files.base import ContentFile
from django.core.files.storage import default_storage
from django.db import migrations, models


def canonical(value):
    return json.dumps(value, ensure_ascii=False, sort_keys=True, separators=(",", ":"), allow_nan=False).encode("utf-8")


def verify(row, prefix):
    value = getattr(row, prefix)
    data = canonical(value)
    if (not isinstance(value, dict) or value.get("preparation_id") != str(row.preparation_id)
            or getattr(row, prefix + "_schema_version") != 1
            or len(data) != getattr(row, prefix + "_size_bytes")
            or hashlib.sha256(data).hexdigest() != getattr(row, prefix + "_sha256")):
        raise RuntimeError(f"Request migration refused inconsistent {prefix} manifest on {row.pk}")
    return data


def move_requests(apps, schema_editor):
    alias = schema_editor.connection.alias
    details = apps.get_model("studio", "ExecutionPreparationContract").objects.using(alias)
    attempts = apps.get_model("studio", "GenerationAttempt").objects.using(alias)
    # Verify every source and peer before producing any new files. Migration
    # preserves hashes, sizes, IDs, timestamps, reviews, results and costs.
    for row in details.exclude(prepared_request=None).select_related("preparation").iterator():
        verify(row, "prepared_request")
        if row.preparation.request_sha256 != row.prepared_request_sha256 or not row.preparation.request_frozen_at:
            raise RuntimeError(f"Request migration refused unfrozen or mismatched preparation {row.preparation_id}")
    for row in attempts.iterator():
        data = verify(row, "request_snapshot")
        detail = details.get(preparation_id=row.preparation_id)
        if row.request_sha256 != detail.prepared_request_sha256 or data != verify(detail, "prepared_request"):
            raise RuntimeError(f"Request migration refused divergent production attempt {row.pk}")
    for row in details.exclude(prepared_request=None).iterator():
        data = verify(row, "prepared_request")
        key = f"execution-requests/{row.preparation_id}/{row.prepared_request_sha256}.json"
        if not default_storage.exists(key):
            actual = default_storage.save(key, ContentFile(data))
            if actual != key:
                raise RuntimeError("Request migration encountered a concurrent storage write")
        with default_storage.open(key, "rb") as stream:
            if stream.read(len(data) + 1) != data:
                raise RuntimeError(f"Request migration read-back failed for preparation {row.preparation_id}")
        details.filter(pk=row.pk).update(prepared_request_key=key)
        attempts.filter(preparation_id=row.preparation_id).update(request_snapshot_key=key)


class Migration(migrations.Migration):
    dependencies = [("studio", "0193_asset_text_response_usage")]
    operations = [
        migrations.AddField(model_name="executionpreparationcontract", name="prepared_request_key",
            field=models.CharField(max_length=191, null=True, blank=True)),
        migrations.AddField(model_name="generationattempt", name="request_snapshot_key",
            field=models.CharField(max_length=191, default=""), preserve_default=False),
        migrations.RunPython(move_requests),
        migrations.RemoveConstraint(model_name="executionpreparationcontract", name="ck_exec_prepared_metadata"),
        migrations.RemoveField(model_name="executionpreparationcontract", name="prepared_request"),
        migrations.RemoveField(model_name="generationattempt", name="request_snapshot"),
        migrations.AlterField(model_name="executionpreparationcontract", name="prepared_request_size_bytes",
            field=models.PositiveIntegerField(null=True, blank=True, validators=[django.core.validators.MinValueValidator(2)])),
        migrations.AddConstraint(model_name="executionpreparationcontract", constraint=models.CheckConstraint(
            condition=(models.Q(prepared_request_key__isnull=True, prepared_request_schema_version__isnull=True,
                               prepared_request_size_bytes__isnull=True, prepared_request_sha256__isnull=True)
                | models.Q(prepared_request_key__isnull=False, prepared_request_schema_version__isnull=False,
                           prepared_request_size_bytes__isnull=False, prepared_request_sha256__isnull=False,
                           prepared_request_schema_version=1, prepared_request_size_bytes__gte=2)),
            name="ck_exec_prepared_metadata")),
    ]
