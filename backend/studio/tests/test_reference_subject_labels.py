from copy import deepcopy
from dataclasses import replace
from unittest.mock import patch

from django.test import SimpleTestCase

from studio.services.prompt_compiler_contract import PromptBuildError
from studio.services.reference_context import compile_reference_context, validate_rendering_policy
from studio.services.reference_context_facts import ReferenceSubjectFacts
from studio.services.reference_subject_labels import declared_reference_labels, reference_subject_labels
from studio.tests.reference_rendering_fixtures import reference_context_fixture, reference_unit_fixture, rendering_config


class ReferenceSubjectLabelTests(SimpleTestCase):
    def setUp(self):
        self.enterContext(patch("studio.services.reference_context.boundary_int", return_value=65536))
        self.policy = rendering_config()["primitive_options"]["reference_context_ir_v1"]["rendering"]
        self.policy["reference_layout"] = {"subject_label_template": "<Subject {index}>", "source_separator": ", ",
            "summary_reference_template": "{label}: {usage}", "shot_reference_template": ""}
        self.policy["templates"]["entity_reference"] = "{label} is {subject} from {source_labels}."
        self.policy["templates"]["reviewed_shot"] = "{shot_marker} {description} {reference_usage} {dialogue}"
        first = reference_unit_fixture(unit_key="one", subject_keys=(("character", "a"),))
        second = reference_unit_fixture(unit_key="two", subject_keys=(("character", "b"),))
        self.context = reference_context_fixture(ordered_units=(first, second), include_audio=False,
            subject_facts=(ReferenceSubjectFacts("character", "a", ("same name",)), ReferenceSubjectFacts("character", "b", ("same name",))),
            reference_bindings=(
                {"category": "character", "entity_id": "a", "entity_label": "同名", "input_label": "<Picture 1>", "unit_keys": ["one"]},
                {"category": "character", "entity_id": "a", "entity_label": "同名", "input_label": "<Picture 2>", "unit_keys": ["one"]},
                {"category": "character", "entity_id": "b", "entity_label": "同名", "input_label": "<Picture 3>", "unit_keys": ["two"]},
            ), generation_contributions={("one", "shot.detailed_description"): "<Subject 1> turns toward the window.",
                                         ("two", "shot.detailed_description"): "<Subject 2> closes the door."})

    def test_exact_identity_groups_sources_without_merging_equal_names_or_ports(self):
        before = deepcopy(self.context)
        mapping = reference_subject_labels(self.context, self.policy)
        self.assertEqual(mapping[("character", "a")]["source_labels"], ["<Picture 1>", "<Picture 2>"])
        self.assertEqual(mapping[("character", "b")]["label"], "<Subject 2>")
        result = compile_reference_context(self.context, self.policy)
        self.assertEqual(result["fields"]["subject_definitions"], [
            "<Subject 1> is 同名 from <Picture 1>, <Picture 2>.", "<Subject 2> is 同名 from <Picture 3>."])
        self.assertEqual(len(result["fields"]["retention_analysis"]), 2)
        description = "\n".join(result["fields"]["detailed_description"])
        self.assertEqual(description.count("[Shot 1]"), 1)
        self.assertIn("[Shot 2] At 00:03.500,", description)
        self.assertNotIn("Preserve the referenced", description)
        self.assertEqual(result["fields"]["summary"].count("Preserve the referenced character identity."), 2)
        self.assertNotIn("language_fields", result)
        self.assertEqual(self.context, before)
        self.assertEqual(declared_reference_labels(self.context, self.policy),
                         ["<Picture 1>", "<Picture 2>", "<Picture 3>", "<Subject 1>", "<Subject 2>"])

    def test_unconfigured_profiles_do_not_acquire_subject_labels(self):
        del self.policy["reference_layout"]
        self.assertEqual(reference_subject_labels(self.context, self.policy), {})
        self.assertEqual(declared_reference_labels(self.context, self.policy), ["<Picture 1>", "<Picture 2>", "<Picture 3>"])

    def test_unknown_identity_collision_and_contradictory_roles_fail_closed(self):
        for change in ({"entity_id": "missing"}, {"purpose": "different"}, {"prompt_hint": "contradictory instruction"}):
            rows = list(deepcopy(self.context.reference_bindings))
            rows[1].update(change)
            with self.subTest(change=change), self.assertRaises(PromptBuildError):
                compile_reference_context(replace(self.context, reference_bindings=tuple(rows)), self.policy)
        self.policy["reference_layout"]["subject_label_template"] = "<Picture {index}>"
        with self.assertRaisesMessage(PromptBuildError, "冲突"):
            compile_reference_context(self.context, self.policy)

    def test_bad_layout_rejected_no_scripts_or_silent_missing_sources(self):
        for key, value in (("subject_label_template", "<Subject {index.x}>"), ("source_separator", ""),
                           ("summary_reference_template", "{label.upper()}"), ("shot_reference_template", "{unknown}")):
            policy = deepcopy(self.policy)
            policy["reference_layout"][key] = value
            with self.subTest(key=key), self.assertRaises(PromptBuildError):
                validate_rendering_policy(policy)
        self.policy["templates"]["entity_reference"] = "{label}: {subject}"
        with self.assertRaisesMessage(PromptBuildError, "source_labels"):
            validate_rendering_policy(self.policy)

    def test_layout_is_model_neutral_and_empty_reference_does_not_invent_subject(self):
        self.policy["reference_layout"]["subject_label_template"] = "<Entity {index}>"
        self.assertEqual(reference_subject_labels(self.context, self.policy)[("character", "a")]["label"], "<Entity 1>")
        context = replace(self.context, reference_bindings=(), generation_contributions={})
        self.assertEqual(declared_reference_labels(context, self.policy), [])

    def test_paired_audio_is_physical_not_an_extra_visual_subject(self):
        rows = list(deepcopy(self.context.reference_bindings))
        rows[0].update(audio_input_label="<Audio 2>", audio_purpose="audio_reference")
        result = compile_reference_context(replace(self.context, reference_bindings=tuple(rows)), self.policy)
        text = "\n".join(result["fields"]["subject_definitions"])
        self.assertIn("<Audio 2>", text)
        self.assertNotIn("<Subject 3>", text)
