"""Original task adapter tests with real Run/Preparation/Attempt and PNG bytes.

Task allocation uses production dispatch without legacy trace fixtures.
This adapter suite isolates the
admission collector; controlled peers are not GPU acceptance.
"""
from copy import deepcopy
from datetime import timedelta
from importlib import import_module
from types import SimpleNamespace
from unittest.mock import Mock, patch
from uuid import uuid4

from django.apps import apps
from django.core.exceptions import ValidationError
from django.db import connection, IntegrityError, transaction
from django.db.models import QuerySet
from django.test import SimpleTestCase, TestCase, override_settings
from django.test.utils import CaptureQueriesContext
from django.utils import timezone

from studio.models import ExecutionPreparation, GenerationAttempt, GenerationTask, HostCapabilitySnapshot, ModelDeployment, ProductionRun, RuntimeHost, StageRun, WorkflowSmokeAttempt
from studio.services.comfyui import ComfyUIError
from studio.services.comfyui_contract import ComfyUIContractError
from studio.services.execution_production_workflow_queue import locked_prepared_generation_task, prepared_generation_concurrency, read_prepared_generation_task
from studio.services.json_contracts import assign_json_contract
from studio.services.runtime_platform import host_resolution_and_concurrency
from studio.services.security_boundaries import boundary_int
from studio.services.task_queue import (
    _ensure_task_deadlines, _mark_task_meaningful_progress, _task_idle_seconds,
    _task_node_type, claim_next_task, comfy_client_for_task, fail_claimed_task_preflight, prepare_workflow, process_task, task_wait_timeout,
    _finalize_cancelled, _open_task_progress_socket, _record_progress_telemetry, _record_runtime_state,
    _claimed_task_rows, _refresh_claimed_task, _wait_for_task_completion, recover_stale_tasks, TaskClaimLost,
)
from studio.services.workflow_media_tokens import collect_workflow_media_use_tokens
from studio.services.workflow_smoke_resolution import resolve_workflow_smoke_attempt
from studio.tests.test_execution_production_workflow_requests import ProductionWorkflowRequestTests
from studio.tests.test_execution_production_auxiliaries import production_observations
from studio.tests.test_execution_workflow_staging import RecordingUploadClient


