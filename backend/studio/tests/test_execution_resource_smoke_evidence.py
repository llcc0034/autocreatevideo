"""Isolated prepared resource outcomes; no public cutover or live model calls."""
from copy import deepcopy
from datetime import timedelta
import json
from unittest.mock import Mock, patch
from uuid import uuid4

from django.core.exceptions import ValidationError
from django.test import TestCase, override_settings
from django.utils import timezone

from studio.models import ExecutionCapacityObservation, ModelDeploymentCapabilitySnapshot, ModelDeploymentSmokeAttempt
from studio.services.execution_finalization import ExecutionAdmissionFacts, finalize_execution
from studio.services.execution_smoke_evidence import finish_prepared_smoke, read_prepared_smoke_fact, read_instance_smoke_facts
from studio.services.execution_deployment_resource_evidence import read_resource_deployment_snapshot, current_resource_smoke
from studio.services.deployment_smoke_queue import claim_next_deployment_smoke, process_deployment_smoke, recover_stale_deployment_smokes
from studio.services.json_contracts import assign_json_contract, json_contract
from studio.tests import test_execution_resource_selection as fixtures


@override_settings(AI_PROVIDER_ALLOWED_HOSTS=("api.example.com",),
    STORAGES={"default": {"BACKEND": "django.core.files.storage.InMemoryStorage"}})
