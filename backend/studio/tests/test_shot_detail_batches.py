from __future__ import annotations

import uuid
from contextlib import nullcontext
from types import SimpleNamespace
from unittest.mock import patch

from django.test import SimpleTestCase, TestCase, override_settings
from rest_framework.test import APIClient

from studio.models import Shot, ShotDetailDraft, ShotDetailDraftBatch, ShotDetailDraftBatchItem
from studio.services.creative_drafts import CreativeDraftError
from studio.services.shot_detail_batches import (
    _finish_item,
    _provider_queue_available,
    claim_next_shot_detail_item,
    create_shot_detail_batch,
    process_shot_detail_item,
)
from studio.services.shot_detail_drafts import shot_detail_context_hash
from studio.tests.factories import build_story
from studio.management.commands.run_generation_worker import Command


TEST_COMMON_POLICY = {
    "recommended_zh_chars": 500,
    "recommended_en_chars": 750,
    "transport_zh_max_chars": 3000,
    "transport_en_max_chars": 4500,
    "output_tokens": 2304,
    "context_max_chars": 18000,
    "review_min_zh_chars": 100,
    "review_min_en_chars": 160,
    "review_near_zh_margin_chars": 20,
    "review_near_en_margin_chars": 30,
    "max_dialogue_per_shot": 2,
    "maximum_generation_number": 1000000,
    "minimum_duration_ms": 5000,
    "maximum_duration_ms": 15000,
    "max_attempts": 1,
    "retry_seconds": 0,
    "stale_minutes": 30,
}
TEST_PROVIDER_POLICIES = {
    "local_qwen": {
        "recommended_max_shots": 1,
        "output_tokens": 2304,
        "context_max_chars": 18000,
    },
    "deepseek_api": {
        "recommended_max_shots": 16,
        "output_tokens": 2304,
        "context_max_chars": 18000,
    },
}


class ShotDetailGenerationWorkerPriorityTests(SimpleTestCase):
    def test_comfy_task_is_claimed_before_shot_detail_item(self):
        task = SimpleNamespace(id="comfy-1", attempts=1, max_attempts=3)
        result = SimpleNamespace(id="comfy-1", status="succeeded")
        options = {
            "startup_recover_minutes": None,
            "recover_minutes": 30,
            "recover_interval": 60,
            "wait_timeout": 60,
            "poll": 0.1,
            "once": True,
        }
        with patch(
            "studio.management.commands.run_generation_worker.ComfyUIClient",
        ), patch(
            "studio.management.commands.run_generation_worker.recover_stale_tasks",
            return_value=0,
        ), patch(
            "studio.management.commands.run_generation_worker.recover_stale_shot_detail_items",
            return_value=0,
        ), patch(
            "studio.management.commands.run_generation_worker.local_qwen_runtime_lock",
            return_value=nullcontext(),
        ), patch(
            "studio.management.commands.run_generation_worker.claim_next_task",
            return_value=task,
        ), patch(
            "studio.management.commands.run_generation_worker.process_task",
            return_value=result,
        ) as process_task_mock, patch(
            "studio.management.commands.run_generation_worker.claim_next_shot_detail_item",
        ) as detail_claim:
            Command()._run_worker(options)

        process_task_mock.assert_called_once()
        detail_claim.assert_not_called()