@override_settings(AI_PROVIDER_ALLOWED_HOSTS=("127.0.0.1",))
class ProductionWorkflowQueueTests(TestCase):
    def test_submitted_local_failure_releases_only_reservation_and_keeps_actual_unknown(self):
        from decimal import Decimal
        from studio.services.execution_production_workflow_queue import (
            begin_prepared_generation_submission, update_prepared_generation_lifecycle,
        )
        self.stage_workflow()
        with patch("studio.services.execution_production_dispatch.collect_production_execution_admission",
                side_effect=production_observations):
            self.assertTrue(begin_prepared_generation_submission(self.task))
        GenerationAttempt.objects.filter(pk=self.attempt.pk).update(is_external=False, committed_cost=Decimal("0.5"))
        ProductionRun.objects.filter(pk=self.run.pk).update(committed_cost_total=Decimal("0.8"))
        condition = GenerationTask.objects.filter(pk=self.task.pk, status="running")
        self.assertEqual(update_prepared_generation_lifecycle(self.task,
            {"status": "failed", "submission_state": "terminal_failed"}, condition=condition), 1)
        self.attempt.refresh_from_db()
        self.run.refresh_from_db()
        self.assertEqual(self.attempt.committed_cost, 0)
        self.assertIsNone(self.attempt.actual_cost)
        self.assertEqual(self.run.committed_cost_total, Decimal("0.3"))
        # Repeated observation cannot release another call's reservation.
        self.assertEqual(update_prepared_generation_lifecycle(self.task,
            {"status": "failed", "submission_state": "terminal_failed"}, condition=condition), 0)
        self.run.refresh_from_db()
        self.assertEqual(self.run.committed_cost_total, Decimal("0.3"))

    def test_local_terminal_failure_without_bill_is_retryable_but_unknown_remote_is_not(self):
        from studio.services.execution_production_attempts import require_retryable_failure
        GenerationTask.objects.filter(pk=self.task.pk).update(status="failed", submission_state="terminal_failed")
        GenerationAttempt.objects.filter(pk=self.attempt.pk).update(status="failed", committed_cost=0,
            actual_cost=None, is_external=False, error_code="execution_workflow_failed")
        self.attempt.refresh_from_db()
        require_retryable_failure(self.attempt, automatic=True)
        self.assertIsNone(self.attempt.actual_cost)
        for changes in ({"submission_state": "submitting"}, {"outputs": [{"filename": "not-retryable.png"}]}):
            before = {key: getattr(self.task, key) for key in changes}
            GenerationTask.objects.filter(pk=self.task.pk).update(**changes)
            self.attempt.refresh_from_db()
            with self.assertRaises(ValidationError):
                require_retryable_failure(self.attempt, automatic=True)
            GenerationTask.objects.filter(pk=self.task.pk).update(**before)
        self.attempt.refresh_from_db()
        self.attempt.is_external = True
        with self.assertRaises(ValidationError):
            require_retryable_failure(self.attempt, automatic=True)

    _create_version = ProductionWorkflowRequestTests._create_version
    resolve = ProductionWorkflowRequestTests.resolve

    def setUp(self):
        ProductionWorkflowRequestTests.setUp(self)
        self.task = self.attempt.generation_task
        GenerationTask.objects.filter(pk=self.task.pk).update(status="running", attempts=1, started_at=timezone.now())
        self.task.refresh_from_db()
        _ensure_task_deadlines(self.task)
        self.peer = RecordingUploadClient()

    def stage_workflow(self):
        with patch("studio.services.execution_production_dispatch.collect_production_execution_admission", side_effect=production_observations):
            return prepare_workflow(self.task, self.peer)

    def queue_task(self, **changes):
        GenerationTask.objects.filter(pk=self.task.pk).update(status="queued", attempts=0, started_at=None, **changes)
        self.task.refresh_from_db()

    def queue_submitted_task(self, **changes):
        now, token = timezone.now(), str(self.attempt.pk)
        GenerationAttempt.objects.filter(pk=self.attempt.pk).update(status="submitted", submission_started_at=now,
            submitted_at=now, provider_request_id=token)
        self.queue_task(submission_token=token, comfy_prompt_id=token, submission_state="submitted", **changes)

    def test_explicit_retry_has_new_delivery_uuid_but_same_frozen_preparation(self):
        from studio.services.execution_production_dispatch import dispatch_production_execution
        from studio.services.execution_production_workflow_queue import begin_prepared_generation_submission
        self.stage_workflow()
        with patch("studio.services.execution_production_dispatch.collect_production_execution_admission",
                side_effect=production_observations):
            self.assertTrue(begin_prepared_generation_submission(self.task))
        first_id, first_token = self.attempt.pk, self.task.submission_token
        frozen_sha = self.attempt.request_snapshot_sha256
        self.assertEqual(first_token, str(first_id))
        self.assertNotEqual(first_token, str(self.preparation.pk))
        # An explicit, settled remote failure is the only retry fixture here.
        # It is not a missing/uncertain response or an automatic recovery.
        GenerationTask.objects.filter(pk=self.task.pk).update(status="failed", submission_state="terminal_failed")
        GenerationAttempt.objects.filter(pk=first_id).update(status="failed", committed_cost=0, actual_cost=0,
            error_code="execution_workflow_failed")
        ProductionRun.objects.filter(pk=self.run.pk).update(committed_cost_total=0)
        self.preparation.refresh_from_db()
        with patch("studio.services.execution_production_dispatch.collect_production_execution_admission",
                side_effect=lambda prep, definition, request, **_: production_observations(prep, definition, request)):
            next_attempt = dispatch_production_execution(preparation_id=self.preparation.pk,
                expected_preparation_revision=self.preparation.revision, actor=self.user, retry_of_id=first_id)
        self.assertEqual(next_attempt.retry_of_id, first_id)
        self.assertEqual(next_attempt.preparation_id, self.preparation.pk)
        self.assertEqual(next_attempt.request_snapshot_sha256, frozen_sha)
        self.assertNotEqual(next_attempt.generation_task_id, self.task.pk)
        self.task, self.attempt = claim_next_task(), next_attempt
        self.assertEqual(self.task.pk, next_attempt.generation_task_id)
        self.stage_workflow()
        with patch("studio.services.execution_production_dispatch.collect_production_execution_admission",
                side_effect=production_observations):
            self.assertTrue(begin_prepared_generation_submission(self.task))
        self.assertEqual(self.task.submission_token, str(next_attempt.pk))
        self.assertNotEqual(self.task.submission_token, first_token)
        with self.assertRaises(ValidationError):
            begin_prepared_generation_submission(self.task)

    def another_queued_call(self, *, separate_run=False):
        from studio.services.execution_preparations import create_execution_instance_preparation
        from studio.services.execution_finalization import finalize_execution
        from studio.services.execution_production_dispatch import dispatch_production_execution
        original_run = self.run
        stage = self.stage
        if separate_run:
            from studio.services.production_run_plans import freeze_run_execution_plan
            from studio.services.production_stage_catalog import production_stage_catalog
            self.run = ProductionRun.objects.create(owner=self.user, project=self.project, plan_source="manual",
                status="running", allow_external_calls=True, idempotency_key=str(uuid4()), request_sha256="a" * 64)
            assign_json_contract(self.run, "config_snapshot", original_run.config_snapshot,
                max_bytes=boundary_int("security.snapshot.stage_max_bytes"))
            self.run.save()
            catalog = production_stage_catalog()
            freeze_run_execution_plan(run=self.run, scope={"kind": "project"}, delivery={"kind": "episode_masters"}, initial_content=None,
                stage_catalog_key=catalog.value["key"], expected_stage_catalog_sha256=catalog.sha256, actor=self.user)
            media_stage = next(row for row in catalog.value["stages"] if row["key"] == "media_generation")
            stage = StageRun.objects.create(production_run=self.run, stage_key=media_stage["key"], ordinal=media_stage["ordinal"],
                mode="manual", status="running", idempotency_key=str(uuid4()))
        preparation = create_execution_instance_preparation(owner_ref=("stage_run_id", stage.pk),
            version_id=self.version.pk, expected_definition_sha256=self.definition.sha256,
            unit_key="media-unit", request_key="candidate-2", intent=self.intent, resolve_facts=self.resolve, actor=self.user)
        preparation = finalize_execution(preparation_id=preparation.pk,
            expected_preparation_revision=preparation.revision, actor=self.user, collect_admission_facts=production_observations)
        with patch("studio.services.execution_production_dispatch.collect_production_execution_admission", side_effect=production_observations):
            attempt = dispatch_production_execution(preparation_id=preparation.pk,
                expected_preparation_revision=preparation.revision, actor=self.user)
        task = attempt.generation_task
        task.priority = self.task.priority + 1
        task.save(update_fields=["priority", "updated_at"])
        self.run = original_run
        return task, attempt, stage

    def test_claim_reads_original_run_not_current_defaults_or_old_task_capacity(self):
        from studio.services.configuration import resolve_configuration
        def security_only(*args, **kwargs):
            self.assertFalse(args)
            self.assertFalse(kwargs.get("run") or kwargs.get("project"))
            self.assertTrue(all(key.startswith("security.") for key in kwargs["required_keys"]))
            return resolve_configuration(**kwargs)
        self.queue_task()
        self.assertNotIn("capability_snapshot", {field.name for field in GenerationTask._meta.fields})
        before = self.task.hard_deadline_at
        with patch("studio.services.configuration.resolve_configuration", side_effect=security_only), \
                patch("studio.services.production_run_auto_advance.schedule_generation_task_run_advance"):
            self.assertEqual(prepared_generation_concurrency(self.task), 3)
            claimed = claim_next_task()
        self.assertEqual(claimed.pk, self.task.pk)
        self.assertEqual(claimed.hard_deadline_at, before)
        self.assertEqual(claimed.attempts, 1)
        self.assertEqual(self.peer.uploads, [])

    def test_capacity_rejects_missing_or_rewritten_original_run_facts(self):
        saved = deepcopy(self.run.config_snapshot)
        # Corrupt bytes cannot be repaired with task metadata or current defaults.
        ProductionRun.objects.filter(pk=self.run.pk).update(config_snapshot={})
        with self.assertRaises(ValidationError):
            prepared_generation_concurrency(self.task)
        # Even a self-consistent new Run hash cannot replace this Preparation's source.
        self.run.config_snapshot = saved
        self.run.config_snapshot["values"]["production.default_concurrency"] = 2
        assign_json_contract(self.run, "config_snapshot", self.run.config_snapshot,
            max_bytes=boundary_int("security.snapshot.stage_max_bytes"))
        self.run.save()
        with self.assertRaisesMessage(ValidationError, "不属于本次冻结Run"):
            prepared_generation_concurrency(self.task)

    def test_bad_unsubmitted_task_is_failed_without_blocking_next_valid_task(self):
        self.queue_task()
        bad, bad_attempt, _ = self.another_queued_call()
        GenerationAttempt.objects.filter(pk=bad_attempt.pk).update(request_sha256="0" * 64)
        with patch("studio.services.task_queue.schedule_generation_task_run_advance"):
            claimed = claim_next_task()
        self.assertEqual(claimed.pk, self.task.pk)
        bad.refresh_from_db()
        self.assertEqual(bad.status, "failed")
        self.assertIn("冻结准备请求", bad.error_message)
        self.assertFalse(bad.submission_token)
        bad_attempt.refresh_from_db()
        self.assertEqual(bad_attempt.status, "failed")

    def test_paused_root_does_not_starve_other_queued_calls(self):
        self.queue_task()
        paused, attempt, stage = self.another_queued_call(separate_run=True)
        StageRun.objects.filter(pk=stage.pk).update(status="paused")
        claimed = claim_next_task()
        self.assertEqual(claimed.pk, self.task.pk)
        paused.refresh_from_db()
        attempt.refresh_from_db()
        self.assertEqual(paused.status, "queued")
        self.assertEqual(paused.attempts, 0)
        self.assertIsNone(attempt.submission_started_at)

    def test_orphan_task_is_not_claimed_or_given_a_hidden_preparation(self):
        self.queue_task()
        orphan = GenerationTask.objects.create(project=self.project, owner=self.user,
            kind="video", status="queued", priority=self.task.priority + 1)
        before = (ExecutionPreparation.objects.count(), GenerationAttempt.objects.count())
        claimed = claim_next_task()
        self.assertEqual(claimed.pk, self.task.pk)
        orphan.refresh_from_db()
        self.assertEqual(orphan.status, "queued")
        self.assertEqual(before, (ExecutionPreparation.objects.count(), GenerationAttempt.objects.count()))

    def test_running_task_with_missing_preparation_blocks_pool_expansion(self):
        self.queue_task()
        bad = GenerationTask.objects.create(project=self.project, owner=self.user, kind="video", status="running")
        with override_settings(TASK_MAX_CONCURRENT=3):
            self.assertIsNone(claim_next_task())
        bad.refresh_from_db()
        self.assertEqual(bad.status, "running")
        self.task.refresh_from_db()
        self.assertEqual(self.task.status, "queued")

    def test_remote_reattachment_does_not_require_new_quota_or_reset_deadlines(self):
        self.queue_submitted_task()
        GenerationTask.objects.filter(pk=self.task.pk).update(attempts=5)
        self.task.refresh_from_db()
        hard, token = self.task.hard_deadline_at, self.task.submission_token
        with patch("studio.services.task_queue.prepared_generation_concurrency", side_effect=AssertionError("Recovery is not new work")):
            claimed = claim_next_task()
        self.assertEqual(claimed.pk, self.task.pk)
        self.assertEqual(claimed.attempts, 6)
        self.assertEqual(claimed.submission_token, token)
        self.assertEqual(claimed.hard_deadline_at, hard)

    def test_corrupt_remote_request_keeps_token_and_circuit_breaker(self):
        token = str(self.attempt.pk)
        self.queue_submitted_task()
        GenerationAttempt.objects.filter(pk=self.attempt.pk).update(request_sha256="0" * 64)
        with patch("studio.services.task_queue.schedule_generation_task_run_advance"):
            self.assertIsNone(claim_next_task())
        self.task.refresh_from_db()
        self.assertEqual(self.task.status, "failed")
        self.assertEqual(self.task.submission_token, token)
        self.assertEqual(self.task.submission_state, "submitted")
        self.assertIsNone(claim_next_task())

    def test_recovered_request_with_missing_deadlines_never_gets_a_new_window(self):
        self.queue_submitted_task(deadline_started_at=None,
            hard_deadline_at=None, idle_deadline_at=None)
        with patch("studio.services.task_queue.schedule_generation_task_run_advance"):
            self.assertIsNone(claim_next_task())
        self.task.refresh_from_db()
        self.assertEqual(self.task.status, "failed")
        self.assertIsNone(self.task.hard_deadline_at)
        self.assertIsNone(self.task.deadline_started_at)
        self.assertTrue(self.task.submission_token)

    def test_unresolved_smoke_blocks_original_queue_until_explicit_resolution(self):
        self.queue_task()
        from studio.services.execution_smoke import create_instance_smoke
        from studio.services.execution_finalization import ExecutionAdmissionFacts, finalize_execution
        from studio.services.json_contracts import json_contract
        smoke, prep, _ = create_instance_smoke(version_id=self.version.pk,
            expected_definition_sha256=self.definition.sha256, fixture=self.intent.value, field_values=[],
            client_idempotency_key="isolated-uncertain-workflow", allow_external_calls=False, budget_limit=None, actor=self.user)
        # This queue arbitration fixture isolates admission only; resolution
        # must still verify the real immutable Smoke Preparation and request.
        prep = finalize_execution(preparation_id=prep.pk, expected_preparation_revision=prep.revision, actor=self.user,
            collect_admission_facts=lambda prep, definition, request: ExecutionAdmissionFacts((), "unknown", [],
                json_contract({"preparation_id": str(prep.pk), "request_sha256": request.sha256})))
        self.assertEqual(prep.status, "ready", prep.reason)
        smoke.status = "uncertain"
        smoke.save(update_fields=("status", "updated_at"))
        self.assertIsNone(claim_next_task())
        self.task.refresh_from_db()
        self.assertEqual(self.task.status, "queued")
        resolve_workflow_smoke_attempt(smoke, outcome="confirmed_not_submitted", provider_request_id="",
            note="Isolated test operator explicitly verified that the remote request was not submitted.", user=self.user)
        claimed = claim_next_task()
        self.assertEqual(claimed.pk, self.task.pk)
        smoke.refresh_from_db()
        self.assertNotEqual(smoke.status, "succeeded")

    def test_waiting_smoke_yields_to_remote_recovery_but_not_to_new_work(self):
        self.queue_task()
        smoke = WorkflowSmokeAttempt.objects.create(workflow_version=self.workflow, deployment=self.deployment,
            created_by=self.user, scope="resource", status="queued", graph_sha256="a" * 64,
            execution_contract_sha256="b" * 64, validation_evidence_sha256="c" * 64, target_sha256="d" * 64)
        self.assertIsNone(claim_next_task())
        self.queue_submitted_task()
        claimed = claim_next_task()
        self.assertEqual(claimed.pk, self.task.pk)
        smoke.refresh_from_db()
        self.assertEqual(smoke.status, "queued")
        self.assertFalse(smoke.provider_request_id)

    def test_active_smoke_still_blocks_recovery_from_claiming_resource(self):
        self.queue_submitted_task()
        WorkflowSmokeAttempt.objects.create(workflow_version=self.workflow, deployment=self.deployment,
            created_by=self.user, scope="resource", status="running", graph_sha256="a" * 64,
            execution_contract_sha256="b" * 64, validation_evidence_sha256="c" * 64, target_sha256="d" * 64)
        self.assertIsNone(claim_next_task())
        self.task.refresh_from_db()
        self.assertEqual(self.task.status, "queued")

    def test_stale_worker_cannot_fail_or_refresh_itself_into_another_claim(self):
        from unittest.mock import Mock
        for changes in ({"attempts": self.task.attempts + 1},
                {"started_at": self.task.started_at + timedelta(seconds=1)}, {"status": "succeeded"}):
            original = {key: getattr(self.task, key) for key in changes}
            GenerationTask.objects.filter(pk=self.task.pk).update(**changes)
            try:
                with self.subTest(changes=changes), patch("studio.services.task_queue.schedule_generation_task_run_advance") as advance:
                    result = fail_claimed_task_preflight(self.task, ComfyUIError("old worker error"))
                    self.assertNotEqual(result.status, "failed")
                    self.assertNotEqual(result.error_message, "old worker error")
                    advance.assert_not_called()
                    peer = Mock()
                    process_task(self.task, peer)
                    self.assertEqual(peer.mock_calls, [])
            finally:
                GenerationTask.objects.filter(pk=self.task.pk).update(**original)

    def test_current_worker_preflight_failure_updates_only_its_task(self):
        with patch("studio.services.task_queue.schedule_generation_task_run_advance") as advance:
            result = fail_claimed_task_preflight(self.task, ComfyUIError("current worker invalid request"))
            self.assertEqual(result.status, "failed")
            self.assertEqual(result.error_message, "current worker invalid request")
            advance.assert_called_once_with(self.task.pk)
        self.assertFalse(result.submission_token)

    def test_original_task_uses_preparation_and_each_use_receipt_survives_restart(self):
        if connection.vendor == "sqlite":
            plan = GenerationAttempt.objects.filter(generation_task=self.task).order_by().only("id").explain()
            self.assertIn("INDEX", plan.upper())
        frozen = deepcopy(self.preparation.contract.prepared_request)
        hard, idle = self.task.hard_deadline_at, self.task.idle_deadline_at
        with patch("studio.services.workflow_compiler.compile_frozen_workflow_values", side_effect=AssertionError("No recompile")):
            graph = self.stage_workflow()
            self.assertFalse(collect_workflow_media_use_tokens(graph))
            self.assertEqual(len(self.peer.uploads), 2)
            self.assertEqual(len(set(self.peer.uploads)), 2)
            self.task.refresh_from_db()
            self.assertEqual(len(self.task.staged_inputs), 2)
            self.assertEqual(self.task.hard_deadline_at, hard)
            self.assertEqual(self.task.idle_deadline_at, idle)
            self.assertEqual(self.stage_workflow(), graph)
            self.assertEqual(len(self.peer.uploads), 2)
        self.preparation.contract.refresh_from_db()
        self.assertEqual(self.preparation.contract.prepared_request, frozen)
        self.peer.files[self.peer.uploads[0]] = b"changed remote bytes"
        with self.assertRaises(ComfyUIError):
            self.stage_workflow()

    def test_stopped_or_reclaimed_worker_never_uploads(self):
        cases = [(GenerationTask, self.task, {"attempts": 2}),
            (GenerationTask, self.task, {"status": "cancel_requested"}),
            (GenerationTask, self.task, {"submission_token": "already-persisted"}),
            (GenerationTask, self.task, {"hard_deadline_at": timezone.now() - timedelta(seconds=1)}),
            (GenerationTask, self.task, {"idle_deadline_at": None}),
            (GenerationAttempt, self.attempt, {"status": "cancel_requested"}),
            (StageRun, self.stage, {"status": "cancelled"}), (ProductionRun, self.run, {"status": "cancelled"})]
        for model, instance, changes in cases:
            original = {key: getattr(instance, key) for key in changes}
            model.objects.filter(pk=instance.pk).update(**changes)
            try:
                with self.subTest(changes=changes), self.assertRaises(ComfyUIError):
                    self.stage_workflow()
            finally:
                model.objects.filter(pk=instance.pk).update(**original)
        self.assertEqual(self.peer.uploads, [])

    def test_stage_must_match_frozen_plan_before_any_upload(self):
        original = {"ordinal": self.stage.ordinal, "mode": self.stage.mode}
        for changes in ({"ordinal": self.stage.ordinal + 1}, {"mode": "existing_pipeline"}):
            with self.subTest(changes=changes):
                StageRun.objects.filter(pk=self.stage.pk).update(**changes)
                try:
                    with self.assertRaisesMessage(ComfyUIError, "当前执行步骤与冻结运行计划不一致"):
                        self.stage_workflow()
                    self.assertEqual(self.peer.uploads, [])
                finally:
                    StageRun.objects.filter(pk=self.stage.pk).update(**original)

    def test_missing_or_foreign_attempt_is_not_reconstructed_from_legacy_snapshot(self):
        GenerationAttempt.objects.filter(pk=self.attempt.pk).update(generation_task=None)
        with self.assertRaisesMessage(ValidationError, "唯一的实际主调用"):
            read_prepared_generation_task(self.task)
        GenerationAttempt.objects.filter(pk=self.attempt.pk).update(generation_task=self.task)
        original = self.task.kind
        self.task.kind = "image"
        with self.assertRaisesMessage(ValidationError, "产物与主准备"):
            read_prepared_generation_task(self.task)
        self.task.kind = original
        GenerationAttempt.objects.filter(pk=self.attempt.pk).update(request_sha256="0" * 64)
        with self.assertRaisesMessage(ValidationError, "冻结准备请求"):
            read_prepared_generation_task(self.task)

    def test_hard_and_node_deadlines_use_frozen_preparation_not_task_metadata(self):
        self.assertGreater(task_wait_timeout(self.task), 0)
        self.assertLessEqual(task_wait_timeout(self.task), 900)
        self.assertEqual(_task_idle_seconds(self.task), 240)
        context = read_prepared_generation_task(self.task)
        graph = context.media["payload"]["compiled_workflow"]["graph"]
        load = next(key for key, row in graph.items() if row["class_type"] == "LoadImage")
        self.assertEqual(_task_node_type(self.task, load), "LoadImage")
        self.assertEqual(_task_idle_seconds(self.task, node_id=load), 75)
        _ensure_task_deadlines(self.task)
        hard = self.task.hard_deadline_at
        self.assertEqual((hard - self.task.deadline_started_at).total_seconds(), 900)
        observed = self.task.deadline_started_at + timedelta(seconds=10)
        self.assertTrue(_mark_task_meaningful_progress(self.task, "node:" + load, now=observed, node_id=load))
        self.assertFalse(_mark_task_meaningful_progress(self.task, "node:" + load, now=observed, node_id=load))
        self.task.refresh_from_db()
        self.assertEqual(self.task.hard_deadline_at, hard)
        self.assertEqual(self.task.idle_deadline_at, observed + timedelta(seconds=75))
        GenerationTask.objects.filter(pk=self.task.pk).update(attempts=2)
        self.assertFalse(_mark_task_meaningful_progress(self.task, "node:stale", now=observed + timedelta(seconds=1)))

    def test_original_gate_blocks_missing_resource_evidence_before_upload(self):
        with self.assertRaisesMessage(ComfyUIError, "尚未通过当前生产准入"):
            prepare_workflow(self.task, self.peer)
        self.assertEqual(self.peer.uploads, [])

    def test_peer_uses_exact_instance_and_does_not_retarget_after_config_change(self):
        with patch("studio.services.task_queue.ComfyUIClient") as factory:
            comfy_client_for_task(self.task)
            self.assertEqual(factory.call_count, 1)
            first = factory.call_args
            ModelDeployment.objects.filter(pk=self.deployment.pk).update(status="inactive")
            comfy_client_for_task(self.task)
            self.assertEqual(factory.call_args, first)
            config = deepcopy(self.deployment.runtime_config)
            config["base_url"] = "http://127.0.0.1:18188"
            ModelDeployment.objects.filter(pk=self.deployment.pk).update(runtime_config=config)
            with self.assertRaises(ComfyUIError):
                comfy_client_for_task(self.task)
            self.assertEqual(factory.call_count, 2)

    def test_database_enforces_one_task_one_primary_attempt_without_blocking_unallocated_calls(self):
        values = {"stage_run": self.stage, "preparation": self.preparation, "execution_instance_version": self.version,
            "model_deployment": self.deployment, "unit_key": self.preparation.unit_key, "attempt_number": 2,
            "role": "primary", "operation": self.version.operation, "status": "failed", "request_sha256": "a" * 64}
        with self.assertRaises(IntegrityError), transaction.atomic():
            GenerationAttempt.objects.create(**values, generation_task=self.task)
        # Nullable references remain valid before allocation and for text/API;
        # the new unique index must not turn NULL into a shared queue identity.
        GenerationAttempt.objects.create(**values)
        GenerationAttempt.objects.create(**{**values, "attempt_number": 3})
        parent = ExecutionPreparation.objects.create(stage_run=self.stage, execution_instance_version=self.version,
            purpose="production", status="awaiting_input", unit_key="parent", request_key=str(uuid4()), created_by=self.user, intent_sha256="b" * 64)
        with self.assertRaises(IntegrityError), transaction.atomic():
            GenerationAttempt.objects.filter(pk=self.attempt.pk).update(role="auxiliary", parent_preparation=parent,
                auxiliary_purpose="generate_fields", field_batch_key="c" * 64)

    def test_migration_requires_reviewed_cleanup_and_never_relinks_old_tasks(self):
        migration = import_module("studio.migrations.0152_generation_task_primary_attempt")
        editor = SimpleNamespace(connection=connection)
        with self.assertRaisesMessage(RuntimeError, "reviewed P5 cutover"):
            migration.require_unlinked_old_tasks(apps, editor)
        self.attempt.refresh_from_db()
        self.assertEqual(self.attempt.generation_task_id, self.task.pk)
        GenerationAttempt.objects.filter(pk=self.attempt.pk).update(generation_task=None)
        migration.require_unlinked_old_tasks(apps, editor)
        self.attempt.refresh_from_db()
        self.assertIsNone(self.attempt.generation_task_id)

    def test_upload_callback_cannot_write_receipt_after_task_cancel_or_worker_reclaim(self):
        for model, instance, changes in ((GenerationTask, self.task, {"attempts": 2}),
                (GenerationAttempt, self.attempt, {"operation": "image.generate"}),
                (GenerationAttempt, self.attempt, {"request_snapshot_sha256": "d" * 64})):
            original = {key: getattr(instance, key) for key in changes}
            self.peer = RecordingUploadClient()
            original_upload = self.peer.upload_input_file
            def upload(*args, **kwargs):
                response = original_upload(*args, **kwargs)
                model.objects.filter(pk=instance.pk).update(**changes)
                return response
            try:
                with self.subTest(changes=changes), patch.object(self.peer, "upload_input_file", side_effect=upload), self.assertRaises(ComfyUIError):
                    self.stage_workflow()
                self.assertEqual(GenerationTask.objects.get(pk=self.task.pk).staged_inputs, [])
                self.assertEqual(len(self.peer.uploads), 1)
            finally:
                model.objects.filter(pk=instance.pk).update(**original)



