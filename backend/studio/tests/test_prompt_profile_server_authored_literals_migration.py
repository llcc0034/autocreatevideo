from __future__ import annotations

import hashlib
import importlib

from django.utils import timezone

from studio.tests.test_configuration_migrations import _MigrationTestCase


class PromptProfileServerAuthoredLiteralsMigrationTests(_MigrationTestCase):
    migrate_from = "0083_prompt_profile_protected_token_policy"
    migrate_to = "0084_prompt_profile_server_authored_literals"

    def set_up_before_migration(self, apps):
        Profile = apps.get_model("studio", "PromptProfileVersion")
        Rule = apps.get_model("studio", "PromptProfileFieldRule")
        Field = apps.get_model("studio", "PromptFieldDefinition")
        Binding = apps.get_model("studio", "PromptProfileBinding")

        source = Profile.objects.filter(
            profile_key="minimax_h3.video.ref2va",
            version=9,
        ).first()
        if source is None:
            source = Profile.objects.create(
                profile_key="minimax_h3.video.ref2va",
                version=9,
                name="H3 Ref2VA v9",
                description="Protected-token policy fixture",
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
                instruction = ""
                if key == "detailed_description":
                    instruction = (
                        "Expand frozen facts and wrap dialogue as "
                        "<d>[Language] ...</d>."
                    )
                elif model_generated:
                    instruction = "Use only frozen facts."
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
                    generation_instruction=instruction,
                    generation_output_semantics=(
                        "contribution" if model_generated else "final"
                    ),
                    generation_required=True,
                    language_policy="en" if model_generated else "any",
                    length_unit="english_words" if model_generated else "none",
                    generation_min=1 if model_generated else None,
                    generation_max=500 if model_generated else None,
                    generation_validator_keys=(
                        [
                            "non_empty",
                            "no_control_characters",
                            (
                                "protected_tokens_at_least_once"
                                if key == "detailed_description"
                                else "protected_tokens_preserve_count"
                            ),
                        ]
                        if model_generated else ["protected_tokens_preserve_count"]
                    ),
                )
            migration = importlib.import_module(
                "studio.migrations.0084_prompt_profile_server_authored_literals",
            )
            source_rules = list(
                Rule.objects.filter(profile_version_id=source.id)
                .select_related("field_definition")
                .order_by("position", "id")
            )
            encoded = migration._canonical_bytes(
                migration._profile_contract(source, source_rules),
            )
            Profile.objects.filter(pk=source.pk).update(
                contract_size_bytes=len(encoded),
                contract_sha256=hashlib.sha256(encoded).hexdigest(),
            )
            source.refresh_from_db()

        Profile.objects.filter(
            profile_key=source.profile_key,
            version=10,
        ).delete()
        binding = Binding.objects.filter(
            key="minimax-h3-ref2va-official-v8",
        ).first()
        if binding is None:
            binding = Binding.objects.create(
                key="minimax-h3-ref2va-official-v8",
                name="H3 v9 current binding",
                operation=source.operation,
                model_family=source.model_family,
                mode=source.mode,
                prompt_profile_version_id=source.id,
                priority=200,
                is_active=True,
            )
        else:
            Binding.objects.filter(pk=binding.pk).update(
                prompt_profile_version_id=source.id,
                is_active=True,
            )

        self.source_id = source.id
        self.binding_id = binding.id
        self.source_contract = (
            source.contract_size_bytes,
            source.contract_sha256,
            source.status,
        )
        self.source_instructions = {
            rule.field_definition.key: rule.generation_instruction
            for rule in Rule.objects.filter(profile_version_id=source.id)
            .select_related("field_definition")
        }

    def test_v10_moves_authored_literal_assembly_to_the_configured_server_projection(self):
        Profile = self.apps.get_model("studio", "PromptProfileVersion")
        Rule = self.apps.get_model("studio", "PromptProfileFieldRule")
        Binding = self.apps.get_model("studio", "PromptProfileBinding")
        migration = importlib.import_module(
            "studio.migrations.0084_prompt_profile_server_authored_literals",
        )

        source = Profile.objects.get(pk=self.source_id)
        target = Profile.objects.get(
            profile_key="minimax_h3.video.ref2va",
            version=10,
        )
        self.assertEqual(
            (source.contract_size_bytes, source.contract_sha256, source.status),
            self.source_contract,
        )
        source_rules = list(
            Rule.objects.filter(profile_version_id=source.id)
            .select_related("field_definition")
            .order_by("position", "id")
        )
        target_rules = list(
            Rule.objects.filter(profile_version_id=target.id)
            .select_related("field_definition")
            .order_by("position", "id")
        )
        self.assertEqual(len(source_rules), 6)
        self.assertEqual(len(target_rules), 6)
        for source_rule, target_rule in zip(source_rules, target_rules, strict=True):
            key = source_rule.field_definition.key
            self.assertEqual(
                source_rule.generation_instruction,
                self.source_instructions[key],
            )
            if key == "detailed_description":
                self.assertEqual(
                    target_rule.generation_instruction,
                    migration.DETAIL_GENERATION_INSTRUCTION,
                )
                self.assertNotIn(
                    "wrap dialogue as",
                    target_rule.generation_instruction,
                )
                self.assertIn(
                    "deterministic server projection appends",
                    target_rule.generation_instruction,
                )
            else:
                self.assertEqual(
                    target_rule.generation_instruction,
                    source_rule.generation_instruction,
                )
            self.assertEqual(
                target_rule.generation_validator_keys,
                source_rule.generation_validator_keys,
            )

        encoded = migration._canonical_bytes(
            migration._profile_contract(target, target_rules),
        )
        self.assertEqual(target.contract_size_bytes, len(encoded))
        self.assertEqual(
            target.contract_sha256,
            hashlib.sha256(encoded).hexdigest(),
        )
        binding = Binding.objects.get(pk=self.binding_id)
        self.assertEqual(binding.prompt_profile_version_id, target.id)
        self.assertTrue(binding.is_active)
        self.assertFalse(Binding.objects.filter(
            key="minimax-h3-ref2va-official-v10",
        ).exists())
