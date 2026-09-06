from django.db import migrations


class Migration(migrations.Migration):

    dependencies = [
        ("studio", "0120_runtime_execution_policy"),
    ]

    operations = [
        migrations.DeleteModel(name="ShotDetailDraftBatchItem"),
        migrations.DeleteModel(name="ShotDetailDraftBatch"),
        migrations.DeleteModel(name="ShotDetailDraft"),
        migrations.DeleteModel(name="EpisodeCreativeDraft"),
    ]
