"""Actual prepared tasks, typed check records and claim/owner isolation."""
from copy import deepcopy
from unittest.mock import Mock, patch

from django.contrib.auth import get_user_model
from django.core.exceptions import ValidationError
from django.db import connection
from django.test import TestCase, override_settings
from django.test.utils import CaptureQueriesContext
from django.utils import timezone
from rest_framework.test import APIClient

from studio.models import GenerationTask, GenerationTaskObservation
from studio.services.comfyui import ComfyUIPreSubmitResourceReleaseError
from studio.services.execution_task_observations import record_task_observation
from studio.services.task_queue import _release_resources_for_new_submission, TaskClaimLost
from studio.tests import test_execution_production_workflow_queue as fixtures


@override_settings(AI_PROVIDER_ALLOWED_HOSTS=("127.0.0.1",))
class ExecutionTaskObservationTests(TestCase):
    _create_version = fixtures.ProductionWorkflowQueueTests._create_version
    resolve = fixtures.ProductionWorkflowQueueTests.resolve
    setUp = fixtures.ProductionWorkflowQueueTests.setUp

    def release_report(self, **changes):
        now = timezone.now().isoformat()
        return {"schema_version": 1, "status": "succeeded", "policy_sha256": "a" * 64,
            "unload_models": True, "free_memory": False, "attempted_at": now, "completed_at": now,
            "error_code": "", "http_status": 200, **changes}

    def record_preflight(self, **changes):
        return record_task_observation(self.task, kind="preflight", started_at=timezone.now(),
            report={"node_count": 3, "link_count": 2, "class_types": ["ControlledPeer"], "schema_sha256": "b" * 64, **changes})

    def test_checks_preserve_frozen_inputs_and_do_not_reintroduce_task_snapshot(self):
        frozen = deepcopy(self.preparation.contract.prepared_request)
        self.assertNotIn("capability_snapshot", {field.name for field in GenerationTask._meta.fields})
        self.assertTrue(self.record_preflight())
        self.assertTrue(record_task_observation(self.task, kind="resource_release", report=self.release_report()))
        first, second = list(self.task.observations.order_by("created_at"))
        self.assertEqual((first.kind, second.kind), ("preflight", "resource_release"))
        self.assertEqual(first.schema_sha256, "b" * 64)
        self.assertEqual(second.policy_sha256, "a" * 64)
        self.assertFalse(second.free_memory)
        self.assertIsNone(second.node_count)
        self.assertEqual(first.request_sha256, self.preparation.request_sha256)
        self.assertEqual(first.claim_number, self.task.attempts)
        self.task.refresh_from_db()
        self.preparation.contract.refresh_from_db()
        self.assertEqual(self.preparation.contract.prepared_request, frozen)
        self.assertFalse(self.task.submission_token)

    def test_failed_release_is_recorded_and_propagated_without_a_prompt_post(self):
        report = self.release_report(status="failed", error_code="comfyui_resource_release_unconfirmed", http_status=None)
        peer = Mock(spec=["release_resources_before_submit", "queue_prompt"])
        peer.release_resources_before_submit.side_effect = ComfyUIPreSubmitResourceReleaseError(
            report["error_code"], "controlled release failure", audit=report)
        with self.assertRaises(ComfyUIPreSubmitResourceReleaseError):
            _release_resources_for_new_submission(self.task, peer)
        observation = self.task.observations.get()
        self.assertEqual(observation.status, "failed")
        self.assertEqual(observation.error_code, report["error_code"])
        peer.queue_prompt.assert_not_called()
        self.assertFalse(self.task.submission_token)

    def test_original_claim_cancellation_preserves_observation_but_reclaimed_worker_cannot_write(self):
        GenerationTask.objects.filter(pk=self.task.pk).update(status="cancel_requested")
        self.assertTrue(self.record_preflight())  # Check completed; not authority to submit.
        GenerationTask.objects.filter(pk=self.task.pk).update(status="running", attempts=self.task.attempts + 1)
        self.assertFalse(self.record_preflight())
        self.assertEqual(self.task.observations.count(), 1)
        peer = Mock(spec=["release_resources_before_submit"])
        with self.assertRaises(TaskClaimLost):
            _release_resources_for_new_submission(self.task, peer)
        peer.release_resources_before_submit.assert_not_called()

    def test_late_release_and_failed_insert_never_become_success_or_mutate_preparation(self):
        peer = Mock(spec=["release_resources_before_submit"])
        def late_release():
            GenerationTask.objects.filter(pk=self.task.pk).update(attempts=self.task.attempts + 1)
            return self.release_report()
        peer.release_resources_before_submit.side_effect = late_release
        with self.assertRaises(TaskClaimLost):
            _release_resources_for_new_submission(self.task, peer)
        self.assertFalse(self.task.observations.exists())
        GenerationTask.objects.filter(pk=self.task.pk).update(attempts=self.task.attempts)
        with patch.object(GenerationTaskObservation, "save", side_effect=RuntimeError("database refused")), self.assertRaises(RuntimeError):
            self.record_preflight()
        self.assertFalse(self.task.observations.exists())

    def test_malformed_checks_fail_without_generic_payload_or_sensitive_text_storage(self):
        for changes in ({"schema_sha256": ""}, {"node_count": True}, {"node_count": -1}, {"prompt": "sensitive"}):
            with self.subTest(changes=changes), self.assertRaises(ValidationError):
                self.record_preflight(**changes)
        for changes in ({"http_status": True}, {"policy_sha256": ""}, {"completed_at": "invalid"},
                {"unload_models": 1}, {"status": "succeeded", "http_status": 500}, {"password": "sensitive"}):
            with self.subTest(changes=changes), self.assertRaises(ValidationError):
                record_task_observation(self.task, kind="resource_release", report=self.release_report(**changes))
        self.assertFalse(self.task.observations.exists())

    def test_existing_submission_reconciliation_never_repeats_preflight_or_resource_release(self):
        from studio.tests.test_execution_production_workflow_lifecycle import ProductionWorkflowLifecycleTests
        from studio.services.task_queue import process_task
        peer = ProductionWorkflowLifecycleTests.submitted(self)
        peer.release_resources_before_submit = Mock(side_effect=AssertionError("No new global release"))
        peer.history.return_value = {"status": {"status_str": "error", "completed": False}}
        with patch("studio.services.task_queue.preflight_comfyui_workflow", side_effect=AssertionError("No new preflight")), \
                patch("studio.services.task_queue.schedule_generation_task_run_advance"):
            result = process_task(self.task, peer)
        self.assertEqual(result.submission_state, "terminal_failed")
        self.assertFalse(self.task.observations.exists())
        peer.release_resources_before_submit.assert_not_called()
        peer.queue_prompt.assert_not_called()

    def test_paginated_readonly_api_has_real_empty_state_owner_scope_and_indexed_small_rows(self):
        client = APIClient()
        self.user.is_staff = False
        self.user.save(update_fields=["is_staff"])
        client.force_authenticate(self.user)
        url = f"/api/v1/tasks/{self.task.pk}/observations/"
        empty = client.get(url)
        self.assertEqual(empty.status_code, 200, empty.data)
        self.assertEqual(empty.data["count"], 0)
        self.record_preflight()
        record_task_observation(self.task, kind="preflight", started_at=timezone.now(), error_code="contract_unavailable")
        with CaptureQueriesContext(connection) as queries:
            response = client.get(url, {"page_size": 1})
        self.assertEqual(response.status_code, 200, response.data)
        self.assertEqual(response.data["count"], 2)
        self.assertEqual(len(response.data["results"]), 1)
        self.assertIsNotNone(response.data["next"])
        row = response.data["results"][0]
        self.assertEqual(row["status"], "failed")
        self.assertEqual(row["error_code"], "contract_unavailable")
        self.assertEqual(row["request_sha256"], self.preparation.request_sha256)
        self.assertNotIn("capability_snapshot", str(response.data))
        self.assertEqual(response["Cache-Control"], "private, no-store")
        for query in queries:
            self.assertFalse(query["sql"].lstrip().upper().startswith(("INSERT", "UPDATE", "DELETE")))
            for forbidden in ('"capability_snapshot"', '"workflow_json"', '"prepared_request"', '"outputs"', '"staged_inputs"'):
                self.assertNotIn(forbidden, query["sql"])
        if connection.vendor == "sqlite":
            plan = self.task.observations.all().explain().upper()
            self.assertIn("IDX_TASK_OBSERVATION", plan)
            self.assertNotIn("TEMP B-TREE", plan)
        self.assertEqual(client.get(url, {"shot_id": "old"}).status_code, 400)
        self.assertEqual(client.post(url, {}).status_code, 405)
        foreign = get_user_model().objects.create_user(username="observations-other")
        client.force_authenticate(foreign)
        self.assertEqual(client.get(url).status_code, 404)
