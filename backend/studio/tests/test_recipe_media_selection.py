"""Recipe-owned choices, original frozen plans and real isolated Asset reads.

The authoring API test supplies a service contract fixture; it is not published
workflow acceptance. No model, Smoke, business database or real upload is used.
"""
from copy import deepcopy
from unittest.mock import patch
from uuid import uuid4

from django.core.exceptions import ValidationError
from django.test import SimpleTestCase, TestCase, override_settings
from django.urls import reverse

from studio.models import ExecutionPreparation, GenerationAttempt, ProductionRecipeVersion, ProductionRun, StageRun
from studio.services.asset_provenance import ASSET_UPLOAD_SOURCE_INFO
from studio.services.execution_input_resolution import resolve_execution_input_sources
from studio.services.json_contracts import assign_json_contract, json_contract
from studio.services.production_recipe_contracts import validate_recipe_input_sources
from studio.services.production_recipe_media_preparations import resolve_recipe_media_sources
from studio.services.production_recipe_media_selection import bind_recipe_media_selection
from studio.services.production_run_plans import freeze_run_execution_plan, get_run_execution_plan
from studio.services.production_stage_catalog import production_stage_catalog
from studio.tests import test_production_reviewed_references as reference_fixture
from studio.tests import test_recipe_schema3_authoring as author_fixture
from studio.tests.test_execution_media_source_policy import choices


def spec():
    return {"key": "images", "position": 0, "kind": "image", "required": True, "min_items": 1, "max_items": 2,
        "media_source_policy": choices(), "semantic_contract": {"schema_version": 1,
            "allowed_purposes": [], "allowed_subject_kinds": [], "allowed_scope_kinds": ["request"],
            "paired_with_input_key": None, "allow_repeated_asset": True},
        "targets": [{"target_kind": "workflow_slot", "target_key": "images", "is_primary": True}]}


def definition(input_spec=None):
    return json_contract({"schema_version": 1, "configuration": {"inputs": [input_spec or spec()]},
        "resources": {"conditioning_mode": None}, "interface": {"input_targets": {"images": {
            "target_kind": "workflow_slot", "target_key": "images", "kind": "image", "min_items": 1, "max_items": 2,
            "wiring": {"kind": "semantic_group", "semantic_keys": ["ref.one", "ref.two"]}}}}})


def rule(asset_id="12345678-1234-1234-1234-123456789012", source="asset_library"):
    return {"kind": "media_selection", "selection": {"source": source,
        "items": [{"item_key": "explicit-one", "purpose": None, "asset_id": str(asset_id)}]}}


class RecipeMediaSelectionContractTests(SimpleTestCase):
    def test_explicit_selection_and_override_use_same_instance_rules_without_io(self):
        value = rule()
        original = deepcopy(value)
        validate_recipe_input_sources(definition(), {"images": value}, prior_stages=[])
        bound = bind_recipe_media_selection(value, spec())
        self.assertEqual(bound["selection"]["items"][0]["scope_ref"], {"kind": "request"})
        self.assertEqual(value, original)
        changed = bind_recipe_media_selection(rule(source="asset_upload"), spec())
        self.assertEqual(changed["selection"], {"source": "asset_library", "items": []})
        self.assertEqual(changed["override"]["source"], "asset_upload")
        locked = spec(); locked["media_source_policy"]["allow_override"] = False
        with self.assertRaises(ValidationError):
            bind_recipe_media_selection(rule(source="asset_upload"), locked)

    def test_invalid_or_unsupported_choices_do_not_become_draft_success(self):
        for value in ({**rule(), "max_items": 3}, rule(source="selected_output"), rule(source="bible_reference"),
                {"kind": "media_selection", "selection": None},
                {"kind": "media_selection", "selection": {"source": "asset_library", "items": []}},
                rule(asset_id="file:///tmp/example.png")):
            with self.subTest(value=value), self.assertRaises(ValidationError):
                validate_recipe_input_sources(definition(), {"images": value}, prior_stages=[], require_complete=False)
        for input_spec in ({**spec(), "media_source_policy": None}, {**spec(), "kind": "text"}):
            with self.subTest(input_spec=input_spec), self.assertRaises(ValidationError):
                bind_recipe_media_selection(rule(), input_spec)

    def test_quantity_empty_and_order_remain_explicit(self):
        input_spec = spec(); input_spec.update(required=False, min_items=0)
        empty = rule(); empty["selection"]["items"] = []
        self.assertEqual(bind_recipe_media_selection(empty, input_spec)["selection"]["items"], [])
        value = rule(); value["selection"]["items"].insert(0, {**value["selection"]["items"][0], "item_key": "other"})
        items = bind_recipe_media_selection(value, spec())["selection"]["items"]
        self.assertEqual([(row["item_key"], row["order"]) for row in items], [("other", 0), ("explicit-one", 1)])
        input_spec["max_items"] = 1
        with self.assertRaises(ValidationError):
            bind_recipe_media_selection(value, input_spec)


