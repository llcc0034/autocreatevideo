from __future__ import annotations

from studio.tests.test_configuration_migrations import _MigrationTestCase


class RuntimeStateConfigurationMigrationTests(_MigrationTestCase):
    migrate_from = "0117_explicit_provider_execution_contracts"
    migrate_to = "0118_runtime_state_configuration"

    defaults = {
        "security.delivery.public_distribution_enabled": True,
        "runtime.readiness_generation_worker_required": True,
        "runtime.readiness_text_generation_worker_required": True,
        "runtime.readiness_assembly_worker_required": True,
        "runtime.distribution_lock_stale_seconds": 180,
    }

    def set_up_before_migration(self, apps):
        Definition = apps.get_model("studio", "ConfigDefinition")
        Revision = apps.get_model("studio", "ConfigRevision")
        Value = apps.get_model("studio", "ConfigValue")
        definition_ids = Definition.objects.filter(key__in=self.defaults).values_list(
            "id", flat=True,
        )
        Value.objects.filter(definition_id__in=definition_ids).delete()
        Definition.objects.filter(key__in=self.defaults).delete()
        active = Revision.objects.get(
            scope_type="system",
            status="published",
            activation_state="active",
        )
        self.previous_revision_id = active.id
        self.previous_values = dict(
            Value.objects.filter(revision=active).values_list("definition__key", "value")
        )

    def test_forward_registers_runtime_state_and_clones_active_revision(self):
        Definition = self.apps.get_model("studio", "ConfigDefinition")
        Revision = self.apps.get_model("studio", "ConfigRevision")
        Value = self.apps.get_model("studio", "ConfigValue")
        active = Revision.objects.get(
            scope_type="system",
            status="published",
            activation_state="active",
        )
        self.assertNotEqual(active.id, self.previous_revision_id)
        self.assertEqual(active.based_on_revision_id, self.previous_revision_id)
        values = dict(
            Value.objects.filter(revision=active).values_list("definition__key", "value")
        )
        for key, value in self.previous_values.items():
            self.assertEqual(values[key], value)
        for key, value in self.defaults.items():
            self.assertEqual(values[key], value)
            definition = Definition.objects.get(key=key)
            self.assertEqual(definition.allowed_scopes, "system")
            self.assertTrue(definition.is_required)

        gate = Definition.objects.get(
            key="security.delivery.public_distribution_enabled",
        )
        self.assertEqual(gate.domain, "security")
        self.assertEqual(gate.configuration_class, "security_boundary")
        self.assertEqual(gate.value_type, "boolean")

        lock = Definition.objects.get(key="runtime.distribution_lock_stale_seconds")
        self.assertEqual(lock.configuration_class, "standard")
        self.assertEqual(lock.value_type, "integer")
        self.assertEqual(
            lock.value_schema["security_ceiling_key"],
            "security.runtime.max_timeout_seconds",
        )
