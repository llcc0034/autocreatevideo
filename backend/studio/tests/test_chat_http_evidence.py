"""Real Requests serialization with isolated network boundaries; never external I/O."""
from contextvars import Context
from copy import deepcopy
from hashlib import sha256
from types import SimpleNamespace
from unittest.mock import Mock, patch
import json
import uuid

import requests
from django.core.exceptions import ValidationError
from django.db import connection, OperationalError
from django.test import SimpleTestCase, TestCase, override_settings
from django.test.utils import CaptureQueriesContext

from studio.models import GenerationAttempt, GenerationAttemptResolution
from studio.services.chat_http_evidence import (
    current_chat_body_observer, observe_chat_http_body, prepared_json_body_facts,
    read_chat_body_note, record_production_chat_body, validate_body_facts,
)
from studio.services.provider_registry import _PinnedAddressAdapter, _request_json, ProviderDriverError
from studio.tests import test_execution_production_dispatch as fixtures


def prepared_request():
    return requests.Request("POST", "https://api.example.com/chat/completions",
        json={"model": "fixture", "messages": [{"role": "user", "content": "中文\n<script>private</script>"}],
              "seed": 0, "option": False}, headers={"Authorization": "Bearer PRIVATE-KEY"}).prepare()


class PreparedHttpBodyTests(SimpleTestCase):
    def test_pre_transport_failure_codes_are_readable_without_raw_exception_data(self):
        from studio.services.provider_registry import safe_chat_failure_message
        self.enterContext(patch("studio.services.provider_registry.boundary_int", return_value=1000000))
        for code in ("provider_dns_unavailable", "provider_private_address_forbidden", "provider_config_invalid",
                "provider_payload_invalid", "credential_missing", "provider_driver_not_registered"):
            message = safe_chat_failure_message(ProviderDriverError("PRIVATE URL TOKEN", code=code))
            self.assertIn(code, message)
            self.assertNotIn("PRIVATE", message)
        self.assertNotIn("PRIVATE", safe_chat_failure_message(ProviderDriverError("PRIVATE", code="PRIVATE_UNKNOWN_CODE")))

    def test_transport_diagnostics_distinguish_connect_from_unknown_send_and_read(self):
        from studio.services.provider_registry import safe_chat_failure_details, safe_chat_failure_message
        self.enterContext(patch("studio.services.provider_registry.boundary_int", return_value=1000000))
        endpoint = SimpleNamespace(scheme="https", peer_addresses=("203.0.113.1",))
        for failure, uncertain, phase in ((requests.ConnectTimeout("PRIVATE URL TOKEN"), False, "connect"),
                (requests.ReadTimeout("PRIVATE URL TOKEN"), True, "request"),
                (requests.ConnectionError("PRIVATE URL TOKEN"), True, "request")):
            with self.subTest(kind=type(failure).__name__), \
                    patch("studio.services.provider_registry._resolved_provider_endpoint", return_value=endpoint), \
                    patch("requests.adapters.HTTPAdapter.send", side_effect=failure), \
                    patch("requests.Session.close") as close, self.assertRaises(ProviderDriverError) as raised:
                _request_json("POST", "https://api.example.com/chat/completions", payload={}, timeout=15, submission=True)
            error = raised.exception
            self.assertEqual(error.submission_uncertain, uncertain)
            self.assertEqual(error.retryable, not uncertain)
            self.assertEqual(safe_chat_failure_details(error), {"transport_phase": phase, "exception_kind": type(failure).__name__})
            self.assertNotIn("PRIVATE", safe_chat_failure_message(error))
            close.assert_called_once()
        response = Mock(status_code=200)
        with patch("studio.services.provider_registry._resolved_provider_endpoint", return_value=endpoint), \
                patch("requests.Session.request", return_value=response), \
                patch("studio.services.provider_registry._peer_address", return_value="203.0.113.1"), \
                patch("studio.services.provider_registry._response_json", side_effect=requests.exceptions.ChunkedEncodingError("PRIVATE")), \
                patch("requests.Session.close") as close, self.assertRaises(ProviderDriverError) as raised:
            _request_json("POST", "https://api.example.com/chat/completions", payload={}, timeout=15, submission=True)
        self.assertTrue(raised.exception.submission_uncertain)
        self.assertEqual(safe_chat_failure_details(raised.exception), {
            "transport_phase": "response_body", "exception_kind": "ChunkedEncodingError", "http_status": 200})
        close.assert_called_once()

    def test_http_error_diagnostics_are_allowlisted_not_response_or_request_content(self):
        from studio.services.provider_registry import safe_chat_failure_details, safe_chat_failure_message
        self.enterContext(patch("studio.services.provider_registry.boundary_int", return_value=1000000))
        endpoint = SimpleNamespace(scheme="https", peer_addresses=("203.0.113.1",))
        for status in (401, 429, 503):
            with self.subTest(status=status), patch("studio.services.provider_registry._resolved_provider_endpoint", return_value=endpoint), \
                    patch("requests.Session.request", return_value=Mock(status_code=status)), \
                    patch("studio.services.provider_registry._peer_address", return_value="203.0.113.1"), self.assertRaises(ProviderDriverError) as raised:
                _request_json("POST", "https://api.example.com/chat/completions", payload={}, timeout=15, submission=True)
            error = raised.exception
            error.metadata.update(url="PRIVATE", headers={"Authorization": "SECRET"}, exception_kind="PRIVATE")
            self.assertEqual(safe_chat_failure_details(error), {"transport_phase": "response_headers", "http_status": status})
            self.assertIn(f"HTTP {status}", safe_chat_failure_message(error))
            self.assertNotIn("PRIVATE", safe_chat_failure_message(error))
            self.assertEqual(error.submission_uncertain, status != 401)

    def test_operator_resolution_cannot_forge_transport_evidence(self):
        from rest_framework.exceptions import ValidationError as ApiValidationError
        from studio.platform_serializers import ResolveUncertainAttemptInputSerializer
        with self.assertRaises(ApiValidationError):
            ResolveUncertainAttemptInputSerializer().fields["outcome"].run_validation("http_body_prepared")

    def test_exact_requests_bytes_not_canonical_reserialization(self):
        request = prepared_request()
        facts = prepared_json_body_facts(request)
        self.assertEqual(facts, {"schema_version": 1, "encoding": "requests.prepared_json/v1",
            "size_bytes": len(request.body), "sha256": sha256(request.body).hexdigest()})
        canonical = json.dumps(json.loads(request.body), ensure_ascii=False, separators=(",", ":")).encode()
        self.assertNotEqual(facts["sha256"], sha256(canonical).hexdigest())
        self.assertNotIn("PRIVATE", str(facts))
        self.assertNotIn("private", str(facts))
        self.assertNotIn("example.com", str(facts))

    def test_invalid_or_unexpected_bytes_fail_closed(self):
        for field, value in (("method", "GET"), ("body", "text"), ("body", b""), ("headers", {})):
            request = prepared_request()
            setattr(request, field, value)
            with self.subTest(field=field, value=value), self.assertRaises(ValidationError):
                prepared_json_body_facts(request)
        facts = prepared_json_body_facts(prepared_request())
        for key, value in (("schema_version", True), ("size_bytes", True), ("size_bytes", 0),
                           ("sha256", "A" * 64), ("encoding", "other"), ("secret", "PRIVATE")):
            with self.subTest(key=key, value=value), self.assertRaises(ValidationError):
                validate_body_facts({**facts, key: value})

    def test_context_is_single_call_and_resets_on_error(self):
        observer = Mock()
        with self.assertRaisesRegex(RuntimeError, "isolated"):
            with observe_chat_http_body(observer):
                self.assertIs(current_chat_body_observer(), observer)
                self.assertIsNone(Context().run(current_chat_body_observer))
                with self.assertRaises(ValidationError):
                    with observe_chat_http_body(Mock()):
                        self.fail("nested observer accepted")
                raise RuntimeError("isolated")
        self.assertIsNone(current_chat_body_observer())

    def test_adapter_observes_before_send_and_propagates_write_failure(self):
        order = []
        request = prepared_request()
        adapter = _PinnedAddressAdapter(None, before_send=lambda facts: order.append(("audit", facts)))
        with patch("requests.adapters.HTTPAdapter.send", side_effect=lambda *args, **kwargs: order.append(("network", None))):
            adapter.send(request, timeout=15)
        self.assertEqual(order, [("audit", prepared_json_body_facts(request)), ("network", None)])
        for error in (ValidationError("invalid audit"), OperationalError("database unavailable")):
            adapter = _PinnedAddressAdapter(None, before_send=Mock(side_effect=error))
            with patch("requests.adapters.HTTPAdapter.send") as network, self.assertRaises(type(error)):
                adapter.send(request)
            network.assert_not_called()

    def test_original_session_observes_only_chat_submission_and_never_leaks_headers(self):
        endpoint = SimpleNamespace(scheme="https", peer_addresses=("203.0.113.1",))
        observer, response = Mock(), requests.Response()
        response.status_code = 200
        response._content = b'{"ok": true}'
        payload = json.loads(prepared_request().body)
        # Real Session and PreparedRequest; only DNS/peer and network I/O are isolated.
        with patch("studio.services.provider_registry._resolved_provider_endpoint", return_value=endpoint), \
                patch("studio.services.provider_registry._peer_address", return_value="203.0.113.1"), \
                patch("studio.services.provider_registry._response_json", return_value={"ok": True}), \
                patch("requests.adapters.HTTPAdapter.send", return_value=response) as network:
            with observe_chat_http_body(observer):
                for submission, inspect in ((True, True), (True, False), (False, True), (False, False)):
                    self.assertEqual(_request_json("POST", "https://api.example.com/chat/completions",
                        payload=payload, headers={"Authorization": "Bearer PRIVATE-KEY"}, timeout=15,
                        submission=submission, inspect_text_capacity=inspect), {"ok": True})
            observer.assert_called_once_with(prepared_json_body_facts(network.call_args_list[0].args[0]))
            self.assertEqual(network.call_count, 4)
            self.assertEqual(network.call_args.kwargs["proxies"], {})
            self.assertTrue(network.call_args.kwargs["stream"])
            self.assertIsNone(current_chat_body_observer())

    def test_transport_audit_failure_closes_session_without_send_and_network_failure_stays_uncertain(self):
        endpoint = SimpleNamespace(scheme="https", peer_addresses=("203.0.113.1",))
        with patch("studio.services.provider_registry._resolved_provider_endpoint", return_value=endpoint), \
                patch("requests.adapters.HTTPAdapter.send") as network, \
                patch("requests.Session.close") as close:
            with observe_chat_http_body(Mock(side_effect=OperationalError("audit unavailable"))), self.assertRaises(OperationalError):
                _request_json("POST", "https://api.example.com/chat/completions", payload={"messages": []},
                    timeout=15, submission=True, inspect_text_capacity=True)
            network.assert_not_called()
            close.assert_called_once()
        observer = Mock()
        with patch("studio.services.provider_registry._resolved_provider_endpoint", return_value=endpoint), \
                patch("requests.adapters.HTTPAdapter.send", side_effect=requests.ConnectionError("isolated")) as network:
            with observe_chat_http_body(observer), self.assertRaises(ProviderDriverError) as failure:
                _request_json("POST", "https://api.example.com/chat/completions", payload={"messages": []},
                    timeout=15, submission=True, inspect_text_capacity=True)
            self.assertTrue(failure.exception.submission_uncertain)
            observer.assert_called_once()
            network.assert_called_once()


