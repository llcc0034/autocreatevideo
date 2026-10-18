import uuid

import django.core.validators
import django.db.models.deletion
from django.db import migrations, models


class Migration(migrations.Migration):
    dependencies = [("studio", "0220_production_material_fulfillment")]

    # No guessed backfill from the old mixed participant/reference pools.
    operations = [
        migrations.CreateModel(name="ShotCharacterRelation", fields=[
            ("id", models.UUIDField(default=uuid.uuid4, editable=False, primary_key=True, serialize=False)),
            ("created_at", models.DateTimeField(auto_now_add=True)),
            ("updated_at", models.DateTimeField(auto_now=True)),
            ("role", models.CharField(choices=[("visible", "可见人物"), ("speaker", "对白发言者")], max_length=8)),
            ("position", models.PositiveIntegerField()),
            ("character", models.ForeignKey(on_delete=django.db.models.deletion.PROTECT,
                related_name="shot_relations", to="studio.characterbible")),
            ("shot", models.ForeignKey(db_index=False, on_delete=django.db.models.deletion.CASCADE,
                related_name="character_relations", to="studio.shot")),
        ], options={"ordering": ("role", "position"), "constraints": [
            models.UniqueConstraint(fields=("shot", "role", "position"), name="uk_shot_character_role_position"),
            models.UniqueConstraint(fields=("shot", "character"), condition=models.Q(role="visible"),
                name="uk_shot_visible_character"),
            models.CheckConstraint(condition=models.Q(role__in=("visible", "speaker")), name="ck_shot_character_role"),
        ]}),
        migrations.CreateModel(name="ShotReferenceUse", fields=[
            ("id", models.UUIDField(default=uuid.uuid4, editable=False, primary_key=True, serialize=False)),
            ("created_at", models.DateTimeField(auto_now_add=True)),
            ("updated_at", models.DateTimeField(auto_now=True)),
            ("use_key", models.CharField(max_length=64)),
            ("position", models.PositiveIntegerField()),
            ("media_kind", models.CharField(choices=[("image", "图片"), ("video", "视频"), ("audio", "音频")], max_length=8)),
            ("purpose", models.CharField(max_length=160)),
            ("source_mode", models.CharField(blank=True, choices=[("upload", "上传素材"), ("existing", "已有素材"),
                ("generated", "模型生成")], max_length=16)),
            ("asset_sha256", models.CharField(blank=True, max_length=64,
                validators=[django.core.validators.RegexValidator(r"^[0-9a-f]{64}$")])),
            ("asset", models.ForeignKey(blank=True, null=True, on_delete=django.db.models.deletion.PROTECT,
                related_name="shot_reference_uses", to="studio.asset")),
            ("reference_version", models.ForeignKey(blank=True, null=True, on_delete=django.db.models.deletion.PROTECT,
                related_name="shot_reference_uses", to="studio.assetreferenceversion")),
            ("setting_revision", models.ForeignKey(blank=True, null=True, on_delete=django.db.models.deletion.PROTECT,
                related_name="shot_reference_uses", to="studio.biblevariantrevision")),
            ("shot", models.ForeignKey(db_index=False, on_delete=django.db.models.deletion.CASCADE,
                related_name="reference_uses", to="studio.shot")),
        ], options={"ordering": ("position",), "constraints": [
            models.UniqueConstraint(fields=("shot", "use_key"), name="uk_shot_reference_use_key"),
            models.UniqueConstraint(fields=("shot", "position"), name="uk_shot_reference_use_position"),
            models.CheckConstraint(condition=models.Q(media_kind__in=("image", "video", "audio")), name="ck_shot_use_media_kind"),
            models.CheckConstraint(condition=~models.Q(use_key="") & ~models.Q(purpose=""), name="ck_shot_use_identity"),
            models.CheckConstraint(condition=models.Q(source_mode__in=("", "generated", "existing", "upload")),
                name="ck_shot_use_source_mode"),
            models.CheckConstraint(condition=(models.Q(reference_version__isnull=True, asset__isnull=True, asset_sha256="")
                | ((models.Q(reference_version__isnull=False, asset__isnull=True)
                    | models.Q(reference_version__isnull=True, asset__isnull=False))
                    & ~models.Q(source_mode="") & ~models.Q(asset_sha256=""))), name="ck_shot_use_exact_source"),
        ]}),
    ]
