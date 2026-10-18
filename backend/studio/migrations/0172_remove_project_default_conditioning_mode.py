from django.db import migrations


def remove_project_default(apps, schema_editor):
    project_model = apps.get_model("studio", "SeriesProject")
    projects = project_model.objects.using(schema_editor.connection.alias).filter(
        production_settings__has_key="default_conditioning_mode",
    ).only("id", "production_settings")
    for project in projects.iterator(chunk_size=200):
        settings = dict(project.production_settings)
        settings.pop("default_conditioning_mode")
        project_model.objects.using(schema_editor.connection.alias).filter(pk=project.pk).update(
            production_settings=settings,
        )


class Migration(migrations.Migration):
    dependencies = [("studio", "0171_production_run_auto_advance_index")]

    # Retired preference only; do not rewrite Shot or frozen run/task evidence.
    # Removed defaults are not reconstructed on rollback.
    operations = [migrations.RunPython(remove_project_default)]
