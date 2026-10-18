from copy import deepcopy
from dataclasses import asdict
import json

from django.test import SimpleTestCase

from studio.services.execution_reference_inputs import reference_context_input_schema, project_reference_unit_input
from studio.services.prompt_compiler_contract import PromptBuildError
from studio.services.reference_context import compile_reference_context
from studio.services.reference_context_facts import ReferenceSubjectFacts
from studio.tests import test_reference_context_facts as reference_fixtures


class ExecutionReferenceInputTests(SimpleTestCase):
    def setUp(self):
        reference_fixtures.ReferenceContextFactTests.setUp(self)
        self.value = {"schema_version": 1, "ordered_units": [json.loads(json.dumps(asdict(self.unit)))],
            "color_script": "Muted blue and amber", "extra_direction": "", "overall_soundscape": "", "non_diegetic_music": ""}
        self.value["ordered_units"][0].pop("render_values")

    def project(self, value=None, **changes):
        return project_reference_unit_input(value if value is not None else self.value, **{
            "prompt_type": "image", "mode": "explicit-mode", "include_audio": False, "language": "English",
            "subject_facts": (), "reference_bindings": (), "conditioning_mode_contract": {"key": "explicit-mode"},
            "frame_policy": None, "max_bytes": 65536, "unit_render_values": {self.unit.unit_key: self.unit.render_values}, **changes})

    def test_literal_asset_unit_uses_the_existing_renderer_without_a_fake_model(self):
        before = deepcopy(self.value)
        context = self.project()
        self.assertEqual(context.ordered_units[0].unit_key, self.unit.unit_key)
        self.assertIn("A red object turns slowly.", str(compile_reference_context(context, self.policy)))
        self.assertEqual(self.value, before)
        self.assertFalse(hasattr(context, "project"))

    def test_form_schema_contains_only_editable_facts_without_saved_defaults(self):
        schema = reference_context_input_schema()
        self.assertEqual(set(schema["properties"]), set(self.value))
        self.assertEqual(set(schema["properties"]["ordered_units"]["items"]["properties"]), set(self.value["ordered_units"][0]))
        for key in ("default", "reference_bindings", "include_audio", "mode", "generation_contributions"):
            self.assertNotIn(f'"{key}":', json.dumps(schema))

    def test_extra_internal_facts_missing_sources_and_bad_dialogue_do_not_pass_through(self):
        cases = [{**self.value, "include_audio": True}, {**self.value, "reference_bindings": []}, {**self.value, "generation_contributions": {}}]
        missing = deepcopy(self.value)
        del missing["ordered_units"][0]["duration_ms"]
        cases.append(missing)
        extra = deepcopy(self.value)
        extra["ordered_units"][0]["source_facts"] = {"shot.action": "hidden"}
        cases.append(extra)
        derived = deepcopy(self.value)
        derived["ordered_units"][0]["render_values"] = self.unit.render_values
        cases.append(derived)
        for value in cases:
            with self.subTest(value=value), self.assertRaises(PromptBuildError):
                self.project(value)

    def test_unit_subjects_must_match_resolved_identity_and_audio_is_not_silently_dropped(self):
        self.value["ordered_units"][0]["subject_keys"] = [["external", "subject-one"]]
        with self.assertRaises(PromptBuildError):
            self.project()
        self.project(subject_facts=[ReferenceSubjectFacts("external", "subject-one", ("red object",))])
        self.value["ordered_units"][0]["subject_keys"] = []
        self.value["ordered_units"][0]["dialogue"] = [{"speaker_id": "", "text": "Hello", "delivery": "soft", "subject": "Narrator",
            "voice_profile": "calm", "voiceover": True, "start_ms": 0, "end_ms": 1000}]
        with self.assertRaises(PromptBuildError):
            self.project()
        self.project(include_audio=True)

    def test_exact_speaker_identity_is_an_optional_authored_key_not_a_rendered_label(self):
        line = dict(speaker_id="voice", text="Hello", delivery="soft", subject="Narrator", voice_profile="calm",
                    voiceover=True, start_ms=0, end_ms=1000, speaker_subject_key=["character", "subject-one"])
        self.value["ordered_units"][0]["dialogue"] = [line]
        result = self.project(include_audio=True)
        self.assertEqual(result.ordered_units[0].dialogue[0]["speaker_subject_key"], ["character", "subject-one"])
        schema = reference_context_input_schema()["properties"]["ordered_units"]["items"]["properties"]["dialogue"]["items"]
        self.assertIn("speaker_subject_key", schema["properties"])
        self.assertNotIn("speaker_subject_key", schema["required"])
        for key in ([], ["character"], ["character", ""], [1, "subject-one"], "character:subject-one"):
            with self.subTest(key=key), self.assertRaises(PromptBuildError):
                self.value["ordered_units"][0]["dialogue"] = [dict(line, speaker_subject_key=key)]
                self.project(include_audio=True)
        self.value["ordered_units"][0]["dialogue"] = [dict(line, speaker_reference_label="<Subject 1>")]
        with self.assertRaises(PromptBuildError):
            self.project(include_audio=True)
