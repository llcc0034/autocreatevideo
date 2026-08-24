from django.db import migrations


def demote_generic_versions_without_resolution_bindings(apps, schema_editor):
    WorkflowTemplateVersion = apps.get_model("studio", "WorkflowTemplateVersion")
    candidates = WorkflowTemplateVersion.objects.filter(
        compiler_id="generic.comfy",
    ).filter(
        status__in=("validated", "published"),
    ) | WorkflowTemplateVersion.objects.filter(
        compiler_id="generic.comfy",
        is_current=True,
    )
    for version in candidates.distinct().iterator():
        keys = set(version.semantic_bindings.values_list("semantic_key", flat=True))
        if {"image.width", "image.height"} <= keys:
            continue
        if version.status == "published":
            # A published version is historical production evidence. Retire it
            # from future selection without rewriting who published it or what
            # validate/smoke evidence existed at publication time.
            version.status = "retired"
            version.is_current = False
            version.save(update_fields=("status", "is_current", "updated_at"))
            continue
        if version.status != "validated":
            version.is_current = False
            version.save(update_fields=("is_current", "updated_at"))
            continue
        version.status = "draft"
        version.validation_state = "not_run"
        version.validation_message = (
            "needs_review: generic.comfy 缺少 image.width/image.height 成对语义绑定；"
            "必须导入新版本并重新 validate/smoke-test。"
        )
        version.smoke_test_state = "not_run"
        version.smoke_test_message = (
            "needs_review: 旧 smoke-test 未验证配置中心分辨率注入。"
        )
        version.validated_deployment_id = None
        version.validation_evidence = {}
        version.validation_evidence_sha256 = ""
        version.smoke_test_evidence = {}
        version.smoke_test_evidence_sha256 = ""
        version.is_current = False
        version.save(update_fields=(
            "status", "validation_state", "validation_message",
            "smoke_test_state", "smoke_test_message", "validated_deployment_id",
            "validation_evidence", "validation_evidence_sha256",
            "smoke_test_evidence", "smoke_test_evidence_sha256", "is_current",
            "updated_at",
        ))


class Migration(migrations.Migration):
    dependencies = [
        ("studio", "0046_configure_production_capabilities"),
    ]

    operations = [
        migrations.RunPython(
            demote_generic_versions_without_resolution_bindings,
            migrations.RunPython.noop,
        ),
    ]
