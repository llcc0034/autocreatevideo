import uuid

from django.db import migrations, models
import django.db.models.deletion


class Migration(migrations.Migration):
    dependencies = [("studio", "0214_retire_script_resource_advisory")]

    operations = [
        migrations.CreateModel(
            name="BibleEntityIdentity",
            fields=[
                ("id", models.UUIDField(default=uuid.uuid4, editable=False, primary_key=True, serialize=False)),
                ("created_at", models.DateTimeField(auto_now_add=True)),
                ("updated_at", models.DateTimeField(auto_now=True)),
                ("entity_key", models.CharField(max_length=64)),
                ("character", models.OneToOneField(blank=True, null=True, on_delete=django.db.models.deletion.CASCADE,
                    related_name="declaration_identity", to="studio.characterbible")),
                ("scene", models.OneToOneField(blank=True, null=True, on_delete=django.db.models.deletion.CASCADE,
                    related_name="declaration_identity", to="studio.scenebible")),
                ("prop", models.OneToOneField(blank=True, null=True, on_delete=django.db.models.deletion.CASCADE,
                    related_name="declaration_identity", to="studio.propbible")),
                ("project", models.ForeignKey(on_delete=django.db.models.deletion.CASCADE,
                    related_name="bible_entity_identities", to="studio.seriesproject")),
            ],
            options={"constraints": [
                models.UniqueConstraint(fields=("project", "entity_key"), name="uk_bible_project_entity_key"),
                models.CheckConstraint(condition=~models.Q(entity_key=""), name="ck_bible_entity_key_nonempty"),
                models.CheckConstraint(condition=(
                    models.Q(character__isnull=False, scene__isnull=True, prop__isnull=True)
                    | models.Q(character__isnull=True, scene__isnull=False, prop__isnull=True)
                    | models.Q(character__isnull=True, scene__isnull=True, prop__isnull=False)
                ), name="ck_bible_identity_one_entity"),
            ]},
        ),
    ]
