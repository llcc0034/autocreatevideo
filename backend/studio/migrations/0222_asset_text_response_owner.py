import django.db.models.deletion
from django.db import migrations, models


class Migration(migrations.Migration):
    dependencies = [("studio", "0221_shot_semantic_relations")]

    # Existing production responses keep their original owner and bytes.
    # No historical AssetGenerationJob response is reconstructed or backfilled.
    operations = [
        migrations.AlterField(model_name="executiontextresponsereview", name="attempt",
            field=models.OneToOneField(blank=True, null=True, on_delete=django.db.models.deletion.PROTECT,
                related_name="text_response_review", to="studio.generationattempt")),
        migrations.AddField(model_name="executiontextresponsereview", name="asset_job",
            field=models.OneToOneField(blank=True, null=True, on_delete=django.db.models.deletion.PROTECT,
                related_name="text_response_review", to="studio.assetgenerationjob")),
        migrations.AddConstraint(model_name="executiontextresponsereview", constraint=models.CheckConstraint(
            condition=models.Q(attempt__isnull=False, asset_job__isnull=True)
                | models.Q(attempt__isnull=True, asset_job__isnull=False), name="ck_text_response_single_owner")),
    ]
