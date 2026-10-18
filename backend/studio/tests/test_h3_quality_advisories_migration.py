from __future__ import annotations

from copy import deepcopy
import hashlib
import importlib

from studio.tests.test_configuration_migrations import _MigrationTestCase


class H3QualityAdvisoriesMigrationTests(_MigrationTestCase):
    migrate_from = "0101_remove_prompt_profile_generation_instruction_limit"
    migrate_to = "0102_h3_quality_advisories"

    def set_up_before_migration(self, apps):
        previous_case = importlib.import_module(
            "studio.tests.test_prompt_profile_generation_instruction_budget_migration",
        ).PromptProfileGenerationInstructionBudgetMigrationTests
        previous_case.set_up_before_migration(self, apps)
        previous = importlib.import_module(
            "studio.migrations.0100_prompt_profile_generation_instruction_budget",
        )
        previous.publish_instruction_budget_successor(apps, None)

        Profile = apps.get_model("studio", "PromptProfileVersion")
        Rule = apps.get_model("studio", "PromptProfileFieldRule")
        Binding = apps.get_model("studio", "PromptProfileBinding")
        source = Profile.objects.get(
            profile_key=previous.PROFILE_KEY,
            version=previous.TARGET_VERSION,
        )
        self.source_id = source.id
        self.source_hash = source.contract_sha256
        self.source_config = deepcopy(source.source_projection_config)
        self.source_rules = [
            previous._rule_payload(rule)
            for rule in Rule.objects.filter(profile_version_id=source.id)
            .select_related("field_definition")
            .order_by("position", "id")
        ]
        self.binding_id = Binding.objects.get(
            key=previous.CURRENT_BINDING_KEY,
        ).id

    def test_v18_adds_hashed_advisories_without_mutating_v17(self):
        Profile = self.apps.get_model("studio", "PromptProfileVersion")
        Rule = self.apps.get_model("studio", "PromptProfileFieldRule")
        Binding = self.apps.get_model("studio", "PromptProfileBinding")
        migration = importlib.import_module(
            "studio.migrations.0102_h3_quality_advisories",
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

        self.assertEqual(source.status, "retired")
        self.assertEqual(source.contract_sha256, self.source_hash)
        self.assertEqual(source.source_projection_config, self.source_config)
        self.assertEqual(
            [
                migration._rule_payload(rule)
                for rule in Rule.objects.filter(profile_version_id=source.id)
                .select_related("field_definition")
                .order_by("position", "id")
            ],
            self.source_rules,
        )
        self.assertEqual(
            [migration._rule_payload(rule) for rule in target_rules],
            [
                {
                    **row,
                    "generation_source_selector": (
                        migration.CONTRIBUTION_SOURCES.get(
                            row["field_key"],
                            row["generation_source_selector"],
                        )
                    ),
                }
                for row in self.source_rules
            ],
        )
        self.assertEqual(target.status, "published")
        self.assertEqual(target.review_state, "ready")
        self.assertEqual(target.contract_schema_version, 5)
        self.assertEqual(
            target.source_projection_config["quality_advisories"],
            migration.QUALITY_ADVISORIES,
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

        # The migration's own canonical bytes above prove its historical
        # result. Current execution deliberately rejects this old schema;
        # historical migration tests must not require a runtime reader for it.

        migration.publish_quality_advisory_successor(self.apps, None)
        self.assertEqual(
            Profile.objects.filter(
                profile_key=migration.PROFILE_KEY,
                version=migration.TARGET_VERSION,
            ).count(),
            1,
        )

    def test_reverse_restores_v17_binding_and_removes_v18(self):
        Profile = self.apps.get_model("studio", "PromptProfileVersion")
        Binding = self.apps.get_model("studio", "PromptProfileBinding")
        migration = importlib.import_module(
            "studio.migrations.0102_h3_quality_advisories",
        )

        migration.restore_v17_binding_and_remove_v18(self.apps, None)

        source = Profile.objects.get(pk=self.source_id)
        binding = Binding.objects.get(pk=self.binding_id)
        self.assertEqual(source.status, "published")
        self.assertEqual(source.contract_sha256, self.source_hash)
        self.assertEqual(source.source_projection_config, self.source_config)
        self.assertEqual(binding.prompt_profile_version_id, self.source_id)
        self.assertEqual(binding.name, migration.SOURCE_BINDING_NAME)
        self.assertTrue(binding.is_active)
        self.assertFalse(Profile.objects.filter(
            profile_key=migration.PROFILE_KEY,
            version=migration.TARGET_VERSION,
        ).exists())
