"""INT-03 failure recovery on new authored contracts, never a live model run."""
from copy import deepcopy
from decimal import Decimal
from types import SimpleNamespace
from unittest.mock import Mock, patch

from django.test import TestCase, override_settings
from django.utils import timezone

from studio.models import ExecutionPreparation, GeneratedArtifact, GenerationAttempt, GenerationTask, ReleaseBundle
from studio.services.assembly_queue import claim_next_assembly, process_assembly_job
from studio.services.comfyui import ComfyUIHTTPError
from studio.services.task_queue import claim_next_task, process_task
from studio.tests.int_contract_fixtures import IntegrationContracts
from studio.tests import test_int_mixed_journey as mixed
from studio.tests import test_int_production_journey as journey
from studio.tests.test_execution_workflow_staging import RecordingUploadClient
from studio.tests.test_execution_production_auxiliaries import production_observations


@override_settings(AI_PROVIDER_ALLOWED_HOSTS=("127.0.0.1",))
class IntegrationExceptionJourneyTests(IntegrationContracts, TestCase):
    def setUp(self):
        journey.IntegrationProductionJourneyTests.setUp(self)
        def observations(preparation, definition, request, *, retry_of=None):
            if retry_of is not None:
                self.assertEqual(retry_of.preparation_id, preparation.pk)
                self.assertEqual(retry_of.request_sha256, preparation.request_sha256)
            return production_observations(preparation, definition, request)
        self.enterContext(patch("studio.services.execution_production_dispatch.collect_production_execution_admission",
            side_effect=observations))
    tearDown = journey.IntegrationProductionJourneyTests.tearDown
    advance = journey.IntegrationProductionJourneyTests.advance
    state = journey.IntegrationProductionJourneyTests.state
    collect = journey.IntegrationProductionJourneyTests.collect
    assert_media_lineage_and_delivery = journey.IntegrationProductionJourneyTests.assert_media_lineage_and_delivery
    approve = mixed.IntegrationMixedJourneyTests.approve
    upload = mixed.IntegrationMixedJourneyTests.upload
    fulfill_primaries = mixed.IntegrationMixedJourneyTests.fulfill_primaries

    def reject(self, task):
        peer = RecordingUploadClient()
        now = timezone.now().isoformat()
        peer.release_resources_before_submit = Mock(return_value={"schema_version": 1, "status": "succeeded",
            "policy_sha256": "d" * 64, "unload_models": True, "free_memory": True,
            "attempted_at": now, "completed_at": now, "error_code": "", "http_status": 200})
        peer.queue_prompt = Mock(side_effect=ComfyUIHTTPError(400, "INT explicit isolated rejection"))
        with patch("studio.services.task_queue.preflight_comfyui_workflow", return_value=SimpleNamespace(
                as_dict=lambda: {"node_count": 8, "link_count": 7, "class_types": ["IsolatedPeer"], "schema_sha256": "c" * 64})):
            result = process_task(task, peer)
        self.assertEqual(result.status, "failed", result.error_message)
        self.assertEqual(result.submission_state, "terminal_failed")
        peer.queue_prompt.assert_called_once()
        failed = GenerationAttempt.objects.get(generation_task=task)
        self.assertEqual(failed.error_code, "execution_submission_rejected")
        self.assertFalse(GeneratedArtifact.objects.filter(generation_attempt=failed).exists())
        return failed, peer

    def test_failed_second_shot_retries_only_it_and_delivers_with_original_success(self):
        self.existing = self.upload("INT recovery existing primary")
        self.sample = self.int_sample(mode="mixed")
        self.url = f"/api/v1/production-runs/{self.sample.run.pk}/"
        self.advance()
        self.approve("bible_review")
        self.fulfill_primaries()
        failed = success = None
        frozen = None
        retry_body = None
        # Record queue_prompt even on failure; successful-output count is not
        # a substitute for the number of actual external-boundary submissions.
        success_peers = []
        def new_peer():
            peer = RecordingUploadClient()
            success_peers.append(peer)
            return peer
        with patch.object(journey, "RecordingUploadClient", side_effect=new_peer):
            for _ in range(30):
                run = self.advance()
                if run.status == "succeeded":
                    break
                if run.status == "awaiting_approval":
                    self.approve(run.current_stage_key)
                    continue
                if run.status in {"failed", "blocked"}:
                    self.assertIsNotNone(failed, self.state())
                    self.assertIsNone(retry_body, self.state())
                    page = self.api("get", self.url + "retry-preview/", {"expected_stage_id": str(failed.stage_run_id)})
                    self.assertFalse(page["can_submit"])
                    self.assertIsNone(failed.actual_cost)
                    # This isolated peer has no billing API. First prove that
                    # unknown cost is not silently fabricated, then supply an
                    # explicit test-only settled-cost dependency (not live
                    # reconciliation) before exercising failed-only retry.
                    GenerationAttempt.objects.filter(pk=failed.pk).update(actual_cost=Decimal("0"))
                    page = self.api("get", self.url + "retry-preview/", {"expected_stage_id": str(failed.stage_run_id)})
                    disposition = {row["attempt_id"]: row["disposition"] for row in page["results"]}
                    self.assertEqual(disposition[str(failed.pk)], "retry", page)
                    self.assertEqual(disposition[str(success.pk)], "skip")
                    retry_body = page["retry_context"]
                    self.assertEqual(retry_body["expected_attempt_ids"], [str(failed.pk)])
                    for _ in range(2):
                        self.api("post", self.url + "retry/", retry_body)
                    successor = GenerationAttempt.objects.get(retry_of=failed)
                    self.assertEqual(successor.preparation_id, failed.preparation_id)
                    self.assertEqual(successor.request_sha256, failed.request_sha256)
                    self.assertEqual(successor.request_snapshot, frozen)
                    self.assertFalse(GenerationAttempt.objects.filter(retry_of=success).exists())
                    continue
                task = claim_next_task()
                if task is not None:
                    attempt = GenerationAttempt.objects.get(generation_task=task)
                    if attempt.stage_run.stage_key == "media_generation" and success is not None and failed is None:
                        frozen = deepcopy(attempt.request_snapshot)
                        failed, rejected_peer = self.reject(task)
                    else:
                        self.collect(task)
                        if attempt.stage_run.stage_key == "media_generation" and success is None:
                            success = attempt
                            success_fact = deepcopy(GeneratedArtifact.objects.values().get(generation_attempt=success))
                        elif attempt.retry_of_id is not None:
                            # Before the later explicit artifact-selection
                            # stage, retry must leave the successful row whole.
                            self.assertEqual(GeneratedArtifact.objects.values().get(generation_attempt=success), success_fact)
                    continue
                assembly = claim_next_assembly(worker_id="int-exception-isolated")
                if assembly is not None:
                    with self.captureOnCommitCallbacks(execute=True):
                        result = process_assembly_job(assembly)
                    self.assertEqual(result.status, "succeeded", result.error_message)
                    continue
                self.fail(self.state())
            else:
                self.fail(self.state())
        self.assertIsNotNone(retry_body)
        successor = GenerationAttempt.objects.get(retry_of=failed)
        self.assertEqual(successor.status, "succeeded")
        self.assertEqual(successor.request_snapshot, frozen)
        selected_success = GeneratedArtifact.objects.values().get(generation_attempt=success)
        self.assertEqual(selected_success["status"], "selected")
        self.assertEqual({key: value for key, value in selected_success.items() if key not in {"updated_at", "status"}},
            {key: value for key, value in success_fact.items() if key not in {"updated_at", "status"}})
        self.assertEqual(GenerationAttempt.objects.filter(preparation=success.preparation).count(), 1)
        self.assertEqual(GenerationAttempt.objects.filter(stage_run__production_run=run).count(), 5)
        self.assertEqual(sum(peer.queue_prompt.call_count for peer in success_peers), 4)
        self.assertEqual(rejected_peer.queue_prompt.call_count, 1)
        bundle = ReleaseBundle.objects.get(production_run=run)
        def facts():
            return {model.__name__: deepcopy(list(model.objects.order_by("pk").values())) for model in (
                ExecutionPreparation, GenerationAttempt, GenerationTask, GeneratedArtifact, ReleaseBundle)}
        before_reads = facts()
        verified = self.api("get", f"/api/v1/release-bundles/{bundle.pk}/verify/")
        self.assertEqual(verified["verification"]["status"], "verified")
        self.assertEqual({row["generated_artifact_id"] for row in verified["artifacts"]}, {
            str(GeneratedArtifact.objects.get(generation_attempt=success).pk),
            str(GeneratedArtifact.objects.get(generation_attempt=successor).pk)})
        self.assert_media_lineage_and_delivery(run)
        self.assertEqual(facts(), before_reads)
        # Once delivery has advanced the Run, the old-stage request must be
        # refused, not mistaken for a fresh retry or a current-stage replay.
        self.api("post", self.url + "retry/", retry_body, status=400)
        self.advance()
        self.assertEqual(facts(), before_reads)
        self.assertIsNone(claim_next_task())
        self.assertEqual(sum(peer.queue_prompt.call_count for peer in success_peers) + rejected_peer.queue_prompt.call_count, 5)
