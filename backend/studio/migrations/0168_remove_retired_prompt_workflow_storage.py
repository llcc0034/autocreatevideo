from django.db import migrations


def require_retired_storage_cleanup(apps, schema_editor):
    alias = schema_editor.connection.alias
    for name in ("GeneratedWorkflow", "PromptArtifact"):
        if apps.get_model("studio", name).objects.using(alias).exists():
            raise RuntimeError(
                f"{name} 仍含旧生产数据；须先完成R7精确依赖清理，"
                "不将旧产物转换为Preparation或新执行证据。"
            )
    apps.get_model("studio", "PlatformLifecycleAuditEvent").objects.using(alias).filter(
        object_type__in=("prompt_artifact", "generated_workflow"),
    ).delete()


class Migration(migrations.Migration):
    dependencies = [("studio", "0167_remove_retired_prompt_bindings")]
    operations = [
        migrations.RunPython(require_retired_storage_cleanup, migrations.RunPython.noop),
        migrations.DeleteModel(name="GeneratedWorkflow"),
        migrations.DeleteModel(name="PromptArtifact"),
    ]
