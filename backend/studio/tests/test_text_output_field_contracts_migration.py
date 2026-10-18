from __future__ import annotations

from copy import deepcopy
import importlib

from django.core.exceptions import ValidationError

from studio.tests.test_configuration_migrations import _MigrationTestCase


class TextOutputFieldContractsMigrationTests(_MigrationTestCase):
    migrate_from = "0112_runtime_contract_security_boundaries"
    migrate_to = "0113_text_output_field_contracts"

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
                "studio.tests.test_text_chat_request_options_migration",
            ).TextChatRequestOptionsMigrationTests
            previous_case.set_up_before_migration(self, apps)
            importlib.import_module(
                "studio.migrations.0111_text_chat_request_options",
            ).publish_text_chat_request_options_successor(apps, None)
        source = Profile.objects.get(version=18, deployments__key=self.route_keys[0])
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

    def test_v19_publishes_field_contracts_and_repoints_stable_routes(self):
        Profile = self.apps.get_model("studio", "ModelProfileVersion")
        Deployment = self.apps.get_model("studio", "ModelDeployment")
        Binding = self.apps.get_model("studio", "PromptProfileBinding")
        migration = importlib.import_module(
            "studio.migrations.0113_text_output_field_contracts",
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
        self.assertEqual(
            target.specification["series_concept"]["field_contract"],
            migration.SERIES_CONCEPT_FIELD_CONTRACT,
        )
        self.assertEqual(
            target.specification["script_structure"]["field_contract"],
            migration.SCRIPT_STRUCTURE_FIELD_CONTRACT,
        )
        size, sha256 = migration._canonical_contract(expected)
        self.assertEqual(target.specification_size_bytes, size)
        self.assertEqual(target.specification_sha256, sha256)

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
        from studio.services.script_generation_contract import (
            validate_script_generation_model_profile,
        )
        from studio.services.script_structure import (
            validate_script_structure_model_profile,
        )

        runtime_target = RuntimeProfile.objects.get(pk=target.id)
        # The exact field contracts are asserted against the historical
        # migration above. v19 still includes the old model-owned output
        # allowance, so it must not be accepted as today's runtime contract.
        for validator in (
            validate_script_generation_model_profile,
            validate_script_structure_model_profile,
        ):
            with self.subTest(validator=validator.__name__):
                with self.assertRaises(ValidationError) as caught:
                    validator(runtime_target)
                self.assertIn("specification", caught.exception.message_dict)

        migration.publish_text_output_field_contract_successor(self.apps, None)
        self.assertEqual(Profile.objects.filter(
            profile_key=self.source_profile_key,
            version=migration.TARGET_VERSION,
        ).count(), 1)

    def test_reverse_restores_v18_bindings_and_removes_v19(self):
        Profile = self.apps.get_model("studio", "ModelProfileVersion")
        Deployment = self.apps.get_model("studio", "ModelDeployment")
        Binding = self.apps.get_model("studio", "PromptProfileBinding")
        migration = importlib.import_module(
            "studio.migrations.0113_text_output_field_contracts",
        )

        migration.restore_v18_and_remove_v19(self.apps, None)

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

        migration.restore_v18_and_remove_v19(self.apps, None)
        self.assertEqual(Profile.objects.filter(
            profile_key=self.source_profile_key,
            version=18,
            status="published",
        ).count(), 1)
