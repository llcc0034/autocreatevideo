"""New authored manual contract: an unknown POST must never be sent twice."""
from copy import deepcopy
from datetime import timedelta
from types import SimpleNamespace
from unittest.mock import Mock, patch

from django.test import TestCase, override_settings
from django.utils import timezone

from studio.models import (ExecutionPreparation, GeneratedArtifact, GenerationAttempt,
    GenerationTask, ProductionRun, PropBible, SeriesProject)
from studio.services.comfyui import ComfyUIHTTPError
from studio.services.execution_finalization import finalize_execution
from studio.services.execution_production_dispatch import dispatch_production_execution
from studio.services.production_runs import advance_production_run
from studio.services.production_stage_catalog import production_stage_catalog
from studio.services.task_queue import claim_next_task, process_task, recover_stale_tasks
from studio.tests.int_contract_fixtures import IntegrationContracts
from studio.tests import test_int_production_journey as journey
from studio.tests import test_int_mixed_journey as mixed
from studio.tests import test_int_three_entry_journey as entries
from studio.tests.test_execution_context_inputs import PROJECT_CONFIG
from studio.tests.test_execution_production_auxiliaries import production_observations
from studio.tests.test_execution_workflow_staging import RecordingUploadClient


@override_settings(AI_PROVIDER_ALLOWED_HOSTS=("127.0.0.1",))
class IntegrationUnknownJourneyTests(IntegrationContracts, TestCase):
    setUp = entries.IntegrationThreeEntryJourneyTests.setUp
    tearDown = journey.IntegrationProductionJourneyTests.tearDown
    collect = journey.IntegrationProductionJourneyTests.collect
    upload = mixed.IntegrationMixedJourneyTests.upload
    request = entries.IntegrationThreeEntryJourneyTests.request
    raw_inputs = entries.IntegrationThreeEntryJourneyTests.raw_inputs

    def test_new_contract_unknown_result_does_not_resend_or_replace_success(self):
        self.existing = self.upload("INT unknown governed reference")
        project = SeriesProject.objects.create(owner=self.actor, title="INT unknown manual", **PROJECT_CONFIG)
        catalog = production_stage_catalog()
        created = self.api("post", "/api/v1/production-runs/", {"project_id": str(project.pk), "plan_source": "manual",
            "idempotency_key": "int-unknown-root", "allow_external_calls": True, "config_overrides": {}, "manual_plan": {
                "stage_catalog_key": catalog.value["key"], "expected_stage_catalog_sha256": catalog.sha256,
                "scope": {"kind": "project"}, "delivery": {"kind": "episode_masters"}, "initial_content": None}}, status=201)
        run = ProductionRun.objects.get(pk=created["id"])
        url = f"/api/v1/production-runs/{run.pk}/"
        attempts = []
        for index in range(2):
            prop = PropBible.objects.create(project=project, slug=f"coat-{index}", name=f"外套{index}", description="A coat")
            unit = f"prop:{prop.pk}"
            body = {"action": "generate", "stage_key": "bible_image_generation", "unit_key": unit,
                "scope": {"kind": "prop", "id": str(prop.pk)}, "expected_run_plan_sha256": run.execution_plan.definition_sha256,
                "expected_selection_revision": None, "idempotency_key": f"int-unknown-{index}",
                "payload": self.request(self.variant, self.raw_inputs(unit))}
            prepared = self.api("post", url + "manual-actions/", body, status=202)
            prep = ExecutionPreparation.objects.get(pk=prepared["target_id"])
            prep = finalize_execution(preparation_id=prep.pk, expected_preparation_revision=prep.revision,
                actor=self.actor, collect_admission_facts=production_observations)
            attempts.append(dispatch_production_execution(preparation_id=prep.pk,
                expected_preparation_revision=prep.revision, actor=self.actor))
            task = claim_next_task()
            self.assertEqual(task.pk, attempts[-1].generation_task_id)
            if index == 0:
                self.collect(task)
        good, unknown = attempts
        good_fact = deepcopy(GeneratedArtifact.objects.values().get(generation_attempt=good))
        request = deepcopy(unknown.request_snapshot)
        peer = RecordingUploadClient()
        now = timezone.now().isoformat()
        peer.release_resources_before_submit = Mock(return_value={"schema_version": 1, "status": "succeeded",
            "policy_sha256": "d" * 64, "unload_models": True, "free_memory": True,
            "attempted_at": now, "completed_at": now, "error_code": "", "http_status": 200})
        peer.queue_prompt = Mock(side_effect=ComfyUIHTTPError(503, "Possible acceptance; reply lost"))
        with patch("studio.services.task_queue.preflight_comfyui_workflow", return_value=SimpleNamespace(
                as_dict=lambda: {"node_count": 8, "link_count": 7, "class_types": ["IsolatedPeer"], "schema_sha256": "c" * 64})):
            result = process_task(task, peer)
        self.assertEqual(result.status, "failed", result.error_message)
        self.assertEqual(result.submission_state, "prepared")
        unknown.refresh_from_db()
        self.assertEqual(unknown.error_code, "uncertain_remote_response")
        peer.queue_prompt.assert_called_once()
        deadline = result.hard_deadline_at
        task_ids = set(GenerationTask.objects.values_list("pk", flat=True))
        GenerationTask.objects.filter(pk=result.pk).update(heartbeat_at=timezone.now() - timedelta(minutes=10))
        peer.history, peer.is_queued = Mock(return_value=None), Mock(return_value=False)
        with patch("studio.services.task_queue.comfy_client_for_task", return_value=peer):
            for _ in range(2):
                recover_stale_tasks(older_than_minutes=1)
                run = advance_production_run(run)
        # Manual work must use its own formal action, not recipe retry APIs.
        self.api("get", url + "retry-preview/", {"expected_stage_id": str(unknown.stage_run_id)}, status=400)
        prep.refresh_from_db()
        tasks_before_retry = list(GenerationTask.objects.order_by("pk").values())
        self.api("post", url + "manual-actions/", {**body, "action": "retry", "idempotency_key": "int-unknown-retry",
            "payload": {"preparation_id": str(prep.pk), "expected_preparation_revision": prep.revision,
                "expected_attempt_id": str(unknown.pk)}}, status=400)
        self.assertEqual(list(GenerationTask.objects.order_by("pk").values()), tasks_before_retry)
        self.assertEqual(set(GenerationTask.objects.values_list("pk", flat=True)), task_ids)
        result.refresh_from_db()
        unknown.refresh_from_db()
        self.assertEqual(result.submission_token, str(unknown.pk))
        self.assertEqual(result.hard_deadline_at, deadline)
        self.assertEqual(unknown.request_snapshot, request)
        self.assertEqual(unknown.error_code, "uncertain_remote_response")
        self.assertEqual(GenerationAttempt.objects.count(), 2)
        self.assertFalse(GenerationAttempt.objects.filter(retry_of__isnull=False).exists())
        self.assertFalse(GeneratedArtifact.objects.filter(generation_attempt=unknown).exists())
        self.assertEqual(GeneratedArtifact.objects.values().get(generation_attempt=good), good_fact)
        self.assertIsNone(claim_next_task())
        peer.queue_prompt.assert_called_once()
