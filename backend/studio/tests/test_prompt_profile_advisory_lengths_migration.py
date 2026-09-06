from __future__ import annotations

import hashlib
import importlib

from studio.tests.test_configuration_migrations import _MigrationTestCase


class PromptProfileAdvisoryLengthsMigrationTests(_MigrationTestCase):
    migrate_from = "0089_prompt_normalization_reasoning_budget"
    migrate_to = "0090_prompt_profile_advisory_lengths"

    def set_up_before_migration(self, apps):
        previous_case = importlib.import_module(
            "studio.tests.test_prompt_profile_twelve_sentence_scaffold_migration",
        ).PromptProfileTwelveSentenceScaffoldMigrationTests
        previous_case.set_up_before_migration(self, apps)
        previous = importlib.import_module(
            "studio.migrations.0087_prompt_profile_twelve_sentence_scaffold",
        )
        previous.publish_twelve_sentence_scaffold_successor(apps, None)

        Profile = apps.get_model("studio", "PromptProfileVersion")
        Rule = apps.get_model("studio", "PromptProfileFieldRule")
        Binding = apps.get_model("studio", "PromptProfileBinding")
        source = Profile.objects.get(
            profile_key=previous.PROFILE_KEY,
            version=previous.TARGET_VERSION,
        )
        self.source_id = source.id
        self.source_contract = (
            source.contract_size_bytes,
            source.contract_sha256,
            source.status,
        )
        self.source_validators = {
            rule.field_definition.key: list(rule.generation_validator_keys or [])
            for rule in Rule.objects.filter(profile_version_id=source.id)
            .select_related("field_definition")
        }
        self.binding_id = Binding.objects.get(
            key=previous.CURRENT_BINDING_KEY,
        ).id

    def test_v14_marks_all_generated_length_ranges_advisory(self):
        Profile = self.apps.get_model("studio", "PromptProfileVersion")
        Rule = self.apps.get_model("studio", "PromptProfileFieldRule")
        Binding = self.apps.get_model("studio", "PromptProfileBinding")
        migration = importlib.import_module(
            "studio.migrations.0090_prompt_profile_advisory_lengths",
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
        rules = {
            rule.field_definition.key: rule
            for rule in Rule.objects.filter(profile_version_id=target.id)
            .select_related("field_definition")
        }
        self.assertEqual(len(rules), 6)
        advisory_fields = {
            "detailed_description",
            "overall_soundscape",
            "non_diegetic_music",
        }
        for key, rule in rules.items():
            expected = list(self.source_validators[key])
            if key in advisory_fields:
                expected.append(migration.ADVISORY_VALIDATOR)
            self.assertEqual(rule.generation_validator_keys, expected)
        detail = rules["detailed_description"]
        self.assertEqual(
            (detail.length_unit, detail.generation_min, detail.generation_max),
            ("english_words", 350, 500),
        )
        self.assertIn("Target twelve complete sentences", detail.generation_instruction)
        self.assertIn("advisory review warning", detail.generation_instruction)
        self.assertNotIn(
            "official inclusive 350-500 English word contract",
            detail.generation_instruction,
        )
        for key in ("overall_soundscape", "non_diegetic_music"):
            self.assertIn(
                "recommended target",
                rules[key].generation_instruction,
            )

        encoded = migration._canonical_bytes(
            migration._profile_contract(
                target,
                list(
                    Rule.objects.filter(profile_version_id=target.id)
                    .select_related("field_definition")
                    .order_by("position", "id")
                ),
            ),
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

        migration.publish_advisory_length_successor(self.apps, None)
        self.assertEqual(
            Profile.objects.filter(
                profile_key=migration.PROFILE_KEY,
                version=migration.TARGET_VERSION,
            ).count(),
            1,
        )
