from django.db import migrations
from django.db.models import Q


def retire_uncontracted_workflows(apps, schema_editor):
    """Remove invalid rows from the current/published capability catalog.

    A missing workflow_mode cannot be backfilled without changing the frozen
    input/execution contract.  Preserving PASSED evidence after such a rewrite
    would falsely claim that the new contract was smoke-tested, so historical
    rows are retired intact and must be imported/validated/smoked as a new
    version before they can become current again.
    """

    WorkflowTemplateVersion = apps.get_model("studio", "WorkflowTemplateVersion")
    candidates = WorkflowTemplateVersion.objects.filter(
        Q(status="published") | Q(is_current=True),
    ).only("id", "input_contract", "status", "is_current")
    for version in candidates.iterator():
        contract = version.input_contract
        workflow_mode = contract.get("workflow_mode") if isinstance(contract, dict) else None
        if (
            isinstance(workflow_mode, str)
            and workflow_mode.strip()
            and workflow_mode == workflow_mode.strip()
        ):
            continue
        WorkflowTemplateVersion.objects.filter(pk=version.pk).update(
            status="retired",
            is_current=False,
        )


class Migration(migrations.Migration):
    dependencies = [
        ("studio", "0094_queue_capacity_advisory_contract"),
    ]

    operations = [
        migrations.RunPython(
            retire_uncontracted_workflows,
            migrations.RunPython.noop,
        ),
    ]
