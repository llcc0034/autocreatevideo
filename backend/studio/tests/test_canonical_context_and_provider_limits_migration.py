import importlib

from django.db import connection
from django.db.migrations.executor import MigrationExecutor

from studio.tests.test_configuration_migrations import _MigrationTestCase


class CanonicalContextAndProviderLimitsMigrationTests(_MigrationTestCase):
    migrate_from = "0125_configurable_operational_list_and_recovery_limits"
    migrate_to = "0126_configurable_canonical_context_and_provider_limits"

    canonical_context_policy = {
        "schema_version": 1,
        "proper_name_glossary_advisory_entries": 40,
        "visual_identity_glossary_advisory_entries": 80,
        "description_advisory_characters": 800,
        "visual_identity_advisory_characters": 800,
        "glossary_term_advisory_characters": 160,
    }
    defaults = {
        "production.canonical_context_policy": canonical_context_policy,
        "runtime.gpu_probe_timeout_seconds": 5.0,
        "security.provider.async_rest_terminal_status_max_items": 32,
        "security.provider.async_rest_terminal_status_max_characters": 120,
        "security.provider.async_rest_artifact_host_max_items": 32,
    }

    def set_up_before_migration(self, apps):
        Definition = apps.get_model("studio", "ConfigDefinition")
        Revision = apps.get_model("studio", "ConfigRevision")
        Value = apps.get_model("studio", "ConfigValue")
        definition_ids = Definition.objects.filter(
            key__in=self.defaults,
        ).values_list("id", flat=True)
        Value.objects.filter(definition_id__in=definition_ids).delete()
        Definition.objects.filter(key__in=self.defaults).delete()
        active = Revision.objects.get(
            scope_type="system",
            status="published",
            activation_state="active",
        )
        self.previous_revision_id = active.id
        self.previous_values = dict(
            Value.objects.filter(revision=active).values_list(
                "definition__key", "value",
            )
        )

    def test_forward_registers_limits_and_clones_active_revision(self):
        migration = importlib.import_module(
            "studio.migrations.0126_configurable_canonical_context_and_provider_limits",
        )
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
            Value.objects.filter(revision=active).values_list(
                "definition__key", "value",
            )
        )
        for key, value in self.previous_values.items():
            self.assertEqual(values[key], value)
        for key, value in self.defaults.items():
            self.assertEqual(values[key], value)
            definition = Definition.objects.get(key=key)
            self.assertTrue(definition.is_required)
            self.assertEqual(definition.initial_value, value)
            if key in migration.SECURITY_BOUNDARIES:
                expected_schema = migration.SECURITY_BOUNDARIES[key][2]
            else:
                expected_schema = migration.STANDARD_CONFIG[key][4]
            schema_size, schema_sha = migration._contract(expected_schema)
            initial_size, initial_sha = migration._contract(value)
            self.assertEqual(definition.value_schema, expected_schema)
            self.assertEqual(definition.value_schema_size_bytes, schema_size)
            self.assertEqual(definition.value_schema_sha256, schema_sha)
            self.assertEqual(definition.initial_value_size_bytes, initial_size)
            self.assertEqual(definition.initial_value_sha256, initial_sha)

        context_policy = Definition.objects.get(
            key="production.canonical_context_policy",
        )
        self.assertEqual(context_policy.configuration_class, "standard")
        self.assertEqual(context_policy.allowed_scopes, "system,project")
        self.assertFalse(context_policy.is_secret)
        self.assertTrue(context_policy.value_schema["closed_contract"])
        self.assertFalse(context_policy.value_schema["additionalProperties"])

        gpu_timeout = Definition.objects.get(
            key="runtime.gpu_probe_timeout_seconds",
        )
        self.assertEqual(gpu_timeout.configuration_class, "standard")
        self.assertEqual(gpu_timeout.allowed_scopes, "system")
        self.assertEqual(
            gpu_timeout.value_schema["security_ceiling_key"],
            "security.runtime.max_timeout_seconds",
        )

        for key in (
            "security.provider.async_rest_terminal_status_max_items",
            "security.provider.async_rest_terminal_status_max_characters",
            "security.provider.async_rest_artifact_host_max_items",
        ):
            boundary = Definition.objects.get(key=key)
            self.assertEqual(boundary.configuration_class, "security_boundary")
            self.assertEqual(boundary.domain, "security")
            self.assertEqual(boundary.allowed_scopes, "system")
            self.assertFalse(boundary.is_secret)

        self.assertIn(
            "只触发告警",
            Definition.objects.get(key="production.canonical_name_policy").description,
        )


class CanonicalContextAndProviderLimitsConflictTests(_MigrationTestCase):
    migrate_from = "0125_configurable_operational_list_and_recovery_limits"
    migrate_to = "0126_configurable_canonical_context_and_provider_limits"
    migrate_to_in_setup = False

    def set_up_before_migration(self, apps):
        migration = importlib.import_module(
            "studio.migrations.0126_configurable_canonical_context_and_provider_limits",
        )
        Definition = apps.get_model("studio", "ConfigDefinition")
        wider_schema = {"minimum": 0}
        schema_size, schema_sha = migration._contract(wider_schema)
        initial_size, initial_sha = migration._contract(32)
        Definition.objects.update_or_create(
            key="security.provider.async_rest_terminal_status_max_items",
            defaults={
                "domain": "security",
                "configuration_class": "security_boundary",
                "value_type": "integer",
                "unit": "items",
                "allowed_scopes": "system",
                "allowed_default_sources": "",
                "is_required": True,
                "is_secret": False,
                "is_active": True,
                "value_schema": wider_schema,
                "value_schema_version": 1,
                "value_schema_size_bytes": schema_size,
                "value_schema_sha256": schema_sha,
                "initial_value": 32,
                "initial_value_schema_version": 1,
                "initial_value_size_bytes": initial_size,
                "initial_value_sha256": initial_sha,
            },
        )

    def test_existing_boundary_with_wider_schema_fails_closed(self):
        executor = MigrationExecutor(connection)
        with self.assertRaisesMessage(
            RuntimeError,
            "0126 安全边界定义 security.provider.async_rest_terminal_status_max_items 与既有合同冲突",
        ):
            executor.migrate([("studio", self.migrate_to)])
