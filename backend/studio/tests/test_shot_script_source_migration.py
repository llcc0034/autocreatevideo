"""Only add empty source bindings; never turn legacy shots into new authorship."""
from django.db import connection
from django.db.migrations.executor import MigrationExecutor

from studio.tests.test_configuration_migrations import _MigrationTestCase


class ShotScriptSourceMigrationTests(_MigrationTestCase):
    migrate_from = "0225_execution_preparation_supersedes"
    migrate_to = "0226_shot_script_source"

    def set_up_before_migration(self, apps):
        actor = apps.get_model("auth", "User").objects.create(username="shot-source-schema")
        project = apps.get_model("studio", "SeriesProject").objects.create(owner=actor, title="Isolated fixture",
            width=768, height=1024, fps=24, default_shot_duration_ms=5000, continuity_seed=1)
        episode = apps.get_model("studio", "Episode").objects.create(project=project, number=3, title="Original")
        shot = apps.get_model("studio", "Shot").objects.create(episode=episode, number=8, narrative="Original fixed columns",
            duration_ms=5000, media_type="video", include_audio=False)
        self.shot_id = shot.pk
        self.before = apps.get_model("studio", "Shot").objects.filter(pk=shot.pk).values().get()

    def verify_original(self):
        self.assertEqual(self.apps.get_model("studio", "Shot").objects.filter(pk=self.shot_id).values().get(), self.before)
        self.assertFalse(self.apps.get_model("studio", "ShotScriptSource").objects.exists())
        connection.check_constraints()

    def test_upgrade_leaves_existing_shots_and_empty_new_source_table(self):
        self.verify_original()

    def test_empty_reverse_and_upgrade_do_not_invent_source_references(self):
        MigrationExecutor(connection).migrate([("studio", self.migrate_from)])
        MigrationExecutor(connection).migrate([("studio", self.migrate_to)])
        self.verify_original()
