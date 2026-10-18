"""M8 known-shot timing author, isolated original queue; no model or encoder.

The run/selection is the existing explicit synthetic queue fixture, not a real
production acceptance. Output structures use their formal versioned writers.
"""
from copy import deepcopy
import json
from unittest.mock import patch
from uuid import uuid4

from django.core.exceptions import ValidationError
from django.test import TestCase
from django.urls import reverse

from studio.models import AssemblyJob, GenerationAttempt, GenerationTask
from studio.services.assembly_contracts import assembly_timing_schema, validate_assembly_timing
from studio.services.business_outputs import write_output
from studio.services.json_contracts import assign_json_contract
from studio.services.production_recipe_stage_snapshots import recipe_assembly_configuration
from studio.services.production_runs import _execute_assembly
from studio.services.security_boundaries import boundary_int
from studio.tests import test_recipe_assembly_output as author
from studio.tests.output_structure_fixtures import response_structure
from studio.tests.test_structure_mapping_preview import field


class RecipeAssemblyTimingTests(TestCase):
    setUp = author.RecipeAssemblyOutputTests.setUp
    _selected_artifact = author.RecipeAssemblyOutputTests._selected_artifact
    _read_fixture_candidate = author.RecipeAssemblyOutputTests._read_fixture_candidate
    freeze = author.RecipeAssemblyOutputTests.freeze
    author_client = author.RecipeAssemblyOutputTests.author_client

    def rule(self, *, nested=False):
        self.timing = {"episodes": [{"number": self.episode.number, "shots": [
            {key: getattr(shot, key) for key in ("number", "duration_ms", "episode_start_ms", "episode_end_ms")}
            for shot in (self.shot1, self.shot2)]}]}
        sample = deepcopy(self.timing)
        for shot in sample["episodes"][0]["shots"]:
            shot["episode_start_ms"] = shot["episode_end_ms"] = 0
        sample = {"时序": sample, "说明": "  原值\r\n"}
        value = {"时序": self.timing, "说明": "  原值\r\n"}
        if nested:
            sample, value = [sample], [value]
        structure = response_structure(example=sample, nullable_fields=("episode_start_ms", "episode_end_ms"))
        output = write_output(actor=self.user, data={"name": "Timing " + uuid4().hex,
            "description": "Isolated timing author", "parser_key": "structured_json.v1",
            "data_structure_version": str(structure.pk), "quality_advisories": {}})
        self.output = write_output(actor=self.user, output_id=output.pk,
            expected_revision=output.revision, action="publish")
        return {"output_version_id": str(output.pk), "expected_contract_sha256": self.output.contract_sha256,
            "source_text": json.dumps(value, ensure_ascii=False),
            "mapping": {"schema_version": 1, "node": field(*([0, "时序"] if nested else ["时序"]))}}

    def recipe(self, *, definition):
        self.authored_rule = self.rule()
        definition["stages"][-1]["assembly_timing"] = self.authored_rule
        return author.fixtures.plan_fixtures.ProductionRunPlanTests.recipe(self, definition=definition)

    def author_definition(self, rule=None):
        return {"schema_version": 3, "interaction_mode": "assisted", "stages": [
            {"key": "assembly", "mode": "existing_pipeline", "approval": "auto",
             "assembly_timing": rule or self.authored_rule}]}

    def test_formal_api_two_layouts_keep_raw_values_and_offer_target_schema(self):
        client = self.author_client()
        response = client.get(reverse("production-recipe-editor-options"))
        self.assertEqual(response.status_code, 200, response.data)
        self.assertEqual(response.data["assembly_timing_schema"], assembly_timing_schema())
        definition = self.author_definition()
        response = client.post(reverse("production-recipe-list"), {"recipe_key": "timing-" + uuid4().hex,
            "name": "Known-shot timing", "definition": definition}, format="json")
        self.assertEqual(response.status_code, 201, response.data)
        url = reverse("production-recipe-detail", args=[response.data["id"]])
        self.assertEqual(client.get(url).data["definition"], definition)
        second = self.author_definition(self.rule(nested=True))
        response = client.patch(url, {"definition": second}, format="json")
        self.assertEqual(response.status_code, 200, response.data)
        self.assertEqual(client.get(url).data["definition"], second)
        bad = deepcopy(second)
        bad["stages"][0]["assembly_timing"]["mapping"]["node"] = field(0, "absent")
        response = client.patch(url, {"definition": bad}, format="json")
        self.assertEqual(response.status_code, 400, response.data)
        self.assertEqual(client.get(url).data["definition"], second)
        self.assertFalse(AssemblyJob.objects.exists())
        self.assertFalse(GenerationAttempt.objects.exists())

    def test_freeze_retains_exact_contract_and_queue_replay_uses_no_current_output(self):
        self.freeze()
        frozen = deepcopy(self.assembly_stage.config_snapshot)
        contract = frozen["assembly_timing"]
        self.assertEqual(contract["projection"]["output"], self.timing)
        self.assertEqual(contract["projection"]["raw_value"]["说明"], "  原值\r\n")
        self.assertEqual(contract["output_reference"]["id"], self.authored_rule["output_version_id"])
        self.assertTrue(contract["projection"]["provenance"])
        self.output.status = "retired"
        self.output.save(update_fields=("status",))
        with patch("studio.services.assembly_author_contracts.output_reference", side_effect=AssertionError("Frozen only")):
            self.freeze()
            self.assertEqual(_execute_assembly(self.run, self.assembly_stage), _execute_assembly(self.run, self.assembly_stage))
        job = AssemblyJob.objects.get()
        self.assertEqual(list(job.source_items.order_by("ordinal").values_list("duration_ms", flat=True)),
            [self.shot1.duration_ms, self.shot2.duration_ms])
        self.assertEqual(self.assembly_stage.config_snapshot, frozen)
        self.assertFalse(GenerationAttempt.objects.exists())
        self.assertFalse(GenerationTask.objects.exists())

    def test_current_timing_conflict_rejects_before_input_freeze_and_enqueue_without_mutation(self):
        self.shot1.duration_ms += 1000
        self.shot1.save(update_fields=("duration_ms",))
        with patch("studio.services.production_runs.create_assembly", side_effect=AssertionError("No enqueue")):
            with self.assertRaisesMessage(ValidationError, "与本轮分镜不一致"):
                self.freeze()
        self.assembly_stage.refresh_from_db()
        self.shot1.refresh_from_db()
        self.assertFalse(self.assembly_stage.input_snapshot)
        self.assertEqual(self.shot1.duration_ms, self.timing["episodes"][0]["shots"][0]["duration_ms"] + 1000)
        self.assertFalse(AssemblyJob.objects.exists())

    def test_complete_scope_types_order_and_interval_semantics_are_not_guessed(self):
        from studio.services.assembly_contracts import assert_assembly_timing_matches
        self.freeze()
        source = self.assembly_stage.input_snapshot["assembly_sources"]
        for mutate in (lambda shots: shots.pop(), lambda shots: shots.append({**shots[-1], "number": 3})):
            value = deepcopy(self.timing)
            mutate(value["episodes"][0]["shots"])
            with self.assertRaises(ValidationError):
                assert_assembly_timing_matches(value, source)
        for key, bad in (("number", True), ("number", 0), ("duration_ms", "3000"),
                         ("duration_ms", 3.5), ("episode_start_ms", 0), ("episode_end_ms", False)):
            value = deepcopy(self.timing)
            value["episodes"][0]["shots"][0][key] = bad
            with self.subTest(key=key, bad=bad), self.assertRaises(ValidationError):
                validate_assembly_timing(value)
        value = deepcopy(self.timing)
        value["episodes"][0]["shots"].reverse()
        with self.assertRaises(ValidationError):
            validate_assembly_timing(value)
        value = deepcopy(self.timing)
        cursor = 0
        for shot in value["episodes"][0]["shots"]:
            shot["episode_start_ms"], shot["episode_end_ms"] = cursor, cursor + shot["duration_ms"]
            cursor += shot["duration_ms"]
        self.assertEqual(validate_assembly_timing(value), value)
        with self.assertRaises(ValidationError):  # null-pair and authored interval are distinct facts
            assert_assembly_timing_matches(value, source)
        value["episodes"][0]["shots"][1]["episode_start_ms"] += 1
        value["episodes"][0]["shots"][1]["episode_end_ms"] += 1
        with self.assertRaises(ValidationError):
            validate_assembly_timing(value)

    def test_damaged_or_missing_frozen_projection_cannot_be_reconstructed(self):
        self.freeze()
        original = deepcopy(self.assembly_stage.config_snapshot)
        cases = []
        bad = deepcopy(original); bad.pop("assembly_timing"); cases.append(bad)
        bad = deepcopy(original); bad["assembly_timing"]["projection"]["raw_value"]["说明"] = "changed"; cases.append(bad)
        bad = deepcopy(original); bad["assembly_timing"]["projection"]["output"]["episodes"] = []; cases.append(bad)
        bad = deepcopy(original); bad["assembly_timing"]["output_reference"]["sha256"] = "0" * 64; cases.append(bad)
        for value in cases:
            assign_json_contract(self.assembly_stage, "config_snapshot", value,
                max_bytes=boundary_int("security.snapshot.stage_max_bytes"))
            with self.subTest(value=value), self.assertRaises(ValidationError):
                recipe_assembly_configuration(self.assembly_stage)
        self.assertFalse(AssemblyJob.objects.exists())
