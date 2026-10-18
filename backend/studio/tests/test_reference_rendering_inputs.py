"""Explicit arbitrary creative structures, no ORM column fallback or AI calls."""
from copy import deepcopy
from unittest.mock import patch

from django.core.exceptions import ValidationError
from django.test import override_settings

from studio.services.data_structures import data_structure_snapshot
from studio.services.reference_rendering_inputs import resolve_unit_render_values, validate_unit_text_bindings
from studio.services.execution_profile_inputs import profile_input_targets
from studio.services.reference_context_facts import read_reference_context_facts
from studio.services.json_contracts import JsonContract
from studio.tests.test_data_structures import StructureFixture
from studio.tests import test_execution_reference_preparation as reference


class RenderingPathTests(StructureFixture):
    def setUp(self):
        self.snapshot = data_structure_snapshot(self.publish(self.create([
            self.node("root", "object"), self.node("custom", "list", "root", property_name="custom.part"),
            self.node("item", "object", "custom"), self.node("text", "text", "item", property_name="words", allow_null=True),
            self.node("count", "integer", "item", property_name="count", position=1),
        ])))
        self.path = ["custom.part", 0, "words"]

    def resolve(self, raw, **changes):
        return resolve_unit_render_values(**{"bindings": {"description": self.path}, "structure": self.snapshot,
            "structured_inputs": {"one": raw}, "unit_keys": ["one"], "max_bytes": 65536, **changes})

    def test_exact_dotted_property_index_raw_text_and_unused_fields(self):
        raw = {"custom.part": [{"words": "  Keep original.\n", "count": 0}], "other": False}
        before = deepcopy(raw)
        with self.assertNumQueries(0):
            values = self.resolve(raw)["one"]
        self.assertEqual(values["description"], raw["custom.part"][0]["words"])
        self.assertEqual(values["narrative"], "")
        self.assertEqual(raw, before)

    def test_missing_null_nontext_and_out_of_bounds_do_not_fall_back(self):
        for raw in ({}, {"custom.part": []}, {"custom.part": [{}]}, {"custom.part": [{"words": None}]},
                    {"custom.part": [{"words": 1}]}):
            with self.subTest(raw=raw), self.assertRaises(ValidationError):
                self.resolve(raw)

    def test_unknown_slots_paths_and_nontext_schema_are_rejected(self):
        for bindings in ({"unknown": self.path}, {"action": "custom.part.0.words"}, {"action": [True]},
                         {"action": ["custom.part", -1, "words"]}, {"action": ["custom.part", 0, "count"]},
                         {"action": ["custom", "part", 0, "words"]}):
            with self.subTest(bindings=bindings), self.assertRaises(ValidationError):
                validate_unit_text_bindings(bindings, structure=self.snapshot, check_structure=True)

    def test_empty_bindings_need_no_structure_and_root_text_needs_explicit_empty_path(self):
        values = self.resolve(None, bindings={}, structure=None, structured_inputs=None)
        self.assertTrue(all(value == "" for value in values["one"].values()))
        snapshot = data_structure_snapshot(self.publish(self.create([self.node("root", "text")])))
        self.assertEqual(self.resolve("Raw root", structure=snapshot, bindings={"action": []})["one"]["action"], "Raw root")

    def test_only_exact_structure_declared_default_resolves_without_mutating_original(self):
        snapshot = data_structure_snapshot(self.publish(self.create([self.node("root", "object"),
            self.node("words", "text", "root", property_name="words", has_default=True, default_value="Declared default", allow_null=True)])))
        raw = {}
        self.assertEqual(self.resolve(raw, structure=snapshot, bindings={"action": ["words"]})["one"]["action"], "Declared default")
        self.assertEqual(raw, {})
        with self.assertRaises(ValidationError):
            self.resolve({"words": None}, structure=snapshot, bindings={"action": ["words"]})

    def test_options_catalog_uses_renderer_slot_registry(self):
        from django.contrib.auth import get_user_model
        from rest_framework.test import APIClient
        from studio.services.reference_rendering_inputs import UNIT_RENDER_SLOTS
        client = APIClient()
        client.force_authenticate(get_user_model().objects.create_user(username="render-catalog", is_staff=True))
        response = client.get("/api/v1/prompt-profile-versions/options/")
        self.assertEqual(response.status_code, 200, response.data)
        self.assertEqual(response.data["reference_rendering_text_slots"], [{"value": key, "label": label} for key, label in UNIT_RENDER_SLOTS.items()])


