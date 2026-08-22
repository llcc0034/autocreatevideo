from __future__ import annotations

from datetime import datetime, timezone as datetime_timezone

from django.db import migrations, models
from django.db.models import Count, Q


def demote_duplicate_applied_creative_drafts(apps, schema_editor):
    EpisodeCreativeDraft = apps.get_model("studio", "EpisodeCreativeDraft")
    duplicate_episode_ids = (
        EpisodeCreativeDraft.objects
        .filter(status="applied")
        .values("episode_id")
        .annotate(applied_count=Count("id"))
        .filter(applied_count__gt=1)
        .values_list("episode_id", flat=True)
    )
    epoch = datetime.min.replace(tzinfo=datetime_timezone.utc)
    for episode_id in duplicate_episode_ids:
        rows = list(
            EpisodeCreativeDraft.objects
            .filter(episode_id=episode_id, status="applied")
        )
        rows.sort(
            key=lambda item: (
                item.applied_at is not None,
                item.applied_at or epoch,
                item.generation_no,
                item.created_at or epoch,
                str(item.pk),
            ),
            reverse=True,
        )
        for draft in rows[1:]:
            draft.revision_history = [
                *(draft.revision_history or []),
                {
                    "revision": draft.revision,
                    "status": draft.status,
                    "draft_data": draft.draft_data,
                    "validation": draft.validation,
                    "updated_at": (
                        draft.updated_at.isoformat()
                        if draft.updated_at else None
                    ),
                    "migration": "0027_unique_applied_episode_creative_draft",
                },
            ]
            draft.revision += 1
            draft.status = "confirmed"
            draft.applied_by_id = None
            draft.applied_at = None
            draft.applied_shot_ids = []
            draft.save(update_fields=(
                "revision",
                "status",
                "applied_by",
                "applied_at",
                "applied_shot_ids",
                "revision_history",
                "updated_at",
            ))


class Migration(migrations.Migration):

    dependencies = [
        ("studio", "0026_deferredgenerationrequest"),
    ]

    operations = [
        migrations.RunPython(
            demote_duplicate_applied_creative_drafts,
            migrations.RunPython.noop,
        ),
        migrations.AddConstraint(
            model_name="episodecreativedraft",
            constraint=models.UniqueConstraint(
                condition=Q(status="applied"),
                fields=("episode",),
                name="unique_applied_creative_draft_per_episode",
            ),
        ),
    ]
