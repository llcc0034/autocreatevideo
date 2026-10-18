from django.db import migrations, models
import django.db.models.deletion


class Migration(migrations.Migration):
    dependencies = [("studio", "0206_structure_node_defaults")]

    operations = [migrations.AddField(
        model_name="executioninputspec", name="data_structure_version",
        field=models.ForeignKey(blank=True, null=True, on_delete=django.db.models.deletion.PROTECT,
            related_name="execution_inputs", to="studio.datastructureversion"),
    )]
