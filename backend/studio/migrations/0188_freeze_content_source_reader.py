"""Materialize the exact reader already covered by each recorded source digest.

This migration deliberately uses historical literals, not runtime catalogs.
Failure rolls back the migration; no digest, revision or audit fact is repaired.
"""
import hashlib
import json

from django.db import migrations, models


def historical_reader(selector):
    if selector in ("project.logline", "style.visual_prompt"):
        return {"protocol": "project_snapshot/v1", "selector": selector, "scope_kind": "project",
            "value_kind": "text", "snapshot_policy": "confirmed_preview"}
    fields = {
        "reviewed_visual_rules.continuity_summary": "continuity_summary",
        "reviewed_visual_rules.color_script": "color_script",
        "reviewed_visual_rules.negative_prompt": "negative_prompt",
    }
    if selector in fields:
        return {"protocol": "reviewed_visual_rules/v1", "selector": selector, "scope_kind": "project",
            "value_kind": "text", "snapshot_policy": "exact_current_approval",
            "stage_key": "visual_rules_review", "output_schema_version": 3, "output_field": fields[selector]}
    return None


def freeze_readers(apps, schema_editor):
    sources = apps.get_model("studio", "ContentSourceVersion").objects.using(schema_editor.connection.alias)
    for source in sources.all().iterator(chunk_size=100):
        reader = historical_reader(source.reader_selector)
        value = {"schema_version": 1, "id": str(source.pk), "source_key": str(source.source_key),
            "version": source.version, "name": source.name, "description": source.description, "reader": reader}
        encoded = json.dumps(value, ensure_ascii=False, sort_keys=True, separators=(",", ":"), allow_nan=False).encode("utf-8")
        if (reader is None or len(encoded) > 4096 or len(encoded) != source.contract_size_bytes
                or hashlib.sha256(encoded).hexdigest() != source.contract_sha256):
            raise RuntimeError(f"Source {source.pk}: recorded contract cannot be reproduced; no facts were replaced")
        sources.filter(pk=source.pk).update(reader_contract=reader)


class Migration(migrations.Migration):
    dependencies = [("studio", "0187_production_text_response_usage")]
    operations = [
        migrations.AddField(model_name="contentsourceversion", name="reader_contract",
            field=models.JSONField(default=dict, editable=False)),
        migrations.RunPython(freeze_readers),
    ]
