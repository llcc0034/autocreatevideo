"""Dynamic input preparation only. Dependency-reader fixtures are explicit;
real registration/selection has its own tests. No mixed dispatch is claimed.
"""
from copy import deepcopy
from types import SimpleNamespace
from unittest.mock import patch
from uuid import uuid4

from django.core.exceptions import ValidationError
from django.test import SimpleTestCase, override_settings

from studio.models import ExecutionPreparation, GenerationAttempt
from studio.services.execution_preparations import read_execution_preparation_detail
from studio.services.media_generation_targets import read_media_generation_target
from studio.services.production_recipe_contracts import validate_recipe_execution_route
from studio.services.production_recipe_material_inputs import recipe_material_binding, bind_material_sources
from studio.services.production_recipe_media_preparations import prepare_recipe_media_unit
from studio.services.prompt_profile_structures import STRUCTURED_INPUT_SELECTOR as SOURCE
from studio.tests.test_data_structures import StructureFixture
from studio.tests import test_recipe_variant_preparations as fixtures


class MaterialStructureBoundaryTests(SimpleTestCase):
    def test_non_recipe_structure_and_unbound_evaluation_reject_markers(self):
        from studio.services.execution_structure_mapping import validate_structure, evaluate_structure
        marker = {"kind": "material_difference"}
        with self.assertRaisesMessage(ValidationError, "当前入口"):
            validate_structure(marker, leaf_validator=lambda *_: self.fail("Not a recipe reader"), path="input")
        with self.assertRaisesMessage(ValidationError, "不能直接求值"):
            evaluate_structure(marker, read_leaf=lambda *_: self.fail("No read"), unit_key="unit", reference_subjects=[], path="input")


