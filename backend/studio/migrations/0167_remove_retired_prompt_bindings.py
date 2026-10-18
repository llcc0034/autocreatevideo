"""Drop the retired policy-selection table, not current exact execution rows.

This intentionally discards old Binding configuration and its lifecycle events.
Evidence carrying a Binding must first be included in the exact R7 cleanup;
removing a column cannot turn historical evidence into current evidence.
"""
from django.db import migrations


def require_old_evidence_cleanup(apps, schema_editor):
    alias = schema_editor.connection.alias
    for name in ("ModelDeploymentSmokeAttempt", "ModelDeploymentCapabilitySnapshot"):
        if apps.get_model("studio", name).objects.using(alias).filter(prompt_profile_binding__isnull=False).exists():
            raise RuntimeError(f"{name}仍引用旧提示词绑定；请先按R7精确清理旧证据及其依赖，不能仅清空绑定字段。")
    if apps.get_model("studio", "GenerationAttempt").objects.using(alias).exclude(prompt_profile_binding_key="").exists():
        raise RuntimeError("旧生成尝试仍冻结提示词绑定key；请先按R7清理，不能转写为当前执行证据。")
    apps.get_model("studio", "PlatformLifecycleAuditEvent").objects.using(alias).filter(
        object_type="prompt_profile_binding").delete()


class Migration(migrations.Migration):
    dependencies = [("studio", "0166_execution_recommendation_configuration")]

    operations = [
        migrations.RunPython(require_old_evidence_cleanup, migrations.RunPython.noop),
        migrations.RemoveField(model_name="modeldeploymentsmokeattempt", name="prompt_profile_binding"),
        migrations.RemoveField(model_name="modeldeploymentcapabilitysnapshot", name="prompt_profile_binding"),
        migrations.RemoveField(model_name="generationattempt", name="prompt_profile_binding_key"),
        migrations.DeleteModel(name="PromptProfileBinding"),
    ]
