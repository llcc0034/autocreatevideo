"""Prepare reviewable successors, not a silent replacement of published prompts."""
from copy import deepcopy
from importlib import import_module
import hashlib
import json

from django.db import migrations, models


# Example DATA for the review draft. The compiler has no runtime default text.
RENDERING = {
    "separator": "\n",
    "list_prefix": "- ",
    "prompt_template_key": "reference_context_configured_v1",
    "templates": {
        "reference": "{label}: {subject}; category={category}; purpose={purpose}; source={source_kind}; paired video={parent_label}; {description}; {guidance}",
        "retention": "{label} defines {subject}; preserve the declared {purpose}. Guidance: {guidance}. Used in shots: {shots}.",
        "summary": "Mode: {mode}. Generate {shot_count} shots over {duration_seconds} seconds, with {reference_count} supplied references. Audio enabled: {include_audio}.",
        "shot": "[Shot {index}] At {start_seconds} seconds, duration {duration_seconds} seconds. {narrative}\n{description}\nAction: {action}\nCamera: {camera}\nSubjects: {subjects}\nEnvironment: {environment}\nLighting: {lighting}\nColors: {color_palette}\nReferences: {references}\nContinuity: {continuity}\nVoice profiles: {voice_profiles}\nFirst-frame continuity required: {requires_first_frame_continuity}; last-frame continuity required: {requires_last_frame_continuity}.\n{dialogue}",
        "reviewed_shot": "[Shot {index}] At {start_seconds} seconds; subjects: {subject_labels}.\n{description}\nFirst-frame continuity required: {requires_first_frame_continuity}; last-frame continuity required: {requires_last_frame_continuity}.\n{dialogue}",
        "dialogue": "{subject} ({speaker_id}) {start_ms}–{end_ms} ms; voice={voice_profile}; delivery={delivery}: <d>[{language}] {text}</d>",
        "voiceover_dialogue": "Off-screen voiceover {subject} ({speaker_id}) {start_ms}–{end_ms} ms; voice={voice_profile}; delivery={delivery}: <d>[{language}] {text}</d>. No on-screen lip movement for this speech.",
        "style": "Visual style: {visual_prompt}\nColor design: {color_script}",
        "sound": "[Shot {index}] {description}",
        "music": "[Shot {index}] {description}",
        "direction": "Across all shots: {description}",
        "empty_references": "No reference media supplied; use the text-defined scene.",
        "empty_retention": "No external reference relationships to preserve.",
        "silent_sound": "No audio output.",
        "silent_music": "No music output.",
    },
}


def prepare_review_drafts(apps, schema_editor):
    alias = schema_editor.connection.alias
    Profile = apps.get_model("studio", "PromptProfileVersion")
    Rule = apps.get_model("studio", "PromptProfileFieldRule")
    Audit = apps.get_model("studio", "PlatformLifecycleAuditEvent")
    historical = import_module("studio.migrations.0132_h3_declarative_prompt_profile_v19")
    seen = set()
    sources = Profile.objects.using(alias).filter(
        status="published",
        source_projection_key__in=("declarative_v1", "classified_reference_context_ir_v1"),
    ).order_by("profile_key", "-version")
    for source in sources:
        if source.profile_key in seen:
            continue
        seen.add(source.profile_key)
        if (source.source_projection_config or {}).get("schema_version") == 4:
            continue
        marker = f"0140:source:{source.pk}"
        if Audit.objects.using(alias).filter(reason_code="reference_rendering_review_draft", before_summary__prompt_profile_version_id=str(source.pk)).exists():
            continue
        version = Profile.objects.using(alias).filter(profile_key=source.profile_key).order_by("-version").values_list("version", flat=True).first() + 1
        config = {"schema_version": 4, "primitive_options": {"reference_context_ir_v1": {"rendering": deepcopy(RENDERING)}}}
        if "quality_advisories" in (source.source_projection_config or {}):
            config["quality_advisories"] = deepcopy(source.source_projection_config["quality_advisories"])
        target = Profile.objects.using(alias).create(
            profile_key=source.profile_key, version=version,
            name=(source.name[:130] + " · 通用渲染待复核"),
            description=(f"从 v{source.version} 创建的通用渲染后继草稿（{marker}）。"
                         "模板是可编辑示例，不承诺与旧内置文案逐字相同；需复核素材身份、配音语义、字段长度和工作流模板标识，再发布并显式切换绑定。"),
            model_family=source.model_family, operation=source.operation, mode=source.mode,
            source_projection_key="declarative_v1", source_projection_config=config,
            official_reference=source.official_reference, channel_limits=deepcopy(source.channel_limits),
            review_state="needs_review", status="draft", contract_schema_version=6,
        )
        for rule in Rule.objects.using(alias).filter(profile_version_id=source.pk):
            values = {field.attname: deepcopy(getattr(rule, field.attname)) for field in Rule._meta.concrete_fields
                      if field.name not in {"id", "created_at", "updated_at", "profile_version"}}
            selector = values["source_selector"]
            if selector.startswith("request.h3_ref2va."):
                result_field = selector.removeprefix("request.h3_ref2va.")
                if result_field not in historical.PRIMITIVES:
                    raise RuntimeError(f"0140 无法转换历史字段 {selector}")
                values["source_selector"] = "projection.output"
                values["projection_primitive"] = historical.PRIMITIVES[result_field]
                if values["generation_source_selector"] == selector:
                    values["generation_source_selector"] = "projection.output"
            Rule.objects.using(alias).create(profile_version_id=target.pk, **values)
        rules = list(Rule.objects.using(alias).filter(profile_version_id=target.pk).select_related("field_definition").order_by("position", "id"))
        encoded = historical._canonical_bytes(historical._profile_contract(target, rules))
        Profile.objects.using(alias).filter(pk=target.pk).update(contract_size_bytes=len(encoded), contract_sha256=hashlib.sha256(encoded).hexdigest())
        before = {"prompt_profile_version_id": str(source.pk), "contract_sha256": source.contract_sha256}
        after = {"prompt_profile_version_id": str(target.pk), "version": version, "review_state": "needs_review"}
        digest = lambda value: hashlib.sha256(json.dumps(value, sort_keys=True, separators=(",", ":")).encode()).hexdigest()
        Audit.objects.using(alias).create(action="create", object_type="prompt_profile_version", object_id=target.pk,
            reason_code="reference_rendering_review_draft", before_summary=before, after_summary=after,
            before_sha256=digest(before), after_sha256=digest(after))


class Migration(migrations.Migration):
    dependencies = [("studio", "0139_configurable_image_resolution_limits")]
    operations = [
        migrations.AlterField(model_name="promptprofileversion", name="source_projection_config",
            field=models.JSONField(blank=True, default=dict, help_text="来源投影器的版本化数据合同；只接受登记的声明式配置与简单占位符模板，不接受表达式、函数或自定义正则。")),
        migrations.RunPython(prepare_review_drafts, migrations.RunPython.noop),
    ]
