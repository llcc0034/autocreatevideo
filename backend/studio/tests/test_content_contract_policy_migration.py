from __future__ import annotations

from studio.tests.test_configuration_migrations import _MigrationTestCase


class ContentContractPolicyMigrationTests(_MigrationTestCase):
    migrate_from = "0118_runtime_state_configuration"
    migrate_to = "0119_content_contract_policy"

    defaults = {
        "security.translation.max_entries_per_request": 100,
        "security.translation.max_promotions_per_request": 20,
        "security.canonical_glossary.max_entities_per_request": 100,
        "security.canonical_glossary.max_runtime_items_per_request": 100,
        "security.audit_note.minimum_characters": 8,
        "security.audit_note.maximum_characters": 4_000,
        "security.contract_complexity.canonical_name_max_characters": 80,
        "security.contract_complexity.canonical_name_max_words": 8,
        "security.contract_complexity.prompt_profile_operation_max_characters": 100,
        "security.contract_complexity.prompt_profile.max_validators": 8,
        "security.contract_complexity.prompt_profile.max_context_selectors": 16,
        "security.contract_complexity.prompt_profile.field_list_max_items": 100,
        "security.contract_complexity.prompt_profile.assignment_separator_max_characters": 32,
        "security.contract_complexity.prompt_profile.transition_patterns_max_items": 16,
        "security.contract_complexity.prompt_profile.value_options_max_items": 100,
        "security.contract_complexity.prompt_profile.value_option_max_characters": 500,
        "security.contract_complexity.media_contract_max_rules": 16,
        "security.contract_complexity.media_contract_max_modes": 32,
        "security.contract_complexity.media_contract_text_max_characters": 500,
    }

    def set_up_before_migration(self, apps):
        Definition = apps.get_model("studio", "ConfigDefinition")
        Revision = apps.get_model("studio", "ConfigRevision")
        Value = apps.get_model("studio", "ConfigValue")
        target_keys = {*self.defaults, "production.canonical_name_policy"}
        definition_ids = Definition.objects.filter(key__in=target_keys).values_list("id", flat=True)
        Value.objects.filter(definition_id__in=definition_ids).delete()
        Definition.objects.filter(key__in=target_keys).delete()
        active = Revision.objects.get(
            scope_type="system", status="published", activation_state="active",
        )
        self.previous_revision_id = active.id
        self.previous_values = dict(
            Value.objects.filter(revision=active).values_list("definition__key", "value"),
        )

    def test_forward_clones_active_revision_and_registers_content_contracts(self):
        Definition = self.apps.get_model("studio", "ConfigDefinition")
        Revision = self.apps.get_model("studio", "ConfigRevision")
        Value = self.apps.get_model("studio", "ConfigValue")
        active = Revision.objects.get(
            scope_type="system", status="published", activation_state="active",
        )
        self.assertNotEqual(active.id, self.previous_revision_id)
        values = dict(Value.objects.filter(revision=active).values_list("definition__key", "value"))
        for key, value in self.previous_values.items():
            self.assertEqual(values[key], value)
        for key, value in self.defaults.items():
            self.assertEqual(values[key], value)
            definition = Definition.objects.get(key=key)
            self.assertEqual(definition.configuration_class, "security_boundary")
            self.assertEqual(definition.allowed_scopes, "system")

        policy = Definition.objects.get(key="production.canonical_name_policy")
        self.assertEqual(policy.allowed_scopes, "system,project")
        self.assertEqual(values[policy.key]["max_characters"], 80)
        self.assertEqual(values[policy.key]["max_words"], 8)
        self.assertEqual(
            policy.value_schema["security_ceiling_paths"]["max_characters"],
            "security.contract_complexity.canonical_name_max_characters",
        )
