"""P2 storage migration is additive; P5 is the only business-data cutover."""
from django.db import connection
from django.db.migrations.executor import MigrationExecutor

from studio.tests.test_configuration_migrations import _MigrationTestCase


class ExecutionPreparationMigrationTests(_MigrationTestCase):
    migrate_from = "0141_execution_instance_registry"
    migrate_to = "0142_execution_preparation_records"

    def set_up_before_migration(self, apps):
        self.instance_id = apps.get_model("studio", "ModelExecutionInstance").objects.create(
            key="preserved-registry", name="Unchanged configuration", description="Not execution evidence",
        ).pk
        self.counts = {
            name: apps.get_model("studio", name).objects.count() for name in (
                "ModelExecutionInstance", "ModelExecutionInstanceVersion", "AssetGenerationJob",
                "GenerationAttempt", "ModelDeploymentSmokeAttempt", "WorkflowSmokeAttempt",
            )
        }

    def test_no_legacy_task_is_converted_to_a_preparation(self):
        for name, count in self.counts.items():
            self.assertEqual(self.apps.get_model("studio", name).objects.count(), count, name)
        for name in ("ExecutionPreparation", "ExecutionPreparationContract", "ExecutionFieldValue", "ExecutionFieldSelection"):
            self.assertEqual(self.apps.get_model("studio", name).objects.count(), 0, name)
        instance = self.apps.get_model("studio", "ModelExecutionInstance").objects.get(pk=self.instance_id)
        self.assertEqual(instance.description, "Not execution evidence")

    def test_empty_preparation_schema_roundtrip_preserves_registry(self):
        MigrationExecutor(connection).migrate([("studio", self.migrate_from)])
        self.assertNotIn("studio_executionpreparation", connection.introspection.table_names())
        MigrationExecutor(connection).migrate([("studio", self.migrate_to)])
        self.assertIn("studio_executionpreparation", connection.introspection.table_names())
        self.assertEqual(self.apps.get_model("studio", "ModelExecutionInstance").objects.get(pk=self.instance_id).name, "Unchanged configuration")
