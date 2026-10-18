"""The same frozen Profile compiles from values without a fake owner."""
from copy import deepcopy
from dataclasses import replace

from django.core.exceptions import ValidationError
from django.db import connection
from django.test import TestCase
from django.test.utils import CaptureQueriesContext

from studio.services.reference_rendering_inputs import UNIT_RENDER_SLOTS
from studio.models import SeriesProject, Shot
from studio.services.prompt_compiler_contract import PromptBuildError
from studio.services.prompt_profile_compiler import (
    PromptProfileCompilationFacts,
    compile_prompt_profile_facts, prompt_profile_facts_fingerprint,
)
from studio.services.prompt_profiles import PromptProfileError, refresh_prompt_profile_contract
from studio.services.reference_context_facts import (
    ReferenceContextFacts, ReferenceUnitFacts,
)
from studio.tests import test_prompt_profiles as profile_fixtures
from studio.tests.factories import build_story
from studio.tests.reference_rendering_fixtures import rendering_config, generation_structure_fixture


class ExecutionProfileCompilationTests(TestCase):
    _field = profile_fixtures.PromptProfileServiceTests._field
    _profile = profile_fixtures.PromptProfileServiceTests._profile
    _rule = profile_fixtures.PromptProfileServiceTests._rule
    _publish = profile_fixtures.PromptProfileServiceTests._publish

    def setUp(self):
        profile_fixtures.PromptProfileServiceTests.setUp(self)
        self.unit = ReferenceUnitFacts(
            unit_key="asset-unit", duration_ms=1000,
            render_values={key: "" for key in UNIT_RENDER_SLOTS},
            subject_keys=(), dialogue=(), voice_profiles=(), continuity_notes="",
            requires_first_frame_continuity=False, requires_last_frame_continuity=False, ambient_sound="",
        )
        self.reference = ReferenceContextFacts(
            prompt_type="image", mode="T2VA", include_audio=False, language="English",
            ordered_units=(self.unit,), subject_facts=(), reference_bindings=(),
            conditioning_mode_contract={"key": "T2VA"}, frame_policy=None, color_script="", extra_direction="",
            overall_soundscape="", non_diegetic_music="", generation_contributions={},
        )
        self.facts = PromptProfileCompilationFacts(
            sources={"request.theme": "A red object turns slowly."}, source_language="English", target_language="English",
            reference_context=None, reviewed_values={}, generation_contributions={},
        )

    def direct_profile(self):
        profile = self._profile()
        rule = self._rule(profile, self._field())
        rule.required = True
        rule.save()
        refresh_prompt_profile_contract(profile)
        return self._publish(profile)

    def reference_profile(self, *, contribution=False, logical_labels=False):
        profile = self._profile(source_projection_key="declarative_v1")
        profile.source_projection_config = rendering_config()
        if logical_labels:
            rendering = profile.source_projection_config["primitive_options"]["reference_context_ir_v1"]["rendering"]
            rendering["reference_layout"] = {"subject_label_template": "<Subject {index}>", "source_separator": ", ",
                "summary_reference_template": "{label}: {usage}", "shot_reference_template": ""}
            rendering["templates"]["entity_reference"] = "{label} is {subject} from {source_labels}."
            rendering["templates"]["reviewed_shot"] = "{shot_marker} {description} {reference_usage} {dialogue}"
        profile.save()
        rule = self._rule(profile, self._field("qa_reference"), selector="projection.output")
        rule.projection_primitive = "reference_context.detailed_description" if contribution else "reference_context.summary"
        rule.generation_mode = "model_generated" if contribution else "server_locked"
        rule.generation_output_semantics = "contribution" if contribution else "final"
        rule.generation_source_selector = "request.structured_input" if contribution else "projection.output"
        if contribution:
            generation_structure_fixture(profile)
            rule.source_path = ["direction"]
        rule.generation_instruction = "Describe the supplied shot facts without inventing references." if contribution else ""
        if logical_labels:
            rule.generation_validator_keys = ["no_unresolved_placeholders", "no_shot_headers"]
        rule.save()
        refresh_prompt_profile_contract(profile)
        return self._publish(profile)

    def _compile_without_business_queries(self, profile, facts):
        with CaptureQueriesContext(connection) as queries:
            result = compile_prompt_profile_facts(profile, facts=facts)
        for query in queries:
            self.assertNotRegex(query["sql"].lower(),
                r"\bstudio_(?:seriesproject|episode|shot|shotcharacterrelation|characterbible|scenebible|"
                r"propbible|biblevariant(?:revision|review)?|promptfieldtranslation|"
                r"asset(?:referenceseries|referenceversion)?)\b")
        return result

    def test_direct_profile_needs_no_project_or_shot_queries(self):
        profile = self.direct_profile()
        result = self._compile_without_business_queries(profile, self.facts)
        self.assertIn("A red object turns slowly.", result.assembly["channels"]["positive_prompt"])
        self.assertEqual(SeriesProject.objects.count(), 0)
        self.assertEqual(Shot.objects.count(), 0)

    def test_reference_profile_compiles_plain_asset_unit_and_does_not_mutate_facts(self):
        profile = self.reference_profile()
        facts = replace(self.facts, reference_context=self.reference)
        before = deepcopy(facts)
        result = self._compile_without_business_queries(profile, facts)
        self.assertIn("1 shots over 1 seconds", result.assembly["channels"]["positive_prompt"])
        self.assertEqual(result.model_prompt_contract["format"], "reference_context_ir_v2")
        self.assertEqual(facts, before)
        self.assertEqual(Shot.objects.count(), 0)

    def test_existing_project_cannot_override_explicit_compiler_facts(self):
        profile = self.direct_profile()
        _, project, _, _, _, shot, _ = build_story(username="profile-facts-owner")
        before = deepcopy(self.facts)
        first = compile_prompt_profile_facts(profile, facts=self.facts)
        shot.title = "CHANGED CURRENT SHOT"
        shot.save(update_fields=("title",))
        project.title = "CHANGED CURRENT PROJECT"
        project.save(update_fields=("title",))
        self.assertEqual(first, compile_prompt_profile_facts(profile, facts=self.facts))
        self.assertEqual(self.facts, before)
        self.assertNotIn("CHANGED CURRENT", first.assembly["channels"]["positive_prompt"])

    def test_compiler_preserves_explicit_selection_evidence_without_inventing_it(self):
        profile = self.direct_profile()
        evidence = {"fixture_selection": {"id": "selected-1", "value_sha256": "a" * 64}}
        facts = replace(self.facts, reviewed_evidence=evidence,
                        reviewed_fingerprint={"field_selection_sha256": "b" * 64})
        before = deepcopy(facts)
        result = compile_prompt_profile_facts(profile, facts=facts)
        self.assertEqual(result.reviewed_field_evidence, evidence)
        result.reviewed_field_evidence["fixture_selection"]["id"] = "changed"
        self.assertEqual(facts, before)
        self.assertIsNone(compile_prompt_profile_facts(profile, facts=self.facts).reviewed_field_evidence)

    def test_reference_projection_cannot_guess_missing_units(self):
        with self.assertRaisesMessage(PromptProfileError, "需要明确的参考单元"):
            compile_prompt_profile_facts(self.reference_profile(), facts=self.facts)

    def test_unknown_or_object_source_and_missing_required_source_rejected(self):
        profile = self.direct_profile()
        for sources in ({"arbitrary.path": "bad"}, {"request.theme": profile}, {}):
            with self.subTest(sources=sources), self.assertRaises((ValidationError, PromptProfileError)):
                compile_prompt_profile_facts(profile, facts=replace(self.facts, sources=sources))

    def test_unconfigured_contribution_and_server_field_override_rejected(self):
        profile = self.direct_profile()
        for facts in (
            replace(self.facts, reviewed_values={"qa_theme": "overwrite"}),
            replace(self.facts, generation_contributions={("asset-unit", "shot.camera"): "unconfigured"}),
        ):
            with self.assertRaises(PromptProfileError):
                compile_prompt_profile_facts(profile, facts=facts)

    def test_malformed_facts_fail_explicitly(self):
        profile = self.direct_profile()
        for facts in ({"project": "fake"}, replace(self.facts, source_language=None),
                      replace(self.facts, reviewed_values=[]), replace(self.facts, generation_contributions={"key": "text"})):
            with self.subTest(facts=facts), self.assertRaises(PromptProfileError):
                compile_prompt_profile_facts(profile, facts=facts)

    def test_fact_fingerprint_changes_with_content_language_and_selection(self):
        initial = prompt_profile_facts_fingerprint(self.facts)
        for facts in (replace(self.facts, target_language="Chinese"), replace(self.facts, sources={"request.theme": "Changed"}),
                      replace(self.facts, reviewed_fingerprint={"selection_sha256": "a" * 64})):
            self.assertNotEqual(initial, prompt_profile_facts_fingerprint(facts))
        self.assertEqual(initial, prompt_profile_facts_fingerprint(deepcopy(self.facts)))

    def test_advisories_use_resolved_unit_duration_and_action(self):
        profile = self.reference_profile()
        unit = replace(self.unit, render_values={**self.unit.render_values, "action": "turn, fall, rise, stop, run, jump, hide"})
        result = compile_prompt_profile_facts(profile, facts=replace(self.facts, reference_context=replace(self.reference, ordered_units=(unit,))))
        self.assertTrue(any("asset-unit" in warning and "动作密度" in warning for warning in result.assembly["warnings"]))

    def test_selected_contribution_uses_real_unit_key_and_is_validated(self):
        profile = self.reference_profile(contribution=True)
        facts = replace(self.facts, reference_context=self.reference,
                        generation_contributions={("asset-unit", "shot.detailed_description"): "A red object rotates."})
        result = compile_prompt_profile_facts(profile, facts=facts)
        self.assertIn("A red object rotates.", result.assembly["channels"]["positive_prompt"])
        self.assertEqual(result.generation_length_audit[0]["unit_key"], "asset-unit")
        self.assertNotIn("shot_id", result.generation_length_audit[0])
        with self.assertRaises((PromptProfileError, PromptBuildError)):
            compile_prompt_profile_facts(profile, facts=replace(facts, generation_contributions={("other-unit", "shot.detailed_description"): "Wrong owner."}))

    def test_projected_output_and_second_contribution_source_are_rejected(self):
        profile = self.reference_profile()
        for facts in (
            replace(self.facts, reference_context=self.reference, sources={"projection.output": {"qa_reference": "injected"}}),
            replace(self.facts, reference_context=replace(self.reference, generation_contributions={("asset-unit", "shot.detailed_description"): "hidden"})),
        ):
            with self.assertRaises(PromptProfileError):
                compile_prompt_profile_facts(profile, facts=facts)

    def logical_reference_facts(self):
        from studio.services.reference_context_facts import ReferenceSubjectFacts
        context = replace(self.reference,
            ordered_units=(replace(self.unit, subject_keys=(("character", "a"),)),
                           replace(self.unit, unit_key="other-unit", subject_keys=(("character", "b"),))),
            subject_facts=(ReferenceSubjectFacts("character", "a", ("Actor A",)),
                           ReferenceSubjectFacts("character", "b", ("Actor B",))),
            reference_bindings=(
                {"category": "character", "entity_id": "a", "entity_label": "Actor A", "input_label": "<Picture 1>", "unit_keys": ["asset-unit"]},
                {"category": "character", "entity_id": "b", "entity_label": "Actor B", "input_label": "<Picture 2>", "unit_keys": ["other-unit"]}))
        return replace(self.facts, reference_context=context, generation_contributions={
            ("asset-unit", "shot.detailed_description"): "<Subject 1> slowly turns.",
            ("other-unit", "shot.detailed_description"): "<Subject 2> opens the door."})

    def test_logical_contribution_survives_compiler_and_final_transport_channel_validation(self):
        from studio.services.execution_prompts import normalize_compiled_profile_channels
        profile = self.reference_profile(contribution=True, logical_labels=True)
        result = compile_prompt_profile_facts(profile, facts=self.logical_reference_facts())
        channel = {"semantic_key": "prompt.positive", "rule": {"type": "string", "required": True},
                   "layers": [{"source": "fixture", "rule": {"type": "string", "required": True}}]}
        config = {"mode": "profile", "profile_id": str(profile.pk), "profile_sha256": profile.contract_sha256,
                  "channels": {"positive_prompt": channel}}
        resolved = normalize_compiled_profile_channels(config, result, declared_reference_labels=["<Picture 1>", "<Picture 2>"])
        self.assertIn("[Shot 1] <Subject 1> slowly turns.", resolved["values"]["positive_prompt"])
        self.assertEqual(len(result.execution_facts["reference_subject_bindings"]), 2)
        with self.assertRaisesMessage(ValidationError, "本次实际输入"):
            normalize_compiled_profile_channels(config, result, declared_reference_labels=["<Picture 1>"])

    def test_contribution_cannot_use_another_units_label_or_duplicate_template_header(self):
        profile = self.reference_profile(contribution=True, logical_labels=True)
        facts = self.logical_reference_facts()
        for text in ("<Subject 2> turns.", "<Picture 2> moves.", "<Subject 99> moves.",
                     "{{missing}} turns.", "[Shot 7] <Subject 1> turns.", "Shot 7: <Subject 1> turns."):
            contributions = {**facts.generation_contributions, ("asset-unit", "shot.detailed_description"): text}
            with self.subTest(text=text), self.assertRaises(PromptProfileError):
                compile_prompt_profile_facts(profile, facts=replace(facts, generation_contributions=contributions))
