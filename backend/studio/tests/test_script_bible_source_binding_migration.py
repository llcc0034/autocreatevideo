"""0198 schema transition in an isolated database; no historical inference."""
from hashlib import sha256

from django.db import connection
from django.db.migrations.executor import MigrationExecutor

from studio.services.json_contracts import canonical_json_bytes
from studio.tests.test_configuration_migrations import _MigrationTestCase


class ScriptBibleSourceBindingMigrationTests(_MigrationTestCase):
    migrate_from = "0197_bible_variant_settings"
    migrate_to = "0198_script_bible_source_bindings"

    def set_up_before_migration(self, apps):
        actor = apps.get_model("auth", "User").objects.create(username="source-binding-schema")
        project = apps.get_model("studio", "SeriesProject").objects.create(owner=actor, title="Schema fixture",
            width=768, height=1024, fps=24, default_shot_duration_ms=1000, continuity_seed=1)
        entity = apps.get_model("studio", "CharacterBible").objects.create(project=project,
            slug="schema-entity", name="旧人物", stable_speaker_id="S1")
        variant = apps.get_model("studio", "BibleVariant").objects.create(project=project, character=entity,
            name="旧设定", reference_name="旧设定", reference_key="旧设定", created_by=actor)
        self.value = dict(name="旧设定", reference_name="旧设定", identity_anchor="旧身份",
            appearance_anchor="旧外观", basis_asset_id=None, basis_asset_sha256="")
        self.digest = sha256(canonical_json_bytes(self.value)).hexdigest()
        setting = apps.get_model("studio", "BibleVariantRevision").objects.create(variant=variant, revision=1,
            **self.value, content_sha256=self.digest, created_by=actor)
        review = apps.get_model("studio", "BibleVariantReview").objects.create(setting=setting,
            decision="approve", comment="历史事实", decided_by=actor)
        self.variant_id, self.setting_id, self.review_id = variant.pk, setting.pk, review.pk

    def assert_history_unchanged(self):
        variant = self.apps.get_model("studio", "BibleVariant").objects.get(pk=self.variant_id)
        setting = self.apps.get_model("studio", "BibleVariantRevision").objects.get(pk=self.setting_id)
        review = self.apps.get_model("studio", "BibleVariantReview").objects.get(pk=self.review_id)
        self.assertEqual(variant.appearance_role, "variant")
        self.assertIsNone(variant.source_binding_id)
        self.assertEqual(variant.source_variant_key, "")
        self.assertEqual(setting.payload_schema_version, 1)
        self.assertEqual(setting.content_sha256, self.digest)
        self.assertEqual(setting.source_declaration_sha256, "")
        self.assertIsNone(setting.primary_setting_revision_id)
        self.assertEqual({key: getattr(setting, key) for key in self.value}, self.value)
        self.assertEqual((review.decision, review.comment), ("approve", "历史事实"))
        self.assertFalse(self.apps.get_model("studio", "ScriptBibleEntityBinding").objects.exists())

    def test_forward_preserves_history_without_fabricating_primary_or_source(self):
        self.assert_history_unchanged()
        from studio.models import BibleVariantRevision
        from studio.services.bible_variants import BibleVariantConflict, verify_setting
        # Preserving original migration facts does not support the retired
        # runtime contract or convert its body-only hash into current evidence.
        with self.assertRaisesMessage(BibleVariantConflict, "设定摘要版本已停用"):
            verify_setting(BibleVariantRevision.objects.get(pk=self.setting_id))

    def test_additive_schema_roundtrip_preserves_original_ids_and_digests(self):
        MigrationExecutor(connection).migrate([("studio", self.migrate_from)])
        MigrationExecutor(connection).migrate([("studio", self.migrate_to)])
        self.assert_history_unchanged()
