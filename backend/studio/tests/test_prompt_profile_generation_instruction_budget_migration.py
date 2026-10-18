from __future__ import annotations

import hashlib
import importlib

from studio.tests.test_configuration_migrations import _MigrationTestCase


class PromptProfileGenerationInstructionBudgetMigrationTests(_MigrationTestCase):
    migrate_from = "0099_prompt_profile_generation_context_scope"
    migrate_to = "0100_prompt_profile_generation_instruction_budget"

    def set_up_before_migration(self, apps):
        previous_case = importlib.import_module(
            "studio.tests.test_prompt_profile_generation_context_scope_migration",
        ).PromptProfileGenerationContextScopeMigrationTests
        previous_case.set_up_before_migration(self, apps)
        previous = importlib.import_module(
            "studio.migrations.0099_prompt_profile_generation_context_scope",
        )
        previous.publish_shot_scoped_context_successor(apps, None)

        Profile = apps.get_model("studio", "PromptProfileVersion")
        Rule = apps.get_model("studio", "PromptProfileFieldRule")
        Binding = apps.get_model("studio", "PromptProfileBinding")
        source = Profile.objects.get(
            profile_key=previous.PROFILE_KEY,
            version=previous.TARGET_VERSION,
        )
        source_rules = list(
            Rule.objects.filter(profile_version_id=source.id)
            .select_related("field_definition")
            .order_by("position", "id")
        )
        self.source_id = source.id
        self.source_hash = source.contract_sha256
        self.source_instructions = {
            rule.field_definition.key: rule.generation_instruction
            for rule in source_rules
        }
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
                rule.generation_context_selectors,
            )
            for rule in source_rules
        ]
        self.binding_id = Binding.objects.get(
            key=previous.CURRENT_BINDING_KEY,
        ).id

    def test_v17_freezes_historical_contract_and_retires_v16_without_mutating_it(self):
        Profile = self.apps.get_model("studio", "PromptProfileVersion")
        Rule = self.apps.get_model("studio", "PromptProfileFieldRule")
        Binding = self.apps.get_model("studio", "PromptProfileBinding")
        migration = importlib.import_module(
            "studio.migrations.0100_prompt_profile_generation_instruction_budget",
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
                rule.generation_context_selectors,
            )
            for rule in target_rules
        ]

        self.assertEqual(source.status, "retired")
        self.assertEqual(source.contract_sha256, self.source_hash)
        self.assertEqual(
            {
                rule.field_definition.key: rule.generation_instruction
                for rule in Rule.objects.filter(profile_version_id=source.id)
                .select_related("field_definition")
            },
            self.source_instructions,
        )
        self.assertEqual(target.status, "published")
        self.assertEqual(target.review_state, "ready")
        self.assertEqual(target.contract_schema_version, 5)
        self.assertEqual(target_shape, self.source_shape)
        for rule in target_rules:
            self.assertLessEqual(
                len(rule.generation_instruction),
                migration.MAX_GENERATION_INSTRUCTION_CHARS,
            )
            if rule.field_definition.key in migration.MODEL_GENERATED_FIELDS:
                self.assertTrue(
                    rule.generation_instruction.endswith(
                        migration.TARGET_CONTEXT_INSTRUCTION,
                    ),
                )
                self.assertNotIn(
                    migration.SOURCE_CONTEXT_INSTRUCTION,
                    rule.generation_instruction,
                )
                self.assertEqual(
                    rule.generation_context_selectors,
                    migration.TARGET_CONTEXT_SELECTORS,
                )

        encoded = migration._canonical_bytes(
            migration._profile_contract(target, target_rules),
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

        # This is the 0100 schema and its historical data contract, not a
        # current-runtime compatibility promise. Exact historical bytes/hash,
        # rule preservation and idempotence are asserted here; current strategy
        # execution is covered by the common Preparation/compiler tests.
        migration.publish_instruction_budget_successor(self.apps, None)
        self.assertEqual(
            Profile.objects.filter(
                profile_key=migration.PROFILE_KEY,
                version=migration.TARGET_VERSION,
            ).count(),
            1,
        )


    def test_reverse_restores_published_v16_binding_and_removes_v17(self):
        Profile = self.apps.get_model("studio", "PromptProfileVersion")
        Binding = self.apps.get_model("studio", "PromptProfileBinding")
        migration = importlib.import_module(
            "studio.migrations.0100_prompt_profile_generation_instruction_budget",
        )

        migration.restore_v16_binding_and_remove_v17(self.apps, None)

        source = Profile.objects.get(pk=self.source_id)
        binding = Binding.objects.get(pk=self.binding_id)
        self.assertEqual(source.status, "published")
        self.assertEqual(source.contract_sha256, self.source_hash)
        self.assertEqual(binding.prompt_profile_version_id, self.source_id)
        self.assertEqual(binding.name, migration.SOURCE_BINDING_NAME)
        self.assertTrue(binding.is_active)
        self.assertFalse(Profile.objects.filter(
            profile_key=migration.PROFILE_KEY,
            version=migration.TARGET_VERSION,
        ).exists())
