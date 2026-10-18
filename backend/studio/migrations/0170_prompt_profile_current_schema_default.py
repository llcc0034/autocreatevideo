from django.db import migrations, models


class Migration(migrations.Migration):
    dependencies = [("studio", "0169_remove_recommendation_recipe_writer")]
    operations = [
        migrations.AlterField(
            model_name="promptprofileversion",
            name="contract_schema_version",
            field=models.PositiveIntegerField(default=6),
        ),
    ]
