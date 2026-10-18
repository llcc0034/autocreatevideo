"""Materialize the already-hashed protocol, without rewriting version facts.

This one-time snapshot is deliberately independent of the runtime registry.
Any record that cannot reproduce its recorded digest stops the migration.
"""
import hashlib
import json

from django.db import migrations, models


PARSER = {
    "key": "visual_rules.v2", "operation": "visual_rules.generate", "response_schema_version": 2,
    "fields": [
        {"field": "continuity_summary", "label": "已审核连续性说明", "response_field": "continuity_summary",
         "transformation": "校验完整英文正文后保留，不自动截断。", "source_selector": "reviewed_visual_rules.continuity_summary"},
        {"field": "color_script", "label": "已审核色彩脚本", "response_field": "color_design",
         "transformation": "按平台色彩枚举渲染为英文色彩脚本；不是模型直接返回的第二份正文。", "source_selector": "reviewed_visual_rules.color_script"},
        {"field": "negative_prompt", "label": "已审核负面约束", "response_field": "negative_constraints",
         "transformation": "模型负面约束与本版本建议短语去重合并；审核时可明确修改或移除。", "source_selector": "reviewed_visual_rules.negative_prompt"},
    ],
    "response_example": {"schema_version": 2,
        "color_design": {"base_colors": ["teal", "slate"], "accent_color": "amber", "palette_relation": "cinematic",
            "lighting": "soft_diffuse", "contrast": "restrained", "saturation": "muted", "exposure": "balanced",
            "progression": "stable", "preserve_natural_skin_exposure": True},
        "negative_constraints": ["story-specific visual defect phrase"],
        "continuity_summary": "English project-wide visual continuity rules"},
}


def freeze_parsers(apps, schema_editor):
    outputs = apps.get_model("studio", "BusinessOutputVersion").objects.using(schema_editor.connection.alias)
    for output in outputs.all().iterator(chunk_size=100):
        value = {"schema_version": 1, "id": str(output.pk), "output_key": str(output.output_key),
            "version": output.version, "name": output.name, "description": output.description,
            "parser": PARSER, "quality_advisories": output.quality_advisories}
        encoded = json.dumps(value, ensure_ascii=False, sort_keys=True, separators=(",", ":"), allow_nan=False).encode("utf-8")
        if (output.parser_key != PARSER["key"] or len(encoded) > 65536
                or len(encoded) != output.contract_size_bytes
                or hashlib.sha256(encoded).hexdigest() != output.contract_sha256):
            raise RuntimeError(f"Output {output.pk}: recorded contract cannot be reproduced; no facts were replaced")
        outputs.filter(pk=output.pk).update(parser_contract=PARSER)


class Migration(migrations.Migration):
    dependencies = [("studio", "0185_business_output_versions")]
    operations = [
        migrations.AddField(model_name="businessoutputversion", name="parser_contract",
            field=models.JSONField(default=dict, editable=False)),
        migrations.RunPython(freeze_parsers),
    ]
