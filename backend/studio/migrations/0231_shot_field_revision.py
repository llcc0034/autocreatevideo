import uuid

from django.conf import settings
from django.db import migrations, models
import django.db.models.deletion


class Migration(migrations.Migration):
    dependencies = [
        ("studio", "0230_retire_shot_mixed_reference_fields"),
        migrations.swappable_dependency(settings.AUTH_USER_MODEL),
    ]

    # Empty append-only detail table. No old prose conversion or live adoption.
    operations = [migrations.CreateModel(
        name="ShotFieldRevision",
        fields=[
            ("id", models.UUIDField(default=uuid.uuid4, editable=False, primary_key=True, serialize=False)),
            ("created_at", models.DateTimeField(auto_now_add=True)),
            ("updated_at", models.DateTimeField(auto_now=True)),
            ("revision", models.PositiveIntegerField()),
            ("source_sha256", models.CharField(max_length=64)),
            ("overrides", models.JSONField()),
            ("content_schema_version", models.PositiveSmallIntegerField(default=1)),
            ("content_size_bytes", models.PositiveIntegerField()),
            ("content_sha256", models.CharField(max_length=64)),
            ("request_id", models.UUIDField()),
            ("request_sha256", models.CharField(max_length=64)),
            ("created_by", models.ForeignKey(on_delete=django.db.models.deletion.PROTECT,
                related_name="shot_field_revisions", to=settings.AUTH_USER_MODEL)),
            ("previous", models.ForeignKey(blank=True, null=True, on_delete=django.db.models.deletion.PROTECT,
                related_name="+", to="studio.shotfieldrevision")),
            ("shot", models.ForeignKey(db_index=False, on_delete=django.db.models.deletion.CASCADE,
                related_name="field_revisions", to="studio.shot")),
        ],
        options={"ordering": ("-revision",), "constraints": [
            models.UniqueConstraint(fields=("shot", "revision"), name="uk_shot_field_revision"),
            models.UniqueConstraint(fields=("shot", "request_id"), name="uk_shot_field_request"),
            models.CheckConstraint(condition=models.Q(revision__gte=1), name="ck_shot_field_revision"),
            models.CheckConstraint(condition=models.Q(content_size_bytes__lte=65536), name="ck_shot_field_bytes"),
            models.CheckConstraint(condition=models.Q(content_schema_version=1), name="ck_shot_field_schema"),
        ]},
    )]
