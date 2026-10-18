from django.db import migrations, models
from django.conf import settings
import django.db.models.deletion


class Migration(migrations.Migration):
    dependencies = [("studio", "0159_project_translation_sources")]
    operations = [migrations.AddField(
        model_name="generationattempt", name="retry_of",
        field=models.OneToOneField(blank=True, null=True, on_delete=django.db.models.deletion.PROTECT,
            related_name="retry_successor", to="studio.generationattempt"),
    ), migrations.AddField(
        model_name="generationattempt", name="retry_requested_by",
        field=models.ForeignKey(blank=True, null=True, on_delete=django.db.models.deletion.PROTECT,
            related_name="requested_generation_retries", to=settings.AUTH_USER_MODEL),
    ), migrations.AddConstraint(
        model_name="generationattempt", constraint=models.CheckConstraint(
            condition=~models.Q(retry_of=models.F("pk")), name="ck_attempt_retry_not_self"),
    ), migrations.AddConstraint(
        model_name="generationattempt", constraint=models.CheckConstraint(condition=(
            models.Q(retry_of__isnull=True, retry_requested_by__isnull=True)
            | models.Q(retry_of__isnull=False, retry_requested_by__isnull=False, role="primary")), name="ck_attempt_retry_actor"),
    )]