@override_settings(AI_PROVIDER_ALLOWED_HOSTS=("api.example.com",))
class ProductionHttpBodyTests(TestCase):
    setUpTestData = classmethod(fixtures.ProductionDispatchTests.setUpTestData.__func__)
    setUp = fixtures.ProductionDispatchTests.setUp
    profile_instance = fixtures.ProductionDispatchTests.profile_instance
    root = fixtures.ProductionDispatchTests.root
    prepare = fixtures.ProductionDispatchTests.prepare
    structure_instance = fixtures.ProductionDispatchTests.structure_instance
    pass_smoke = fixtures.ProductionDispatchTests.pass_smoke
    advance = fixtures.ProductionDispatchTests.advance
    response = fixtures.ProductionDispatchTests.response
    process = fixtures.ProductionDispatchTests.process
    queued = fixtures.ProductionDispatchTests.queued

    def begin(self):
        from studio.services.text_generation_queue import claim_next_text_attempt
        from studio.services.execution_production_dispatch import begin_production_execution_submission
        _, attempt = self.queued()
        allowed, claim = begin_production_execution_submission(claim_next_text_attempt("http-evidence-fixture"))
        self.assertTrue(allowed)
        self.assertEqual(attempt.pk, claim.pk)
        return claim

    def test_worker_stores_original_attempt_evidence_before_isolated_network_and_no_replay(self):
        from studio.services.text_generation_queue import process_text_attempt
        _, attempt = self.queued()
        request = prepared_request()

        def invoke(*args):
            adapter = _PinnedAddressAdapter(None, before_send=current_chat_body_observer())
            self.assertIsNotNone(adapter._before_send)
            adapter.send(request)
            return self.response()

        def network(*args, **kwargs):
            evidence = attempt.resolutions.get(outcome="http_body_prepared")
            self.assertEqual(evidence.evidence["body"], prepared_json_body_facts(request))
            self.assertIsNone(evidence.decided_by_id)
            return Mock()

        with patch("requests.adapters.HTTPAdapter.send", side_effect=network) as send:
            result, peer = self.process(effect=invoke)
            self.assertEqual(result.status, "succeeded", result.error_code)
            process_text_attempt(result)
        send.assert_called_once()
        peer.invoke.assert_called_once()
        self.assertEqual(result.resolutions.filter(outcome="http_body_prepared").count(), 1)
        self.assertIsNone(result.actual_cost)
        self.assertIsNone(current_chat_body_observer())
        note = read_chat_body_note(result, preparation_id=result.preparation_id)
        self.assertIn(sha256(request.body).hexdigest(), note)
        self.assertIn("不是已发送或已送达", note)
        self.assertNotIn("private", note)
        from rest_framework.test import APIClient
        client = APIClient()
        client.force_authenticate(self.actor)
        detail = client.get(f"/api/v1/task-center/preparation/{result.preparation_id}/")
        self.assertEqual(detail.status_code, 200, detail.data)
        self.assertIn(note, detail.data["submission"]["notes"])
        self.assertEqual(detail.data["output"]["state"], "available")
        self.assertIsNone(detail.data["actual_cost"])
        client.force_authenticate(None)
        self.assertIn(client.get(f"/api/v1/task-center/preparation/{result.preparation_id}/").status_code, (401, 403))

    def test_worker_audit_write_failure_never_reaches_network(self):
        _, attempt = self.queued()

        def invoke(*args):
            return _PinnedAddressAdapter(None, before_send=current_chat_body_observer()).send(prepared_request())

        with patch("studio.services.text_generation_queue.append_attempt_resolution", side_effect=OperationalError("isolated")), \
                patch("requests.adapters.HTTPAdapter.send") as send, self.assertRaises(OperationalError):
            self.process(effect=invoke)
        send.assert_not_called()
        attempt.refresh_from_db()
        self.assertEqual(attempt.status, "submitting")
        self.assertIsNone(attempt.actual_cost)
        self.assertFalse(attempt.resolutions.exists())
        self.assertIsNone(current_chat_body_observer())

    def test_stale_claim_cancelled_owner_or_request_change_cannot_record(self):
        claim = self.begin()
        original = claim.request_sha256
        for mutation in ({"claim_token": uuid.uuid4()}, {"status": "cancel_requested"},
                         {"ignore_late_result": True}, {"request_sha256": "f" * 64}):
            GenerationAttempt.objects.filter(pk=claim.pk).update(**mutation)
            with self.subTest(mutation=mutation), self.assertRaises(ValidationError):
                record_production_chat_body(claim, prepared_json_body_facts(prepared_request()))
            GenerationAttempt.objects.filter(pk=claim.pk).update(
                claim_token=claim.claim_token, status="submitting", ignore_late_result=False, request_sha256=original)
        self.run.status = "cancelled"
        self.run.save(update_fields=("status",))
        with self.assertRaises(ValidationError):
            record_production_chat_body(claim, prepared_json_body_facts(prepared_request()))
        self.assertFalse(claim.resolutions.exists())

    def test_single_bounded_indexed_read_missing_history_no_backfill_and_duplicate_send_rejected(self):
        claim = self.begin()
        with self.assertNumQueries(1):
            self.assertIn("未记录", read_chat_body_note(claim, preparation_id=claim.preparation_id))
        self.assertFalse(claim.resolutions.exists())
        record_production_chat_body(claim, prepared_json_body_facts(prepared_request()))
        with self.assertRaises(ValidationError):
            record_production_chat_body(claim, prepared_json_body_facts(prepared_request()))
        with CaptureQueriesContext(connection) as queries:
            self.assertIn("HTTP库序列化记录", read_chat_body_note(claim, preparation_id=claim.preparation_id))
        sql = [q["sql"] for q in queries if 'FROM "studio_generationattemptresolution"' in q["sql"]]
        self.assertEqual(len(sql), 1)
        self.assertIn("LIMIT 2", sql[0])
        self.assertNotIn('"note"', sql[0])
        self.assertNotIn('"provider_request_id"', sql[0])
        if connection.vendor == "sqlite":
            with connection.cursor() as cursor:
                cursor.execute("EXPLAIN QUERY PLAN " + sql[0])
                self.assertIn("INDEX", str(cursor.fetchall()).upper())

    def test_corrupt_duplicate_or_mislinked_evidence_is_not_reconstructed(self):
        from studio.services.json_contracts import json_contract
        from studio.services.text_generation_queue import append_attempt_resolution
        claim = self.begin()
        record_production_chat_body(claim, prepared_json_body_facts(prepared_request()))
        row = claim.resolutions.get()
        original = deepcopy(row.evidence)
        for changes in ({"request_sha256": "f" * 64}, {"preparation_id": str(uuid.uuid4())}, {"raw": "PRIVATE"},
                        {"body": {**original["body"], "size_bytes": False}}):
            value = json_contract({**original, **changes}, max_bytes=8192)
            GenerationAttemptResolution.objects.filter(pk=row.pk).update(evidence=value.value,
                evidence_size_bytes=value.size_bytes, evidence_sha256=value.sha256)
            with self.subTest(changes=changes):
                self.assertIn("无法核验", read_chat_body_note(claim, preparation_id=claim.preparation_id))
                self.assertNotIn("PRIVATE", read_chat_body_note(claim, preparation_id=claim.preparation_id))
        GenerationAttemptResolution.objects.filter(pk=row.pk).update(evidence_sha256="f" * 64)
        self.assertIn("无法核验", read_chat_body_note(claim, preparation_id=claim.preparation_id))
        append_attempt_resolution(claim, outcome="http_body_prepared", evidence=original)
        self.assertIn("无法核验", read_chat_body_note(claim, preparation_id=claim.preparation_id))
        claim.refresh_from_db()
        self.assertEqual(claim.status, "submitting")
        self.assertIsNone(claim.actual_cost)
