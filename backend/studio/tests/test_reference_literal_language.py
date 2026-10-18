"""Literal Bible/reference facts are not generated-language targets."""
from copy import deepcopy
from dataclasses import replace
from uuid import uuid4

from django.contrib.auth import get_user_model
from django.test import TestCase

from studio.models import PromptProfileVersion
from studio.services.prompt_profile_compiler import compile_prompt_profile_facts, prompt_profile_facts_fingerprint
from studio.services.prompt_profiles import PromptProfileError, publish_prompt_profile, refresh_prompt_profile_contract
from studio.services.reference_context_facts import ReferenceSubjectFacts
from studio.tests.field_definition_fixtures import create_field_definition, create_profile_field_rule
from studio.tests.reference_rendering_fixtures import (
    generation_structure_fixture, profile_compilation_fixture, reference_context_fixture, reference_unit_fixture, rendering_config,
)


class ReferenceLiteralLanguageTests(TestCase):
    def setUp(self):
        self.actor = get_user_model().objects.create_user(username="literal-language")
        self.name = "林夏 / Alex · 橙夹克🧥 {原名}"
        self.context = reference_context_fixture(
            ordered_units=(reference_unit_fixture(subject_keys=(("character", "bible-original"),)),),
            subject_facts=(ReferenceSubjectFacts("character", "bible-original", ("保留原外观，不翻译。",)),),
            reference_bindings=({"category": "character", "entity_category": "character",
                "entity_id": "bible-original", "entity_label": self.name,
                "input_label": "<Picture1>", "purpose": "", "asset_id": "original-image"},),
        )

    def profile(self, policy, *, primitive="subject_definitions", templates=None, maximum=None, contribution=False):
        config = rendering_config()
        rendering = config["primitive_options"]["reference_context_ir_v1"]["rendering"]
        rendering["list_prefix"] = ""
        rendering["templates"]["entity_reference"] = "{subject} = {label}"
        if templates:
            rendering["templates"].update(templates)
        profile = PromptProfileVersion.objects.create(profile_key=f"literal-{uuid4().hex}", version=1,
            name="Literal language fixture", operation="video.generate", model_family="fixture",
            source_projection_key="declarative_v1", source_projection_config=config, review_state="ready")
        if contribution:
            generation_structure_fixture(profile)
        field = create_field_definition(key=f"literal_{uuid4().hex}", label="Literal reference", value_type="long_text")
        create_profile_field_rule(profile_version=profile, field_definition=field,
            source_selector="projection.output", projection_primitive=f"reference_context.{primitive}",
            generation_source_selector="request.structured_input" if contribution else "projection.output",
            generation_mode="model_generated" if contribution else "passthrough",
            generation_output_semantics="contribution" if contribution else "final",
            source_path=["direction"] if contribution else None,
            generation_instruction="Write creative direction." if contribution else "",
            language_policy=policy, required=True, generation_required=True,
            generation_validator_keys=["no_control_characters"], generation_min=1, generation_max=maximum,
            length_unit="characters", channel="positive_prompt", position=0, label_style="none", max_chars=7000)
        refresh_prompt_profile_contract(profile)
        return publish_prompt_profile(profile, actor=self.actor)

    def compile(self, profile, context=None):
        facts = profile_compilation_fixture(sources={}, reference_context=context or self.context)
        original = deepcopy(facts)
        fingerprint = prompt_profile_facts_fingerprint(facts)
        result = compile_prompt_profile_facts(profile, facts=facts)
        self.assertEqual(facts, original)
        self.assertEqual(prompt_profile_facts_fingerprint(facts), fingerprint)
        return result.assembly["channels"]["positive_prompt"]

    def test_english_reference_binding_preserves_multilingual_bible_name_verbatim(self):
        profile = self.profile("en")
        original_sha = profile.contract_sha256
        self.assertEqual(self.compile(profile), f"{self.name} = <Picture1>")
        profile.refresh_from_db()
        self.assertEqual(profile.contract_sha256, original_sha)

    def test_literal_only_binding_is_identical_for_every_language_policy(self):
        for policy in ("en", "zh-Hans", "bilingual:en+zh-Hans", "inherit", "preserve_source", "any", "ja"):
            with self.subTest(policy=policy):
                self.assertEqual(self.compile(self.profile(policy)), f"{self.name} = <Picture1>")

    def test_original_bible_description_is_not_a_translation_target(self):
        profile = self.profile("en", templates={"entity_reference": "Use {subject} = {label}: {description}"})
        self.assertEqual(self.compile(profile), f"Use {self.name} = <Picture1>: 保留原外观，不翻译。")

    def test_language_policy_still_rejects_authored_prose_even_when_it_matches_a_name(self):
        profile = self.profile("en", templates={"entity_reference": "林夏 {subject} = {label}"})
        with self.assertRaises(PromptProfileError) as failure:
            self.compile(profile)
        self.assertEqual(failure.exception.code, "prompt_profile_generation_language_invalid")

    def test_literal_names_still_count_toward_the_full_value_length_limit(self):
        profile = self.profile("en", maximum=8)
        with self.assertRaises(PromptProfileError) as failure:
            self.compile(profile)
        self.assertEqual(failure.exception.code, "prompt_profile_generation_value_length_invalid")

    def test_literal_names_do_not_bypass_control_character_validation(self):
        from dataclasses import replace
        bad = replace(self.context, reference_bindings=({**self.context.reference_bindings[0],
            "entity_label": self.name + "\u202e"},))
        with self.assertRaises(PromptProfileError) as failure:
            self.compile(self.profile("en"), bad)
        self.assertEqual(failure.exception.code, "prompt_profile_generation_value_validator_failed")

    def test_nested_reference_usage_preserves_name_without_exempting_authored_body(self):
        profile = self.profile("en", primitive="summary", templates={
            "summary": "{task_types} {reference_relationships}",
            "reference_usage": "Use {subject} from {label}: {usage}",
        })
        self.assertIn(f"Use {self.name} from <Picture1>", self.compile(profile))

    def test_numeric_authored_projection_without_literal_provenance_is_not_exempt(self):
        from dataclasses import replace
        profile = self.profile("en", templates={"empty_references": "123/---"})
        with self.assertRaises(PromptProfileError) as failure:
            self.compile(profile, replace(self.context, reference_bindings=()))
        self.assertEqual(failure.exception.code, "prompt_profile_generation_language_invalid")

    def dialogue_context(self, text, *, voiceover=False, delivery="", description="The traveler turns."):
        unit = self.context.ordered_units[0]
        return replace(self.context, ordered_units=(replace(unit,
            render_values={**unit.render_values, "description": description},
            dialogue=({"speaker_id": "bible-original", "subject": self.name, "text": text, "language": "Chinese",
                "delivery": delivery, "voice_profile": "", "voiceover": voiceover,
                "start_ms": 0, "end_ms": 1000},)),))

    def test_original_dialogue_and_voiceover_survive_english_direction_verbatim(self):
        original = "等一下！ Café 🙂\r\n原句，不翻译。"
        profile = self.profile("en", primitive="detailed_description")
        for voiceover in (False, True):
            with self.subTest(voiceover=voiceover):
                context = self.dialogue_context(original, voiceover=voiceover)
                text = self.compile(profile, context)
                self.assertIn(original, text)
                self.assertIn("The traveler turns.", text)
                self.assertEqual("Off-screen voiceover" in text, voiceover)
                self.assertEqual(context.ordered_units[0].dialogue[0]["text"], original)

    def test_quoted_dialogue_does_not_exempt_matching_creative_direction_or_delivery(self):
        profile = self.profile("en", primitive="detailed_description")
        for changes in ({"description": "等一下"}, {"delivery": "等一下"}):
            with self.subTest(changes=changes), self.assertRaises(PromptProfileError) as failure:
                self.compile(profile, self.dialogue_context("等一下", **changes))
            self.assertEqual(failure.exception.code, "prompt_profile_generation_language_invalid")

    def test_original_dialogue_keeps_full_value_control_and_length_checks(self):
        for value, maximum, expected in (("等一下\u202e", None, "prompt_profile_generation_value_validator_failed"),
                ("原文" * 300, 400, "prompt_profile_generation_value_length_invalid")):
            with self.subTest(expected=expected), self.assertRaises(PromptProfileError) as failure:
                self.compile(self.profile("en", primitive="detailed_description", maximum=maximum),
                    self.dialogue_context(value))
            self.assertEqual(failure.exception.code, expected)

    def test_generated_contribution_matching_original_dialogue_is_not_language_exempt(self):
        original = "等一下！"
        context = self.dialogue_context(original)
        profile = self.profile("en", primitive="detailed_description", contribution=True)
        facts = profile_compilation_fixture(sources={}, reference_context=context,
            generation_contributions={(context.ordered_units[0].unit_key, "shot.detailed_description"): original})
        before = deepcopy(facts)
        with self.assertRaises(PromptProfileError) as failure:
            compile_prompt_profile_facts(profile, facts=facts)
        self.assertEqual(failure.exception.code, "prompt_profile_generation_language_invalid")
        self.assertEqual(facts, before)
