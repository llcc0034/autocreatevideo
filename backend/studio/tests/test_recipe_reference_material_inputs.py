"""Recipe differences use the original per-unit contribution reader.

Isolated PNGs/DB and the existing dependency fixture; no live dispatch.
"""
from copy import deepcopy
from unittest.mock import patch

from django.core.exceptions import ValidationError
from django.test import override_settings

from studio.models import ExecutionPreparation, GenerationAttempt
from studio.services.execution_preparations import read_execution_preparation_detail
from studio.services.json_contracts import json_contract
from studio.services.media_generation_targets import read_media_generation_target
from studio.services.production_recipe_contracts import validate_recipe_execution_route
from studio.services.production_recipe_material_inputs import recipe_material_binding
from studio.tests.test_data_structures import StructureFixture
from studio.tests import test_recipe_material_inputs as material_fixtures


def literal_tree(value):
    if isinstance(value, dict):
        return {"kind": "object", "fields": {key: literal_tree(item) for key, item in value.items()}}
    if isinstance(value, list):
        return {"kind": "array", "items": [literal_tree(item) for item in value]}
    return {"kind": "literal", "value": value}


@override_settings(AI_PROVIDER_ALLOWED_HOSTS=("127.0.0.1", "api.example.com"))
class RecipeReferenceMaterialInputTests(StructureFixture):
    base = material_fixtures.RecipeMaterialInputTests
    create_job = base.create_job
    primary_version = base.primary_version
    write = base.write
    input = staticmethod(base.input)
    setUp = base.setUp
    configure_declaration = base.configure_declaration
    dependency = base.dependency
    prepare = base.prepare
    route = base.route
    variant_anchors = ("Orange jacket only.", "Green jacket only.")

    def configure_variant_sources(self, sources):
        original = sources["reference_2"]["selection"]["items"][0]
        sources["reference_2"] = {"kind": "material_primary", "item_key": original["item_key"], "purpose": original["purpose"]}
        value = deepcopy(sources["units"]["value"])
        value["ordered_units"] = value["ordered_units"][:1]
        template = literal_tree(value)
        unit = template["fields"]["ordered_units"]["items"][0]["fields"]
        unit["unit_key"] = {"kind": "unit_key"}
        unit["subject_keys"] = {"kind": "reference_subjects"}
        unit["structured_input"]["fields"]["changes"]["items"][0]["fields"]["detail.text"] = {"kind": "material_difference"}
        sources["units"] = {"kind": "structured", "template": template}

    def tearDown(self):
        self.external.assert_not_called()
        self.assertFalse(GenerationAttempt.objects.exists())

    def test_reference_difference_prepares_real_sibling_units_and_replays_without_reselection(self):
        route = self.route()
        before = deepcopy(route)
        validate_recipe_execution_route(route, stage_key="bible_image_generation", prior_stages=["bible_review"])
        self.assertEqual(recipe_material_binding(self.definition, route["input_sources"])["difference_input"],
            {"kind": "reference_field", "key": "units", "field_key": "unit_description"})
        dependency = self.dependency()
        with patch("studio.services.production_recipe_material_registration.read_registered_recipe_primary_dependency", return_value=dependency):
            preparations = [self.prepare(index) for index in range(2)]
        for index, preparation in enumerate(preparations):
            facts = read_execution_preparation_detail(preparation).resolved_inputs
            target = read_media_generation_target(preparation).value
            self.assertEqual(target["appearance"]["difference"], {
                "kind": "reference_field", "key": "units", "field_key": "unit_description", "unit_key": preparation.unit_key,
                "value_sha256": json_contract(self.variant_anchors[index]).sha256})
            unit = facts["inputs"]["logical_values"]["units"]["ordered_units"][0]
            self.assertEqual(unit["unit_key"], preparation.unit_key)
            self.assertEqual(unit["structured_input"]["changes"][0]["detail.text"], self.variant_anchors[index])
            self.assertEqual(facts["recipe_material_binding"]["primary"]["decision_id"], str(dependency["decision"].pk))
            self.assertTrue(facts["field_requests"])
        frozen = [deepcopy(read_execution_preparation_detail(row).resolved_inputs) for row in preparations]
        with patch("studio.services.production_recipe_material_registration.read_registered_recipe_primary_dependency", side_effect=AssertionError("No reselection")):
            self.assertEqual([self.prepare(i).pk for i in range(2)], [row.pk for row in preparations])
        self.assertEqual(frozen, [read_execution_preparation_detail(row).resolved_inputs for row in preparations])
        self.assertEqual(route, before)
        self.assertEqual(ExecutionPreparation.objects.filter(stage_run=self.stage).count(), 2)

    def test_reference_marker_requires_actual_unit_and_unique_exact_contribution_path(self):
        for change in ("literal_unit", "wrong_path", "duplicate", "undeclared_text"):
            with self.subTest(change=change):
                route = deepcopy(self.route())
                unit = route["input_sources"]["units"]["template"]["fields"]["ordered_units"]["items"][0]["fields"]
                if change == "literal_unit":
                    unit["unit_key"] = {"kind": "literal", "value": "invented-unit"}
                elif change == "wrong_path":
                    unit["structured_input"]["fields"]["changes"]["items"][0]["fields"]["detail.text"] = {"kind": "literal", "value": "not-difference"}
                    unit["ambient_sound"] = {"kind": "material_difference"}
                elif change == "duplicate":
                    route["input_sources"]["units"]["template"]["fields"]["ordered_units"]["items"].append(deepcopy({"kind": "object", "fields": unit}))
                else:
                    unit["structured_input"]["fields"]["changes"]["items"][0]["fields"]["detail.text"]["value"] = "replacement"
                with self.assertRaises(ValidationError):
                    validate_recipe_execution_route(route, stage_key="bible_image_generation", prior_stages=["bible_review"])
        self.assertFalse(ExecutionPreparation.objects.filter(stage_run=self.stage).exists())
