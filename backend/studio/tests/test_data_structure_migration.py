from django.db import IntegrityError, transaction

from studio.tests.test_configuration_migrations import _MigrationTestCase


class DataStructureMigrationTests(_MigrationTestCase):
    migrate_from = "0202_field_identity_only"
    migrate_to = "0203_data_structures"

    def set_up_before_migration(self, apps):
        identity = apps.get_model("studio", "FieldDefinition").objects.create(key="structure_migration_field")
        self.definition_id = apps.get_model("studio", "FieldDefinitionVersion").objects.create(
            field_definition=identity, version=7, label="Existing meaning", category="test", value_type="object", status="published").pk
        self.source_id = apps.get_model("studio", "ContentSourceVersion").objects.create(
            name="Unchanged reader", reader_selector="fixture", reader_contract={"fixture": True},
            contract_sha256="a" * 64, contract_size_bytes=123).pk

    def test_new_structure_tables_keep_existing_definitions_and_contracts_unchanged(self):
        Structure = self.apps.get_model("studio", "DataStructureVersion")
        Node = self.apps.get_model("studio", "DataStructureNode")
        self.assertEqual(Structure.objects.count(), 0)
        self.assertEqual(Node.objects.count(), 0)
        field = self.apps.get_model("studio", "FieldDefinitionVersion").objects.get(pk=self.definition_id)
        self.assertEqual((field.version, field.label), (7, "Existing meaning"))
        source = self.apps.get_model("studio", "ContentSourceVersion").objects.get(pk=self.source_id)
        self.assertEqual((source.reader_contract, source.contract_sha256, source.contract_size_bytes), ({"fixture": True}, "a" * 64, 123))
        version = Structure.objects.create(name="Isolated structure")
        Node.objects.create(structure=version, node_key="root", field_definition_version=field)
        with self.assertRaises(IntegrityError), transaction.atomic():
            Node.objects.create(structure=version, node_key="root", field_definition_version=field)
        with self.assertRaises(IntegrityError), transaction.atomic():
            Structure.objects.create(structure_key=version.structure_key, version=version.version)
