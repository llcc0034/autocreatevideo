import django.db.models.deletion
from django.db import migrations, models


class Migration(migrations.Migration):
    dependencies = [("studio", "0204_content_source_structure")]

    operations = [
        migrations.AddField(
            model_name="businessoutputversion",
            name="data_structure_version",
            field=models.ForeignKey(blank=True, null=True, on_delete=django.db.models.deletion.PROTECT,
                related_name="business_output_versions", to="studio.datastructureversion"),
        ),
    ]
