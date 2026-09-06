from __future__ import annotations

from studio.tests.test_configuration_migrations import _MigrationTestCase


class RuntimePolicyConfigurationMigrationTests(_MigrationTestCase):
    migrate_from = "0115_media_processing_configuration"
    migrate_to = "0116_runtime_policy_configuration"

    boundary_defaults = {
        "security.dialogue.max_entries": 64,
        "security.dialogue.speaker_id_max_characters": 16,
        "security.dialogue.text_max_characters": 2_000,
        "security.dialogue.delivery_max_characters": 500,
    }
    scalar_defaults = {
        "runtime.host_probe_heartbeat_seconds": 2.0,
        "runtime.host_probe_lease_seconds": 300,
        "runtime.host_probe_stale_seconds": 300,
        "runtime.host_probe_refresh_buffer_seconds": 120,
        "runtime.platform_maintenance_poll_seconds": 60.0,
        "runtime.platform_maintenance_lease_seconds": 180,
        "runtime.readiness_worker_max_age_seconds": 30,
        "runtime.readiness_min_disk_free_bytes": 536_870_912,
    }
    object_defaults = {
        "production.series_concept_resource_advisory": {
            "schema_version": 1,
            "input_protocol_overhead_chars": 2_048,
            "output_base_units": 512,
            "output_units_per_shot": 200,
            "minimum_output_units": 1_024,
            "output_hint_warning_percent": 80,
            "script_breakdown_reference_chars": 1_000_000,
        },
        "production.script_prop_alias_policy": {
            "schema_version": 1,
            "minimum_core_characters": 2,
            "maximum_prefix_trim_characters": 2,
            "generic_aliases": ["东西", "物件", "物品", "设备", "工具", "道具"],
        },
    }

    def set_up_before_migration(self, apps):
        Definition = apps.get_model("studio", "ConfigDefinition")
        Revision = apps.get_model("studio", "ConfigRevision")
        Value = apps.get_model("studio", "ConfigValue")
        keys = (
            set(self.boundary_defaults)
            | set(self.scalar_defaults)
            | set(self.object_defaults)
        )
        definition_ids = Definition.objects.filter(key__in=keys).values_list("id", flat=True)
        Value.objects.filter(definition_id__in=definition_ids).delete()
        Definition.objects.filter(key__in=keys).delete()
        active = Revision.objects.get(
            scope_type="system",
            status="published",
            activation_state="active",
        )
        self.previous_revision_id = active.id
        self.previous_values = dict(
            Value.objects.filter(revision=active).values_list("definition__key", "value")
        )

    def test_forward_registers_runtime_policies_and_clones_active_revision(self):
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

        for key, value in self.boundary_defaults.items():
            definition = Definition.objects.get(key=key)
            self.assertEqual(values[key], value)
            self.assertEqual(definition.domain, "security")
            self.assertEqual(definition.configuration_class, "security_boundary")
            self.assertEqual(definition.allowed_scopes, "system")
            self.assertTrue(definition.is_required)

        for key, value in self.scalar_defaults.items():
            definition = Definition.objects.get(key=key)
            self.assertEqual(values[key], value)
            self.assertEqual(definition.configuration_class, "standard")
            self.assertEqual(definition.allowed_scopes, "system")
            self.assertTrue(definition.is_required)

        for key, value in self.object_defaults.items():
            definition = Definition.objects.get(key=key)
            self.assertEqual(values[key], value)
            self.assertEqual(definition.configuration_class, "standard")
            self.assertEqual(definition.allowed_scopes, "system,project")
            self.assertEqual(definition.value_type, "object")
            self.assertTrue(definition.value_schema["closed_contract"])
            self.assertFalse(definition.value_schema["additionalProperties"])
            self.assertEqual(
                set(definition.value_schema["required"]),
                set(value),
            )

        advisory_properties = Definition.objects.get(
            key="production.series_concept_resource_advisory",
        ).value_schema["properties"]
        self.assertFalse(any("token" in key.lower() for key in advisory_properties))
