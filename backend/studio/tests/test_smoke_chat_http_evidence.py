"""Original Smoke audit and real HTTP serialization with network I/O isolated."""
from copy import deepcopy
from hashlib import sha256
import json
from unittest.mock import Mock, patch
from uuid import uuid4

import requests
from django.core.exceptions import ValidationError
from django.db import connection, OperationalError
from django.test import TestCase, override_settings
from django.test.utils import CaptureQueriesContext
from django.utils import timezone
from rest_framework.test import APIClient

from studio.models import ExecutionPreparation, ModelDeploymentSmokeAttempt, ModelDeploymentSmokeAttemptResolution
from studio.services.chat_http_evidence import current_chat_body_observer, prepared_json_body_facts, record_smoke_chat_body, read_chat_body_note
from studio.services.deployment_smoke_queue import process_deployment_smoke
from studio.services.deployment_smoke_resolution import has_operator_resolution, unresolved_deployment_smoke_attempt, resolve_deployment_smoke_attempt
from studio.services.provider_registry import _PinnedAddressAdapter, ProviderDriverError
from studio.tests import test_execution_resource_smoke_evidence as resource
from studio.tests import test_execution_smoke_evidence as instance
from studio.tests import test_deployment_text_smoke_queue as queue


@override_settings(AI_PROVIDER_ALLOWED_HOSTS=("api.example.com",),
    STORAGES={"default": {"BACKEND": "django.core.files.storage.InMemoryStorage"}})
