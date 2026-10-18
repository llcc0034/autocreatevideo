"""Scoped temporary storage exception; no preparation or audit facts rewritten."""
import django.core.validators
from django.db import migrations, models


def require_small_records_before_rollback(apps, schema_editor):
    contracts = apps.get_model("studio", "ExecutionPreparationContract")
    if contracts.objects.using(schema_editor.connection.alias).filter(resolved_inputs_size_bytes__gt=65536).exists():
        raise RuntimeError(
            "Cannot restore the 64 KiB preparation limit while larger facts exist. "
            "Migrate and verify those records through OPT-PREP-STORAGE-001 first; do not truncate or delete them."
        )


class Migration(migrations.Migration):
    dependencies = [("studio", "0195_production_stage_input_amendment")]

    operations = [
        migrations.RemoveConstraint(model_name="executionpreparationcontract", name="ck_exec_prep_contract_size"),
        migrations.AlterField(
            model_name="executionpreparationcontract", name="resolved_inputs_size_bytes",
            field=models.PositiveIntegerField(validators=[django.core.validators.MaxValueValidator(262144)]),
        ),
        migrations.AddConstraint(
            model_name="executionpreparationcontract",
            constraint=models.CheckConstraint(condition=models.Q(
                intent_schema_version__gte=1, resolved_inputs_schema_version__gte=1, review_contract_schema_version__gte=1,
                intent_size_bytes__lte=65536, resolved_inputs_size_bytes__lte=262144, review_contract_size_bytes__lte=65536,
            ), name="ck_exec_prep_contract_size"),
        ),
        migrations.RunPython(migrations.RunPython.noop, require_small_records_before_rollback),
    ]
