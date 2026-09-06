from django.db import migrations


DESCRIPTIONS = {
    "production.recommended_max_pending": (
        "待处理任务数量建议值；超过后仅生成告警并继续，不作为生产阻断条件"
    ),
    "production.recommended_max_active_per_user": (
        "单用户活跃任务数量建议值；超过后仅生成告警并继续，不作为生产阻断条件"
    ),
}

LEGACY_DESCRIPTIONS = {
    "production.recommended_max_pending": "待处理任务数量的确认建议阈值",
    "production.recommended_max_active_per_user": "单个用户活跃任务数量的确认建议阈值",
}


def publish_advisory_descriptions(apps, schema_editor):
    ConfigDefinition = apps.get_model("studio", "ConfigDefinition")
    definitions = {
        row.key: row
        for row in ConfigDefinition.objects.filter(key__in=DESCRIPTIONS)
    }
    missing = sorted(set(DESCRIPTIONS) - set(definitions))
    if missing:
        raise RuntimeError(
            "Cannot publish queue advisory contract; definitions missing: "
            + ", ".join(missing)
        )
    for key, description in DESCRIPTIONS.items():
        ConfigDefinition.objects.filter(pk=definitions[key].pk).update(
            description=description,
        )


def restore_confirmation_descriptions(apps, schema_editor):
    ConfigDefinition = apps.get_model("studio", "ConfigDefinition")
    for key, description in LEGACY_DESCRIPTIONS.items():
        ConfigDefinition.objects.filter(key=key).update(description=description)


class Migration(migrations.Migration):
    dependencies = [
        ("studio", "0093_prompt_profile_official_reference_layout"),
    ]

    operations = [
        migrations.RunPython(
            publish_advisory_descriptions,
            restore_confirmation_descriptions,
        ),
    ]
