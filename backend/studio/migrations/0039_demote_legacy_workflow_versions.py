from django.db import migrations
from django.db.models import Q


def demote_versions_without_semantic_contract(apps, schema_editor):
    WorkflowTemplateVersion = apps.get_model("studio", "WorkflowTemplateVersion")
    WorkflowTemplateVersion.objects.filter(
        Q(compiler_id="")
        | Q(compiler_version=0)
        | Q(semantic_bindings_sha256="")
        | Q(model_requirements_sha256="")
        | Q(execution_contract_sha256="")
    ).filter(
        Q(status__in=("validated", "published"))
        | Q(is_current=True)
        | Q(validation_state="passed")
        | Q(smoke_test_state="passed")
    ).update(
        status="draft",
        validation_state="not_run",
        validation_message=(
            "needs_review: legacy workflow version 缺少 semantic compiler execution contract；"
            "必须导入新版本并重新 validate/smoke-test。"
        ),
        smoke_test_state="not_run",
        smoke_test_message="needs_review: legacy smoke-test 未绑定 semantic execution contract。",
        validated_deployment_id=None,
        validation_evidence={},
        validation_evidence_sha256="",
        smoke_test_evidence={},
        smoke_test_evidence_sha256="",
        is_current=False,
        published_at=None,
        published_by_id=None,
    )


class Migration(migrations.Migration):

    dependencies = [
        ("studio", "0038_generation_attempt_resolution"),
    ]

    operations = [
        migrations.RunPython(
            demote_versions_without_semantic_contract,
            migrations.RunPython.noop,
        ),
    ]
