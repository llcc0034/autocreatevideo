from studio.tests.test_configuration_migrations import _MigrationTestCase


class ContentSourceStructureMigrationTests(_MigrationTestCase):
    migrate_from = "0203_data_structures"
    migrate_to = "0204_content_source_structure"

    def set_up_before_migration(self, apps):
        self.source_id = apps.get_model("studio", "ContentSourceVersion").objects.create(
            name="Historical source", reader_selector="request.theme", reader_contract={"fixture": "unchanged"},
            contract_sha256="a" * 64, contract_size_bytes=123).pk

    def test_no_guessed_structure_or_rehashed_history(self):
        source = self.apps.get_model("studio", "ContentSourceVersion").objects.get(pk=self.source_id)
        self.assertIsNone(source.data_structure_version_id)
        self.assertEqual(source.reader_contract, {"fixture": "unchanged"})
        self.assertEqual((source.contract_sha256, source.contract_size_bytes), ("a" * 64, 123))
        self.assertFalse(self.apps.get_model("studio", "DataStructureVersion").objects.exists())
