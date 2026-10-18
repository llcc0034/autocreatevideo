import uuid

from django.db import migrations, models
import django.db.models.deletion


class Migration(migrations.Migration):
    dependencies = [("studio", "0215_bible_entity_identity")]

    operations = [
        migrations.RemoveConstraint(model_name="biblevariant", name="uk_bible_appearance_source"),
        *[migrations.AlterField(model_name="scriptbibleentitybinding", name=kind,
            field=models.ForeignKey(blank=True, null=True, on_delete=django.db.models.deletion.PROTECT,
                related_name="script_bindings", to=f"studio.{kind}bible")) for kind in ("character", "scene", "prop")],
        *[migrations.AddConstraint(model_name="scriptbibleentitybinding",
            constraint=models.UniqueConstraint(fields=("script_revision", kind), name=f"uk_script_bible_{kind}"))
            for kind in ("character", "scene", "prop")],
        *[migrations.AddConstraint(model_name="biblevariant",
            constraint=models.UniqueConstraint(fields=(kind, "source_variant_key"),
                condition=models.Q(source_binding__isnull=False), name=f"uk_{kind}_declared_look"))
            for kind in ("character", "scene", "prop")],
        migrations.CreateModel(name="ScriptBibleAppearanceBinding", fields=[
            ("id", models.UUIDField(default=uuid.uuid4, editable=False, primary_key=True, serialize=False)),
            ("created_at", models.DateTimeField(auto_now_add=True)),
            ("updated_at", models.DateTimeField(auto_now=True)),
            ("variant_key", models.CharField(blank=True, max_length=64)),
            ("declaration_sha256", models.CharField(max_length=64)),
            ("setting_sha256", models.CharField(max_length=64)),
            ("source_binding", models.ForeignKey(on_delete=django.db.models.deletion.PROTECT,
                related_name="setting_bindings", to="studio.scriptbibleentitybinding")),
            ("setting", models.ForeignKey(on_delete=django.db.models.deletion.PROTECT,
                related_name="script_uses", to="studio.biblevariantrevision")),
        ], options={"constraints": [models.UniqueConstraint(fields=("source_binding", "variant_key"),
            name="uk_script_appearance_setting")]}),
    ]
