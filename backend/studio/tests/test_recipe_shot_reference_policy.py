"""Isolated recipe authoring/frozen input, not actual storyboard adoption.

The M4 fixture uses real selected/fulfilled outputs. Its downstream stage and
Shot uses are explicit fixtures; no recipe selection command is claimed here.
"""
from copy import deepcopy
from unittest.mock import patch

from django.core.exceptions import PermissionDenied, ValidationError
from django.db import connection
from django.test import SimpleTestCase, override_settings
from django.test.utils import CaptureQueriesContext
from django.contrib.auth.models import AnonymousUser

from studio.models import ProductionRecipeVersion, ShotReferenceUse, StageRun
from studio.services.production_recipe_contracts import recipe_stage_declarations
from studio.services.production_recipe_shot_references import SHOT_REFERENCE_SOURCE_OPTIONS
from studio.services.production_shot_reference_defaults import plan_configured_shot_reference_choices
from studio.services.production_runs import _freeze_stage_config, _freeze_stage_input, _save_stage_snapshot
from studio.tests import test_production_shot_reference_defaults as fixtures
from studio.tests.test_data_structures import StructureFixture


RULE = {"source": "upstream", "source_stage_key": "bible_image_generation",
    "selection": "unique_setting", "allow_override": False}
REVIEW = {"key": "storyboard_review", "mode": "review", "approval": "required", "shot_reference_selection": RULE}


class ShotReferencePolicyShapeTests(SimpleTestCase):
    def test_openapi_exposes_complete_source_strategy_catalog(self):
        from drf_spectacular.generators import SchemaGenerator
        models = SchemaGenerator().get_schema(request=None, public=True)["components"]["schemas"]
        self.assertIn("shot_reference_source_options", models["ProductionRecipeEditorOptions"]["required"])
        option = models["RecipeShotReferenceSourceOption"]
        self.assertEqual(set(option["required"]), {"value", "label", "upstream_stage_key", "selection_options"})
        self.assertTrue(option["properties"]["upstream_stage_key"]["nullable"])

    def definition(self, **changes):
        return {"schema_version": 3, "interaction_mode": "assisted", "stages": [
            {"key": "bible_image_generation", "mode": "existing_pipeline", "approval": "auto"},
            {**deepcopy(REVIEW), "shot_reference_selection": {**RULE, **changes}}]}

    def test_all_registered_sources_are_explicit_and_do_not_mutate_input(self):
        for option in SHOT_REFERENCE_SOURCE_OPTIONS:
            for selection in option["selection_options"]:
                with self.subTest(source=option["value"], selection=selection["value"]):
                    value = self.definition(source=option["value"], source_stage_key=option["upstream_stage_key"],
                        selection=selection["value"])
                    before = deepcopy(value)
                    self.assertEqual(recipe_stage_declarations(value), value["stages"])
                    self.assertEqual(before, value)

    def test_no_implicit_strategy_permission_model_purpose_or_source(self):
        for changes in ({"source": []}, {"source": "latest"}, {"selection": "auto"}, {"selection": []},
                {"allow_override": 1}, {"allow_override": "false"}, {"allow_override": None},
                {"source_stage_key": "storyboard_review"}, {"model_purpose": None},
                {"source": "existing", "source_stage_key": None}, {"source": "upload", "source_stage_key": None}):
            with self.subTest(changes=changes), self.assertRaises(ValidationError):
                recipe_stage_declarations(self.definition(**changes))
        for value in (None, {}, [], {k: v for k, v in RULE.items() if k != "allow_override"}):
            definition = self.definition()
            definition["stages"][1]["shot_reference_selection"] = value
            with self.subTest(value=value), self.assertRaises(ValidationError):
                recipe_stage_declarations(definition)

    def test_wrong_duty_mode_absent_or_later_upstream_and_idle_source_rejected(self):
        values = []
        for change in ({"key": "bible_image_review"}, {"mode": "manual"}):
            value = self.definition()
            value["stages"][1].update(change)
            values.append(value)
        value = self.definition()
        value["stages"][0]["mode"] = "manual"
        values.extend([value, {**self.definition(), "stages": [deepcopy(REVIEW)]},
            {**self.definition(), "stages": list(reversed(self.definition()["stages"]))},
            self.definition(source="existing", selection="manual")])
        for value in values:
            with self.subTest(value=value), self.assertRaises(ValidationError):
                recipe_stage_declarations(value)

    def test_absence_does_not_manufacture_defaults_or_permissions(self):
        value = self.definition()
        value["stages"][1].pop("shot_reference_selection")
        self.assertNotIn("shot_reference_selection", recipe_stage_declarations(value)[1])