class RecipeMediaSelectionReadTests(TestCase):
    setUp = reference_fixture.ReviewedReferenceTests.setUp
    approve = reference_fixture.ReviewedReferenceTests.approve

    def resolve(self, source, owner=None):
        contract = definition()
        with patch("studio.services.production_recipe_media_preparations.read_reviewed_shot_references",
                   side_effect=AssertionError("Explicit Assets must not read a different selection")):
            bound = resolve_recipe_media_sources(definition=contract, route={"input_sources": {"images": source}},
                stage=self.stage, plan=get_run_execution_plan(self.run), scope={"kind": "shot", "id": str(self.shot.pk)})
        self.assertIsNone(bound["review"])
        return resolve_execution_input_sources(contract, bound["inputs"], owner=owner or self.actor,
            subject_facts=[], max_media_bytes=65536, project_id=self.project.pk,
            production_run_id=str(self.run.pk), eligible_stage_ids=bound["eligible_stage_ids"]).value

    def test_real_asset_read_uses_exact_choice_and_records_override(self):
        source = rule(self.asset.pk)
        record = self.resolve(source)["logical_values"]["images"][0]
        self.assertEqual(record["asset_id"], str(self.asset.pk))
        self.assertEqual(record["media_facts"]["sha256"], self.asset.checksum)
        self.assertEqual(record["source_selection"], {"source": "asset_library", "overridden": False})
        with self.assertRaises(ValidationError):
            self.resolve(rule(self.asset.pk, "asset_upload"))
        self.asset.source_info = ASSET_UPLOAD_SOURCE_INFO; self.asset.save(update_fields=["source_info"])
        record = self.resolve(rule(self.asset.pk, "asset_upload"))["logical_values"]["images"][0]
        self.assertEqual(record["source_selection"], {"source": "asset_upload", "overridden": True})
        self.assertFalse(ExecutionPreparation.objects.exists()); self.assertFalse(GenerationAttempt.objects.exists())

    def test_missing_inactive_or_wrong_owner_does_not_fallback_to_reviewed_image(self):
        with self.assertRaises(ValidationError): self.resolve(rule())
        with self.assertRaises(ValidationError): self.resolve(rule(self.asset.pk), self.other)
        self.asset.is_active = False; self.asset.save(update_fields=["is_active"])
        with self.assertRaises(ValidationError): self.resolve(rule(self.asset.pk))

    def test_run_plan_keeps_original_recipe_choice_after_current_configuration_changes(self):
        recipe = ProductionRecipeVersion(recipe_key="explicit-assets", version=1, name="Frozen selection", status="published")
        route = {"execution_instance_version_id": str(uuid4()), "expected_definition_sha256": "a" * 64,
                 "input_sources": {"images": rule(self.asset.pk)}, "parameter_policies": []}
        value = {"schema_version": 3, "interaction_mode": "assisted", "stages": [
            {"key": "media_generation", "mode": "existing_pipeline", "approval": "required",
             "routing": {"mode": "fixed", "variant_count": 1, "routes": [route], "evaluator": None}}]}
        assign_json_contract(recipe, "definition", value, schema_version=3); recipe.save()
        run = ProductionRun.objects.create(owner=self.actor, project=self.project, recipe_version=recipe,
            plan_source="recipe", status="running", idempotency_key=str(uuid4()), request_sha256="b" * 64)
        catalog = production_stage_catalog()
        freeze_run_execution_plan(run=run, scope={"kind": "project"}, delivery={"kind": "episode_masters"},
            initial_content=None, stage_catalog_key=catalog.value["key"], expected_stage_catalog_sha256=catalog.sha256, actor=self.actor)
        stage = StageRun.objects.create(production_run=run, stage_key="media_generation", ordinal=1,
            mode="existing_pipeline", status="running", idempotency_key=str(uuid4()))
        # Isolated storage drift only: the frozen plan must never reread it.
        changed = deepcopy(value); changed["stages"][0]["routing"]["routes"][0]["input_sources"] = {}
        assign_json_contract(recipe, "definition", changed, schema_version=3); recipe.save()
        plan = get_run_execution_plan(run)
        frozen_route = plan.value["recipe"]["definition"]["stages"][0]["routing"]["routes"][0]
        self.assertEqual(frozen_route, route)
        bound = resolve_recipe_media_sources(definition=definition(), route=frozen_route, stage=stage, plan=plan,
            scope={"kind": "shot", "id": str(self.shot.pk)})
        result = resolve_execution_input_sources(definition(), bound["inputs"], owner=self.actor, subject_facts=[], max_media_bytes=65536)
        self.assertEqual(result.value["logical_values"]["images"][0]["asset_id"], str(self.asset.pk))

    def test_reviewed_mapping_uses_declared_reader_and_preserves_original_shot_scope(self):
        from studio.tests.test_production_reference_mapping import mapping
        input_spec = spec(); input_spec["semantic_contract"].update(allowed_scope_kinds=["shot"], allowed_purposes=["configured_style"])
        contract = definition(input_spec)
        def bind():
            return resolve_recipe_media_sources(definition=contract, route={"input_sources": {"images": mapping("images")}},
                stage=self.stage, plan=get_run_execution_plan(self.run), scope={"kind": "shot", "id": str(self.shot.pk)})
        bound = bind()
        self.assertEqual(bound["inputs"]["images"]["selection"]["source"], "asset_library")
        resolved = resolve_execution_input_sources(contract, bound["inputs"], owner=self.actor, subject_facts=[],
            max_media_bytes=65536, project_id=self.project.pk, production_run_id=str(self.run.pk),
            eligible_stage_ids=bound["eligible_stage_ids"])
        self.assertEqual(resolved.value["logical_values"]["images"][0]["scope_ref"], {"kind": "shot", "id": str(self.shot.pk)})
        self.assertEqual(bound["review"]["decision_id"], str(self.decision.pk))
        input_spec["media_source_policy"]["default_source"] = "selected_output"
        contract = definition(input_spec)
        with self.assertRaisesMessage(ValidationError, "所选来源一致"):
            resolve_execution_input_sources(contract, bind()["inputs"], owner=self.actor, subject_facts=[], max_media_bytes=65536)


