from django.db import migrations, models


class Migration(migrations.Migration):
    dependencies = [("studio", "0147_execution_smoke_authorization")]
    operations = [migrations.AddField(model_name="workflowsmokeattempt", name="staged_inputs",
        field=models.JSONField(default=list, blank=True))]
