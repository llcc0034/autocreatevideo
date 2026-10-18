from studio.tests.test_configuration_migrations import _MigrationTestCase


class BusinessOutputStructureMigrationTests(_MigrationTestCase):
    migrate_from = "0204_content_source_structure"
    migrate_to = "0205_business_output_structure"

    def set_up_before_migration(self, apps):
        self.output_id = apps.get_model("studio", "BusinessOutputVersion").objects.create(
            name="Historical output", parser_key="visual_rules.v2", parser_contract={"fixture": "unchanged"},
            contract_sha256="a" * 64, contract_size_bytes=123).pk

    def test_no_inferred_structure_or_rehashed_history(self):
        output = self.apps.get_model("studio", "BusinessOutputVersion").objects.get(pk=self.output_id)
        self.assertIsNone(output.data_structure_version_id)
        self.assertEqual(output.parser_contract, {"fixture": "unchanged"})
        self.assertEqual((output.contract_sha256, output.contract_size_bytes), ("a" * 64, 123))
        self.assertFalse(self.apps.get_model("studio", "DataStructureVersion").objects.exists())
