from studio.tests.test_configuration_migrations import _MigrationTestCase


class StructureDefaultsMigrationTests(_MigrationTestCase):
    migrate_from = "0205_business_output_structure"
    migrate_to = "0206_structure_node_defaults"

    def set_up_before_migration(self, apps):
        structure = apps.get_model("studio", "DataStructureVersion").objects.create(name="Existing config")
        self.node_id = apps.get_model("studio", "DataStructureNode").objects.create(structure=structure, node_key="root").pk
        self.output_id = apps.get_model("studio", "BusinessOutputVersion").objects.create(
            name="Historical output", parser_key="visual_rules.v2", parser_contract={"frozen": "unchanged"},
            contract_sha256="b" * 64, contract_size_bytes=123).pk

    def test_defaults_disabled_and_historical_contract_not_rewritten(self):
        node = self.apps.get_model("studio", "DataStructureNode").objects.get(pk=self.node_id)
        self.assertFalse(node.has_default)
        self.assertIsNone(node.default_value)
        output = self.apps.get_model("studio", "BusinessOutputVersion").objects.get(pk=self.output_id)
        self.assertEqual(output.parser_contract, {"frozen": "unchanged"})
        self.assertEqual((output.contract_sha256, output.contract_size_bytes), ("b" * 64, 123))
