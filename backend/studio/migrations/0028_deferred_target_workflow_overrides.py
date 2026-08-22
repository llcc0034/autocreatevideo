from __future__ import annotations

from django.db import migrations, models


class Migration(migrations.Migration):

    dependencies = [
        ("studio", "0027_unique_applied_episode_creative_draft"),
    ]

    operations = [
        migrations.AddField(
            model_name="deferredgenerationrequest",
            name="target_workflow_overrides",
            field=models.JSONField(blank=True, default=dict),
        ),
    ]
