from django.conf import settings
from django.db import migrations, models
import django.db.models.deletion


class Migration(migrations.Migration):

    dependencies = [
        migrations.swappable_dependency(settings.AUTH_USER_MODEL),
        ("studio", "0050_access_rates_and_prompt_language_policy"),
    ]

    operations = [
        migrations.AddField(
            model_name="modelartifact",
            name="verified_at",
            field=models.DateTimeField(blank=True, null=True),
        ),
        migrations.AddField(
            model_name="modelartifact",
            name="verified_by",
            field=models.ForeignKey(
                blank=True,
                null=True,
                on_delete=django.db.models.deletion.PROTECT,
                related_name="verified_model_artifacts",
                to=settings.AUTH_USER_MODEL,
            ),
        ),
        migrations.AddField(
            model_name="modelartifact",
            name="verified_snapshot",
            field=models.ForeignKey(
                blank=True,
                null=True,
                on_delete=django.db.models.deletion.PROTECT,
                related_name="verified_model_artifacts",
                to="studio.hostcapabilitysnapshot",
            ),
        ),
        migrations.AlterField(
            model_name="modeldeploymentcapabilitysnapshot",
            name="deployment",
            field=models.ForeignKey(
                on_delete=django.db.models.deletion.PROTECT,
                related_name="capability_snapshots",
                to="studio.modeldeployment",
            ),
        ),
        migrations.AlterField(
            model_name="runtimemodelavailability",
            name="deployment",
            field=models.ForeignKey(
                on_delete=django.db.models.deletion.PROTECT,
                related_name="runtime_availability",
                to="studio.modeldeployment",
            ),
        ),
    ]
