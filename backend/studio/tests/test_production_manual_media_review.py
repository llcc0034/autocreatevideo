"""Original PNG archive -> selection -> explicit approval, no assembly/AI I/O."""
from copy import deepcopy
from unittest.mock import patch
from uuid import uuid4

from django.contrib.auth import get_user_model
from django.core.exceptions import ValidationError
from django.db import connection
from django.test import TestCase, override_settings
from django.test.utils import CaptureQueriesContext

from studio.models import ApprovalDecision, ArtifactSelectionDecision, AssemblyJob, CharacterBible, Episode, GenerationAttempt, ProductionManualCommand, Shot, StageRun
from studio.services.generated_artifacts import select_artifact
from studio.services.execution_instances import execution_configuration_max_bytes
from studio.services.json_contracts import assign_json_contract
from studio.services.production_run_plans import get_run_execution_plan
from studio.services.production_manual_media_review import _selection
from studio.tests import test_production_manual_media_adoption as adoption


@override_settings(AI_PROVIDER_ALLOWED_HOSTS=("127.0.0.1",))
class ManualMediaReviewTests(TestCase):
    mark_submitted = adoption.ManualMediaAdoptionTests.mark_submitted
    archive = adoption.ManualMediaAdoptionTests.archive
    resolve = adoption.ManualMediaAdoptionTests.resolve
    post = adoption.ManualMediaAdoptionTests.post

    def setUp(self):
        adoption.ManualMediaAdoptionTests.setUp(self)
        plan = get_run_execution_plan(self.run)
        ordinal = next(row["ordinal"] for row in plan.value["stage_catalog"]["value"]["stages"] if row["key"] == "artifact_selection")
        self.review = StageRun(production_run=self.run, stage_key="artifact_selection", mode="manual",
            ordinal=ordinal, status="pending", idempotency_key=str(uuid4()))
        assign_json_contract(self.review, "output_snapshot", {"schema_version": 2,
            "run_plan_sha256": self.body["expected_run_plan_sha256"]}, schema_version=2)
        self.review.save()
        self.review_url = f"/api/v1/production-runs/{self.run.pk}/manual-media-review/"

    def inventory(self):
        response = self.client.get(self.review_url)
        self.assertEqual(response.status_code, 200, response.data)
        self.assertEqual(response["Cache-Control"], "private, no-store")
        return response.data

    def request(self, detail):
        return {"action": "approve", "stage_key": "artifact_selection", "scope": {"kind": "project", "id": str(self.project.pk)},
            "unit_key": f"project:{self.project.pk}", "expected_run_plan_sha256": detail["run_plan_sha256"],
            "expected_selection_revision": detail["approval"]["revision"], "idempotency_key": "review-media",
            "payload": {"selection": deepcopy(detail["selection"]), "expected_selection_sha256": detail["selection_sha256"],
                "comment": "已逐项核对采用对象和内容"}}

    def test_inventory_is_metadata_only_and_empty_approval_does_not_succeed(self):
        with patch("studio.services.production_manual_media_review.read_production_media_candidate", side_effect=AssertionError("No list probe")):
            detail = self.inventory()
            self.assertEqual(detail["selection"]["items"], [])
            self.assertEqual(detail["approval"]["status"], "not_reviewed")
            self.assertEqual(self.post(self.request(detail)).status_code, 400)
            self.assertEqual(self.post().status_code, 201)
            self.assertEqual(len(self.inventory()["selection"]["items"]), 1)
        self.assertFalse(ApprovalDecision.objects.exists())

    def test_approval_replay_and_same_selection_use_original_decision_without_advancing(self):
        self.assertEqual(self.post().status_code, 201)
        request = self.request(self.inventory())
        attempts = GenerationAttempt.objects.count()
        first = self.post(request)
        self.assertEqual(first.status_code, 201, first.data)
        self.assertEqual(first.data["result_kind"], "approval_decision")
        self.review.refresh_from_db(); self.stage.refresh_from_db()
        self.assertEqual((self.review.status, self.stage.status), ("succeeded", "running"))
        self.assertEqual(self.review.output_snapshot["selection"], request["payload"]["selection"])
        self.assertEqual(ApprovalDecision.objects.get().output_snapshot_sha256, self.review.output_snapshot_sha256)
        self.assertEqual(self.post(request).data["target_id"], first.data["target_id"])
        self.assertEqual(self.inventory()["approval"]["status"], "current")
        repeated = self.request(self.inventory()); repeated["idempotency_key"] = "same-selection"
        self.assertEqual(self.post(repeated).data["target_id"], first.data["target_id"])
        self.assertEqual(ApprovalDecision.objects.count(), 1)
        self.assertEqual(GenerationAttempt.objects.count(), attempts)
        self.assertFalse(AssemblyJob.objects.exists())

    def test_changed_selection_revision_is_visible_and_old_snapshot_cannot_be_approved(self):
        self.assertEqual(self.post().status_code, 201)
        old = self.request(self.inventory())
        self.assertEqual(self.post(old).status_code, 201)
        select_artifact(self.artifact, user=self.user)
        detail = self.inventory()
        self.assertEqual(detail["approval"]["status"], "changed")
        old["idempotency_key"] = "stale-review"
        self.assertEqual(self.post(old).status_code, 409)
        self.assertEqual(ApprovalDecision.objects.count(), 1)
        fresh = self.request(detail); fresh["idempotency_key"] = "review-changed"
        self.assertEqual(self.post(fresh).status_code, 201)
        self.assertEqual(self.inventory()["approval"]["revision"], 2)

    def test_wrong_member_scope_hash_or_revision_and_corrupt_media_are_rejected(self):
        self.assertEqual(self.post().status_code, 201)
        request = self.request(self.inventory())
        bad = deepcopy(request); bad["payload"]["selection"]["items"] = []
        wrong = deepcopy(request); wrong["scope"]["id"] = str(self.run.pk)
        for value in (bad, wrong, {**request, "expected_selection_revision": None},
                {**request, "payload": {**request["payload"], "expected_selection_sha256": "0" * 64}}):
            with self.subTest(value=value):
                self.assertIn(self.post(value).status_code, (400, 409))
        with patch("studio.services.production_manual_media_review.read_production_media_candidate", side_effect=ValidationError("Corrupt archived bytes")):
            self.assertEqual(self.post(request).status_code, 400)
        self.assertFalse(ApprovalDecision.objects.exists())
        self.assertFalse(ProductionManualCommand.objects.filter(action="approve").exists())

    def test_receipt_failure_rolls_back_approval_and_read_does_not_repair_corruption(self):
        self.assertEqual(self.post().status_code, 201)
        request = self.request(self.inventory())
        with patch("studio.services.production_manual_commands.ProductionManualCommand.save", side_effect=RuntimeError("receipt failure")):
            with self.assertRaises(RuntimeError): self.post(request)
        self.review.refresh_from_db()
        self.assertEqual(self.review.status, "pending")
        self.assertFalse(ApprovalDecision.objects.exists())
        ArtifactSelectionDecision.objects.all().delete()  # Deliberate corruption only in the isolated test DB.
        self.assertEqual(self.client.get(self.review_url).status_code, 400)

    def test_other_owner_and_hidden_member_filters_are_rejected(self):
        self.assertEqual(self.client.get(self.review_url, {"page": 1}).status_code, 400)
        self.client.force_authenticate(get_user_model().objects.create_user(username="other-media-review"))
        self.assertEqual(self.client.get(self.review_url).status_code, 404)

    def test_selection_inventory_is_one_narrow_indexed_query(self):
        self.assertEqual(self.post().status_code, 201)
        plan = get_run_execution_plan(self.run)
        max_manifest_bytes = execution_configuration_max_bytes()
        with CaptureQueriesContext(connection) as queries:
            inventory = _selection(self.run, self.stage, plan, max_manifest_bytes=max_manifest_bytes)
        self.assertEqual(len(queries), 1, list(queries))
        self.assertEqual(inventory.value["items"][0]["artifact_id"], str(self.artifact.pk))
        sql = queries[0]["sql"]
        self.assertNotIn("comparison_parameters", sql)
        self.assertNotIn("request_snapshot", sql)
        if connection.vendor == "sqlite":
            with connection.cursor() as cursor:
                cursor.execute("EXPLAIN QUERY PLAN " + sql)
                details = " ".join(str(row[-1]) for row in cursor.fetchall())
            self.assertIn("INDEX", details)
            self.assertNotIn("SCAN studio_generatedartifact", details)

    def test_creative_changes_require_explicit_reuse_without_rewriting_generation(self):
        episode = Episode.objects.create(project=self.project, number=1, title="Original episode")
        shot = Shot.objects.create(episode=episode, number=1, duration_ms=2000, media_type="image", include_audio=False)
        character = CharacterBible.objects.create(project=self.project, slug="lead", stable_speaker_id="S1", name="Lead", description="Original identity")
        self.assertEqual(self.post().status_code, 201)
        frozen_request = GenerationAttempt.objects.get(pk=self.attempt.pk).request_sha256
        original_manifest = self.artifact.manifest_metadata_sha256
        changes = [
            (shot, "title", "Different title", "分镜内容与参考素材"),
            (character, "description", "Different identity", "人物、场景或道具设定"),
            (self.project, "continuity_summary", "Different continuity", "影视规则"),
        ]
        for obj, field, value, label in changes:
            request = self.request(self.inventory()); request["idempotency_key"] = str(uuid4())
            self.assertEqual(self.post(request).status_code, 201)
            setattr(obj, field, value); obj.save(update_fields=(field,))
            detail = self.inventory()
            self.assertEqual(detail["approval"]["status"], "changed")
            self.assertIn(label, detail["approval"]["changes"])
            request["idempotency_key"] = str(uuid4())
            self.assertEqual(self.post(request).status_code, 409)
            from studio.services.execution_selected_outputs import resolve_selected_execution_media
            decision = ArtifactSelectionDecision.objects.get(artifact=self.artifact)
            with self.assertRaisesMessage(ValidationError, "请重新核对"):
                resolve_selected_execution_media(decision_id=str(decision.pk), artifact_id=str(self.artifact.pk),
                    owner=self.user, production_run_id=str(self.run.pk), eligible_stage_ids=[str(self.stage.pk)],
                    expected_kind="image", max_media_bytes=1024 * 1024)
            fresh = self.request(detail); fresh["idempotency_key"] = str(uuid4())
            self.assertEqual(self.post(fresh).status_code, 201)
            self.assertEqual(self.inventory()["approval"]["changes"], [])
        self.assertEqual(GenerationAttempt.objects.get(pk=self.attempt.pk).request_sha256, frozen_request)
        self.artifact.refresh_from_db()
        self.assertEqual(self.artifact.manifest_metadata_sha256, original_manifest)
        self.assertEqual(GenerationAttempt.objects.count(), 1)

    def test_content_inventory_batches_bible_references_without_per_entity_queries(self):
        CharacterBible.objects.create(project=self.project, slug="initial", stable_speaker_id="S1", name="Initial")
        with CaptureQueriesContext(connection) as initial:
            before = self.inventory()
        for number in range(6):
            CharacterBible.objects.create(project=self.project, slug=f"extra-{number}", stable_speaker_id=f"S{number + 2}", name=f"Extra {number}")
        with CaptureQueriesContext(connection) as expanded:
            after = self.inventory()
        self.assertNotEqual(before["selection"]["content_basis"]["bible_sha256"], after["selection"]["content_basis"]["bible_sha256"])
        self.assertEqual(len(initial), len(expanded))

    def test_old_or_incomplete_review_basis_is_not_treated_as_current(self):
        self.assertEqual(self.post().status_code, 201)
        self.assertEqual(self.post(self.request(self.inventory())).status_code, 201)
        self.review.refresh_from_db()
        saved = deepcopy(self.review.output_snapshot)
        for basis in ({}, {"storyboard_sha256": "invalid"}):
            broken = deepcopy(saved); broken["selection"]["content_basis"] = basis
            from studio.services.json_contracts import json_contract
            broken["selection_sha256"] = json_contract(broken["selection"]).sha256
            assign_json_contract(self.review, "output_snapshot", broken, schema_version=4)
            self.review.save()
            ApprovalDecision.objects.filter(stage_run=self.review).update(output_snapshot_sha256=self.review.output_snapshot_sha256)
            self.assertEqual(self.client.get(self.review_url).status_code, 400)
        for missing in ("items", "source_stage_id", "run_plan_sha256"):
            broken = deepcopy(saved); del broken["selection"][missing]
            broken["selection_sha256"] = json_contract(broken["selection"]).sha256
            assign_json_contract(self.review, "output_snapshot", broken, schema_version=4)
            self.review.save()
            ApprovalDecision.objects.filter(stage_run=self.review).update(output_snapshot_sha256=self.review.output_snapshot_sha256)
            self.assertEqual(self.client.get(self.review_url).status_code, 400)

    def authored_storyboard(self):
        plan = get_run_execution_plan(self.run)
        for key in ("script_input", "script_review", "storyboard"):
            ordinal = next(row["ordinal"] for row in plan.value["stage_catalog"]["value"]["stages"] if row["key"] == key)
            stage = StageRun(production_run=self.run, stage_key=key, ordinal=ordinal, mode="manual", status="pending", idempotency_key=str(uuid4()))
            assign_json_contract(stage, "output_snapshot", {"schema_version": 2, "run_plan_sha256": plan.sha256}, schema_version=2)
            stage.save()
        episode = Episode.objects.create(project=self.project, number=1)
        return Shot.objects.create(episode=episode, number=1, duration_ms=2000, include_audio=False, media_type="image")

    def adopt_storyboard(self, shot):
        response = self.client.get(f"/api/v1/production-runs/{self.run.pk}/manual-storyboard/")
        self.assertEqual(response.status_code, 200, response.data)
        detail = response.data
        request = {"action": "adopt", "stage_key": "storyboard", "scope": {"kind": "project", "id": str(self.project.pk)},
            "unit_key": f"project:{self.project.pk}", "expected_run_plan_sha256": detail["run_plan_sha256"],
            "expected_selection_revision": detail["adoption"]["revision"], "idempotency_key": str(uuid4()),
            "payload": {"source": {"kind": "existing_storyboard"}, "expected_content_sha256": detail["content_sha256"],
                "reviewed_shot_ids": [str(shot.pk)], "comment": "Explicitly adopt current storyboard"}}
        response = self.post(request)
        self.assertEqual(response.status_code, 201, response.data)
        return response.data["target_id"]

    def settle_archived_task(self):
        # The archival fixture deliberately stops before queue completion.
        # Supply its separately observed terminal receipt only in tests that
        # require a fully closed call; do not weaken the production gate.
        from django.utils import timezone
        self.task.refresh_from_db()
        self.task.submission_state = "terminal_succeeded"
        self.task.finished_at = timezone.now()
        self.task.save(update_fields=("submission_state", "finished_at"))

    def test_completed_media_allows_storyboard_readoption_then_requires_explicit_reuse(self):
        shot = self.authored_storyboard()
        self.settle_archived_task()
        self.assertEqual(self.post().status_code, 201)
        first = self.adopt_storyboard(shot)
        request = self.request(self.inventory()); self.assertEqual(self.post(request).status_code, 201)
        frozen = GenerationAttempt.objects.values("request_sha256", "result_sha256", "preparation_id").get(pk=self.attempt.pk)
        shot.title = "Revised action"; shot.save(update_fields=("title",))
        impact = self.client.get(f"/api/v1/production-runs/{self.run.pk}/manual-change-impact/", {"stage_key": "storyboard"})
        self.assertEqual(impact.status_code, 200, impact.data)
        self.assertEqual(impact.data["replacement"]["code"], "media_review_required")
        self.assertTrue(impact.data["replacement"]["allowed"])
        second = self.adopt_storyboard(shot)
        self.assertNotEqual(first, second)
        detail = self.inventory()
        self.assertEqual(set(detail["approval"]["changes"]), {"分镜内容与参考素材", "上游采用或审核修订", "交付分镜"})
        # A new human reuse review must not resolve the original inputs again:
        # they may refer to a previous candidate in this same media stage.
        with patch("studio.services.execution_input_resolution.resolve_execution_input_sources", side_effect=AssertionError("Frozen provenance is not re-resolved")):
            fresh = self.request(detail); fresh["idempotency_key"] = str(uuid4())
            self.assertEqual(self.post(fresh).status_code, 201)
        self.assertEqual(self.inventory()["approval"]["status"], "current")
        self.assertEqual(GenerationAttempt.objects.values("request_sha256", "result_sha256", "preparation_id").get(pk=self.attempt.pk), frozen)
        self.assertEqual(GenerationAttempt.objects.count(), 1)
        self.assertTrue(ApprovalDecision.objects.filter(pk=first).exists())
        # Even identical content with a new explicit adoption is a new basis.
        self.adopt_storyboard(shot)
        self.assertEqual(self.inventory()["approval"]["changes"], ["上游采用或审核修订"])

    def test_media_change_gate_keeps_running_unknown_and_unsubmitted_work_closed(self):
        from studio.models import ExecutionPreparation
        from studio.services.production_manual_change_impact import content_replacement_state
        from django.db import transaction
        self.authored_storyboard()
        source = StageRun.objects.get(production_run=self.run, stage_key="storyboard")
        def state():
            with transaction.atomic():
                return content_replacement_state(run=self.run, stage=source)
        self.assertEqual(state()["code"], "unresolved_calls")
        self.settle_archived_task()
        self.assertTrue(state()["allowed"])
        original = GenerationAttempt.objects.values("status", "error_code", "stage_run_id").get(pk=self.attempt.pk)
        for patch_value, code in (({"status": "submitted"}, "unresolved_calls"),
                ({"error_code": "uncertain_remote_response"}, "unresolved_calls"),
                ({"stage_run_id": source.pk}, "inconsistent_records")):
            GenerationAttempt.objects.filter(pk=self.attempt.pk).update(**patch_value)
            result = state(); self.assertFalse(result["allowed"]); self.assertEqual(result["code"], code)
            GenerationAttempt.objects.filter(pk=self.attempt.pk).update(**original)
        ExecutionPreparation.objects.filter(pk=self.preparation.pk).update(status="ready")
        self.assertEqual(state()["code"], "active_preparations")
