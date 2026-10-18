import uuid

import django.core.validators
import django.db.models.deletion
from django.db import migrations, models


class Migration(migrations.Migration):
    dependencies = [("studio", "0148_workflow_smoke_staged_inputs")]
    operations = [migrations.CreateModel(
        name="ExecutionCapacityObservation",
        fields=[
            ("id", models.UUIDField(default=uuid.uuid4, editable=False, primary_key=True, serialize=False)),
            ("created_at", models.DateTimeField(auto_now_add=True)),
            ("updated_at", models.DateTimeField(auto_now=True)),
            ("request_sha256", models.CharField(max_length=64, validators=[django.core.validators.RegexValidator(r"^[a-f0-9]{64}$")])),
            ("signature_sha256", models.CharField(blank=True, max_length=64, null=True, validators=[django.core.validators.RegexValidator(r"^[a-f0-9]{64}$")])),
            ("observed_at", models.DateTimeField()),
            ("elapsed_ms", models.PositiveBigIntegerField()),
            ("details", models.JSONField()),
            ("details_schema_version", models.PositiveIntegerField(validators=[django.core.validators.MinValueValidator(1)])),
            ("details_size_bytes", models.PositiveIntegerField(validators=[django.core.validators.MaxValueValidator(65536)])),
            ("details_sha256", models.CharField(max_length=64, validators=[django.core.validators.RegexValidator(r"^[a-f0-9]{64}$")])),
            ("preparation", models.OneToOneField(on_delete=django.db.models.deletion.CASCADE, related_name="capacity_observation", to="studio.executionpreparation")),
            ("execution_instance_version", models.ForeignKey(db_index=False, on_delete=django.db.models.deletion.PROTECT, related_name="capacity_observations", to="studio.modelexecutioninstanceversion")),
        ],
        options={"ordering": ("-observed_at", "-id"),
            "indexes": [models.Index(fields=["execution_instance_version", "signature_sha256", "-observed_at", "-id"], name="idx_exec_capacity_sample")],
            "constraints": [models.CheckConstraint(condition=models.Q(("details_size_bytes__lte", 65536)), name="ck_exec_capacity_detail_size")]},
    )]
