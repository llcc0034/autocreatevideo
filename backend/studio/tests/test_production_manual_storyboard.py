"""Adopting authored shots needs no fabricated script, model, or workflow."""
from copy import deepcopy
from unittest.mock import patch

from django.core.exceptions import ValidationError
from django.test import TestCase

from studio.models import ApprovalDecision, Asset, Episode, ExecutionPreparation, GenerationAttempt, ProductionManualCommand, ProductionRun, Shot
from studio.services.json_contracts import json_contract
from studio.tests import test_production_manual_creation as creation


class ProductionManualStoryboardTests(TestCase):
    create = creation.ProductionManualCreationTests.create

    def setUp(self):
        creation.ProductionManualCreationTests.setUp(self)
        self.episode = Episode.objects.create(project=self.project, number=1, title="手工分镜")
        self.outside = Episode.objects.create(project=self.project, number=2, title="不在本轮")
        self.shots = [Shot.objects.create(episode=self.episode, number=n, duration_ms=6000,
            media_type="image", include_audio=False) for n in (1, 2)]
        self.payload["manual_plan"]["scope"] = {"kind": "episodes", "episode_ids": [str(self.episode.pk)]}
        result = self.create()
        self.assertEqual(result.status_code, 201, result.data)
        self.run = ProductionRun.objects.get(pk=result.data["id"])
        self.url = f"/api/v1/production-runs/{self.run.pk}/manual-actions/"
        self.read_url = f"/api/v1/production-runs/{self.run.pk}/manual-storyboard/"
        self.body = {"action": "adopt", "stage_key": "storyboard", "unit_key": f"project:{self.project.pk}",
            "scope": {"kind": "project", "id": str(self.project.pk)}, "expected_run_plan_sha256": result.data["execution_plan"]["sha256"],
            "expected_selection_revision": 0, "idempotency_key": "adopt-storyboard", "payload": {}}

    def read(self):
        result = self.client.get(self.read_url)
        self.assertEqual(result.status_code, 200, result.data)
        self.assertEqual(result["Cache-Control"], "private, no-store")
        return result.data

    def request_body(self):
        data = self.read()
        return {**self.body, "expected_selection_revision": data["adoption"]["revision"], "payload": {
            "expected_content_sha256": data["content_sha256"], "source": {"kind": "existing_storyboard"},
            "reviewed_shot_ids": [str(shot.pk) for shot in self.shots], "comment": "已逐镜核对"}}

    def post(self, body):
        return self.client.post(self.url, body, format="json")

    def test_existing_content_is_explicitly_adopted_without_ai_or_auto_completion(self):
        before = self.read()
        self.assertIsNone(before["content"]["script"])
        self.assertEqual([row["id"] for row in before["content"]["episodes"]], [str(self.episode.pk)])
        self.assertFalse(ProductionManualCommand.objects.exists())
        body = self.request_body()
        first = self.post(body)
        self.assertEqual(first.status_code, 201, first.data)
        self.assertEqual(self.post(body).data, first.data)
        self.assertEqual(self.read()["adoption"]["status"], "current")
        self.run.refresh_from_db()
        self.assertEqual(self.run.status, "awaiting_input")
        self.assertIsNone(self.run.script_revision_id)
        self.assertFalse(self.run.stages.exclude(stage_key="storyboard").exclude(status="pending").exists())
        self.assertFalse(GenerationAttempt.objects.exists())
        self.assertFalse(ExecutionPreparation.objects.exists())
        self.assertEqual((Episode.objects.count(), Shot.objects.count(), ApprovalDecision.objects.count()), (2, 2, 1))
        self.shots[0].title = "明确更改"
        self.shots[0].save(update_fields=("title",))
        self.assertEqual(self.read()["adoption"]["status"], "changed")

    def test_partial_stale_and_downstream_adoption_do_not_create_a_decision(self):
        body = self.request_body()
        for ids in ([], [str(self.shots[0].pk)], [str(self.shots[0].pk)] * 2):
            bad = deepcopy(body); bad["payload"]["reviewed_shot_ids"] = ids
            self.assertEqual(self.post(bad).status_code, 400)
        self.shots[1].requires_last_frame_continuity = True
        self.shots[1].save(update_fields=("requires_last_frame_continuity",))
        self.assertEqual(self.post(body).status_code, 409)
        body = self.request_body()
        self.run.stages.filter(stage_key="media_generation").update(status="running")
        self.assertEqual(self.post(body).status_code, 400)
        self.assertFalse(ApprovalDecision.objects.exists())
        self.assertFalse(ProductionManualCommand.objects.exists())

    def test_reference_review_does_not_invalidate_creative_adoption_but_new_shot_does(self):
        body = self.request_body()
        self.assertEqual(self.post(body).status_code, 201)
        before = self.read()
        self.assertEqual(before["content"]["schema_version"], 3)
        stage = self.run.stages.get(stage_key="storyboard")
        self.assertEqual(stage.output_snapshot_schema_version, 5)
        original_snapshot = deepcopy(stage.output_snapshot)
        asset = Asset.objects.create(owner=self.project.owner, kind="reference", name="隔离测试参考素材")
        self.shots[0].metadata = {**self.shots[0].metadata, "referenceBindingConfigured": True,
            "reference_bindings": {"reference_image": {"asset_id": str(asset.pk)}}}
        self.shots[0].save(update_fields=("metadata",))
        after = self.read()
        self.assertEqual(after["adoption"]["status"], "current")
        self.assertEqual(after["content_sha256"], before["content_sha256"])
        shot = after["content"]["episodes"][0]["shots"][0]
        self.assertNotIn("reference_bindings", shot)
        self.assertNotIn("reference_asset_ids", shot)
        stage.refresh_from_db()
        self.assertEqual(stage.output_snapshot, original_snapshot)
        Shot.objects.create(episode=self.episode, number=3, duration_ms=6000,
            media_type="image", include_audio=False)
        self.assertEqual(self.read()["adoption"]["status"], "changed")

    def test_old_mixed_adoption_is_rejected_not_reinterpreted(self):
        self.assertEqual(self.post(self.request_body()).status_code, 201)
        stage = self.run.stages.get(stage_key="storyboard")
        value = deepcopy(stage.output_snapshot)
        value["schema_version"] = 3
        saved = json_contract(value)
        stage.output_snapshot = saved.value
        stage.output_snapshot_schema_version = saved.schema_version
        stage.output_snapshot_size_bytes = saved.size_bytes
        stage.output_snapshot_sha256 = saved.sha256
        stage.save(update_fields=("output_snapshot", "output_snapshot_schema_version", "output_snapshot_size_bytes", "output_snapshot_sha256"))
        stage.approval_decisions.update(output_snapshot_sha256=saved.sha256)
        response = self.client.get(self.read_url)
        self.assertEqual(response.status_code, 400)
        self.assertIn("分镜采用决策与正式内容快照不一致", str(response.data))

    def test_receipt_failure_rolls_back_and_empty_or_missing_scope_is_explicit(self):
        body = self.request_body()
        with patch("studio.services.production_manual_commands.ProductionManualCommand.save", side_effect=ValidationError("receipt failed")):
            self.assertEqual(self.post(body).status_code, 400)
        self.assertEqual(self.run.stages.get(stage_key="storyboard").status, "pending")
        self.assertFalse(ApprovalDecision.objects.exists())
        self.episode.shots.all().delete()
        self.assertEqual(self.post(self.request_body()).status_code, 400)
        self.episode.delete()
        self.assertEqual(self.client.get(self.read_url).status_code, 400)

    def test_ownership_and_exact_whole_scope_cannot_be_bypassed(self):
        self.assertEqual(self.client.get(self.read_url, {"episode_id": str(self.outside.pk)}).status_code, 400)
        body = self.request_body()
        self.client.force_authenticate(self.other)
        self.assertEqual(self.client.get(self.read_url).status_code, 404)
        self.assertEqual(self.post(body).status_code, 404)
