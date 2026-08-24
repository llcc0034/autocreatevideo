import uuid

import django.core.validators
import django.db.models.deletion
import django.utils.timezone
from django.db import migrations, models


def fail_unfrozen_active_assemblies(apps, schema_editor):
    AssemblyJob = apps.get_model("studio", "AssemblyJob")
    AssemblyJob.objects.filter(status__in=("queued", "running")).update(
        status="failed",
        error_message=(
            "升级后缺少不可变逐镜来源清单；为避免从旧任务推断或伪造输入，"
            "请通过 ProductionRun 显式重试组装。"
        ),
        finished_at=django.utils.timezone.now(),
        claim_token=None,
        worker_id="",
        updated_at=django.utils.timezone.now(),
    )


class Migration(migrations.Migration):

    dependencies = [
        ("studio", "0042_config_boot_activation_and_leases"),
    ]

    operations = [
        migrations.CreateModel(
            name="AssemblySourceItem",
            fields=[
                ("id", models.UUIDField(default=uuid.uuid4, editable=False, primary_key=True, serialize=False)),
                ("created_at", models.DateTimeField(auto_now_add=True)),
                ("updated_at", models.DateTimeField(auto_now=True)),
                ("ordinal", models.PositiveIntegerField(validators=[django.core.validators.MinValueValidator(1)])),
                ("storage_backend", models.CharField(max_length=40)),
                ("object_key", models.CharField(max_length=1000)),
                ("mime_type", models.CharField(max_length=120)),
                ("size_bytes", models.PositiveBigIntegerField()),
                ("sha256", models.CharField(max_length=64)),
                ("start_ms", models.PositiveIntegerField(default=0)),
                ("duration_ms", models.PositiveIntegerField(validators=[django.core.validators.MinValueValidator(1)])),
                ("trim_to_shot", models.BooleanField(default=False)),
                ("has_audio_stream", models.BooleanField(default=False)),
                ("assembly_job", models.ForeignKey(on_delete=django.db.models.deletion.CASCADE, related_name="source_items", to="studio.assemblyjob")),
                ("generated_artifact", models.ForeignKey(blank=True, null=True, on_delete=django.db.models.deletion.PROTECT, related_name="assembly_source_items", to="studio.generatedartifact")),
                ("generation_task", models.ForeignKey(blank=True, null=True, on_delete=django.db.models.deletion.PROTECT, related_name="assembly_source_items", to="studio.generationtask")),
                ("shot", models.ForeignKey(on_delete=django.db.models.deletion.PROTECT, related_name="assembly_source_items", to="studio.shot")),
            ],
            options={"ordering": ("assembly_job", "ordinal")},
        ),
        migrations.AddConstraint(
            model_name="assemblysourceitem",
            constraint=models.UniqueConstraint(fields=("assembly_job", "ordinal"), name="unique_assembly_source_ordinal"),
        ),
        migrations.AddConstraint(
            model_name="assemblysourceitem",
            constraint=models.UniqueConstraint(fields=("assembly_job", "shot"), name="unique_assembly_source_shot"),
        ),
        migrations.AddConstraint(
            model_name="assemblysourceitem",
            constraint=models.CheckConstraint(
                condition=(
                    models.Q(("generated_artifact__isnull", True), ("generation_task__isnull", False))
                    | models.Q(("generated_artifact__isnull", False), ("generation_task__isnull", True))
                ),
                name="assembly_source_exactly_one_origin",
            ),
        ),
        migrations.AddIndex(
            model_name="assemblysourceitem",
            index=models.Index(fields=["assembly_job", "ordinal"], name="studio_asse_assembl_cdebbd_idx"),
        ),
        migrations.AddIndex(
            model_name="assemblysourceitem",
            index=models.Index(fields=["generated_artifact", "assembly_job"], name="studio_asse_generat_3b4e90_idx"),
        ),
        migrations.RunPython(fail_unfrozen_active_assemblies, migrations.RunPython.noop),
    ]
