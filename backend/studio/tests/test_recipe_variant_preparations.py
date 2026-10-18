"""M3 review -> exact variant preparation, without dispatch or live writes.

Uses real isolated PNGs, published workflow/Profile and original review writer.
Fixed media mappings exercise preparation; dynamic dependency dispatch is NOT
implemented or simulated by these tests.
"""
from copy import deepcopy
from unittest.mock import patch
from uuid import uuid4

from django.core.exceptions import ValidationError
from django.test import override_settings

from studio.models import ExecutionPreparation, GenerationAttempt, ProductionRecipeVersion, ProductionRun, StageRun
from studio.services.configuration import resolve_configuration
from studio.services.execution_definitions import build_execution_definition
from studio.services.execution_instances import create_execution_draft, create_execution_instance, publish_execution_version
from studio.services.json_contracts import assign_json_contract
from studio.services.media_generation_targets import read_media_generation_target
from studio.services.production_recipe_media_preparations import prepare_recipe_media_unit, read_recipe_media_candidate
from studio.services.production_recipe_routing import require_current_recipe_preparation
from studio.services.production_run_plans import freeze_run_execution_plan, get_run_execution_plan
from studio.services.production_stage_catalog import production_stage_catalog
from studio.services.security_boundaries import boundary_int
from studio.tests import test_media_appearance_profile_images as image_fixtures
from studio.tests import test_media_generation_appearances as appearance_fixtures
from studio.tests.test_data_structures import StructureFixture
from studio.tests.test_execution_media_source_policy import choices