@override_settings(AI_PROVIDER_ALLOWED_HOSTS=("api.example.com", "127.0.0.1"))
class RenderingPreparationTests(StructureFixture):
    input = staticmethod(reference.ExecutionReferencePreparationTests.input)
    write = reference.ExecutionReferencePreparationTests.write
    resolve = reference.ExecutionReferencePreparationTests.resolve
    prepare = reference.ExecutionReferencePreparationTests.prepare
    preview = reference.ExecutionReferencePreparationTests.preview
    select = reference.ExecutionReferencePreparationTests.select

    def configure(self, profile):
        from studio.tests.reference_rendering_fixtures import generation_structure_fixture
        generation_structure_fixture(profile)
        profile.source_projection_config["primitive_options"]["reference_context_ir_v1"]["rendering"]["unit_text_bindings"] = {"description": ["direction"]}
        if self.raw_only:
            rule = profile.field_rules.get()
            rule.generation_mode = "passthrough"
            rule.generation_source_selector = "projection.output"
            rule.generation_output_semantics = "final"
            rule.source_path = None
            rule.generation_required = False
            rule.generation_instruction = ""
            rule.generation_context_selectors = []
            rule.save()
        profile.save(update_fields=("source_projection_config",))

    def setup_profile(self, *, raw_only):
        self.raw_only = raw_only
        for target in ("studio.services.provider_registry._request_json", "studio.services.comfyui.ComfyUIClient"):
            self.enterContext(patch(target, side_effect=AssertionError("No model calls")))
        reference.ExecutionReferencePreparationTests.setUp(self, configure_profile=self.configure)

    def test_raw_only_profile_freezes_mapping_and_replays_without_generation_requests(self):
        self.setup_profile(raw_only=True)
        targets = profile_input_targets(self.prompt_profile)
        unit = targets["reference_context.units"]["value_schema"]["properties"]["ordered_units"]["items"]
        self.assertIn("structured_input", unit["required"])
        self.assertNotIn("source_facts", unit["properties"])
        self.assertNotIn("render_values", unit["properties"])
        before = deepcopy(self.sources)
        self.prepare()
        frozen = self.prep.contract.resolved_inputs
        self.assertEqual(frozen["field_requests"], [])
        self.assertEqual(frozen["profile_sources"]["projection"]["reference"]["unit_input"], before["units"]["value"])
        result = self.preview()
        self.assertIn("A courier arrives.", str(result.resolved_channels))
        self.sources["units"]["value"]["ordered_units"][0]["structured_input"]["direction"] = "Later edit"
        self.assertEqual(self.preview().resolved_channels, result.resolved_channels)

    def test_mapped_raw_and_explicit_selected_contribution_remain_separate(self):
        self.setup_profile(raw_only=False)
        self.prepare()
        frozen = self.prep.contract.resolved_inputs
        context = read_reference_context_facts(JsonContract(**frozen["profile_sources"]["reference_context"]), max_bytes=65536)
        self.assertEqual(context.ordered_units[0].render_values["description"], "A courier arrives.")
        self.select("first", "Selected first description.")
        self.select("second", "Selected second description.")
        result = str(self.preview().resolved_channels)
        self.assertIn("Selected first description.", result)
        self.assertNotIn("A courier arrives.", result)
