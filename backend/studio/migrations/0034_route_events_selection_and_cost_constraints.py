import uuid

import django.core.validators
import django.db.models.deletion
from django.conf import settings
from django.db import migrations, models


def retire_unexecutable_default_recipe(apps, schema_editor):
    Recipe = apps.get_model("studio", "ProductionRecipeVersion")
    Recipe.objects.filter(
        recipe_key="default-series-production",
        version=1,
        status="published",
    ).update(status="draft", published_at=None, published_by=None)


class Migration(migrations.Migration):

    dependencies = [
        ("studio", "0033_production_run_routing_cost_delivery"),
        migrations.swappable_dependency(settings.AUTH_USER_MODEL),
    ]

    operations = [
        migrations.CreateModel(
            name="ArtifactSelectionDecision",
            fields=[
                ("id", models.UUIDField(default=uuid.uuid4, editable=False, primary_key=True, serialize=False)),
                ("created_at", models.DateTimeField(auto_now_add=True)),
                ("updated_at", models.DateTimeField(auto_now=True)),
                ("variant_group", models.CharField(max_length=120)),
                ("revision", models.PositiveIntegerField(validators=[django.core.validators.MinValueValidator(1)])),
                ("artifact", models.ForeignKey(on_delete=django.db.models.deletion.PROTECT, related_name="selection_decisions", to="studio.generatedartifact")),
                ("selected_by", models.ForeignKey(on_delete=django.db.models.deletion.PROTECT, related_name="artifact_selection_decisions", to=settings.AUTH_USER_MODEL)),
                ("stage_run", models.ForeignKey(on_delete=django.db.models.deletion.CASCADE, related_name="artifact_selection_decisions", to="studio.stagerun")),
            ],
            options={
                "ordering": ("stage_run", "variant_group", "revision"),
                "indexes": [models.Index(fields=["stage_run", "variant_group", "revision"], name="idx_artifact_selection_group")],
                "constraints": [models.UniqueConstraint(fields=("stage_run", "variant_group", "revision"), name="unique_artifact_selection_revision")],
            },
        ),
        migrations.CreateModel(
            name="StageRouteEvent",
            fields=[
                ("id", models.UUIDField(default=uuid.uuid4, editable=False, primary_key=True, serialize=False)),
                ("created_at", models.DateTimeField(auto_now_add=True)),
                ("updated_at", models.DateTimeField(auto_now=True)),
                ("sequence", models.PositiveIntegerField(validators=[django.core.validators.MinValueValidator(1)])),
                ("unit_key", models.CharField(max_length=191)),
                ("route_index", models.PositiveSmallIntegerField(blank=True, null=True)),
                ("event_type", models.CharField(max_length=64)),
                ("code", models.CharField(blank=True, max_length=100)),
                ("stage_run", models.ForeignKey(on_delete=django.db.models.deletion.CASCADE, related_name="route_events", to="studio.stagerun")),
            ],
            options={
                "ordering": ("stage_run", "sequence"),
                "indexes": [models.Index(fields=["stage_run", "unit_key", "sequence"], name="idx_stage_route_event_unit")],
                "constraints": [models.UniqueConstraint(fields=("stage_run", "sequence"), name="unique_stage_route_event_sequence")],
            },
        ),
        migrations.AddConstraint(
            model_name="productionrun",
            constraint=models.CheckConstraint(
                condition=models.Q(("budget_limit__isnull", True), ("budget_limit__gte", 0), _connector="OR"),
                name="production_run_budget_nonnegative",
            ),
        ),
        migrations.AddConstraint(
            model_name="productionrun",
            constraint=models.CheckConstraint(
                condition=models.Q(("estimated_cost_total__gte", 0), ("committed_cost_total__gte", 0), ("actual_cost_total__gte", 0)),
                name="production_run_costs_nonnegative",
            ),
        ),
        migrations.AddConstraint(
            model_name="stagerun",
            constraint=models.CheckConstraint(
                condition=models.Q(("approved_estimated_cost__isnull", True), ("approved_estimated_cost__gte", 0), _connector="OR"),
                name="stage_approved_cost_nonnegative",
            ),
        ),
        migrations.AddConstraint(
            model_name="generationattempt",
            constraint=models.CheckConstraint(
                condition=(
                    models.Q(("estimated_cost__isnull", True), ("estimated_cost__gte", 0), _connector="OR")
                    & models.Q(("committed_cost__isnull", True), ("committed_cost__gte", 0), _connector="OR")
                    & models.Q(("actual_cost__isnull", True), ("actual_cost__gte", 0), _connector="OR")
                ),
                name="generation_attempt_costs_nonnegative",
            ),
        ),
        migrations.RunPython(retire_unexecutable_default_recipe, migrations.RunPython.noop),
    ]
