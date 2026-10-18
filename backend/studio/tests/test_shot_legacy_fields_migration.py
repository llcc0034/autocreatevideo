"""0230 drops only obsolete pools; never translates them into authored facts."""
from django.db import connection

from studio.tests.test_configuration_migrations import _MigrationTestCase


class ShotLegacyFieldsMigrationTests(_MigrationTestCase):
    migrate_from = "0229_optional_shot_use_description"
    migrate_to = "0230_retire_shot_mixed_reference_fields"

    def set_up_before_migration(self, apps):
        actor = apps.get_model("auth", "User").objects.create(username="isolated-shot-retirement")
        project = apps.get_model("studio", "SeriesProject").objects.create(owner=actor, title="Migration fixture",
            width=768, height=1024, fps=24, default_shot_duration_ms=1000, continuity_seed=0)
        character = apps.get_model("studio", "CharacterBible").objects.create(project=project,
            slug="voice", name="Voice", stable_speaker_id="S1")
        scene = apps.get_model("studio", "SceneBible").objects.create(project=project, slug="room", name="Room")
        prop = apps.get_model("studio", "PropBible").objects.create(project=project, slug="key", name="Key")
        episode = apps.get_model("studio", "Episode").objects.create(project=project, number=1, title="Fixture")
        asset = apps.get_model("studio", "Asset").objects.create(owner=actor, name="Stored identity only",
            kind="reference", checksum="a" * 64, mime_type="image/png", file="isolated/not-resolved.png")
        Shot = apps.get_model("studio", "Shot")
        rows = []
        for number in (1, 2):
            shot = Shot.objects.create(episode=episode, number=number, scene=scene, narrative="  原文，不拼接  ",
                duration_ms=6000, episode_start_ms=1200, episode_end_ms=7200, media_type="video", include_audio=True,
                dialogue=[{"speaker_id": "S1", "text": "  原对白  ", "delivery": " softly ", "voiceover": True,
                    "start_ms": 123, "end_ms": 987}], metadata={"kept": "value", "reference_bindings": {"opaque": "kept"}},
                reference_bindings={"first_frame_asset_id": str(asset.pk)})
            shot.characters.add(character)
            shot.reference_assets.add(asset)
            shot.props.add(prop)
            rows.append(shot)
        apps.get_model("studio", "ShotCharacterRelation").objects.create(shot=rows[0], character=character,
            role="speaker", position=0)
        apps.get_model("studio", "ShotReferenceUse").objects.create(shot=rows[0], use_key="chosen", position=0,
            media_kind="image", purpose="  可选描述 first_frame  ", source_mode="existing", asset=asset, asset_sha256=asset.checksum)
        self.old_only_id = rows[1].pk
        self.before_shots = [{k: v for k, v in row.items() if k != "reference_bindings"}
            for row in Shot.objects.order_by("pk").values()]
        self.preserved = {name: list(apps.get_model("studio", name).objects.order_by("pk").values())
            for name in ("ShotCharacterRelation", "ShotReferenceUse", "Asset", "CharacterBible", "SceneBible", "PropBible")}
        self.props = list(Shot.props.through.objects.order_by("pk").values())

    def test_old_storage_removed_without_rewriting_or_fabricating_new_facts(self):
        Shot = self.apps.get_model("studio", "Shot")
        self.assertEqual(list(Shot.objects.order_by("pk").values()), self.before_shots)
        self.assertEqual(list(Shot.props.through.objects.order_by("pk").values()), self.props)
        for name, before in self.preserved.items():
            self.assertEqual(list(self.apps.get_model("studio", name).objects.order_by("pk").values()), before)
        self.assertFalse(self.apps.get_model("studio", "ShotCharacterRelation").objects.filter(shot_id=self.old_only_id).exists())
        self.assertFalse(self.apps.get_model("studio", "ShotReferenceUse").objects.filter(shot_id=self.old_only_id).exists())
        self.assertFalse({"characters", "reference_assets", "reference_bindings"} & {f.name for f in Shot._meta.get_fields()})
        tables = connection.introspection.table_names()
        self.assertNotIn("studio_shot_characters", tables)
        self.assertNotIn("studio_shot_reference_assets", tables)
        with connection.cursor() as cursor:
            columns = connection.introspection.get_table_description(cursor, "studio_shot")
        self.assertNotIn("reference_bindings", {column.name for column in columns})
        connection.check_constraints()
