import hashlib
import json

from django.db import migrations


PROFILE_SPECS = (
    {
        "profile_key": "deepseek.script.structure",
        "name": "DeepSeek 导入剧本结构化装配策略",
        "model_family": "deepseek",
        "official_reference": "https://api-docs.deepseek.com/guides/json_mode/",
    },
    {
        "profile_key": "qwen.script.structure",
        "name": "Qwen 导入剧本结构化装配策略",
        "model_family": "qwen",
        "official_reference": "https://qwen.readthedocs.io/en/stable/deployment/vllm.html",
    },
)

RULE_SPECS = (
    ("system_guidance", "definition.default", "system_instruction"),
    ("theme", "request.theme", "structured_input"),
    ("narrative", "request.text", "structured_input"),
    ("language", "request.language", "structured_input"),
)


def _canonical(value):
    return json.dumps(
        value,
        ensure_ascii=False,
        sort_keys=True,
        separators=(",", ":"),
        allow_nan=False,
    ).encode("utf-8")


def seed_script_structure_profiles(apps, schema_editor):
    Field = apps.get_model("studio", "PromptFieldDefinition")
    Profile = apps.get_model("studio", "PromptProfileVersion")
    Rule = apps.get_model("studio", "PromptProfileFieldRule")

    definitions = {
        item.key: item
        for item in Field.objects.filter(key__in=[row[0] for row in RULE_SPECS])
    }
    missing = sorted(set(row[0] for row in RULE_SPECS) - set(definitions))
    if missing:
        raise RuntimeError(
            "script.structure PromptProfile 缺少字段定义: " + ", ".join(missing)
        )

    for spec in PROFILE_SPECS:
        profile, _ = Profile.objects.update_or_create(
            profile_key=spec["profile_key"],
            version=1,
            defaults={
                "name": spec["name"],
                "description": (
                    "内置导入剧本忠实结构化策略，仅作为待审核草稿；管理员必须核对"
                    "模型 JSON 输出能力、完整源行覆盖合同和本地部署后才能标记 ready 并发布。"
                ),
                "model_family": spec["model_family"],
                "operation": "script.structure",
                "mode": "",
                "official_reference": spec["official_reference"],
                "status": "draft",
                "review_state": "needs_review",
                "published_at": None,
                "published_by": None,
            },
        )
        Rule.objects.filter(profile_version=profile).delete()
        for position, (field_key, selector, channel) in enumerate(RULE_SPECS, start=1):
            Rule.objects.create(
                profile_version=profile,
                field_definition=definitions[field_key],
                source_selector=selector,
                channel=channel,
                position=position,
                label_style="label_colon",
                separator="newline",
                required=False,
                omit_if_empty=True,
                max_chars=30_000 if field_key in {"system_guidance", "narrative"} else 4_000,
            )

        rules = []
        for rule in Rule.objects.filter(profile_version=profile).select_related(
            "field_definition",
        ).order_by("position", "id"):
            definition = rule.field_definition
            rules.append({
                "field_key": definition.key,
                "field_value_type": definition.value_type,
                "field_default_value": definition.default_value,
                "field_value_options": definition.value_options,
                "field_required": definition.is_required,
                "source_selector": rule.source_selector,
                "channel": rule.channel,
                "position": rule.position,
                "label_style": rule.label_style,
                "separator": rule.separator,
                "required": rule.required,
                "omit_if_empty": rule.omit_if_empty,
                "max_chars": rule.max_chars,
            })
        contract = {
            "schema_version": 1,
            "profile_key": profile.profile_key,
            "version": profile.version,
            "model_family": profile.model_family,
            "operation": profile.operation,
            "mode": profile.mode,
            "rules": rules,
        }
        encoded = _canonical(contract)
        profile.contract_schema_version = 1
        profile.contract_size_bytes = len(encoded)
        profile.contract_sha256 = hashlib.sha256(encoded).hexdigest()
        profile.save(update_fields=(
            "contract_schema_version",
            "contract_size_bytes",
            "contract_sha256",
            "updated_at",
        ))


def unseed_script_structure_profiles(apps, schema_editor):
    Profile = apps.get_model("studio", "PromptProfileVersion")
    Profile.objects.filter(
        profile_key__in=[item["profile_key"] for item in PROFILE_SPECS],
        version=1,
    ).delete()


class Migration(migrations.Migration):
    dependencies = [
        ("studio", "0058_prompt_profiles"),
    ]

    operations = [
        migrations.RunPython(
            seed_script_structure_profiles,
            unseed_script_structure_profiles,
        ),
    ]
