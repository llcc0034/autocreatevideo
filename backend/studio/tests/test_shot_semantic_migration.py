"""0221 adds empty explicit relations; never guesses visibility or material use."""
from django.db import connection
from django.db.migrations.executor import MigrationExecutor

from studio.tests.test_configuration_migrations import _MigrationTestCase


class ShotSemanticMigrationTests(_MigrationTestCase):
    migrate_from = "0220_production_material_fulfillment"
    migrate_to = "0221_shot_semantic_relations"

    def set_up_before_migration(self, apps):
        actor = apps.get_model("auth", "User").objects.create(username="shot-semantic-schema")
        project = apps.get_model("studio", "SeriesProject").objects.create(owner=actor, title="Isolated fixture",
            width=768, height=1024, fps=24, default_shot_duration_ms=1000, continuity_seed=1)
        character = apps.get_model("studio", "CharacterBible").objects.create(project=project, slug="voice",
            name="名字不是出场证据", stable_speaker_id="S1")
        episode = apps.get_model("studio", "Episode").objects.create(project=project, number=1, title="Fixture")
        shot = apps.get_model("studio", "Shot").objects.create(episode=episode, number=1, narrative="空站台",
            duration_ms=1000, media_type="video", include_audio=True,
            dialogue=[{"speaker_id": "S1", "text": "画外音", "is_voiceover": True}])
        shot.characters.add(character)
        self.shot_id, self.character_id = shot.pk, character.pk
        self.before = apps.get_model("studio", "Shot").objects.filter(pk=shot.pk).values().get()

    def assert_not_inferred(self):
        Shot = self.apps.get_model("studio", "Shot")
        self.assertEqual(Shot.objects.filter(pk=self.shot_id).values().get(), self.before)
        self.assertEqual(list(Shot.objects.get(pk=self.shot_id).characters.values_list("pk", flat=True)), [self.character_id])
        self.assertFalse(self.apps.get_model("studio", "ShotCharacterRelation").objects.exists())
        self.assertFalse(self.apps.get_model("studio", "ShotReferenceUse").objects.exists())
        connection.check_constraints()

    def test_upgrade_preserves_original_shot_without_fabricated_visibility(self):
        self.assert_not_inferred()

    def test_empty_schema_reverse_and_upgrade_preserve_original_shot(self):
        MigrationExecutor(connection).migrate([("studio", self.migrate_from)])
        MigrationExecutor(connection).migrate([("studio", self.migrate_to)])
        self.assert_not_inferred()
