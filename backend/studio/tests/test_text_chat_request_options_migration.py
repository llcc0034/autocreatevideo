from __future__ import annotations

from copy import deepcopy
import importlib

from django.core.exceptions import ValidationError

from studio.tests.test_configuration_migrations import _MigrationTestCase


class TextChatRequestOptionsMigrationTests(_MigrationTestCase):
    migrate_from = "0110_provider_smoke_security_boundaries"
    migrate_to = "0111_text_chat_request_options"

    route_keys = (
        "deepseek-text-v5-prod",
        "deepseek-visual-rules-v7-prod",
    )

    def set_up_before_migration(self, apps):
        Profile = apps.get_model("studio", "ModelProfileVersion")
        Deployment = apps.get_model("studio", "ModelDeployment")
        Binding = apps.get_model("studio", "PromptProfileBinding")
        if not Deployment.objects.filter(key__in=self.route_keys).exists():
            previous_case = importlib.import_module(
                "studio.tests.test_visual_rules_quality_advisory_migration",
            ).VisualRulesQualityAdvisoryMigrationTests
            previous_case.set_up_before_migration(self, apps)
            importlib.import_module(
                "studio.migrations.0103_visual_rules_quality_advisories",
            ).publish_visual_rules_quality_advisory_successor(apps, None)
        source = Profile.objects.get(version=17, deployments__key=self.route_keys[0])
        self.source_id = source.id
        self.source_profile_key = source.profile_key
        self.source_specification = deepcopy(source.specification)
        self.source_hash = source.specification_sha256
        self.deployment_ids = {
            row.key: row.id
            for row in Deployment.objects.filter(key__in=self.route_keys)
        }
        source_bindings = Binding.objects.filter(
            model_profile_version_id=source.id,
            is_active=True,
        )
        self.source_binding_ids = set(source_bindings.values_list("id", flat=True))
        self.source_binding_contracts = {
            (
                row.operation,
                row.mode,
                row.model_family,
                row.prompt_profile_version_id,
                row.priority,
            )
            for row in source_bindings
        }

    def test_v18_freezes_one_request_contract_for_all_text_operations(self):
        Profile = self.apps.get_model("studio", "ModelProfileVersion")
        Deployment = self.apps.get_model("studio", "ModelDeployment")
        Binding = self.apps.get_model("studio", "PromptProfileBinding")
        migration = importlib.import_module(
            "studio.migrations.0111_text_chat_request_options",
        )

        source = Profile.objects.get(pk=self.source_id)
        target = Profile.objects.get(
            profile_key=self.source_profile_key,
            version=migration.TARGET_VERSION,
        )
        expected = migration._target_specification(self.source_specification)
        self.assertEqual(source.status, "retired")
        self.assertEqual(source.specification, self.source_specification)
        self.assertEqual(source.specification_sha256, self.source_hash)
        self.assertEqual(target.status, "published")
        self.assertEqual(target.specification, expected)
        size, sha256 = migration._canonical_contract(expected)
        self.assertEqual(target.specification_size_bytes, size)
        self.assertEqual(target.specification_sha256, sha256)
        for policy_key, temperature in migration.POLICY_KEYS:
            policy = target.specification[policy_key]
            self.assertNotIn("chat_completion_options", policy)
            self.assertEqual(
                policy["chat_request_options"]["temperature"],
                temperature,
            )
            self.assertEqual(
                policy["chat_request_options"]["response_format"],
                {"type": "json_object"},
            )

        for key, deployment_id in self.deployment_ids.items():
            deployment = Deployment.objects.get(pk=deployment_id)
            self.assertEqual(deployment.key, key)
            self.assertEqual(deployment.profile_version_id, target.id)
        target_bindings = list(Binding.objects.filter(
            model_profile_version_id=target.id,
            is_active=True,
        ))
        self.assertEqual(
            {
                (
                    row.operation,
                    row.mode,
                    row.model_family,
                    row.prompt_profile_version_id,
                    row.priority,
                )
                for row in target_bindings
            },
            self.source_binding_contracts,
        )
        self.assertFalse(Binding.objects.filter(
            pk__in=self.source_binding_ids,
            is_active=True,
        ).exists())

        from studio.models import ModelProfileVersion as RuntimeProfile
        from studio.services.production_visual_rules import (
            validate_visual_rules_model_profile,
        )
        from studio.services.prompt_normalization import (
            validate_prompt_normalization_model_profile,
        )
        from studio.services.script_generation_contract import (
            validate_script_generation_model_profile,
        )
        from studio.services.script_structure import (
            validate_script_structure_model_profile,
        )

        runtime_target = RuntimeProfile.objects.get(pk=target.id)
        # v18 remains a truthful historical snapshot.  Current runtime
        # validators intentionally reject its text-output policies because
        # the required field contract is introduced by the v19 successor.
        with self.assertRaises(ValidationError):
            validate_script_generation_model_profile(runtime_target)
        with self.assertRaises(ValidationError):
            validate_script_structure_model_profile(runtime_target)
        normalized_policies = (
            validate_prompt_normalization_model_profile(runtime_target),
            validate_visual_rules_model_profile(runtime_target),
        )
        for policy in normalized_policies:
            self.assertIn("chat_request_options", policy)

        migration.publish_text_chat_request_options_successor(self.apps, None)
        self.assertEqual(Profile.objects.filter(
            profile_key=self.source_profile_key,
            version=migration.TARGET_VERSION,
        ).count(), 1)

    def test_reverse_restores_v17_bindings_and_removes_v18(self):
        Profile = self.apps.get_model("studio", "ModelProfileVersion")
        Deployment = self.apps.get_model("studio", "ModelDeployment")
        Binding = self.apps.get_model("studio", "PromptProfileBinding")
        migration = importlib.import_module(
            "studio.migrations.0111_text_chat_request_options",
        )

        migration.restore_v17_and_remove_v18(self.apps, None)

        source = Profile.objects.get(pk=self.source_id)
        self.assertEqual(source.status, "published")
        self.assertEqual(source.specification, self.source_specification)
        self.assertEqual(source.specification_sha256, self.source_hash)
        self.assertFalse(Profile.objects.filter(
            profile_key=self.source_profile_key,
            version=migration.TARGET_VERSION,
        ).exists())
        self.assertEqual(
            set(Binding.objects.filter(
                model_profile_version_id=source.id,
                is_active=True,
            ).values_list("id", flat=True)),
            self.source_binding_ids,
        )
        for key, deployment_id in self.deployment_ids.items():
            deployment = Deployment.objects.get(pk=deployment_id)
            self.assertEqual(deployment.key, key)
            self.assertEqual(deployment.profile_version_id, source.id)

        migration.restore_v17_and_remove_v18(self.apps, None)
        self.assertEqual(Profile.objects.filter(
            profile_key=self.source_profile_key,
            version=17,
            status="published",
        ).count(), 1)
