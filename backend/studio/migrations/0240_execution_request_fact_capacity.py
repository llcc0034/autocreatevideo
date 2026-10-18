from django.core.validators import MinValueValidator
from django.db import migrations, models


class Migration(migrations.Migration):
    dependencies = [("studio", "0239_auxiliary_uncertain_retry_authorization")]

    operations = [
        migrations.RemoveConstraint(
            model_name="executionpreparationcontract", name="ck_exec_prep_contract_size"),
        migrations.AlterField(
            model_name="executionpreparationcontract", name="intent_size_bytes",
            field=models.PositiveIntegerField(validators=[MinValueValidator(2)])),
        migrations.AlterField(
            model_name="executionpreparationcontract", name="resolved_inputs_size_bytes",
            field=models.PositiveIntegerField(validators=[MinValueValidator(2)])),
        migrations.AddConstraint(
            model_name="executionpreparationcontract",
            constraint=models.CheckConstraint(
                condition=models.Q(intent_schema_version__gte=1, resolved_inputs_schema_version__gte=1,
                    review_contract_schema_version__gte=1, intent_size_bytes__gte=2,
                    resolved_inputs_size_bytes__gte=2, review_contract_size_bytes__lte=65536),
                name="ck_exec_prep_contract_size")),
    ]
