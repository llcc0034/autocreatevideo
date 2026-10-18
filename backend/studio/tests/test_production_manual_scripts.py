"""Formal manual script approval through real preparations and original worker.

Only the external provider response is an isolated fixture; no live model call.
"""
from copy import deepcopy
from decimal import Decimal
from unittest.mock import Mock, patch

from django.core.exceptions import ValidationError
from django.test import TestCase, override_settings

from studio.models import ApprovalDecision, ArtifactSelectionDecision, ExecutionPreparation, GeneratedArtifact, GenerationAttempt, ProductionManualCommand, ProductionRun, ScriptRevision
from studio.services.execution_advancement import advance_execution_preparation
from studio.services.script_sources import create_pasted_script_source, confirm_script_revision
from studio.tests import test_production_manual_preparations as manual
from studio.tests import test_execution_production_dispatch as dispatch


@override_settings(AI_PROVIDER_ALLOWED_HOSTS=("api.example.com",))
class ManualScriptReviewTests(TestCase):
    setUpTestData = classmethod(manual.ManualPreparationTests.setUpTestData.__func__)
    def setUp(self):
        from tempfile import TemporaryDirectory
        root = self.enterContext(TemporaryDirectory())
        self.enterContext(override_settings(MEDIA_ROOT=root))
        manual.ManualPreparationTests.setUp(self)
    post = manual.ManualPreparationTests.post
    preparation = manual.ManualPreparationTests.preparation

    def make_candidate(self):
        from studio.services.deployment_smoke_queue import claim_next_deployment_smoke, process_deployment_smoke
        from studio.services.text_generation_queue import claim_next_text_attempt, process_text_attempt
        _, smoke, _ = manual.native.ExecutionNativeTextTests.smoke(self)
        smoke = advance_execution_preparation(preparation_id=smoke.pk, expected_preparation_revision=smoke.revision, actor=self.actor)
        self.assertEqual(smoke.status, "dispatched", smoke.reason)
        self.operation_plan = smoke.contract.prepared_request["transport"]["operation_plan"]["plan"]
        peer = Mock()
        peer.invoke.return_value = dispatch.ProductionDispatchTests.response(self)
        with patch("studio.services.execution_smoke_worker.driver_for", return_value=peer):
            result = process_deployment_smoke(claim_next_deployment_smoke("manual-script-smoke"))
        self.assertEqual(result.status, "succeeded", result.error_code)
        self.run.allow_external_calls = True
        self.run.save(update_fields=("allow_external_calls",))
        prep = self.preparation()
        result = advance_execution_preparation(preparation_id=prep.pk, expected_preparation_revision=prep.revision, actor=self.actor)
        self.assertEqual(result.status, "dispatched", result.reason)
        self.operation_plan = result.contract.prepared_request["transport"]["operation_plan"]["plan"]
        peer.invoke.return_value = dispatch.ProductionDispatchTests.response(self)
        with patch("studio.services.execution_production_text_worker.driver_for", return_value=peer):
            attempt = process_text_attempt(claim_next_text_attempt("manual-script-worker"))
        self.assertEqual(attempt.status, "succeeded", attempt.error_message)
        return GeneratedArtifact.objects.get(generation_attempt=attempt)

    def options(self):
        response = self.client.get(f"/api/v1/production-runs/{self.run.pk}/manual-action-options/")
        self.assertEqual(response.status_code, 200, response.data)
        return response.data

    def approval(self, artifact):
        state = self.options()
        return {**self.body, "stage_key": "script_review", "action": "approve", "idempotency_key": "review-script",
            "expected_selection_revision": state["script_review"]["revision"], "payload": {
                "text_candidate": {"artifact_id": str(artifact.pk), "expected_sha256": artifact.sha256,
                    "expected_manifest_sha256": artifact.manifest_metadata_sha256},
                "expected_current_script": state["current_script"], "comment": "已阅读候选"}}

    def assert_identity_query(self, expected):
        from django.db import connection
        from django.test.utils import CaptureQueriesContext
        with CaptureQueriesContext(connection) as queries:
            state = self.options()
        self.assertEqual(state['current_script_identity'], expected)
        identity_queries = [q['sql'] for q in queries if '"studio_generatedartifact"' in q['sql']]
        self.assertEqual(len(identity_queries), 1)
        self.assertNotIn('"manifest_metadata"', identity_queries[0])
        self.assertNotIn('"request_snapshot"', identity_queries[0])
        if connection.vendor == 'sqlite':
            with connection.cursor() as cursor:
                cursor.execute('EXPLAIN QUERY PLAN ' + identity_queries[0])
                self.assertIn('INDEX', str(cursor.fetchall()).upper())

    def test_approval_materializes_original_revision_and_replay_does_not_dispatch(self):
        artifact = self.make_candidate()
        self.project.title, self.project.logline = "人工项目标题", "人工维护的项目梗概"
        self.project.save(update_fields=("title", "logline"))
        body = self.approval(artifact)
        result = self.post(body)
        self.assertEqual(result.status_code, 201, result.data)
        self.run.refresh_from_db(); self.project.refresh_from_db(); artifact.refresh_from_db(); self.stage.refresh_from_db()
        self.assertEqual(self.run.script_revision_id, ScriptRevision.objects.get(pk=artifact.manifest_metadata["script_revision_id"]).pk)
        self.assertEqual(self.project.master_script, artifact.manifest_metadata["result"]["result"]["candidate"]["master_script"])
        self.assertEqual((self.project.title, self.project.logline), ("人工项目标题", "人工维护的项目梗概"))
        self.assertEqual(artifact.status, "selected")
        self.assertEqual(ArtifactSelectionDecision.objects.filter(artifact=artifact).count(), 1)
        self.assertEqual(self.stage.status, "running")  # producer remains open for other candidates
        activity = next(row for row in self.options()["execution_activity"] if row["stage_id"] == str(self.stage.pk))
        self.assertEqual(activity["lifecycle_label"], "步骤开放")
        self.assertEqual([(row["role"], row["status"], row["count"]) for row in activity["calls"]], [("primary", "succeeded", 1)])
        self.assertFalse(self.run.stages.filter(ordinal__gt=4).exclude(status="pending").exists())
        state = self.options()
        self.assertIsNone(state["script_adoption"]["content"])
        self.assertEqual(state["current_script"], state["script_review"]["content"])
        self.assertEqual(state["script_review"]["revision"], 1)
        self.assertEqual(state["current_script_decision_id"], state["script_review"]["decision_id"])
        self.assertEqual(str(self.run.script_adoption_decision_id), state["current_script_decision_id"])
        identity = state["current_script_identity"]
        self.assert_identity_query(identity)
        self.assertEqual(identity["decision_stage_key"], "script_review")
        self.assertEqual(identity["candidate"], {"artifact_id": str(artifact.pk), "variant_index": artifact.variant_index,
            "stage_id": str(artifact.stage_run_id), "stage_key": artifact.stage_run.stage_key,
            "attempt_id": str(artifact.generation_attempt_id), "preparation_id": str(artifact.generation_attempt.preparation_id)})
        self.assertNotEqual(identity["candidate"]["artifact_id"], state["current_script"]["id"])
        self.assertNotIn("candidate", state["current_script"])  # presentation cannot change frozen approval input
        calls = GenerationAttempt.objects.count()
        again = self.post(body)
        self.assertEqual((again.status_code, again.data), (200, result.data))
        self.assertEqual(GenerationAttempt.objects.count(), calls)
        self.assertEqual(ApprovalDecision.objects.filter(stage_run__production_run=self.run).count(), 1)

    def test_manual_approval_rejects_removed_metadata_switch(self):
        artifact = self.make_candidate()
        body = self.approval(artifact)
        for value in (True, False):
            body["payload"]["adopt_generated_metadata"] = value
            response = self.post(body)
            self.assertEqual(response.status_code, 400, response.data)
        artifact.refresh_from_db()
        self.assertEqual(artifact.status, "available")
        self.assertFalse(ApprovalDecision.objects.exists())
        self.assertFalse(ArtifactSelectionDecision.objects.exists())

    def test_identity_is_explicit_for_no_script_and_confirmed_script_without_a_candidate(self):
        self.assertIsNone(self.options()["current_script_identity"])
        source, revision = create_pasted_script_source(project=self.project, content="人工原稿", user=self.actor)
        revision = confirm_script_revision(revision, user=self.actor)
        body = {**self.body, "action": "adopt", "stage_key": "script_input", "idempotency_key": "identity-adopt",
            "expected_selection_revision": 0, "payload": {"content": {"kind": "script_revision", "id": str(revision.pk),
                "expected_sha256": revision.content_sha256}, "comment": "隔离测试明确采用"}}
        response = self.post(body)
        self.assertEqual(response.status_code, 201, response.data)
        state = self.options()
        self.assertEqual(state["current_script_identity"], {"source_name": "", "source_type_label": "粘贴",
            "source_format_label": "纯文本", "decision_stage_key": "script_input", "candidate": None})
        self.assertEqual(state["current_script"]["source_id"], str(source.pk))
        self.assertFalse(GenerationAttempt.objects.exists())

    def test_identity_checks_original_candidate_relation_instead_of_same_text(self):
        artifact = self.make_candidate()
        self.assertEqual(self.post(self.approval(artifact)).status_code, 201)
        # Do not silently substitute another artifact, even if its text digest matches.
        GeneratedArtifact.objects.filter(pk=artifact.pk).update(role="unrelated")
        response = self.client.get(f"/api/v1/production-runs/{self.run.pk}/manual-action-options/")
        self.assertEqual(response.status_code, 400, response.data)
        self.assertIn("原剧本候选", str(response.data))

    def test_activity_aggregates_call_facts_without_payload_reads_or_stage_status_rewrite(self):
        from django.db import connection
        from django.test.utils import CaptureQueriesContext
        from studio.services.production_manual_activity import manual_execution_activity
        artifact = self.make_candidate()
        attempt = artifact.generation_attempt
        for status in ("running", "awaiting_review", "failed", "succeeded"):
            GenerationAttempt.objects.filter(pk=attempt.pk).update(status=status)
            with CaptureQueriesContext(connection) as queries:
                rows = manual_execution_activity(self.run)
            self.assertEqual(len(queries), 3)
            self.assertFalse(any('"request_snapshot"' in q['sql'] or '"prepared_request"' in q['sql'] for q in queries))
            current = next(row for row in rows if row["stage_id"] == str(self.stage.pk))
            self.assertEqual(current["stage_status"], "running")
            self.assertEqual(current["calls"][0]["status"], status)
            self.assertEqual(current["calls"][0]["count"], 1)
        query = GenerationAttempt.objects.filter(stage_run__production_run_id=self.run.pk).values("stage_run_id", "status")
        self.assertIn("INDEX", query.explain().upper())

    def test_action_options_query_count_stays_fixed_when_preparations_and_calls_grow(self):
        from django.db import connection
        from django.test.utils import CaptureQueriesContext
        from studio.services.production_manual_actions import manual_action_options

        artifact = self.make_candidate()
        attempt = artifact.generation_attempt
        parent = attempt.preparation

        def read_options():
            with CaptureQueriesContext(connection) as queries:
                state = manual_action_options(self.run)
            self.assertTrue(all(row["sql"].startswith(("SELECT", "SAVEPOINT", "RELEASE SAVEPOINT")) for row in queries))
            reads = [row["sql"] for row in queries if row["sql"].startswith("SELECT")]
            # No adopted script yet: one fewer revision identity read than the
            # adopted-script fixture in test_production_manual_actions.
            self.assertEqual(len(reads), 11, reads)
            for sql in reads:
                for field in ("content", "request_snapshot", "prepared_request", "resolved_inputs", "manifest_metadata"):
                    self.assertNotIn(f'"{field}"', sql)
            return state

        initial = read_options()
        before = next(row for row in initial["execution_activity"] if row["stage_id"] == str(self.stage.pk))
        self.assertEqual(sum(row["count"] for row in before["calls"]), 1)
        # Metadata-only rows exercise the read-side grouping in the isolated
        # test DB. They are not compiled requests or real execution evidence.
        expected = 0
        for batch, size in enumerate((5, 25)):
            preparations = ExecutionPreparation.objects.bulk_create([
                ExecutionPreparation(stage_run=self.stage, execution_instance_version=attempt.execution_instance_version,
                    purpose="production", unit_key=parent.unit_key, request_key=f"activity-load:{batch}:{index}",
                    created_by=self.actor, intent_sha256="a" * 64, status="awaiting_review")
                for index in range(size)])
            GenerationAttempt.objects.bulk_create([
                GenerationAttempt(stage_run=self.stage, preparation=preparation,
                    execution_instance_version=attempt.execution_instance_version,
                    role="primary" if batch == 0 else "auxiliary",
                    parent_preparation=None if batch == 0 else parent,
                    auxiliary_purpose="" if batch == 0 else "generate_fields",
                    field_batch_key="" if batch == 0 else f"activity-fields:{index}",
                    unit_key=f"activity-load:{batch}:{index}", attempt_number=1,
                    status="failed" if batch == 0 else "awaiting_review", request_sha256="b" * 64)
                for index, preparation in enumerate(preparations)])
            expected += size
            state = read_options()
            activity = next(row for row in state["execution_activity"] if row["stage_id"] == str(self.stage.pk))
            self.assertEqual(len(state["execution_activity"]), len(initial["execution_activity"]))
            self.assertEqual(activity["stage_status"], "running")
            self.assertEqual(activity["lifecycle_label"], "步骤开放")
            self.assertEqual({row["status"]: row["count"] for row in activity["preparations"]},
                {"dispatched": 1, "awaiting_review": expected})
            calls = {(row["role"], row["status"]): row["count"] for row in activity["calls"]}
            self.assertEqual(calls, {("primary", "succeeded"): 1, ("primary", "failed"): 5,
                **({("auxiliary", "awaiting_review"): 25} if batch else {})})
            self.assertEqual(state["script_adoption"], initial["script_adoption"])
            self.assertIsNone(state["current_script"])
        self.stage.refresh_from_db()
        self.assertEqual(self.stage.status, "running")

    def test_initial_adoption_is_history_after_generated_approval(self):
        _, preview = create_pasted_script_source(project=self.project, user=self.actor, content="已确认的原始剧本")
        revision = confirm_script_revision(preview, user=self.actor)
        adopted = self.post({**self.body, "stage_key": "script_input", "action": "adopt", "idempotency_key": "initial-script",
            "expected_selection_revision": 0, "payload": {"comment": "导入旧稿", "content": {
                "kind": "script_revision", "id": str(revision.pk), "expected_sha256": revision.content_sha256}}})
        self.assertEqual(adopted.status_code, 201, adopted.data)
        artifact = self.make_candidate()
        self.assertEqual(self.post(self.approval(artifact)).status_code, 201)
        state = self.options()
        self.assertEqual(state["script_adoption"]["content"]["id"], str(revision.pk))
        self.assertNotEqual(state["current_script"]["id"], str(revision.pk))
        self.assertEqual(state["script_adoption"]["revision"], state["script_review"]["revision"])
        # Separate per-stage revision 1, not a newly invented global counter.
        self.assertEqual(state["current_script_decision_id"], state["script_review"]["decision_id"])
        # Return to the explicitly chosen input through the original action,
        # without relabelling the previous generated result as new work.
        before = GenerationAttempt.objects.count()
        result = self.post({**self.body, "stage_key": "script_input", "action": "adopt", "idempotency_key": "readopt-input",
            "expected_selection_revision": 1, "payload": {"comment": "明确重新采用原稿", "content": {
                "kind": "script_revision", "id": str(revision.pk), "expected_sha256": revision.content_sha256}}})
        self.assertEqual(result.status_code, 201, result.data)
        from studio.services.production_manual_scripts import manual_script_states
        self.run.refresh_from_db()
        read = manual_script_states(self.run)
        self.assertEqual(read["current_script"], read["script_adoption"]["content"])
        self.assertEqual(read["script_review"], state["script_review"])
        self.assertEqual(read["current_script_decision_id"], result.data["target_id"])
        self.assertEqual(read["script_adoption"]["revision"], 2)
        self.assertEqual(GenerationAttempt.objects.count(), before)

    def test_changed_source_keeps_old_candidate_and_requires_explicit_new_preparation(self):
        from studio.services.production_script_baselines import read_script_adoption_baseline
        from studio.services.text_generation_queue import claim_next_text_attempt, process_text_attempt
        artifact = self.make_candidate()
        prep = artifact.generation_attempt.preparation
        original = (prep.request_sha256, deepcopy(prep.contract.prepared_request), read_script_adoption_baseline(prep))
        _, revision = create_pasted_script_source(project=self.project, user=self.actor, content="明确选择的新剧本")
        revision = confirm_script_revision(revision, user=self.actor)
        change = {**self.body, "stage_key": "script_input", "action": "adopt", "idempotency_key": "changed-source",
            "expected_selection_revision": 0, "payload": {"comment": "新来源", "content": {
                "kind": "script_revision", "id": str(revision.pk), "expected_sha256": revision.content_sha256}}}
        impact = self.client.get(f"/api/v1/production-runs/{self.run.pk}/manual-change-impact/", {"stage_key": "script_input"})
        self.assertEqual(impact.status_code, 200, impact.data)
        self.assertEqual(impact.data["replacement"]["code"], "text_review_required")
        self.assertEqual(self.post(change).status_code, 201)
        stale = self.post(self.approval(artifact))
        self.assertEqual(stale.status_code, 409, stale.data)
        self.assertIn("script_candidate_source_drift", str(stale.data))
        self.assertEqual(GenerationAttempt.objects.filter(stage_run=self.stage).count(), 1)
        prep.refresh_from_db()
        self.assertEqual((prep.request_sha256, prep.contract.prepared_request, read_script_adoption_baseline(prep)), original)
        self.assertEqual(self.post().data["target_id"], str(prep.pk))
        create = self.post({**self.body, "idempotency_key": "explicit-new-candidate"})
        self.assertEqual(create.status_code, 202, create.data)
        second = ExecutionPreparation.objects.get(pk=create.data["target_id"])
        self.assertEqual(read_script_adoption_baseline(second)["script_revision_id"], str(revision.pk))
        self.assertIsNone(second.next_check_at)
        self.assertFalse(GenerationAttempt.objects.filter(preparation=second).exists())
        advanced = advance_execution_preparation(preparation_id=second.pk, expected_preparation_revision=second.revision, actor=self.actor)
        self.assertEqual(advanced.status, "dispatched", advanced.reason)
        self.operation_plan = advanced.contract.prepared_request["transport"]["operation_plan"]["plan"]
        peer = Mock(); peer.invoke.return_value = dispatch.ProductionDispatchTests.response(self)
        with patch("studio.services.execution_production_text_worker.driver_for", return_value=peer):
            attempt = process_text_attempt(claim_next_text_attempt("manual-explicit-redo"))
        self.assertEqual(attempt.status, "succeeded", attempt.error_message)
        next_artifact = GeneratedArtifact.objects.get(generation_attempt=attempt)
        self.assertEqual(self.post(self.approval(next_artifact)).status_code, 201)
        artifact.refresh_from_db()
        self.assertEqual(artifact.status, "available")

    def test_completed_text_plus_new_unstopped_preparation_cannot_be_overlooked(self):
        from studio.services.execution_production_cancellation import cancel_production_preparation
        artifact = self.make_candidate()
        create = self.post({**self.body, "idempotency_key": "unsubmitted-extra"})
        prep = ExecutionPreparation.objects.get(pk=create.data["target_id"])
        url = f"/api/v1/production-runs/{self.run.pk}/manual-change-impact/"
        impact = self.client.get(url, {"stage_key": "script_input"})
        self.assertEqual(impact.data["replacement"]["code"], "active_preparations")
        cancel_production_preparation(preparation_id=prep.pk, expected_preparation_revision=prep.revision, actor=self.actor)
        self.assertEqual(self.client.get(url, {"stage_key": "script_input"}).data["replacement"]["code"], "text_review_required")
        original = deepcopy(artifact.manifest_metadata)
        GeneratedArtifact.objects.filter(pk=artifact.pk).update(manifest_metadata={})
        impact = self.client.get(url, {"stage_key": "script_input"})
        self.assertEqual(impact.data["replacement"]["code"], "inconsistent_records")
        GeneratedArtifact.objects.filter(pk=artifact.pk).update(manifest_metadata=original)
        GenerationAttempt.objects.filter(pk=artifact.generation_attempt_id).update(error_code="uncertain_remote_response")
        self.assertEqual(self.client.get(url, {"stage_key": "script_input"}).data["replacement"]["code"], "unresolved_calls")

    def test_text_passive_review_adopted_media_and_stopped_preparation_share_one_change_gate(self):
        from pathlib import Path
        from tempfile import TemporaryDirectory
        from uuid import uuid4
        from studio.models import Asset, Episode, Shot
        from studio.services.execution_production_cancellation import cancel_production_preparation
        from studio.tests.test_asset_management_api import image_upload
        root = Path(self.enterContext(TemporaryDirectory()))
        self.enterContext(override_settings(MEDIA_ROOT=root / "media", ASSET_LIBRARY_ROOT=root / "library"))
        artifact = self.make_candidate()
        episode = Episode.objects.create(project=self.project, number=1, title="Mixed review")
        shot = Shot.objects.create(episode=episode, number=1, duration_ms=5000,
            media_type="image", include_audio=False)
        base = f"/api/v1/production-runs/{self.run.pk}/"
        storyboard = self.client.get(base + "manual-storyboard/").data
        adopt = self.post({**self.body, "stage_key": "storyboard", "action": "adopt", "idempotency_key": str(uuid4()),
            "expected_selection_revision": storyboard["adoption"]["revision"], "payload": {
                "source": {"kind": "existing_storyboard"}, "expected_content_sha256": storyboard["content_sha256"],
                "reviewed_shot_ids": [str(shot.pk)], "comment": "Reviewed"}})
        self.assertEqual(adopt.status_code, 201, adopt.data)
        upload = self.client.post("/api/v1/assets/", {"kind": "reference", "name": "Isolated image",
            "description": "Test-only image", "file": image_upload("mixed.png")}, format="multipart")
        self.assertEqual(upload.status_code, 201, upload.data)
        asset = Asset.objects.get(pk=upload.data["id"])
        chosen = self.post({**self.body, "stage_key": "media_generation", "action": "adopt", "idempotency_key": str(uuid4()),
            "scope": {"kind": "shot", "id": str(shot.pk)}, "unit_key": f"shot:{shot.pk}",
            "expected_selection_revision": 0, "payload": {"source": {"kind": "asset", "asset_id": str(asset.pk),
                "expected_sha256": asset.checksum, "media_kind": "image"}}})
        self.assertEqual(chosen.status_code, 201, chosen.data)
        media = self.client.get(base + "manual-media-review/").data
        approved = self.post({**self.body, "stage_key": "artifact_selection", "action": "approve", "idempotency_key": str(uuid4()),
            "expected_selection_revision": media["approval"]["revision"], "payload": {"selection": media["selection"],
                "expected_selection_sha256": media["selection_sha256"], "comment": "Explicit reuse"}})
        self.assertEqual(approved.status_code, 201, approved.data)
        created = self.post({**self.body, "idempotency_key": "unused-next-text"})
        prep = ExecutionPreparation.objects.get(pk=created.data["target_id"])
        self.assertEqual(self.client.get(base + "manual-change-impact/", {"stage_key": "script_input"}).data["replacement"]["code"], "active_preparations")
        cancel_production_preparation(preparation_id=prep.pk, expected_preparation_revision=prep.revision, actor=self.actor)
        self.assertEqual(self.client.get(base + "manual-change-impact/", {"stage_key": "script_input"}).data["replacement"]["code"], "text_review_required")
        original = GenerationAttempt.objects.values("id", "request_sha256", "result_sha256").get(pk=artifact.generation_attempt_id)
        _, revision = create_pasted_script_source(project=self.project, user=self.actor, content="New explicitly adopted script")
        revision = confirm_script_revision(revision, user=self.actor)
        changed = self.post({**self.body, "stage_key": "script_input", "action": "adopt", "idempotency_key": str(uuid4()),
            "expected_selection_revision": 0, "payload": {"comment": "Changed", "content": {"kind": "script_revision",
                "id": str(revision.pk), "expected_sha256": revision.content_sha256}}})
        self.assertEqual(changed.status_code, 201, changed.data)
        self.assertEqual(self.client.get(base + "manual-storyboard/").data["adoption"]["status"], "changed")
        after = self.client.get(base + "manual-media-review/").data
        self.assertEqual(after["approval"]["status"], "changed")
        self.assertEqual(after["approval"]["decision_id"], approved.data["target_id"])
        self.assertEqual(GenerationAttempt.objects.values("id", "request_sha256", "result_sha256").get(pk=artifact.generation_attempt_id), original)
        self.assertEqual(GenerationAttempt.objects.filter(stage_run__production_run=self.run).count(), 1)

    def test_wrong_hash_stale_state_late_candidate_and_started_downstream_reject_without_decision(self):
        artifact = self.make_candidate()
        original = self.approval(artifact)
        for key in ("expected_sha256", "expected_manifest_sha256"):
            bad = deepcopy(original); bad["payload"]["text_candidate"][key] = "0" * 64
            self.assertEqual(self.post(bad).status_code, 409)
        bad = deepcopy(original); bad["expected_selection_revision"] = 1
        self.assertEqual(self.post(bad).status_code, 409)
        bad = deepcopy(original); bad["payload"]["expected_current_script"] = {"id": str(artifact.pk)}
        self.assertEqual(self.post(bad).status_code, 409)
        GenerationAttempt.objects.filter(pk=artifact.generation_attempt_id).update(ignore_late_result=True)
        self.assertEqual(self.post(original).status_code, 400)
        GenerationAttempt.objects.filter(pk=artifact.generation_attempt_id).update(ignore_late_result=False)
        self.run.stages.filter(stage_key="storyboard").update(status="running")
        self.assertEqual(self.post(original).status_code, 400)
        self.assertFalse(ApprovalDecision.objects.filter(stage_run__production_run=self.run).exists())
        self.assertFalse(ProductionManualCommand.objects.filter(action="approve").exists())
        self.assertEqual(ScriptRevision.objects.get(pk=artifact.manifest_metadata["script_revision_id"]).status, "preview")

    def test_changed_mother_draft_and_receipt_failure_do_not_partially_adopt(self):
        artifact = self.make_candidate()
        body = self.approval(artifact)
        before = self.project.master_script
        self.project.master_script = "生成后人工修改"; self.project.save(update_fields=("master_script",))
        self.assertEqual(self.post(body).status_code, 409)
        self.project.master_script = before; self.project.save(update_fields=("master_script",))
        with patch("studio.services.production_manual_commands.ProductionManualCommand.save", side_effect=ValidationError("receipt failure")):
            self.assertEqual(self.post(body).status_code, 400)
        self.project.refresh_from_db(); self.run.refresh_from_db(); artifact.refresh_from_db()
        self.assertEqual(self.project.master_script, before)
        self.assertIsNone(self.run.script_revision_id)
        self.assertIsNone(self.run.script_adoption_decision_id)
        self.assertEqual(artifact.status, "available")
        self.assertFalse(ArtifactSelectionDecision.objects.filter(artifact=artifact).exists())
        self.assertFalse(ApprovalDecision.objects.filter(stage_run__production_run=self.run).exists())
        self.assertEqual(self.post(body).status_code, 201)

    def test_closed_round_keeps_original_candidate_reads_but_rejects_new_review(self):
        from django.contrib.auth import get_user_model
        from django.db import connection, transaction
        from django.test.utils import CaptureQueriesContext
        artifact = self.make_candidate()
        original = (artifact.sha256, artifact.manifest_metadata_sha256, deepcopy(artifact.manifest_metadata))
        for selected in (False, True):
            if selected:
                self.assertEqual(self.post(self.approval(artifact)).status_code, 201)
            body = {**self.approval(artifact), "idempotency_key": "must-not-review-closed"}
            for status in ("succeeded", "failed", "cancelled"):
                with self.subTest(status=status, selected=selected), transaction.atomic():
                    ProductionRun.objects.filter(pk=self.run.pk).update(status=status)
                    with CaptureQueriesContext(connection) as queries:
                        state = self.options()
                        listing = self.client.get("/api/v1/generated-artifacts/", {
                            "production_run": str(self.run.pk), "stage_run": str(artifact.stage_run_id)})
                        detail = self.client.get(f"/api/v1/generated-artifacts/{artifact.pk}/")
                    self.assertEqual(listing.status_code, 200, listing.data)
                    self.assertEqual(detail.status_code, 200, detail.data)
                    self.assertEqual(listing.data["count"], 1)
                    self.assertEqual(detail.data["text_candidate"]["operation"], "script.generate")
                    self.assertEqual(detail.data["status"], "selected" if selected else "available")
                    self.assertTrue(next(row for row in state["actions"] if row["stage_key"] == "script_review")["source_stage_keys"])
                    for query in queries:
                        self.assertFalse(query["sql"].lstrip().upper().startswith(("INSERT", "UPDATE", "DELETE")), query)
                    rejected = self.post(body)
                    self.assertEqual(rejected.status_code, 400, rejected.data)
                    self.assertIn("此运行已结束", str(rejected.data))
                    self.assertFalse(ProductionManualCommand.objects.filter(idempotency_key="must-not-review-closed").exists())
                    artifact.refresh_from_db()
                    self.assertEqual((artifact.sha256, artifact.manifest_metadata_sha256, artifact.manifest_metadata), original)
                    transaction.set_rollback(True)
        outsider = get_user_model().objects.create_user(username="closed-result-outsider")
        self.client.force_authenticate(outsider)
        self.assertEqual(self.client.get(f"/api/v1/generated-artifacts/{artifact.pk}/").status_code, 404)
        listing = self.client.get("/api/v1/generated-artifacts/", {"production_run": str(self.run.pk)})
        self.assertEqual(listing.data["count"], 0)