@override_settings(
    SHOT_DETAIL_POLICY=TEST_COMMON_POLICY,
    SHOT_DETAIL_PROVIDER_POLICIES=TEST_PROVIDER_POLICIES,
)
class ShotDetailBatchTests(TestCase):
    def setUp(self):
        (
            self.user, self.project, self.character, self.scene, self.episode,
            self.shot, self.next_shot,
        ) = build_story()
        self.client = APIClient()
        self.client.force_authenticate(self.user)

    @staticmethod
    def candidate():
        return {
            "schema_version": 1,
            "detail_zh": (
                "镜头从雨水滑落的窗面缓慢推向人物，人物保持原有站位与服装，"
                "右手压住烧焦信纸，视线从纸角移向门口阴影。冷色环境光沿桌沿移动，"
                "动作在收紧手指与稳定呼吸时结束，不新增人物、道具或剧情事实。"
            ),
            "detail_en": (
                "The camera slowly pushes from rain sliding across the window toward the subject, "
                "preserving the established position and wardrobe. The right hand holds the scorched "
                "letter against the desk while the gaze shifts from its corner to the doorway shadow. "
                "Cool ambient light travels along the desk edge, and the action ends as the fingers "
                "tighten and the breathing steadies, without adding a new person, prop, or story event."
            ),
        }

    def _create_batch(self):
        with patch(
            "studio.services.shot_detail_batches._provider_queue_available",
            return_value=True,
        ):
            return create_shot_detail_batch(
                project=self.project,
                episode=self.episode,
                user=self.user,
                request_id=uuid.uuid4(),
                provider="local_qwen",
                confirmation_acknowledged=True,
            )[0]

    def _process_success(self, item):
        def runtime(request, _config):
            return {
                "result": self.candidate(),
                "source_fingerprint": request["source_fingerprint"],
                "runtime": {"model": "Qwen/Qwen3-4B-GGUF"},
            }

        with patch(
            "studio.services.shot_detail_drafts.ensure_creative_resources_available",
        ), patch(
            "studio.services.shot_detail_drafts._require_runtime",
        ), patch(
            "studio.services.shot_detail_drafts._run_runtime", side_effect=runtime,
        ):
            return process_shot_detail_item(item)

    def test_api_recommended_count_requires_confirmation_but_never_caps_business_volume(self):
        request_id = uuid.uuid4()
        url = f"/api/v1/projects/{self.project.pk}/shot-detail-draft-batches/"
        payload = {
            "episode_id": str(self.episode.pk),
            "request_id": str(request_id),
            "provider": "local_qwen",
        }
        with patch(
            "studio.services.shot_detail_batches._provider_queue_available",
            return_value=True,
        ):
            warning = self.client.post(url, payload, format="json")
            self.assertEqual(warning.status_code, 409, warning.data)
            self.assertEqual(
                warning.data["code"], "shot_detail_batch_confirmation_required",
            )
            self.assertFalse(warning.data["confirmation"]["business_volume_limited"])

            payload["confirmation_acknowledged"] = True
            created = self.client.post(url, payload, format="json")
            self.assertEqual(created.status_code, 202, created.data)
            repeated = self.client.post(url, payload, format="json")
            self.assertEqual(repeated.status_code, 200, repeated.data)
        self.assertEqual(created.data["id"], repeated.data["id"])
        self.assertEqual(created.data["total_count"], 2)
        self.assertEqual(created.data["pending_count"], 2)

    def test_same_request_id_replays_after_server_policy_changes(self):
        request_id = uuid.uuid4()
        with patch(
            "studio.services.shot_detail_batches._provider_queue_available",
            return_value=True,
        ):
            original, created = create_shot_detail_batch(
                project=self.project,
                episode=self.episode,
                user=self.user,
                request_id=request_id,
                provider="local_qwen",
                variation_note="preserve the rain",
                confirmation_acknowledged=True,
            )
        self.assertTrue(created)
        changed_provider_policies = {
            **TEST_PROVIDER_POLICIES,
            "local_qwen": {
                **TEST_PROVIDER_POLICIES["local_qwen"],
                "recommended_max_shots": 20,
                "output_tokens": 1024,
                "context_max_chars": 9000,
            },
        }
        with override_settings(
            SHOT_DETAIL_PROVIDER_POLICIES=changed_provider_policies,
        ):
            replayed, replay_created = create_shot_detail_batch(
                project=self.project,
                episode=self.episode,
                user=self.user,
                request_id=request_id,
                provider="local_qwen",
                variation_note="preserve the rain",
                confirmation_acknowledged=True,
            )

        self.assertFalse(replay_created)
        self.assertEqual(replayed.pk, original.pk)
        self.assertEqual(original.policy_snapshot["output_tokens"], 2304)

    def test_worker_executes_with_frozen_batch_policy(self):
        batch = self._create_batch()
        item = claim_next_shot_detail_item("test-worker")
        self.assertIsNotNone(item)
        frozen_policy = dict(batch.policy_snapshot)
        captured = {}

        def current_hash(_project, _episode, _shot, *, policy=None):
            captured["context_policy"] = policy
            return item.source_context_hash

        def generate(**kwargs):
            captured["generation_policy"] = kwargs["policy_override"]
            from studio.services.creative_drafts import CreativeResourceBusy

            raise CreativeResourceBusy("busy", code="shared_resource_busy")

        changed_provider_policies = {
            **TEST_PROVIDER_POLICIES,
            "local_qwen": {
                **TEST_PROVIDER_POLICIES["local_qwen"],
                "output_tokens": 1024,
                "context_max_chars": 9000,
            },
        }
        with override_settings(
            SHOT_DETAIL_PROVIDER_POLICIES=changed_provider_policies,
        ), patch(
            "studio.services.shot_detail_batches.shot_detail_context_hash",
            side_effect=current_hash,
        ), patch(
            "studio.services.shot_detail_batches.create_shot_detail_draft",
            side_effect=generate,
        ):
            result = process_shot_detail_item(item)

        self.assertEqual(result.status, ShotDetailDraftBatchItem.Status.PENDING)
        self.assertEqual(captured["context_policy"], frozen_policy)
        self.assertEqual(captured["generation_policy"], frozen_policy)
        self.assertEqual(captured["generation_policy"]["output_tokens"], 2304)

    def test_terminal_item_and_batch_summary_rollback_together(self):
        batch = self._create_batch()
        item = claim_next_shot_detail_item("test-worker")
        self.assertIsNotNone(item)
        claim_token_before = item.claim_token
        batch.refresh_from_db()
        revision_before = batch.revision

        with patch(
            "studio.services.shot_detail_batches._refresh_locked_shot_detail_batch",
            side_effect=RuntimeError("simulated summary failure"),
        ), self.assertRaisesRegex(RuntimeError, "simulated summary failure"):
            _finish_item(
                item.pk,
                status=ShotDetailDraftBatchItem.Status.SUCCEEDED,
                expected_claim_token=item.claim_token,
            )

        item.refresh_from_db()
        batch.refresh_from_db()
        self.assertEqual(item.status, ShotDetailDraftBatchItem.Status.RUNNING)
        self.assertEqual(item.claim_token, claim_token_before)
        self.assertEqual(batch.generation_status, ShotDetailDraftBatch.GenerationStatus.RUNNING)
        self.assertEqual(batch.succeeded_count, 0)
        self.assertEqual(batch.revision, revision_before)

    def test_transient_local_resource_busy_is_queueable_not_misconfigured(self):
        with patch(
            "studio.services.shot_detail_batches.get_creative_capability",
            return_value={"providers": [{
                "id": "local_qwen",
                "enabled": True,
                "configured": False,
                "ready": False,
                "model_verified": True,
                "runtime_ready": False,
                "runtime_probe_error_code": "shared_resource_busy",
            }]},
        ):
            self.assertTrue(_provider_queue_available("local_qwen"))

        batch = self._create_batch()
        item = claim_next_shot_detail_item("test-worker")
        from studio.services.creative_drafts import CreativeResourceBusy

        with patch(
            "studio.services.shot_detail_batches.create_shot_detail_draft",
            side_effect=CreativeResourceBusy(
                "busy", code="shared_resource_busy",
            ),
        ):
            result = process_shot_detail_item(item)
        self.assertEqual(result.status, ShotDetailDraftBatchItem.Status.PENDING)
        self.assertEqual(result.attempts, 0)
        self.assertEqual(result.error_code, "shared_resource_busy")
        batch.refresh_from_db()
        self.assertEqual(batch.failed_count, 0)

    def test_worker_preserves_success_and_terminal_failure_per_shot(self):
        batch = self._create_batch()
        first = claim_next_shot_detail_item("test-worker")
        self.assertIsNotNone(first)
        self._process_success(first)
        second = claim_next_shot_detail_item("test-worker")
        self.assertIsNotNone(second)
        with patch(
            "studio.services.shot_detail_drafts.ensure_creative_resources_available",
        ), patch(
            "studio.services.shot_detail_drafts._require_runtime",
        ), patch(
            "studio.services.shot_detail_drafts._run_runtime",
            side_effect=CreativeDraftError("provider failed", code="provider_failed"),
        ):
            process_shot_detail_item(second)

        batch.refresh_from_db()
        self.assertEqual(
            batch.generation_status,
            ShotDetailDraftBatch.GenerationStatus.COMPLETED_WITH_ERRORS,
        )
        self.assertEqual(batch.succeeded_count, 1)
        self.assertEqual(batch.failed_count, 1)
        items = list(batch.items.order_by("ordinal"))
        self.assertEqual(items[0].status, ShotDetailDraftBatchItem.Status.SUCCEEDED)
        self.assertEqual(items[1].status, ShotDetailDraftBatchItem.Status.FAILED)
        self.assertIsNotNone(items[0].draft_id)
        self.assertIsNotNone(items[1].draft_id)

    def test_batch_apply_preflights_all_then_atomically_applies_draft_candidates(self):
        batch = self._create_batch()
        while True:
            item = claim_next_shot_detail_item("test-worker")
            if item is None:
                break
            self._process_success(item)
        batch.refresh_from_db()
        items = list(batch.items.select_related("draft").order_by("ordinal"))
        payload = {
            "expected_batch_revision": batch.revision,
            "items": [{
                "item_id": str(item.pk),
                "draft_id": str(item.draft_id),
                "expected_revision": item.draft.revision,
            } for item in items],
            "language_mode": "bilingual_confirmed",
            "english_review_acknowledged": True,
            "review_acknowledged": True,
        }
        response = self.client.post(
            f"/api/v1/shot-detail-draft-batches/{batch.pk}/apply/",
            payload,
            format="json",
        )
        self.assertEqual(response.status_code, 200, response.data)
        self.assertEqual(response.data["batch"]["apply_status"], "complete")
        self.assertTrue(response.data["translation_written"])
        self.assertEqual(len(response.data["applied_shots"]), len(items))
        self.assertEqual(
            {row["detailed_description"] for row in response.data["applied_shots"]},
            {self.candidate()["detail_en"]},
        )
        for item in items:
            item.draft.refresh_from_db()
            item.shot.refresh_from_db()
            self.assertEqual(item.draft.status, ShotDetailDraft.Status.APPLIED)
            self.assertIsNotNone(item.draft.confirmed_at)
            self.assertEqual(item.shot.detailed_description, self.candidate()["detail_en"])
            self.assertEqual(
                item.draft.context_hash,
                shot_detail_context_hash(self.project, self.episode, item.shot),
            )

    def test_batch_detail_recovers_committed_apply_result_after_response_loss(self):
        batch = self._create_batch()
        while True:
            item = claim_next_shot_detail_item("test-worker")
            if item is None:
                break
            self._process_success(item)
        batch.refresh_from_db()
        items = list(batch.items.select_related("draft").order_by("ordinal"))
        applied = self.client.post(
            f"/api/v1/shot-detail-draft-batches/{batch.pk}/apply/",
            {
                "expected_batch_revision": batch.revision,
                "items": [{
                    "item_id": str(item.pk),
                    "draft_id": str(item.draft_id),
                    "expected_revision": item.draft.revision,
                } for item in items],
                "language_mode": "bilingual_confirmed",
                "english_review_acknowledged": True,
                "review_acknowledged": True,
            },
            format="json",
        )
        self.assertEqual(applied.status_code, 200, applied.data)

        recovered = self.client.get(
            f"/api/v1/shot-detail-draft-batches/{batch.pk}/",
        )
        self.assertEqual(recovered.status_code, 200, recovered.data)
        self.assertEqual(recovered.data["batch"]["apply_status"], "complete")
        self.assertTrue(recovered.data["translation_written"])
        self.assertEqual(
            {row["id"] for row in recovered.data["applied_shots"]},
            {str(item.shot_id) for item in items},
        )
        self.assertTrue(all(row["applied_at"] for row in recovered.data["items"]))

    def test_batch_apply_revision_failure_writes_no_shot(self):
        batch = self._create_batch()
        while True:
            item = claim_next_shot_detail_item("test-worker")
            if item is None:
                break
            self._process_success(item)
        batch.refresh_from_db()
        items = list(batch.items.select_related("draft").order_by("ordinal"))
        before = {
            item.shot_id: Shot.objects.get(pk=item.shot_id).detailed_description
            for item in items
        }
        submitted = [{
            "item_id": str(item.pk),
            "draft_id": str(item.draft_id),
            "expected_revision": item.draft.revision,
        } for item in items]
        submitted[-1]["expected_revision"] += 1
        response = self.client.post(
            f"/api/v1/shot-detail-draft-batches/{batch.pk}/apply/",
            {
                "expected_batch_revision": batch.revision,
                "items": submitted,
                "language_mode": "chinese_only",
                "review_acknowledged": True,
            },
            format="json",
        )
        self.assertEqual(response.status_code, 409, response.data)
        for shot_id, original in before.items():
            self.assertEqual(
                Shot.objects.get(pk=shot_id).detailed_description, original,
            )
        self.assertFalse(ShotDetailDraft.objects.filter(
            batch_item__batch=batch, status=ShotDetailDraft.Status.APPLIED,
        ).exists())
