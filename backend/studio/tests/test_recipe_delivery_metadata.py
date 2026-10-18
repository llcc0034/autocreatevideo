"""Exact M1 release fields: author API and immutable stage, no live media calls."""
from copy import deepcopy
from unittest.mock import patch
from uuid import uuid4

from django.core.exceptions import ValidationError
from django.test import TestCase
from django.urls import reverse

from studio.models import AssemblyJob, GenerationAttempt, StageRun
from studio.services.json_contracts import assign_json_contract
from studio.services.production_recipe_stage_snapshots import recipe_delivery_metadata
from studio.services.production_runs import _freeze_stage_config
from studio.services.production_recipe_contracts import validate_recipe_definition
from studio.services.security_boundaries import boundary_int
from studio.tests.test_structure_mapping_preview import field, obj
from studio.tests import test_recipe_assembly_output as authors
from studio.tests import test_recipe_assembly_configuration as fixtures


class RecipeDeliveryMetadataTests(TestCase):
    _selected_artifact = fixtures.RecipeAssemblyConfigurationTests._selected_artifact
    _read_fixture_candidate = fixtures.RecipeAssemblyConfigurationTests._read_fixture_candidate
    rule = authors.RecipeAssemblyOutputTests.rule
    author_client = authors.RecipeAssemblyOutputTests.author_client

    def recipe(self, *, definition):
        self.metadata_rule = self.rule({"发行": {"名称": "  雨夜 / 夜🌧  ", "说明": "原文\r\nΔ"}, "custom": False},
            obj(title=field("发行", "名称"), notes=field("发行", "说明")))
        definition["stages"].append({"key": "delivery", "mode": "existing_pipeline", "approval": "auto",
            "delivery_metadata": self.metadata_rule})
        return fixtures.plan_fixtures.ProductionRunPlanTests.recipe(self, definition=definition)

    def setUp(self):
        fixtures.RecipeAssemblyConfigurationTests.setUp(self)
        self.delivery = StageRun.objects.create(production_run=self.run, stage_key="delivery", ordinal=3,
            mode="existing_pipeline", status="running", idempotency_key=str(uuid4()))

    def definition(self, rule):
        return {"schema_version": 3, "interaction_mode": "assisted", "stages": [
            {"key": "delivery", "mode": "existing_pipeline", "approval": "auto", "delivery_metadata": rule}]}

    def test_formal_author_api_two_layouts_exact_contract_and_atomic_rejection(self):
        client = self.author_client()
        from studio.services.delivery_contracts import delivery_metadata_schema
        self.assertEqual(client.get(reverse("production-recipe-editor-options")).data["delivery_metadata_schema"],
            delivery_metadata_schema())
        definition = self.definition(self.metadata_rule)
        result = client.post("/api/v1/production-recipes/", {"recipe_key": "delivery-" + uuid4().hex,
            "name": "Isolated delivery metadata", "definition": definition}, format="json")
        self.assertEqual(result.status_code, 201, result.data)
        url = f'/api/v1/production-recipes/{result.data["id"]}/'
        second = self.definition(self.rule([{"name": "  雨夜 / 夜🌧  ", "note": "原文\r\nΔ", "extra": 7}],
            obj(title=field(0, "name"), notes=field(0, "note"))))
        result = client.patch(url, {"definition": second}, format="json")
        self.assertEqual(result.status_code, 200, result.data)
        self.assertEqual(client.get(url).data["definition"], second)
        validate_recipe_definition(second, require_complete=True)
        cases = [dict(expected_contract_sha256="0" * 64), dict(source_text="{}"),
            dict(source_text="invalid JSON"), dict(mapping={"schema_version": 1, "node": obj(title=field(0, "name"))})]
        for change in cases:
            bad = deepcopy(second); bad["stages"][0]["delivery_metadata"].update(change)
            self.assertEqual(client.patch(url, {"definition": bad}, format="json").status_code, 400)
            self.assertEqual(client.get(url).data["definition"], second)
        bad = deepcopy(second); bad["stages"][0]["key"] = "assembly"
        self.assertEqual(client.patch(url, {"definition": bad}, format="json").status_code, 400)
        self.assertFalse(GenerationAttempt.objects.exists()); self.assertFalse(AssemblyJob.objects.exists())

    def test_stage_freezes_original_values_and_rejects_damage_without_registry_repair(self):
        _freeze_stage_config(self.run, self.delivery)
        saved = deepcopy(self.delivery.config_snapshot)
        expected = saved["delivery_metadata"]
        self.assertEqual(expected["projection"]["output"], {"title": "  雨夜 / 夜🌧  ", "notes": "原文\r\nΔ"})
        self.assertIs(expected["projection"]["raw_value"]["custom"], False)
        self.output.status = "retired"; self.output.save(update_fields=("status",))
        with patch("studio.services.assembly_author_contracts.output_reference", side_effect=AssertionError("No registry")):
            self.assertEqual(recipe_delivery_metadata(self.delivery), expected)
        cases = []
        bad = deepcopy(saved); bad.pop("delivery_metadata"); cases.append(bad)
        bad = deepcopy(saved); bad["delivery_metadata"]["projection"]["output"]["notes"] = "changed"; cases.append(bad)
        bad = deepcopy(saved); bad["delivery_metadata"]["output_reference"]["sha256"] = "0" * 64; cases.append(bad)
        for bad in cases:
            assign_json_contract(self.delivery, "config_snapshot", bad,
                max_bytes=boundary_int("security.snapshot.stage_max_bytes")); self.delivery.save()
            with self.assertRaises(ValidationError):
                recipe_delivery_metadata(self.delivery)
            self.delivery.refresh_from_db(); self.assertEqual(self.delivery.config_snapshot, bad)
        self.assertFalse(GenerationAttempt.objects.exists()); self.assertFalse(AssemblyJob.objects.exists())

    def test_target_rejects_real_field_errors_without_normalizing_creative_text(self):
        from studio.services.delivery_contracts import validate_delivery_metadata
        good = {"title": " 标题 ", "notes": ""}
        self.assertEqual(validate_delivery_metadata(good), good)
        for bad in ({"title": "", "notes": ""}, {"title": "x" * 201, "notes": ""},
                {"title": "x", "notes": True}, {"title": "x", "notes": "\x00"},
                {"title": "x", "notes": "", "unknown": "not accepted"}):
            with self.assertRaises(ValidationError):
                validate_delivery_metadata(bad)
