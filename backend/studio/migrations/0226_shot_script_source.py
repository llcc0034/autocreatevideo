import django.core.validators
import django.db.models.deletion
from django.db import migrations, models


class Migration(migrations.Migration):
    dependencies = [("studio", "0225_execution_preparation_supersedes")]

    operations = [migrations.CreateModel(
        name="ShotScriptSource",
        fields=[
            ("shot", models.OneToOneField(primary_key=True, serialize=False,
                on_delete=django.db.models.deletion.CASCADE, related_name="script_source", to="studio.shot")),
            ("structure", models.ForeignKey(on_delete=django.db.models.deletion.PROTECT,
                related_name="shot_sources", to="studio.scriptrevisionstructure")),
            ("stage", models.ForeignKey(db_index=False, on_delete=django.db.models.deletion.PROTECT,
                related_name="shot_sources", to="studio.stagerun")),
            ("episode_number", models.PositiveIntegerField(validators=[django.core.validators.MinValueValidator(1)])),
            ("shot_number", models.PositiveIntegerField(validators=[django.core.validators.MinValueValidator(1)])),
            ("document_sha256", models.CharField(max_length=64,
                validators=[django.core.validators.RegexValidator(r"^[0-9a-f]{64}$")])),
            ("materialization_sha256", models.CharField(max_length=64,
                validators=[django.core.validators.RegexValidator(r"^[0-9a-f]{64}$")])),
            ("created_at", models.DateTimeField(auto_now_add=True)),
        ],
        options={"constraints": [
            models.UniqueConstraint(fields=("stage", "episode_number", "shot_number"), name="uk_shot_script_coordinate"),
            models.CheckConstraint(condition=models.Q(episode_number__gt=0, shot_number__gt=0), name="ck_shot_script_coordinate"),
        ]},
    )]
