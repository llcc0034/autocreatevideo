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


class PromptNormalizationChatOptionsMigrationGuardTests(SimpleTestCase):
    def test_nonempty_shared_runtime_host_is_rejected_before_repoint(self):
        migration = importlib.import_module(
            "studio.migrations.0079_prompt_normalization_chat_options",
        )

        with self.assertRaises(RuntimeError) as captured:
            migration._validate_runtime_host_merge(
                SimpleNamespace(runtime_host_id="same-host"),
                SimpleNamespace(runtime_host_id="same-host"),
            )

        self.assertIn("runtime_host conflict", str(captured.exception))
        migration._validate_runtime_host_merge(
            SimpleNamespace(runtime_host_id=None),
            SimpleNamespace(runtime_host_id=None),
        )
        migration._validate_runtime_host_merge(
            SimpleNamespace(runtime_host_id="host-a"),
            SimpleNamespace(runtime_host_id="host-b"),
        )


class PromptNormalizationChatOptionsMigrationTests(_MigrationTestCase):
    migrate_from = "0078_prompt_normalization_capacity_profiles"
    migrate_to = "0079_prompt_normalization_chat_options"

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

    def _create_source_catalog(self, apps):
        Provider = apps.get_model("studio", "ProviderProfile")
        Profile = apps.get_model("studio", "ModelProfileVersion")
        Deployment = apps.get_model("studio", "ModelDeployment")
        Field = apps.get_model("studio", "PromptFieldDefinition")
        PromptProfile = apps.get_model("studio", "PromptProfileVersion")
        Rule = apps.get_model("studio", "PromptProfileFieldRule")
        Binding = apps.get_model("studio", "PromptProfileBinding")
        Recipe = apps.get_model("studio", "ProductionRecipeVersion")

        provider = Provider.objects.get(key="deepseek")

        def specification(*, visual_rules):
            value = {
                "schema_version": 1,
                "model_family": "deepseek",
                "capabilities": [
                    "script.generate",
                    "prompt.normalize",
                    "script.structure",
                    *(["visual_rules.generate"] if visual_rules else []),
                ],
                "series_concept": {
                    "minimum_shot_duration_ms": 1_000,
                    "maximum_shot_duration_ms": 10_000,
                    "max_output_tokens": 4_096,
                    "timeout_seconds": 180,
                },
                "script_structure": {
                    "minimum_shot_duration_ms": 1_000,
                    "maximum_shot_duration_ms": 10_000,
                    "max_input_bytes": 64_000,
                    "max_output_tokens": 8_192,
                    "timeout_seconds": 180,
                },
                "prompt_normalization": {
                    "max_input_bytes": 29_000,
                    "max_output_tokens": 8_192,
                    "max_items_per_batch": 4,
                    "timeout_seconds": 180.0,
                },
            }
            if visual_rules:
                value["visual_rules"] = {
                    "max_input_bytes": 64_000,
                    "max_output_tokens": 8_192,
                    "timeout_seconds": 180,
                }
            return value

        profiles = {}
        for version, visual_rules in ((8, False), (9, True)):
            spec = specification(visual_rules=visual_rules)
            size, digest = self._contract(spec)
            profiles[version] = Profile.objects.create(
                profile_key="deepseek-text",
                version=version,
                provider=provider,
                model_identifier="deepseek-v4-flash",
                modality="text",
                specification=spec,
                specification_schema_version=1,
                specification_size_bytes=size,
                specification_sha256=digest,
                status="published",
                published_at=timezone.now(),
            )

        empty_size, empty_sha = self._contract({})
        for key, name, version in (
            (
                "deepseek-text-v5-prod",
                "DeepSeek 剧本、结构化与提示词（受管）",
                8,
            ),
            (
                "deepseek-visual-rules-v7-prod",
                "DeepSeek 全剧影视规则（受管）",
                9,
            ),
        ):
            Deployment.objects.create(
                key=key,
                name=name,
                profile_version=profiles[version],
                runtime_config={},
                runtime_config_schema_version=1,
                runtime_config_size_bytes=empty_size,
                runtime_config_sha256=empty_sha,
                priority=200,
                status="active",
            )

        field = Field.objects.create(
            key="migration.chat.options.contract",
            label="Migration chat options contract",
            category="migration",
            value_type="long_text",
            default_value=None,
            value_options=[],
            is_required=False,
            is_active=True,
        )

        def create_prompt_profile(operation):
            prompt_profile = PromptProfile.objects.create(
                profile_key=f"migration.0079.{operation}",
                version=1,
                name=f"Reviewed {operation} assembly",
                model_family="deepseek",
                operation=operation,
                mode="",
                source_projection_key="direct_v1",
                source_projection_config={},
                channel_limits={},
                status="published",
                review_state="ready",
                contract_schema_version=4,
                contract_size_bytes=2,
                contract_sha256="",
                published_at=timezone.now(),
            )
            rule = Rule.objects.create(
                profile_version=prompt_profile,
                field_definition=field,
                source_selector="request.text",
                channel="structured_input",
                position=1,
                label_style="label_colon",
                separator="newline",
                required=True,
                omit_if_empty=False,
                max_chars=4_000,
                generation_mode="server_locked",
                generation_source_selector="request.text",
                generation_instruction="",
                generation_output_semantics="final",
                generation_required=True,
                language_policy="any",
                length_unit="none",
                generation_min=None,
                generation_max=None,
                generation_validator_keys=[],
            )
            contract = {
                "schema_version": 4,
                "profile_key": prompt_profile.profile_key,
                "version": prompt_profile.version,
                "model_family": prompt_profile.model_family,
                "operation": prompt_profile.operation,
                "mode": prompt_profile.mode,
                "source_projection_key": prompt_profile.source_projection_key,
                "channel_limits": prompt_profile.channel_limits,
                "source_projection_config": prompt_profile.source_projection_config,
                "rules": [{
                    "field_key": field.key,
                    "field_value_type": field.value_type,
                    "field_default_value": field.default_value,
                    "field_value_options": field.value_options,
                    "field_required": field.is_required,
                    "source_selector": rule.source_selector,
                    "channel": rule.channel,
                    "position": rule.position,
                    "label_style": rule.label_style,
                    "separator": rule.separator,
                    "required": rule.required,
                    "omit_if_empty": rule.omit_if_empty,
                    "max_chars": rule.max_chars,
                    "generation_mode": rule.generation_mode,
                    "generation_source_selector": rule.generation_source_selector,
                    "generation_instruction": rule.generation_instruction,
                    "generation_output_semantics": rule.generation_output_semantics,
                    "generation_required": rule.generation_required,
                    "language_policy": rule.language_policy,
                    "length_unit": rule.length_unit,
                    "generation_min": rule.generation_min,
                    "generation_max": rule.generation_max,
                    "generation_validator_keys": rule.generation_validator_keys,
                }],
            }
            size, digest = self._contract(contract)
            PromptProfile.objects.filter(pk=prompt_profile.pk).update(
                contract_size_bytes=size,
                contract_sha256=digest,
            )
            return prompt_profile

        prompt_profiles = {
            operation: create_prompt_profile(operation)
            for operation in (
                "script.generate",
                "prompt.normalize",
                "script.structure",
                "visual_rules.generate",
            )
        }
        for version, operations in (
            (8, ("script.generate", "prompt.normalize", "script.structure")),
            (9, (
                "script.generate",
                "prompt.normalize",
                "script.structure",
                "visual_rules.generate",
            )),
        ):
            for operation in operations:
                Binding.objects.create(
                    key=f"deepseek-chain-v{version}-{operation.replace('.', '-')}",
                    name=f"DeepSeek v5 legacy / ModelProfile v8 / ModelProfile v{version}",
                    operation=operation,
                    model_family="deepseek",
                    mode="",
                    model_profile_version=profiles[version],
                    prompt_profile_version=prompt_profiles[operation],
                    priority=200,
                    is_active=True,
                )

        recipe_definition = {
            "schema_version": 1,
            "routes": [
                {"model_deployment_id": str(deployment.id)}
                for deployment in Deployment.objects.filter(
                    key__in=self.route_keys,
                ).order_by("key")
            ],
        }
        recipe_size, recipe_sha = self._contract(recipe_definition)
        Recipe.objects.create(
            recipe_key="migration-chat-options-recipe",
            version=1,
            name="Migration chat options recipe",
            definition=recipe_definition,
            definition_schema_version=1,
            definition_size_bytes=recipe_size,
            definition_sha256=recipe_sha,
            status="published",
            published_at=timezone.now(),
        )

    def set_up_before_migration(self, apps):
        Deployment = apps.get_model("studio", "ModelDeployment")
        Binding = apps.get_model("studio", "PromptProfileBinding")
        Profile = apps.get_model("studio", "ModelProfileVersion")
        Recipe = apps.get_model("studio", "ProductionRecipeVersion")
        Snapshot = apps.get_model("studio", "ModelDeploymentCapabilitySnapshot")

        if not Deployment.objects.filter(key__in=self.route_keys).exists():
            self._create_source_catalog(apps)

        self.deployments = {}
        self.sources = {}
        for deployment_key, source_version in zip(self.route_keys, (8, 9)):
            deployment = Deployment.objects.select_related(
                "profile_version",
            ).get(key=deployment_key)
            source = deployment.profile_version
            self.assertEqual(source.version, source_version)
            self.assertEqual(source.status, "published")
            self.assertEqual(source.specification["prompt_normalization"], {
                "max_input_bytes": 29_000,
                "max_output_tokens": 8_192,
                "max_items_per_batch": 4,
                "timeout_seconds": 180.0,
            })
            deployment_fields = {
                field: getattr(deployment, field)
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
            bindings = list(Binding.objects.filter(
                model_profile_version_id=source.id,
                is_active=True,
            ).values_list(
                "operation",
                "mode",
                "model_family",
                "prompt_profile_version_id",
                "priority",
            ))
            self.assertTrue(bindings)
            self.deployments[deployment_key] = deployment_fields
            self.sources[source_version] = {
                "id": source.id,
                "profile_key": source.profile_key,
                "provider_id": source.provider_id,
                "model_identifier": source.model_identifier,
                "modality": source.modality,
                "specification": deepcopy(source.specification),
                "specification_schema_version": source.specification_schema_version,
                "specification_size_bytes": source.specification_size_bytes,
                "specification_sha256": source.specification_sha256,
                "published_at": source.published_at,
                "published_by_id": source.published_by_id,
                "bindings": set(bindings),
            }

        overlap_v8 = {
            row[:2]: row[2:] for row in self.sources[8]["bindings"]
        }
        overlap_v9 = {
            row[:2]: row[2:] for row in self.sources[9]["bindings"]
        }
        self.assertTrue(all(
            overlap_v9.get(identity) == contract
            for identity, contract in overlap_v8.items()
        ))
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

        runtime_deployment = RuntimeDeployment.objects.get(
            pk=self.deployments["deepseek-visual-rules-v7-prod"]["id"],
        )
        operations = ["prompt.normalize"]
        evidence = {"schema_version": 1, "execution_submitted": True}
        operation_size, operation_sha = self._contract(operations)
        evidence_size, evidence_sha = self._contract(evidence)
        self.old_smoke_target_sha256 = deployment_target_sha256(runtime_deployment)
        previous = Snapshot.objects.filter(
            deployment_id=runtime_deployment.id,
        ).order_by("-revision").first()
        old_smoke = Snapshot.objects.create(
            deployment_id=runtime_deployment.id,
            revision=(previous.revision + 1) if previous else 1,
            probe_kind="smoke",
            status="passed",
            target_sha256=self.old_smoke_target_sha256,
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
        self.old_smoke_id = old_smoke.id
        self.assertEqual(
            Profile.objects.filter(
                profile_key=self.sources[9]["profile_key"],
                version=10,
            ).count(),
            0,
        )

    def test_one_successor_preserves_routes_recipes_and_historical_evidence(self):
        Profile = self.apps.get_model("studio", "ModelProfileVersion")
        Deployment = self.apps.get_model("studio", "ModelDeployment")
        Binding = self.apps.get_model("studio", "PromptProfileBinding")
        Recipe = self.apps.get_model("studio", "ProductionRecipeVersion")
        Snapshot = self.apps.get_model("studio", "ModelDeploymentCapabilitySnapshot")

        target = Profile.objects.get(
            profile_key=self.sources[9]["profile_key"],
            version=10,
        )
        self.assertFalse(Profile.objects.filter(
            profile_key=target.profile_key,
            version=11,
        ).exists())
        expected_specification = deepcopy(self.sources[9]["specification"])
        expected_specification["prompt_normalization"] = {
            "max_input_bytes": 29_000,
            "max_output_tokens": 8_192,
            "max_items_per_batch": 4,
            "timeout_seconds": 180.0,
            "chat_completion_options": {
                "thinking": {"type": "disabled"},
            },
        }
        self.assertEqual(target.specification, expected_specification)
        expected_size, expected_sha256 = self._contract(expected_specification)
        self.assertEqual(target.specification_size_bytes, expected_size)
        self.assertEqual(target.specification_sha256, expected_sha256)
        self.assertEqual(target.status, "published")

        for deployment_key in self.route_keys:
            deployment = Deployment.objects.get(key=deployment_key)
            self.assertEqual(deployment.profile_version_id, target.id)
            for field, before in self.deployments[deployment_key].items():
                self.assertEqual(getattr(deployment, field), before, field)

        target_bindings = list(Binding.objects.filter(
            model_profile_version_id=target.id,
            is_active=True,
        ).order_by("operation", "mode", "key"))
        self.assertEqual(len(target_bindings), 4)
        self.assertEqual(
            {(row.operation, row.mode) for row in target_bindings},
            {
                ("script.generate", ""),
                ("prompt.normalize", ""),
                ("script.structure", ""),
                ("visual_rules.generate", ""),
            },
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
            self.sources[9]["bindings"],
        )
        for binding in target_bindings:
            self.assertNotIn(" / ModelProfile v8", binding.name)
            self.assertNotIn(" / ModelProfile v9", binding.name)
            self.assertNotIn("v5", binding.name.casefold())
            self.assertNotIn("v7", binding.name.casefold())

        for source_version in (8, 9):
            before = self.sources[source_version]
            source = Profile.objects.get(pk=before["id"])
            self.assertEqual(source.status, "retired")
            for field in (
                "specification",
                "specification_schema_version",
                "specification_size_bytes",
                "specification_sha256",
                "published_at",
                "published_by_id",
            ):
                self.assertEqual(getattr(source, field), before[field], field)
            self.assertFalse(Binding.objects.filter(
                model_profile_version_id=source.id,
                is_active=True,
            ).exists())

        recipe_facts = {
            str(row.id): (
                row.definition,
                row.definition_schema_version,
                row.definition_size_bytes,
                row.definition_sha256,
                row.status,
            )
            for row in Recipe.objects.all()
        }
        self.assertEqual(recipe_facts, self.recipe_facts)
        old_smoke = Snapshot.objects.get(pk=self.old_smoke_id)
        self.assertEqual(old_smoke.target_sha256, self.old_smoke_target_sha256)

        from studio.models import ModelDeployment
        from studio.services.async_media_contract import fresh_smoke_snapshot

        current = ModelDeployment.objects.get(
            pk=self.deployments["deepseek-visual-rules-v7-prod"]["id"],
        )
        self.assertIsNone(fresh_smoke_snapshot(current, "prompt.normalize"))

    def test_v10_historical_snapshot_keeps_one_successor_binding_closure(self):
        Deployment = self.apps.get_model("studio", "ModelDeployment")
        Binding = self.apps.get_model("studio", "PromptProfileBinding")

        deployments = list(Deployment.objects.select_related(
            "profile_version",
        ).filter(key__in=self.route_keys))
        self.assertEqual(len({row.profile_version_id for row in deployments}), 1)
        profile = deployments[0].profile_version
        self.assertEqual(profile.version, 10)
        policy = profile.specification["prompt_normalization"]
        self.assertEqual(policy["chat_completion_options"], {
            "thinking": {"type": "disabled"},
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
            self.assertEqual(
                binding.model_profile_version_id,
                profile.id,
            )
