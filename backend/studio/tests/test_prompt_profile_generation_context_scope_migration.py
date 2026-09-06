from __future__ import annotations

import hashlib
import importlib

from studio.tests.test_configuration_migrations import _MigrationTestCase


class PromptProfileGenerationContextScopeMigrationTests(_MigrationTestCase):
    migrate_from = "0098_static_reference_initial_clause_policy"
    migrate_to = "0099_prompt_profile_generation_context_scope"

    def set_up_before_migration(self, apps):
        previous_case = importlib.import_module(
            "studio.tests.test_prompt_profile_official_reference_layout_migration",
        ).PromptProfileOfficialReferenceLayoutMigrationTests
        previous_case.set_up_before_migration(self, apps)
        previous = importlib.import_module(
            "studio.migrations.0093_prompt_profile_official_reference_layout",
        )
        previous.publish_official_reference_layout_successor(apps, None)

        Profile = apps.get_model("studio", "PromptProfileVersion")
        Rule = apps.get_model("studio", "PromptProfileFieldRule")
        Binding = apps.get_model("studio", "PromptProfileBinding")
        source = Profile.objects.get(
            profile_key=previous.PROFILE_KEY,
            version=previous.TARGET_VERSION,
        )
        self.source_id = source.id
        self.source_hash = source.contract_sha256
        self.source_shape = [
            (
                rule.field_definition.key,
                rule.source_selector,
                rule.channel,
                rule.position,
                rule.label_style,
                rule.separator,
                rule.generation_mode,
                rule.generation_output_semantics,
            )
            for rule in Rule.objects.filter(profile_version_id=source.id)
            .select_related("field_definition")
            .order_by("position", "id")
        ]
        self.binding_id = Binding.objects.get(key=previous.CURRENT_BINDING_KEY).id

    def test_v16_scopes_each_generated_field_and_changes_the_frozen_hash(self):
        Profile = self.apps.get_model("studio", "PromptProfileVersion")
        Rule = self.apps.get_model("studio", "PromptProfileFieldRule")
        Binding = self.apps.get_model("studio", "PromptProfileBinding")
        migration = importlib.import_module(
            "studio.migrations.0099_prompt_profile_generation_context_scope",
        )

        source = Profile.objects.get(pk=self.source_id)
        target = Profile.objects.get(
            profile_key=migration.PROFILE_KEY,
            version=migration.TARGET_VERSION,
        )
        target_rules = list(
            Rule.objects.filter(profile_version_id=target.id)
            .select_related("field_definition")
            .order_by("position", "id")
        )
        target_shape = [
            (
                rule.field_definition.key,
                rule.source_selector,
                rule.channel,
                rule.position,
                rule.label_style,
                rule.separator,
                rule.generation_mode,
                rule.generation_output_semantics,
            )
            for rule in target_rules
        ]

        self.assertEqual(source.contract_sha256, self.source_hash)
        self.assertEqual(source.contract_schema_version, 4)
        self.assertEqual(target.contract_schema_version, 5)
        self.assertEqual(target_shape, self.source_shape)
        self.assertEqual(
            [rule.field_definition.key for rule in target_rules],
            migration.EXPECTED_FIELDS,
        )
        for rule in target_rules:
            if rule.field_definition.key in migration.MODEL_GENERATED_FIELDS:
                self.assertEqual(
                    rule.generation_context_selectors,
                    migration.TARGET_CONTEXT_SELECTORS,
                )
                self.assertIn(
                    "never use another shot's dialogue",
                    rule.generation_instruction,
                )
                self.assertIn(
                    "assembled separately by the deterministic server renderer",
                    rule.generation_instruction,
                )
            else:
                self.assertEqual(rule.generation_context_selectors, [])

        encoded = migration._canonical_bytes(
            migration._profile_contract(target, target_rules, schema_version=5),
        )
        self.assertEqual(target.contract_size_bytes, len(encoded))
        self.assertEqual(
            target.contract_sha256,
            hashlib.sha256(encoded).hexdigest(),
        )
        self.assertNotEqual(target.contract_sha256, source.contract_sha256)
        binding = Binding.objects.get(pk=self.binding_id)
        self.assertEqual(binding.prompt_profile_version_id, target.id)
        self.assertEqual(binding.name, migration.TARGET_BINDING_NAME)
        self.assertTrue(binding.is_active)

        migration.publish_shot_scoped_context_successor(self.apps, None)
        self.assertEqual(
            Profile.objects.filter(
                profile_key=migration.PROFILE_KEY,
                version=migration.TARGET_VERSION,
            ).count(),
            1,
        )

    def test_reverse_restores_v15_binding_and_removes_v16(self):
        Profile = self.apps.get_model("studio", "PromptProfileVersion")
        Binding = self.apps.get_model("studio", "PromptProfileBinding")
        migration = importlib.import_module(
            "studio.migrations.0099_prompt_profile_generation_context_scope",
        )

        migration.restore_v15_binding_and_remove_v16(self.apps, None)

        binding = Binding.objects.get(pk=self.binding_id)
        self.assertEqual(binding.prompt_profile_version_id, self.source_id)
        self.assertEqual(binding.name, migration.SOURCE_BINDING_NAME)
        self.assertFalse(Profile.objects.filter(
            profile_key=migration.PROFILE_KEY,
            version=migration.TARGET_VERSION,
        ).exists())