@override_settings(AI_PROVIDER_ALLOWED_HOSTS=("127.0.0.1",))
class ProductionWorkflowClaimIsolationTests(TestCase):
    """Real durable task/preparation rows; controlled remote and archival edges.

    These test claim ownership, not GPU execution. Archive mocks below are
    explicitly not media acceptance evidence; native archival has its own suite.
    """
    _create_version = ProductionWorkflowRequestTests._create_version
    resolve = ProductionWorkflowRequestTests.resolve
    setUp = ProductionWorkflowQueueTests.setUp

    def submitted(self, **changes):
        GenerationAttempt.objects.filter(pk=self.attempt.pk).update(status="submitted",
            submission_started_at=timezone.now(), submitted_at=timezone.now(), provider_request_id=str(self.attempt.pk))
        GenerationTask.objects.filter(pk=self.task.pk).update(submission_token=str(self.attempt.pk), comfy_prompt_id=str(self.attempt.pk),
            submission_state="submitted", **changes)
        self.task.refresh_from_db()
        peer = Mock(spec=["history", "is_queued", "cancel", "extract_outputs"])
        peer.history.return_value = {"status": {"completed": True, "status_str": "success"}}
        peer.is_queued.return_value = False
        peer.extract_outputs.return_value = [{"filename": "test.png"}]
        return peer

    def reclaim(self):
        GenerationTask.objects.filter(pk=self.task.pk).update(attempts=self.task.attempts + 1,
            started_at=timezone.now(), heartbeat_at=timezone.now(), remote_error="current worker")

    def test_late_history_success_does_not_archive_or_advance(self):
        peer = self.submitted()
        def late_history(*_):
            self.reclaim()
            return {"status": {"completed": True}}
        peer.history.side_effect = late_history
        with patch("studio.services.task_queue.persist_task_provenance") as seal, \
                patch("studio.services.task_queue.schedule_generation_task_run_advance") as advance:
            result = process_task(self.task, peer)
        self.assertEqual(result.status, "running")
        self.assertEqual(result.remote_error, "current worker")
        seal.assert_not_called()
        advance.assert_not_called()
        peer.cancel.assert_not_called()
        peer.extract_outputs.assert_not_called()

    def test_late_history_error_cannot_fail_new_claim(self):
        peer = self.submitted()
        def late_error(*_):
            self.reclaim()
            raise ComfyUIError("old connection failure")
        peer.history.side_effect = late_error
        with patch("studio.services.task_queue.schedule_generation_task_run_advance") as advance:
            result = process_task(self.task, peer)
        self.assertEqual(result.status, "running")
        self.assertEqual(result.remote_error, "current worker")
        advance.assert_not_called()

    def test_claim_lost_during_media_inspection_does_not_write_sidecars(self):
        peer = self.submitted()
        def inspect(*_):
            self.reclaim()
            return [{"filename": "test.png"}]
        with patch("studio.services.task_queue.inspect_task_output_media", side_effect=inspect), \
                patch("studio.services.task_queue.persist_task_provenance") as seal, \
                patch("studio.services.task_queue.record_generation_task_artifacts") as archive, \
                patch("studio.services.task_queue.schedule_generation_task_run_advance") as advance:
            result = process_task(self.task, peer)
        self.assertEqual(result.status, "running")
        self.assertEqual(result.outputs, [])
        seal.assert_not_called()
        archive.assert_not_called()
        advance.assert_not_called()

    def test_current_claim_publishes_outputs_and_success_together(self):
        peer = self.submitted()
        enriched = [{"filename": "test.png", "provenance": {"test": "controlled archive boundary"}}]
        def archive(task):
            self.assertTrue(connection.in_atomic_block)
            current = GenerationTask.objects.get(pk=task.pk)
            self.assertEqual(current.outputs, enriched)
            self.assertEqual(current.status, "succeeded")
        with patch("studio.services.task_queue.inspect_task_output_media", return_value=peer.extract_outputs.return_value), \
                patch("studio.services.task_queue.persist_task_provenance", return_value=enriched) as seal, \
                patch("studio.services.task_queue.record_generation_task_artifacts", side_effect=archive), \
                patch("studio.services.task_queue.schedule_generation_task_run_advance") as advance, \
                patch("studio.services.task_distribution_recovery.prepare_all_task_distributions") as distribute:
            result = process_task(self.task, peer)
        self.assertEqual(result.status, "succeeded")
        self.assertEqual(result.outputs, enriched)
        seal.assert_called_once()
        advance.assert_called_once_with(self.task.pk)
        distribute.assert_called_once()

    def test_worker_finishes_distribution_before_scheduling_recipe_advance(self):
        peer = self.submitted()
        events = []
        with patch("studio.services.task_queue.inspect_task_output_media", return_value=[]), \
                patch("studio.services.task_queue.persist_task_provenance", return_value=[]), \
                patch("studio.services.task_queue.record_generation_task_artifacts"), \
                patch("studio.services.task_distribution_recovery.prepare_all_task_distributions", side_effect=lambda _, **kwargs: events.append("distribution")), \
                patch("studio.services.task_queue.schedule_generation_task_run_advance", side_effect=lambda _: events.append("advance")):
            result = process_task(self.task, peer)
        self.assertEqual(result.status, "succeeded")
        self.assertEqual(events, ["distribution", "advance"])

    def test_archive_failure_rolls_back_outputs_and_success(self):
        peer = self.submitted()
        with patch("studio.services.task_queue.inspect_task_output_media", return_value=[]), \
                patch("studio.services.task_queue.persist_task_provenance", return_value=[{"test": "uncommitted"}]), \
                patch("studio.services.task_queue.record_generation_task_artifacts", side_effect=ValueError("manifest refused")), \
                patch("studio.services.task_queue.schedule_generation_task_run_advance"), \
                patch("studio.services.task_distribution_recovery.prepare_all_task_distributions") as distribute:
            result = process_task(self.task, peer)
        self.assertEqual(result.status, "failed")
        self.assertEqual(result.outputs, [])
        distribute.assert_not_called()

    def test_cancellation_during_inspection_prevents_publication(self):
        peer = self.submitted()
        def inspect(*_):
            GenerationTask.objects.filter(pk=self.task.pk).update(status="cancel_requested")
            return []
        with patch("studio.services.task_queue.inspect_task_output_media", side_effect=inspect), \
                patch("studio.services.task_queue.persist_task_provenance") as seal, \
                patch("studio.services.task_queue.schedule_generation_task_run_advance"):
            result = process_task(self.task, peer)
        self.assertEqual(result.status, "cancelled")
        self.assertEqual(result.submission_state, "terminal_cancelled")
        seal.assert_not_called()
        peer.cancel.assert_called_once_with(result.submission_token)

    def test_late_worker_cannot_write_telemetry_deadlines_or_cancellation(self):
        self.reclaim()
        current = GenerationTask.objects.get(pk=self.task.pk)
        with patch("studio.services.task_queue.schedule_generation_task_run_advance") as advance:
            for callback in (lambda: _record_runtime_state(self.task, "degraded", remote_error="late"),
                    lambda: _record_progress_telemetry(self.task, "receiving", comfy_progress=99),
                    lambda: _finalize_cancelled(self.task, remote_confirmed=True),
                    lambda: _refresh_claimed_task(self.task)):
                with self.subTest(callback=callback), self.assertRaises(TaskClaimLost):
                    callback()
            self.assertFalse(_mark_task_meaningful_progress(self.task, "late-node"))
            advance.assert_not_called()
        after = GenerationTask.objects.get(pk=self.task.pk)
        for key in ("status", "comfy_progress", "remote_error", "heartbeat_at", "idle_deadline_at", "hard_deadline_at"):
            self.assertEqual(getattr(after, key), getattr(current, key))

    def test_websocket_open_losing_claim_closes_socket(self):
        peer = Mock(spec=["open_progress_socket"])
        socket = Mock(spec=["close"])
        GenerationTask.objects.filter(pk=self.task.pk).update(client_id="controlled-task-client")
        self.task.refresh_from_db()
        def open_socket(*_, **__):
            self.reclaim()
            return socket
        peer.open_progress_socket.side_effect = open_socket
        with override_settings(TASK_PROGRESS_WEBSOCKET_ENABLED=True), self.assertRaises(TaskClaimLost):
            _open_task_progress_socket(self.task, peer)
        socket.close.assert_called_once()

    def test_claim_refresh_is_one_indexed_query_without_reloading_large_payloads(self):
        GenerationTask.objects.filter(pk=self.task.pk).update(outputs=[{"test": "new output metadata"}])
        with CaptureQueriesContext(connection) as queries:
            _refresh_claimed_task(self.task)
        self.assertEqual(len(queries), 1)
        selected = queries[0]["sql"].split(" FROM ")[0].lower()
        for field in ("capability_snapshot", "staged_inputs", "outputs"):
            self.assertNotIn(field, selected)
        self.assertEqual(self.task.outputs, [])
        _refresh_claimed_task(self.task, include_outputs=True)
        self.assertEqual(self.task.outputs, [{"test": "new output metadata"}])
        if connection.vendor == "sqlite":
            self.assertIn("INDEX", _claimed_task_rows(self.task).order_by().only("id").explain().upper())

    def test_collection_and_staging_share_root_to_task_lock_order(self):
        context = read_prepared_generation_task(self.task)
        original = QuerySet.select_for_update
        locks = []
        expected = [ProductionRun, StageRun, ExecutionPreparation, GenerationAttempt, GenerationTask]
        def record(query, *args, **kwargs):
            if query.model in expected:
                locks.append(query.model)
            return original(query, *args, **kwargs)
        with patch.object(QuerySet, "select_for_update", record):
            with locked_prepared_generation_task(self.task, context) as (_, _, attempt, task):
                self.assertTrue(connection.in_atomic_block)
                self.assertEqual(attempt.generation_task_id, task.pk)
        self.assertEqual(locks, expected)

    def test_polling_stops_after_claim_loss_without_remote_cancel(self):
        peer = self.submitted()
        def late_poll(*_):
            self.reclaim()
            return {"status": {"completed": True}}
        peer.history.side_effect = late_poll
        with self.assertRaises(TaskClaimLost):
            _wait_for_task_completion(self.task, peer, self.task.submission_token)
        peer.cancel.assert_not_called()

    def test_preflight_late_result_does_not_release_resources_or_post(self):
        graph = ProductionWorkflowQueueTests.stage_workflow(self)
        peer = Mock(spec=["release_resources_before_submit", "queue_prompt"])
        def late_preflight(*_, **__):
            self.reclaim()
            return SimpleNamespace(as_dict=lambda: {"test": "late preflight"})
        with patch("studio.services.task_queue.prepare_workflow", return_value=graph), \
                patch("studio.services.task_queue.preflight_comfyui_workflow", side_effect=late_preflight), \
                patch("studio.services.task_queue.schedule_generation_task_run_advance") as advance:
            result = process_task(self.task, peer)
        self.assertEqual(result.status, "running")
        self.assertFalse(result.submission_token)
        peer.release_resources_before_submit.assert_not_called()
        peer.queue_prompt.assert_not_called()
        advance.assert_not_called()

    def test_cancellation_during_resource_release_never_allocates_token_or_posts(self):
        graph = ProductionWorkflowQueueTests.stage_workflow(self)
        peer = Mock(spec=["release_resources_before_submit", "queue_prompt"])
        def cancel_during_release():
            GenerationTask.objects.filter(pk=self.task.pk).update(status="cancel_requested")
            return None
        peer.release_resources_before_submit.side_effect = cancel_during_release
        with patch("studio.services.task_queue.prepare_workflow", return_value=graph), \
                patch("studio.services.task_queue.preflight_comfyui_workflow",
                    return_value=SimpleNamespace(as_dict=lambda: {"node_count": 3, "link_count": 2, "class_types": ["ControlledPeer"], "schema_sha256": "c" * 64})):
            result = process_task(self.task, peer)
        self.assertEqual(result.status, "cancel_requested")
        self.assertFalse(result.submission_token)
        peer.queue_prompt.assert_not_called()

    def test_late_post_response_keeps_durable_token_for_new_claim(self):
        graph = ProductionWorkflowQueueTests.stage_workflow(self)
        peer = Mock(spec=["queue_prompt"])
        def late_response(*_, **kwargs):
            self.reclaim()
            return SimpleNamespace(prompt_id=kwargs["prompt_id"], node_errors={})
        peer.queue_prompt.side_effect = late_response
        with patch("studio.services.task_queue.prepare_workflow", return_value=graph), \
                patch("studio.services.execution_production_dispatch.collect_production_execution_admission", side_effect=production_observations), \
                patch("studio.services.task_queue.preflight_comfyui_workflow",
                    return_value=SimpleNamespace(as_dict=lambda: {"node_count": 3, "link_count": 2, "class_types": ["ControlledPeer"], "schema_sha256": "c" * 64})), \
                patch("studio.services.task_queue.schedule_generation_task_run_advance") as advance:
            result = process_task(self.task, peer)
        self.assertEqual(result.status, "running")
        self.assertEqual(result.submission_state, "prepared")
        self.assertTrue(result.submission_token)
        self.assertEqual(result.submission_token, peer.queue_prompt.call_args.kwargs["prompt_id"])
        peer.queue_prompt.assert_called_once()
        advance.assert_not_called()

    def test_cancel_request_with_definitive_remote_failure_finishes_cancelled(self):
        peer = self.submitted()
        def remote_failure(*_):
            GenerationTask.objects.filter(pk=self.task.pk).update(status="cancel_requested")
            return {"status": {"completed": False, "status_str": "error"}}
        peer.history.side_effect = remote_failure
        with patch("studio.services.task_queue.schedule_generation_task_run_advance") as advance:
            result = process_task(self.task, peer)
        self.assertEqual(result.status, "cancelled")
        self.assertEqual(result.submission_state, "terminal_failed")
        advance.assert_called_once_with(self.task.pk)

    def test_cancel_request_during_preflight_failure_is_not_overwritten(self):
        graph = ProductionWorkflowQueueTests.stage_workflow(self)
        peer = Mock(spec=["queue_prompt"])
        def cancelled_preflight(*_, **__):
            GenerationTask.objects.filter(pk=self.task.pk).update(status="cancel_requested")
            raise ComfyUIContractError(["controlled schema mismatch"])
        with patch("studio.services.task_queue.prepare_workflow", return_value=graph), \
                patch("studio.services.task_queue.preflight_comfyui_workflow", side_effect=cancelled_preflight), \
                patch("studio.services.task_queue.schedule_generation_task_run_advance") as advance:
            result = process_task(self.task, peer)
        self.assertEqual(result.status, "cancel_requested")
        self.assertFalse(result.submission_token)
        self.assertFalse(result.error_message)
        peer.queue_prompt.assert_not_called()
        advance.assert_not_called()

    def test_recovery_observation_cannot_overwrite_resumed_worker(self):
        for event in ("heartbeat", "reclaim", "cancel", "terminal"):
            with self.subTest(event=event):
                peer = self.submitted(status="running", heartbeat_at=timezone.now() - timedelta(minutes=10))
                def changed_while_reading(*_):
                    if event == "reclaim":
                        self.reclaim()
                    else:
                        updates = {"heartbeat_at": timezone.now()} if event == "heartbeat" else {
                            "status": "cancel_requested" if event == "cancel" else "succeeded"}
                        GenerationTask.objects.filter(pk=self.task.pk).update(**updates)
                    return {"status": {"completed": True}}
                peer.history.side_effect = changed_while_reading
                with patch("studio.services.task_queue.comfy_client_for_task", return_value=peer), \
                        patch("studio.services.task_queue.schedule_generation_task_run_advance") as advance:
                    self.assertEqual(recover_stale_tasks(older_than_minutes=1), 0)
                advance.assert_not_called()
                result = GenerationTask.objects.get(pk=self.task.pk)
                self.assertNotEqual(result.status, "queued")
                self.assertEqual(result.submission_token, self.task.submission_token)

    def test_recovery_current_remote_token_is_requeued_without_new_window(self):
        peer = self.submitted(heartbeat_at=timezone.now() - timedelta(minutes=10))
        hard = self.task.hard_deadline_at
        with patch("studio.services.task_queue.comfy_client_for_task", return_value=peer):
            self.assertEqual(recover_stale_tasks(older_than_minutes=1), 1)
        result = GenerationTask.objects.get(pk=self.task.pk)
        self.assertEqual(result.status, "queued")
        self.assertEqual(result.submission_token, self.task.submission_token)
        self.assertEqual(result.hard_deadline_at, hard)

    def test_recovery_late_cancel_response_does_not_cancel_new_claim(self):
        old = timezone.now() - timedelta(minutes=10)
        peer = self.submitted(status="cancel_requested", heartbeat_at=old, updated_at=old)
        def reclaim_during_cancel(*_):
            self.reclaim()
            GenerationTask.objects.filter(pk=self.task.pk).update(status="running")
        peer.cancel.side_effect = reclaim_during_cancel
        with patch("studio.services.task_queue.comfy_client_for_task", return_value=peer), \
                patch("studio.services.task_queue.schedule_generation_task_run_advance") as advance:
            self.assertEqual(recover_stale_tasks(older_than_minutes=1), 0)
        result = GenerationTask.objects.get(pk=self.task.pk)
        self.assertEqual(result.status, "running")
        self.assertEqual(result.submission_state, "submitted")
        advance.assert_not_called()


