from django.db import migrations


LEGACY_TERMINAL_FAILURE_PREFIX = "ComfyUI 执行失败:"


def classify_legacy_terminal_failures(apps, schema_editor):
    GenerationTask = apps.get_model("studio", "GenerationTask")
    GenerationTask.objects.filter(
        status="failed",
        error_message__startswith=LEGACY_TERMINAL_FAILURE_PREFIX,
    ).exclude(
        submission_token="",
    ).update(
        submission_state="terminal_failed",
    )


class Migration(migrations.Migration):

    dependencies = [
        ("studio", "0008_add_terminal_failed_submission_state"),
    ]

    operations = [
        migrations.RunPython(
            classify_legacy_terminal_failures,
            migrations.RunPython.noop,
        ),
    ]
