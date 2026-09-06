from __future__ import annotations

from django.core.exceptions import ValidationError
from django.db import connection
from django.db.migrations.executor import MigrationExecutor

from studio.tests.test_configuration_migrations import _MigrationTestCase


class PromptProfileGenerationInstructionLimitMigrationTests(_MigrationTestCase):
    migrate_from = "0100_prompt_profile_generation_instruction_budget"
    migrate_to = "0101_remove_prompt_profile_generation_instruction_limit"

    def set_up_before_migration(self, apps):
        Profile = apps.get_model("studio", "PromptProfileVersion")
        Definition = apps.get_model("studio", "PromptFieldDefinition")
        Rule = apps.get_model("studio", "PromptProfileFieldRule")
        profile = Profile.objects.create(
            profile_key="qa.migration.long-generation-instruction",
            version=99,
            name="QA long generation instruction",
            model_family="qa",
            operation="prompt.normalize",
            mode="",
            source_projection_key="direct_v1",
            contract_schema_version=5,
        )
        definition = Definition.objects.create(
            key="qa_migration_long_generation_instruction",
            label="QA migration long generation instruction",
            category="qa",
            description="Migration-only fixture",
            impact="Migration-only fixture",
            value_type="long_text",
            value_options=[],
            is_active=True,
        )
        instruction = "Use frozen facts only. " + ("A" * 4_500)
        self.rule_id = Rule.objects.create(
            profile_version=profile,
            field_definition=definition,
            source_selector="request.text",
            channel="positive_prompt",
            position=1,
            label_style="label_block",
            separator="newline",
            required=True,
            omit_if_empty=False,
            max_chars=7_000,
            generation_mode="model_generated",
            generation_source_selector="shot.detailed_description",
            generation_instruction=instruction,
            generation_output_semantics="final",
            generation_required=True,
            language_policy="en",
            length_unit="none",
            generation_validator_keys=["non_empty"],
            generation_context_selectors=[],
        ).id
        self.instruction = instruction

    @staticmethod
    def _instruction_limits(apps):
        field = apps.get_model(
            "studio",
            "PromptProfileFieldRule",
        )._meta.get_field("generation_instruction")
        return [
            validator.limit_value
            for validator in field.validators
            if hasattr(validator, "limit_value")
        ]

    def test_forward_removes_only_the_arbitrary_field_validator(self):
        Rule = self.apps.get_model("studio", "PromptProfileFieldRule")
        rule = Rule.objects.get(pk=self.rule_id)

        self.assertNotIn(4_000, self._instruction_limits(self.apps))
        self.assertEqual(rule.generation_instruction, self.instruction)
        rule.full_clean()

    def test_reverse_restores_the_previous_validator_without_losing_data(self):
        executor = MigrationExecutor(connection)
        executor.migrate([("studio", self.migrate_from)])
        old_apps = executor.loader.project_state([
            ("studio", self.migrate_from),
        ]).apps
        Rule = old_apps.get_model("studio", "PromptProfileFieldRule")
        rule = Rule.objects.get(pk=self.rule_id)

        self.assertIn(4_000, self._instruction_limits(old_apps))
        self.assertEqual(rule.generation_instruction, self.instruction)
        with self.assertRaises(ValidationError):
            rule.full_clean()
