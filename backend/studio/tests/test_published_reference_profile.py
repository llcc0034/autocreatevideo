from studio.services.reference_rendering_inputs import UNIT_RENDER_SLOTS
from studio.tests.field_definition_fixtures import get_or_create_field_definition
"""Complete schema-8 reference configuration, created only in an isolated DB.

This synthetic fixture is not evidence about any deployed policy or migration.
Media rows are renderer facts, not live media/Smoke evidence.
Reviewed contributions are explicit test inputs, not claimed model output.
"""
from copy import deepcopy
from dataclasses import asdict, replace
import json
from pathlib import Path

from django.test import TestCase

from studio.tests.field_definition_fixtures import create_profile_field_rule
from studio.models import FieldDefinition, PromptProfileFieldRule, PromptProfileVersion, SeriesProject, Shot
from studio.services.execution_reference_inputs import project_reference_unit_input
from studio.services.prompt_compiler_contract import PromptBuildError
from studio.services.prompt_profile_compiler import PromptProfileCompilationFacts, compile_prompt_profile_facts
from studio.services.prompt_profiles import PromptProfileError, publish_prompt_profile, refresh_prompt_profile_contract, verify_prompt_profile_contract
from studio.services.reference_context_facts import (
    ReferenceContextFacts, ReferenceSubjectFacts, ReferenceUnitFacts,
    freeze_reference_context_facts, read_reference_context_facts,
)
from studio.tests import test_prompt_profiles as profile_fixture


def publish_full_reference_fixture(actor):
    payload = json.loads(Path(__file__).with_name("fixtures").joinpath("reference_profile_schema8.json").read_text(encoding="utf-8"))
    values = {key: deepcopy(value) for key, value in payload.items() if key not in {"rules", "schema_version"}}
    profile = PromptProfileVersion.objects.create(**values, name="Isolated complete reference policy",
        review_state="ready", contract_schema_version=payload["schema_version"])
    from studio.tests.reference_rendering_fixtures import generation_structure_fixture
    from studio.services.data_structures import data_structure_snapshot
    generation_structure_fixture(profile, properties=("visual", "sfx", "music"))
    payload["input_structure"] = data_structure_snapshot(profile.input_structure_version)
    for frozen in payload["rules"]:
        # Schema seeds contain common dictionary keys. Exact fixture facts are
        # restored only inside isolated tests, never against runtime data.
        field, _ = get_or_create_field_definition(key=frozen["field_key"], defaults={
            "label": frozen["field_key"], "value_type": frozen["field_value_type"],
            "value_options": frozen["field_value_options"]})
        rule = create_profile_field_rule(profile_version=profile, field_definition=field,
            **{key: deepcopy(value) for key, value in frozen.items() if not key.startswith("field_")})
        from studio.services.field_definitions import field_definition_snapshot
        frozen["field_definition"] = field_definition_snapshot(rule.field_definition_version)
    refresh_prompt_profile_contract(profile)
    return payload, publish_prompt_profile(profile, actor=actor)


