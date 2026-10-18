import uuid

import django.core.validators
import django.db.models.deletion
from django.conf import settings
from django.db import migrations, models


class Migration(migrations.Migration):
    dependencies = [
        ("studio", "0210_artifact_selection_mode"),
        migrations.swappable_dependency(settings.AUTH_USER_MODEL),
    ]

    operations = [
        migrations.CreateModel(
            name="AssetReferenceSeries",
            fields=[
                ("id", models.UUIDField(default=uuid.uuid4, editable=False, primary_key=True, serialize=False)),
                ("created_at", models.DateTimeField(auto_now_add=True)),
                ("updated_at", models.DateTimeField(auto_now=True)),
                ("name", models.CharField(max_length=160)),
                ("role", models.CharField(choices=[("primary", "主图"), ("variant", "变体")], max_length=8)),
                ("bible_variant", models.ForeignKey(blank=True, null=True, on_delete=django.db.models.deletion.PROTECT,
                    related_name="asset_reference_series", to="studio.biblevariant")),
                ("owner", models.ForeignKey(on_delete=django.db.models.deletion.CASCADE,
                    related_name="asset_reference_series", to=settings.AUTH_USER_MODEL)),
                ("primary_series", models.ForeignKey(blank=True, null=True, on_delete=django.db.models.deletion.PROTECT,
                    related_name="variants", to="studio.assetreferenceseries")),
            ],
            options={"ordering": ("name", "id")},
        ),
        migrations.CreateModel(
            name="AssetReferenceVersion",
            fields=[
                ("id", models.UUIDField(default=uuid.uuid4, editable=False, primary_key=True, serialize=False)),
                ("created_at", models.DateTimeField(auto_now_add=True)),
                ("updated_at", models.DateTimeField(auto_now=True)),
                ("version", models.PositiveIntegerField(validators=[django.core.validators.MinValueValidator(1)])),
                ("source", models.CharField(choices=[("upload", "上传素材"), ("existing", "已有素材"),
                    ("generated", "模型生成")], max_length=16)),
                ("asset", models.ForeignKey(on_delete=django.db.models.deletion.PROTECT,
                    related_name="reference_versions", to="studio.asset")),
                ("generated_artifact", models.ForeignKey(blank=True, null=True, on_delete=django.db.models.deletion.PROTECT,
                    related_name="reference_versions", to="studio.generatedartifact")),
                ("generation_job", models.ForeignKey(blank=True, null=True, on_delete=django.db.models.deletion.PROTECT,
                    related_name="reference_versions", to="studio.assetgenerationjob")),
                ("generated_from", models.ForeignKey(blank=True, null=True, on_delete=django.db.models.deletion.PROTECT,
                    related_name="generated_variants", to="studio.assetreferenceversion")),
                ("series", models.ForeignKey(on_delete=django.db.models.deletion.CASCADE,
                    related_name="versions", to="studio.assetreferenceseries")),
                ("setting_revision", models.ForeignKey(blank=True, null=True, on_delete=django.db.models.deletion.PROTECT,
                    related_name="asset_reference_versions", to="studio.biblevariantrevision")),
            ],
            options={"ordering": ("series_id", "-version", "id")},
        ),
        migrations.AddIndex(model_name="assetreferenceseries",
            index=models.Index(fields=["owner", "role", "name"], name="idx_asset_series_owner_role")),
        migrations.AddConstraint(model_name="assetreferenceseries",
            constraint=models.CheckConstraint(condition=(models.Q(role="primary", primary_series__isnull=True)
                | models.Q(role="variant", primary_series__isnull=False)), name="ck_asset_series_role_parent")),
        migrations.AddConstraint(model_name="assetreferenceseries",
            constraint=models.CheckConstraint(condition=~models.Q(primary_series=models.F("id")), name="ck_asset_series_not_self")),
        migrations.AddConstraint(model_name="assetreferenceversion",
            constraint=models.UniqueConstraint(fields=("series", "version"), name="uk_asset_series_version")),
        migrations.AddConstraint(model_name="assetreferenceversion",
            constraint=models.CheckConstraint(condition=models.Q(version__gte=1), name="ck_asset_ref_version_positive")),
        migrations.AddConstraint(model_name="assetreferenceversion",
            constraint=models.CheckConstraint(condition=(
                models.Q(source="generated", generated_artifact__isnull=False, generation_job__isnull=True)
                | models.Q(source="generated", generated_artifact__isnull=True, generation_job__isnull=False)
                | models.Q(source__in=("upload", "existing"), generated_artifact__isnull=True,
                    generation_job__isnull=True, generated_from__isnull=True)), name="ck_asset_ref_source_origin")),
        migrations.AddConstraint(model_name="assetreferenceversion",
            constraint=models.CheckConstraint(condition=~models.Q(generated_from=models.F("id")), name="ck_asset_ref_basis_not_self")),
    ]
