"""Original manual import/adoption API and recipe input-source isolation tests.

Recipe plans below are explicit storage fixtures, not authoring/publication or
full Bible/storyboard acceptance. No provider is called or business data used.
"""
from copy import deepcopy
import json
from unittest.mock import patch
from uuid import uuid4

from django.core.exceptions import ValidationError
from django.test import TestCase
from django.db import connection
from django.test.utils import CaptureQueriesContext

from studio.models import ApprovalDecision, GeneratedArtifact, GenerationAttempt, ProductionManualCommand, ScriptRevisionStructure, StageRun
from studio.services.json_contracts import assign_json_contract, json_contract
from studio.services.production_manual_scripts import manual_script_states, read_manual_script_structure
from studio.services.production_run_plans import freeze_run_execution_plan
from studio.services.production_runs import _script_artifact, _assert_stage_output_integrity, decide_stage
from studio.services.production_stage_catalog import production_stage_catalog
from studio.services.script_revision_structures import read_confirmed_script_structure
from studio.services.script_sources import confirm_script_revision
from studio.tests import test_production_manual_actions as manual
from studio.tests import test_script_revision_structures as storage
from studio.tests import test_production_run_plans as plans


LIMIT = storage.LIMIT


class ManualScriptInputStructureTests(TestCase):
    create = manual.ProductionManualActionTests.create
    post = manual.ProductionManualActionTests.post
    make_output = storage.ScriptRevisionStructureTests.make_output

    def setUp(self):
        manual.ProductionManualActionTests.setUp(self)
        self.actor.is_staff = True
        self.actor.save(update_fields=("is_staff",))

    def imported(self, layout="a"):
        output, raw = self.make_output(layout)
        raw["创作补充"] = [0, False, None, {"note": "保留原值"}]
        text = " \r\n" + json.dumps(raw, ensure_ascii=False, indent=2).replace("\n", "\r\n") + "\r\n "
        reply = self.client.post("/api/v1/script-sources/", {"project_id": str(self.project.pk), "content": text}, format="json")
        self.assertEqual(reply.status_code, 201, reply.data)
        revision = reply.data["revision"]
        reply = self.client.post(f'/api/v1/script-revisions/{revision["id"]}/structure/',
            {"output_version_id": str(output.pk)}, format="json")
        self.assertEqual(reply.status_code, 200, reply.data)
        detail = reply.data["structure"]
        reply = self.client.post(f'/api/v1/script-revisions/{revision["id"]}/confirm/', {}, format="json")
        self.assertEqual(reply.status_code, 200, reply.data)
        return revision, detail, raw, text

    def adopt(self, revision, *, selection=0):
        body = deepcopy(self.body)
        body.update(idempotency_key=str(uuid4()), expected_selection_revision=selection)
        body["payload"]["content"] = {"kind": "script_revision", "id": revision["id"],
            "expected_sha256": revision["content_sha256"]}
        reply = self.post(body)
        return reply, body

    def test_two_layouts_http_import_confirm_adopt_and_export_exact_details(self):
        for selection, layout in enumerate(("a", "b")):
            revision, detail, raw, text = self.imported(layout)
            reply, body = self.adopt(revision, selection=selection)
            self.assertEqual(reply.status_code, 201, reply.data)
            self.run.refresh_from_db()
            stage = self.run.stages.get(stage_key="script_input")
            self.assertEqual(stage.output_snapshot_schema_version, 4)
            self.assertEqual(set(stage.output_snapshot), {"schema_version", "content", "structure_source"})
            with patch("studio.services.script_revision_structures.parse_script_semantic_output",
                    side_effect=AssertionError("do not reparse")):
                result = read_manual_script_structure(self.run)
            self.assertEqual(result["source"], stage.output_snapshot["structure_source"])
            self.assertEqual(result["structure"], {**detail, "original_text": text})
            self.assertEqual(result["structure"]["original_text"], text)
            self.assertEqual(result["structure"]["document"]["projection"]["source_value"], raw)
            before = (ApprovalDecision.objects.count(), ProductionManualCommand.objects.count(), ScriptRevisionStructure.objects.count())
            replay = self.post(body)
            self.assertEqual((replay.status_code, replay.data), (200, reply.data))
            self.assertEqual((ApprovalDecision.objects.count(), ProductionManualCommand.objects.count(), ScriptRevisionStructure.objects.count()), before)
        self.assertFalse(GenerationAttempt.objects.exists())
        self.assertFalse(GeneratedArtifact.objects.exists())
        self.assertFalse(self.run.stages.exclude(stage_key="script_input").exclude(status="pending").exists())

    def test_raw_confirmed_text_stays_adoptable_but_is_explicitly_not_structured(self):
        reply = self.post()
        self.assertEqual(reply.status_code, 201, reply.data)
        self.run.refresh_from_db()
        stage = self.run.stages.get(stage_key="script_input")
        self.assertIn("structure_source", stage.output_snapshot)
        self.assertIsNone(stage.output_snapshot["structure_source"])
        with self.assertRaises(ValidationError) as caught:
            read_manual_script_structure(self.run)
        self.assertEqual(caught.exception.code, "script_structure_required")
        self.assertFalse(ScriptRevisionStructure.objects.exists())
        self.assertFalse(GenerationAttempt.objects.exists())

    def test_existing_broken_detail_is_not_downgraded_to_raw_input(self):
        revision, detail, _, _ = self.imported()
        ScriptRevisionStructure.objects.filter(revision_id=revision["id"]).update(document_sha256="0" * 64)
        reply, _ = self.adopt(revision)
        self.assertEqual(reply.status_code, 400, reply.data)
        self.run.refresh_from_db()
        self.assertIsNone(self.run.script_revision_id)
        self.assertFalse(ApprovalDecision.objects.exists())
        self.assertFalse(ProductionManualCommand.objects.exists())
        self.assertEqual(ScriptRevisionStructure.objects.get(revision_id=revision["id"]).document, detail["document"])

    def test_failed_receipt_rolls_back_structured_adoption_but_preserves_import(self):
        revision, detail, _, _ = self.imported()
        with patch("studio.services.production_manual_commands.ProductionManualCommand.save",
                side_effect=ValidationError("isolated receipt failure")):
            reply, _ = self.adopt(revision)
        self.assertEqual(reply.status_code, 400, reply.data)
        self.run.refresh_from_db()
        self.assertIsNone(self.run.script_adoption_decision_id)
        self.assertIsNone(self.run.script_revision_id)
        self.assertEqual(self.run.stages.get(stage_key="script_input").status, "pending")
        self.assertFalse(ApprovalDecision.objects.exists())
        self.assertEqual(ScriptRevisionStructure.objects.get(revision_id=revision["id"]).document, detail["document"])

    def test_on_demand_export_rejects_source_drift_and_options_never_load_large_detail(self):
        revision, _, _, _ = self.imported()
        self.assertEqual(self.adopt(revision)[0].status_code, 201)
        self.run.refresh_from_db()
        with CaptureQueriesContext(connection) as queries:
            state = manual_script_states(self.run)
        self.assertEqual(state["current_script"]["id"], revision["id"])
        self.assertFalse(any('studio_scriptrevisionstructure' in row["sql"] for row in queries))
        stage = self.run.stages.get(stage_key="script_input")
        original = deepcopy(stage.output_snapshot)
        changed = deepcopy(original)
        changed["structure_source"]["document_sha256"] = "0" * 64
        assign_json_contract(stage, "output_snapshot", changed, schema_version=4)
        stage.save()
        stage.approval_decisions.update(output_snapshot_sha256=stage.output_snapshot_sha256)
        with self.assertRaisesMessage(ValidationError, "已冻结来源不一致"):
            read_manual_script_structure(self.run)
        changed["structure_source"]["script"] = {**original["content"], "id": str(uuid4())}
        assign_json_contract(stage, "output_snapshot", changed, schema_version=4)
        stage.save()
        stage.approval_decisions.update(output_snapshot_sha256=stage.output_snapshot_sha256)
        with self.assertRaisesMessage(ValidationError, "不是本次采用"):
            manual_script_states(self.run)


