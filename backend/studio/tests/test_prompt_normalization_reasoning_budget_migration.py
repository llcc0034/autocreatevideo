from __future__ import annotations

import importlib
from copy import deepcopy

from studio.tests.test_configuration_migrations import _MigrationTestCase


class PromptNormalizationReasoningBudgetMigrationTests(_MigrationTestCase):
    migrate_from = "0088_prompt_normalization_thinking_mode"
    migrate_to = "0089_prompt_normalization_reasoning_budget"

    route_keys = (
        "deepseek-text-v5-prod",
        "deepseek-visual-rules-v7-prod",
    )

    def set_up_before_migration(self, apps):
        Profile = apps.get_model("studio", "ModelProfileVersion")
        Deployment = apps.get_model("studio", "ModelDeployment")
        Binding = apps.get_model("studio", "PromptProfileBinding")

        if not Deployment.objects.filter(key__in=self.route_keys).exists():
            from studio.tests.test_prompt_normalization_chat_options_migration import (
                PromptNormalizationChatOptionsMigrationTests,
            )

            self._contract = PromptNormalizationChatOptionsMigrationTests._contract
            PromptNormalizationChatOptionsMigrationTests._create_source_catalog(
                self,
                apps,
            )
            importlib.import_module(
                "studio.migrations.0079_prompt_normalization_chat_options",
            ).publish_chat_options_successor(apps, None)
            importlib.import_module(
                "studio.migrations.0080_prompt_normalization_repair_batches",
            ).publish_repair_batch_successor(apps, None)
            importlib.import_module(
                "studio.migrations.0081_prompt_normalization_smoke_capacity",
            ).publish_smoke_capacity_successor(apps, None)
            importlib.import_module(
                "studio.migrations.0088_prompt_normalization_thinking_mode",
            ).publish_thinking_mode_successor(apps, None)

        deployments = list(
            Deployment.objects.select_related("profile_version")
            .filter(key__in=self.route_keys)
            .order_by("key")
        )
        self.assertEqual(len(deployments), 2)
        self.assertEqual(len({row.profile_version_id for row in deployments}), 1)
        source = deployments[0].profile_version
        self.assertEqual(
            (source.profile_key, source.version, source.status),
            ("deepseek-text", 13, "published"),
        )
        self.assertEqual(
            source.specification["prompt_normalization"],
            {
                "max_input_bytes": 16_000,
                "max_output_tokens": 8_192,
                "max_items_per_batch": 1,
                "timeout_seconds": 180.0,
                "chat_completion_options": {
                    "thinking": {"type": "enabled"},
                    "reasoning_effort": "high",
                },
            },
        )
        self.source_id = source.id
        self.source_specification = deepcopy(source.specification)
        self.deployment_facts = {
            row.key: {
                field: deepcopy(getattr(row, field))
                for field in (
                    "id",
                    "key",
                    "name",
                    "runtime_host_id",
                    "credential_id",
                    "runtime_config",
                    "runtime_config_schema_version",
                    "runtime_config_size_bytes",
                    "runtime_config_sha256",
                    "priority",
                    "is_default",
                    "status",
                )
            }
            for row in deployments
        }
        source_bindings = list(
            Binding.objects.filter(
                model_profile_version_id=source.id,
                is_active=True,
            )
        )
        self.source_binding_ids = {row.id for row in source_bindings}
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
        self.assertFalse(
            Profile.objects.filter(profile_key="deepseek-text", version=14).exists(),
        )

    def test_v14_uses_low_reasoning_with_explicit_output_budget(self):
        Profile = self.apps.get_model("studio", "ModelProfileVersion")
        Deployment = self.apps.get_model("studio", "ModelDeployment")
        Binding = self.apps.get_model("studio", "PromptProfileBinding")

        source = Profile.objects.get(pk=self.source_id)
        target = Profile.objects.get(profile_key="deepseek-text", version=14)
        expected = deepcopy(self.source_specification)
        expected["prompt_normalization"] = {
            "max_input_bytes": 16_000,
            "max_output_tokens": 131_072,
            "output_token_warning_threshold": 32_768,
            "max_items_per_batch": 1,
            "timeout_seconds": 600.0,
            "chat_completion_options": {
                "thinking": {"type": "enabled"},
                "reasoning_effort": "low",
            },
        }
        self.assertEqual(target.specification, expected)
        self.assertEqual(target.status, "published")
        self.assertEqual(source.status, "retired")
        self.assertEqual(source.specification, self.source_specification)

        for key, before in self.deployment_facts.items():
            deployment = Deployment.objects.get(key=key)
            self.assertEqual(deployment.profile_version_id, target.id)
            for field, value in before.items():
                self.assertEqual(getattr(deployment, field), value, field)

        target_bindings = list(
            Binding.objects.filter(
                model_profile_version_id=target.id,
                is_active=True,
            )
        )
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
        self.assertFalse(
            Binding.objects.filter(
                pk__in=self.source_binding_ids,
                is_active=True,
            ).exists(),
        )
