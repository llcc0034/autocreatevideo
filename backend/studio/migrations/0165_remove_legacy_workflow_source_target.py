"""Remove the obsolete graph target after the exact R7 cleanup preflight."""
from django.db import migrations, models


def require_legacy_source_cleanup(apps, schema_editor):
    sources = apps.get_model("studio", "ComfyWorkflowSource")
    if sources.objects.using(schema_editor.connection.alias).filter(generated_workflow__isnull=False).exists():
        raise RuntimeError(
            "旧生成图仍被Comfy来源引用；请先按R7清单清理这些来源及其精确依赖，再执行迁移。"
        )


class Migration(migrations.Migration):
    dependencies = [("studio", "0164_remove_deferred_generation_request")]

    operations = [
        migrations.RunPython(require_legacy_source_cleanup, migrations.RunPython.noop),
        migrations.RemoveConstraint(model_name="comfyworkflowsource", name="valid_comfy_workflow_source_target"),
        migrations.RemoveField(model_name="comfyworkflowsource", name="generated_workflow"),
        migrations.AddConstraint(model_name="comfyworkflowsource", constraint=models.CheckConstraint(
            condition=(
                models.Q(import_kind="template_version", workflow_version__isnull=False, generation_task__isnull=True)
                | models.Q(import_kind="execution_snapshot", workflow_version__isnull=True)
                | models.Q(import_kind="candidate", workflow_version__isnull=True, generation_task__isnull=True)
                | models.Q(import_kind="ignored", workflow_version__isnull=True, generation_task__isnull=True)
            ), name="valid_comfy_workflow_source_target")),
    ]