class SmokeHttpBodyTests(TestCase):
    setUpTestData = resource.ResourcePreparedEvidenceTests.__dict__["setUpTestData"]
    setUp = resource.ResourcePreparedEvidenceTests.setUp
    make_resource_profile = resource.ResourcePreparedEvidenceTests.make_resource_profile
    prepare_resource = resource.ResourcePreparedEvidenceTests.prepare_resource
    attempt = resource.ResourcePreparedEvidenceTests.attempt

    def wire(self):
        return requests.Request("POST", "https://api.example.com/chat/completions",
            json={"messages": [{"role": "user", "content": "PRIVATE 中文"}]},
            headers={"Authorization": "Bearer NEVER-RECORD"}).prepare()

    def record(self, attempt, prep):
        record_smoke_chat_body(attempt, prep.pk, prepared_json_body_facts(self.wire()))

    def run_worker(self, *, invalid=False, uncertain=False):
        attempt, prep = self.attempt(submitting=False)
        peer = Mock()
        def submitted(current):
            current.status, current.submission_started_at = "submitting", timezone.now()
            current.save(update_fields=("status", "submission_started_at"))
            return True, current
        def invoke(_deployment, _operation, payload):
            wire = requests.Request("POST", "https://api.example.com/chat/completions", json=payload).prepare()
            adapter = _PinnedAddressAdapter(None, before_send=current_chat_body_observer())
            self.assertIsNotNone(adapter._before_send)
            adapter.send(wire)
            if uncertain:
                raise ProviderDriverError("isolated", code="isolated_timeout", submission_uncertain=True)
            return {"text": "{" if invalid else json.dumps(self.candidate), "request_id": "isolated-http",
                "usage": {"prompt_tokens": 20, "completion_tokens": 30, "total_tokens": 50}}
        def network(wire, **kwargs):
            event = attempt.resolutions.get(outcome="http_body_prepared")
            self.assertEqual(event.evidence["body"], prepared_json_body_facts(wire))
            self.assertEqual(event.evidence["preparation_id"], str(prep.pk))
            self.assertIsNone(event.decided_by_id)
            return Mock()
        peer.invoke.side_effect = invoke
        with patch("studio.services.execution_smoke_worker.begin_smoke_execution_submission", side_effect=submitted), \
                patch("studio.services.execution_smoke_worker.driver_for", return_value=peer), \
                patch("requests.adapters.HTTPAdapter.send", side_effect=network) as send:
            done = process_deployment_smoke(attempt)
        send.assert_called_once()
        peer.invoke.assert_called_once()
        self.assertIsNone(current_chat_body_observer())
        return done, prep

    def test_worker_and_authorized_detail_keep_one_original_audit_before_network(self):
        done, prep = self.run_worker()
        self.assertEqual(done.status, "succeeded", done.error_code)
        self.assertIsNone(done.actual_cost)
        self.assertEqual(done.resolutions.count(), 1)
        note = read_chat_body_note(done, preparation_id=prep.pk)
        self.assertIn("不是已发送或已送达", note)
        client = APIClient()
        client.force_authenticate(self.actor)
        response = client.get(f"/api/v1/task-center/preparation/{prep.pk}/")
        self.assertEqual(response.status_code, 200, response.data)
        self.assertIn(note, response.data["submission"]["notes"])
        self.assertEqual(response.data["output"]["state"], "available")
        self.assertIsNone(response.data["actual_cost"])
        client.force_authenticate(None)
        self.assertIn(client.get(f"/api/v1/task-center/preparation/{prep.pk}/").status_code, (401, 403))
        with patch("studio.services.execution_smoke_worker.driver_for") as driver, self.assertRaises(ValidationError):
            process_deployment_smoke(done)
        driver.assert_not_called()

    def test_invalid_response_stays_failed_with_audit_not_a_pass(self):
        done, prep = self.run_worker(invalid=True)
        self.assertEqual((done.status, done.error_code), ("failed", "response_json_invalid"))
        self.assertIsNone(done.evidence_expires_at)
        self.assertIn("HTTP库序列化记录", read_chat_body_note(done, preparation_id=prep.pk))
        self.assertIsNone(done.actual_cost)

    def test_uncertain_transport_still_needs_operator_and_blocks_new_smoke(self):
        from studio.services.acceptance_smoke import safe_deployment_attempt_summary
        from studio.platform_serializers import ModelDeploymentSerializer
        done, _ = self.run_worker(uncertain=True)
        self.assertEqual(done.status, "uncertain", done.error_code)
        self.assertFalse(has_operator_resolution(done))
        self.assertEqual(unresolved_deployment_smoke_attempt(self.deployment).pk, done.pk)
        self.assertTrue(safe_deployment_attempt_summary(done)["resolution_required"])
        cached = ModelDeploymentSmokeAttempt.objects.prefetch_related("resolutions").get(pk=done.pk)
        with self.assertNumQueries(0):
            self.assertFalse(has_operator_resolution(cached))
        self.assertIsNotNone(ModelDeploymentSerializer().get_latest_smoke(self.deployment)["unresolved_submission"])
        client = APIClient()
        client.force_authenticate(self.actor)
        response = client.post(f"/api/v1/model-deployments/{self.deployment.pk}/smoke-test/",
            queue.DeploymentTextSmokeQueueTests.body(self), format="json", HTTP_IDEMPOTENCY_KEY="do-not-retry")
        self.assertEqual(response.status_code, 409, response.data)
        self.assertEqual(ModelDeploymentSmokeAttempt.objects.count(), 1)

    def test_operator_cannot_forge_system_event_and_can_resolve_after_it(self):
        from studio.platform_serializers import ResolvePromptNormalizationSmokeInputSerializer
        attempt, prep = self.attempt()
        self.record(attempt, prep)
        serializer = ResolvePromptNormalizationSmokeInputSerializer(data={"smoke_attempt_id": str(attempt.pk),
            "outcome": "http_body_prepared", "note": "forged event"})
        self.assertFalse(serializer.is_valid())
        with self.assertRaises(ValidationError):
            resolve_deployment_smoke_attempt(self.deployment, smoke_attempt_id=attempt.pk,
                outcome="http_body_prepared", note="forged event", user=self.actor)
        resolution = resolve_deployment_smoke_attempt(self.deployment, smoke_attempt_id=attempt.pk,
            outcome="confirmed_remote_failed", note="Isolated operator evidence", user=self.actor)
        self.assertEqual(resolution.revision, 2)
        self.assertEqual(resolution.decided_by_id, self.actor.pk)
        self.assertTrue(has_operator_resolution(attempt))
        self.assertIsNone(unresolved_deployment_smoke_attempt(self.deployment))
        with self.assertRaises(ValidationError):
            resolve_deployment_smoke_attempt(self.deployment, smoke_attempt_id=attempt.pk,
                outcome="confirmed_remote_failed", note="duplicate", user=self.actor)
        attempt.refresh_from_db()
        self.assertEqual(attempt.status, "submitting")
        self.assertIsNone(attempt.actual_cost)

    def test_db_failure_before_send_never_reaches_network(self):
        with patch("studio.models.ModelDeploymentSmokeAttemptResolution.objects.create", side_effect=OperationalError("isolated")), \
                patch("requests.adapters.HTTPAdapter.send") as send, self.assertRaises(OperationalError):
            self.run_worker()
        send.assert_not_called()
        attempt = ModelDeploymentSmokeAttempt.objects.get()
        self.assertEqual(attempt.status, "submitting")
        self.assertFalse(attempt.resolutions.exists())
        self.assertIsNone(current_chat_body_observer())

    def test_claim_cancel_and_request_changes_prevent_audit_and_send(self):
        attempt, prep = self.attempt()
        original = attempt.request_sha256
        for mutation in ({"claim_token": uuid4()}, {"status": "cancel_requested"},
                {"cancel_requested_at": timezone.now()}, {"request_sha256": "f" * 64}):
            ModelDeploymentSmokeAttempt.objects.filter(pk=attempt.pk).update(**mutation)
            with self.subTest(mutation=mutation), self.assertRaises(ValidationError):
                self.record(attempt, prep)
            ModelDeploymentSmokeAttempt.objects.filter(pk=attempt.pk).update(claim_token=attempt.claim_token,
                status="submitting", cancel_requested_at=None, request_sha256=original)
        ExecutionPreparation.objects.filter(pk=prep.pk).update(status="cancelled")
        with self.assertRaises(ValidationError):
            self.record(attempt, prep)
        self.assertFalse(attempt.resolutions.exists())

    def test_reader_is_bounded_indexed_read_only_and_rejects_wrong_identity_or_corruption(self):
        from studio.services.json_contracts import json_contract
        attempt, prep = self.attempt()
        with self.assertNumQueries(1):
            self.assertIn("未记录", read_chat_body_note(attempt, preparation_id=prep.pk))
        self.assertFalse(attempt.resolutions.exists())
        self.record(attempt, prep)
        with self.assertRaises(ValidationError):
            self.record(attempt, prep)
        with CaptureQueriesContext(connection) as queries:
            note = read_chat_body_note(attempt, preparation_id=prep.pk)
        self.assertIn(sha256(self.wire().body).hexdigest(), note)
        self.assertNotIn("PRIVATE", note)
        sql = [q["sql"] for q in queries if 'FROM "studio_modeldeploymentsmokeattemptresolution"' in q["sql"]]
        self.assertEqual(len(sql), 1)
        self.assertIn("LIMIT 2", sql[0])
        self.assertNotIn('"note"', sql[0])
        if connection.vendor == "sqlite":
            with connection.cursor() as cursor:
                cursor.execute("EXPLAIN QUERY PLAN " + sql[0])
                self.assertIn("INDEX", str(cursor.fetchall()).upper())
        self.assertIn("无法核验", read_chat_body_note(attempt, preparation_id=uuid4()))
        event = attempt.resolutions.get()
        original = deepcopy(event.evidence)
        for changes in ({"request_sha256": "f" * 64}, {"raw": "PRIVATE"}, {"body": {**original["body"], "size_bytes": False}}):
            contract = json_contract({**original, **changes})
            ModelDeploymentSmokeAttemptResolution.objects.filter(pk=event.pk).update(evidence=contract.value,
                evidence_size_bytes=contract.size_bytes, evidence_sha256=contract.sha256)
            self.assertIn("无法核验", read_chat_body_note(attempt, preparation_id=prep.pk))
        ModelDeploymentSmokeAttemptResolution.objects.filter(pk=event.pk).update(evidence_sha256="f" * 64)
        self.assertIn("无法核验", read_chat_body_note(attempt, preparation_id=prep.pk))
        event.pk = None
        event._state.adding = True
        event.revision = 2
        event.save()
        self.assertIn("无法核验", read_chat_body_note(attempt, preparation_id=prep.pk))


@override_settings(AI_PROVIDER_ALLOWED_HOSTS=("api.example.com",))
class InstanceSmokeHttpBodyTests(TestCase):
    setUpTestData = instance.ExecutionSmokeEvidenceTests.__dict__["setUpTestData"]
    setUp = instance.ExecutionSmokeEvidenceTests.setUp
    version = instance.ExecutionSmokeEvidenceTests.version
    attempt = instance.ExecutionSmokeEvidenceTests.attempt

    def test_combination_scope_uses_its_own_original_evidence(self):
        attempt, prep = self.attempt()
        attempt.submission_started_at = timezone.now()
        attempt.save(update_fields=("submission_started_at",))
        wire = requests.Request("POST", "https://api.example.com/chat/completions", json={"messages": []}).prepare()
        record_smoke_chat_body(attempt, prep.pk, prepared_json_body_facts(wire))
        self.assertEqual(attempt.scope, "instance")
        self.assertIn(sha256(wire.body).hexdigest(), read_chat_body_note(attempt, preparation_id=prep.pk))
        self.assertFalse(has_operator_resolution(attempt))
        self.assertIsNone(attempt.final_snapshot_id)
