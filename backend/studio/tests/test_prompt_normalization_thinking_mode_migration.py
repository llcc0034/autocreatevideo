from __future__ import annotations

import hashlib
import importlib
import json
from copy import deepcopy
from datetime import timedelta

from django.utils import timezone

from studio.tests.test_configuration_migrations import _MigrationTestCase


class PromptNormalizationThinkingModeMigrationTests(_MigrationTestCase):
    migrate_from = "0087_prompt_profile_twelve_sentence_scaffold"
    migrate_to = "0088_prompt_normalization_thinking_mode"

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
        Snapshot = apps.get_model("studio", "ModelDeploymentCapabilitySnapshot")

        if not Deployment.objects.filter(key__in=self.route_keys).exists():
            from studio.tests.test_prompt_normalization_chat_options_migration import (
                PromptNormalizationChatOptionsMigrationTests,
            )

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
            ("deepseek-text", 12, "published"),
        )
        self.assertEqual(
            source.specification["prompt_normalization"],
            {
                "max_input_bytes": 16_000,
                "max_output_tokens": 8_192,
                "max_items_per_batch": 1,
                "timeout_seconds": 180.0,
                "chat_completion_options": {
                    "thinking": {"type": "disabled"},
                },
            },
        )
        self.source_id = source.id
        self.source_specification = deepcopy(source.specification)
        source_bindings = list(
            Binding.objects.filter(
                model_profile_version_id=source.id,
                is_active=True,
            ).order_by("operation", "mode", "key")
        )
        self.assertEqual(len(source_bindings), 4)
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
        self.source_binding_ids = {row.id for row in source_bindings}
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

        # Opaque historical evidence is preserved by the migration, not
        # recomputed by a runtime reader that requires the latest schema.
        target_sha256 = "c" * 64
        self.source_specification_sha256 = source.specification_sha256
        operations = ["prompt.normalize"]
        evidence = {"schema_version": 1, "execution_submitted": True}
        operations_size, operations_sha = self._contract(operations)
        evidence_size, evidence_sha = self._contract(evidence)
        smoke = Snapshot.objects.create(
            deployment_id=deployments[0].id,
            revision=1,
            probe_kind="smoke",
            status="succeeded",
            target_sha256=target_sha256,
            provider_config_sha256="a" * 64,
            deployment_config_sha256="b" * 64,
            credential_binding_sha256="",
            operations=operations,
            operations_schema_version=1,
            operations_size_bytes=operations_size,
            operations_sha256=operations_sha,
            evidence=evidence,
            evidence_schema_version=1,
            evidence_size_bytes=evidence_size,
            evidence_sha256=evidence_sha,
            captured_at=timezone.now(),
            expires_at=timezone.now() + timedelta(days=1),
        )
        self.smoke_id = smoke.id
        self.smoke_target_sha256 = target_sha256
        self.assertFalse(
            Profile.objects.filter(profile_key="deepseek-text", version=13).exists(),
        )

    def test_v13_changes_target_contract_and_preserves_historical_smoke(self):
        Profile = self.apps.get_model("studio", "ModelProfileVersion")
        Deployment = self.apps.get_model("studio", "ModelDeployment")
        Binding = self.apps.get_model("studio", "PromptProfileBinding")
        Snapshot = self.apps.get_model("studio", "ModelDeploymentCapabilitySnapshot")

        source = Profile.objects.get(pk=self.source_id)
        target = Profile.objects.get(profile_key="deepseek-text", version=13)
        expected_specification = deepcopy(self.source_specification)
        expected_specification["prompt_normalization"] = {
            "max_input_bytes": 16_000,
            "max_output_tokens": 8_192,
            "max_items_per_batch": 1,
            "timeout_seconds": 180.0,
            "chat_completion_options": {
                "thinking": {"type": "enabled"},
                "reasoning_effort": "high",
            },
        }
        expected_size, expected_sha = self._contract(expected_specification)
        self.assertEqual(target.specification, expected_specification)
        self.assertEqual(target.specification_size_bytes, expected_size)
        self.assertEqual(target.specification_sha256, expected_sha)
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
        self.assertFalse(
            Binding.objects.filter(
                pk__in=self.source_binding_ids,
                is_active=True,
            ).exists(),
        )

        smoke = Snapshot.objects.get(pk=self.smoke_id)
        self.assertEqual(smoke.target_sha256, self.smoke_target_sha256)
        current = Deployment.objects.get(
            pk=self.deployment_facts[self.route_keys[0]]["id"],
        )
        self.assertNotEqual(
            current.profile_version_id, self.source_id,
        )
        self.assertNotEqual(
            current.profile_version.specification_sha256,
            self.source_specification_sha256,
        )
