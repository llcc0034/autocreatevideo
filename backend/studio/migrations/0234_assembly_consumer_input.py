from django.db import migrations, models
import django.db.models.deletion


class Migration(migrations.Migration):
    dependencies = [("studio", "0233_alter_promptprofilefieldrule_source_selector")]
    operations = [
        migrations.AddField(
            model_name="assemblyjob", name="consumer_stage",
            field=models.ForeignKey(blank=True, null=True, on_delete=django.db.models.deletion.PROTECT,
                related_name="assembly_jobs", to="studio.stagerun"),
        ),
        migrations.AddField(
            model_name="assemblyjob", name="consumer_input_sha256",
            field=models.CharField(blank=True, max_length=64),
        ),
        # No guessed backfill: old jobs have no proven consuming input version.
        migrations.AddConstraint(
            model_name="assemblyjob",
            constraint=models.CheckConstraint(condition=(models.Q(consumer_stage__isnull=True, consumer_input_sha256="")
                | (models.Q(consumer_stage__isnull=False) & ~models.Q(consumer_input_sha256=""))),
                name="assembly_consumer_input_pair"),
        ),
    ]
