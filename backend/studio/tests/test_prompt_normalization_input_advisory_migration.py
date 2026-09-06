from __future__ import annotations

import importlib
from copy import deepcopy

from studio.tests.test_configuration_migrations import _MigrationTestCase


class PromptNormalizationInputAdvisoryMigrationTests(_MigrationTestCase):
    migrate_from = "0090_prompt_profile_advisory_lengths"
    migrate_to = "0091_prompt_normalization_input_advisory"

    route_keys = (
        "deepseek-text-v5-prod",
        "deepseek-visual-rules-v7-prod",
    )

    def set_up_before_migration(self, apps):
        previous_case = importlib.import_module(
            "studio.tests.test_prompt_normalization_reasoning_budget_migration",
        ).PromptNormalizationReasoningBudgetMigrationTests
        previous_case.set_up_before_migration(self, apps)
        importlib.import_module(
            "studio.migrations.0089_prompt_normalization_reasoning_budget",
        ).publish_reasoning_budget_successor(apps, None)

        Profile = apps.get_model("studio", "ModelProfileVersion")
        Deployment = apps.get_model("studio", "ModelDeployment")
        Binding = apps.get_model("studio", "PromptProfileBinding")
        source = Profile.objects.get(profile_key="deepseek-text", version=14)
        self.source_id = source.id
        self.source_specification = deepcopy(source.specification)
        deployments = list(
            Deployment.objects.select_related("profile_version")
            .filter(key__in=self.route_keys)
            .order_by("key")
        )
        self.deployment_ids = {row.key: row.id for row in deployments}
        self.source_binding_ids = set(
            Binding.objects.filter(
                model_profile_version_id=source.id,
                is_active=True,
            ).values_list("id", flat=True)
        )
        self.source_binding_contracts = {
            (
                row.operation,
                row.mode,
                row.model_family,
                row.prompt_profile_version_id,
                row.priority,
            )
            for row in Binding.objects.filter(
                model_profile_version_id=source.id,
                is_active=True,
            )
        }

    def test_v15_separates_input_advisory_from_hard_capacity(self):
        Profile = self.apps.get_model("studio", "ModelProfileVersion")
        Deployment = self.apps.get_model("studio", "ModelDeployment")
        Binding = self.apps.get_model("studio", "PromptProfileBinding")
        migration = importlib.import_module(
            "studio.migrations.0091_prompt_normalization_input_advisory",
        )

        source = Profile.objects.get(pk=self.source_id)
        target = Profile.objects.get(profile_key="deepseek-text", version=15)
        expected = deepcopy(self.source_specification)
        expected["prompt_normalization"] = migration.TARGET_POLICY
        self.assertEqual(target.specification, expected)
        self.assertEqual(target.status, "published")
        self.assertEqual(source.status, "retired")
        self.assertEqual(source.specification, self.source_specification)

        for key, deployment_id in self.deployment_ids.items():
            deployment = Deployment.objects.get(pk=deployment_id)
            self.assertEqual(deployment.key, key)
            self.assertEqual(deployment.profile_version_id, target.id)

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
