from __future__ import annotations

import hashlib
import importlib

from django.db import connection
from django.db.migrations.executor import MigrationExecutor

from studio.tests.test_configuration_migrations import _MigrationTestCase


class PromptProfileTwelveSentenceScaffoldMigrationTests(_MigrationTestCase):
    migrate_from = "0086_prompt_profile_coverage_pass_budget"
    migrate_to = "0087_prompt_profile_twelve_sentence_scaffold"

    def set_up_before_migration(self, apps):
        previous_test_case = importlib.import_module(
            "studio.tests.test_prompt_profile_coverage_pass_budget_migration",
        ).PromptProfileCoveragePassBudgetMigrationTests
        previous_test_case.set_up_before_migration(self, apps)
        previous = importlib.import_module(
            "studio.migrations.0086_prompt_profile_coverage_pass_budget",
        )
        previous.publish_coverage_pass_budget_successor(apps, None)

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
        binding = Binding.objects.get(key=previous.CURRENT_BINDING_KEY)

        self.source_id = source.id
        self.binding_id = binding.id
        self.source_contract = (
            source.contract_size_bytes,
            source.contract_sha256,
            source.status,
        )
        self.source_instructions = {
            rule.field_definition.key: rule.generation_instruction
            for rule in source_rules
        }

    def test_v13_replaces_private_passes_with_twelve_playback_order_sentences(self):
        Profile = self.apps.get_model("studio", "PromptProfileVersion")
        Rule = self.apps.get_model("studio", "PromptProfileFieldRule")
        Binding = self.apps.get_model("studio", "PromptProfileBinding")
        migration = importlib.import_module(
            "studio.migrations.0087_prompt_profile_twelve_sentence_scaffold",
        )

        source = Profile.objects.get(pk=self.source_id)
        target = Profile.objects.get(
            profile_key=migration.PROFILE_KEY,
            version=migration.TARGET_VERSION,
        )
        self.assertEqual(
            (source.contract_size_bytes, source.contract_sha256, source.status),
            self.source_contract,
        )
        source_rules = list(
            Rule.objects.filter(profile_version_id=source.id)
            .select_related("field_definition")
            .order_by("position", "id")
        )
        target_rules = list(
            Rule.objects.filter(profile_version_id=target.id)
            .select_related("field_definition")
            .order_by("position", "id")
        )
        self.assertEqual(len(source_rules), 6)
        self.assertEqual(len(target_rules), 6)
        self.assertEqual(
            [rule.field_definition.key for rule in target_rules],
            [
                "subject_definitions",
                "summary",
                "retention_analysis",
                "detailed_description",
                "overall_soundscape",
                "non_diegetic_music",
            ],
        )

        for source_rule, target_rule in zip(
            source_rules,
            target_rules,
            strict=True,
        ):
            key = source_rule.field_definition.key
            self.assertEqual(
                source_rule.generation_instruction,
                self.source_instructions[key],
            )
            if key != migration.DETAIL_FIELD_KEY:
                self.assertEqual(
                    target_rule.generation_instruction,
                    source_rule.generation_instruction,
                )
                continue

            instruction = target_rule.generation_instruction
            self.assertEqual(
                instruction,
                migration.DETAIL_GENERATION_INSTRUCTION,
            )
            self.assertEqual(target_rule.length_unit, "english_words")
            self.assertEqual(target_rule.generation_min, 350)
            self.assertEqual(target_rule.generation_max, 500)
            self.assertIn("exactly twelve complete sentences", instruction)
            self.assertIn("each targeting 30-36 English words", instruction)
            self.assertIn("target total of 360-432 English words", instruction)
            self.assertIn("continuous playback-order English director-prose", instruction)
            self.assertIn("Do not output sentence numbers", instruction)
            self.assertIn(
                "order of the supplied shot and timeline facts",
                instruction,
            )
            self.assertIn(
                "continue this allocation across them in their supplied playback order",
                instruction,
            )
            self.assertIn(
                "Preserve every server-provided protected_tokens placeholder",
                instruction,
            )
            self.assertIn("authorized elaborations, not new story facts", instruction)
            self.assertIn("Do not add an unsupported identity", instruction)
            self.assertIn(
                "deterministic server projection appends",
                instruction,
            )
            self.assertNotIn("Privately draft all seven coverage passes", instruction)
            for old_budget in (
                "45-55 words",
                "55-65",
                "80-100",
                "50-60",
                "40-60",
            ):
                self.assertNotIn(old_budget, instruction)
            for raw_tag in (
                "<d>",
                "</d>",
                "<lyrics>",
                "</lyrics>",
                "<screen_text>",
                "</screen_text>",
            ):
                self.assertNotIn(raw_tag, instruction)

        encoded = migration._canonical_bytes(
            migration._profile_contract(target, target_rules),
        )
        self.assertEqual(target.contract_size_bytes, len(encoded))
        self.assertEqual(
            target.contract_sha256,
            hashlib.sha256(encoded).hexdigest(),
        )
        binding = Binding.objects.get(pk=self.binding_id)
        self.assertEqual(binding.prompt_profile_version_id, target.id)
        self.assertEqual(binding.name, migration.TARGET_BINDING_NAME)
        self.assertTrue(binding.is_active)

        migration.publish_twelve_sentence_scaffold_successor(self.apps, None)
        self.assertEqual(
            Profile.objects.filter(
                profile_key=migration.PROFILE_KEY,
                version=migration.TARGET_VERSION,
            ).count(),
            1,
        )
        self.assertEqual(
            Binding.objects.get(pk=self.binding_id).prompt_profile_version_id,
            target.id,
        )

        executor = MigrationExecutor(connection)
        executor.migrate([("studio", self.migrate_from)])
        reverse_apps = executor.loader.project_state([
            ("studio", self.migrate_from),
        ]).apps
        Profile = reverse_apps.get_model("studio", "PromptProfileVersion")
        Binding = reverse_apps.get_model("studio", "PromptProfileBinding")
        source = Profile.objects.get(pk=self.source_id)
        target = Profile.objects.get(
            profile_key=migration.PROFILE_KEY,
            version=migration.TARGET_VERSION,
        )
        binding = Binding.objects.get(pk=self.binding_id)
        self.assertEqual(binding.prompt_profile_version_id, source.id)
        self.assertEqual(binding.name, migration.SOURCE_BINDING_NAME)
        self.assertTrue(binding.is_active)
        self.assertEqual(target.status, "published")

        executor = MigrationExecutor(connection)
        executor.migrate([("studio", self.migrate_to)])
        forward_apps = executor.loader.project_state([
            ("studio", self.migrate_to),
        ]).apps
        Profile = forward_apps.get_model("studio", "PromptProfileVersion")
        Binding = forward_apps.get_model("studio", "PromptProfileBinding")
        target = Profile.objects.get(
            profile_key=migration.PROFILE_KEY,
            version=migration.TARGET_VERSION,
        )
        binding = Binding.objects.get(pk=self.binding_id)
        self.assertEqual(binding.prompt_profile_version_id, target.id)
        self.assertEqual(binding.name, migration.TARGET_BINDING_NAME)
        self.assertEqual(
            Profile.objects.filter(
                profile_key=migration.PROFILE_KEY,
                version=migration.TARGET_VERSION,
            ).count(),
            1,
        )
