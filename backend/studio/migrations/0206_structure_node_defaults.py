from django.db import migrations, models


class Migration(migrations.Migration):
    dependencies = [("studio", "0205_business_output_structure")]

    operations = [
        migrations.AddField(model_name="datastructurenode", name="has_default", field=models.BooleanField(default=False)),
        migrations.AddField(model_name="datastructurenode", name="default_value", field=models.JSONField(blank=True, null=True)),
    ]
