from django.db import migrations, models
import django.db.models.deletion


def require_reviewed_release_cleanup(apps, schema_editor):
    bundles = apps.get_model("studio", "ReleaseBundle").objects.using(schema_editor.connection.alias)
    if bundles.filter(production_run__isnull=True).exists():
        raise RuntimeError("P5 must clear reviewed standalone release closures; no invented ProductionRun")
    for manifest in bundles.exclude(status="draft").values_list("manifest", flat=True).iterator():
        if not isinstance(manifest, dict) or manifest.get("schema") != "autoprompt-production-release-bundle-v3":
            raise RuntimeError("P5 must clear reviewed incompatible release manifests; no signature rewriting")


class Migration(migrations.Migration):
    dependencies = [("studio", "0156_assembly_artifact_sources")]
    operations = [
        migrations.RunPython(require_reviewed_release_cleanup, migrations.RunPython.noop),
        migrations.AlterField(model_name="releasebundle", name="production_run",
            field=models.OneToOneField(on_delete=django.db.models.deletion.PROTECT,
                related_name="release_bundle", to="studio.productionrun")),
    ]
