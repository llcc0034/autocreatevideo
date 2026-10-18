import django.db.models.deletion
from django.db import migrations, models


class Migration(migrations.Migration):
    dependencies = [("studio", "0224_material_fulfillment_selection_mode")]

    operations = [migrations.AddField(
        model_name="executionpreparation", name="supersedes",
        field=models.OneToOneField(blank=True, null=True,
            on_delete=django.db.models.deletion.PROTECT,
            related_name="replacement", to="studio.executionpreparation"),
    )]
