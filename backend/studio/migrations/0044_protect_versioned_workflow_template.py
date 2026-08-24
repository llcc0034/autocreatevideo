import hashlib
import json

from django.db import migrations, models
import django.db.models.deletion


def _contract(value):
    payload = json.dumps(
        value,
        ensure_ascii=False,
        sort_keys=True,
        separators=(",", ":"),
    ).encode("utf-8")
    return len(payload), hashlib.sha256(payload).hexdigest()


def _has_delivery_closure(stages):
    rows = [item for item in stages if isinstance(item, dict)]
    keys = [str(item.get("key") or "") for item in rows]
    if len(keys) != len(set(keys)):
        return False
    by_key = {str(item.get("key") or ""): item for item in rows}
    required = (
        "storyboard", "prompt_compile", "media_generation",
        "artifact_selection", "assembly", "delivery",
    )
    if any(key not in by_key for key in required):
        return False
    content = [
        item for item in rows
        if (item.get("key"), item.get("mode")) in {
            ("script_input", "confirmed_revision"),
            ("script_input", "manual"),
            ("script_generate", "ai"),
        }
    ]
    if len(content) != 1:
        return False
    content_key = str(content[0]["key"])
    if content_key == "script_generate" and by_key.get("script_review", {}).get("mode") != "review":
        return False
    expected_modes = {
        "storyboard": {"deterministic", "manual"},
        "prompt_compile": "existing_pipeline",
        "media_generation": "existing_pipeline",
        "artifact_selection": "review",
        "assembly": "existing_pipeline",
        "delivery": "existing_pipeline",
    }
    if any(
        by_key[key].get("mode") not in (mode if isinstance(mode, set) else {mode})
        for key, mode in expected_modes.items()
    ):
        return False
    positions = {key: index for index, key in enumerate(keys)}
    ordered = [content_key]
    if content_key == "script_generate":
        ordered.append("script_review")
    ordered.extend(required)
    return [positions[key] for key in ordered] == sorted(positions[key] for key in ordered)


def normalize_delivery_recipe_closure(apps, schema_editor):
    Recipe = apps.get_model("studio", "ProductionRecipeVersion")
    for recipe in Recipe.objects.all().iterator():
        definition = recipe.definition if isinstance(recipe.definition, dict) else {}
        stages = definition.get("stages") if isinstance(definition.get("stages"), list) else []
        keys = [
            str(item.get("key") or "")
            for item in stages
            if isinstance(item, dict)
        ]
        changed = False
        if recipe.recipe_key == "default-series-production" and recipe.version == 1:
            if "artifact_selection" not in keys and "media_generation" in keys:
                media_index = keys.index("media_generation")
                stages.insert(media_index + 1, {
                    "key": "artifact_selection",
                    "mode": "review",
                    "approval": "required",
                })
                keys.insert(media_index + 1, "artifact_selection")
                changed = True
            if "delivery" not in keys and "assembly" in keys:
                assembly_index = keys.index("assembly")
                stages.insert(assembly_index + 1, {
                    "key": "delivery",
                    "mode": "existing_pipeline",
                    "approval": "auto",
                })
                keys.insert(assembly_index + 1, "delivery")
                changed = True
        update_fields = []
        if changed:
            definition["stages"] = stages
            size, digest = _contract(definition)
            recipe.definition = definition
            recipe.definition_schema_version = 1
            recipe.definition_size_bytes = size
            recipe.definition_sha256 = digest
            update_fields.extend((
                "definition", "definition_schema_version",
                "definition_size_bytes", "definition_sha256",
            ))
        if recipe.status == "published" and not _has_delivery_closure(stages):
            recipe.status = "draft"
            recipe.published_at = None
            recipe.published_by_id = None
            update_fields.extend(("status", "published_at", "published_by"))
        if update_fields:
            recipe.save(update_fields=tuple(dict.fromkeys(update_fields)))


class Migration(migrations.Migration):

    dependencies = [
        ("studio", "0043_assembly_source_items"),
    ]

    operations = [
        migrations.AlterField(
            model_name="workflowtemplateversion",
            name="template",
            field=models.ForeignKey(
                on_delete=django.db.models.deletion.PROTECT,
                related_name="platform_versions",
                to="studio.workflowtemplate",
            ),
        ),
        migrations.RunPython(
            normalize_delivery_recipe_closure,
            migrations.RunPython.noop,
        ),
    ]