class ProductionWorkflowHostCapacityTests(TestCase):
    _create_version = ProductionWorkflowRequestTests._create_version
    resolve = ProductionWorkflowRequestTests.resolve

    def setUp(self):
        self.host = RuntimeHost.objects.create(key="queue-host", name="Queue host")
        # Bind the real host BEFORE publishing/finalizing, not by rewriting a
        # frozen target or mocking definition verification after publication.
        with patch("studio.tests.test_execution_workflow_request_media.ModelDeployment",
                side_effect=lambda **kwargs: ModelDeployment(runtime_host=self.host, **kwargs)):
            ProductionWorkflowQueueTests.setUp(self)
        self.snapshot = HostCapabilitySnapshot(host=self.host, revision=1, probe_status="succeeded",
            expires_at=timezone.now() + timedelta(hours=1))
        assign_json_contract(self.snapshot, "details", {"gpus": [{"vram_total_bytes": 32 * 1024**3}]})
        self.snapshot.save()

    def test_exact_host_uses_frozen_tiers_and_latest_probe_not_older_success(self):
        if connection.vendor == "sqlite":
            plan = HostCapabilitySnapshot.objects.filter(host=self.host).order_by("-captured_at", "-revision").explain()
            self.assertIn("INDEX", plan.upper())
        self.assertEqual(prepared_generation_concurrency(self.task), 2)
        newer = HostCapabilitySnapshot(host=self.host, revision=2, probe_status="failed",
            expires_at=timezone.now() + timedelta(hours=1))
        assign_json_contract(newer, "details", {"gpus": [{"vram_total_bytes": 32 * 1024**3}]})
        newer.save()
        with self.assertRaisesMessage(ValidationError, "最新探测"):
            prepared_generation_concurrency(self.task)
        newer.probe_status = "partial"
        newer.save(update_fields=["probe_status"])
        self.assertEqual(prepared_generation_concurrency(self.task), 2)
        newer.expires_at = timezone.now() - timedelta(seconds=1)
        newer.save(update_fields=["expires_at"])
        with self.assertRaisesMessage(ValidationError, "最新探测"):
            prepared_generation_concurrency(self.task)

    def test_host_drift_disabled_host_or_tampered_probe_cannot_expand_pool(self):
        RuntimeHost.objects.filter(pk=self.host.pk).update(status="disabled")
        with self.assertRaisesMessage(ValidationError, "运行主机不可用"):
            prepared_generation_concurrency(self.task)
        RuntimeHost.objects.filter(pk=self.host.pk).update(status="active")
        HostCapabilitySnapshot.objects.filter(pk=self.snapshot.pk).update(details={"gpus": []})
        with self.assertRaises(ValidationError):
            prepared_generation_concurrency(self.task)
        ModelDeployment.objects.filter(pk=self.deployment.pk).update(runtime_host=None)
        with self.assertRaises(ValidationError):
            prepared_generation_concurrency(self.task)


