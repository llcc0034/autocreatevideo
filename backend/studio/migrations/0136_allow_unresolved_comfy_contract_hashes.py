from django.db import migrations, models


class Migration(migrations.Migration):
    dependencies = [
        ("studio", "0135_workflow_projector_and_candidate_admission"),
    ]

    operations = [
        migrations.AlterField(
            model_name="comfyworkflowcontractdraft",
            name="converted_graph_sha256",
            field=models.CharField(blank=True, default="", max_length=64),
        ),
        migrations.AlterField(
            model_name="comfyworkflowcontractdraft",
            name="structural_contract_sha256",
            field=models.CharField(blank=True, default="", max_length=64),
        ),
    ]
