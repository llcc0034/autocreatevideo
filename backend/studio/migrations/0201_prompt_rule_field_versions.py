import django.db.models.deletion
from django.db import migrations, models


class Migration(migrations.Migration):
    dependencies = [("studio", "0200_prompt_rule_value_ownership")]

    operations = [
        migrations.AddField(
            model_name="promptprofilefieldrule",
            name="field_definition_version",
            field=models.ForeignKey(
                blank=True, null=True, on_delete=django.db.models.deletion.PROTECT,
                related_name="profile_rules", to="studio.fielddefinitionversion",
            ),
        ),
        migrations.AlterField(
            model_name="promptprofileversion", name="contract_schema_version",
            field=models.PositiveIntegerField(default=8),
        ),
    ]
