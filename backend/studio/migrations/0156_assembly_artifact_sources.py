from django.db import migrations, models
import django.db.models.deletion


def require_reviewed_assembly_cleanup(apps, schema_editor):
    alias = schema_editor.connection.alias
    jobs = apps.get_model("studio", "AssemblyJob").objects.using(alias)
    sources = apps.get_model("studio", "AssemblySourceItem").objects.using(alias)
    if (jobs.exclude(source_contract_version=5).exists()
            or sources.filter(generation_task__isnull=False).exists()
            or sources.filter(generated_artifact__isnull=True).exists()):
        raise RuntimeError("P5 must clear reviewed incompatible assemblies and their closure before removing task origins; no inferred conversion")


class Migration(migrations.Migration):
    dependencies = [("studio", "0155_preparation_backed_generation_tasks")]

    operations = [
        migrations.RunPython(require_reviewed_assembly_cleanup, migrations.RunPython.noop),
        migrations.RemoveConstraint(model_name="assemblysourceitem", name="assembly_source_exactly_one_origin"),
        migrations.RemoveField(model_name="assemblysourceitem", name="generation_task"),
        migrations.RemoveField(model_name="assemblyjob", name="source_tasks"),
        migrations.AlterField(model_name="assemblysourceitem", name="generated_artifact",
            field=models.ForeignKey(on_delete=django.db.models.deletion.PROTECT,
                related_name="assembly_source_items", to="studio.generatedartifact")),
        migrations.AlterField(model_name="assemblyjob", name="source_contract_version",
            field=models.PositiveSmallIntegerField(default=5)),
    ]
