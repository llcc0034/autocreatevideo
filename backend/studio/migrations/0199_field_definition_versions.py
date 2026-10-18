import uuid

import django.core.validators
import django.db.models.deletion
from django.db import migrations, models


def seed_definition_drafts(apps, schema_editor):
    Definition = apps.get_model("studio", "FieldDefinition")
    Version = apps.get_model("studio", "FieldDefinitionVersion")
    database = schema_editor.connection.alias
    for definition in Definition.objects.using(database).iterator():
        Version.objects.using(database).create(
            field_definition_id=definition.pk,
            version=1,
            status="draft",
            label=definition.label,
            category=definition.category,
            description=definition.description,
            impact=definition.impact,
            value_type={"number": "decimal", "string_list": "list"}.get(
                definition.value_type, definition.value_type,
            ),
            unit="",
            value_options=definition.value_options,
        )


class Migration(migrations.Migration):
    dependencies = [("studio", "0198_script_bible_source_bindings")]

    operations = [
        migrations.RenameModel(old_name="PromptFieldDefinition", new_name="FieldDefinition"),
        migrations.RenameIndex(
            model_name="fielddefinition",
            old_name="studio_prom_categor_ce790e_idx",
            new_name="idx_field_category_active",
        ),
        migrations.CreateModel(
            name="FieldDefinitionVersion",
            fields=[
                ("id", models.UUIDField(default=uuid.uuid4, editable=False, primary_key=True, serialize=False)),
                ("created_at", models.DateTimeField(auto_now_add=True)),
                ("updated_at", models.DateTimeField(auto_now=True)),
                ("version", models.PositiveIntegerField(validators=[django.core.validators.MinValueValidator(1)])),
                ("status", models.CharField(choices=[("draft", "草稿"), ("published", "可用"), ("retired", "停用")], default="draft", max_length=12)),
                ("label", models.CharField(blank=True, max_length=160)),
                ("category", models.CharField(blank=True, max_length=80, validators=[django.core.validators.RegexValidator("^[a-z][a-z0-9_.-]{1,79}$")])),
                ("description", models.TextField(blank=True)),
                ("impact", models.TextField(blank=True)),
                ("value_type", models.CharField(blank=True, choices=[("text", "短文本"), ("long_text", "长文本"), ("integer", "整数"), ("decimal", "小数"), ("boolean", "布尔值"), ("enum", "枚举"), ("object", "对象"), ("list", "列表"), ("media", "媒体引用")], max_length=24)),
                ("unit", models.CharField(blank=True, max_length=80)),
                ("value_options", models.JSONField(blank=True, default=list)),
                ("field_definition", models.ForeignKey(on_delete=django.db.models.deletion.CASCADE, related_name="definition_versions", to="studio.fielddefinition")),
            ],
            options={
                "ordering": ("field_definition_id", "-version"),
                "constraints": [
                    models.UniqueConstraint(fields=("field_definition", "version"), name="uk_field_definition_version"),
                    models.CheckConstraint(condition=models.Q(version__gte=1), name="ck_field_definition_version_pos"),
                ],
            },
        ),
        migrations.RunPython(seed_definition_drafts, migrations.RunPython.noop),
    ]
