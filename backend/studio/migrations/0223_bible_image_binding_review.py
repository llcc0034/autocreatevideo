import django.db.models.deletion
from django.db import migrations, models


class Migration(migrations.Migration):
    dependencies = [("studio", "0222_asset_text_response_owner")]

    # No historical choice/approval is inferred or backfilled from Asset links.
    operations = [
        migrations.AddField(model_name=name, name="image_binding_review",
            field=models.ForeignKey(blank=True, null=True,
                on_delete=django.db.models.deletion.RESTRICT, related_name="+", to="studio.stagerun"))
        for name in ("characterbible", "scenebible", "propbible")
    ]