class RecipeScriptInputStructureTests(TestCase):
    make_output = storage.ScriptRevisionStructureTests.make_output
    make_revision = storage.ScriptRevisionStructureTests.make_revision
    save_detail = storage.ScriptRevisionStructureTests.save_detail
    root = plans.ProductionRunPlanTests.root
    recipe = plans.ProductionRunPlanTests.recipe

    def setUp(self):
        storage.ScriptRevisionStructureTests.setUp(self)
        self.save_detail()
        self.revision = confirm_script_revision(self.revision, user=self.actor)
        catalog = production_stage_catalog()
        recipe = self.recipe(definition={"schema_version": 3, "interaction_mode": "assisted", "stages": [
            {"key": "script_input", "mode": "confirmed_revision", "approval": "required"}]})
        self.run = self.root(plan_source="recipe", recipe_version=recipe, script_revision=self.revision)
        plan = freeze_run_execution_plan(run=self.run, scope={"kind": "project"}, delivery={"kind": "episode_masters"},
            initial_content={"kind": "script_revision", "id": str(self.revision.pk), "expected_sha256": self.revision.content_sha256},
            stage_catalog_key=catalog.value["key"], expected_stage_catalog_sha256=catalog.sha256, actor=self.actor)
        # Explicit storage receipt lets the original post-approval scheduler
        # verify this fixture; it is not a public recipe creation test.
        intent = {"schema_version": 4, "plan_source": "recipe", "project_id": str(self.project.pk),
            "recipe_version_id": str(recipe.pk), "creation_inputs": {"idea": "", "style_hint": "",
                "episode_count": None, "shots_per_episode": None, "episode_duration_seconds": None}}
        self.run.request_sha256 = json_contract(intent).sha256
        assign_json_contract(self.run, "input_snapshot", {"schema_version": 4, "request_intent": intent,
            "run_plan_sha256": plan.definition_sha256}, schema_version=4)
        self.run.save()
        self.stage = StageRun.objects.create(production_run=self.run, stage_key="script_input", ordinal=1,
            mode="confirmed_revision", status="running", idempotency_key=str(uuid4()))

    def test_original_recipe_input_writer_freezes_same_source_once_without_fake_call(self):
        artifact = _script_artifact(self.run, self.stage, create=True)
        source = artifact.manifest_metadata["structure_source"]
        self.assertEqual(source["script"]["id"], str(self.revision.pk))
        self.assertEqual(artifact.manifest_schema_version, 2)
        result = read_confirmed_script_structure(project_id=self.project.pk, reference=source["script"],
            expected_source=source, max_bytes=LIMIT)
        self.assertEqual(result["structure"]["document"]["projection"]["source_value"], self.raw)
        self.assertEqual(_script_artifact(self.run, self.stage, create=True).pk, artifact.pk)
        _assert_stage_output_integrity(self.run, self.stage)
        self.assertEqual(GeneratedArtifact.objects.count(), 1)
        self.assertFalse(GenerationAttempt.objects.exists())
        self.assertIsNone(artifact.generation_attempt_id)

    def test_reading_missing_or_corrupt_input_artifact_never_repairs_or_replaces_it(self):
        with self.assertRaisesMessage(ValidationError, "不能在读取或审核中补造"):
            _assert_stage_output_integrity(self.run, self.stage)
        self.assertFalse(GeneratedArtifact.objects.exists())
        artifact = _script_artifact(self.run, self.stage, create=True)
        metadata = deepcopy(artifact.manifest_metadata)
        metadata["structure_source"]["document_sha256"] = "0" * 64
        assign_json_contract(artifact, "manifest_metadata", metadata, schema_version=2,
            schema_version_field_name="manifest_schema_version")
        artifact.save()
        for create in (False, True):
            with self.subTest(create=create), self.assertRaisesMessage(ValidationError, "已冻结的采用来源不一致"):
                _script_artifact(self.run, self.stage, create=create)
        artifact.refresh_from_db()
        self.assertEqual(artifact.manifest_metadata, metadata)
        self.assertEqual(GeneratedArtifact.objects.count(), 1)

    def awaiting_input_approval(self):
        self.run.status, self.run.current_stage_key = "awaiting_approval", "script_input"
        self.run.save(update_fields=("status", "current_stage_key"))
        self.stage.status, self.stage.approval_required = "awaiting_approval", True
        self.stage.save(update_fields=("status", "approval_required"))

    def test_original_approval_transaction_creates_input_source_without_a_model_call(self):
        self.awaiting_input_approval()
        result = decide_stage(self.run, stage_key="script_input", decision="approve", comment="核对导入结构", user=self.actor)
        self.assertEqual((result.status, result.blocked_code), ("blocked", "delivery_manifest_missing"))
        self.stage.refresh_from_db()
        self.assertEqual(self.stage.status, "succeeded")
        artifact = GeneratedArtifact.objects.get(stage_run=self.stage)
        self.assertEqual(artifact.manifest_metadata["structure_source"]["script"]["id"], str(self.revision.pk))
        self.assertEqual(ApprovalDecision.objects.get(stage_run=self.stage).output_snapshot_sha256,
            self.stage.output_snapshot_sha256)
        _assert_stage_output_integrity(self.run, self.stage)
        self.assertFalse(GenerationAttempt.objects.exists())
        with self.assertRaises(ValidationError):
            decide_stage(self.run, stage_key="script_input", decision="approve", comment="重复", user=self.actor)
        self.assertEqual((GeneratedArtifact.objects.count(), ApprovalDecision.objects.count()), (1, 1))

    def test_failed_original_approval_rolls_back_new_input_artifact(self):
        self.awaiting_input_approval()
        with patch("studio.services.production_runs.ApprovalDecision.objects.create",
                side_effect=ValidationError("isolated decision failure")):
            with self.assertRaisesMessage(ValidationError, "isolated decision failure"):
                decide_stage(self.run, stage_key="script_input", decision="approve", comment="核对", user=self.actor)
        self.stage.refresh_from_db()
        self.assertEqual(self.stage.status, "awaiting_approval")
        self.assertFalse(GeneratedArtifact.objects.exists())
        self.assertFalse(ApprovalDecision.objects.exists())
        self.assertEqual(ScriptRevisionStructure.objects.count(), 1)
