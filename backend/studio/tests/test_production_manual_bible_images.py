"""Formal manual endpoint, original media archive/Asset write, isolated PNGs."""
from copy import deepcopy
from hashlib import sha256
from pathlib import Path
from unittest.mock import patch
from uuid import uuid4

from django.core.files.base import ContentFile
from django.test import TestCase, override_settings
from django.utils import timezone

from studio.models import ApprovalDecision, ArtifactSelectionDecision, Asset, GenerationAttempt, ProductionManualCommand, StageRun
from studio.services.json_contracts import assign_json_contract, json_contract
from studio.services.production_manual_actions import perform_manual_action
from studio.tests import test_execution_bible_asset_promotion as fixtures


@override_settings(AI_PROVIDER_ALLOWED_HOSTS=("127.0.0.1",))
class ManualBibleImageTests(TestCase):
    _Fixture = fixtures.BibleAssetPromotionTests._Fixture
    mark_submitted = fixtures.BibleAssetPromotionTests.mark_submitted
    archive = fixtures.BibleAssetPromotionTests.archive
    resolve = fixtures.BibleAssetPromotionTests.resolve

    def setUp(self):
        fixtures.BibleAssetPromotionTests.setUp(self)
        self.read_url = f"/api/v1/production-runs/{self.run.pk}/manual-bible-image-review/"
        self.action_url = f"/api/v1/production-runs/{self.run.pk}/manual-actions/"
        data = self.output.read_bytes()
        self.library = Asset(owner=self.user, kind="prop", name="Reviewed side view", mime_type="image/png", checksum=sha256(data).hexdigest())
        self.library.file.save("reviewed-side.png", ContentFile(data))

    def read(self):
        response = self.client.get(self.read_url)
        self.assertEqual(response.status_code, 200, response.data)
        self.assertEqual(response["Cache-Control"], "private, no-store")
        return response.data

    def request(self, detail, *, library_only=False):
        return {"action": "approve", "stage_key": "bible_image_review", "scope": {"kind": "project", "id": str(self.project.pk)},
            "unit_key": f"project:{self.project.pk}", "expected_run_plan_sha256": detail["run_plan_sha256"],
            "expected_selection_revision": detail["approval"]["revision"], "idempotency_key": str(uuid4()),
            "payload": {"expected_candidates_sha256": detail["candidates_sha256"], "expected_targets_sha256": detail["targets_sha256"],
                "expected_content_basis_sha256": detail["content_basis_sha256"],
                "comment": "Reviewed actual images", "bindings": [{"entity_type": "prop", "entity_id": str(self.entity.pk),
                    "selected_artifact_ids": [] if library_only else [str(self.artifact.pk)],
                    "selected_library_assets": [{"asset_id": str(self.library.pk), "expected_sha256": self.library.checksum}],
                    "anchor_artifact_id": None if library_only else str(self.artifact.pk),
                    "anchor_library_asset_id": str(self.library.pk) if library_only else None}]}}

    def settle_archived_task(self):
        # Archive fixture stops before queue receipt finalization. This is a
        # separate isolated observation, never real provider acceptance.
        self.task.refresh_from_db()
        self.task.submission_state, self.task.finished_at = "terminal_succeeded", timezone.now()
        self.task.save(update_fields=("submission_state", "finished_at"))

    def script_request(self, revision=0):
        from studio.services.production_run_plans import get_run_execution_plan
        from studio.services.script_sources import create_pasted_script_source, confirm_script_revision
        plan = get_run_execution_plan(self.run)
        for key in ("script_input", "script_review"):
            if not self.run.stages.filter(stage_key=key).exists():
                ordinal = next(item["ordinal"] for item in plan.value["stage_catalog"]["value"]["stages"] if item["key"] == key)
                stage = StageRun(production_run=self.run, stage_key=key, ordinal=ordinal,
                    mode="manual", status="pending", idempotency_key=str(uuid4()))
                assign_json_contract(stage, "output_snapshot", {"schema_version": 2, "run_plan_sha256": plan.sha256}, schema_version=2)
                stage.save()
        _, preview = create_pasted_script_source(project=self.project, user=self.user, content="人工确认的道具剧情")
        script = confirm_script_revision(preview, user=self.user)
        return {"action": "adopt", "stage_key": "script_input", "scope": {"kind": "project", "id": str(self.project.pk)},
            "unit_key": f"project:{self.project.pk}", "expected_run_plan_sha256": plan.sha256,
            "expected_selection_revision": revision, "idempotency_key": str(uuid4()), "payload": {"comment": "明确采用",
                "content": {"kind": "script_revision", "id": str(script.pk), "expected_sha256": script.content_sha256}}}

    def test_full_creative_basis_rejects_stale_request_and_binding_does_not_invalidate_itself(self):
        before = self.read()
        result = self.client.post(self.action_url, self.request(before, library_only=True), format="json")
        self.assertEqual(result.status_code, 201, result.data)
        current = self.read()
        self.assertEqual(current["approval"]["status"], "current")
        self.assertEqual(current["content_basis_sha256"], before["content_basis_sha256"])
        stale = self.request(current, library_only=True)
        self.entity.material = "明确调整为银制"
        self.entity.save(update_fields=("material",))
        changed = self.read()
        self.assertEqual(changed["targets_sha256"], current["targets_sha256"])
        self.assertEqual(changed["approval"]["status"], "changed")
        self.assertIn("人物、场景或道具设定", changed["approval"]["changes"])
        self.assertEqual(self.client.post(self.action_url, stale, format="json").status_code, 409)
        result = self.client.post(self.action_url, self.request(changed, library_only=True), format="json")
        self.assertEqual(result.status_code, 201, result.data)
        self.assertEqual(self.read()["approval"]["status"], "current")
        self.assertEqual(ApprovalDecision.objects.filter(stage_run=self.review).count(), 2)

    def test_finished_bible_generation_allows_script_readoption_then_requires_explicit_image_review(self):
        self.settle_archived_task()
        request = self.script_request()
        result = self.client.post(self.action_url, request, format="json")
        self.assertEqual(result.status_code, 201, result.data)
        result = self.client.post(self.action_url, self.request(self.read(), library_only=True), format="json")
        self.assertEqual(result.status_code, 201, result.data)
        current = self.read()
        old_request = self.request(current, library_only=True)
        self.review.refresh_from_db()
        old_snapshot = deepcopy(self.review.output_snapshot)
        original = GenerationAttempt.objects.values("request_sha256", "result_sha256").get(pk=self.attempt.pk)
        impact = self.client.get(f"/api/v1/production-runs/{self.run.pk}/manual-change-impact/", {"stage_key": "script_input"})
        self.assertEqual(impact.status_code, 200, impact.data)
        self.assertEqual(impact.data["replacement"]["code"], "media_review_required")
        # Same content, new original adoption decision still changes context.
        request.update(expected_selection_revision=1, idempotency_key=str(uuid4()))
        with patch("studio.services.execution_input_resolution.resolve_execution_input_sources", side_effect=AssertionError("Do not re-resolve old inputs")):
            result = self.client.post(self.action_url, request, format="json")
            self.assertEqual(result.status_code, 201, result.data)
            changed = self.read()
        self.assertEqual(changed["approval"]["status"], "changed")
        self.assertIn("本轮采用剧本", changed["approval"]["changes"])
        self.assertIn("上游采用或审核修订", changed["approval"]["changes"])
        self.review.refresh_from_db()
        self.assertEqual(self.review.output_snapshot, old_snapshot)
        self.assertEqual(self.client.post(self.action_url, old_request, format="json").status_code, 409)
        result = self.client.post(self.action_url, self.request(changed, library_only=True), format="json")
        self.assertEqual(result.status_code, 201, result.data)
        self.assertEqual(self.read()["approval"]["status"], "current")
        self.assertEqual(original, GenerationAttempt.objects.values("request_sha256", "result_sha256").get(pk=self.attempt.pk))

    def test_bible_generation_unknown_outcome_and_missing_review_basis_never_allow_reuse(self):
        request = self.script_request()
        impact_url = f"/api/v1/production-runs/{self.run.pk}/manual-change-impact/"
        self.assertEqual(self.client.get(impact_url, {"stage_key": "script_input"}).data["replacement"]["code"], "unresolved_calls")
        self.assertEqual(self.client.post(self.action_url, request, format="json").status_code, 400)
        self.settle_archived_task()
        self.assertEqual(self.client.post(self.action_url, request, format="json").status_code, 201)
        result = self.client.post(self.action_url, self.request(self.read(), library_only=True), format="json")
        self.assertEqual(result.status_code, 201, result.data)
        self.review.refresh_from_db()
        original = deepcopy(self.review.output_snapshot)
        for changed in ({key: value for key, value in original.items() if key != "content_basis"},
                {**original, "schema_version": 1}, {**original, "content_basis": {}},
                {**original, "run_plan_sha256": "0" * 64}):
            frozen = json_contract(changed, schema_version=changed["schema_version"])
            StageRun.objects.filter(pk=self.review.pk).update(output_snapshot=frozen.value,
                output_snapshot_schema_version=frozen.schema_version, output_snapshot_size_bytes=frozen.size_bytes,
                output_snapshot_sha256=frozen.sha256)
            self.assertIn(self.client.get(self.read_url).status_code, (400, 409))
            impact = self.client.get(impact_url, {"stage_key": "script_input"})
            self.assertFalse(impact.data["replacement"]["allowed"])

    def test_formal_review_and_idempotency_do_not_complete_a_producer_or_start_next_stage(self):
        before_status = self.stage.status
        detail = self.read()
        self.assertEqual(detail["approval"]["status"], "not_reviewed")
        self.assertEqual(detail["candidates"]["entities"][0]["candidates"][0]["preparation_id"], str(self.preparation.pk))
        request = self.request(detail)
        response = self.client.post(self.action_url, request, format="json")
        self.assertEqual(response.status_code, 201, response.data)
        self.entity.refresh_from_db()
        self.artifact.refresh_from_db()
        self.stage.refresh_from_db()
        self.assertEqual(self.stage.status, before_status)
        self.assertEqual(self.entity.anchor_asset_id, self.artifact.asset_id)
        self.assertEqual(list(self.entity.reference_assets.values_list("pk", flat=True)), [self.library.pk])
        self.assertEqual(ApprovalDecision.objects.filter(stage_run=self.review).count(), 1)
        self.assertEqual(ArtifactSelectionDecision.objects.filter(stage_run=self.review).count(), 1)
        self.assertEqual(self.read()["approval"]["status"], "current")
        counts = (Asset.objects.count(), ApprovalDecision.objects.count(), ProductionManualCommand.objects.count())
        replay = self.client.post(self.action_url, request, format="json")
        self.assertEqual(replay.status_code, 200, replay.data)
        self.assertEqual(counts, (Asset.objects.count(), ApprovalDecision.objects.count(), ProductionManualCommand.objects.count()))

    def test_library_only_with_pending_producer_does_not_need_fake_model_evidence(self):
        self.artifact.delete()
        self.stage.status = "pending"
        self.stage.save(update_fields=("status",))
        detail = self.read()
        self.assertEqual(detail["candidates"]["entities"][0]["candidates"], [])
        counts = (self.stage.generation_attempts.count(), self.stage.execution_preparations.count())
        response = self.client.post(self.action_url, self.request(detail, library_only=True), format="json")
        self.assertEqual(response.status_code, 201, response.data)
        self.entity.refresh_from_db()
        self.stage.refresh_from_db()
        self.assertEqual(self.entity.anchor_asset_id, self.library.pk)
        self.assertEqual(self.stage.status, "pending")
        self.assertEqual(counts, (self.stage.generation_attempts.count(), self.stage.execution_preparations.count()))
        self.assertFalse(ArtifactSelectionDecision.objects.filter(stage_run=self.review).exists())

    def test_stale_relations_scope_and_downstream_work_never_write(self):
        request = self.request(self.read())
        self.entity.reference_assets.add(self.library)
        response = self.client.post(self.action_url, request, format="json")
        self.assertEqual(response.status_code, 409, response.data)
        self.entity.reference_assets.clear()
        bad = deepcopy(request)
        bad["payload"]["bindings"][0]["entity_id"] = str(uuid4())
        response = self.client.post(self.action_url, bad, format="json")
        self.assertEqual(response.status_code, 400, response.data)
        StageRun.objects.create(production_run=self.run, stage_key="storyboard_review", ordinal=self.review.ordinal + 1,
            mode="manual", status="running", idempotency_key=str(uuid4()))
        response = self.client.post(self.action_url, request, format="json")
        self.assertEqual(response.status_code, 400, response.data)
        self.assertFalse(ProductionManualCommand.objects.filter(run=self.run).exists())
        self.assertFalse(ApprovalDecision.objects.filter(stage_run=self.review).exists())
        self.entity.refresh_from_db()
        self.assertIsNone(self.entity.anchor_asset_id)

    def test_receipt_failure_removes_new_files_and_reverts_all_business_writes(self):
        request = self.request(self.read())
        copied, before = [], Asset.objects.count()
        from studio.services.production_runs import _StoredAssetFileGuard
        track = _StoredAssetFileGuard.track
        def tracked(guard, file):
            copied.append(Path(file.path))
            track(guard, file)
        with patch.object(_StoredAssetFileGuard, "track", tracked), patch.object(ProductionManualCommand, "save", side_effect=RuntimeError("receipt failed")):
            with self.assertRaisesMessage(RuntimeError, "receipt failed"):
                perform_manual_action(run_id=self.run.pk, request=request, actor=self.user)
        self.assertTrue(copied)
        self.assertTrue(all(not path.exists() for path in copied))
        self.assertEqual(Asset.objects.count(), before)
        self.assertFalse(ApprovalDecision.objects.filter(stage_run=self.review).exists())
        self.assertFalse(ArtifactSelectionDecision.objects.filter(stage_run=self.review).exists())
        self.entity.refresh_from_db()
        self.artifact.refresh_from_db()
        self.assertIsNone(self.entity.anchor_asset_id)
        self.assertIsNone(self.artifact.asset_id)

    def test_read_is_not_mutation_and_does_not_allow_another_owner(self):
        from django.contrib.auth import get_user_model
        before = (Asset.objects.count(), ApprovalDecision.objects.count(), ProductionManualCommand.objects.count())
        self.read()
        self.assertEqual(before, (Asset.objects.count(), ApprovalDecision.objects.count(), ProductionManualCommand.objects.count()))
        self.assertEqual(self.client.post(self.read_url, {}, format="json").status_code, 405)
        self.assertEqual(self.client.get(self.read_url, {"entity_id": str(self.entity.pk)}).status_code, 400)
        other = get_user_model().objects.create_user(username="manual-image-other", password="isolated-only")
        self.client.force_authenticate(other)
        self.assertEqual(self.client.get(self.read_url).status_code, 404)
