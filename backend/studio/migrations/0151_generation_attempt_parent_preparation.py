"""Explicit production field lineage; never infer parents for old attempts."""
from django.db import migrations, models
import django.db.models.deletion


def require_no_legacy_auxiliaries(apps, schema_editor):
    if apps.get_model("studio", "GenerationAttempt").objects.using(schema_editor.connection.alias).filter(role="auxiliary").exists():
        raise RuntimeError("Clean incompatible auxiliary Attempts during the reviewed cutover; no inferred parent backfill")


class Migration(migrations.Migration):
    dependencies = [("studio", "0150_production_run_plan")]
    operations = [
        migrations.RunPython(require_no_legacy_auxiliaries, migrations.RunPython.noop),
        migrations.AddField(model_name="generationattempt", name="parent_preparation", field=models.ForeignKey(
            to="studio.executionpreparation", on_delete=django.db.models.deletion.PROTECT, related_name="auxiliary_generation_attempts",
            null=True, blank=True, db_index=False)),
        migrations.RemoveConstraint(model_name="generationattempt", name="ck_attempt_execution_role"),
        migrations.AddConstraint(model_name="generationattempt", constraint=models.CheckConstraint(condition=(
            models.Q(role="primary", auxiliary_purpose="", field_batch_key="", parent_preparation__isnull=True)
            | models.Q(role="auxiliary", parent_preparation__isnull=False, auxiliary_purpose__in=("generate_fields", "translate_fields")) & ~models.Q(field_batch_key="")
        ), name="ck_attempt_execution_role")),
        migrations.AddConstraint(model_name="generationattempt", constraint=models.CheckConstraint(
            condition=~models.Q(parent_preparation=models.F("preparation")), name="ck_attempt_distinct_parent")),
        migrations.AddConstraint(model_name="generationattempt", constraint=models.UniqueConstraint(
            fields=("parent_preparation", "field_batch_key", "attempt_number"), name="uk_attempt_parent_batch")),
    ]
