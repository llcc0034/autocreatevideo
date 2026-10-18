"""M8 exact adopted-clip mapping; isolated original queue, no model/encoder."""
from copy import deepcopy
import json
from unittest.mock import patch
from uuid import uuid4

from django.core.exceptions import ValidationError
from django.test import TestCase
from django.urls import reverse

from studio.models import ArtifactSelectionDecision, AssemblyJob, Episode, GenerationAttempt, GenerationTask, Shot
from studio.services.assembly_contracts import assembly_media_schema, assert_assembly_media_matches, validate_assembly_media
from studio.services.security_boundaries import boundary_int
from studio.services.json_contracts import assign_json_contract
from studio.services.production_recipe_stage_snapshots import recipe_assembly_configuration
from studio.services.production_runs import _execute_assembly
from studio.tests import test_recipe_assembly_output as author
from studio.tests.test_structure_mapping_preview import field


class RecipeAssemblyMediaTests(TestCase):
    setUp = author.RecipeAssemblyOutputTests.setUp
    _selected_artifact = author.RecipeAssemblyOutputTests._selected_artifact
    _read_fixture_candidate = author.RecipeAssemblyOutputTests._read_fixture_candidate
    freeze = author.RecipeAssemblyOutputTests.freeze
    author_client = author.RecipeAssemblyOutputTests.author_client

    def recipe(self, *, definition):
        self.episode2 = Episode.objects.create(project=self.project, number=2, title="Second isolated episode")
        self.shot3 = Shot.objects.create(episode=self.episode2, number=1, title="Third clip",
            duration_ms=4000, include_audio=True, media_type="video")
        self._selected_artifact(self.shot3, "three.mp4")
        self.media = {"episodes": [{"number": episode.number, "shots": [{"number": shot.number,
            "artifact_id": self.sources[str(shot.pk)]["generated_artifact_id"]} for shot in shots]}
            for episode, shots in ((self.episode, (self.shot1, self.shot2)), (self.episode2, (self.shot3,)))]}
        self.authored_rule = self.rule()
        definition["stages"][-1]["assembly_media"] = self.authored_rule
        return author.fixtures.plan_fixtures.ProductionRunPlanTests.recipe(self, definition=definition)

    def rule(self, *, nested=False):
        raw = {"片段": self.media, "说明": "  exact / 原值\r\n", "custom": False}
        return author.RecipeAssemblyOutputTests.rule(self, [raw] if nested else raw,
            field(*([0, "片段"] if nested else ["片段"])))

    def test_formal_post_patch_two_layouts_and_invalid_identity_have_no_selection_side_effects(self):
        client = self.author_client()
        self.assertEqual(client.get(reverse("production-recipe-editor-options")).data["assembly_media_schema"], assembly_media_schema())
        count = ArtifactSelectionDecision.objects.count()
        definition = {"schema_version": 3, "interaction_mode": "assisted", "stages": [
            {"key": "assembly", "mode": "existing_pipeline", "approval": "auto", "assembly_media": self.authored_rule}]}
        response = client.post(reverse("production-recipe-list"), {"recipe_key": "media-" + uuid4().hex,
            "name": "Exact clip author", "definition": definition}, format="json")
        self.assertEqual(response.status_code, 201, response.data)
        url = reverse("production-recipe-detail", args=[response.data["id"]])
        definition["stages"][0]["assembly_media"] = self.rule(nested=True)
        self.assertEqual(client.patch(url, {"definition": definition}, format="json").status_code, 200)
        self.assertEqual(client.get(url).data["definition"], definition)
        bad = deepcopy(definition)
        raw = json.loads(bad["stages"][0]["assembly_media"]["source_text"])
        raw[0]["片段"]["episodes"][0]["shots"][0]["artifact_id"] = "local.mp4"
        bad["stages"][0]["assembly_media"]["source_text"] = json.dumps(raw)
        self.assertEqual(client.patch(url, {"definition": bad}, format="json").status_code, 400)
        self.assertEqual(client.get(url).data["definition"], definition)
        self.assertEqual(ArtifactSelectionDecision.objects.count(), count)
        self.assertFalse(AssemblyJob.objects.exists())

    def test_two_episodes_freeze_queue_and_replay_use_exact_original_sources(self):
        count = ArtifactSelectionDecision.objects.count()
        self.freeze()
        frozen = deepcopy(self.assembly_stage.config_snapshot)
        projected = frozen["assembly_media"]
        self.assertEqual(projected["projection"]["output"], self.media)
        self.assertEqual(projected["projection"]["raw_value"]["说明"], "  exact / 原值\r\n")
        self.assertTrue(projected["projection"]["provenance"])
        self.output.status = "retired"; self.output.save(update_fields=("status",))
        with patch("studio.services.assembly_author_contracts.output_reference", side_effect=AssertionError("No registry reread")):
            self.freeze()
            _execute_assembly(self.run, self.assembly_stage)
            _execute_assembly(self.run, self.assembly_stage)
        self.assertEqual(AssemblyJob.objects.count(), 2)
        for job, expected in zip(AssemblyJob.objects.order_by("episode__number"), self.media["episodes"]):
            self.assertEqual([str(value) for value in job.source_items.order_by("ordinal").values_list("generated_artifact_id", flat=True)],
                [shot["artifact_id"] for shot in expected["shots"]])
        self.assertEqual(self.assembly_stage.config_snapshot, frozen)
        self.assertEqual(ArtifactSelectionDecision.objects.count(), count)
        self.assertFalse(GenerationAttempt.objects.exists()); self.assertFalse(GenerationTask.objects.exists())

    def test_scope_order_foreign_and_unselected_ids_do_not_change_original_selection(self):
        self.freeze()
        sources = self.assembly_stage.input_snapshot["assembly_sources"]
        count = ArtifactSelectionDecision.objects.count()
        cases = []
        bad = deepcopy(self.media); bad["episodes"].pop(); cases.append(bad)
        bad = deepcopy(self.media); bad["episodes"].reverse(); cases.append(bad)
        bad = deepcopy(self.media); bad["episodes"][0]["shots"].reverse(); cases.append(bad)
        bad = deepcopy(self.media); bad["episodes"][0]["shots"][0]["artifact_id"] = str(uuid4()); cases.append(bad)
        bad = deepcopy(self.media); bad["episodes"][0]["shots"][0]["artifact_id"] = self.media["episodes"][1]["shots"][0]["artifact_id"]; cases.append(bad)
        for value in cases:
            with self.subTest(value=value), self.assertRaises(ValidationError):
                assert_assembly_media_matches(value, sources)
        self.shot1.number = 3; self.shot1.save(update_fields=("number",))
        with self.assertRaises(ValidationError):
            self.freeze()
        self.assertEqual(ArtifactSelectionDecision.objects.count(), count)
        self.assertFalse(AssemblyJob.objects.exists())

    def test_types_and_rehashed_frozen_projection_fail_closed(self):
        for key, value in (("number", True), ("number", 0), ("artifact_id", 1), ("artifact_id", str(uuid4()).upper())):
            bad = deepcopy(self.media); bad["episodes"][0]["shots"][0][key] = value
            with self.subTest(key=key, value=value), self.assertRaises(ValidationError):
                validate_assembly_media(bad)
        self.freeze()
        original = deepcopy(self.assembly_stage.config_snapshot)
        for field_name in ("raw_value", "output"):
            bad = deepcopy(original); bad["assembly_media"]["projection"][field_name] = {}
            assign_json_contract(self.assembly_stage, "config_snapshot", bad,
                max_bytes=boundary_int("security.snapshot.stage_max_bytes"))
            with self.assertRaises(ValidationError):
                recipe_assembly_configuration(self.assembly_stage)
        self.assertFalse(AssemblyJob.objects.exists())
