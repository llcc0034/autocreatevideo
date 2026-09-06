from __future__ import annotations

from copy import deepcopy
import hashlib
import importlib
from unittest.mock import patch

from django.contrib.auth import get_user_model

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

    def test_v17_is_runtime_valid_and_retires_v16_without_mutating_it(self):
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

        from studio.models import PromptProfileVersion as RuntimeProfile
        from studio.services.prompt_profiles import (
            validate_profile_rule_runtime,
            verify_prompt_profile_contract,
        )

        runtime_target = RuntimeProfile.objects.get(pk=target.id)
        verify_prompt_profile_contract(runtime_target)
        for runtime_rule in runtime_target.field_rules.select_related(
            "field_definition",
        ).order_by("position", "id"):
            validate_profile_rule_runtime(runtime_rule)

        migration.publish_instruction_budget_successor(self.apps, None)
        self.assertEqual(
            Profile.objects.filter(
                profile_key=migration.PROFILE_KEY,
                version=migration.TARGET_VERSION,
            ).count(),
            1,
        )

    def test_migrated_v17_representative_smoke_preflight_never_invokes_provider(self):
        from studio.models import PromptProfileVersion
        from studio.services.json_contracts import assign_json_contract
        from studio.services.prompt_normalization import (
            prepare_prompt_normalization_smoke_request,
        )
        from studio.tests.factories import (
            create_ready_prompt_normalization_test_deployment,
            create_test_prompt_profile_binding,
        )

        migration = importlib.import_module(
            "studio.migrations.0100_prompt_profile_generation_instruction_budget",
        )
        target = PromptProfileVersion.objects.get(
            profile_key=migration.PROFILE_KEY,
            version=migration.TARGET_VERSION,
            status="published",
        )
        admin = get_user_model().objects.create_superuser(
            username="prompt-v17-smoke-admin",
            password="StrongPass123!",
            email="prompt-v17-smoke@example.test",
        )
        deployment = create_ready_prompt_normalization_test_deployment()
        profile = deployment.profile_version
        specification = deepcopy(profile.specification)
        specification["prompt_normalization"].update({
            "max_input_bytes": 65_536,
            "max_items_per_batch": 1,
        })
        assign_json_contract(profile, "specification", specification)
        profile.save(update_fields=(
            "specification",
            "specification_schema_version",
            "specification_size_bytes",
            "specification_sha256",
            "updated_at",
        ))
        create_test_prompt_profile_binding(
            actor=admin,
            model_profile_version=profile,
            operation="prompt.normalize",
        )

        with patch(
            "studio.services.provider_registry.LocalQwenDriver.invoke",
        ) as invoke:
            prepared = prepare_prompt_normalization_smoke_request(deployment)

        invoke.assert_not_called()
        snapshot = prepared["request_snapshot"]
        representative = snapshot["representative_contract"]
        payload = snapshot["payload"]
        self.assertEqual(representative["prompt_profile_version_id"], str(target.id))
        self.assertEqual(representative["item_count"], 1)
        self.assertEqual(
            payload["target_prompt_contract"]["profiles"][0][
                "prompt_profile_version_id"
            ],
            str(target.id),
        )
        self.assertEqual(
            payload["target_prompt_contract"]["profiles"][0]["contract"][
                "schema_version"
            ],
            5,
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
