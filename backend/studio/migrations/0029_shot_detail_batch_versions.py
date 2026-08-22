from __future__ import annotations

import django.db.models.deletion
from django.db import migrations, models
from django.db.models import Q


def _legacy_key(episode_id) -> str:
    return f"legacy:{episode_id}:initial"


def _shot_key(shot) -> str:
    metadata = shot.metadata if isinstance(shot.metadata, dict) else {}
    return str(metadata.get("creative_version_key") or "").strip() or _legacy_key(
        shot.episode_id,
    )


def _shot_label(shot) -> str:
    metadata = shot.metadata if isinstance(shot.metadata, dict) else {}
    value = str(metadata.get("creative_version_label") or "").strip()
    if value:
        return value
    return "初始分镜" if not str(metadata.get("creative_version_key") or "").strip() else "分镜版本"


def backfill_batch_versions(apps, schema_editor):
    Batch = apps.get_model("studio", "ShotDetailDraftBatch")
    Item = apps.get_model("studio", "ShotDetailDraftBatchItem")
    for batch in Batch.objects.all().iterator():
        items = list(
            Item.objects.filter(batch_id=batch.pk)
            .select_related("shot", "shot__episode")
            .order_by("ordinal", "id")
        )
        total_count = int(batch.total_count or 0)
        ordinals = [int(item.ordinal) for item in items]
        members_valid = all(
            item.shot.episode_id == batch.episode_id
            and item.shot.episode.project_id == batch.project_id
            for item in items
        )
        if not items:
            batch.shot_version_key = None
            batch.shot_version_label = "历史归属未知"
            batch.version_scope_status = "legacy_unknown"
        elif len(items) != total_count:
            batch.shot_version_key = None
            batch.shot_version_label = "历史镜头记录不完整"
            batch.version_scope_status = "legacy_partial"
        elif ordinals != list(range(1, total_count + 1)) or not members_valid:
            batch.shot_version_key = None
            batch.shot_version_label = "历史归属无法验证"
            batch.version_scope_status = "legacy_unknown"
        else:
            keys = {_shot_key(item.shot) for item in items}
            if len(keys) == 1:
                batch.shot_version_key = next(iter(keys))
                batch.shot_version_label = _shot_label(items[0].shot)
                batch.version_scope_status = "resolved"
            else:
                batch.shot_version_key = None
                batch.shot_version_label = "历史混合版本批次"
                batch.version_scope_status = "legacy_mixed"
        batch.save(update_fields=(
            "shot_version_key", "shot_version_label", "version_scope_status",
        ))


class Migration(migrations.Migration):

    dependencies = [
        ("studio", "0028_deferred_target_workflow_overrides"),
    ]

    operations = [
        migrations.AddField(
            model_name="shotdetaildraftbatch",
            name="shot_version_key",
            field=models.CharField(blank=True, max_length=191, null=True),
        ),
        migrations.AddField(
            model_name="shotdetaildraftbatch",
            name="shot_version_label",
            field=models.CharField(blank=True, default="", max_length=200),
            preserve_default=False,
        ),
        migrations.AddField(
            model_name="shotdetaildraftbatch",
            name="version_scope_status",
            field=models.CharField(
                choices=[
                    ("resolved", "已归属分镜版本"),
                    ("legacy_mixed", "历史混合版本"),
                    ("legacy_partial", "历史镜头记录不完整"),
                    ("legacy_unknown", "历史归属未知"),
                ],
                default="resolved",
                max_length=20,
            ),
        ),
        migrations.RunPython(backfill_batch_versions, migrations.RunPython.noop),
        migrations.AlterField(
            model_name="shotdetaildraftbatchitem",
            name="shot",
            field=models.ForeignKey(
                on_delete=django.db.models.deletion.RESTRICT,
                related_name="detail_batch_items",
                to="studio.shot",
            ),
        ),
        migrations.RemoveConstraint(
            model_name="shotdetaildraftbatch",
            name="unique_active_shot_detail_batch",
        ),
        migrations.RemoveIndex(
            model_name="shotdetaildraftbatch",
            name="studio_sdbatch_scope_idx",
        ),
        migrations.AddConstraint(
            model_name="shotdetaildraftbatch",
            constraint=models.UniqueConstraint(
                condition=Q(
                    version_scope_status="resolved",
                    generation_status__in=("queued", "running"),
                ),
                fields=("episode", "shot_version_key"),
                name="unique_active_sdetail_version",
            ),
        ),
        migrations.AddConstraint(
            model_name="shotdetaildraftbatch",
            constraint=models.CheckConstraint(
                condition=(
                    Q(version_scope_status="resolved", shot_version_key__isnull=False)
                    | Q(
                        version_scope_status__in=(
                            "legacy_mixed", "legacy_partial", "legacy_unknown",
                        ),
                        shot_version_key__isnull=True,
                    )
                ),
                name="sdetail_version_scope_consistent",
            ),
        ),
        migrations.AddIndex(
            model_name="shotdetaildraftbatch",
            index=models.Index(
                fields=(
                    "project", "episode", "shot_version_key",
                    "generation_status", "created_at",
                ),
                name="studio_sdbatch_ver_idx",
            ),
        ),
    ]
