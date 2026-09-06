from django.core.validators import RegexValidator
from django.db import migrations, models


def rename_project_default_conditioning_mode(apps, schema_editor):
    SeriesProject = apps.get_model("studio", "SeriesProject")
    for project in SeriesProject.objects.only("id", "production_settings").iterator():
        settings = (
            dict(project.production_settings)
            if isinstance(project.production_settings, dict)
            else {}
        )
        if "default_h3_mode" not in settings:
            continue
        legacy = settings.pop("default_h3_mode")
        configured = settings.get("default_conditioning_mode")
        if configured not in {None, "", legacy}:
            raise RuntimeError(
                f"SeriesProject {project.pk} has conflicting conditioning defaults",
            )
        settings["default_conditioning_mode"] = legacy
        project.production_settings = settings
        project.save(update_fields=("production_settings", "updated_at"))


def restore_project_default_h3_mode(apps, schema_editor):
    SeriesProject = apps.get_model("studio", "SeriesProject")
    for project in SeriesProject.objects.only("id", "production_settings").iterator():
        settings = (
            dict(project.production_settings)
            if isinstance(project.production_settings, dict)
            else {}
        )
        if "default_conditioning_mode" not in settings:
            continue
        configured = settings.pop("default_conditioning_mode")
        legacy = settings.get("default_h3_mode")
        if legacy not in {None, "", configured}:
            raise RuntimeError(
                f"SeriesProject {project.pk} has conflicting H3 defaults",
            )
        settings["default_h3_mode"] = configured
        project.production_settings = settings
        project.save(update_fields=("production_settings", "updated_at"))


class Migration(migrations.Migration):
    dependencies = [("studio", "0072_upgrade_h3_ref2va_prompt_profile_v2")]

    operations = [
        migrations.RenameField(
            model_name="shot",
            old_name="h3_mode",
            new_name="conditioning_mode",
        ),
        migrations.AlterField(
            model_name="shot",
            name="conditioning_mode",
            field=models.CharField(
                blank=True,
                default="",
                max_length=64,
                validators=[RegexValidator(r"^[A-Za-z][A-Za-z0-9_.-]{0,63}$")],
            ),
        ),
        migrations.RenameField(
            model_name="promptartifact",
            old_name="h3_mode",
            new_name="conditioning_mode",
        ),
        migrations.AlterField(
            model_name="promptartifact",
            name="conditioning_mode",
            field=models.CharField(
                blank=True,
                default="",
                max_length=64,
                validators=[RegexValidator(r"^[A-Za-z][A-Za-z0-9_.-]{0,63}$")],
            ),
        ),
        migrations.RunPython(
            rename_project_default_conditioning_mode,
            restore_project_default_h3_mode,
        ),
    ]
