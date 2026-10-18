"""No fabricated source or execution plans for historical ProductionRuns."""
import uuid

from django.conf import settings
from django.core.validators import MinValueValidator, MaxValueValidator, RegexValidator
from django.db import migrations, models
import django.db.models.deletion


def require_empty_production_runs(apps, schema_editor):
    if apps.get_model("studio", "ProductionRun").objects.using(schema_editor.connection.alias).exists():
        raise RuntimeError("RunPlan cutover requires reviewed cleanup of incompatible ProductionRun roots before schema changes")


def identity_fields():
    return [("id", models.UUIDField(default=uuid.uuid4, editable=False, primary_key=True, serialize=False)),
        ("created_at", models.DateTimeField(auto_now_add=True)), ("updated_at", models.DateTimeField(auto_now=True))]


def digest_field():
    return models.CharField(max_length=64, validators=[RegexValidator(r"^[a-f0-9]{64}$")])


class Migration(migrations.Migration):
    dependencies = [("studio", "0149_execution_capacity_observation"), migrations.swappable_dependency(settings.AUTH_USER_MODEL)]
    operations = [
        migrations.RunPython(require_empty_production_runs, migrations.RunPython.noop),
        migrations.AlterField(model_name="productionrun", name="status", field=models.CharField(
            choices=[("queued", "排队中"), ("awaiting_input", "等待人工操作"), ("running", "运行中"),
                ("awaiting_approval", "待审批"), ("blocked", "已阻塞"), ("succeeded", "成功"),
                ("failed", "失败"), ("cancelled", "已取消")], default="queued", max_length=24)),
        migrations.AlterField(model_name="productionrun", name="recipe_version", field=models.ForeignKey(blank=True, null=True,
            on_delete=django.db.models.deletion.PROTECT, related_name="production_runs", to="studio.productionrecipeversion")),
        migrations.AddField(model_name="productionrun", name="plan_source",
            field=models.CharField(choices=[("recipe", "配方生产"), ("manual", "人工逐步制作")], max_length=6)),
        migrations.AddConstraint(model_name="productionrun", constraint=models.CheckConstraint(condition=(
            models.Q(plan_source="recipe", recipe_version__isnull=False) | models.Q(plan_source="manual", recipe_version__isnull=True)),
            name="ck_run_plan_source")),
        migrations.CreateModel(name="ProductionRunPlan", fields=[*identity_fields(),
            ("source", models.CharField(choices=[("recipe", "配方生产"), ("manual", "人工逐步制作")], max_length=6)),
            ("stage_catalog_key", models.CharField(max_length=100)),
            ("stage_catalog_version", models.PositiveIntegerField(validators=[MinValueValidator(1)])),
            ("stage_catalog_sha256", digest_field()),
            ("definition", models.JSONField()),
            ("definition_schema_version", models.PositiveIntegerField(validators=[MinValueValidator(1)])),
            ("definition_size_bytes", models.PositiveIntegerField(validators=[MaxValueValidator(65536)])),
            ("definition_sha256", digest_field()),
            ("created_by", models.ForeignKey(on_delete=django.db.models.deletion.PROTECT, related_name="production_run_plans", to=settings.AUTH_USER_MODEL)),
            ("run", models.OneToOneField(on_delete=django.db.models.deletion.CASCADE, related_name="execution_plan", to="studio.productionrun")),
        ], options={"constraints": [models.CheckConstraint(condition=models.Q(definition_schema_version=1, definition_size_bytes__lte=65536), name="ck_run_plan_definition_size")]}),
        migrations.CreateModel(name="ProductionManualCommand", fields=[*identity_fields(),
            ("action", models.CharField(choices=[("generate", "生成候选"), ("adopt", "采用已有内容"), ("approve", "批准"), ("assemble", "装配"), ("deliver", "交付")], max_length=8)),
            ("idempotency_key", models.CharField(max_length=191)),
            ("request_sha256", digest_field()),
            ("result_kind", models.CharField(max_length=40)),
            ("target_id", models.UUIDField()),
            ("result_refs", models.JSONField()),
            ("result_refs_schema_version", models.PositiveIntegerField(validators=[MinValueValidator(1)])),
            ("result_refs_size_bytes", models.PositiveIntegerField(validators=[MaxValueValidator(65536)])),
            ("result_refs_sha256", digest_field()),
            ("created_by", models.ForeignKey(on_delete=django.db.models.deletion.PROTECT, related_name="production_manual_commands", to=settings.AUTH_USER_MODEL)),
            ("run", models.ForeignKey(on_delete=django.db.models.deletion.CASCADE, related_name="manual_commands", to="studio.productionrun")),
            ("stage_run", models.ForeignKey(blank=True, null=True, on_delete=django.db.models.deletion.PROTECT, related_name="manual_commands", to="studio.stagerun")),
        ], options={"ordering": ("-created_at", "-id"), "constraints": [
            models.UniqueConstraint(fields=("run", "idempotency_key"), name="uk_manual_command_key"),
            models.CheckConstraint(condition=models.Q(result_refs_schema_version=1, result_refs_size_bytes__lte=65536), name="ck_manual_command_refs_size"),
        ]}),
    ]