class ResourcePreparedEvidenceTests(TestCase):
    setUpTestData = fixtures.ExecutionResourceSelectionTests.__dict__["setUpTestData"]
    setUp = fixtures.ExecutionResourceSelectionTests.setUp
    make_resource_profile = fixtures.ExecutionResourceSelectionTests.make_resource_profile
    prepare_resource = fixtures.ExecutionResourceSelectionTests.prepare_resource

    def attempt(self, *, mode="manual", submitting=True):
        prep, attempt, _ = self.prepare_resource(mode)
        # This fixture isolates the saved-request/result protocol. Real
        # admission and worker advancement are covered by separate integration tests.
        def isolated_root_facts(prep, definition, request):
            return ExecutionAdmissionFacts((), "unknown", [], json_contract({"preparation_id": str(prep.pk),
                "request_sha256": request.sha256, "test_scope": "isolated resource result protocol"}))
        prep = finalize_execution(preparation_id=prep.pk, expected_preparation_revision=prep.revision,
            actor=self.actor, collect_admission_facts=isolated_root_facts)
        self.assertEqual(prep.status, "ready", prep.reason)
        attempt.request_sha256 = prep.request_sha256
        attempt.status, attempt.claim_token = ("submitting" if submitting else "queued"), uuid4()
        attempt.submission_started_at = timezone.now() if submitting else None
        attempt.save()
        return attempt, prep

    def finish(self, attempt, *, status="succeeded", **kwargs):
        return finish_prepared_smoke(attempt, attempt.claim_token, status=status,
            evidence=json_contract({"schema_version": 1, "isolated_worker_outcome": True,
                "execution_submitted": attempt.submission_started_at is not None,
                "response_sha256": "a" * 64, "response_size_bytes": 10, "result": {"isolated_fixture": True}}), **kwargs)

    def test_resource_result_has_null_version_and_cannot_promote_combination_or_capacity(self):
        attempt, prep = self.attempt(mode="profile")
        frozen = deepcopy(prep.contract.prepared_request)
        done = self.finish(attempt)
        fact = read_prepared_smoke_fact(done)
        self.assertEqual(done.result["scope"], "resource_smoke")
        self.assertEqual(fact.identity.scope, "resource_smoke")
        self.assertIsNone(fact.identity.version_id)
        self.assertEqual(fact.expires_at - fact.finished_at, timedelta(seconds=3600))
        self.assertEqual(done.result["request_sha256"], prep.request_sha256)
        self.assertEqual(read_instance_smoke_facts(self.version, self.definition), ())
        self.assertFalse(ExecutionCapacityObservation.objects.exists())
        self.assertIsNotNone(done.final_snapshot_id)
        self.assertEqual(ModelDeploymentCapabilitySnapshot.objects.count(), 1)
        self.assertEqual(read_resource_deployment_snapshot(done.final_snapshot, current=True).pk, done.pk)
        self.assertEqual(done.final_snapshot.evidence["prompt_profile"]["id"], str(done.prompt_profile_version_id))
        self.assertNotIn("prompt_profile_binding", {field.name for field in done.final_snapshot._meta.fields})
        self.assertEqual(done.final_snapshot.expires_at, done.evidence_expires_at)
        self.assertEqual(done.final_snapshot.evidence["result_sha256"], done.result_sha256)
        self.assertNotIn("result", done.final_snapshot.evidence)
        self.assertNotIn("prompt_profile_binding", {field.name for field in done._meta.fields})
        prep.refresh_from_db()
        self.assertEqual(prep.contract.prepared_request, frozen)

    def test_health_recorder_cannot_manufacture_submitted_or_profile_smoke(self):
        from studio.services.deployment_smoke import record_deployment_capability_snapshot
        for probe_kind, evidence in (
            ("smoke", {"execution_submitted": True}),
            ("health", {"execution_submitted": True}),
            ("health", {"execution_submitted": False, "prompt_profile": {"id": str(uuid4())}}),
        ):
            with self.subTest(probe_kind=probe_kind, evidence=evidence), self.assertRaises(ValidationError):
                record_deployment_capability_snapshot(self.deployment, probe_kind=probe_kind,
                    status="passed", operations=["prompt.normalize"], evidence=evidence)
        self.assertFalse(ModelDeploymentCapabilitySnapshot.objects.exists())
        self.assertFalse(ModelDeploymentSmokeAttempt.objects.exists())

    def test_recommendation_reads_latest_original_resource_outcome_not_old_pass_or_binding(self):
        from django.db import connection
        from django.test.utils import CaptureQueriesContext
        attempt, _ = self.attempt(mode="profile")
        done = self.finish(attempt)
        with CaptureQueriesContext(connection) as queries:
            self.assertEqual(current_resource_smoke(self.deployment, done.operation).pk, done.pk)
        self.assertFalse(any("promptprofilebinding" in row["sql"].lower() for row in queries))
        index_read = queries[0]["sql"]
        self.assertNotIn('"request_snapshot"', index_read)
        self.assertNotIn('"result"', index_read)
        self.assertIn("'resource'", index_read)
        query = ModelDeploymentSmokeAttempt.objects.filter(deployment=self.deployment, operation=done.operation,
            scope="resource", status__in=("succeeded", "failed", "uncertain"), finished_at__isnull=False)
        self.assertIn("INDEX", query.explain().upper())
        self.assertIsNone(current_resource_smoke(self.deployment, "undeclared.operation"))
        with patch("studio.services.execution_deployment_resource_evidence.timezone.now", return_value=done.evidence_expires_at):
            self.assertIsNone(current_resource_smoke(self.deployment, done.operation))
        failed, _ = self.attempt()
        self.finish(failed, status="failed")
        self.assertIsNone(current_resource_smoke(self.deployment, done.operation))
        self.assertEqual(ModelDeploymentCapabilitySnapshot.objects.count(), 2)

    def test_recommendation_rejects_damaged_original_resource_result_without_repair(self):
        attempt, _ = self.attempt()
        done = self.finish(attempt)
        ModelDeploymentSmokeAttempt.objects.filter(pk=done.pk).update(result_sha256="0" * 64)
        with self.assertRaises(ValidationError):
            current_resource_smoke(self.deployment, done.operation)
        done.refresh_from_db()
        self.assertEqual(done.result_sha256, "0" * 64)

    def test_current_resource_evidence_read_does_not_acquire_registry_write_guard(self):
        attempt, _ = self.attempt(mode="profile")
        done = self.finish(attempt)
        with patch("studio.services.registry_reference_lock.registry_reference_write_guard",
                side_effect=AssertionError("read-only evidence must not lock database writers")):
            self.assertEqual(current_resource_smoke(self.deployment, done.operation).pk, done.pk)

    def test_recommendation_rejects_result_disappearing_between_reads(self):
        attempt, _ = self.attempt()
        done = self.finish(attempt)
        with patch.object(ModelDeploymentCapabilitySnapshot.objects, "get",
                side_effect=ModelDeploymentCapabilitySnapshot.DoesNotExist):
            with self.assertRaisesMessage(ValidationError, "原结果已不存在"):
                current_resource_smoke(self.deployment, done.operation)
        done.refresh_from_db()
        self.assertEqual(done.status, "succeeded")
        self.assertEqual(current_resource_smoke(self.deployment, done.operation).pk, done.pk)

    def test_text_resource_recommendation_uses_original_smoke_and_not_old_passes(self):
        from studio.services.runtime_platform import _candidate_state
        def read():
            return _candidate_state(snapshot=None, deployment=self.deployment, workflow=None, availability=[], max_vram_bytes=0)
        self.assertEqual(read(), ("smoke_test_required", "text_resource_smoke_required"))
        attempt, _ = self.attempt()
        done = self.finish(attempt)
        self.assertEqual(read(), ("compatible", "text_resource_smoke_passed"))
        with patch("studio.services.execution_deployment_resource_evidence.timezone.now", return_value=done.evidence_expires_at):
            self.assertEqual(read(), ("smoke_test_required", "text_resource_smoke_required"))
        ModelDeploymentSmokeAttempt.objects.filter(pk=done.pk).update(result_sha256="0" * 64)
        self.assertEqual(read(), ("unavailable", "resource_smoke_evidence_invalid"))
        failed, _ = self.attempt()
        self.finish(failed, status="failed")
        self.assertEqual(read(), ("smoke_test_required", "text_resource_smoke_required"))

    def test_resource_cannot_record_instance_capacity_or_replace_terminal_result(self):
        attempt, _ = self.attempt()
        with self.assertRaisesMessage(ValidationError, "不能写入"):
            self.finish(attempt, capacity_request={"fabricated": "instance capacity"})
        attempt.refresh_from_db()
        self.assertIsNone(attempt.finished_at)
        done = self.finish(attempt)
        self.assertIsNone(done.claim_token)
        late = self.finish(attempt, status="failed")
        self.assertEqual(late.result_sha256, done.result_sha256)
        self.assertEqual(late.status, "succeeded")
        self.assertEqual(ModelDeploymentCapabilitySnapshot.objects.count(), 1)

    def test_stale_claim_and_cancellation_do_not_create_pass(self):
        attempt, _ = self.attempt()
        ModelDeploymentSmokeAttempt.objects.filter(pk=attempt.pk).update(claim_token=uuid4())
        stale = self.finish(attempt)
        self.assertIsNone(stale.finished_at)
        stale.status = "cancel_requested"
        stale.save(update_fields=["status"])
        done = self.finish(stale)
        self.assertEqual(done.status, "cancelled")
        self.assertIsNone(read_prepared_smoke_fact(done).expires_at)

    def test_success_cannot_be_reported_before_submit_and_failure_is_explicit(self):
        attempt, _ = self.attempt(submitting=False)
        with self.assertRaisesMessage(ValidationError, "尚未进入"):
            self.finish(attempt)
        done = self.finish(attempt, status="failed")
        self.assertEqual(read_prepared_smoke_fact(done).status, "failed")
        self.assertIsNone(done.evidence_expires_at)

    def test_changed_configuration_does_not_relabel_saved_scope_or_refresh_ttl(self):
        attempt, _ = self.attempt()
        done = self.finish(attempt)
        fact = read_prepared_smoke_fact(done)
        provider = self.deployment.provider
        assign_json_contract(provider, "driver_config", {**provider.driver_config, "capability_ttl_seconds": 7200})
        provider.save()
        self.assertEqual(read_prepared_smoke_fact(done), fact)
        self.assertEqual(read_resource_deployment_snapshot(done.final_snapshot, current=True).pk, done.pk)
        corrupt = deepcopy(done.result)
        corrupt["scope"] = "instance_smoke"
        assign_json_contract(done, "result", corrupt)
        with self.assertRaisesMessage(ValidationError, "不一致"):
            read_prepared_smoke_fact(done)

    def test_resource_snapshot_rejects_changed_target_and_rehashed_fake_result_pointer(self):
        attempt, _ = self.attempt()
        done = self.finish(attempt)
        snapshot = done.final_snapshot
        provider = self.deployment.provider
        assign_json_contract(provider, "driver_config", {**provider.driver_config, "base_url": "https://api.example.com/changed"})
        provider.save()
        self.assertEqual(read_resource_deployment_snapshot(snapshot).pk, done.pk)
        with self.assertRaises(ValidationError):
            read_resource_deployment_snapshot(snapshot, current=True)
        assign_json_contract(snapshot, "evidence", {**snapshot.evidence, "result_sha256": "0" * 64}, schema_version=2)
        with self.assertRaisesMessage(ValidationError, "不一致"):
            read_resource_deployment_snapshot(snapshot)

    def test_resource_overview_uses_exact_profile_and_latest_failure_without_binding_or_fallback(self):
        from rest_framework.test import APIClient
        client = APIClient()
        client.force_authenticate(self.actor)
        attempt, _ = self.attempt(mode="profile")
        done = self.finish(attempt)
        from importlib.util import find_spec
        self.assertIsNone(find_spec("studio.services.deployment_smoke_profiles"))

        def read():
            response = client.get("/api/v1/acceptance-smoke/", {"subject": "deployment", "deployment_id": str(self.deployment.pk)})
            self.assertEqual(response.status_code, 200, response.data)
            operations = response.data["results"][0]["deployment_smoke"]["operations"]
            return next(row for row in operations if row["operation"] == attempt.operation)

        row = read()
        self.assertEqual(row["evidence_state"], "fresh")
        self.assertEqual(row["latest_snapshot"]["prompt_profile"]["id"], str(done.prompt_profile_version_id))
        self.assertEqual(row["latest_snapshot"]["prompt_profile"]["name"], done.prompt_profile_version.name)
        self.assertNotIn("prompt_profile_binding_id", str(row))
        self.assertEqual(len(row["prompt_profiles"]), 1)
        response = client.get(f"/api/v1/model-deployments/{self.deployment.pk}/")
        self.assertEqual(response.status_code, 200, response.data)
        self.assertTrue(response.data["latest_smoke"]["fresh"])
        # Same capability's next explicit manual sample fails. Do not retrieve
        # the earlier successful profile sample as its latest overall result.
        failed, _ = self.attempt()
        failed = self.finish(failed, status="failed")
        row = read()
        self.assertEqual(row["evidence_state"], "failed")
        self.assertEqual(row["latest_snapshot"]["id"], str(failed.final_snapshot_id))
        self.assertIsNone(row["fresh_passed_snapshot"])
        self.assertTrue(row["action"]["enabled"])
        snapshot = failed.final_snapshot
        assign_json_contract(snapshot, "evidence", {**snapshot.evidence, "result_sha256": "0" * 64}, schema_version=2)
        snapshot.save()
        row = read()
        self.assertEqual(row["evidence_state"], "unverified")
        self.assertIsNone(row["fresh_passed_snapshot"])
        self.assertTrue(row["action"]["enabled"])  # Damaged evidence must allow a new explicit test.
        query = ModelDeploymentCapabilitySnapshot.objects.filter(deployment=self.deployment, probe_kind="smoke")
        self.assertIn("INDEX", query.explain().upper())

    def test_preparing_resource_reserves_action_without_starting_remote_clock(self):
        from rest_framework.test import APIClient
        prep, attempt, _ = self.prepare_resource("manual")
        client = APIClient()
        client.force_authenticate(self.actor)
        response = client.get("/api/v1/acceptance-smoke/", {"subject": "deployment", "deployment_id": str(self.deployment.pk)})
        self.assertEqual(response.status_code, 200, response.data)
        operations = response.data["results"][0]["deployment_smoke"]["operations"]
        row = next(item for item in operations if item["operation"] == attempt.operation)
        self.assertEqual(row["latest_attempt"]["status"], "preparing")
        self.assertFalse(row["action"]["enabled"])
        self.assertEqual(row["action"]["reason_code"], "attempt_in_progress_or_resolution_required")
        attempt.refresh_from_db()
        self.assertEqual(prep.status, "preparing")
        self.assertIsNone(attempt.submission_started_at)
        self.assertIsNone(attempt.hard_deadline_at)

    def test_overview_retains_independent_capability_expiry_failure_and_current_attempt(self):
        from dataclasses import replace
        from rest_framework.test import APIClient
        from studio.tests.test_execution_text_contracts import text_specification
        text_contract = text_specification()
        model = self.deployment.profile_version
        assign_json_contract(model, "specification", {**model.specification,
            "capabilities": ["script.generate", *text_contract["capabilities"]],
            "text_execution": text_contract["text_execution"]})
        model.save()
        self.selection = replace(self.selection, deployment=self.deployment)
        first, _ = self.attempt(mode="profile")
        first = self.finish(first)
        self.selection = replace(self.selection, operation="prompt.draft")
        self.fixture = {**self.fixture, "inputs": {}, "parameters": {"seed": 42}}
        second_time = first.finished_at + timedelta(minutes=30)
        with patch("studio.services.execution_smoke_evidence.timezone.now", return_value=second_time):
            second, _ = self.attempt()
            second = self.finish(second)
        client = APIClient()
        client.force_authenticate(self.actor)

        def read():
            response = client.get("/api/v1/acceptance-smoke/", {
                "subject": "deployment", "deployment_id": str(self.deployment.pk)})
            self.assertEqual(response.status_code, 200, response.data)
            return {row["operation"]: row for row in response.data["results"][0]["deployment_smoke"]["operations"]}

        with patch("studio.services.acceptance_smoke.timezone.now", return_value=first.evidence_expires_at):
            rows = read()
        self.assertEqual(rows["script.generate"]["evidence_state"], "expired")
        self.assertEqual(rows["script.generate"]["latest_snapshot"]["id"], str(first.final_snapshot_id))
        self.assertEqual(rows["script.generate"]["prompt_profiles"][0]["evidence_state"], "expired")
        self.assertIsNone(rows["script.generate"]["fresh_passed_snapshot"])
        self.assertEqual(rows["prompt.draft"]["evidence_state"], "fresh")
        self.assertEqual(rows["prompt.draft"]["latest_snapshot"]["id"], str(second.final_snapshot_id))
        self.assertEqual(rows["prompt.fields.generate"]["evidence_state"], "missing")
        self.assertEqual(rows["prompt.fields.translate"]["evidence_state"], "missing")
        # The next definitive failure supersedes this capability's earlier
        # passing result, while a new preparing attempt disables only its action.
        with patch("studio.services.execution_smoke_evidence.timezone.now", return_value=second_time + timedelta(minutes=1)):
            failed, _ = self.attempt()
            failed = self.finish(failed, status="failed")
        with patch("studio.services.execution_smoke_evidence.timezone.now", return_value=second_time + timedelta(minutes=2)):
            _, preparing, _ = self.prepare_resource("manual")
        rows = read()
        self.assertEqual(rows["prompt.draft"]["evidence_state"], "failed")
        self.assertEqual(rows["prompt.draft"]["latest_snapshot"]["id"], str(failed.final_snapshot_id))
        self.assertEqual(rows["prompt.draft"]["latest_attempt"]["id"], str(preparing.pk))
        self.assertFalse(rows["prompt.draft"]["action"]["enabled"])
        self.assertTrue(rows["script.generate"]["action"]["enabled"])
        self.assertIsNone(rows["prompt.draft"]["fresh_passed_snapshot"])

    def test_resource_text_worker_uses_same_exact_protocol_and_single_submit_boundary(self):
        attempt, prep = self.attempt(submitting=False)
        def isolated_submission(current):
            current.status, current.submission_started_at = "submitting", timezone.now()
            current.save(update_fields=["status", "submission_started_at"])
            return True, current
        peer = Mock()
        peer.invoke.return_value = {"text": json.dumps(self.candidate, ensure_ascii=False), "request_id": "isolated-resource-worker",
            "usage": {"prompt_tokens": 123, "completion_tokens": 456, "total_tokens": 579, "private": "do-not-persist"}}
        with patch("studio.services.execution_smoke_worker.begin_smoke_execution_submission", side_effect=isolated_submission), \
                patch("studio.services.execution_smoke_worker.driver_for", return_value=peer):
            done = process_deployment_smoke(attempt)
        self.assertEqual(done.status, "succeeded", done.error_code)
        peer.invoke.assert_called_once()
        self.assertEqual(peer.invoke.call_args.args[2], prep.contract.prepared_request["transport"]["payload"])
        summary = done.result["evidence"]["result"]
        self.assertEqual(summary["representation"], "script_validation_summary")
        self.assertTrue(summary["valid"])
        self.assertEqual(summary["output_version_id"], str(self.output_version.pk))
        from studio.services.execution_smoke_responses import read_smoke_response
        self.assertEqual(read_smoke_response(done, done.result["evidence"]["response_body"]), peer.invoke.return_value["text"])
        self.assertEqual(done.result["evidence"]["usage"], {"prompt_tokens": 123, "completion_tokens": 456, "total_tokens": 579})
        self.assertIsNone(done.actual_cost)
        self.assertNotIn("do-not-persist", json.dumps(done.result))
        self.assertEqual(read_prepared_smoke_fact(done).identity.scope, "resource_smoke")
        self.assertFalse(ExecutionCapacityObservation.objects.exists())

    def run_response(self, text, **response_fields):
        attempt, prep = self.attempt(submitting=False)
        def submitted(current):
            current.status, current.submission_started_at = "submitting", timezone.now()
            current.save(update_fields=["status", "submission_started_at"])
            return True, current
        peer = Mock()
        peer.invoke.return_value = {"text": text, "request_id": "test-response", **response_fields}
        with patch("studio.services.execution_smoke_worker.begin_smoke_execution_submission", side_effect=submitted), \
                patch("studio.services.execution_smoke_worker.driver_for", return_value=peer):
            done = process_deployment_smoke(attempt)
        peer.invoke.assert_called_once()
        return done, prep

    def test_invalid_response_retains_original_and_precise_failure_without_passing_or_retry(self):
        from studio.services.execution_smoke_responses import read_smoke_response
        from studio.task_center_details import inspect_output
        from django.core.files.storage import default_storage
        text = '{"broken": "Bearer secret-never-display",'
        done, prep = self.run_response(text, usage={"prompt_tokens": 0, "completion_tokens": True, "total_tokens": 10**50,
            "token_secret": "do-not-persist"})
        self.assertEqual((done.status, done.error_code), ("failed", "script_semantic_result_invalid"))
        self.assertIn("response_text", done.result["evidence"]["diagnostics"]["structure_errors"])
        self.assertEqual(done.result["evidence"]["failure_phase"], "response_validation")
        self.assertEqual(done.result["evidence"]["usage"], {"prompt_tokens": 0})
        self.assertEqual(done.result["evidence"]["diagnostics"]["usage"], {"prompt_tokens": 0})
        self.assertNotIn("do-not-persist", json.dumps(done.result))
        manifest = done.result["evidence"]["response_body"]
        self.assertEqual(read_smoke_response(done, manifest), text)
        output, _ = inspect_output(done, "deployment_smoke", {}, None)
        self.assertIn('"broken"', output["text"])
        self.assertNotIn("secret-never-display", output["text"])
        self.assertEqual(output["result"]["evidence"]["response_body"]["object_key"], "[redacted]")
        self.assertIsNone(done.evidence_expires_at)
        self.assertFalse(ModelDeploymentCapabilitySnapshot.objects.filter(status="passed").exists())
        with patch("studio.services.execution_smoke_worker.driver_for", side_effect=AssertionError("No retry")):
            with self.assertRaisesMessage(ValidationError, "有效租约"):
                process_deployment_smoke(done)
        default_storage.delete(manifest["object_key"])
        with self.assertRaises(ValidationError):
            inspect_output(done, "deployment_smoke", {}, None)

    def test_response_storage_failure_is_not_a_validation_failure_or_an_automatic_retry(self):
        with patch("studio.services.execution_smoke_responses.store_response", side_effect=OSError("private path")):
            done, _ = self.run_response(json.dumps(self.candidate))
        self.assertEqual((done.status, done.error_code), ("failed", "smoke_response_storage_failed"))
        self.assertNotIn("private path", done.error_message)
        self.assertNotIn("response_body", done.result["evidence"])

    def test_response_metadata_failure_still_retains_original_body(self):
        from studio.services.execution_smoke_responses import read_smoke_response
        text = json.dumps(self.candidate)
        done, _ = self.run_response(text, request_id={"not": "a string"})
        self.assertEqual((done.status, done.error_code), ("uncertain", "invalid_provider_request_id"))
        self.assertEqual(read_smoke_response(done, done.result["evidence"]["response_body"]), text)
        self.assertNotIn("usage", done.result["evidence"])

    def test_evidence_failure_does_not_claim_response_was_invalid(self):
        from studio.services.execution_smoke_evidence import finish_prepared_smoke
        def finish_with_invalid_success(*args, **kwargs):
            if kwargs["status"] == "succeeded":
                raise ValidationError("private finalization failure")
            return finish_prepared_smoke(*args, **kwargs)
        with patch("studio.services.execution_smoke_worker.finish_prepared_smoke", side_effect=finish_with_invalid_success):
            done, _ = self.run_response(json.dumps(self.candidate))
        self.assertEqual((done.status, done.error_code), ("failed", "smoke_evidence_invalid"))
        self.assertEqual(done.result["evidence"]["failure_phase"], "evidence_finalization")
        self.assertEqual(done.result["evidence"]["response_body"]["state"], "available")
        self.assertNotIn("private finalization", done.error_message)
        self.assertFalse(ModelDeploymentCapabilitySnapshot.objects.filter(status="passed").exists())

    def test_resource_text_cancel_and_uncertainty_resolution_keep_original_prepared_identity(self):
        from rest_framework.test import APIClient
        attempt, prep = self.attempt()
        client = APIClient()
        client.force_authenticate(self.actor)
        cancelled = client.post(f"/api/v1/model-deployment-smoke-attempts/{attempt.pk}/cancel/", {}, format="json")
        self.assertEqual(cancelled.status_code, 400, cancelled.data)
        with patch("studio.services.execution_smoke_worker.driver_for", side_effect=AssertionError("Never resubmit")):
            done = process_deployment_smoke(attempt)
        self.assertEqual(done.status, "uncertain")
        saved = deepcopy(done.result)
        endpoint = f"/api/v1/model-deployments/{self.deployment.pk}/resolve-smoke-uncertain/"
        data = {"smoke_attempt_id": str(done.pk), "outcome": "confirmed_not_submitted", "note": "已核对受控测试对端未接收到该请求。"}
        ModelDeploymentSmokeAttempt.objects.filter(pk=done.pk).update(request_sha256="0" * 64)
        rejected = client.post(endpoint, data, format="json")
        self.assertEqual(rejected.status_code, 400, rejected.data)
        self.assertFalse(done.resolutions.exists())
        ModelDeploymentSmokeAttempt.objects.filter(pk=done.pk).update(request_sha256=prep.request_sha256)
        response = client.post(endpoint, data, format="json")
        self.assertEqual(response.status_code, 201, response.data)
        evidence = done.resolutions.get().evidence
        self.assertEqual(evidence["scope"], "resource")
        self.assertIsNone(evidence["execution_instance_version_id"])
        self.assertEqual(evidence["preparation_id"], str(prep.pk))
        self.assertEqual(evidence["prepared_request_sha256"], prep.request_sha256)
        done.refresh_from_db()
        self.assertEqual(done.result, saved)
        self.assertEqual(done.status, "uncertain")
        self.assertIsNone(done.evidence_expires_at)
        self.assertFalse(ModelDeploymentCapabilitySnapshot.objects.exists())

    def test_submission_crash_is_uncertain_not_a_second_call(self):
        attempt, _ = self.attempt()
        with patch("studio.services.execution_smoke_worker.driver_for", side_effect=AssertionError("Never resubmit")):
            done = process_deployment_smoke(attempt)
        self.assertEqual((done.status, done.error_code), ("uncertain", "submission_uncertain"))
        self.assertIsNone(read_prepared_smoke_fact(done).expires_at)

    def test_original_queue_recovery_never_replays_a_started_resource_submission(self):
        attempt, prep = self.attempt()
        attempt.heartbeat_at = timezone.now() - timedelta(minutes=2)
        attempt.save(update_fields=["heartbeat_at"])
        self.assertEqual(recover_stale_deployment_smokes(1), 1)
        recovered = claim_next_deployment_smoke("replacement-resource-worker")
        self.assertEqual(recovered.pk, attempt.pk)
        self.assertNotEqual(recovered.claim_token, attempt.claim_token)
        with patch("studio.services.execution_smoke_worker.driver_for", side_effect=AssertionError("Never resubmit")):
            done = process_deployment_smoke(recovered)
        self.assertEqual((done.status, done.error_code), ("uncertain", "submission_uncertain"))
        self.assertEqual(done.result["preparation_id"], str(prep.pk))
        self.assertFalse(ModelDeploymentCapabilitySnapshot.objects.exists())
        self.assertIsNone(claim_next_deployment_smoke("must-resolve-first"))

    def test_recovered_resource_worker_with_lost_claim_cannot_append_orphan_snapshot(self):
        stale, _ = self.attempt(submitting=False)
        stale.heartbeat_at = timezone.now() - timedelta(minutes=2)
        stale.save(update_fields=["heartbeat_at"])
        self.assertEqual(recover_stale_deployment_smokes(1), 1)
        replacement = claim_next_deployment_smoke("replacement-resource-owner")
        self.assertNotEqual(stale.claim_token, replacement.claim_token)
        with patch("studio.services.execution_smoke_worker.driver_for", side_effect=AssertionError("Stale lease")):
            observed = process_deployment_smoke(stale)
        self.assertEqual(observed.claim_token, replacement.claim_token)
        self.assertEqual(observed.status, "queued")
        self.assertIsNone(observed.finished_at)
        self.assertFalse(ModelDeploymentCapabilitySnapshot.objects.exists())

    def test_resource_provider_failure_records_safe_code_not_untrusted_response(self):
        from studio.services.provider_registry import ProviderDriverError
        attempt, _ = self.attempt(submitting=False)
        def submit(current):
            current.status, current.submission_started_at = "submitting", timezone.now()
            current.save(update_fields=["status", "submission_started_at"])
            return True, current
        peer = Mock()
        peer.invoke.side_effect = ProviderDriverError("opaque provider credential-like text", code="provider_http_error",
            submission_uncertain=False)
        with patch("studio.services.execution_smoke_worker.begin_smoke_execution_submission", side_effect=submit), \
                patch("studio.services.execution_smoke_worker.driver_for", return_value=peer):
            done = process_deployment_smoke(attempt)
        self.assertEqual((done.status, done.error_code), ("failed", "provider_http_error"))
        self.assertEqual(peer.invoke.call_count, 1)
        self.assertIsNone(done.evidence_expires_at)
        self.assertEqual(done.final_snapshot.status, "failed")
        self.assertNotIn("opaque provider", str(done.result) + done.error_message + str(done.final_snapshot.evidence))

    def test_resource_response_rejection_preserves_only_safe_transport_diagnostics(self):
        from studio.tests.test_prepared_text_failure_details import response_failure
        from studio.services.provider_registry import safe_chat_failure_details
        attempt, prep = self.attempt(submitting=False)
        def submit(current):
            current.status, current.submission_started_at = "submitting", timezone.now()
            current.save(update_fields=["status", "submission_started_at"])
            return True, current
        peer = Mock()
        peer.invoke.side_effect = response_failure()
        with patch("studio.services.execution_smoke_worker.begin_smoke_execution_submission", side_effect=submit), \
                patch("studio.services.execution_smoke_worker.driver_for", return_value=peer):
            done = process_deployment_smoke(attempt)
        self.assertEqual((done.status, done.error_code), ("failed", "provider_response_invalid"))
        for key, value in safe_chat_failure_details(response_failure()).items():
            self.assertEqual(done.result["evidence"][key], value)
        self.assertEqual(read_resource_deployment_snapshot(done.final_snapshot).pk, done.pk)
        self.assertIsNone(done.evidence_expires_at)
        persisted = json.dumps(done.result) + done.error_message + json.dumps(done.final_snapshot.evidence)
        for text in ("TOP-SECRET", "must-not-persist", "cached_tokens", "raw_message", "provider_payload"):
            self.assertNotIn(text, persisted)
        peer.invoke.assert_called_once()

        from rest_framework.test import APIClient
        client = APIClient()
        client.force_authenticate(self.actor)
        response = client.get(f"/api/v1/execution-preparations/{prep.pk}/")
        self.assertEqual(response.status_code, 200, response.data)
        self.assertEqual(response.data["response_diagnostic"]["state"], "recorded")
        self.assertEqual(str(response.data["response_diagnostic"]["task_id"]), str(done.pk))
        self.assertIn("输出长度额度", response.data["response_diagnostic"]["summary"])
        type(done).objects.filter(pk=done.pk).update(result_sha256="0" * 64)
        response = client.get(f"/api/v1/execution-preparations/{prep.pk}/")
        self.assertEqual(response.status_code, 200, response.data)
        self.assertEqual(response.data["response_diagnostic"]["state"], "unverifiable")
        peer.invoke.assert_called_once()

    def test_queued_resource_cancellation_uses_public_endpoint_and_preserves_original_preparation(self):
        from rest_framework.test import APIClient
        attempt, prep = self.attempt(submitting=False)
        client = APIClient()
        client.force_authenticate(self.actor)
        response = client.post(f"/api/v1/model-deployment-smoke-attempts/{attempt.pk}/cancel/", {}, format="json")
        self.assertEqual(response.status_code, 202, response.data)
        attempt.refresh_from_db()
        prep.refresh_from_db()
        self.assertEqual(attempt.status, "cancelled")
        self.assertEqual(prep.status, "cancelled")
        self.assertIsNone(attempt.submission_started_at)
        self.assertIsNone(attempt.evidence_expires_at)
        self.assertFalse(ModelDeploymentCapabilitySnapshot.objects.exists())
        self.assertIsNone(claim_next_deployment_smoke("cancelled-resource-worker"))