@override_settings(AI_PROVIDER_ALLOWED_HOSTS=("api.example.com", "cdn.example.com"))
class RecipeMediaSelectionAuthoringTests(TestCase):
    setUpTestData = classmethod(author_fixture.RecipeSchema3AuthoringTests.setUpTestData.__func__)
    write_contract = staticmethod(author_fixture.RecipeSchema3AuthoringTests.write_contract)
    setUp = author_fixture.RecipeSchema3AuthoringTests.setUp
    post = author_fixture.RecipeSchema3AuthoringTests.post

    def test_save_read_and_clone_keep_selection_not_copied_policy(self):
        from studio.services.execution_definitions import stored_execution_contract
        from studio.services.json_contracts import verify_json_contract
        original = stored_execution_contract(self.version.pk)
        payload = deepcopy(original.definition)
        payload["configuration"]["inputs"] = [spec()]
        payload["interface"]["input_targets"] = definition().value["interface"]["input_targets"]
        service_contract = json_contract(payload)
        self.definition["stages"][0]["routing"]["routes"][0]["input_sources"] = {"images": rule()}
        with patch("studio.services.production_recipe_contracts.verify_current_execution_definition", return_value=service_contract):
            response = self.post()
            self.assertEqual(response.status_code, 201, response.data)
            recipe = ProductionRecipeVersion.objects.get(pk=response.data["id"])
            frozen = verify_json_contract(recipe.definition, schema_version=recipe.definition_schema_version,
                size_bytes=recipe.definition_size_bytes, sha256=recipe.definition_sha256)
            self.assertEqual(frozen.value, self.definition)
            url = reverse("production-recipe-detail", args=[recipe.pk])
            self.assertEqual(self.client.get(url).data["definition"], self.definition)
            recipe.status = "published"; recipe.save(update_fields=["status"])
            cloned = self.client.post(url + "clone-successor/", {"expected_definition_sha256": recipe.definition_sha256}, format="json")
            self.assertEqual(cloned.status_code, 201, cloned.data)
            self.assertEqual(cloned.data["definition"], self.definition)
        self.assertNotIn("media_source_policy", str(recipe.definition))
        self.assertFalse(ExecutionPreparation.objects.exists()); self.assertFalse(GenerationAttempt.objects.exists())
