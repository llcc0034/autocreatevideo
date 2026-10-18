"""0232 removes fixed creative columns without inventing field/source history."""
from uuid import uuid4

from django.db import connection

from studio.tests.test_configuration_migrations import _MigrationTestCase


class ShotCreativeColumnsMigrationTests(_MigrationTestCase):
    migrate_from = "0231_shot_field_revision"
    migrate_to = "0232_retire_shot_fixed_creative_columns"
    retired = {"narrative", "detailed_description", "camera", "action",
        "sound_effects", "music", "continuity_notes"}

    def set_up_before_migration(self, apps):
        actor = apps.get_model("auth", "User").objects.create(username="isolated-creative-retirement")
        project = apps.get_model("studio", "SeriesProject").objects.create(owner=actor,
            title="Migration fixture", width=768, height=1024, fps=24,
            default_shot_duration_ms=6000, continuity_seed=0)
        episode = apps.get_model("studio", "Episode").objects.create(project=project, number=1)
        character = apps.get_model("studio", "CharacterBible").objects.create(project=project,
            slug="voice", name="Voice", stable_speaker_id="S1")
        prop = apps.get_model("studio", "PropBible").objects.create(project=project, slug="key", name="Key")
        Shot = apps.get_model("studio", "Shot")
        shot = Shot.objects.create(episode=episode, number=1, title="  保留标题  ",
            duration_ms=6000, episode_start_ms=1200, episode_end_ms=7200, media_type="video", include_audio=True,
            dialogue=[{"speaker_id": "S1", "text": "  原对白\n", "delivery": " softly ",
                "voiceover": True, "start_ms": 123, "end_ms": 987}],
            metadata={"test_note": "保留", "framing": "历史元数据，不转换"},
            **{field: "  obsolete original text\n" for field in self.retired})
        shot.props.add(prop)
        apps.get_model("studio", "ShotCharacterRelation").objects.create(shot=shot,
            character=character, role="speaker", position=0)
        apps.get_model("studio", "ShotReferenceUse").objects.create(shot=shot,
            use_key="appearance", position=0, media_kind="image", purpose="  可选业务用途  ")
        apps.get_model("studio", "PromptFieldTranslation").objects.create(project=project,
            scope_type="shot", scope_id=shot.pk, field_key="shot.narrative",
            source_text="Historical request source", source_hash="a" * 64,
            translated_text="Historical translation", status="confirmed",
            runtime_metadata={"promotion": {"promoted": True}})
        apps.get_model("studio", "ShotFieldRevision").objects.create(shot=shot, revision=1,
            source_sha256="b" * 64, overrides=[{"isolated_fixture_original": "  独立原值\n"}],
            content_size_bytes=64, content_sha256="c" * 64, request_id=uuid4(),
            request_sha256="d" * 64, created_by=actor)
        self.before = [{key: value for key, value in row.items() if key not in self.retired}
            for row in Shot.objects.order_by("pk").values()]
        self.props = list(Shot.props.through.objects.order_by("pk").values())
        self.preserved = {name: list(apps.get_model("studio", name).objects.order_by("pk").values())
            for name in ("ShotCharacterRelation", "ShotReferenceUse", "ShotFieldRevision",
                "ShotScriptSource", "PromptFieldTranslation", "ApprovalDecision", "GenerationAttempt",
                "CharacterBible", "PropBible", "SeriesProject", "Episode")}

    def test_only_old_columns_are_removed_without_rewriting_retained_facts(self):
        Shot = self.apps.get_model("studio", "Shot")
        self.assertEqual(list(Shot.objects.order_by("pk").values()), self.before)
        self.assertEqual(list(Shot.props.through.objects.order_by("pk").values()), self.props)
        for name, before in self.preserved.items():
            with self.subTest(model=name):
                self.assertEqual(list(self.apps.get_model("studio", name).objects.order_by("pk").values()), before)
        self.assertFalse(self.retired & {field.name for field in Shot._meta.get_fields()})
        with connection.cursor() as cursor:
            columns = connection.introspection.get_table_description(cursor, "studio_shot")
            self.assertFalse(self.retired & {column.name for column in columns})
            if connection.vendor == "sqlite":
                cursor.execute("PRAGMA integrity_check")
                self.assertEqual(cursor.fetchall(), [("ok",)])
        connection.check_constraints()
