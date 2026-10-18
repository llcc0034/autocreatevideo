from copy import deepcopy
from dataclasses import replace
from uuid import uuid4
from unittest.mock import patch

from django.test import SimpleTestCase

from studio.services.prompt_compiler_contract import PromptBuildError
from studio.services.production_linked_content import reviewed_linked_shot_value
from studio.services.reference_context import compile_reference_context, rendering_options
from studio.services.reference_context_facts import (
    ReferenceSubjectFacts, freeze_reference_context_facts, read_reference_context_facts,
)
from studio.tests.reference_rendering_fixtures import reference_context_fixture, reference_unit_fixture, rendering_config


class ReferenceDialogueBindingTests(SimpleTestCase):
    def setUp(self):
        guard = patch("studio.services.reference_context.boundary_int", return_value=65536)
        guard.start()
        self.addCleanup(guard.stop)
        self.first, self.second = str(uuid4()), str(uuid4())
        self.policy = rendering_config()["primitive_options"]["reference_context_ir_v1"]["rendering"]
        self.policy["reference_layout"] = {"subject_label_template": "<Actor {index}>", "source_separator": ", ",
            "summary_reference_template": "Use {label}.", "shot_reference_template": ""}
        self.policy["templates"]["entity_reference"] = "{label} is {subject} from {source_labels}."
        self.policy["templates"]["dialogue"] = "{speaker_reference_label} (V{speaker_index}) {start_timecode}-{end_timecode}: {text}"
        self.policy["templates"]["voiceover_dialogue"] = "Off-screen {speaker_reference_label} (V{speaker_index}): {text}"
        self.line = dict(speaker_id="stable-22", subject="同名", text="先别关门。", delivery="calmly", voice_profile="low",
            voiceover=False, start_ms=100, end_ms=1000, speaker_subject_key=["character", self.second])
        self.unit = reference_unit_fixture(unit_key="one", duration_ms=5000,
            subject_keys=(("character", self.first), ("character", self.second)), dialogue=(self.line,))
        self.context = reference_context_fixture(ordered_units=(self.unit,),
            subject_facts=tuple(ReferenceSubjectFacts("character", key, ()) for key in (self.first, self.second)),
            reference_bindings=tuple(dict(category="character", entity_category="character", entity_id=key,
                input_label=f"<Picture {index}>", subject="同名", purpose="")
                for index, key in enumerate((self.first, self.second), 1)))

    def rendered(self, context=None, policy=None):
        return compile_reference_context(context or self.context, policy or self.policy)

    def test_exact_identity_not_name_or_project_speaker_number_and_no_h3_spelling(self):
        before = deepcopy(self.context)
        result = self.rendered()
        self.assertIn("<Actor 2> (V1) 00:00.100-00:01.000: 先别关门。", str(result["fields"]))
        self.assertNotIn("language_fields", result)
        self.assertEqual(self.context, before)
        reordered = replace(self.context, reference_bindings=tuple(reversed(self.context.reference_bindings)))
        self.assertIn("<Actor 1> (V1)", str(self.rendered(reordered)["fields"]))

    def test_same_character_voiceover_preserves_binding_and_clip_voice_order(self):
        # Input list order is not voice order: timestamps own it.
        second_line = dict(self.line, speaker_id="stable-99", speaker_subject_key=["character", self.first],
                           text="等等。", start_ms=0, end_ms=100)
        unit = replace(self.unit, dialogue=(self.line, second_line))
        later = replace(self.unit, unit_key="two", dialogue=(dict(self.line, voiceover=True),))
        third = replace(self.unit, unit_key="three")
        output = str(self.rendered(replace(self.context, ordered_units=(unit, later, third)))["fields"])
        self.assertIn("<Actor 1> (V1)", output)
        self.assertIn("<Actor 2> (V2)", output)
        self.assertIn("Off-screen <Actor 2> (V2): 先别关门。", output)
        self.assertIn("00:10.100-00:11.000", output)

    def test_missing_wrong_out_of_unit_and_unreferenced_speaker_fail_when_label_requested(self):
        missing = {k: v for k, v in self.line.items() if k != "speaker_subject_key"}
        wrong = dict(self.line, speaker_subject_key=["scene", self.second])
        for entry in (missing, wrong):
            with self.subTest(entry=entry), self.assertRaisesMessage(PromptBuildError, "缺少本单元"):
                self.rendered(replace(self.context, ordered_units=(replace(self.unit, dialogue=(entry,)),)))
        for change in (
            {"reference_bindings": ()},
            {"ordered_units": (replace(self.unit, subject_keys=(("character", self.first),)),)},
            {"reference_bindings": tuple(dict(row, unit_keys=["two"]) for row in self.context.reference_bindings),
             "ordered_units": (self.unit, replace(self.unit, unit_key="two", dialogue=()))},
        ):
            with self.subTest(change=change), self.assertRaisesMessage(PromptBuildError, "缺少本单元"):
                self.rendered(replace(self.context, **change))

    def test_inconsistent_identity_or_voice_id_is_rejected(self):
        for extra in (dict(self.line, speaker_subject_key=["character", self.first]), dict(self.line, speaker_id="other")):
            with self.subTest(extra=extra), self.assertRaisesMessage(PromptBuildError, "关联冲突"):
                self.rendered(replace(self.context, ordered_units=(replace(self.unit, dialogue=(self.line, extra)),)))

    def test_unreferenced_narrator_uses_explicit_voice_policy_not_invented_subject(self):
        narrator = {k: v for k, v in self.line.items() if k != "speaker_subject_key"}
        narrator.update(voiceover=True, speaker_id="narrator", subject="Narrator")
        policy = deepcopy(self.policy)
        policy["templates"]["voiceover_dialogue"] = "{voice_profile} (V{speaker_index}) narrates: {text}"
        result = self.rendered(replace(self.context, ordered_units=(replace(self.unit, dialogue=(narrator,)),)), policy)
        self.assertIn("low (V1) narrates: 先别关门。", str(result["fields"]))

    def test_preparation_freezes_exact_identity_and_catalog_teaches_derived_fields(self):
        frozen = freeze_reference_context_facts(self.context, max_bytes=65536)
        restored = read_reference_context_facts(frozen, max_bytes=65536)
        self.assertEqual(restored.ordered_units[0].dialogue[0]["speaker_subject_key"], ["character", self.second])
        self.assertEqual(self.rendered(restored), self.rendered())
        for option in rendering_options():
            if option["key"] in {"dialogue", "voiceover_dialogue"}:
                for key in ("speaker_reference_label", "speaker_index", "start_timecode", "end_timecode"):
                    self.assertIn(key, option["placeholders"])
                    self.assertTrue(option["placeholder_help"][key]["description"])

    def test_production_join_carries_speaker_relation_not_visible_order_or_name(self):
        shot = {"scene_id": None, "prop_ids": [], "dialogue": [dict(self.line)], "character_relations": [
            {"role": "visible", "position": 0, "character_id": self.first},
            {"role": "speaker", "position": 0, "character_id": self.second},
        ]}
        # Authored/derived injection cannot override the exact reviewed relation.
        shot["dialogue"][0]["speaker_subject_key"] = ["character", self.first]
        dependencies = {"bible": {"canonical": {"characters": [
            {"id": key, "name": "同名", "voice_profile": "low", "stable_speaker_id": speaker}
            for key, speaker in ((self.first, "stable-1"), (self.second, "stable-22"))], "scenes": [], "props": []}}}
        result = reviewed_linked_shot_value("dialogue_with_speakers", shot, dependencies, run_id="run", project_id="project", cache={})
        self.assertEqual(result[0]["speaker_subject_key"], ["character", self.second])
