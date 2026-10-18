"""Owner-neutral rendering: no project/shot rows, database or external AI."""
from copy import deepcopy
from dataclasses import replace
from unittest.mock import patch
from django.core.exceptions import ValidationError

from django.test import SimpleTestCase

from studio.services.reference_rendering_inputs import UNIT_RENDER_SLOTS
from studio.services.prompt_compiler_contract import PromptBuildError
from studio.services.reference_context import compile_reference_context
from studio.services.reference_context_facts import (
    ReferenceContextFacts, ReferenceSubjectFacts, ReferenceUnitFacts,
    freeze_reference_context_facts, read_reference_context_facts,
)
from studio.services.json_contracts import json_contract
from studio.tests.reference_rendering_fixtures import rendering_config


class ReferenceContextFactTests(SimpleTestCase):
    def setUp(self):
        guard = patch("studio.services.reference_context.boundary_int", return_value=65536)
        guard.start()
        self.addCleanup(guard.stop)
        self.policy = rendering_config()["primitive_options"]["reference_context_ir_v1"]["rendering"]
        self.unit = ReferenceUnitFacts(
            unit_key="asset-request-unit", duration_ms=1000,
            render_values={**{key: "" for key in UNIT_RENDER_SLOTS},
                          "description": "A red object turns slowly.", "subjects": "red object"},
            subject_keys=(), dialogue=(), voice_profiles=(), continuity_notes="",
            requires_first_frame_continuity=False, requires_last_frame_continuity=False, ambient_sound="",
        )
        self.context = ReferenceContextFacts(
            prompt_type="image", mode="explicit-mode", include_audio=False, language="English",
            ordered_units=(self.unit,), subject_facts=(), reference_bindings=(),
            conditioning_mode_contract={"key": "explicit-mode"}, frame_policy=None,
            color_script="", extra_direction="", overall_soundscape="", non_diegetic_music="",
            generation_contributions={},
        )

    def test_asset_unit_renders_without_fake_project_or_shot(self):
        result = compile_reference_context(self.context, self.policy)
        self.assertIn("A red object turns slowly.", str(result["fields"]))
        self.assertIn("explicit-mode", result["fields"]["summary"])
        self.assertEqual(result["consumed_contributions"], frozenset())
        self.assertFalse(hasattr(self.context, "project"))
        self.assertFalse(hasattr(self.unit, "episode"))

    def test_image_can_render_explicitly_without_a_video_conditioning_mode(self):
        context = replace(self.context, mode="", conditioning_mode_contract=None)
        rendered = compile_reference_context(context, self.policy)
        self.assertEqual(rendered["execution_facts"]["effective_conditioning_mode"], "")
        self.assertIn("A red object turns slowly.", str(rendered["fields"]))
        self.assertIsNone(context.conditioning_mode_contract)

    def test_no_mode_image_branch_does_not_relax_video_or_unknown_mode_validation(self):
        for changes in ({"prompt_type": "video", "mode": "", "conditioning_mode_contract": None},
                {"mode": "unregistered", "conditioning_mode_contract": None},
                {"mode": "", "conditioning_mode_contract": {"key": "other"}}):
            with self.subTest(changes=changes), self.assertRaises(PromptBuildError):
                compile_reference_context(replace(self.context, **changes), self.policy)

    def test_artifact_reference_is_not_described_as_an_asset_and_scope_is_explicit(self):
        reference = {"category": "prop", "entity_id": None, "entity_label": "",
            "input_label": "<Picture 1>", "purpose": "", "artifact_id": "actual-artifact-id", "asset_id": None,
            "description": "selected production output", "prompt_hint": "", "unit_keys": [self.unit.unit_key]}
        context = replace(self.context, reference_bindings=(reference,))
        policy = deepcopy(self.policy)
        policy["templates"]["reference"] = "{label}: {source_kind}; {description}"
        self.assertIn("generated_artifact", str(compile_reference_context(context, policy)["fields"]))
        for members in ([], ["unknown"], [self.unit.unit_key, self.unit.unit_key], "all"):
            with self.subTest(members=members), self.assertRaises(PromptBuildError):
                compile_reference_context(replace(context, reference_bindings=({**reference, "unit_keys": members},)), policy)

    def test_persisted_json_round_trip_keeps_the_same_rendering_and_unit_identities(self):
        subject = ReferenceSubjectFacts("external", "request-subject", ("blue paper", "angular shape"))
        context = replace(self.context, subject_facts=(subject,),
            ordered_units=(replace(self.unit, subject_keys=(("external", "request-subject"),)),))
        frozen = freeze_reference_context_facts(context, max_bytes=65536)
        self.assertIsInstance(frozen.value["context"]["ordered_units"], list)
        self.assertEqual(frozen.value["context"]["ordered_units"][0]["subject_keys"], [["external", "request-subject"]])
        restored = read_reference_context_facts(frozen, max_bytes=65536)
        self.assertEqual(restored, context)
        self.assertEqual(compile_reference_context(restored, self.policy), compile_reference_context(context, self.policy))
        restored.ordered_units[0].render_values["action"] = "Changed local copy"
        self.assertNotIn("Changed local copy", str(frozen.value))

    def test_reference_snapshot_does_not_accept_contributions_or_fill_missing_fields(self):
        with self.assertRaises(PromptBuildError):
            freeze_reference_context_facts(replace(self.context, generation_contributions={(self.unit.unit_key, "shot.detailed_description"): "Approved?"}), max_bytes=65536)
        frozen = freeze_reference_context_facts(self.context, max_bytes=65536)
        for key in ("frame_policy", "subject_facts", "include_audio"):
            value = deepcopy(frozen.value)
            del value["context"][key]
            with self.subTest(key=key), self.assertRaises(PromptBuildError):
                read_reference_context_facts(json_contract(value, max_bytes=65536), max_bytes=65536)

    def test_reference_snapshot_rechecks_semantics_checksum_and_size(self):
        frozen = freeze_reference_context_facts(self.context, max_bytes=65536)
        value = deepcopy(frozen.value)
        value["context"]["ordered_units"][0]["duration_ms"] = 0
        with self.assertRaises(PromptBuildError):
            read_reference_context_facts(json_contract(value, max_bytes=65536), max_bytes=65536)
        with self.assertRaises(ValidationError):
            read_reference_context_facts(frozen, max_bytes=10)
        frozen.value["context"]["mode"] = "changed"
        with self.assertRaises(ValidationError):
            read_reference_context_facts(frozen, max_bytes=65536)

    def test_reference_and_reviewed_text_share_the_explicit_unit_identity(self):
        subject = ReferenceSubjectFacts("prop", "owned-subject", ("bright red", "metal shell"))
        reference = {"category": "prop", "entity_id": "owned-subject", "entity_label": "<Subject 1>",
                     "input_label": "<Picture 1>", "prompt_hint": "Preserve red finish."}
        context = replace(self.context, subject_facts=(subject,),
            ordered_units=(replace(self.unit, subject_keys=(("prop", "owned-subject"),)),),
            reference_bindings=(reference,), generation_contributions={(self.unit.unit_key, "shot.detailed_description"): "Reviewed rotation."})
        before = deepcopy(context)
        result = compile_reference_context(context, self.policy)
        text = str(result["fields"])
        for value in ("bright red", "metal shell", "<Picture 1>", "<Subject 1>", "Reviewed rotation."):
            self.assertIn(value, text)
        self.assertNotIn("A red object turns slowly.", text)
        self.assertEqual(result["consumed_contributions"], frozenset(context.generation_contributions))
        self.assertEqual(context, before)

    def test_missing_fact_is_not_silently_replaced_with_empty_text(self):
        source = dict(self.unit.render_values)
        del source["camera"]
        with self.assertRaisesMessage(PromptBuildError, "映射后的渲染值"):
            compile_reference_context(replace(self.context, ordered_units=(replace(self.unit, render_values=source),)), self.policy)

    def test_duplicate_unit_or_subject_and_unknown_subject_are_rejected(self):
        subject = ReferenceSubjectFacts("prop", "subject", ("facts",))
        for context in (
            replace(self.context, ordered_units=(self.unit, self.unit)),
            replace(self.context, subject_facts=(subject, subject)),
            replace(self.context, ordered_units=(replace(self.unit, subject_keys=(("prop", "missing"),)),)),
        ):
            with self.subTest(context=context), self.assertRaises(PromptBuildError):
                compile_reference_context(context, self.policy)

    def test_reviewed_contribution_cannot_cross_request_unit(self):
        with self.assertRaisesMessage(PromptBuildError, "当前请求单元"):
            compile_reference_context(replace(self.context, generation_contributions={("other-unit", "shot.detailed_description"): "wrong"}), self.policy)

    def test_rendering_rejects_orm_shaped_input_instead_of_using_a_fallback(self):
        with self.assertRaisesMessage(PromptBuildError, "事实值对象"):
            compile_reference_context({"project": "fake", "shots": []}, self.policy)

    def test_malformed_fact_containers_fail_explicitly_not_with_python_type_errors(self):
        for changes in ({"ordered_units": None}, {"subject_facts": "subject"}, {"reference_bindings": ["file"]},
                        {"frame_policy": []}, {"subject_facts": [ReferenceSubjectFacts([], "id", ())]},
                        {"subject_facts": [ReferenceSubjectFacts("prop", "id", "not-a-list")]},
                        {"ordered_units": (replace(self.unit, subject_keys=(["prop", "id"],)),)},
                        {"ordered_units": (replace(self.unit, voice_profiles="not-a-list"),)},
                        {"ordered_units": (replace(self.unit, dialogue=None),)}):
            with self.subTest(changes=changes), self.assertRaises(PromptBuildError):
                compile_reference_context(replace(self.context, **changes), self.policy)

    def test_explicit_voiceover_uses_the_same_configured_dialogue_template(self):
        dialogue = {"speaker_id": "speaker", "text": "Look closer.", "delivery": "quietly", "subject": "",
                    "voice_profile": "clear voice", "voiceover": True, "start_ms": 0, "end_ms": 900, "language": "English"}
        context = replace(self.context, include_audio=True, ordered_units=(replace(self.unit, dialogue=(dialogue,)),))
        self.assertIn("Look closer.", str(compile_reference_context(context, self.policy)["fields"]))
        for entry in ({**dialogue, "end_ms": 1200}, {**dialogue, "start_ms": True}, {**dialogue, "voiceover": "yes"}):
            with self.assertRaises(PromptBuildError):
                compile_reference_context(replace(context, ordered_units=(replace(self.unit, dialogue=(entry,)),)), self.policy)
        with self.assertRaisesMessage(PromptBuildError, "不能静默丢弃"):
            compile_reference_context(replace(context, include_audio=False), self.policy)