@override_settings(AI_PROVIDER_ALLOWED_HOSTS=("127.0.0.1", "api.example.com"))
class FrozenShotReferencePolicyTests(StructureFixture):
    Fixture = fixtures.ShotReferenceDefaultTests.Fixture
    review_declaration = REVIEW
    tearDown = fixtures.ShotReferenceDefaultTests.tearDown
    facts = fixtures.ShotReferenceDefaultTests.facts
    attach = fixtures.ShotReferenceDefaultTests.attach
    choose = fixtures.ShotReferenceDefaultTests.choose
    create_job = fixtures.ShotReferenceDefaultTests.create_job
    primary_version = fixtures.ShotReferenceDefaultTests.primary_version
    write = fixtures.ShotReferenceDefaultTests.write
    input = staticmethod(fixtures.ShotReferenceDefaultTests.input)
    configure_variant_sources = fixtures.ShotReferenceDefaultTests.configure_variant_sources
    configure_declaration = fixtures.ShotReferenceDefaultTests.configure_declaration
    target = fixtures.ShotReferenceDefaultTests.target
    prepare = fixtures.ShotReferenceDefaultTests.prepare
    complete = fixtures.ShotReferenceDefaultTests.complete
    use = fixtures.ShotReferenceDefaultTests.use

    def setUp(self):
        fixtures.ShotReferenceDefaultTests.setUp(self)
        # The downstream review is a declared Stage fixture, not a fake human
        # review/adoption or a change to production history.
        self.review.stage_key = "storyboard_review"
        self.review.save(update_fields=["stage_key"])
        self.use(setting=self.settings[0])
        _freeze_stage_config(self.run, self.review)
        _freeze_stage_input(self.run, self.review)

    def configured(self, **changes):
        return plan_configured_shot_reference_choices(**{"stage_run_id": self.review.pk,
            "actor": self.user, "shot_id": self.shots[0].pk, **changes})

    def test_original_runplan_and_stage_input_freeze_rule_and_original_output_identity(self):
        self.assertEqual(self.plan.value["recipe"]["definition"]["stages"][-1], REVIEW)
        self.assertEqual(self.review.input_snapshot["shot_reference_selection"], {"rule": RULE,
            "upstream": {"stage_run_id": str(self.stage.pk), "output_snapshot_sha256": self.stage.output_snapshot_sha256}})
        before = (self.facts(), list(ShotReferenceUse.objects.values()))
        with CaptureQueriesContext(connection) as queries:
            planned = self.configured()
            _freeze_stage_input(self.run, self.review)
        self.assertFalse(fixtures.ShotReferenceDefaultTests.writes(queries))
        self.assertTrue(planned.ready)
        self.assertEqual(planned.choices[0]["reference_version_id"], str(self.fulfillments[1].reference_version_id))
        self.assertEqual(before, (self.facts(), list(ShotReferenceUse.objects.values())))

    def test_current_recipe_edit_cannot_replace_frozen_override_or_source(self):
        before = self.configured().choices
        # Deliberately corrupt current registry bytes in the isolated database;
        # the persisted RunPlan is still the only authority for this old Run.
        ProductionRecipeVersion.objects.filter(pk=self.run.recipe_version_id).update(definition={"changed": True})
        self.assertEqual(self.configured().choices, before)
        self.review.refresh_from_db()
        self.assertFalse(self.review.input_snapshot["shot_reference_selection"]["rule"]["allow_override"])

    def test_rehashed_new_upstream_output_does_not_rebind_frozen_source(self):
        value = deepcopy(self.stage.output_snapshot)
        value["changed"] = "isolated later result"
        _save_stage_snapshot(self.stage, "output_snapshot", value)
        with self.assertRaisesMessage(ValidationError, "上游已改变"):
            self.configured()
        self.assertFalse(ShotReferenceUse.objects.exclude(source_mode="").exists())

    def test_forged_frozen_rule_or_missing_input_is_not_authorization(self):
        value = deepcopy(self.review.input_snapshot)
        value["shot_reference_selection"]["rule"]["allow_override"] = True
        _save_stage_snapshot(self.review, "input_snapshot", value)
        with self.assertRaisesMessage(ValidationError, "不能用新结果"):
            self.configured()
        StageRun.objects.filter(pk=self.review.pk).update(input_snapshot_sha256="")
        with self.assertRaises(ValidationError):
            self.configured()
        with self.assertRaises(PermissionDenied):
            self.configured(actor=AnonymousUser())

    def test_old_human_entity_anchor_writer_has_been_removed(self):
        from studio.services import production_runs, production_recipe_shot_references
        self.assertFalse(hasattr(production_runs, "_approve_storyboard_reference_bindings"))
        self.assertFalse(hasattr(production_recipe_shot_references, "require_shot_reference_command_adapter"))

    def test_editor_options_serializer_owns_source_strategy_catalog(self):
        from studio.platform_serializers import ProductionRecipeEditorOptionsSerializer
        from studio.services.production_runs import production_recipe_editor_options
        data = ProductionRecipeEditorOptionsSerializer(production_recipe_editor_options()).data
        self.assertEqual(data["shot_reference_source_options"], list(SHOT_REFERENCE_SOURCE_OPTIONS))
