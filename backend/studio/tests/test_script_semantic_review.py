"""Original candidate/detail/manual-review APIs over request-bound script output.

Only definition authoring/admission and provider submission are isolated fixtures.
The queue, stored response, public readers, manual command and adoption writer
are real. No live service, model or business record is used.
"""
from copy import deepcopy
import json
from unittest.mock import patch

from django.contrib.auth import get_user_model
from django.core.exceptions import ValidationError
from django.db import connection
from django.test import TestCase, override_settings
from django.test.utils import CaptureQueriesContext
from rest_framework.test import APIClient

from studio.models import (ApprovalDecision, ArtifactSelectionDecision, GenerationAttempt,
    ProductionManualCommand, ProductionRun, ScriptRevision, ScriptRevisionStructure)
from studio.services.business_outputs import output_reference, write_output
from studio.services.data_structures import validate_data_structure_snapshot
from studio.services.production_script_baselines import capture_script_adoption_baseline
from studio.services.production_stage_catalog import production_stage_catalog
from studio.tests import test_script_semantic_worker as worker
from studio.tests import test_production_manual_scripts as manual


@override_settings(STORAGES={
    "default": {"BACKEND": "django.core.files.storage.InMemoryStorage"},
    "staticfiles": {"BACKEND": "django.contrib.staticfiles.storage.StaticFilesStorage"},
})
class ScriptSemanticReviewTests(TestCase):
    make_output = worker.ScriptSemanticWorkerTests.make_output
    counts = worker.ScriptSemanticWorkerTests.counts
    freeze = worker.ScriptSemanticWorkerTests.freeze
    queued = worker.ScriptSemanticWorkerTests.queued
    process = worker.ScriptSemanticWorkerTests.process
    options = manual.ManualScriptReviewTests.options
    approval = manual.ManualScriptReviewTests.approval

    def call(self, operation="script.generate"):
        attempt = worker.ScriptSemanticWorkerTests.call(self, operation)
        attempt.unit_key = attempt.preparation.unit_key = f"project:{self.project.pk}"
        return attempt

    def setUp(self):
        worker.ScriptSemanticWorkerTests.setUp(self)
        self.client = APIClient()
        self.client.force_authenticate(self.actor)
        catalog = production_stage_catalog()
        reply = self.client.post("/api/v1/production-runs/", {
            "project_id": str(self.project.pk), "plan_source": "manual", "idempotency_key": "semantic-review-root",
            "allow_external_calls": False, "config_overrides": {}, "manual_plan": {
                "stage_catalog_key": catalog.value["key"], "expected_stage_catalog_sha256": catalog.sha256,
                "scope": {"kind": "project"}, "delivery": {"kind": "episode_masters"}, "initial_content": None}}, format="json")
        self.assertEqual(reply.status_code, 201, reply.data)
        self.run = ProductionRun.objects.get(pk=reply.data["id"])
        self.run.status = "running"  # Explicit execution lifecycle fixture; no native admission claimed.
        self.run.save(update_fields=("status",))
        self.stage = self.run.stages.get(stage_key="script_generate")
        self.url = f"/api/v1/production-runs/{self.run.pk}/manual-actions/"
        self.body = {"unit_key": f"project:{self.project.pk}", "scope": {"kind": "project", "id": str(self.project.pk)},
            "expected_run_plan_sha256": reply.data["execution_plan"]["sha256"]}

    def candidate(self, operation="script.generate"):
        self.stage = self.run.stages.get(stage_key="script_generate" if operation == "script.generate" else "script_structure")
        self.stage.status = "running"
        self.stage.save(update_fields=("status",))
        self.attempt = self.call(operation)
        baseline = capture_script_adoption_baseline(run_id=self.run.pk, project_id=self.project.pk, owner=self.actor)
        self.queued(operation=operation, adoption_baseline=baseline)
        result = self.process(self.text)
        self.assertEqual(result.status, "succeeded", result.error_message)
        return result.artifacts.get()

    def detail(self, artifact):
        reply = self.client.get(f"/api/v1/generated-artifacts/{artifact.pk}/")
        self.assertEqual(reply.status_code, 200, reply.data)
        return reply.data["text_candidate"]

    def post(self, body):
        return self.client.post(self.url, body, format="json")

    def assert_unadopted(self, artifact):
        self.run.refresh_from_db(); self.project.refresh_from_db(); artifact.refresh_from_db()
        self.assertIsNone(self.run.script_revision_id)
        self.assertIsNone(self.run.script_adoption_decision_id)
        self.assertEqual(artifact.status, "available")
        self.assertEqual(ScriptRevision.objects.get(pk=artifact.manifest_metadata["script_revision_id"]).status, "preview")
        self.assertFalse(ApprovalDecision.objects.filter(stage_run__production_run=self.run).exists())
        self.assertFalse(ArtifactSelectionDecision.objects.exists())
        self.assertFalse(ProductionManualCommand.objects.filter(run=self.run).exists())

    def test_original_candidate_adoption_then_bible_api_uses_saved_semantics_once(self):
        from uuid import uuid4
        from studio.models import BibleVariant, ScriptBibleEntityBinding

        artifact = self.candidate()
        reply = self.post(self.approval(artifact))
        self.assertEqual(reply.status_code, 201, reply.data)
        before = self.counts()
        url = f"/api/v1/production-runs/{self.run.pk}/bible-review/"
        detail = self.client.get(url).data
        source = {**self.body, "action": "adopt", "stage_key": "bible_materialize", "idempotency_key": str(uuid4()),
            "expected_selection_revision": 0, "payload": {"expected_current_script": detail["current_script"], "comment": ""}}
        with patch("studio.services.script_revision_structures.parse_script_semantic_output", side_effect=AssertionError("no reparse")):
            response = self.post(source)
            self.assertEqual(response.status_code, 201, response.data)
            detail = self.client.get(url).data
            request = {**self.body, "action": "approve", "stage_key": "bible_review", "idempotency_key": str(uuid4()),
                "expected_selection_revision": 0, "payload": {"expected_current_script": detail["current_script"],
                    "expected_source_snapshot_sha256": detail["source"]["snapshot_sha256"],
                    "expected_project_bible_sha256": detail["project_bible_sha256"],
                    "reviewed_candidate_ids": [row["id"] for row in detail["candidates"]],
                    "reviewed_setting_keys": [row["review_key"] for row in detail["settings"]],
                    "reviewed_project_bible": True, "comment": ""}}
            response = self.post(request)
            self.assertEqual(response.status_code, 201, response.data)
            replay = self.post(request)
            self.assertEqual((replay.status_code, replay.data), (200, response.data))
        self.assertEqual(ScriptBibleEntityBinding.objects.count(), 1)
        self.assertEqual(BibleVariant.objects.count(), 3)
        self.assertEqual(self.counts(), before)
        self.assertEqual(detail["settings"][0]["source_facts"]["structure_source"]["script"]["id"],
            artifact.manifest_metadata["script_revision_id"])

    def test_adopted_candidate_check_uses_saved_structure_and_reports_corruption(self):
        artifact = self.candidate()
        reply = self.post(self.approval(artifact))
        self.assertEqual(reply.status_code, 201, reply.data)
        url = f"/api/v1/production-runs/{self.run.pk}/manual-script-structure/"
        before = self.counts()
        with patch("studio.services.script_revision_structures.parse_script_semantic_output",
                   side_effect=AssertionError("never reparse")), CaptureQueriesContext(connection) as queries:
            valid = self.client.get(url)
        self.assertEqual(valid.status_code, 200, valid.data)
        self.assertEqual(valid.data["status"], "structured")
        self.assertEqual((valid.data["episode_count"], valid.data["shot_count"]), (2, 4))
        for query in queries:
            self.assertNotIn('"request_snapshot"', query["sql"])
        ScriptRevisionStructure.objects.filter(revision_id=artifact.manifest_metadata["script_revision_id"]).update(
            document_sha256="0" * 64)
        failed = self.client.get(url)
        self.assertEqual(failed.status_code, 400, failed.data)
        self.assertNotEqual(failed.data.get("status"), "unstructured")
        self.assertEqual(before, self.counts())
        self.run.refresh_from_db()
        self.assertEqual(str(self.run.script_revision_id), artifact.manifest_metadata["script_revision_id"])

    def test_original_detail_and_task_center_read_exact_candidate_without_reparse(self):
        self.raw["自定义.字段"] = {"number": 0, "flag": False, "empty": None}
        self.text = " \r\n" + json.dumps(self.raw, ensure_ascii=False) + "\r\n "
        artifact = self.candidate()
        before = self.counts()
        with patch("studio.services.script_revision_structures.parse_script_semantic_output", side_effect=AssertionError("no reparse")):
            detail = self.detail(artifact)
            self.assertEqual(self.detail(artifact), detail)
            center = self.client.get(f"/api/v1/task-center/preparation/{self.prep.pk}/")
        self.assertEqual(detail["candidate"], self.raw)
        self.assertEqual(detail["text"], self.text)
        self.assertEqual(detail["output_contract"], self.output_ref)
        self.assertEqual(detail["json_schema"], validate_data_structure_snapshot(self.output_ref["data_structure"]))
        self.assertEqual(detail["adoption_block_reason"], "")
        self.assertTrue(detail["warnings"])
        self.assertNotIn("source_value", detail["projection"])
        self.assertEqual(center.status_code, 200, center.data)
        data = center.data.get("data", center.data)
        self.assertEqual(data["output"]["state"], "available", data["validation"])
        self.assertEqual(data["output"]["result"]["artifacts"][0]["result"]["candidate"], self.raw)
        self.assertEqual(self.counts(), before)
        self.assert_unadopted(artifact)

    def test_manual_approval_uses_original_revision_and_preserves_project_metadata_and_replay(self):
        artifact = self.candidate()
        self.project.title, self.project.logline = "后续人工标题", "后续人工梗概"
        self.project.save(update_fields=("title", "logline"))
        body = self.approval(artifact)
        with patch("studio.services.script_revision_structures.parse_script_semantic_output", side_effect=AssertionError("no reparse")):
            reply = self.post(body)
        self.assertEqual(reply.status_code, 201, reply.data)
        self.run.refresh_from_db(); self.project.refresh_from_db(); artifact.refresh_from_db()
        revision = ScriptRevision.objects.get(pk=artifact.manifest_metadata["script_revision_id"])
        self.assertEqual(self.run.script_revision_id, revision.pk)
        self.assertEqual((revision.status, revision.confirmed_by_id), ("confirmed", self.actor.pk))
        self.assertEqual((self.project.master_script, revision.content), (self.text, self.text))
        self.assertEqual((self.project.title, self.project.logline), ("后续人工标题", "后续人工梗概"))
        self.assertEqual(artifact.status, "selected")
        decision = self.run.script_adoption_decision
        self.assertEqual((decision.actor, decision.decided_by_id), ("human", self.actor.pk))
        self.assertEqual(decision.stage_run.output_snapshot["content"]["id"], str(revision.pk))
        self.assertEqual(ArtifactSelectionDecision.objects.filter(artifact=artifact).count(), 1)
        self.assertEqual(self.detail(artifact)["candidate"], self.raw)
        state = self.options()
        self.assertEqual(state["current_script"]["id"], str(revision.pk))
        self.assertEqual(state["current_script_identity"]["candidate"]["artifact_id"], str(artifact.pk))
        before = (self.counts(), GenerationAttempt.objects.count(), ApprovalDecision.objects.count())
        replay = self.post(body)
        self.assertEqual((replay.status_code, replay.data), (200, reply.data))
        self.assertEqual((self.counts(), GenerationAttempt.objects.count(), ApprovalDecision.objects.count()), before)
        self.assertFalse(self.run.stages.filter(ordinal__gt=decision.stage_run.ordinal).exclude(status="pending").exists())

    def test_structure_other_layout_reads_and_adopts_without_rewriting_source(self):
        self.output, self.raw = self.make_output("b")
        self.output_ref = output_reference(self.output)
        self.text = " \r\n" + json.dumps(self.raw, ensure_ascii=False) + "\r\n "
        artifact = self.candidate("script.structure")
        self.assertEqual(self.detail(artifact)["candidate"], self.raw)
        reply = self.post(self.approval(artifact))
        self.assertEqual(reply.status_code, 201, reply.data)
        self.project.refresh_from_db()
        self.assertEqual(self.project.master_script, self.text)
        self.assertEqual(self.transport["operation_plan"]["plan"]["inputs"]["source_text"], " 原始对白\r\n不添加剧情 ")

    def test_array_root_is_not_forced_to_fixed_script_fields(self):
        from studio.tests.output_structure_fixtures import response_structure
        from studio.tests.script_semantics_fixtures import script_example

        raw, _, mapping = script_example()
        def wrap(node):
            if isinstance(node, dict):
                if node.get("kind") == "path" and node.get("scope") == "root":
                    node["path"] = [0, *node["path"]]
                else:
                    for value in node.values():
                        wrap(value)
            elif isinstance(node, list):
                for value in node:
                    wrap(value)
        wrap(mapping)
        structure = response_structure(example=[raw])
        self.output = write_output(actor=self.actor, data={"name": "Review array", "parser_key": "script_semantics.v1",
            "data_structure_version": str(structure.pk), "semantic_mapping": mapping})
        self.output = write_output(actor=self.actor, output_id=self.output.pk, expected_revision=self.output.revision, action="publish")
        self.output_ref = output_reference(self.output)
        raw["custom"] = [0, False, None]
        self.raw, self.text = [raw], json.dumps([raw], ensure_ascii=False)
        artifact = self.candidate()
        self.assertEqual(self.detail(artifact)["json_schema"]["type"], "array")
        self.assertEqual(self.detail(artifact)["candidate"], self.raw)
        edited = deepcopy(self.raw)
        edited[0]["custom"].append({"人工编辑": "数组根保持不变"})
        body = self.approval(artifact)
        body["payload"]["script_content"] = json.dumps(edited, ensure_ascii=False)
        reply = self.post(body)
        self.assertEqual(reply.status_code, 201, reply.data)
        self.project.refresh_from_db(); self.run.refresh_from_db()
        self.assertEqual(self.project.master_script, body["payload"]["script_content"])
        self.assertEqual(self.run.script_revision.structured_content.document["projection"]["source_value"], edited)
        self.assertEqual(self.detail(artifact)["candidate"], self.raw)

    def test_retired_output_still_uses_exact_frozen_definition(self):
        artifact = self.candidate()
        self.output = write_output(actor=self.actor, output_id=self.output.pk, expected_revision=self.output.revision, action="retire")
        self.assertEqual(self.detail(artifact)["output_contract"], self.output_ref)
        self.assertEqual(self.post(self.approval(artifact)).status_code, 201)

    def test_candidate_list_does_not_load_structure_or_frozen_request(self):
        artifact = self.candidate()
        with CaptureQueriesContext(connection) as queries, \
                patch("studio.services.execution_preparations.read_production_attempt_request", side_effect=AssertionError("no list detail")):
            reply = self.client.get("/api/v1/generated-artifacts/", {"production_run": str(self.run.pk)})
        self.assertEqual(reply.status_code, 200, reply.data)
        rows = reply.data.get("results", reply.data) if isinstance(reply.data, dict) else reply.data
        self.assertIn(str(artifact.pk), [row["id"] for row in rows])
        self.assertTrue(all("text_candidate" not in row for row in rows))
        self.assertFalse(any("studio_scriptrevisionstructure" in item["sql"] for item in queries))

    def test_other_project_user_cannot_view_candidate(self):
        artifact = self.candidate()
        stranger = get_user_model().objects.create_user(username="other-script-viewer")
        self.client.force_authenticate(stranger)
        self.assertEqual(self.client.get(f"/api/v1/generated-artifacts/{artifact.pk}/").status_code, 404)

    def test_corrupt_saved_detail_fails_read_and_approval_without_fallback(self):
        artifact = self.candidate()
        body = self.approval(artifact)
        ScriptRevisionStructure.objects.filter(revision_id=artifact.manifest_metadata["script_revision_id"]).update(document_sha256="0" * 64)
        self.assertEqual(self.client.get(f"/api/v1/generated-artifacts/{artifact.pk}/").status_code, 400)
        reply = self.post(body)
        self.assertEqual(reply.status_code, 400, reply.data)
        self.assert_unadopted(artifact)

    def test_legacy_result_shape_cannot_bypass_exact_script_revision_pointer(self):
        from studio.services.json_contracts import assign_json_contract, json_contract

        artifact = self.candidate()
        body = self.approval(artifact)
        metadata = deepcopy(artifact.manifest_metadata)
        legacy = {"result": {"candidate": {"master_script": self.text},
            "format": {"schema_version": 4}}, "warnings": []}
        result = json_contract(legacy, max_bytes=1_000_000)
        metadata.update(result=result.value, result_sha256=result.sha256)
        assign_json_contract(artifact, "manifest_metadata", metadata, max_bytes=1_000_000)
        artifact.save()
        self.attempt.result_sha256 = result.sha256
        self.attempt.save(update_fields=("result_sha256",))
        before = self.counts()
        with patch("studio.services.script_revision_structures.parse_script_semantic_output",
                side_effect=AssertionError("no compatibility reparse")):
            reply = self.client.get(f"/api/v1/generated-artifacts/{artifact.pk}/")
            self.assertEqual(reply.status_code, 400, reply.data)
            self.assertIn("确切修订引用", str(reply.data))
            reply = self.post(body)
        self.assertEqual(reply.status_code, 409, reply.data)
        self.assertEqual(reply.data["error"]["code"], "manual_selection_conflict")
        self.assertEqual(self.counts(), before)
        self.assert_unadopted(artifact)

    def test_changed_master_is_not_overwritten(self):
        artifact = self.candidate()
        body = self.approval(artifact)
        self.project.master_script = "后续人工修改"
        self.project.save(update_fields=("master_script",))
        reply = self.post(body)
        self.assertEqual(reply.status_code, 409, reply.data)
        self.assert_unadopted(artifact)
        self.assertEqual(self.project.master_script, "后续人工修改")

    def test_missing_original_request_fails_detail_and_adoption_not_rebuilt(self):
        from django.core.files.storage import default_storage

        artifact = self.candidate()
        body = self.approval(artifact)
        default_storage.delete(self.prep.contract.prepared_request_key)
        with patch("studio.services.script_semantic_requests.build_script_semantic_request", side_effect=AssertionError("no rebuild")):
            reply = self.client.get(f"/api/v1/generated-artifacts/{artifact.pk}/")
            self.assertEqual(reply.status_code, 400, reply.data)
            reply = self.post(body)
        self.assertEqual(reply.status_code, 400, reply.data)
        self.assert_unadopted(artifact)

    def test_selection_failure_rolls_back_revision_project_and_command(self):
        artifact = self.candidate()
        body = self.approval(artifact)
        with patch("studio.services.script_review.select_artifact", side_effect=ValidationError("isolated selection failure")):
            reply = self.post(body)
        self.assertEqual(reply.status_code, 400, reply.data)
        self.assert_unadopted(artifact)
        self.assertEqual(self.project.master_script, "")

    def test_writer_rejects_different_reviewed_detail(self):
        from django.db import transaction
        from studio.services.script_review import apply_verified_script_candidate

        artifact = self.candidate()
        detail = deepcopy(self.detail(artifact))
        detail["revision"]["content_sha256"] = "0" * 64
        with self.assertRaisesMessage(ValidationError, "详情已变化"), transaction.atomic():
            apply_verified_script_candidate(run=self.run, artifact=artifact, detail=detail, user=self.actor)
        self.assert_unadopted(artifact)

    def test_writer_does_not_fall_back_to_old_declarations_without_output_contract(self):
        from django.db import transaction
        from studio.services.script_review import apply_verified_script_candidate

        artifact = self.candidate()
        detail = deepcopy(self.detail(artifact))
        detail.pop("output_contract")
        with self.assertRaisesMessage(ValidationError, "确切结构化输出合同"), transaction.atomic():
            apply_verified_script_candidate(run=self.run, artifact=artifact, detail=detail, user=self.actor)
        self.assert_unadopted(artifact)

    def edited_body(self, artifact):
        value = deepcopy(self.raw)
        value["额外编辑"] = {"text": "人工调整", "flag": False, "zero": 0, "empty": None}
        content = json.dumps(value, ensure_ascii=False, indent=2)
        body = self.approval(artifact)
        body["payload"]["script_content"] = content
        return body, content, value

    def test_field_edit_saves_human_revision_in_original_review_and_replays_once(self):
        artifact = self.candidate()
        original_id = artifact.manifest_metadata["script_revision_id"]
        body, content, value = self.edited_body(artifact)
        attempt_count = GenerationAttempt.objects.count()
        self.output = write_output(actor=self.actor, output_id=self.output.pk, expected_revision=self.output.revision, action="retire")
        reply = self.post(body)
        self.assertEqual(reply.status_code, 201, reply.data)
        self.run.refresh_from_db(); self.project.refresh_from_db()
        edited = self.run.script_revision
        self.assertNotEqual(str(edited.pk), original_id)
        self.assertIsNone(edited.origin_generation_attempt_id)
        self.assertEqual((edited.created_by_id, edited.status, edited.content), (self.actor.pk, "confirmed", content))
        self.assertEqual(edited.structured_content.document["projection"]["source_value"], value)
        self.assertEqual(edited.structured_content.document["output"], self.output_ref)
        self.assertEqual(self.project.master_script, content)
        self.assertEqual(ScriptRevision.objects.get(pk=original_id).content, self.text)
        self.assertEqual(self.detail(artifact)["text"], self.text)
        evidence = self.run.script_adoption_decision.stage_run.output_snapshot["candidate"]
        self.assertEqual(evidence["edited_from_revision_id"], original_id)
        from studio.services.script_revision_structures import read_confirmed_script_structure

        source = evidence["structure_source"]
        downstream = read_confirmed_script_structure(project_id=self.project.pk, reference=source["script"],
            expected_source=source, max_bytes=1_000_000)
        self.assertEqual(source["script"]["id"], str(edited.pk))
        self.assertEqual(downstream["structure"]["original_text"], content)
        self.assertEqual(downstream["structure"]["document"]["projection"]["source_value"], value)
        state = self.options()
        self.assertEqual(state["current_script"]["id"], str(edited.pk))
        self.assertEqual(state["current_script_identity"]["candidate"]["edited_from_revision_id"], original_id)
        before = (ScriptRevision.objects.count(), ScriptRevisionStructure.objects.count(), ApprovalDecision.objects.count())
        replay = self.post(body)
        self.assertEqual((replay.status_code, replay.data), (200, reply.data))
        self.assertEqual((ScriptRevision.objects.count(), ScriptRevisionStructure.objects.count(), ApprovalDecision.objects.count()), before)
        self.assertEqual(GenerationAttempt.objects.count(), attempt_count)

    def test_invalid_edits_never_leave_revision_selection_or_receipt(self):
        artifact = self.candidate()
        before = (ScriptRevision.objects.count(), ScriptRevisionStructure.objects.count())
        for content in (None, False, {}, "", "{broken", "[]"):
            with self.subTest(content=content):
                body = self.approval(artifact)
                body["payload"]["script_content"] = content
                reply = self.post(body)
                self.assertEqual(reply.status_code, 400, reply.data)
                self.assertEqual((ScriptRevision.objects.count(), ScriptRevisionStructure.objects.count()), before)
                self.assert_unadopted(artifact)

    def test_edit_storage_or_selection_failure_rolls_back_all_new_facts(self):
        artifact = self.candidate()
        body, _, _ = self.edited_body(artifact)
        before = (ScriptRevision.objects.count(), ScriptRevisionStructure.objects.count())
        for target in ("studio.services.script_revision_structures.save_script_revision_structure",
                "studio.services.script_revision_structures.read_confirmed_script_structure", "studio.services.script_review.select_artifact"):
            with self.subTest(target=target), patch(target, side_effect=ValidationError("isolated write failure")):
                reply = self.post(body)
                self.assertEqual(reply.status_code, 400, reply.data)
            self.assertEqual((ScriptRevision.objects.count(), ScriptRevisionStructure.objects.count()), before)
            self.assert_unadopted(artifact)

    def test_same_original_text_does_not_create_fake_edit_revision(self):
        artifact = self.candidate()
        body = self.approval(artifact)
        body["payload"]["script_content"] = self.text
        before = ScriptRevision.objects.count()
        reply = self.post(body)
        self.assertEqual(reply.status_code, 201, reply.data)
        self.assertEqual(ScriptRevision.objects.count(), before)
        self.run.refresh_from_db()
        self.assertNotIn("edited_from_revision_id", self.run.script_adoption_decision.stage_run.output_snapshot["candidate"])

    def test_edit_rejects_changed_baseline_before_creating_any_revision(self):
        artifact = self.candidate()
        body, _, _ = self.edited_body(artifact)
        before = ScriptRevision.objects.count()
        self.project.master_script = "later user edit"
        self.project.save(update_fields=("master_script",))
        reply = self.post(body)
        self.assertEqual(reply.status_code, 409, reply.data)
        self.assertEqual(ScriptRevision.objects.count(), before)
        self.assert_unadopted(artifact)
