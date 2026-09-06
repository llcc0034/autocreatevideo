from __future__ import annotations

import hashlib
import importlib

from studio.tests.test_configuration_migrations import _MigrationTestCase


class PromptProfileOfficialReferenceLayoutMigrationTests(_MigrationTestCase):
    migrate_from = "0092_signed_media_throttle_scope"
    migrate_to = "0093_prompt_profile_official_reference_layout"

    def set_up_before_migration(self, apps):
        previous_case = importlib.import_module(
            "studio.tests.test_prompt_profile_advisory_lengths_migration",
        ).PromptProfileAdvisoryLengthsMigrationTests
        previous_case.set_up_before_migration(self, apps)
        previous = importlib.import_module(
            "studio.migrations.0090_prompt_profile_advisory_lengths",
        )
        previous.publish_advisory_length_successor(apps, None)

        Profile = apps.get_model("studio", "PromptProfileVersion")
        Binding = apps.get_model("studio", "PromptProfileBinding")
        source = Profile.objects.get(
            profile_key=previous.PROFILE_KEY,
            version=previous.TARGET_VERSION,
        )
        self.source_id = source.id
        self.source_contract = (
            source.contract_size_bytes,
            source.contract_sha256,
            source.source_projection_config,
            source.status,
        )
        self.binding_id = Binding.objects.get(
            key=previous.CURRENT_BINDING_KEY,
        ).id

    def test_v15_moves_official_layout_rules_into_versioned_projection_config(self):
        Profile = self.apps.get_model("studio", "PromptProfileVersion")
        Rule = self.apps.get_model("studio", "PromptProfileFieldRule")
        Binding = self.apps.get_model("studio", "PromptProfileBinding")
        migration = importlib.import_module(
            "studio.migrations.0093_prompt_profile_official_reference_layout",
        )

        source = Profile.objects.get(pk=self.source_id)
        target = Profile.objects.get(
            profile_key=migration.PROFILE_KEY,
            version=migration.TARGET_VERSION,
        )
        self.assertEqual(
            (
                source.contract_size_bytes,
                source.contract_sha256,
                source.source_projection_config,
                source.status,
            ),
            self.source_contract,
        )
        self.assertEqual(
            target.source_projection_config,
            migration.TARGET_SOURCE_PROJECTION_CONFIG,
        )
        target_rules = list(
            Rule.objects.filter(profile_version_id=target.id)
            .select_related("field_definition")
            .order_by("position", "id")
        )
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
        self.assertTrue(all(
            rule.generation_mode == "server_locked"
            and rule.generation_output_semantics == "final"
            for rule in target_rules[:3]
        ))
        detail = target_rules[3]
        self.assertEqual(
            (detail.length_unit, detail.generation_min, detail.generation_max),
            ("english_words", 350, 500),
        )
        self.assertIn("length_advisory", detail.generation_validator_keys)
        self.assertIn(
            "one or two locked visual-style sentences before [Shot 1]",
            detail.generation_instruction,
        )
        self.assertIn(
            "first clear visual appearance",
            detail.generation_instruction,
        )
        self.assertIn(
            "deterministic server projection appends",
            detail.generation_instruction,
        )

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

        migration.publish_official_reference_layout_successor(self.apps, None)
        self.assertEqual(
            Profile.objects.filter(
                profile_key=migration.PROFILE_KEY,
                version=migration.TARGET_VERSION,
            ).count(),
            1,
        )