@override_settings(AI_PROVIDER_ALLOWED_HOSTS=("127.0.0.1", "api.example.com"))
class RecipeVariantPreparationTests(StructureFixture):
    create_job = image_fixtures.ProfileImageAppearanceTests.create_job
    primary_version = image_fixtures.ProfileImageAppearanceTests.primary_version
    write = image_fixtures.ProfileImageAppearanceTests.write
    input = staticmethod(image_fixtures.ProfileImageAppearanceTests.input)

    def setUp(self):
        image_fixtures.ProfileImageAppearanceTests.setUp(self)
        self.primary_setting = appearance_fixtures.create_setting(self.user, self.project, self.character)
        self.settings = [appearance_fixtures.create_setting(self.user, self.project, self.character,
            primary_setting=self.primary_setting, appearance_anchor=anchor)
            for anchor in getattr(self, "variant_anchors", ("蓝色外套", "蓝色外套"))]
        self.primary = self.primary_version(setting=self.primary_setting, asset=self.assets[1])
        from studio.services.prompt_profile_structures import STRUCTURED_INPUT_SELECTOR
        self.inputs[STRUCTURED_INPUT_SELECTOR]["value"]["changes"][1]["detail.text"] = self.settings[0].appearance_anchor
        config = deepcopy(self.definition.value["configuration"])
        for spec in config["inputs"]:
            if spec["kind"] == "image":
                spec["media_source_policy"] = choices()
        instance = create_execution_instance(key="recipe-variant-fixture", name="Recipe variant", description="", actor=self.user)
        self.version = create_execution_draft(instance_id=instance.pk, expected_revision=instance.revision, actor=self.user,
            selection={"deployment": self.deployment, "operation": "image.image_to_image", "mode": "",
                "workflow_version": self.workflow, "prompt_profile_version": self.prompt_profile, "prompt_mode": "profile"},
            configuration={"inputs": config["inputs"], "auxiliaries": config["auxiliaries"]})
        self.definition = build_execution_definition(self.version.pk)
        self.version = publish_execution_version(version_id=self.version.pk, expected_revision=self.version.revision,
            expected_definition_sha256=self.definition.sha256, actor=self.user)
        sources = deepcopy(self.inputs)
        for key, source in list(sources.items()):
            if "items" in source:
                selection = {"source": "asset_library", "items": [
                    {name: value for name, value in item.items() if name != "kind"} for item in source["items"]]}
                sources[key] = {"kind": "media_selection", "selection": selection}
                self.inputs[key] = {"selection": {"source": "asset_library", "items": deepcopy(source["items"])}}
        self.parameters = {"image.width": 16, "image.height": 24, "sampling.seed": 42,
            "sampling.cfg_scale": 6.5, "sampling.scheduler": "karras"}
        route = {"execution_instance_version_id": str(self.version.pk), "expected_definition_sha256": self.definition.sha256,
            "input_sources": sources, "parameter_policies": [
                {"key": key, "policy": "locked_value", "value": value} for key, value in self.parameters.items()]}
        other = deepcopy(route)
        other["parameter_policies"][0]["value"] = 32
        self.configure_variant_sources(route["input_sources"])
        self.declaration = {"key": "bible_image_generation", "mode": "existing_pipeline", "approval": "required",
            "routing": {"mode": "fixed", "variant_count": 1, "evaluator": None, "routes": [other]},
            "variant_routing": {"mode": "manual_choice", "variant_count": 3, "evaluator": "first_succeeded", "routes": [other, route]}}
        self.configure_declaration(self.declaration)
        recipe = ProductionRecipeVersion(recipe_key="variant-isolated", version=1, name="Isolated", status="published")
        assign_json_contract(recipe, "definition", {"schema_version": 3, "interaction_mode": "assisted", "stages": [
            {"key": "bible_review", "mode": "manual", "approval": "required"},
            *deepcopy(getattr(self, "extra_before_media_declarations", [])), self.declaration,
            deepcopy(getattr(self, "review_declaration", {"key": "bible_image_review", "mode": "review", "approval": "required"})),
            *deepcopy(getattr(self, "extra_after_review_declarations", []))]}, schema_version=3)
        recipe.save()
        self.run = ProductionRun.objects.create(owner=self.user, project=self.project, recipe_version=recipe, plan_source="recipe",
            status="awaiting_approval", current_stage_key="bible_review", idempotency_key=str(uuid4()), request_sha256="a" * 64)
        assign_json_contract(self.run, "config_snapshot", resolve_configuration(run=self.run).snapshot(),
            max_bytes=boundary_int("security.snapshot.stage_max_bytes"))
        assign_json_contract(self.run, "input_snapshot", {"schema_version": 1, "interaction_mode": "assisted"})
        self.run.save()
        catalog = production_stage_catalog()
        freeze_run_execution_plan(run=self.run, scope={"kind": "project"}, delivery={"kind": "episode_masters"}, initial_content=None,
            stage_catalog_key=catalog.value["key"], expected_stage_catalog_sha256=catalog.sha256, actor=self.user)
        self.plan = get_run_execution_plan(self.run)
        self.content_review = StageRun.objects.create(production_run=self.run, stage_key="bible_review", mode="manual",
            status="awaiting_approval", ordinal=1, approval_required=True, idempotency_key=str(uuid4()))
        self.stage = StageRun.objects.create(production_run=self.run, stage_key="bible_image_generation", mode="existing_pipeline",
            status="pending", ordinal=2 + len(getattr(self, "extra_before_media_declarations", [])), idempotency_key=str(uuid4()))
        self.scope = {"kind": "character", "id": str(self.character.pk)}
        from studio.services.production_runs import _freeze_stage_config, _freeze_stage_input, decide_stage, select_stage_route
        from studio.services.bible_content_review import recipe_bible_review_detail
        _freeze_stage_config(self.run, self.content_review)
        _freeze_stage_input(self.run, self.content_review)
        _freeze_stage_config(self.run, self.stage)
        if self.declaration.get("variant_routing", {}).get("mode") == "manual_choice":
            select_stage_route(self.run, stage_key=self.stage.stage_key, route_index=1, appearance_role="variant", user=self.user)
        detail = recipe_bible_review_detail(self.run)
        # Only unrelated delivery closure and subsequent scheduling are isolated.
        with patch("studio.services.production_runs.advance_production_run", side_effect=lambda run: run), patch(
                "studio.services.production_runs._assert_frozen_bible_run_closure"):
            self.run = decide_stage(self.run, stage_key="bible_review", decision="approve", user=self.user,
                comment="Isolated variant preparation", bible_review={
                    "expected_source_snapshot_sha256": detail["source_snapshot_sha256"],
                    "expected_project_bible_sha256": detail["project_bible_sha256"], "reviewed_project_bible": True,
                    "reviewed_candidate_ids": [row["id"] for row in detail["candidates"]],
                    "reviewed_setting_keys": [row["review_key"] for row in detail["settings"]]})
        self.run.status, self.run.current_stage_key = "running", self.stage.stage_key
        self.run.save(update_fields=("status", "current_stage_key"))
        self.stage.refresh_from_db()
        self.stage.status = "running"
        self.stage.save(update_fields=("status",))
        if hasattr(self, "create_prior_media_stages"):
            self.create_prior_media_stages()
        _freeze_stage_input(self.run, self.stage)

    def configure_variant_sources(self, sources):
        pass

    def configure_declaration(self, declaration):
        pass

    def target(self, index=0):
        return {**image_fixtures.ProfileImageAppearanceTests.request(self, "structured_field"),
            "setting_revision_id": str(self.settings[index].pk)}

    def prepare(self, *, target=None, number=1):
        return prepare_recipe_media_unit(stage_run_id=self.stage.pk, expected_run_plan_sha256=self.plan.sha256,
            scope=self.scope, candidate_number=number, actor=self.user, appearance_target=target or self.target())

    def tearDown(self):
        self.external.assert_not_called()
        self.assertFalse(GenerationAttempt.objects.exists())

    def test_siblings_use_separate_units_same_exact_primary_and_variant_route(self):
        preparations = [self.prepare(target=self.target(index), number=3) for index in range(2)]
        self.assertEqual(len({row.unit_key for row in preparations}), 2)
        for index, preparation in enumerate(preparations):
            target = read_media_generation_target(preparation).value
            facts = preparation.contract.resolved_inputs
            self.assertEqual(preparation.unit_key, f"appearance:{self.settings[index].variant_id}")
            self.assertEqual(preparation.request_key, "route:1:candidate:3")
            self.assertEqual(target["candidates"], {"count": 3, "evaluator": "first_succeeded"})
            self.assertEqual(target["appearance"]["primary"]["version_id"], str(self.primary.pk))
            self.assertEqual(facts["parameters"]["values"]["image.width"], 16)
            self.assertEqual(facts["recipe_material_requirement"]["target"]["setting"], target["setting"])
            self.assertNotIn("bible_target", facts)
            self.assertEqual(facts["profile_sources"]["scope_units"], [
                {"kind": "project", "id": str(self.project.pk), "unit_keys": ["first", "second"]}])
            self.assertEqual(read_recipe_media_candidate(preparation, stage=self.stage, plan=self.plan,
                declaration=self.declaration), (self.scope, 3))
        self.assertEqual(ProductionRun.objects.count(), 1)
        response = self.client.get(f"/api/v1/execution-preparations/{preparations[0].pk}/")
        self.assertEqual(response.status_code, 200, response.data)
        self.assertEqual(response.data["resolved_inputs"]["media_generation_target"],
            read_media_generation_target(preparations[0]).value)

    def test_independent_and_recipe_share_appearance_contract_and_actual_image_inputs(self):
        recipe = self.prepare()
        job, _ = self.create_job(self.target(), key="same-variant-independent", manual_channels={},
            source_language=self.project.language, target_language=self.project.language)
        independent = job.execution_preparations.get()
        left, right = read_media_generation_target(recipe).value, read_media_generation_target(independent).value
        self.assertEqual(left["appearance"], right["appearance"])
        self.assertEqual(left["setting"], right["setting"])
        self.assertEqual(left["candidates"], {"count": 3, "evaluator": "first_succeeded"})
        self.assertEqual(right["candidates"], {"count": 1, "evaluator": None})
        self.assertEqual({**left, "candidates": right["candidates"]}, right)
        for preparation in (recipe, independent):
            item = preparation.contract.resolved_inputs["inputs"]["logical_values"]["reference_2"][0]
            self.assertEqual(item["asset_id"], str(self.primary.asset_id))
            self.assertEqual(item["media_facts"]["sha256"], left["appearance"]["primary"]["sha256"])
        self.assertIsNone(recipe.asset_job_id)
        self.assertIsNone(independent.stage_run_id)
        self.assertEqual(ProductionRun.objects.count(), 1)

    def test_replay_does_not_read_current_requirement_or_asset_and_changed_primary_conflicts(self):
        preparation = self.prepare()
        original = deepcopy(preparation.contract.resolved_inputs)
        with patch("studio.services.bible_material_requirements.read_bible_material_requirements", side_effect=AssertionError("No rereview")), patch(
                "studio.services.media_generation_appearances.resolve_execution_asset_media", side_effect=AssertionError("No reprobe")):
            self.assertEqual(self.prepare().pk, preparation.pk)
        with self.assertRaises(ValidationError):
            self.prepare(target={**self.target(), "primary_version_id": str(uuid4())})
        preparation.contract.refresh_from_db()
        self.assertEqual(preparation.contract.resolved_inputs, original)
        self.assertEqual(ExecutionPreparation.objects.count(), 1)

    def test_wrong_primary_port_difference_or_unreviewed_setting_rolls_back(self):
        target = self.target()
        invalid = [(dict(target, primary_version_id=str(uuid4())), "确切主图素材版本"),
            (dict(target, setting_revision_id=None), "配方变体必须引用"),
            (dict(target, setting_revision_id=str(self.primary_setting.pk)), "没有此单元"),
            (dict(target, primary_input={"input_key": "reference_1", "item_key": "image_1"}), "原解析输入"),
            (dict(target, difference_input={"kind": "structured_field", "key": "units", "field_key": "absent"}), "唯一的原结构化直通字段"),
            (dict(target, difference_input={"kind": "reference_field", "key": "units", "unit_key": "first",
                "field_key": "unit_description"}), "实际差异或主图依赖")]
        before = self.stage.route_events.count()
        for value, message in invalid:
            with self.subTest(value=value), self.assertRaisesMessage(ValidationError, message):
                self.prepare(target=value)
        with self.assertRaisesMessage(ValidationError, "候选编号超过冻结配方数量"):
            self.prepare(number=4)
        self.assertFalse(ExecutionPreparation.objects.exists())
        self.assertEqual(self.stage.route_events.count(), before)

    def test_changed_reviewed_setting_blocks_new_candidate_without_rewriting_history(self):
        from studio.services.bible_variants import revise_variant, setting_payload
        from studio.services.production_recipe_media_preparations import require_current_recipe_material_requirement
        preparation = self.prepare()
        original = deepcopy(preparation.contract.resolved_inputs)
        revise_variant(actor=self.user, variant=self.settings[0].variant, expected_revision=1,
            value={**setting_payload(self.settings[0]), "appearance_anchor": "New unreviewed difference"})
        self.assertEqual(self.prepare().pk, preparation.pk)
        with self.assertRaisesMessage(ValidationError, "已审核内容已变更"):
            self.prepare(number=2)
        with self.assertRaisesMessage(ValidationError, "已审核内容已变更"):
            require_current_recipe_material_requirement(preparation, stage=self.stage)
        preparation.contract.refresh_from_db()
        self.assertEqual(preparation.contract.resolved_inputs, original)
        self.assertEqual(ExecutionPreparation.objects.count(), 1)

    def test_preparation_does_not_open_dispatch_or_primary_writeback(self):
        preparation = self.prepare()
        with self.assertRaisesMessage(ValidationError, "缺少本轮确切主图"):
            require_current_recipe_preparation(preparation, stage=self.stage)
        from studio.services.production_recipe_media_stages import advance_recipe_media_stage
        with self.assertRaisesMessage(ValidationError, "尚未配置本轮主图"):
            advance_recipe_media_stage(self.run, self.stage)

    def test_primary_dependency_uses_exact_run_selection_not_an_existing_library_version(self):
        from studio.services.production_recipe_material_selection import read_recipe_primary_dependency
        for setting in self.settings:
            # The fixture already has a valid library primary version. That is
            # not a generated-candidate choice and must never silently satisfy
            # an explicitly in-run primary dependency.
            self.assertIsNone(read_recipe_primary_dependency(stage=self.stage,
                unit_key=f"appearance:{setting.variant_id}"))
        with self.assertRaisesMessage(ValidationError, "只有变体单元"):
            read_recipe_primary_dependency(stage=self.stage, unit_key=f"appearance:{self.primary_setting.variant_id}")
        self.assertFalse(ExecutionPreparation.objects.exists())

    def test_primary_dependency_projection_preserves_exact_setting_and_sibling_identity(self):
        from studio.services.production_recipe_material_selection import read_recipe_primary_dependency, _context
        expected = self.stage.input_snapshot["material_requirements"]["targets"]
        primary = next(row for row in expected if row["appearance_role"] == "primary")
        # Only the upstream selection result is isolated here; its actual
        # queue/archive/decision validation has separate integration coverage.
        with patch("studio.services.production_recipe_material_selection.read_recipe_material_selection",
                return_value={"target": primary}) as reader:
            for setting in self.settings:
                self.assertEqual(read_recipe_primary_dependency(stage=self.stage,
                    unit_key=f"appearance:{setting.variant_id}"), {"target": primary})
                self.assertEqual(reader.call_args.kwargs["unit_key"], f"appearance:{self.primary_setting.variant_id}")
        key = f"appearance:{self.settings[0].variant_id}"
        context = list(_context(self.stage, key))
        context[2] = deepcopy(context[2])
        context[2]["primary_dependency"]["requirement_key"] = f"setting:{uuid4()}"
        with patch("studio.services.production_recipe_material_selection._context", return_value=tuple(context)), patch(
                "studio.services.production_recipe_material_selection.read_recipe_material_selection") as reader:
            with self.assertRaisesMessage(ValidationError, "没有变体所需的确切主图修订"):
                read_recipe_primary_dependency(stage=self.stage, unit_key=key)
            reader.assert_not_called()