class ExplicitHostTierTests(SimpleTestCase):
    def test_host_rules_never_fill_missing_measurements_or_configuration(self):
        snapshot = SimpleNamespace(details={"gpus": [{"vram_total_bytes": 10}]})
        tiers = [{"max_vram_bytes": 5, "short_edge": 256, "concurrency": 1},
            {"max_vram_bytes": None, "short_edge": 768, "concurrency": 2}]
        self.assertEqual(host_resolution_and_concurrency(snapshot, tiers), (768, 2))
        self.assertEqual(host_resolution_and_concurrency(SimpleNamespace(details={"gpus": []}), tiers), (256, 1))
        for details in ({}, {"gpus": [{}]}, {"gpus": [{"vram_total_bytes": "10"}]},
                {"gpus": [{"vram_total_bytes": True}]}, {"gpus": [{"vram_total_bytes": -1}]}):
            with self.subTest(details=details), self.assertRaises(ValidationError):
                host_resolution_and_concurrency(SimpleNamespace(details=details), tiers)
        for invalid in ([], [{}], [{"max_vram_bytes": None, "concurrency": 1}],
                [{"max_vram_bytes": None, "short_edge": 512, "concurrency": True}],
                list(reversed(tiers)), [tiers[0]], [tiers[0], tiers[0]]):
            with self.subTest(tiers=invalid), self.assertRaises(ValidationError):
                host_resolution_and_concurrency(snapshot, invalid)
