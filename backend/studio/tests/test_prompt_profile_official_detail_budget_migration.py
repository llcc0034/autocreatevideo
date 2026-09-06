from __future__ import annotations

import hashlib
import importlib

from django.db import connection
from django.db.migrations.executor import MigrationExecutor
from django.utils import timezone

from studio.tests.test_configuration_migrations import _MigrationTestCase


class PromptProfileOfficialDetailBudgetMigrationTests(_MigrationTestCase):
    migrate_from = "0084_prompt_profile_server_authored_literals"
    migrate_to = "0085_prompt_profile_official_detail_budget"

    def set_up_before_migration(self, apps):
        Profile = apps.get_model("studio", "PromptProfileVersion")
        Rule = apps.get_model("studio", "PromptProfileFieldRule")
        Field = apps.get_model("studio", "PromptFieldDefinition")
        Binding = apps.get_model("studio", "PromptProfileBinding")
        migration = importlib.import_module(
            "studio.migrations.0085_prompt_profile_official_detail_budget",
        )
        previous = importlib.import_module(
            "studio.migrations.0084_prompt_profile_server_authored_literals",
        )

        target = Profile.objects.filter(
            profile_key=migration.PROFILE_KEY,
            version=migration.TARGET_VERSION,
        ).first()
        if target is not None:
            Binding.objects.filter(prompt_profile_version_id=target.id).delete()
            target.delete()
        Binding.objects.filter(key=migration.CURRENT_BINDING_KEY).delete()

        source = Profile.objects.filter(
            profile_key=migration.PROFILE_KEY,
            version=migration.SOURCE_VERSION,
        ).first()
        if source is None:
            source = Profile.objects.create(
                profile_key=migration.PROFILE_KEY,
                version=migration.SOURCE_VERSION,
                name="MiniMax H3 Ref2VA v10 fixture",
                description="Server-authored literal fixture",
                model_family="minimax_h3",
                operation="video.generate",
                mode="Ref2VA",
                source_projection_key="classified_reference_context_ir_v1",
                source_projection_config={},
                official_reference="https://example.invalid/h3",
                channel_limits={
                    "positive_prompt": {
                        "max_chars": 7000,
                        "warn_at_chars": 6200,
                    },
                },
                review_state="ready",
                status="published",
                contract_schema_version=4,
                contract_size_bytes=2,
                contract_sha256="",
                published_at=timezone.now(),
            )
        else:
            Rule.objects.filter(profile_version_id=source.id).delete()
            Profile.objects.filter(pk=source.pk).update(
                name="MiniMax H3 Ref2VA v10 fixture",
                description="Server-authored literal fixture",
                model_family="minimax_h3",
                operation="video.generate",
                mode="Ref2VA",
                source_projection_key="classified_reference_context_ir_v1",
                source_projection_config={},
                official_reference="https://example.invalid/h3",
                channel_limits={
                    "positive_prompt": {
                        "max_chars": 7000,
                        "warn_at_chars": 6200,
                    },
                },
                review_state="ready",
                status="published",
                contract_schema_version=4,
                contract_size_bytes=2,
                contract_sha256="",
                published_at=timezone.now(),
                published_by_id=None,
            )
            source.refresh_from_db()

        specs = (
            (
                "subject_definitions",
                "request.h3_ref2va.subject_definitions",
                "label_block",
            ),
            ("summary", "request.h3_ref2va.summary", "label_colon"),
            (
                "retention_analysis",
                "request.h3_ref2va.retention_analysis",
                "label_block",
            ),
            (
                "detailed_description",
                "request.h3_ref2va.detailed_description",
                "label_block",
            ),
            (
                "overall_soundscape",
                "request.h3_ref2va.overall_soundscape",
                "label_colon",
            ),
            (
                "non_diegetic_music",
                "request.h3_ref2va.non_diegetic_music",
                "label_colon",
            ),
        )
        generated_instructions = {
            "detailed_description": previous.DETAIL_GENERATION_INSTRUCTION,
            "overall_soundscape": (
                "Summarize only synchronized physical sound and diegetic ambience "
                "supported by the frozen shot facts."
            ),
            "non_diegetic_music": (
                "Describe only non-diegetic music supported by the frozen shot facts."
            ),
        }
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
                    "is_required": True,
                    "is_active": True,
                },
            )
            model_generated = key in generated_instructions
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
                generation_instruction=generated_instructions.get(key, ""),
                generation_output_semantics=(
                    "contribution" if model_generated else "final"
                ),
                generation_required=model_generated,
                language_policy="en" if model_generated else "any",
                length_unit="english_words" if model_generated else "none",
                generation_min=(
                    350
                    if key == "detailed_description"
                    else (1 if model_generated else None)
                ),
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
                    if model_generated
                    else ["protected_tokens_preserve_count"]
                ),
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
        binding = Binding.objects.create(
            key=migration.CURRENT_BINDING_KEY,
            name=migration.SOURCE_BINDING_NAME,
            operation=source.operation,
            model_family=source.model_family,
            mode=source.mode,
            prompt_profile_version_id=source.id,
            priority=200,
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
            for rule in source_rules
        }

    def test_v11_keeps_the_official_budget_and_server_owned_authored_literals(self):
        Profile = self.apps.get_model("studio", "PromptProfileVersion")
        Rule = self.apps.get_model("studio", "PromptProfileFieldRule")
        Binding = self.apps.get_model("studio", "PromptProfileBinding")
        migration = importlib.import_module(
            "studio.migrations.0085_prompt_profile_official_detail_budget",
        )

        source = Profile.objects.get(pk=self.source_id)
        target = Profile.objects.get(
            profile_key=migration.PROFILE_KEY,
            version=migration.TARGET_VERSION,
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
            if key == migration.DETAIL_FIELD_KEY:
                self.assertEqual(
                    target_rule.generation_instruction,
                    migration.DETAIL_GENERATION_INSTRUCTION,
                )
                self.assertEqual(target_rule.length_unit, "english_words")
                self.assertEqual(target_rule.generation_min, 350)
                self.assertEqual(target_rule.generation_max, 500)
                self.assertIn(
                    "A single shot does not justify a shorter description",
                    target_rule.generation_instruction,
                )
                self.assertIn(
                    "count the English words and revise",
                    target_rule.generation_instruction,
                )
                self.assertIn(
                    "deterministic server projection appends",
                    target_rule.generation_instruction,
                )
                for raw_tag in (
                    "<d>", "</d>", "<lyrics>", "</lyrics>",
                    "<screen_text>", "</screen_text>",
                ):
                    self.assertNotIn(raw_tag, target_rule.generation_instruction)
            else:
                self.assertEqual(
                    target_rule.generation_instruction,
                    source_rule.generation_instruction,
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
        self.assertEqual(binding.name, migration.TARGET_BINDING_NAME)
        self.assertTrue(binding.is_active)

        migration.publish_official_detail_budget_successor(self.apps, None)
        self.assertEqual(Profile.objects.filter(
            profile_key=migration.PROFILE_KEY,
            version=migration.TARGET_VERSION,
        ).count(), 1)
        self.assertEqual(
            Binding.objects.get(pk=self.binding_id).prompt_profile_version_id,
            target.id,
        )

        executor = MigrationExecutor(connection)
        executor.migrate([("studio", self.migrate_from)])
        reverse_apps = executor.loader.project_state([
            ("studio", self.migrate_from),
        ]).apps
        Profile = reverse_apps.get_model("studio", "PromptProfileVersion")
        Binding = reverse_apps.get_model("studio", "PromptProfileBinding")
        source = Profile.objects.get(pk=self.source_id)
        target = Profile.objects.get(
            profile_key=migration.PROFILE_KEY,
            version=migration.TARGET_VERSION,
        )
        binding = Binding.objects.get(pk=self.binding_id)
        self.assertEqual(binding.prompt_profile_version_id, source.id)
        self.assertEqual(binding.name, migration.SOURCE_BINDING_NAME)
        self.assertTrue(binding.is_active)
        self.assertEqual(target.status, "published")

        executor = MigrationExecutor(connection)
        executor.migrate([("studio", self.migrate_to)])
        forward_apps = executor.loader.project_state([
            ("studio", self.migrate_to),
        ]).apps
        Profile = forward_apps.get_model("studio", "PromptProfileVersion")
        Binding = forward_apps.get_model("studio", "PromptProfileBinding")
        target = Profile.objects.get(
            profile_key=migration.PROFILE_KEY,
            version=migration.TARGET_VERSION,
        )
        binding = Binding.objects.get(pk=self.binding_id)
        self.assertEqual(binding.prompt_profile_version_id, target.id)
        self.assertEqual(binding.name, migration.TARGET_BINDING_NAME)
        self.assertEqual(Profile.objects.filter(
            profile_key=migration.PROFILE_KEY,
            version=migration.TARGET_VERSION,
        ).count(), 1)