class PublishedReferenceProfileTests(TestCase):
    def setUp(self):
        profile_fixture.PromptProfileServiceTests.setUp(self)
        self.payload, self.profile = publish_full_reference_fixture(self.actor)
        subject = ReferenceSubjectFacts("character", "courier", ("A courier in a blue jacket.",))
        first = ReferenceUnitFacts(unit_key="first", duration_ms=5000,
            render_values={**{key: "" for key in UNIT_RENDER_SLOTS},
                "visual_prompt": "Natural light and restrained camera motion.",
                "narrative": "The courier brings a parcel.", "description": "The courier holds the parcel.",
                "action": "The courier places the parcel on the table.", "subjects": "Courier"},
            subject_keys=(("character", "courier"),), dialogue=({"speaker_id": "courier", "subject": "Courier",
                "text": "包裹到了。", "delivery": "calm", "voice_profile": "clear voice", "voiceover": False,
                "start_ms": 700, "end_ms": 1900},), voice_profiles=("clear voice",), continuity_notes="Parcel stays on the table.",
            requires_first_frame_continuity=False, requires_last_frame_continuity=False, ambient_sound="Room tone.")
        second = replace(first, unit_key="second", duration_ms=3000, dialogue=(),
            render_values={**first.render_values, "description": "The courier opens the parcel."})
        self.context = ReferenceContextFacts(prompt_type="video", mode="Ref2VA", include_audio=True, language="Chinese",
            ordered_units=(first, second), subject_facts=(subject,), reference_bindings=(
                {"category": "character", "entity_id": "courier", "entity_label": "<Subject 1>", "purpose": "",
                    "input_label": "<Picture 1>", "asset_id": "fixture-image", "unit_keys": ["first", "second"], "prompt_hint": "Preserve the jacket."},
                {"category": "video", "entity_id": None, "entity_label": "", "purpose": "motion_camera_reference",
                    "input_label": "<Video 1>", "asset_id": "fixture-video", "description": "A slow camera move.",
                    "prompt_hint": "Follow the lateral camera motion at table height.", "unit_keys": ["second"],
                    "audio_input_label": "<Audio 1>", "audio_purpose": "audio_reference", "use_embedded_audio": False,
                    "audio_description": "Paired room tone.", "audio_prompt_hint": "Match the ambience."},
                {"category": "audio", "entity_id": None, "entity_label": "", "purpose": "audio_reuse",
                    "input_label": "<Audio 2>", "asset_id": "fixture-audio", "description": "A short sound cue.",
                    "prompt_hint": "Use this sound cue once.", "unit_keys": ["first"]},
            ), conditioning_mode_contract={"key": "Ref2VA"}, frame_policy={"fps": 24, "minimum_frames": 5,
                "maximum_frames": 3600, "alignment_modulus": 17, "alignment_remainder": 5}, color_script="Muted blue and amber.",
            extra_direction="Preserve the parcel's position.", overall_soundscape="Quiet room.", non_diegetic_music="Soft sustained notes.",
            generation_contributions={})
        self.contributions = {(key, source): value for key, description in (
            ("first", "The courier lowers the parcel onto the table."), ("second", "The courier opens the parcel on the table."))
            for source, value in (("shot.detailed_description", description), ("shot.sound_effects", "The parcel touches the tabletop."),
                ("shot.music", "A soft sustained note accompanies the movement."))}

    def compile(self, context=None, contributions=None):
        return compile_prompt_profile_facts(self.profile, facts=PromptProfileCompilationFacts(sources={},
            source_language="Chinese", target_language="English", reference_context=context or self.context,
            reviewed_values={}, generation_contributions=self.contributions if contributions is None else contributions))

    def test_all_six_rules_and_complete_rendering_are_the_verified_published_contract(self):
        self.assertEqual(verify_prompt_profile_contract(self.profile), self.payload)
        result = self.compile()
        fields = result.assembly["structured_fields"]
        keys = [rule["field_key"] for rule in self.payload["rules"]]
        self.assertEqual(list(fields), keys)
        text = result.assembly["channels"]["positive_prompt"]
        for key in keys:
            self.assertTrue(fields[key], key)
        self.assertIn("2 shots over 8 seconds", fields["summary"])
        self.assertIn("4 supplied references", fields["summary"])
        self.assertIn("paired video=<Video 1>", fields["subject_definitions"])
        self.assertIn("<Audio 2>", text)
        self.assertLess(text.index("[Shot 1]"), text.index("[Shot 2]"))
        self.assertIn("700–1900", fields["detailed_description"])
        self.assertEqual(fields["detailed_description"].count("包裹到了。"), 1)
        self.assertIn("(shots 2)", fields["retention_analysis"])
        self.assertEqual(len(result.generation_length_audit), 9)  # Three locked sections, six per-unit contributions.
        self.assertEqual(sum("unit_key" in row for row in result.generation_length_audit), 6)
        self.assertLessEqual(len(text), 7000)
        self.assertEqual(result.model_prompt_contract["format"], "reference_context_ir_v2")

    def test_structured_neutral_input_and_frozen_round_trip_keep_the_same_full_compilation(self):
        counts = (SeriesProject.objects.count(), Shot.objects.count())
        value = {"schema_version": 1, "ordered_units": json.loads(json.dumps([asdict(unit) for unit in self.context.ordered_units])),
            **{key: getattr(self.context, key) for key in ("color_script", "extra_direction", "overall_soundscape", "non_diegetic_music")}}
        render_values = {unit["unit_key"]: unit.pop("render_values") for unit in value["ordered_units"]}
        neutral = project_reference_unit_input(value, **{key: getattr(self.context, key) for key in (
            "prompt_type", "mode", "include_audio", "language", "subject_facts", "reference_bindings", "conditioning_mode_contract", "frame_policy")}, max_bytes=65536, unit_render_values=render_values)
        frozen = freeze_reference_context_facts(neutral, max_bytes=65536)
        restored = read_reference_context_facts(frozen, max_bytes=65536)
        self.assertEqual(self.compile(), self.compile(restored))
        self.assertEqual(counts, (SeriesProject.objects.count(), Shot.objects.count()))

    def test_unknown_purpose_and_out_of_unit_dialogue_are_not_silently_rewritten(self):
        refs = deepcopy(self.context.reference_bindings)
        refs[1]["purpose"] = "not-in-published-config"
        with self.assertRaises((PromptBuildError, PromptProfileError)):
            self.compile(replace(self.context, reference_bindings=refs))
        first = self.context.ordered_units[0]
        dialogue = {**first.dialogue[0], "start_ms": 45000, "end_ms": 50000}
        with self.assertRaises((PromptBuildError, PromptProfileError)):
            self.compile(replace(self.context, ordered_units=(replace(first, dialogue=(dialogue,)), self.context.ordered_units[1])))

    def test_changed_upstream_facts_do_not_update_a_previously_frozen_input(self):
        frozen = freeze_reference_context_facts(self.context, max_bytes=65536)
        before = self.compile(read_reference_context_facts(frozen, max_bytes=65536))
        changed = replace(self.context, color_script="Explicit new green palette.")
        self.assertNotEqual(before, self.compile(changed))
        self.assertEqual(before, self.compile(read_reference_context_facts(frozen, max_bytes=65536)))
