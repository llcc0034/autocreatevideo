"""Plan storage and source boundaries, not a completed production UI/worker."""
from copy import deepcopy
from uuid import uuid4
from unittest.mock import Mock, patch

from django.contrib.auth import get_user_model
from django.core.exceptions import PermissionDenied, ValidationError
from django.db import connection, IntegrityError, transaction
from django.test import TestCase
from django.test.utils import CaptureQueriesContext

from studio.models import Episode, ProductionManualCommand, ProductionRecipeVersion, ProductionRun, ProductionRunPlan, ScriptRevision, SeriesProject
from studio.services.json_contracts import assign_json_contract, json_contract
from studio.services.production_run_plans import build_run_execution_plan, freeze_run_execution_plan, get_run_execution_plan
from studio.services.production_stage_catalog import production_stage_catalog
from studio.services.script_sources import create_pasted_script_source, confirm_script_revision, create_script_revision
from studio.tests.test_execution_context_inputs import PROJECT_CONFIG


class ProductionRunPlanTests(TestCase):
    def setUp(self):
        self.actor = get_user_model().objects.create_user(username="run-plan-owner")
        self.other = get_user_model().objects.create_user(username="run-plan-other")
        self.project = SeriesProject.objects.create(owner=self.actor, title="Manual project", **PROJECT_CONFIG)
        self.catalog = production_stage_catalog()
        self.request = dict(scope={"kind": "project"}, delivery={"kind": "episode_masters"}, initial_content=None,
            stage_catalog_key=self.catalog.value["key"], expected_stage_catalog_sha256=self.catalog.sha256, actor=self.actor)

    def root(self, **changes):
        fields = dict(owner=self.actor, project=self.project, plan_source="manual", recipe_version=None,
            status="awaiting_approval", idempotency_key=str(uuid4()), request_sha256="a" * 64)
        fields.update(changes)
        run = ProductionRun(**fields)
        for name in ("config_snapshot", "input_snapshot"):
            assign_json_contract(run, name, {"schema_version": 1})
        run.save()
        return run

    def build(self, **changes):
        request = dict(self.request, source="manual", project_id=self.project.pk, recipe_version_id=None)
        request.update(changes)
        return build_run_execution_plan(**request)

    def recipe(self, *, definition=None, status="published", schema=3):
        # Storage fixture, deliberately not evidence of recipe publication or
        # business executability. These tests cover the plan's source boundary.
        row = ProductionRecipeVersion(recipe_key="storage-test", version=ProductionRecipeVersion.objects.count() + 1,
            name="Storage contract", status=status)
        assign_json_contract(row, "definition", definition if definition is not None else {
            "schema_version": schema, "interaction_mode": "assisted", "stages": [
                {"key": "script_input", "mode": "manual", "approval": "required"},
                {"key": "storyboard", "mode": "manual", "approval": "required"},
                {"key": "assembly", "mode": "existing_pipeline", "approval": "required"},
                {"key": "delivery", "mode": "existing_pipeline", "approval": "required"}]}, schema_version=schema)
        row.save()
        return row

    def test_manual_blank_plan_does_not_create_or_select_anything(self):
        recipe_count = ProductionRecipeVersion.objects.count()
        with self.assertNumQueries(1):
            plan = self.build()
        self.assertEqual(plan.value["scope"], {"kind": "project"})
        self.assertIsNone(plan.value["recipe"])
        self.assertIsNone(plan.value["initial_content"])
        self.assertEqual(plan.value["stage_catalog"]["sha256"], self.catalog.sha256)
        self.assertNotIn("execution_instance_version_id", str(plan.value))
        self.assertNotIn("status", str(plan.value))
        self.assertFalse(ProductionRun.objects.exists())
        self.assertEqual(ProductionRecipeVersion.objects.count(), recipe_count)
        self.assertFalse(ScriptRevision.objects.exists())

    def test_run_source_database_union_has_no_implicit_recipe_or_default(self):
        recipe = self.recipe()
        for changes in ({"plan_source": "manual", "recipe_version": recipe},
                {"plan_source": "recipe"}, {"plan_source": ""}, {"plan_source": "unknown"}):
            with self.subTest(changes=changes), self.assertRaises(IntegrityError), transaction.atomic():
                self.root(**changes)
        self.root(plan_source="recipe", recipe_version=recipe)
        self.assertFalse(ProductionRun._meta.get_field("plan_source").has_default())
        for changes in ({"source": "recipe"}, {"recipe_version_id": recipe.pk}, {"source": "unknown"}):
            with self.subTest(changes=changes), self.assertRaises(ValidationError):
                self.build(**changes)

    def test_explicit_scope_is_owned_nonempty_unique_and_canonical(self):
        first = Episode.objects.create(project=self.project, number=1, title="One")
        second = Episode.objects.create(project=self.project, number=2, title="Two")
        ids = [str(first.pk), str(second.pk)]
        one = self.build(scope={"kind": "episodes", "episode_ids": ids})
        two = self.build(scope={"kind": "episodes", "episode_ids": ids[::-1]})
        self.assertEqual(one, two)
        for scope in ({"kind": "episodes", "episode_ids": []}, {"kind": "episodes", "episode_ids": ids * 2},
                {"kind": "project", "episode_ids": ids}, {"kind": "episodes", "episode_ids": [str(uuid4())]},
                {"kind": "all"}, {"kind": "episodes", "episode_ids": [True]}):
            with self.subTest(scope=scope), self.assertRaises(ValidationError):
                self.build(scope=scope)
        with self.assertRaises(PermissionDenied):
            self.build(actor=self.other)

    def test_bad_delivery_catalog_and_oversized_payload_are_not_silently_defaulted(self):
        for changes in ({"delivery": {"kind": "season_master"}}, {"delivery": {}},
                {"stage_catalog_key": "user_dag"}, {"expected_stage_catalog_sha256": "0" * 64},
                {"initial_content": {"content": "x" * 65536}}):
            with self.subTest(changes=changes), self.assertRaises(ValidationError):
                self.build(**changes)

    def test_real_initial_script_reference_is_frozen_without_later_reselection(self):
        source, preview = create_pasted_script_source(project=self.project, user=self.actor, content="第一版剧本")
        revision = confirm_script_revision(preview, user=self.actor)
        initial = {"kind": "script_revision", "id": str(revision.pk), "expected_sha256": revision.content_sha256}
        run = self.root()
        record = freeze_run_execution_plan(run=run, **{**self.request, "initial_content": initial})
        first = get_run_execution_plan(run)
        self.assertEqual(first.value["initial_content"]["id"], str(revision.pk))
        self.assertNotIn("content", first.value["initial_content"])
        successor = create_script_revision(source=source, user=self.actor, content="第二版剧本")
        confirm_script_revision(successor, user=self.actor)
        self.assertEqual(first, get_run_execution_plan(run))
        self.assertEqual(record.definition_sha256, first.sha256)
        with self.assertRaises(ValidationError):
            self.build(initial_content=initial)

    def test_unconfirmed_foreign_and_corrupt_script_are_rejected_not_approved(self):
        source, preview = create_pasted_script_source(project=self.project, user=self.actor, content="待确认")
        initial = {"kind": "script_revision", "id": str(preview.pk), "expected_sha256": preview.content_sha256}
        with self.assertRaises(ValidationError):
            self.build(initial_content=initial)
        revision = confirm_script_revision(preview, user=self.actor)
        another = SeriesProject.objects.create(owner=self.actor, title="Other project", **PROJECT_CONFIG)
        with self.assertRaises(ValidationError):
            self.build(project_id=another.pk, initial_content=initial)
        ScriptRevision.objects.filter(pk=revision.pk).update(content="corrupt isolated source")
        with self.assertRaises(ValidationError):
            self.build(initial_content=initial)

    def test_recipe_plan_copies_exact_bytes_and_never_reads_current_recipe_on_replay(self):
        recipe = self.recipe()
        run = self.root(plan_source="recipe", recipe_version=recipe)
        freeze_run_execution_plan(run=run, **self.request)
        first = get_run_execution_plan(run)
        self.assertEqual(first.value["recipe"]["definition"], recipe.definition)
        ProductionRecipeVersion.objects.filter(pk=recipe.pk).update(status="retired")
        with patch("studio.services.production_run_plans.production_stage_catalog", side_effect=AssertionError("no current catalog")), \
                patch("studio.services.production_run_plans._recipe_snapshot", side_effect=AssertionError("no current recipe")):
            self.assertEqual(first, get_run_execution_plan(run))
        with self.assertRaises(ValidationError):
            self.build(source="recipe", recipe_version_id=recipe.pk)

    def test_recipe_schema_stage_order_duplicates_unknown_handlers_and_digest(self):
        definition = self.recipe().definition
        wrong_order = deepcopy(definition)
        wrong_order["stages"].reverse()
        duplicate = deepcopy(definition)
        duplicate["stages"].append(duplicate["stages"][0])
        unknown = deepcopy(definition)
        unknown["stages"][0]["key"] = "arbitrary_handler"
        for value in (wrong_order, duplicate, unknown, {**definition, "schema_version": 2}, {**definition, "handler": "shell"}):
            row = self.recipe(definition=value)
            with self.subTest(value=value), self.assertRaises(ValidationError):
                self.build(source="recipe", recipe_version_id=row.pk)
        legacy = self.recipe(schema=1)
        with self.assertRaises(ValidationError):
            self.build(source="recipe", recipe_version_id=legacy.pk)
        draft = self.recipe(status="draft")
        with self.assertRaises(ValidationError):
            self.build(source="recipe", recipe_version_id=draft.pk)
        row = self.recipe()
        ProductionRecipeVersion.objects.filter(pk=row.pk).update(definition_sha256="f" * 64)
        with self.assertRaises(ValidationError):
            self.build(source="recipe", recipe_version_id=row.pk)

    def test_frozen_source_schema_survives_read_and_rejects_drift_before_first_binding(self):
        from studio.services.execution_structure_mapping import bind_structure, execution_context_field_options

        run = self.root()
        record = freeze_run_execution_plan(run=run, **self.request)
        original = deepcopy(record.definition)
        with patch("studio.services.execution_structure_mapping.execution_context_field_options",
                side_effect=AssertionError("history must not read current sources")):
            self.assertEqual(get_run_execution_plan(run).value, original)
        changed = deepcopy(execution_context_field_options())
        next(row for row in changed if row["selector_key"] == "shot.dialogue")["value_schema"]["description"] = "Changed contract"
        reader = Mock()
        with patch("studio.services.execution_structure_mapping.execution_context_field_options", return_value=changed), \
                self.assertRaisesMessage(ValidationError, "与冻结运行计划不一致"):
            bind_structure({"kind": "context_field", "selector_key": "shot.dialogue", "scope": {"kind": "shot"},
                "path": [0, "text"]}, reader, source_schemas=original["structure_source_schemas"])
        reader.assert_not_called()
        record.refresh_from_db()
        self.assertEqual(record.definition, original)

    def test_missing_plan_fails_even_if_recipe_exists_and_no_repair_is_attempted(self):
        run = self.root(plan_source="recipe", recipe_version=self.recipe())
        with self.assertRaisesMessage(ValidationError, "不能从配方"):
            get_run_execution_plan(run)
        self.assertFalse(ProductionRunPlan.objects.exists())

    def test_production_episode_reader_uses_only_plan_scope_not_old_snapshot_or_other_project(self):
        from studio.services.production_runs import _selected_episodes, StageBlocked
        first = Episode.objects.create(project=self.project, number=1, title="Selected")
        second = Episode.objects.create(project=self.project, number=2, title="Not selected")
        run = self.root()
        freeze_run_execution_plan(run=run, **{**self.request, "scope": {"kind": "episodes", "episode_ids": [str(first.pk)]}})
        run.input_snapshot = {"episode_ids": [str(second.pk)]}
        self.assertEqual([row.pk for row in _selected_episodes(run)], [first.pk])
        first.delete()
        with self.assertRaises(StageBlocked):
            _selected_episodes(run)

    def test_production_recipe_identity_comes_from_plan_and_missing_plan_never_falls_back(self):
        from studio.services.production_runs import _frozen_recipe_definition_sha256, _is_unattended_run, StageBlocked
        recipe = self.recipe()
        run = self.root(plan_source="recipe", recipe_version=recipe)
        record = freeze_run_execution_plan(run=run, **self.request)
        digest = record.definition["recipe"]["sha256"]
        ProductionRecipeVersion.objects.filter(pk=recipe.pk).update(definition_sha256="e" * 64)
        self.assertEqual(_frozen_recipe_definition_sha256(run), digest)
        manual = self.root()
        freeze_run_execution_plan(run=manual, **self.request)
        self.assertFalse(_is_unattended_run(manual))
        with self.assertRaises(StageBlocked):
            _frozen_recipe_definition_sha256(manual)
        with self.assertRaises(ValidationError):
            _frozen_recipe_definition_sha256(self.root(plan_source="recipe", recipe_version=self.recipe()))

    def test_snapshot_cannot_be_replaced_or_bulk_rewritten(self):
        run = self.root()
        record = freeze_run_execution_plan(run=run, **self.request)
        with self.assertRaises(ValidationError):
            freeze_run_execution_plan(run=run, **self.request)
        record.definition = {"schema_version": 1}
        with self.assertRaises(ValidationError):
            record.save()
        with self.assertRaises(ValidationError):
            ProductionRunPlan.objects.filter(pk=record.pk).update(definition_sha256="e" * 64)
        self.assertEqual(ProductionRunPlan.objects.filter(run=run).count(), 1)
        with self.assertRaises(PermissionDenied):
            freeze_run_execution_plan(run=self.root(), **{**self.request, "actor": self.other})

    def test_reader_rechecks_real_root_and_indexed_narrow_queries(self):
        run = self.root()
        freeze_run_execution_plan(run=run, **self.request)
        with CaptureQueriesContext(connection) as captured:
            result = get_run_execution_plan(run)
        self.assertEqual(len(captured), 2)
        for row in captured:
            self.assertNotIn("SELECT *", row["sql"].upper())
            self.assertNotIn("studio_productionrecipeversion", row["sql"])
            if connection.vendor == "sqlite":
                with connection.cursor() as cursor:
                    cursor.execute("EXPLAIN QUERY PLAN " + row["sql"])
                    explain = " ".join(str(item) for item in cursor.fetchall()).upper()
                self.assertIn("SEARCH", explain)
                self.assertNotIn("TEMP B-TREE", explain)
        self.assertLess(result.size_bytes, 65536)
        other_project = SeriesProject.objects.create(owner=self.actor, title="Changed root", **PROJECT_CONFIG)
        ProductionRun.objects.filter(pk=run.pk).update(project=other_project)
        with self.assertRaises(ValidationError):
            get_run_execution_plan(run)  # stale caller object cannot hide drift

    def test_manual_receipts_are_unique_immutable_refs_not_another_queue(self):
        run = self.root()
        target = uuid4()
        fields = dict(run=run, action="adopt", idempotency_key="one-action", request_sha256="a" * 64,
            created_by=self.actor, result_kind="artifact_selection", target_id=target)
        receipt = ProductionManualCommand(**fields)
        assign_json_contract(receipt, "result_refs", {"schema_version": 1, "target_id": str(target)})
        receipt.save()  # storage constraint only; no fabricated business action
        duplicate = ProductionManualCommand(**fields)
        assign_json_contract(duplicate, "result_refs", receipt.result_refs)
        with self.assertRaises(IntegrityError), transaction.atomic():
            duplicate.save()
        receipt.target_id = uuid4()
        with self.assertRaises(ValidationError):
            receipt.save()
        self.assertNotIn("status", {field.name for field in ProductionManualCommand._meta.fields})
