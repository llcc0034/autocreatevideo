"""Real preparations/original queue; isolated admission and provider responses.

These tests are not remote Smoke evidence or full recipe/media acceptance.
"""
from copy import deepcopy
from datetime import timedelta
from decimal import Decimal
from unittest.mock import Mock, patch
from uuid import uuid4

from django.core.exceptions import ValidationError
from django.db import transaction
from django.test import TestCase, override_settings
from django.utils import timezone

from studio.models import ArtifactSelectionDecision, AssetGenerationJob, GeneratedArtifact, GenerationAttempt, ProductionRun, SeriesProject, StageRun
from studio.services.async_media_queue import (claim_next_async_media_attempt, process_async_media_attempt,
    recover_stale_async_media_attempts, _succeed, _resolved_actual_cost)
from studio.services.execution_advancement import advance_execution_preparation
from studio.services.execution_production_dispatch import dispatch_production_execution
from studio.services.execution_production_admission import collect_production_execution_admission as actual_admission
from studio.services.execution_production_media import read_prepared_production_media, production_media_peer
from studio.services.execution_preparations import create_execution_instance_preparation
from studio.services.execution_request_preparation import resolve_execution_request_facts
from studio.services.json_contracts import assign_json_contract, json_contract
from studio.services.provider_registry import ProviderDriverError
from studio.tests import test_execution_preparation_creation as fixtures
from studio.tests.test_execution_context_inputs import PROJECT_CONFIG
from studio.tests.test_execution_production_auxiliaries import production_observations


