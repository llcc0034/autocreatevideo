"""M8 formal recipe author boundary and original frozen queue (isolated media).

Recipe publication/whole chain readiness is not manufactured by these tests:
the existing queue fixture stores a published recipe without dispatch/Smoke.
The output/structure itself uses the real versioned writer and publication.
"""
from copy import deepcopy
import json
from unittest.mock import patch
from uuid import uuid4

from django.core.exceptions import ValidationError
from django.test import TestCase
from django.urls import reverse
from rest_framework.test import APIClient

from studio.models import AssemblyJob, GenerationAttempt, GenerationTask, ProductionRecipeVersion
from studio.services.business_outputs import write_output
from studio.services.json_contracts import assign_json_contract
from studio.services.production_recipe_contracts import validate_recipe_definition, recipe_stage_declarations
from studio.services.production_recipe_stage_snapshots import recipe_assembly_configuration
from studio.services.production_runs import _execute_assembly
from studio.services.security_boundaries import boundary_int
from studio.tests.output_structure_fixtures import response_structure
from studio.tests.test_structure_mapping_preview import field, obj
from studio.tests import test_recipe_assembly_configuration as fixtures


class RecipeAssemblyOutputTests(TestCase):
    setUp = fixtures.RecipeAssemblyConfigurationTests.setUp
    _selected_artifact = fixtures.RecipeAssemblyConfigurationTests._selected_artifact
    _read_fixture_candidate = fixtures.RecipeAssemblyConfigurationTests._read_fixture_candidate
    freeze = fixtures.RecipeAssemblyConfigurationTests.freeze

    def rule(self, value=None, node=None):
        value = {"render": {"宽": 640, "高": 360, "率": 30}, "说明": "  原值不清洗\r\n"} if value is None else value
        node = obj(width=field("render", "宽"), height=field("render", "高"), fps=field("render", "率")) if node is None else node
        structure = response_structure(example=value)
        output = write_output(actor=self.user, data={"name": "Assembly source " + uuid4().hex,
            "description": "Isolated author values", "parser_key": "structured_json.v1",
            "data_structure_version": str(structure.pk), "quality_advisories": {}})
        output = write_output(actor=self.user, output_id=output.pk, expected_revision=output.revision, action="publish")
        self.output = output
        return {"output_version_id": str(output.pk), "expected_contract_sha256": output.contract_sha256,
            "source_text": json.dumps(value, ensure_ascii=False), "mapping": {"schema_version": 1, "node": node}}

    def recipe(self, *, definition):
        self.authored_rule = self.rule()
        definition["stages"][-1]["assembly_output"] = self.authored_rule
        return fixtures.plan_fixtures.ProductionRunPlanTests.recipe(self, definition=definition)

    def author_definition(self, rule=None):
        return {"schema_version": 3, "interaction_mode": "assisted", "stages": [
            {"key": "assembly", "mode": "existing_pipeline", "approval": "auto",
             "assembly_output": self.authored_rule if rule is None else rule}]}

    def author_client(self):
        self.user.is_staff = True
        self.user.save(update_fields=("is_staff",))
        client = APIClient()
        client.force_authenticate(self.user)
        return client

    def test_formal_recipe_post_patch_get_preserve_two_layouts_and_reject_invalid_mapping(self):
        client = self.author_client()
        from studio.services.assembly_contracts import assembly_output_schema
        catalog = client.get(reverse("production-recipe-editor-options"))
        self.assertEqual(catalog.status_code, 200, catalog.data)
        self.assertEqual(catalog.data["assembly_output_schema"], assembly_output_schema())
        first = self.author_definition()
        response = client.post(reverse("production-recipe-list"), {"recipe_key": "assembly-" + uuid4().hex,
            "name": "Isolated geometry author", "definition": first}, format="json")
        self.assertEqual(response.status_code, 201, response.data)
        url = reverse("production-recipe-detail", args=[response.data["id"]])
        self.assertEqual(client.get(url).data["definition"], first)
        second = self.author_definition(self.rule([{"pixels": [640, 360], "rate": 30, "extra": False}],
            obj(width=field(0, "pixels", 0), height=field(0, "pixels", 1), fps=field(0, "rate"))))
        response = client.patch(url, {"definition": second}, format="json")
        self.assertEqual(response.status_code, 200, response.data)
        self.assertEqual(client.get(url).data["definition"], second)
        self.assertEqual(validate_recipe_definition(second, require_complete=True), second["stages"])
        invalid = deepcopy(second)
        invalid["stages"][0]["assembly_output"]["mapping"]["node"]["fields"].pop("fps")
        response = client.patch(url, {"definition": invalid}, format="json")
        self.assertEqual(response.status_code, 400, response.data)
        self.assertEqual(client.get(url).data["definition"], second)
        self.assertFalse(GenerationAttempt.objects.exists())
        self.assertFalse(AssemblyJob.objects.exists())

    def test_exact_published_reference_and_closed_stage_shape_are_required(self):
        definition = self.author_definition()
        count = ProductionRecipeVersion.objects.count()
        client = self.author_client()
        changes = [dict(output_version_id=str(uuid4())), dict(expected_contract_sha256="0" * 64),
            dict(source_text="not JSON"), dict(source_text='{"render":{"宽":640,"高":360,"率":true}}'),
            dict(mapping={"schema_version": True, "node": obj()}), dict(extra="ignored?")]
        for change in changes:
            bad = deepcopy(definition)
            bad["stages"][0]["assembly_output"].update(change)
            response = client.post(reverse("production-recipe-list"), {"recipe_key": "assembly-" + uuid4().hex,
                "name": "Rejected author fixture", "definition": bad}, format="json")
            with self.subTest(change=change):
                self.assertEqual(response.status_code, 400, response.data)
                self.assertIn("assembly_output", str(response.data))
        self.assertEqual(ProductionRecipeVersion.objects.count(), count)
        for stage_change in ({"key": "delivery"}, {"mode": "skip"}):
            bad = deepcopy(definition)
            bad["stages"][0].update(stage_change)
            with self.assertRaises(ValidationError):
                recipe_stage_declarations(bad)
        self.output.status = "retired"
        self.output.save(update_fields=("status",))
        with self.assertRaises(ValidationError):
            validate_recipe_definition(definition)

    def test_projection_reaches_original_queue_and_frozen_replay_never_reads_current_registry(self):
        configuration = self.freeze()
        self.assertEqual([configuration[key] for key in ("width", "height", "fps")], [640, 360, 30])
        frozen = deepcopy(self.assembly_stage.config_snapshot)
        mapping = frozen["assembly_output"]
        self.assertEqual(mapping["projection"]["raw_value"]["说明"], "  原值不清洗\r\n")
        self.assertEqual(mapping["output_reference"]["id"], self.authored_rule["output_version_id"])
        self.assertTrue(all(row["source_structure_version_id"] == mapping["output_reference"]["data_structure"]["id"]
            for row in mapping["projection"]["provenance"]))
        self.output.status = "retired"
        self.output.save(update_fields=("status",))
        self.project.width, self.project.height, self.project.fps = 320, 240, 24
        self.project.save(update_fields=("width", "height", "fps"))
        with patch("studio.services.assembly_author_contracts.output_reference", side_effect=AssertionError("Frozen reference only")):
            self.assertEqual(self.freeze(), configuration)
            first = _execute_assembly(self.run, self.assembly_stage)
            second = _execute_assembly(self.run, self.assembly_stage)
        self.assertEqual(first, second)
        self.assertEqual(AssemblyJob.objects.count(), 1)
        job = AssemblyJob.objects.get()
        self.assertIn("640:360", str(job.ffmpeg_command))
        self.assertIn("fps=30", str(job.ffmpeg_command))
        self.assertEqual(self.assembly_stage.config_snapshot, frozen)
        self.assertFalse(GenerationTask.objects.exists())
        self.assertFalse(GenerationAttempt.objects.exists())

    def test_damaged_saved_projection_is_rejected_even_with_new_outer_sha(self):
        self.freeze()
        original = deepcopy(self.assembly_stage.config_snapshot)
        cases = []
        bad = deepcopy(original); bad.pop("assembly_output"); cases.append(bad)
        bad = deepcopy(original); bad["assembly_output"] = None; cases.append(bad)
        bad = deepcopy(original); bad["assembly_output"]["projection"]["raw_value"]["说明"] = "changed"; cases.append(bad)
        bad = deepcopy(original); bad["assembly_output"]["projection"]["output"]["fps"] = 29; cases.append(bad)
        bad = deepcopy(original); bad["assembly_configuration"]["fps"] = 29; cases.append(bad)
        bad = deepcopy(original); bad["assembly_output"]["output_reference"]["sha256"] = "0" * 64; cases.append(bad)
        with patch("studio.services.production_runs.create_assembly", side_effect=AssertionError("No enqueue")):
            for value in cases:
                assign_json_contract(self.assembly_stage, "config_snapshot", value,
                    max_bytes=boundary_int("security.snapshot.stage_max_bytes"))
                with self.subTest(value=value):
                    with self.assertRaises(ValidationError):
                        recipe_assembly_configuration(self.assembly_stage)
        self.assertFalse(AssemblyJob.objects.exists())

    def test_patch_revalidates_output_under_original_reference_write_guard(self):
        from studio.platform_serializers import ProductionRecipeVersionSerializer
        from studio.platform_views import ProductionRecipeVersionViewSet
        row = ProductionRecipeVersion.objects.create(recipe_key=uuid4().hex, version=1, name="lock fixture")
        serializer = ProductionRecipeVersionSerializer(row, data={"definition": self.author_definition()}, partial=True)
        self.assertTrue(serializer.is_valid(), serializer.errors)
        self.output.status = "retired"
        self.output.save(update_fields=("status",))
        from rest_framework.exceptions import ValidationError as ApiValidationError
        with self.assertRaises(ApiValidationError):
            ProductionRecipeVersionViewSet().perform_update(serializer)
        row.refresh_from_db()
        self.assertEqual(row.definition, {})
