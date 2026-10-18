"""One original Comfy task belongs to one primary call; no inferred backfill."""
from django.db import migrations, models
import django.db.models.deletion


def require_unlinked_old_tasks(apps, schema_editor):
    attempts = apps.get_model("studio", "GenerationAttempt").objects.using(schema_editor.connection.alias)
    if attempts.filter(generation_task__isnull=False).exists():
        raise RuntimeError("Clean incompatible Comfy task/attempt records during the reviewed P5 cutover; no inferred relinking")


class Migration(migrations.Migration):
    dependencies = [("studio", "0151_generation_attempt_parent_preparation")]
    operations = [
        migrations.RunPython(require_unlinked_old_tasks, migrations.RunPython.noop),
        migrations.AlterField(model_name="generationattempt", name="generation_task", field=models.ForeignKey(
            to="studio.generationtask", on_delete=django.db.models.deletion.PROTECT, related_name="platform_attempts",
            null=True, blank=True, db_index=False)),
        migrations.AddConstraint(model_name="generationattempt", constraint=models.UniqueConstraint(
            fields=("generation_task",), name="uk_attempt_generation_task")),
        migrations.AddConstraint(model_name="generationattempt", constraint=models.CheckConstraint(
            condition=models.Q(generation_task__isnull=True) | models.Q(role="primary"), name="ck_attempt_task_primary")),
    ]
