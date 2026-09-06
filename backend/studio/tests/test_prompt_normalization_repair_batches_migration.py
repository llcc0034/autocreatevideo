from __future__ import annotations

import hashlib
import importlib
import json
from copy import deepcopy
from datetime import timedelta
from types import SimpleNamespace

from django.test import SimpleTestCase
from django.utils import timezone

from studio.tests.test_configuration_migrations import _MigrationTestCase


class PromptNormalizationRepairBatchMigrationGuardTests(SimpleTestCase):
    def test_nonempty_shared_runtime_host_is_rejected_before_repoint(self):
        migration = importlib.import_module(
            "studio.migrations.0080_prompt_normalization_repair_batches",
        )

        with self.assertRaises(RuntimeError) as captured:
            migration._validate_runtime_host_merge([
                SimpleNamespace(runtime_host_id="same-host"),
                SimpleNamespace(runtime_host_id="same-host"),
            ])

        self.assertIn("runtime_host conflict", str(captured.exception))
        migration._validate_runtime_host_merge([
            SimpleNamespace(runtime_host_id=None),
            SimpleNamespace(runtime_host_id=None),
        ])
        migration._validate_runtime_host_merge([
            SimpleNamespace(runtime_host_id="host-a"),
            SimpleNamespace(runtime_host_id="host-b"),
        ])


