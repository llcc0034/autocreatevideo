"""Only the additive registry schema; not the future P5 production-data purge."""
from django.db import connection
from django.db.migrations.executor import MigrationExecutor

from studio.tests.test_configuration_migrations import _MigrationTestCase


class ExecutionInstanceRegistryMigrationTests(_MigrationTestCase):
    migrate_from = "0140_explicit_reference_rendering_drafts"
    migrate_to = "0141_execution_instance_registry"

    def set_up_before_migration(self, apps):
        Provider = apps.get_model("studio", "ProviderProfile")
        self.provider_id = Provider.objects.create(
            key="preserved-resource-fixture", name="Preserved resource", driver_type="comfyui",
            driver_config={"fixture": "do not change"},
        ).pk
        self.resources_before = {
            name: apps.get_model("studio", name).objects.count()
            for name in ("ProviderProfile", "ModelProfileVersion", "PromptProfileVersion", "WorkflowTemplateVersion", "Asset")
        }

    def test_additive_schema_does_not_convert_or_populate_resources(self):
        for name, count in self.resources_before.items():
            self.assertEqual(self.apps.get_model("studio", name).objects.count(), count, name)
        for name in (
            "ModelExecutionInstance", "ModelExecutionInstanceVersion", "ExecutionInputSpec",
            "ExecutionInputTarget", "ExecutionParameterSpec", "ExecutionAuxiliaryBinding", "ExecutionVersionContract",
        ):
            self.assertEqual(self.apps.get_model("studio", name).objects.count(), 0, name)
        resource = self.apps.get_model("studio", "ProviderProfile").objects.get(pk=self.provider_id)
        self.assertEqual(resource.driver_config, {"fixture": "do not change"})

    def test_empty_registry_schema_can_round_trip_without_touching_resources(self):
        MigrationExecutor(connection).migrate([("studio", self.migrate_from)])
        self.assertNotIn("studio_modelexecutioninstance", connection.introspection.table_names())
        MigrationExecutor(connection).migrate([("studio", self.migrate_to)])
        self.assertIn("studio_modelexecutioninstance", connection.introspection.table_names())
        self.assertEqual(self.apps.get_model("studio", "ProviderProfile").objects.get(pk=self.provider_id).name, "Preserved resource")
