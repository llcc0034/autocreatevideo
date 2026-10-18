from django.db import migrations, models


class Migration(migrations.Migration):
    dependencies = [("studio", "0234_assembly_consumer_input")]
    operations = [
        migrations.AddField(model_name="assetreferenceversion", name="origin_history_cleared",
            field=models.BooleanField(default=False, editable=False,
                help_text="生成来源历史已显式清理；保留素材和主图/变体关系，不伪造上传来源。")),
        migrations.RemoveConstraint(model_name="assetreferenceversion", name="ck_asset_ref_source_origin"),
        migrations.AddConstraint(model_name="assetreferenceversion", constraint=models.CheckConstraint(
            name="ck_asset_ref_source_origin", condition=(
                models.Q(source="generated", origin_history_cleared=False, generated_artifact__isnull=False, generation_job__isnull=True)
                | models.Q(source="generated", origin_history_cleared=False, generated_artifact__isnull=True, generation_job__isnull=False)
                | models.Q(source="generated", origin_history_cleared=True, generated_artifact__isnull=True, generation_job__isnull=True)
                | models.Q(source__in=("upload", "existing"), generated_artifact__isnull=True,
                    generation_job__isnull=True, generated_from__isnull=True, origin_history_cleared=False)))),
    ]
