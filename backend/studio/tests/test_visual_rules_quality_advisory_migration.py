from __future__ import annotations

from copy import deepcopy
import importlib

from studio.tests.test_configuration_migrations import _MigrationTestCase


class VisualRulesQualityAdvisoryMigrationTests(_MigrationTestCase):
    migrate_from = "0102_h3_quality_advisories"
    migrate_to = "0103_visual_rules_quality_advisories"

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
                "studio.tests.test_prompt_normalization_input_advisory_migration",
            ).PromptNormalizationInputAdvisoryMigrationTests
            previous_case.set_up_before_migration(self, apps)
            importlib.import_module(
                "studio.migrations.0091_prompt_normalization_input_advisory",
            ).publish_input_advisory_successor(apps, None)
            importlib.import_module(
                "studio.migrations.0097_visual_rules_advisory_capacity",
            ).publish_visual_rules_capacity_successor(apps, None)
        source = Profile.objects.get(version=16, deployments__key=self.route_keys[0])
        self.source_id = source.id
        self.source_profile_key = source.profile_key
        self.source_specification = deepcopy(source.specification)
        self.source_hash = source.specification_sha256
        self.deployment_ids = {
            row.key: row.id
            for row in Deployment.objects.filter(key__in=self.route_keys)
        }
        self.source_binding_ids = set(Binding.objects.filter(
            model_profile_version_id=source.id,
            is_active=True,
        ).values_list("id", flat=True))
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

    def test_v17_adds_hashed_quality_advisories_without_mutating_v16(self):
        Profile = self.apps.get_model("studio", "ModelProfileVersion")
        Deployment = self.apps.get_model("studio", "ModelDeployment")
        Binding = self.apps.get_model("studio", "PromptProfileBinding")
        migration = importlib.import_module(
            "studio.migrations.0103_visual_rules_quality_advisories",
        )

        source = Profile.objects.get(pk=self.source_id)
        target = Profile.objects.get(
            profile_key=self.source_profile_key,
            version=migration.TARGET_VERSION,
        )
        expected = deepcopy(self.source_specification)
        expected["visual_rules"] = migration.TARGET_POLICY

        self.assertEqual(source.status, "retired")
        self.assertEqual(source.specification, self.source_specification)
        self.assertEqual(source.specification_sha256, self.source_hash)
        self.assertEqual(target.status, "published")
        self.assertEqual(target.specification, expected)
        encoded = migration._canonical_contract(expected)
        self.assertEqual(target.specification_size_bytes, encoded[0])
        self.assertEqual(target.specification_sha256, encoded[1])
        self.assertEqual(
            target.specification["visual_rules"]["quality_advisories"],
            migration.QUALITY_ADVISORIES,
        )
        self.assertNotEqual(target.specification_sha256, self.source_hash)

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

        self.assertEqual(
            target.specification["visual_rules"]["quality_advisories"],
            migration.QUALITY_ADVISORIES,
        )

        migration.publish_visual_rules_quality_advisory_successor(
            self.apps,
            None,
        )
        self.assertEqual(Profile.objects.filter(
            profile_key=self.source_profile_key,
            version=migration.TARGET_VERSION,
        ).count(), 1)

    def test_reverse_restores_v16_bindings_and_removes_v17(self):
        Profile = self.apps.get_model("studio", "ModelProfileVersion")
        Deployment = self.apps.get_model("studio", "ModelDeployment")
        Binding = self.apps.get_model("studio", "PromptProfileBinding")
        migration = importlib.import_module(
            "studio.migrations.0103_visual_rules_quality_advisories",
        )

        migration.restore_v16_and_remove_v17(self.apps, None)

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

        migration.restore_v16_and_remove_v17(self.apps, None)
        self.assertEqual(Profile.objects.filter(
            profile_key=self.source_profile_key,
            version=16,
            status="published",
        ).count(), 1)
