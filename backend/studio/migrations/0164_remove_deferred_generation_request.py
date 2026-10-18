"""Drop the retired automatic continuation queue, without converting history.

The removed model only points outward. Shared tasks, assets, shots and workflow
versions are not deleted or rewritten by dropping this table. Reversing the
schema can recreate an empty table; it cannot restore discarded request rows.
"""
from django.db import migrations


class Migration(migrations.Migration):
    dependencies = [("studio", "0163_shot_episode_timing")]

    operations = [migrations.DeleteModel(name="DeferredGenerationRequest")]
