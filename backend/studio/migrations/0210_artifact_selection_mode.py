from django.conf import settings
from django.db import migrations, models
import django.db.models.deletion


class Migration(migrations.Migration):
    dependencies = [("studio", "0209_execution_media_source_policy")]

    operations = [
        migrations.AddField(
            model_name="artifactselectiondecision", name="selection_mode",
            field=models.CharField(choices=[("manual", "人工选择"), ("automatic", "按配置自动选择")],
                default="manual", max_length=16),
        ),
        migrations.AlterField(
            model_name="artifactselectiondecision", name="selected_by",
            field=models.ForeignKey(blank=True, null=True, on_delete=django.db.models.deletion.PROTECT,
                related_name="artifact_selection_decisions", to=settings.AUTH_USER_MODEL),
        ),
        migrations.AddConstraint(
            model_name="artifactselectiondecision",
            constraint=models.CheckConstraint(condition=(models.Q(selection_mode="manual", selected_by__isnull=False)
                | models.Q(selection_mode="automatic", selected_by__isnull=True)), name="artifact_selection_actor_mode"),
        ),
    ]
