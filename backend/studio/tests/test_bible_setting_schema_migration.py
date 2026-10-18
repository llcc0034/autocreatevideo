"""0218 changes only the new-instance default in an isolated database."""
from django.db import connection
from django.db.migrations.executor import MigrationExecutor

from studio.tests.test_configuration_migrations import _MigrationTestCase
from studio.tests import test_script_bible_source_binding_migration as fixtures


class BibleSettingSchemaDefaultMigrationTests(_MigrationTestCase):
    migrate_from = "0217_remove_bible_visual_identity"
    migrate_to = "0218_bible_setting_schema_default"
    set_up_before_migration = fixtures.ScriptBibleSourceBindingMigrationTests.set_up_before_migration

    def assert_original_unchanged(self):
        setting = self.apps.get_model("studio", "BibleVariantRevision").objects.get(pk=self.setting_id)
        self.assertEqual(setting.payload_schema_version, 1)
        self.assertEqual(setting.content_sha256, self.digest)
        self.assertEqual({key: getattr(setting, key) for key in self.value}, self.value)
        self.assertIsNone(setting.primary_setting_revision_id)
        review = self.apps.get_model("studio", "BibleVariantReview").objects.get(pk=self.review_id)
        self.assertEqual((review.setting_id, review.decision, review.comment), (self.setting_id, "approve", "历史事实"))
        self.assertFalse(self.apps.get_model("studio", "ScriptBibleEntityBinding").objects.exists())
        self.assertEqual(self.apps.get_model("studio", "BibleVariantRevision")().payload_schema_version, 2)
        connection.check_constraints()

    def test_new_default_preserves_old_body_digest_and_review_without_conversion(self):
        self.assert_original_unchanged()

    def test_schema_roundtrip_does_not_relabel_or_remove_existing_facts(self):
        MigrationExecutor(connection).migrate([("studio", self.migrate_from)])
        MigrationExecutor(connection).migrate([("studio", self.migrate_to)])
        self.assert_original_unchanged()