class PromptNormalizationRepairBatchMigrationTests(_MigrationTestCase):
    migrate_from = "0079_prompt_normalization_chat_options"
    migrate_to = "0080_prompt_normalization_repair_batches"

    route_keys = (
        "deepseek-text-v5-prod",
        "deepseek-visual-rules-v7-prod",
    )

    @staticmethod
    def _contract(value):
        encoded = json.dumps(
            value,
            ensure_ascii=False,
            sort_keys=True,
            separators=(",", ":"),
            allow_nan=False,
        ).encode("utf-8")
        return len(encoded), hashlib.sha256(encoded).hexdigest()

    def set_up_before_migration(self, apps):
        Profile = apps.get_model("studio", "ModelProfileVersion")
        Deployment = apps.get_model("studio", "ModelDeployment")
        Binding = apps.get_model("studio", "PromptProfileBinding")
        Recipe = apps.get_model("studio", "ProductionRecipeVersion")
        Snapshot = apps.get_model("studio", "ModelDeploymentCapabilitySnapshot")

        if not Deployment.objects.filter(key__in=self.route_keys).exists():
            from studio.tests.test_prompt_normalization_chat_options_migration import (
                PromptNormalizationChatOptionsMigrationTests,
            )

            PromptNormalizationChatOptionsMigrationTests._create_source_catalog(
                self,
                apps,
            )
            migration_0079 = importlib.import_module(
                "studio.migrations.0079_prompt_normalization_chat_options",
            )
            migration_0079.publish_chat_options_successor(apps, None)

        deployments = list(Deployment.objects.select_related(
            "profile_version",
        ).filter(key__in=self.route_keys).order_by("key"))
        self.assertEqual(len(deployments), 2)
        self.assertEqual(len({row.profile_version_id for row in deployments}), 1)
        source = deployments[0].profile_version
        self.assertEqual(source.profile_key, "deepseek-text")
        self.assertEqual(source.version, 10)
        self.assertEqual(source.status, "published")
        self.assertEqual(source.specification["prompt_normalization"], {
            "max_input_bytes": 29_000,
            "max_output_tokens": 8_192,
            "max_items_per_batch": 4,
            "timeout_seconds": 180.0,
            "chat_completion_options": {
                "thinking": {"type": "disabled"},
            },
        })
        self.source = {
            field: deepcopy(getattr(source, field))
            for field in (
                "id",
                "profile_key",
                "provider_id",
                "model_identifier",
                "modality",
                "specification",
                "specification_schema_version",
                "specification_size_bytes",
                "specification_sha256",
                "published_at",
                "published_by_id",
            )
        }
        source_bindings = list(Binding.objects.filter(
            model_profile_version_id=source.id,
            is_active=True,
        ).order_by("operation", "mode", "key"))
        self.assertEqual(len(source_bindings), 4)
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
        self.deployments = {
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
        self.recipe_facts = {
            str(row.id): (
                deepcopy(row.definition),
                row.definition_schema_version,
                row.definition_size_bytes,
                row.definition_sha256,
                row.status,
            )
            for row in Recipe.objects.all()
        }

        from studio.models import ModelDeployment as RuntimeDeployment
        from studio.services.async_media_contract import deployment_target_sha256

        self.smoke_facts = {}
        for deployment in deployments:
            runtime = RuntimeDeployment.objects.get(pk=deployment.id)
            target_sha256 = deployment_target_sha256(runtime)
            operations = ["prompt.normalize"]
            evidence = {"schema_version": 1, "execution_submitted": True}
            operation_size, operation_sha = self._contract(operations)
            evidence_size, evidence_sha = self._contract(evidence)
            previous = Snapshot.objects.filter(
                deployment_id=deployment.id,
            ).order_by("-revision").first()
            smoke = Snapshot.objects.create(
                deployment_id=deployment.id,
                revision=(previous.revision + 1) if previous else 1,
                probe_kind="smoke",
                status="passed",
                target_sha256=target_sha256,
                provider_config_sha256="a" * 64,
                deployment_config_sha256="b" * 64,
                credential_binding_sha256="",
                operations=operations,
                operations_schema_version=1,
                operations_size_bytes=operation_size,
                operations_sha256=operation_sha,
                evidence=evidence,
                evidence_schema_version=1,
                evidence_size_bytes=evidence_size,
                evidence_sha256=evidence_sha,
                captured_at=timezone.now(),
                expires_at=timezone.now() + timedelta(days=1),
            )
            self.smoke_facts[deployment.key] = {
                "id": smoke.id,
                "target_sha256": target_sha256,
                "evidence": deepcopy(smoke.evidence),
                "evidence_sha256": smoke.evidence_sha256,
            }
        self.assertFalse(Profile.objects.filter(
            profile_key=source.profile_key,
            version=11,
        ).exists())

    def test_v11_repoints_stable_routes_and_preserves_historical_facts(self):
        Profile = self.apps.get_model("studio", "ModelProfileVersion")
        Deployment = self.apps.get_model("studio", "ModelDeployment")
        Binding = self.apps.get_model("studio", "PromptProfileBinding")
        Recipe = self.apps.get_model("studio", "ProductionRecipeVersion")
        Snapshot = self.apps.get_model("studio", "ModelDeploymentCapabilitySnapshot")

        source = Profile.objects.get(pk=self.source["id"])
        target = Profile.objects.get(profile_key=self.source["profile_key"], version=11)
        self.assertNotEqual(target.id, source.id)
        expected_specification = deepcopy(self.source["specification"])
        expected_specification["prompt_normalization"] = {
            "max_input_bytes": 12_000,
            "max_output_tokens": 8_192,
            "max_items_per_batch": 1,
            "timeout_seconds": 180.0,
            "chat_completion_options": {
                "thinking": {"type": "disabled"},
            },
        }
        expected_size, expected_sha = self._contract(expected_specification)
        self.assertEqual(target.specification, expected_specification)
        self.assertEqual(target.specification_size_bytes, expected_size)
        self.assertEqual(target.specification_sha256, expected_sha)
        self.assertEqual(target.status, "published")
        self.assertFalse(Profile.objects.filter(
            profile_key=target.profile_key,
            version=12,
        ).exists())

        for deployment_key, before in self.deployments.items():
            deployment = Deployment.objects.get(key=deployment_key)
            self.assertEqual(deployment.profile_version_id, target.id)
            for field, value in before.items():
                self.assertEqual(getattr(deployment, field), value, field)

        target_bindings = list(Binding.objects.filter(
            model_profile_version_id=target.id,
            is_active=True,
        ))
        self.assertEqual(len(target_bindings), 4)
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
        self.assertEqual(
            {(row.operation, row.mode) for row in target_bindings},
            {
                ("script.generate", ""),
                ("prompt.normalize", ""),
                ("script.structure", ""),
                ("visual_rules.generate", ""),
            },
        )
        self.assertEqual(source.status, "retired")
        for field in (
            "specification",
            "specification_schema_version",
            "specification_size_bytes",
            "specification_sha256",
            "published_at",
            "published_by_id",
        ):
            self.assertEqual(getattr(source, field), self.source[field], field)
        self.assertEqual(
            set(Binding.objects.filter(pk__in=self.source_binding_ids).values_list(
                "id",
                flat=True,
            )),
            self.source_binding_ids,
        )
        self.assertFalse(Binding.objects.filter(
            pk__in=self.source_binding_ids,
            is_active=True,
        ).exists())
        self.assertEqual(
            {
                str(row.id): (
                    row.definition,
                    row.definition_schema_version,
                    row.definition_size_bytes,
                    row.definition_sha256,
                    row.status,
                )
                for row in Recipe.objects.all()
            },
            self.recipe_facts,
        )

        from studio.models import ModelDeployment as RuntimeDeployment
        from studio.services.async_media_contract import (
            deployment_target_sha256,
            fresh_smoke_snapshot,
        )

        for deployment_key, smoke_before in self.smoke_facts.items():
            smoke = Snapshot.objects.get(pk=smoke_before["id"])
            self.assertEqual(smoke.target_sha256, smoke_before["target_sha256"])
            self.assertEqual(smoke.evidence, smoke_before["evidence"])
            self.assertEqual(smoke.evidence_sha256, smoke_before["evidence_sha256"])
            current = RuntimeDeployment.objects.get(
                pk=self.deployments[deployment_key]["id"],
            )
            self.assertNotEqual(
                deployment_target_sha256(current),
                smoke_before["target_sha256"],
            )
            self.assertIsNone(fresh_smoke_snapshot(current, "prompt.normalize"))

    def test_v11_historical_snapshot_keeps_exact_binding_closure(self):
        Deployment = self.apps.get_model("studio", "ModelDeployment")
        Binding = self.apps.get_model("studio", "PromptProfileBinding")

        deployments = list(Deployment.objects.select_related(
            "profile_version",
        ).filter(key__in=self.route_keys))
        self.assertEqual(len({row.profile_version_id for row in deployments}), 1)
        profile = deployments[0].profile_version
        self.assertEqual(profile.version, 11)
        policy = profile.specification["prompt_normalization"]
        self.assertEqual(policy, {
            "max_input_bytes": 12_000,
            "max_output_tokens": 8_192,
            "max_items_per_batch": 1,
            "timeout_seconds": 180.0,
            "chat_completion_options": {
                "thinking": {"type": "disabled"},
            },
        })
        for operation in (
            "script.generate",
            "prompt.normalize",
            "script.structure",
            "visual_rules.generate",
        ):
            binding = Binding.objects.get(
                operation=operation,
                model_family="deepseek",
                model_profile_version=profile,
                is_active=True,
            )
            self.assertEqual(binding.model_profile_version_id, profile.id)
