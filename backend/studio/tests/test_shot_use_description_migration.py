"""0229 keeps original descriptions and permits no creative description."""
from django.db import IntegrityError, connection, transaction

from studio.tests.test_configuration_migrations import _MigrationTestCase


class ShotUseDescriptionMigrationTests(_MigrationTestCase):
    migrate_from = "0228_manual_shot_reference_selection_action"
    migrate_to = "0229_optional_shot_use_description"

    def set_up_before_migration(self, apps):
        actor = apps.get_model("auth", "User").objects.create(username="optional-shot-purpose")
        project = apps.get_model("studio", "SeriesProject").objects.create(owner=actor, title="Isolated fixture",
            width=768, height=1024, fps=24, default_shot_duration_ms=5000, continuity_seed=1)
        episode = apps.get_model("studio", "Episode").objects.create(project=project, number=1)
        shot = apps.get_model("studio", "Shot").objects.create(episode=episode, number=1,
            duration_ms=5000, media_type="video", include_audio=False)
        self.shot_id = shot.pk
        row = apps.get_model("studio", "ShotReferenceUse").objects.create(shot=shot, use_key="coat",
            position=0, media_kind="image", purpose="本镜头延续雨衣造型", source_mode="")
        self.original = apps.get_model("studio", "ShotReferenceUse").objects.filter(pk=row.pk).values().get()

    def test_preserves_original_and_allows_blank_but_not_missing_identity(self):
        use = self.apps.get_model("studio", "ShotReferenceUse")
        self.assertEqual(use.objects.get(pk=self.original["id"]).purpose, "本镜头延续雨衣造型")
        self.assertEqual(use.objects.filter(pk=self.original["id"]).values().get(), self.original)
        row = use.objects.create(shot_id=self.shot_id, use_key="sound", position=1,
            media_kind="audio", purpose="", source_mode="")
        self.assertEqual(use.objects.get(pk=row.pk).purpose, "")
        for key, position in (("", 2), ("sound", 3)):
            with self.assertRaises(IntegrityError), transaction.atomic():
                use.objects.create(shot_id=self.shot_id, use_key=key, position=position,
                    media_kind="audio", purpose="", source_mode="")
        connection.check_constraints()