@override_settings(AI_PROVIDER_ALLOWED_HOSTS=("127.0.0.1", "api.example.com"))
class RecipeMaterialInputTests(StructureFixture):
    create_job = fixtures.RecipeVariantPreparationTests.create_job
    primary_version = fixtures.RecipeVariantPreparationTests.primary_version
    write = fixtures.RecipeVariantPreparationTests.write
    input = staticmethod(fixtures.RecipeVariantPreparationTests.input)
    setUp = fixtures.RecipeVariantPreparationTests.setUp
    configure_declaration = fixtures.RecipeVariantPreparationTests.configure_declaration
    variant_anchors = ("蓝色外套", "红色外套，保留相同人物")

    def configure_variant_sources(self, sources):
        original = sources["reference_2"]["selection"]["items"][0]
        sources["reference_2"] = {"kind": "material_primary", "item_key": original["item_key"], "purpose": original["purpose"]}
        sources[SOURCE] = {"kind": "structured", "template": {"kind": "object", "fields": {
            "changes": {"kind": "array", "items": [
                {"kind": "object", "fields": {"detail.text": {"kind": "literal", "value": "Keep unrelated text"}}},
                {"kind": "object", "fields": {"detail.text": {"kind": "material_difference"}}}]}}}}

    def tearDown(self):
        self.external.assert_not_called()
        self.assertFalse(GenerationAttempt.objects.exists())

    def dependency(self):
        return {"version": self.primary, "decision": SimpleNamespace(pk=uuid4()),
            "artifact": SimpleNamespace(pk=uuid4()), "preparation": SimpleNamespace(pk=uuid4())}

    def prepare(self, index=0, number=1):
        return prepare_recipe_media_unit(stage_run_id=self.stage.pk, expected_run_plan_sha256=self.plan.sha256,
            scope=self.scope, candidate_number=number, actor=self.user,
            material_unit_key=f"appearance:{self.settings[index].variant_id}")

    def route(self):
        return self.declaration["variant_routing"]["routes"][1]

    def test_exact_primary_and_distinct_sibling_text_reach_original_inputs(self):
        dependency = self.dependency()
        with patch("studio.services.production_recipe_material_registration.read_registered_recipe_primary_dependency",
                return_value=dependency) as reader:
            preparations = [self.prepare(index) for index in range(2)]
        self.assertEqual(reader.call_count, 2)
        for index, preparation in enumerate(preparations):
            facts = read_execution_preparation_detail(preparation).resolved_inputs
            target = read_media_generation_target(preparation).value
            self.assertEqual(facts["inputs"]["logical_values"][SOURCE]["changes"][1]["detail.text"], self.variant_anchors[index])
            self.assertEqual(facts["inputs"]["logical_values"][SOURCE]["changes"][0]["detail.text"], "Keep unrelated text")
            self.assertEqual(target["appearance"]["primary"]["version_id"], str(self.primary.pk))
            self.assertEqual(target["appearance"]["difference"]["field_key"], "exact_change")
            self.assertEqual(facts["inputs"]["logical_values"]["reference_2"][0]["asset_id"], str(self.primary.asset_id))
            self.assertEqual(facts["recipe_material_binding"]["primary"]["decision_id"], str(dependency["decision"].pk))
            self.assertEqual(facts["recipe_material_binding"]["difference"]["setting_revision_id"], str(self.settings[index].pk))
        self.assertNotEqual(preparations[0].unit_key, preparations[1].unit_key)

    def test_replay_does_not_reread_primary_or_difference(self):
        with patch("studio.services.production_recipe_material_registration.read_registered_recipe_primary_dependency", return_value=self.dependency()):
            preparation = self.prepare()
        original = deepcopy(read_execution_preparation_detail(preparation).resolved_inputs)
        with patch("studio.services.production_recipe_material_registration.read_registered_recipe_primary_dependency", side_effect=AssertionError("No reselection")), patch(
                "studio.services.bible_setting_evidence.read_setting_revisions", side_effect=AssertionError("No current text")):
            self.assertEqual(self.prepare().pk, preparation.pk)
        self.assertEqual(read_execution_preparation_detail(preparation).resolved_inputs, original)

    def test_library_primary_is_not_a_substitute_for_missing_run_selection(self):
        with self.assertRaisesMessage(ValidationError, "尚未选择或登记"):
            self.prepare()
        self.assertFalse(ExecutionPreparation.objects.filter(stage_run=self.stage).exists())

    def test_config_rejects_unmapped_difference_or_duplicate_primary(self):
        route = self.route()
        validate_recipe_execution_route(route, stage_key="bible_image_generation", prior_stages=["bible_review"])
        for change in ("wrong_position", "duplicate_primary", "wrong_purpose", "text_override"):
            with self.subTest(change=change):
                broken = deepcopy(route)
                sources = broken["input_sources"]
                if change == "wrong_position":
                    sources[SOURCE]["template"]["fields"]["changes"]["items"].reverse()
                elif change == "duplicate_primary":
                    sources["reference_1"] = deepcopy(sources["reference_2"])
                elif change == "wrong_purpose":
                    sources["reference_2"]["purpose"] = "not-a-purpose"
                else:
                    sources[SOURCE]["template"]["fields"]["changes"]["items"][1]["fields"]["detail.text"]["value"] = "injected"
                with self.assertRaises(ValidationError):
                    validate_recipe_execution_route(broken, stage_key="bible_image_generation", prior_stages=["bible_review"])

    def test_markers_are_not_accepted_outside_material_context_or_with_other_scope(self):
        from studio.services.production_recipe_contracts import validate_recipe_definition
        definition = deepcopy(self.plan.value["recipe"]["definition"])
        declaration = next(row for row in definition["stages"] if row["key"] == "bible_image_generation")
        declaration["routing"]["routes"] = [deepcopy(self.route())]
        with self.assertRaisesMessage(ValidationError, "只能配置在变体路线"):
            validate_recipe_definition(definition)
        with self.assertRaises(ValidationError):
            validate_recipe_execution_route(self.route(), stage_key="media_generation", prior_stages=["bible_review"])
        with self.assertRaises(ValidationError):
            prepare_recipe_media_unit(stage_run_id=self.stage.pk, expected_run_plan_sha256=self.plan.sha256,
                scope={"kind": "character", "id": str(uuid4())}, candidate_number=1, actor=self.user,
                material_unit_key=f"appearance:{self.settings[0].variant_id}")

    def test_binding_does_not_mutate_authored_template(self):
        source = deepcopy(self.route()["input_sources"])
        before = deepcopy(source)
        binding = recipe_material_binding(self.definition, source)
        self.assertEqual(binding["primary_input"], {"input_key": "reference_2", "item_key": "image_2"})
        bound = bind_material_sources(source, version=self.primary, difference=self.variant_anchors[1])
        self.assertEqual(source, before)
        self.assertNotEqual(bound, before)

    def test_dispatch_requires_original_current_dependency_but_frozen_reader_does_not_reselect(self):
        from studio.services.production_recipe_material_inputs import read_recipe_material_binding
        from studio.services.production_recipe_routing import require_current_recipe_preparation
        dependency = self.dependency()
        with patch("studio.services.production_recipe_material_registration.read_registered_recipe_primary_dependency", return_value=dependency):
            preparation = self.prepare()
            require_current_recipe_preparation(preparation, stage=self.stage)
        original = deepcopy(read_execution_preparation_detail(preparation).resolved_inputs)
        with patch("studio.services.production_recipe_material_registration.read_registered_recipe_primary_dependency", return_value=self.dependency()):
            with self.assertRaisesMessage(ValidationError, "主图选择已改变"):
                require_current_recipe_preparation(preparation, stage=self.stage)
        with patch("studio.services.production_recipe_material_registration.read_registered_recipe_primary_dependency", side_effect=AssertionError("No current choice")):
            self.assertEqual(read_recipe_material_binding(preparation)["primary"]["decision_id"], str(dependency["decision"].pk))
        self.assertEqual(read_execution_preparation_detail(preparation).resolved_inputs, original)
