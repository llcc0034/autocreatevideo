from django.db import migrations, models


class Migration(migrations.Migration):
    dependencies = [("studio", "0170_prompt_profile_current_schema_default")]

    operations = [migrations.AddIndex(
        model_name="productionrun",
        index=models.Index(fields=("plan_source", "status", "id"), name="idx_run_auto_advance"),
    )]
