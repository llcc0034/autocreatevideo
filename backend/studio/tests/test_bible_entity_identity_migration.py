"""0215 only adds key identity schema; it does not infer keys or rewrite history."""
from django.db import connection
from django.db.migrations.executor import MigrationExecutor

from studio.tests.test_configuration_migrations import _MigrationTestCase


class BibleEntityIdentityMigrationTests(_MigrationTestCase):
    migrate_from = "0214_retire_script_resource_advisory"
    migrate_to = "0215_bible_entity_identity"

    def set_up_before_migration(self, apps):
        actor = apps.get_model("auth", "User").objects.create(username="identity-schema")
        project = apps.get_model("studio", "SeriesProject").objects.create(owner=actor, title="Isolated schema fixture",
            width=768, height=1024, fps=24, default_shot_duration_ms=1000, continuity_seed=1)
        Character = apps.get_model("studio", "CharacterBible")
        for number in (1, 2):
            Character.objects.create(project=project, slug=f"fixture-{number}", name="同名实体",
                stable_speaker_id=f"S{number}", visual_identity=f"不同设定{number}")
        self.project_id = project.pk
        self.before = list(Character.objects.filter(project=project).order_by("id").values())

    def assert_source_unchanged(self):
        self.assertEqual(list(self.apps.get_model("studio", "CharacterBible").objects.filter(
            project_id=self.project_id).order_by("id").values()), self.before)
        self.assertFalse(self.apps.get_model("studio", "BibleEntityIdentity").objects.exists())

    def test_adds_no_name_matched_identities_and_rewrites_no_entities(self):
        self.assert_source_unchanged()

    def test_empty_identity_schema_roundtrip_keeps_existing_entities(self):
        MigrationExecutor(connection).migrate([("studio", self.migrate_from)])
        MigrationExecutor(connection).migrate([("studio", self.migrate_to)])
        self.assert_source_unchanged()
