from __future__ import annotations

import hashlib
import json

from django.utils import timezone

from studio.tests.test_configuration_migrations import _MigrationTestCase


class PromptNormalizationCapacityProfileMigrationTests(_MigrationTestCase):
    migrate_from = "0077_prompt_profile_static_reference_policy_v4"
    migrate_to = "0078_prompt_normalization_capacity_profiles"

    @staticmethod
    def _contract(value):
        encoded = json.dumps(
            value,
            ensure_ascii=False,
            sort_keys=True,
            separators=(",", ":"),
        ).encode("utf-8")
        return len(encoded), hashlib.sha256(encoded).hexdigest()

    def set_up_before_migration(self, apps):
        Provider = apps.get_model("studio", "ProviderProfile")
        ModelProfile = apps.get_model("studio", "ModelProfileVersion")
        Deployment = apps.get_model("studio", "ModelDeployment")
        Field = apps.get_model("studio", "PromptFieldDefinition")
        PromptProfile = apps.get_model("studio", "PromptProfileVersion")
        Rule = apps.get_model("studio", "PromptProfileFieldRule")
        Binding = apps.get_model("studio", "PromptProfileBinding")

        provider = Provider.objects.get(key="deepseek")

        def create_model_profile(version, capabilities):
            specification = {
                "schema_version": 1,
                "model_family": "deepseek",
                "capabilities": capabilities,
                "script_structure": {
                    "max_input_bytes": 64_000,
                    "max_output_tokens": 8_192,
                    "minimum_shot_duration_ms": 1_000,
                    "maximum_shot_duration_ms": 10_000,
                    "timeout_seconds": 180,
                },
            }
            size, digest = self._contract(specification)
            return ModelProfile.objects.create(
                profile_key="deepseek-text",
                version=version,
                provider=provider,
                model_identifier="deepseek-v4-flash",
                modality="text",
                specification=specification,
                specification_schema_version=1,
                specification_size_bytes=size,
                specification_sha256=digest,
                status="published",
                published_at=timezone.now(),
            )

        source_v5 = create_model_profile(
            5,
            ["script.generate", "prompt.normalize", "script.structure"],
        )
        source_v7 = create_model_profile(
            7,
            [
                "script.generate",
                "prompt.normalize",
                "script.structure",
                "visual_rules.generate",
            ],
        )
        empty_size, empty_sha = self._contract({})
        for key, name, source in (
            ("deepseek-text-v5-prod", "DeepSeek text v5 prod", source_v5),
            (
                "deepseek-visual-rules-v7-prod",
                "DeepSeek visual rules v7 prod",
                source_v7,
            ),
        ):
            Deployment.objects.create(
                key=key,
                name=name,
                profile_version=source,
                runtime_config={},
                runtime_config_schema_version=1,
                runtime_config_size_bytes=empty_size,
                runtime_config_sha256=empty_sha,
                status="active",
            )

        field = Field.objects.create(
            key="migration.capacity.contract",
            label="Migration capacity contract",
            category="migration",
            value_type="long_text",
            default_value=None,
            value_options=[],
            is_required=False,
            is_active=True,
        )

        def create_prompt_profile(operation):
            profile = PromptProfile.objects.create(
                profile_key=f"migration.{operation}",
                version=1,
                name=f"Migration {operation}",
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
                profile_version=profile,
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
                "profile_key": profile.profile_key,
                "version": profile.version,
                "model_family": profile.model_family,
                "operation": profile.operation,
                "mode": profile.mode,
                "source_projection_key": profile.source_projection_key,
                "channel_limits": profile.channel_limits,
                "source_projection_config": profile.source_projection_config,
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
            PromptProfile.objects.filter(pk=profile.pk).update(
                contract_size_bytes=size,
                contract_sha256=digest,
            )
            profile.contract_size_bytes = size
            profile.contract_sha256 = digest
            return profile

        prompt_profiles = {
            operation: create_prompt_profile(operation)
            for operation in (
                "script.generate",
                "prompt.normalize",
                "script.structure",
                "visual_rules.generate",
            )
        }
        for operation in ("script.generate", "prompt.normalize", "script.structure"):
            Binding.objects.create(
                key=f"deepseek-{operation.replace('.', '-')}-v5",
                name=f"DeepSeek {operation} v5",
                operation=operation,
                model_family="deepseek",
                mode="",
                model_profile_version=source_v5,
                prompt_profile_version=prompt_profiles[operation],
                priority=200,
                is_active=True,
            )
        Binding.objects.create(
            key="deepseek-visual-rules-v7",
            name="DeepSeek visual rules v7",
            operation="visual_rules.generate",
            model_family="deepseek",
            mode="",
            model_profile_version=source_v7,
            prompt_profile_version=prompt_profiles["visual_rules.generate"],
            priority=200,
            is_active=True,
        )
        self.source_v5_id = source_v5.id
        self.source_v7_id = source_v7.id

    def test_successors_repoint_routes_and_preserve_capability_closures(self):
        Profile = self.apps.get_model("studio", "ModelProfileVersion")
        Deployment = self.apps.get_model("studio", "ModelDeployment")
        Binding = self.apps.get_model("studio", "PromptProfileBinding")

        target_v8 = Profile.objects.get(profile_key="deepseek-text", version=8)
        target_v9 = Profile.objects.get(profile_key="deepseek-text", version=9)
        policy = {
            "max_input_bytes": 29_000,
            "max_output_tokens": 8_192,
            "max_items_per_batch": 4,
            "timeout_seconds": 180.0,
        }
        self.assertEqual(target_v8.specification["prompt_normalization"], policy)
        self.assertEqual(target_v9.specification["prompt_normalization"], policy)
        self.assertEqual(
            target_v8.specification["capabilities"],
            ["script.generate", "prompt.normalize", "script.structure"],
        )
        self.assertEqual(
            target_v9.specification["capabilities"],
            [
                "script.generate",
                "prompt.normalize",
                "script.structure",
                "visual_rules.generate",
            ],
        )
        text_deployment = Deployment.objects.get(key="deepseek-text-v5-prod")
        visual_rules_deployment = Deployment.objects.get(
            key="deepseek-visual-rules-v7-prod",
        )
        self.assertEqual(text_deployment.profile_version_id, target_v8.id)
        self.assertEqual(
            text_deployment.name,
            "DeepSeek 剧本、结构化与提示词（受管）",
        )
        self.assertEqual(visual_rules_deployment.profile_version_id, target_v9.id)
        self.assertEqual(
            visual_rules_deployment.name,
            "DeepSeek 全剧影视规则（受管）",
        )
        self.assertEqual(
            set(Binding.objects.filter(
                model_profile_version_id=target_v8.id,
                is_active=True,
            ).values_list("operation", flat=True)),
            {"script.generate", "prompt.normalize", "script.structure"},
        )
        self.assertEqual(
            set(Binding.objects.filter(
                model_profile_version_id=target_v9.id,
                is_active=True,
            ).values_list("operation", flat=True)),
            {
                "script.generate",
                "prompt.normalize",
                "script.structure",
                "visual_rules.generate",
            },
        )
        self.assertFalse(Binding.objects.filter(
            model_profile_version_id__in=(self.source_v5_id, self.source_v7_id),
            is_active=True,
        ).exists())

    def test_runtime_resolver_uses_successor_exact_bindings(self):
        # Migration tests must query through the historical app registry.
        # Importing the leaf runtime resolver here makes the current ORM select
        # columns that don't exist yet at migration 0078.
        Deployment = self.apps.get_model("studio", "ModelDeployment")
        Binding = self.apps.get_model("studio", "PromptProfileBinding")

        for deployment_key, operations in (
            (
                "deepseek-text-v5-prod",
                ("script.generate", "prompt.normalize", "script.structure"),
            ),
            (
                "deepseek-visual-rules-v7-prod",
                (
                    "script.generate",
                    "prompt.normalize",
                    "script.structure",
                    "visual_rules.generate",
                ),
            ),
        ):
            deployment = Deployment.objects.select_related(
                "profile_version",
            ).get(key=deployment_key)
            for operation in operations:
                binding = Binding.objects.get(
                    operation=operation,
                    model_family="deepseek",
                    model_profile_version=deployment.profile_version,
                    is_active=True,
                )
                self.assertEqual(
                    binding.model_profile_version_id,
                    deployment.profile_version_id,
                )
