from importlib import import_module

from django.db import connection

from studio.services.execution_policy_configuration import EXECUTION_POLICY_KEYS, read_execution_policies
from studio.tests.test_configuration_migrations import _MigrationTestCase


class ExecutionPolicyMigrationTests(_MigrationTestCase):
    migrate_from = "0143_execution_preparation_evaluation"
    migrate_to = "0144_execution_policy_configuration"

    def set_up_before_migration(self, apps):
        Definition = apps.get_model("studio", "ConfigDefinition")
        Value = apps.get_model("studio", "ConfigValue")
        Revision = apps.get_model("studio", "ConfigRevision")
        # Only isolated test-fixture seed removal: RunPython reverse is a noop,
        # so descending from the leaf otherwise retains its configuration data.
        Value.objects.filter(definition__key__in=EXECUTION_POLICY_KEYS).delete()
        Definition.objects.filter(key__in=EXECUTION_POLICY_KEYS).delete()
        active = Revision.objects.get(scope_type="system", status="published", activation_state="active")
        self.previous_id = active.pk
        self.previous_values = dict(Value.objects.filter(revision=active).values_list("definition__key", "value"))

    def test_one_revision_registers_policies_and_preserves_other_values(self):
        Revision = self.apps.get_model("studio", "ConfigRevision")
        active = Revision.objects.get(scope_type="system", status="published", activation_state="active")
        self.assertEqual(active.based_on_revision_id, self.previous_id)
        actual = dict(self.apps.get_model("studio", "ConfigValue").objects.filter(revision=active).values_list("definition__key", "value"))
        self.assertEqual({key: actual[key] for key in self.previous_values}, self.previous_values)
        self.assertEqual(len(actual), len(self.previous_values) + 4)
        self.assertEqual(read_execution_policies().configuration.value["revision_ids"], [str(active.pk)])

    def test_repeat_is_idempotent_without_replacing_runtime_policy(self):
        Revision = self.apps.get_model("studio", "ConfigRevision")
        before = Revision.objects.count()
        with connection.schema_editor() as editor:
            import_module("studio.migrations.0144_execution_policy_configuration").register_execution_policies(self.apps, editor)
        self.assertEqual(Revision.objects.count(), before)
