from __future__ import annotations

from studio.tests.test_configuration_migrations import _MigrationTestCase


class MediaProcessingConfigurationMigrationTests(_MigrationTestCase):
    migrate_from = "0114_api_effective_limit_contracts"
    migrate_to = "0115_media_processing_configuration"

    boundary_defaults = {
        "security.delivery.disclosure_text_max_characters": 400,
        "security.media.duration_verification_tolerance_max_ms": 1_000,
        "security.media.audio_bitrate_max_kbps": 2_048,
        "security.media.audio_sample_rate_max_hz": 384_000,
        "security.media.processing_argument_max_characters": 80,
    }
    profile_keys = {
        "delivery.assembly_media_profile",
        "delivery.distribution_media_profile",
        "reference.media_preprocessing_profile",
    }
    standard_defaults = {"artifact.media_probe_timeout_seconds": 30}

    def set_up_before_migration(self, apps):
        Definition = apps.get_model("studio", "ConfigDefinition")
        Revision = apps.get_model("studio", "ConfigRevision")
        Value = apps.get_model("studio", "ConfigValue")
        keys = set(self.boundary_defaults) | self.profile_keys | set(self.standard_defaults)
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

    def test_forward_registers_profiles_boundaries_and_new_active_revision(self):
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
            self.assertEqual(values[key], value)
            definition = Definition.objects.get(key=key)
            self.assertEqual(definition.configuration_class, "security_boundary")
            self.assertEqual(definition.domain, "security")
            self.assertEqual(definition.allowed_scopes, "system")
            self.assertTrue(definition.is_required)

        for key in self.profile_keys:
            definition = Definition.objects.get(key=key)
            self.assertEqual(definition.configuration_class, "standard")
            self.assertEqual(definition.value_type, "object")
            self.assertEqual(definition.allowed_scopes, "system,project")
            self.assertTrue(definition.is_required)
            self.assertTrue(definition.value_schema["closed_contract"])
            self.assertTrue(definition.value_schema["security_ceiling_paths"])
            self.assertEqual(values[key]["schema_version"], 1)
        for key, value in self.standard_defaults.items():
            definition = Definition.objects.get(key=key)
            self.assertEqual(values[key], value)
            self.assertEqual(definition.configuration_class, "standard")
            self.assertEqual(definition.allowed_scopes, "system")
            self.assertEqual(
                definition.value_schema["security_ceiling_key"],
                "security.runtime.max_timeout_seconds",
            )
