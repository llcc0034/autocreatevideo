import uuid

import django.core.validators
import django.db.models.deletion
from django.conf import settings
from django.db import migrations, models


class Migration(migrations.Migration):
    dependencies = [
        ("studio", "0219_stagerun_selected_variant_route_index"),
        migrations.swappable_dependency(settings.AUTH_USER_MODEL),
    ]

    operations = [migrations.CreateModel(
        name="ProductionMaterialFulfillment",
        fields=[
            ("id", models.UUIDField(default=uuid.uuid4, editable=False, primary_key=True, serialize=False)),
            ("created_at", models.DateTimeField(auto_now_add=True)),
            ("updated_at", models.DateTimeField(auto_now=True)),
            ("unit_key", models.CharField(max_length=191)),
            ("requirement_key", models.CharField(max_length=191)),
            ("run_plan_sha256", models.CharField(max_length=64,
                validators=[django.core.validators.RegexValidator(r"^[a-f0-9]{64}$")])),
            ("target_sha256", models.CharField(max_length=64,
                validators=[django.core.validators.RegexValidator(r"^[a-f0-9]{64}$")])),
            ("reference_version", models.ForeignKey(on_delete=django.db.models.deletion.PROTECT,
                related_name="production_fulfillments", to="studio.assetreferenceversion")),
            ("selected_by", models.ForeignKey(on_delete=django.db.models.deletion.PROTECT,
                related_name="production_material_fulfillments", to=settings.AUTH_USER_MODEL)),
            ("stage_run", models.ForeignKey(db_index=False, on_delete=django.db.models.deletion.CASCADE,
                related_name="material_fulfillments", to="studio.stagerun")),
        ],
        options={"constraints": [models.UniqueConstraint(fields=("stage_run", "unit_key"),
            name="uk_material_fulfillment_unit")]},
    )]
