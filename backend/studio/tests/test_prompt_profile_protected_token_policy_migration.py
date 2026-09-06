from __future__ import annotations

import hashlib
import importlib
import json
from copy import deepcopy

from django.utils import timezone

from studio.tests.test_configuration_migrations import _MigrationTestCase


class PromptProfileProtectedTokenPolicyMigrationTests(_MigrationTestCase):
    migrate_from = "0082_comfyui_pre_submit_resource_release"
    migrate_to = "0083_prompt_profile_protected_token_policy"

    @staticmethod
    def _canonical(value):
        return json.dumps(
            value,
            ensure_ascii=False,
            sort_keys=True,
            separators=(",", ":"),
        ).encode("utf-8")

    def set_up_before_migration(self, apps):
        Profile = apps.get_model("studio", "PromptProfileVersion")
        Rule = apps.get_model("studio", "PromptProfileFieldRule")
        Field = apps.get_model("studio", "PromptFieldDefinition")
        Binding = apps.get_model("studio", "PromptProfileBinding")

        source = Profile.objects.filter(
            profile_key="minimax_h3.video.ref2va",
            version=8,
        ).first()
        if source is None:
            source = Profile.objects.create(
                profile_key="minimax_h3.video.ref2va",
                version=8,
                name="H3 Ref2VA v8",
                description="Historical six-section fixture",
                model_family="minimax_h3",
                operation="video.generate",
                mode="Ref2VA",
                source_projection_key="classified_reference_context_ir_v1",
                source_projection_config={},
                official_reference="https://example.invalid/h3",
                channel_limits={},
                review_state="ready",
                status="published",
                contract_schema_version=4,
                contract_size_bytes=2,
                contract_sha256="",
                published_at=timezone.now(),
            )
            specs = (
                ("subject_definitions", "request.h3_ref2va.subject_definitions", "label_block"),
                ("summary", "request.h3_ref2va.summary", "label_colon"),
                ("retention_analysis", "request.h3_ref2va.retention_analysis", "label_block"),
                ("detailed_description", "request.h3_ref2va.detailed_description", "label_block"),
                ("overall_soundscape", "request.h3_ref2va.overall_soundscape", "label_colon"),
                ("non_diegetic_music", "request.h3_ref2va.non_diegetic_music", "label_colon"),
            )
            for position, (key, selector, label_style) in enumerate(specs, start=1):
                definition, _ = Field.objects.get_or_create(
                    key=key,
                    defaults={
                        "label": key,
                        "category": "prompt.contract",
                        "description": key,
                        "impact": key,
                        "value_type": "long_text",
                        "value_options": [],
                        "is_active": True,
                    },
                )
                model_generated = position > 3
                Rule.objects.create(
                    profile_version_id=source.id,
                    field_definition_id=definition.id,
                    source_selector=selector,
                    channel="positive_prompt",
                    position=position,
                    label_style=label_style,
                    separator="newline",
                    required=True,
                    omit_if_empty=False,
                    max_chars=7000,
                    generation_mode=(
                        "model_generated" if model_generated else "server_locked"
                    ),
                    generation_source_selector=selector,
                    generation_instruction=(
                        "Use only frozen facts." if model_generated else ""
                    ),
                    generation_output_semantics=(
                        "contribution" if model_generated else "final"
                    ),
                    generation_required=True,
                    language_policy="en" if model_generated else "any",
                    length_unit="english_words" if model_generated else "none",
                    generation_min=1 if model_generated else None,
                    generation_max=220 if model_generated else None,
                    generation_validator_keys=(
                        ["non_empty", "no_control_characters"]
                        if model_generated else []
                    ),
                )
            migration = importlib.import_module(
                "studio.migrations.0083_prompt_profile_protected_token_policy",
            )
            rules = list(
                Rule.objects.filter(profile_version_id=source.id)
                .select_related("field_definition")
                .order_by("position", "id")
            )
            encoded = self._canonical(migration._profile_contract(source, rules))
            Profile.objects.filter(pk=source.pk).update(
                contract_size_bytes=len(encoded),
                contract_sha256=hashlib.sha256(encoded).hexdigest(),
            )
            source.refresh_from_db()

        self.assertFalse(Profile.objects.filter(
            profile_key="minimax_h3.video.ref2va",
            version=9,
        ).exists())
        malformed = Profile.objects.create(
            profile_key=source.profile_key,
            version=9,
            name="Incomplete v9",
            description="Pre-existing partial target",
            model_family=source.model_family,
            operation=source.operation,
            mode=source.mode,
            source_projection_key=source.source_projection_key,
            source_projection_config=source.source_projection_config,
            official_reference=source.official_reference,
            channel_limits=source.channel_limits,
            review_state="ready",
            status="published",
            contract_schema_version=4,
            contract_size_bytes=2,
            contract_sha256="",
            published_at=timezone.now(),
        )
        migration = importlib.import_module(
            "studio.migrations.0083_prompt_profile_protected_token_policy",
        )
        with self.assertRaises(RuntimeError) as malformed_error:
            migration.publish_protected_token_policy_successor(apps, None)
        self.assertIn("contract drifted", str(malformed_error.exception))
        malformed.refresh_from_db()
        self.assertEqual(malformed.contract_sha256, "")
        malformed.delete()

        binding = Binding.objects.filter(
            key="minimax-h3-ref2va-official-v8",
        ).first()
        if binding is None:
            binding = Binding.objects.create(
                key="minimax-h3-ref2va-official-v8",
                name="H3 v8 current binding",
                operation=source.operation,
                model_family=source.model_family,
                mode=source.mode,
                prompt_profile_version_id=source.id,
                priority=200,
                is_active=True,
            )
        elif binding.prompt_profile_version_id != source.id:
            Binding.objects.filter(pk=binding.pk).update(
                prompt_profile_version_id=source.id,
                is_active=True,
            )
            binding.refresh_from_db()

        self.source_id = source.id
        self.source_contract = (
            source.contract_size_bytes,
            source.contract_sha256,
            source.status,
        )
        self.source_validators = {
            rule.field_definition.key: deepcopy(rule.generation_validator_keys)
            for rule in Rule.objects.filter(
                profile_version_id=source.id,
            ).select_related("field_definition")
        }
        self.binding_id = binding.id

    def test_v9_is_immutable_successor_and_reuses_current_binding_identity(self):
        Profile = self.apps.get_model("studio", "PromptProfileVersion")
        Rule = self.apps.get_model("studio", "PromptProfileFieldRule")
        Binding = self.apps.get_model("studio", "PromptProfileBinding")

        source = Profile.objects.get(pk=self.source_id)
        target = Profile.objects.get(
            profile_key="minimax_h3.video.ref2va",
            version=9,
        )
        self.assertNotEqual(source.id, target.id)
        self.assertEqual(
            (source.contract_size_bytes, source.contract_sha256, source.status),
            self.source_contract,
        )
        self.assertEqual({
            rule.field_definition.key: rule.generation_validator_keys
            for rule in Rule.objects.filter(
                profile_version_id=source.id,
            ).select_related("field_definition")
        }, self.source_validators)

        target_validators = {
            rule.field_definition.key: rule.generation_validator_keys
            for rule in Rule.objects.filter(
                profile_version_id=target.id,
            ).select_related("field_definition")
        }
        self.assertIn(
            "protected_tokens_at_least_once",
            target_validators["detailed_description"],
        )
        for key, validators in target_validators.items():
            if key != "detailed_description":
                self.assertIn("protected_tokens_preserve_count", validators)
            self.assertEqual(
                len({
                    value for value in validators
                    if value in {
                        "protected_tokens_preserve_count",
                        "protected_tokens_at_least_once",
                    }
                }),
                1,
            )

        binding = Binding.objects.get(pk=self.binding_id)
        self.assertEqual(binding.key, "minimax-h3-ref2va-official-v8")
        self.assertEqual(binding.prompt_profile_version_id, target.id)
        self.assertTrue(binding.is_active)
        self.assertFalse(Binding.objects.filter(
            key="minimax-h3-ref2va-official-v9",
        ).exists())

        encoded = self._canonical(importlib.import_module(
            "studio.migrations.0083_prompt_profile_protected_token_policy",
        )._profile_contract(
            target,
            list(Rule.objects.filter(profile_version_id=target.id)
                 .select_related("field_definition")
                 .order_by("position", "id")),
        ))
        self.assertEqual(target.contract_size_bytes, len(encoded))
        self.assertEqual(
            target.contract_sha256,
            hashlib.sha256(encoded).hexdigest(),
        )

    def test_forward_rejects_retired_existing_target_before_binding_activation(self):
        Profile = self.apps.get_model("studio", "PromptProfileVersion")
        Binding = self.apps.get_model("studio", "PromptProfileBinding")
        target = Profile.objects.get(
            profile_key="minimax_h3.video.ref2va",
            version=9,
        )
        Profile.objects.filter(pk=target.pk).update(status="retired")
        Binding.objects.filter(pk=self.binding_id).update(
            prompt_profile_version_id=self.source_id,
            is_active=False,
        )
        migration = importlib.import_module(
            "studio.migrations.0083_prompt_profile_protected_token_policy",
        )

        with self.assertRaises(RuntimeError) as captured:
            migration.publish_protected_token_policy_successor(self.apps, None)

        self.assertIn("contract drifted", str(captured.exception))
        binding = Binding.objects.get(pk=self.binding_id)
        self.assertEqual(binding.prompt_profile_version_id, self.source_id)
        self.assertFalse(binding.is_active)

    def test_forward_rejects_source_contract_checksum_or_size_drift(self):
        Profile = self.apps.get_model("studio", "PromptProfileVersion")
        Rule = self.apps.get_model("studio", "PromptProfileFieldRule")
        source = Profile.objects.get(pk=self.source_id)
        source_rule = Rule.objects.filter(
            profile_version_id=source.id,
        ).order_by("position", "id").first()
        migration = importlib.import_module(
            "studio.migrations.0083_prompt_profile_protected_token_policy",
        )

        original_max_chars = source_rule.max_chars
        Rule.objects.filter(pk=source_rule.pk).update(
            max_chars=original_max_chars + 1,
        )
        with self.assertRaises(RuntimeError) as checksum_error:
            migration.publish_protected_token_policy_successor(self.apps, None)
        self.assertIn("v8 contract checksum drifted", str(checksum_error.exception))

        Rule.objects.filter(pk=source_rule.pk).update(max_chars=original_max_chars)
        Profile.objects.filter(pk=source.pk).update(
            contract_size_bytes=source.contract_size_bytes + 1,
        )
        with self.assertRaises(RuntimeError) as size_error:
            migration.publish_protected_token_policy_successor(self.apps, None)
        self.assertIn("v8 contract checksum drifted", str(size_error.exception))