@override_settings(AI_PROVIDER_ALLOWED_HOSTS=("api.example.com", "cdn.example.com"))
class ProductionMediaTests(TestCase):
    setUpTestData = classmethod(fixtures.ExecutionPreparationCreationTests.setUpTestData.__func__)
    write_contract = staticmethod(fixtures.ExecutionPreparationCreationTests.write_contract)
    job_row = fixtures.ExecutionPreparationCreationTests.job_row
    resolve = fixtures.ExecutionPreparationCreationTests.resolve

    def setUp(self):
        import tempfile
        self.media_root = self.enterContext(tempfile.TemporaryDirectory())
        self.enterContext(self.settings(MEDIA_ROOT=self.media_root))
        fixtures.ExecutionPreparationCreationTests.setUp(self)
        self.job.delete()  # Remove only the irrelevant isolated asset fixture.
        self.project = SeriesProject.objects.create(owner=self.actor, title="Prepared media", **PROJECT_CONFIG)
        self.run = ProductionRun.objects.create(owner=self.actor, project=self.project, plan_source="manual",
            status="running", allow_external_calls=True, idempotency_key=str(uuid4()), request_sha256="a" * 64)
        from studio.services.configuration import resolve_configuration
        from studio.services.security_boundaries import boundary_int
        assign_json_contract(self.run, "config_snapshot", resolve_configuration(run=self.run).snapshot(),
            max_bytes=boundary_int("security.snapshot.stage_max_bytes"))
        self.run.save()
        from studio.services.production_run_plans import freeze_run_execution_plan
        from studio.services.production_stage_catalog import production_stage_catalog
        catalog = production_stage_catalog()
        freeze_run_execution_plan(run=self.run, scope={"kind": "project"}, delivery={"kind": "episode_masters"}, initial_content=None,
            stage_catalog_key=catalog.value["key"], expected_stage_catalog_sha256=catalog.sha256, actor=self.actor)
        self.stage = StageRun.objects.create(production_run=self.run, stage_key="media_generation", ordinal=1,
            mode="existing_pipeline", status="running", idempotency_key=str(uuid4()))
        def resolve(definition, stage):
            return resolve_execution_request_facts(definition, self.intent.value, owner=self.actor, unit_key="media-unit",
                max_media_bytes=65536, parameter_policies=[], resolve_parameter_source=lambda _: self.fail("No inferred sources"),
                review_contract=json_contract({"schema_version": 1, "mode": "manual"}),
                project_id=self.project.pk, production_run_id=str(self.run.pk),
                production_scope={"kind": "project", "id": str(self.project.pk)})
        self.resolve_production_inputs = resolve
        self.preparation = create_execution_instance_preparation(owner_ref=("stage_run_id", self.stage.pk),
            version_id=self.version.pk, expected_definition_sha256=self.definition.sha256, unit_key="media-unit", request_key="candidate-1",
            intent=self.intent, resolve_facts=resolve, actor=self.actor)
        for target in ("studio.services.execution_production_admission.collect_production_execution_admission",
                "studio.services.execution_production_dispatch.collect_production_execution_admission"):
            self.enterContext(patch(target, side_effect=production_observations))
        self.preparation = advance_execution_preparation(preparation_id=self.preparation.pk,
            expected_preparation_revision=self.preparation.revision, actor=self.actor)
        self.assertEqual(self.preparation.status, "dispatched", self.preparation.reason)
        self.attempt = GenerationAttempt.objects.get(preparation=self.preparation)

    def submit(self, response=None, effect=None):
        attempt = claim_next_async_media_attempt("isolated-media-worker")
        self.assertIsNotNone(attempt)
        peer = Mock()
        peer.invoke.return_value = response if response is not None else {"request_id": "remote-fixture", "status": "submitted"}
        peer.invoke.side_effect = effect
        with patch("studio.services.async_media_queue.driver_for", return_value=peer):
            result = process_async_media_attempt(attempt)
        return result, peer, attempt

    def test_exact_cancel_submitted_media_uses_original_cancel_queue_and_receipt(self):
        from studio.services.execution_production_cancellation import cancel_production_preparation
        submitted, _, _ = self.submit()
        self.assertEqual(submitted.status, "submitted")
        with self.assertRaisesMessage(ValidationError, "准备已变化"):
            cancel_production_preparation(preparation_id=self.preparation.pk,
                expected_preparation_revision=self.preparation.revision, actor=self.actor)
        self.preparation.refresh_from_db()
        stopped = cancel_production_preparation(preparation_id=self.preparation.pk,
            expected_preparation_revision=self.preparation.revision, actor=self.actor)
        self.assertEqual(stopped.status, "cancelled")
        self.attempt.refresh_from_db()
        self.assertEqual(self.attempt.status, "cancel_requested")
        self.assertEqual(self.attempt.provider_request_id, submitted.provider_request_id)
        before = self.attempt.committed_cost
        outcome, peer, _ = self.submit(response={"terminal_cancelled": True})
        self.assertEqual(outcome.status, "cancelled")
        self.assertEqual(peer.invoke.call_args.args[1], "cancel")
        self.assertIsNone(outcome.actual_cost)
        self.assertEqual(outcome.committed_cost, before)
        self.run.refresh_from_db()
        self.assertEqual(self.run.status, "running")

    def test_successful_primary_cannot_be_cancelled_or_erased(self):
        from studio.services.execution_production_cancellation import cancel_production_preparation, production_cancellation_state
        GenerationAttempt.objects.filter(pk=self.attempt.pk).update(status="succeeded", finished_at=timezone.now())
        self.assertFalse(production_cancellation_state(self.preparation)["allowed"])
        with self.assertRaisesMessage(ValidationError, "主调用已经成功"):
            cancel_production_preparation(preparation_id=self.preparation.pk,
                expected_preparation_revision=self.preparation.revision, actor=self.actor)
        self.attempt.refresh_from_db(); self.preparation.refresh_from_db()
        self.assertEqual((self.attempt.status, self.preparation.status), ("succeeded", "dispatched"))

    def test_original_preparation_scanner_advances_media_but_not_unsubmitted_manual_preparations(self):
        from studio.services.execution_preparation_queue import advance_next_execution_preparation, due_execution_preparations
        from studio.models import ExecutionPreparation
        prep = create_execution_instance_preparation(owner_ref=("stage_run_id", self.stage.pk), version_id=self.version.pk,
            expected_definition_sha256=self.definition.sha256, unit_key="media-unit", request_key="candidate-scanned",
            intent=self.intent, resolve_facts=self.resolve_production_inputs, actor=self.actor)
        self.assertTrue(due_execution_preparations(timezone.now()).filter(pk=prep.pk).exists())
        ExecutionPreparation.objects.filter(pk=prep.pk).update(next_check_at=None)
        self.assertFalse(due_execution_preparations(timezone.now()).filter(pk=prep.pk).exists())
        ExecutionPreparation.objects.filter(pk=prep.pk).update(next_check_at=timezone.now())
        with patch("studio.services.provider_registry._request_json", side_effect=AssertionError("scan cannot make remote calls")), \
                patch("studio.services.execution_parameter_preparation.randbelow", side_effect=AssertionError("scan cannot redraw parameters")):
            result = advance_next_execution_preparation()
        self.assertEqual((result.pk, result.status), (prep.pk, "dispatched"), result.reason)
        attempt = GenerationAttempt.objects.get(preparation=prep)
        self.assertEqual(attempt.status, "queued")
        self.assertIsNone(attempt.submission_started_at)
        self.assertFalse(due_execution_preparations(timezone.now()).filter(pk=prep.pk).exists())
        self.assertFalse(AssetGenerationJob.objects.exists())

    def test_request_reader_and_worker_never_resolve_new_parameters_or_old_routes(self):
        expected = deepcopy(self.preparation.contract.prepared_request["transport"])
        with patch("studio.services.execution_preparation_requests.compile_prepared_execution_transport", side_effect=AssertionError("No recompile")), \
                patch("studio.services.execution_parameter_preparation.randbelow", side_effect=AssertionError("No redraw")):
            first = read_prepared_production_media(self.attempt)
            result, peer, claimed = self.submit()
        self.assertEqual(first["payload"], expected["payload"])
        peer.invoke.assert_called_once()
        self.assertEqual(peer.invoke.call_args.args[1:], ("submit", {
            **expected["payload"], "idempotency_key": f"production-attempt:{self.attempt.pk}"}))
        self.assertEqual(result.status, "submitted")
        self.assertEqual(expected["idempotency_key"], str(self.preparation.pk))
        self.assertNotEqual(str(result.pk), str(self.preparation.pk))
        self.run.refresh_from_db()
        self.assertEqual(self.run.committed_cost_total, Decimal("0.1"))
        self.assertFalse(AssetGenerationJob.objects.exists())
        with patch("studio.services.async_media_queue.driver_for", return_value=peer):
            process_async_media_attempt(claimed)
        self.assertEqual(peer.invoke.call_count, 1)

    def test_replayed_dispatch_cannot_allocate_another_attempt(self):
        result = dispatch_production_execution(preparation_id=self.preparation.pk, expected_preparation_revision=1, actor=self.actor)
        self.assertEqual(result.pk, self.attempt.pk)
        self.assertEqual(GenerationAttempt.objects.count(), 1)

    def test_actual_admission_keeps_root_stop_and_budget_checks_for_media(self):
        self.run.status = "awaiting_input"
        self.run.save(update_fields=("status",))
        request = json_contract(self.preparation.contract.prepared_request)
        facts = actual_admission(self.preparation, self.definition, request)
        codes = {row["code"] for row in facts.hard_blockers}
        self.assertIn("execution_root_not_submittable", codes)
        self.assertNotIn("execution_budget_exceeded", codes)
        self.assertEqual(facts.root_contract.value["budget"]["replaced_reservation"], "0.100000")
        self.assertEqual(facts.root_contract.value["preparation_id"], str(self.preparation.pk))
        self.attempt.refresh_from_db()
        self.assertIsNone(self.attempt.submission_started_at)

    def test_old_snapshot_and_mutated_envelope_are_rejected_without_remote_io(self):
        from studio.services.execution_request_storage import store_prepared_request, reference_prepared_request
        from studio.models import ExecutionPreparationContract
        for value in ({"schema_version": 1, "transport": "restricted_async_rest", "route": {}, "payload": {}},
                {**self.attempt.request_snapshot, "preparation_id": str(uuid4())}):
            detail = ExecutionPreparationContract(preparation_id=self.attempt.preparation_id)
            with self.assertRaises(ValidationError):
                store_prepared_request(detail, value)
        detail = ExecutionPreparationContract(preparation_id=self.attempt.preparation_id)
        store_prepared_request(detail, {"schema_version": 1, "preparation_id": str(self.attempt.preparation_id), "transport": {}})
        reference_prepared_request(self.attempt, detail)
        self.attempt.request_sha256 = self.attempt.request_snapshot_sha256
        self.attempt.save()
        with self.assertRaises(ValidationError):
            read_prepared_production_media(self.attempt)

    def test_changed_endpoint_cannot_receive_original_submit_or_cancel(self):
        config = deepcopy(self.provider.driver_config)
        config["poll_path"] = "/different/jobs/{request_id}"
        self.write_contract(self.provider, "driver_config", config)
        with self.assertRaises(ValidationError):
            production_media_peer(self.attempt)
        result, peer, _ = self.submit()
        self.assertEqual(result.status, "failed")
        peer.invoke.assert_not_called()

    def test_missing_or_oversized_remote_id_is_uncertain_and_never_truncated(self):
        for request_id in (None, False, 123, {}, [], "", " ", "x" * 192):
            with self.subTest(request_id=request_id), transaction.atomic():
                result, peer, _ = self.submit({"request_id": request_id})
                self.assertEqual((result.status, result.error_code, result.provider_request_id), ("failed", "uncertain_remote_response", ""))
                self.assertIsNone(claim_next_async_media_attempt("no-auto-retry"))
                self.run.refresh_from_db()
                self.assertEqual(self.run.committed_cost_total, Decimal("0.1"))
                peer.invoke.assert_called_once()
                transaction.set_rollback(True)

    def test_known_submit_rejection_releases_reservation_without_inventing_a_bill(self):
        result, peer, _ = self.submit(effect=ProviderDriverError("isolated explicit rejection",
            code="provider_request_rejected", submission_uncertain=False))
        self.assertEqual((result.status, result.error_code), ("failed", "provider_request_rejected"))
        self.run.refresh_from_db()
        self.assertEqual((result.committed_cost, self.run.committed_cost_total), (Decimal("0"), Decimal("0")))
        self.assertIsNone(result.actual_cost)
        self.assertFalse(GeneratedArtifact.objects.exists())
        peer.invoke.assert_called_once()

    def test_uncertain_post_and_stale_claim_never_repost(self):
        result, _, _ = self.submit(effect=ProviderDriverError("lost reply", code="lost", submission_uncertain=True))
        self.assertEqual(result.error_code, "uncertain_remote_response")
        self.assertIsNone(claim_next_async_media_attempt("no-auto-retry"))

    def test_deadline_pair_is_frozen_before_claim(self):
        first = read_prepared_production_media(self.attempt)["execution_deadlines"]
        with patch("studio.services.execution_production_media.resolve_configuration_from_snapshot", side_effect=AssertionError("No new deadline")):
            result, _, _ = self.submit()
        self.assertEqual((result.hard_deadline_at - result.deadline_started_at).total_seconds(), first["hard_timeout_seconds"])
        self.assertEqual(read_prepared_production_media(result)["execution_deadlines"], first)
        self.assertEqual(first["configuration_sha256"], self.run.config_snapshot_sha256)

    def test_first_resolution_uses_frozen_run_configuration_not_mutable_project_defaults(self):
        from studio.services.execution_production_media import freeze_production_media_runtime
        from studio.services.configuration import resolve_configuration
        from studio.services.security_boundaries import boundary_int
        snapshot = deepcopy(self.run.config_snapshot)
        snapshot["values"].update({"runtime.task_idle_timeout_seconds": 180, "production.task_timeout_seconds": 900})
        # An explicitly isolated Run snapshot, not a synthetic live revision.
        assign_json_contract(self.run, "config_snapshot", snapshot, max_bytes=boundary_int("security.snapshot.stage_max_bytes"))
        def current_security_only(*args, **kwargs):
            # Existing current system ceilings still apply; business values
            # must come exclusively from the frozen Run, not current defaults.
            self.assertFalse(args)
            self.assertIsNone(kwargs.get("project"))
            self.assertIsNone(kwargs.get("run"))
            self.assertTrue(kwargs.get("required_keys"))
            self.assertTrue(all(key.startswith("security.") for key in kwargs["required_keys"]))
            return resolve_configuration(**kwargs)
        with patch("studio.services.configuration.resolve_configuration", side_effect=current_security_only):
            actual = freeze_production_media_runtime(run=self.run)
        self.assertEqual((actual["idle_timeout_seconds"], actual["hard_timeout_seconds"]), (180, 900))
        self.assertEqual(actual["configuration_sha256"], self.run.config_snapshot_sha256)

    def test_corrupt_request_is_claimed_and_failed_without_poisoning_the_worker_queue(self):
        GenerationAttempt.objects.filter(pk=self.attempt.pk).update(request_snapshot_sha256="0" * 64)
        result, peer, _ = self.submit()
        self.assertEqual((result.status, result.error_code), ("failed", "async_attempt_invalid"))
        peer.invoke.assert_not_called()
        self.assertIsNone(claim_next_async_media_attempt("next-worker"))
        self.run.refresh_from_db()
        self.assertEqual(self.run.committed_cost_total, Decimal("0"))

    def test_cancel_before_submission_releases_only_original_reservation(self):
        GenerationAttempt.objects.filter(pk=self.attempt.pk).update(status="cancel_requested")
        result, peer, _ = self.submit()
        self.assertEqual(result.status, "cancelled")
        peer.invoke.assert_not_called()
        self.run.refresh_from_db()
        self.assertEqual(self.run.committed_cost_total, Decimal("0"))
        self.assertIsNone(result.actual_cost)

    def artifact(self, attempt):
        artifact = GeneratedArtifact(production_run=self.run, stage_run=self.stage, generation_attempt=attempt,
            manifest_key=str(uuid4()), kind="video", role="primary_generation_output", variant_group="isolated-group", variant_index=1,
            status="available", object_key="isolated-media-fixture", mime_type="video/mp4", size_bytes=1, sha256="b" * 64)
        assign_json_contract(artifact, "manifest_metadata", {"schema_version": 1, "scope": "isolated settlement, not decoded media"})
        artifact.save()
        return artifact

    def test_success_preserves_candidates_and_unknown_bill_is_not_an_estimate(self):
        result, _, _ = self.submit()
        claimed = claim_next_async_media_attempt("isolated-collector")
        artifact = self.artifact(claimed)
        self.assertEqual(_resolved_actual_cost(claimed, {}, self.provider.driver_config), (None, "USD"))
        result = _succeed(claimed.pk, claimed.claim_token, artifacts=[artifact], actual_cost=None, currency="USD")
        artifact.refresh_from_db()
        self.run.refresh_from_db()
        self.assertEqual((result.status, artifact.status), ("succeeded", "available"))
        self.assertFalse(ArtifactSelectionDecision.objects.filter(stage_run=self.stage).exists())
        self.assertIsNone(result.actual_cost)
        self.assertEqual(self.run.committed_cost_total, Decimal("0.1"))
        self.assertEqual(self.run.actual_cost_total, Decimal("0"))

    def test_cancelled_run_settles_real_bill_but_rejects_late_candidate(self):
        self.submit()
        claimed = claim_next_async_media_attempt("isolated-collector")
        artifact = self.artifact(claimed)
        self.run.status = "cancelled"
        self.run.save(update_fields=("status",))
        result = _succeed(claimed.pk, claimed.claim_token, artifacts=[artifact], actual_cost=Decimal("0.03"), currency="USD")
        artifact.refresh_from_db()
        self.run.refresh_from_db()
        self.assertEqual((result.status, artifact.status), ("cancelled", "rejected"))
        self.assertEqual(self.run.actual_cost_total, Decimal("0.03"))
        self.assertEqual(self.run.committed_cost_total, Decimal("0"))

    def test_actual_overrun_preserves_candidate_and_stops_for_budget_review(self):
        self.submit()
        claimed = claim_next_async_media_attempt("isolated-cost-collector")
        artifact = self.artifact(claimed)  # Settlement-only fixture, not media acceptance.
        result = _succeed(claimed.pk, claimed.claim_token, artifacts=[artifact], actual_cost=Decimal("1.25"), currency="USD")
        self.run.refresh_from_db()
        self.stage.refresh_from_db()
        artifact.refresh_from_db()
        self.assertEqual((result.status, artifact.status), ("succeeded", "available"))
        self.assertEqual((result.committed_cost, self.run.committed_cost_total), (Decimal("0"), Decimal("0")))
        self.assertEqual(self.run.actual_cost_total, Decimal("1.25"))
        self.assertEqual((self.run.status, self.stage.reason_code), ("awaiting_approval", "actual_budget_exceeded"))
        self.assertFalse(ArtifactSelectionDecision.objects.exists())

    def test_frozen_transport_timeout_prevents_early_submitting_claim_recovery(self):
        deployment = production_media_peer(self.attempt)
        seconds = float(deployment.profile_version.provider.driver_config["timeout_seconds"])
        observed = timezone.now()
        heartbeat = observed - timedelta(seconds=seconds / 2)
        token = uuid4()
        GenerationAttempt.objects.filter(pk=self.attempt.pk).update(status="submitting", claim_token=token,
            claimed_at=heartbeat, heartbeat_at=heartbeat, submission_started_at=heartbeat)
        before = deepcopy(self.attempt.request_snapshot)
        with patch("studio.services.async_media_queue.timezone.now", return_value=observed):
            result = recover_stale_async_media_attempts(minutes=seconds / 240)
        self.assertEqual(result, {"recovered": 0, "uncertain": 0})
        self.attempt.refresh_from_db()
        self.assertEqual((self.attempt.status, self.attempt.claim_token), ("submitting", token))
        self.assertEqual(self.attempt.request_snapshot, before)

    def test_confirmed_remote_submission_resumes_same_attempt_and_api_hides_request(self):
        from rest_framework.test import APIClient
        from studio.services.production_runs import resolve_uncertain_attempt
        result, _, _ = self.submit(effect=ProviderDriverError("isolated lost acknowledgement", submission_uncertain=True))
        before = deepcopy(result.request_snapshot)
        resolve_uncertain_attempt(self.run, attempt_id=result.pk, outcome="confirmed_remote_submitted",
            note="隔离测试确认远端已接收原请求，继续轮询而非重提。", user=self.actor,
            provider_request_id="confirmed-original-request")
        result.refresh_from_db()
        self.assertEqual((result.status, result.provider_request_id), ("submitted", "confirmed-original-request"))
        self.assertIsNotNone(result.next_poll_at)
        self.assertEqual(result.request_snapshot, before)
        self.assertEqual(GenerationAttempt.objects.count(), 1)
        client = APIClient()
        client.force_authenticate(self.actor)
        response = client.get(f"/api/v1/generation-attempts/{result.pk}/")
        self.assertEqual(response.status_code, 200, response.data)
        self.assertNotIn("request_snapshot", response.data)

    def test_stale_submitting_claim_records_uncertain_without_returning_to_queue(self):
        old = timezone.now() - timedelta(days=1)
        GenerationAttempt.objects.filter(pk=self.attempt.pk).update(status="submitting", claim_token=uuid4(), heartbeat_at=old,
            submission_started_at=old)
        result = recover_stale_async_media_attempts(minutes=1)
        self.assertEqual(result["uncertain"], 1)
        self.attempt.refresh_from_db()
        self.assertEqual(self.attempt.error_code, "uncertain_remote_response")
        self.assertIsNone(claim_next_async_media_attempt("no-auto-retry"))

    def test_download_failure_preserves_real_bill_without_claiming_media_success(self):
        from studio.services.async_media_queue import _collect
        self.submit(response={"request_id": "remote-fixture", "status": self.provider.driver_config["terminal_success"][0]})
        claimed = claim_next_async_media_attempt("isolated-collector")
        peer = Mock()
        peer.invoke.return_value = {"request_id": "remote-fixture", "artifacts": [], "actual_cost": "0.025", "currency": "USD"}
        with patch("studio.services.async_media_queue.archive_remote_artifacts",
                side_effect=ProviderDriverError("isolated download failure", code="artifact_archive_failed")):
            result = _collect(claimed, claimed.claim_token, read_prepared_production_media(claimed), self.deployment, peer)
        self.run.refresh_from_db()
        self.assertEqual((result.status, result.actual_cost, self.run.actual_cost_total), ("failed", Decimal("0.025"), Decimal("0.025")))
        self.assertEqual(self.run.committed_cost_total, Decimal("0"))
        self.assertFalse(GeneratedArtifact.objects.exists())

    def test_cancel_during_collect_retains_bill_and_does_not_archive_late_media(self):
        from studio.services.async_media_queue import _collect
        self.submit()
        claimed = claim_next_async_media_attempt("isolated-collector")
        peer = Mock()
        def late(*args):
            ProductionRun.objects.filter(pk=self.run.pk).update(status="cancelled")
            return {"request_id": "remote-fixture", "artifacts": [{"kind": "video"}], "actual_cost": "0.04", "currency": "USD"}
        peer.invoke.side_effect = late
        with patch("studio.services.async_media_queue.archive_remote_artifacts",
                side_effect=ProviderDriverError("cancelled during transfer", code="late_result_ignored")):
            result = _collect(claimed, claimed.claim_token, read_prepared_production_media(claimed), self.deployment, peer)
        self.run.refresh_from_db()
        self.assertEqual((result.status, result.actual_cost, self.run.actual_cost_total), ("cancelled", Decimal("0.04"), Decimal("0.04")))
        self.assertEqual(self.run.committed_cost_total, Decimal("0"))
        self.assertFalse(GeneratedArtifact.objects.exists())

    def test_bill_replay_is_idempotent_and_conflicting_or_wrong_currency_never_overwrites(self):
        from studio.services.async_media_queue import _record_actual_bill
        self.submit()
        claimed = claim_next_async_media_attempt("isolated-collector")
        for _ in range(2):
            _record_actual_bill(claimed.pk, claimed.claim_token, Decimal("0.03"), "USD")
        with self.assertRaises(ValidationError):
            _record_actual_bill(claimed.pk, claimed.claim_token, Decimal("0.08"), "USD")
        with self.assertRaises(ProviderDriverError):
            _record_actual_bill(claimed.pk, claimed.claim_token, Decimal("0.03"), "EUR")
        self.run.refresh_from_db()
        self.assertEqual(self.run.actual_cost_total, Decimal("0.03"))
        self.assertEqual(self.run.committed_cost_total, Decimal("0"))

    def test_no_candidate_or_foreign_candidate_cannot_mark_attempt_successful(self):
        self.submit()
        claimed = claim_next_async_media_attempt("isolated-collector")
        for rows in ([], [self.artifact(claimed)]):
            if rows:
                GeneratedArtifact.objects.filter(pk=rows[0].pk).update(generation_attempt=None)
            with self.assertRaises(ValidationError):
                _succeed(claimed.pk, claimed.claim_token, artifacts=rows, actual_cost=None, currency="USD")
        claimed.refresh_from_db()
        self.assertEqual(claimed.status, "submitted")

    def image_attempt(self, *, bible=False):
        from studio.services.execution_instances import create_execution_instance, create_execution_draft, publish_execution_version
        from studio.services.execution_definitions import build_execution_definition
        GenerationAttempt.objects.filter(pk=self.attempt.pk).update(status="cancel_requested")
        self.submit()
        stage = self.stage
        scope = {"kind": "project", "id": str(self.project.pk)}
        if bible:
            from studio.models import PropBible
            from studio.services.production_stage_catalog import production_stage_catalog
            entity = PropBible.objects.create(project=self.project, slug="image-target", name="铜钥匙", description="方柄铜钥匙")
            declaration = next(row for row in production_stage_catalog().value["stages"] if row["key"] == "bible_image_generation")
            stage = StageRun.objects.create(production_run=self.run, stage_key=declaration["key"], ordinal=declaration["ordinal"],
                mode="existing_pipeline", status="running", idempotency_key=str(uuid4()))
            scope = {"kind": "prop", "id": str(entity.pk)}
        instance = create_execution_instance(key="isolated-image", name="Image fixture", description="", actor=self.actor)
        from studio.models import ModelProfileVersion, ModelDeployment
        specification = deepcopy(self.model.specification)
        specification.pop("conditioning_contract")
        specification["capabilities"] = ["image.text_to_image"]
        specification["async_media"]["operations"] = {"image.text_to_image": specification["async_media"]["operations"]["image.text_to_image"]}
        model = ModelProfileVersion(profile_key="isolated-image", version=1, model_identifier="image-fixture", modality="image",
            provider=self.provider, status="published")
        self.write_contract(model, "specification", specification)
        deployment = ModelDeployment(key="isolated-image", name="Image fixture", profile_version=model)
        self.write_contract(deployment, "runtime_config", {})
        version = create_execution_draft(instance_id=instance.pk, expected_revision=instance.revision, actor=self.actor,
            selection={"deployment": deployment, "operation": "image.text_to_image", "mode": "", "prompt_mode": "manual",
                "prompt_profile_version": None, "workflow_version": None})
        definition = build_execution_definition(version.pk)
        version = publish_execution_version(version_id=version.pk, expected_revision=version.revision,
            expected_definition_sha256=definition.sha256, actor=self.actor)
        requested = {**self.intent.value, "parameters": {"width": 64, "height": 64}}
        def resolve(definition, stage):
            return resolve_execution_request_facts(definition, requested, owner=self.actor, unit_key="image-unit",
                max_media_bytes=65536, parameter_policies=[], resolve_parameter_source=lambda _: self.fail("No source lock"),
                review_contract=json_contract({"schema_version": 1, "mode": "manual"}),
                project_id=self.project.pk, production_run_id=str(self.run.pk),
                production_scope=scope)
        prep = create_execution_instance_preparation(owner_ref=("stage_run_id", stage.pk), version_id=version.pk,
            expected_definition_sha256=definition.sha256, unit_key="image-unit", request_key="candidate-image", intent=json_contract(requested),
            resolve_facts=resolve, actor=self.actor)
        prep = advance_execution_preparation(preparation_id=prep.pk, expected_preparation_revision=prep.revision, actor=self.actor)
        self.assertEqual(prep.status, "dispatched", prep.reason)
        self.submit()
        claimed = claim_next_async_media_attempt("isolated-image-collector")
        self.assertEqual(claimed.preparation_id, prep.pk)
        return prep, claimed

    @staticmethod
    def download_image(**kwargs):
        from PIL import Image
        from studio.services.generated_artifacts import verified_local_media_facts
        # Simulate only HTTP transfer. The original archive and image decoder
        # operate on actual temporary PNG bytes and formal Artifact rows.
        path = kwargs["target"]
        path.parent.mkdir(parents=True, exist_ok=True)
        Image.new("RGB", (64, 64), "blue").save(path, format="PNG")
        return verified_local_media_facts(path, expected_kind="image", max_bytes=kwargs["max_bytes"])

    def test_image_archive_uses_exact_preparation_and_decodable_bytes_without_prompt_artifact(self):
        from pathlib import Path
        from studio.services.remote_artifacts import archive_remote_artifacts
        prep, claimed = self.image_attempt()
        directory = self.media_root
        with patch("studio.services.remote_artifacts._download_one", side_effect=self.download_image) as transfer:
            rows = archive_remote_artifacts(claimed, artifacts=[{"kind": "image", "url": "https://cdn.example.com/isolated.png"}],
                provider_config=self.provider.driver_config, expected_kind="image")
            self.assertEqual(len(rows), 1)
            artifact = rows[0]
            self.assertTrue((Path(directory) / artifact.object_key).is_file())
            self.assertEqual(artifact.manifest_metadata["preparation_id"], str(prep.pk))
            self.assertNotIn("prompt_artifact_id", artifact.manifest_metadata)
            self.assertEqual(artifact.comparison_parameters["generation"], {"width": 64, "height": 64})
            replay = archive_remote_artifacts(claimed, artifacts=[{"kind": "image", "url": "https://cdn.example.com/isolated.png"}],
                provider_config=self.provider.driver_config, expected_kind="image")
            self.assertEqual(replay[0].pk, artifact.pk)
            self.assertEqual(transfer.call_count, 1)
            _succeed(claimed.pk, claimed.claim_token, artifacts=rows, actual_cost=Decimal("0.02"), currency="USD")
            artifact.refresh_from_db()
            self.assertEqual(artifact.status, "available")
            self.assertFalse(ArtifactSelectionDecision.objects.exists())
            from studio.services.execution_production_candidates import read_production_media_candidate
            detail = read_production_media_candidate(artifact, max_media_bytes=65536)
            self.assertEqual(detail["preparation_id"], str(prep.pk))
            self.assertEqual(detail["production_unit"]["scope"], {"kind": "project", "id": str(self.project.pk)})
            self.assertEqual(detail["media_facts"]["sha256"], artifact.sha256)

    def image_collect_claim(self, *, bible=False):
        prep, claimed = self.image_attempt(bible=bible)
        peer = Mock()
        peer.invoke.return_value = {"status": self.provider.driver_config["terminal_success"][0], "terminal_success": True}
        with patch("studio.services.async_media_queue.driver_for", return_value=peer):
            polled = process_async_media_attempt(claimed)
        self.assertEqual(polled.status, "submitted")
        peer.invoke.assert_called_once()
        self.assertEqual(peer.invoke.call_args.args[1], "poll")
        self.assertFalse(GeneratedArtifact.objects.exists())
        GenerationAttempt.objects.filter(pk=polled.pk).update(next_poll_at=timezone.now())
        collecting = claim_next_async_media_attempt("isolated-image-collector")
        self.assertEqual(collecting.pk, claimed.pk)
        return prep, collecting

    def test_original_image_worker_polls_then_collects_actual_bytes_without_automatic_selection(self):
        from pathlib import Path
        from PIL import Image
        prep, claimed = self.image_collect_claim()
        peer = Mock()
        peer.invoke.return_value = {"request_id": "remote-fixture", "artifacts": [{"kind": "image", "url": "https://cdn.example.com/isolated.png"}],
            "actual_cost": "0.02", "currency": "USD"}
        directory = self.media_root
        with patch("studio.services.async_media_queue.driver_for", return_value=peer), \
                patch("studio.services.remote_artifacts._download_one", side_effect=self.download_image):
            result = process_async_media_attempt(claimed)
            self.assertEqual(result.status, "succeeded", result.error_message)
            artifact = GeneratedArtifact.objects.get(generation_attempt=result)
            self.assertEqual((artifact.status, artifact.kind), ("available", "image"))
            self.assertEqual(artifact.manifest_metadata["preparation_id"], str(prep.pk))
            with Image.open(Path(directory) / artifact.object_key) as image:
                self.assertEqual(image.size, (64, 64))
            peer.invoke.assert_called_once()
            self.assertEqual(peer.invoke.call_args.args[1], "collect")
            self.assertFalse(ArtifactSelectionDecision.objects.exists())
            self.run.refresh_from_db()
            self.assertEqual((self.run.actual_cost_total, self.run.committed_cost_total), (Decimal("0.02"), Decimal("0")))

    def test_cancel_during_actual_image_download_removes_unmanaged_bytes_and_keeps_bill(self):
        from pathlib import Path
        _, claimed = self.image_collect_claim()
        peer = Mock()
        peer.invoke.return_value = {"request_id": "remote-fixture", "artifacts": [{"kind": "image", "url": "https://cdn.example.com/isolated.png"}],
            "actual_cost": "0.04", "currency": "USD"}
        downloaded = []
        def cancel_after_download(**kwargs):
            facts = self.download_image(**kwargs)
            downloaded.append(kwargs["target"])
            self.assertTrue(downloaded[-1].is_file())
            ProductionRun.objects.filter(pk=self.run.pk).update(status="cancelled")
            return facts
        directory = self.media_root
        with patch("studio.services.async_media_queue.driver_for", return_value=peer), \
                patch("studio.services.remote_artifacts._download_one", side_effect=cancel_after_download):
            result = process_async_media_attempt(claimed)
            self.assertEqual((result.status, result.error_code), ("cancelled", "late_result_ignored"))
            self.assertEqual(len(downloaded), 1)
            self.assertFalse(downloaded[0].exists())
            self.assertFalse(any(path.is_file() for path in (Path(directory) / "generated-artifacts").rglob("*")))
            from studio.models import ExecutionTextResponseReview
            self.assertTrue(ExecutionTextResponseReview.objects.get(attempt=result).original_text)
            self.assertFalse(GeneratedArtifact.objects.exists())
        self.run.refresh_from_db()
        self.assertEqual((self.run.actual_cost_total, self.run.committed_cost_total), (Decimal("0.04"), Decimal("0")))

    def test_bible_image_worker_rejects_multiple_outputs_before_downloading_anything(self):
        _, claimed = self.image_collect_claim(bible=True)
        peer = Mock()
        peer.invoke.return_value = {"request_id": "remote-fixture", "artifacts": [
            {"kind": "image", "url": "https://cdn.example.com/one.png"},
            {"kind": "image", "url": "https://cdn.example.com/two.png"}], "actual_cost": "0.02", "currency": "USD"}
        with patch("studio.services.async_media_queue.driver_for", return_value=peer), \
                patch("studio.services.remote_artifacts._download_one", side_effect=AssertionError("No partial downloads")) as transfer:
            result = process_async_media_attempt(claimed)
        self.assertEqual((result.status, result.error_code), ("failed", "artifact_response_invalid"))
        transfer.assert_not_called()
        self.assertFalse(GeneratedArtifact.objects.exists())
        self.run.refresh_from_db()
        self.assertEqual((self.run.actual_cost_total, self.run.committed_cost_total), (Decimal("0.02"), Decimal("0")))
