"""Asset text observations on the original worker; HTTP network is isolated."""
from copy import deepcopy
from types import SimpleNamespace
from unittest.mock import Mock, patch
from uuid import uuid4

import requests
from django.core.exceptions import ValidationError
from django.db import OperationalError, connection, transaction
from django.test import TestCase, override_settings
from django.test.utils import CaptureQueriesContext
from rest_framework.exceptions import ValidationError as ApiValidationError
from rest_framework.test import APIClient

from studio.asset_generation_serializers import AssetGenerationJobSerializer, AssetGenerationResolutionInputSerializer
from studio.models import AssetGenerationJob, AssetGenerationJobResolution, ExecutionPreparation
from studio.services.asset_generation import (
    _collected_actual_cost, claim_next_text_asset_job, process_text_asset_job,
    request_asset_generation_cancel, resolve_uncertain_asset_generation_job,
)
from studio.services.chat_http_evidence import (
    current_chat_body_observer, prepared_json_body_facts, read_chat_body_note, record_asset_chat_body,
)
from studio.services.execution_asset_dispatch import begin_asset_execution_submission
from studio.services.json_contracts import json_contract
from studio.services.provider_registry import _PinnedAddressAdapter, ProviderDriverError
from studio.tests import test_execution_asset_text_worker as fixtures
from studio.tests import test_execution_asset_auxiliaries as auxiliary


@override_settings(AI_PROVIDER_ALLOWED_HOSTS=("api.example.com",))
class AssetChatHttpEvidenceTests(TestCase):
    setUpTestData = fixtures.ExecutionAssetTextWorkerTests.__dict__["setUpTestData"]
    setUp = fixtures.ExecutionAssetTextWorkerTests.setUp
    version = fixtures.ExecutionAssetTextWorkerTests.version
    invoke = fixtures.ExecutionAssetTextWorkerTests.invoke

    def wire(self):
        return requests.Request("POST", "https://api.example.com/chat/completions",
            json={"messages": [{"role": "user", "content": "PRIVATE 中文"}]},
            headers={"Authorization": "Bearer NEVER-RECORD"}).prepare()

    def ready(self):
        claim = claim_next_text_asset_job("isolated-body-worker")
        permitted, claim = begin_asset_execution_submission(claim)
        self.assertTrue(permitted)
        return claim

    def record(self, claim):
        record_asset_chat_body(claim, self.prep.pk, self.prep.request_sha256, prepared_json_body_facts(self.wire()))

    def note(self, job):
        return read_chat_body_note(job, preparation_id=self.prep.pk, request_sha256=self.prep.request_sha256)

    def worker(self, *, invalid=False, uncertain=False, cancel=False):
        before = deepcopy(self.prep.contract.prepared_request)
        def invoke(_deployment, _operation, payload):
            wire = requests.Request("POST", "https://api.example.com/chat/completions", json=payload).prepare()
            observer = current_chat_body_observer()
            self.assertIsNotNone(observer)
            _PinnedAddressAdapter(None, before_send=observer).send(wire)
            if cancel:
                request_asset_generation_cancel(AssetGenerationJob.objects.get(pk=self.job.pk))
            if uncertain:
                raise ProviderDriverError("isolated disconnect", code="transport_error", submission_uncertain=True)
            return {"text": "" if invalid else "A blue parcel.", "request_id": "isolated-chat"}
        def network(wire, **kwargs):
            row = self.job.resolutions.get(outcome="http_body_prepared")
            self.assertEqual(row.evidence["body"], prepared_json_body_facts(wire))
            self.assertEqual(row.evidence["request_sha256"], self.prep.request_sha256)
            self.assertIsNone(row.decided_by_id)
            self.assertEqual(self.job.resolutions.count(), 1)
            return Mock()
        self.driver.invoke.side_effect = invoke
        with patch("requests.adapters.HTTPAdapter.send", side_effect=network) as send:
            result = self.invoke()
        send.assert_called_once()
        self.assertIsNone(current_chat_body_observer())
        self.prep.refresh_from_db()
        self.assertEqual(self.prep.contract.prepared_request, before)
        return result

    def test_original_worker_detail_and_bounded_reader_show_only_summary(self):
        result = self.worker()
        self.assertEqual(result.status, "succeeded", result.error_message)
        self.assertIsNone(result.actual_cost)
        with CaptureQueriesContext(connection) as queries:
            note = self.note(result)
        self.assertIn("HTTP库序列化记录", note)
        self.assertIn("不是已发送或已送达", note)
        audit_queries = [q["sql"] for q in queries if "studio_assetgenerationjobresolution" in q["sql"]]
        self.assertEqual(len(audit_queries), 1, audit_queries)
        self.assertIn("LIMIT 2", audit_queries[0])
        if connection.vendor == "sqlite":
            with connection.cursor() as cursor:
                cursor.execute("EXPLAIN QUERY PLAN " + audit_queries[0])
                self.assertIn("INDEX", str(cursor.fetchall()).upper())
        self.assertNotIn("PRIVATE", note)
        self.assertNotIn("NEVER-RECORD", note)
        self.assertIsNone(AssetGenerationJobSerializer().get_latest_resolution(result))
        cached = AssetGenerationJob.objects.prefetch_related("resolutions").get(pk=result.pk)
        self.assertIsNone(AssetGenerationJobSerializer().get_latest_resolution(cached))
        self.assertEqual(_collected_actual_cost(result, {}), (None, ""))
        client = APIClient()
        client.force_authenticate(self.actor)
        response = client.get(f"/api/v1/task-center/preparation/{self.prep.pk}/")
        self.assertEqual(response.status_code, 200, response.data)
        self.assertIn(note, response.data["submission"]["notes"])
        self.assertEqual(response.data["output"]["state"], "available")
        client.force_authenticate(None)
        self.assertIn(client.get(f"/api/v1/task-center/preparation/{self.prep.pk}/").status_code, (401, 403))
        self.assertIsNone(claim_next_text_asset_job("no-replay"))

    def test_rejected_response_is_failed_not_a_success_from_body_evidence(self):
        result = self.worker(invalid=True)
        self.assertEqual((result.status, result.error_code), ("failed", "asset_text_response_invalid"))
        self.assertIsNone(result.actual_cost)
        self.assertIn("HTTP库序列化记录", self.note(result))
        self.assertIsNone(claim_next_text_asset_job("no-retry"))

    def test_uncertainty_still_needs_human_resolution_and_preserves_billing(self):
        result = self.worker(uncertain=True)
        self.assertEqual(result.status, "uncertain")
        self.assertIsNone(AssetGenerationJobSerializer().get_latest_resolution(result))
        self.assertIsNone(claim_next_text_asset_job("no-retry"))
        with self.assertRaises(ValidationError):
            resolve_uncertain_asset_generation_job(result, outcome="confirmed_remote_failed", user=self.actor,
                note="隔离远端已核对", client_idempotency_key="system:http_body_prepared")
        row, created = resolve_uncertain_asset_generation_job(result, outcome="confirmed_remote_failed", user=self.actor,
            note="隔离远端已核对，账单未提供", client_idempotency_key="human-resolution")
        self.assertTrue(created)
        self.assertEqual(row.revision, 2)
        self.assertEqual(row.decided_by_id, self.actor.pk)
        self.assertEqual(AssetGenerationJobSerializer().get_latest_resolution(result)["id"], str(row.pk))
        result.refresh_from_db()
        self.assertEqual(result.status, "failed")
        self.assertIsNone(result.actual_cost)
        self.assertEqual(_collected_actual_cost(result, {}), (None, ""))
        replay, created = resolve_uncertain_asset_generation_job(result, outcome="confirmed_remote_failed", user=self.actor,
            note="隔离远端已核对，账单未提供", client_idempotency_key="human-resolution")
        self.assertFalse(created)
        self.assertEqual(replay.pk, row.pk)
        self.assertEqual(result.resolutions.count(), 2)

    def test_operator_cannot_forge_system_event(self):
        with self.assertRaises(ApiValidationError):
            AssetGenerationResolutionInputSerializer().fields["outcome"].run_validation("http_body_prepared")
        with self.assertRaises(ValidationError):
            resolve_uncertain_asset_generation_job(self.job, outcome="http_body_prepared", note="伪造系统事件", user=self.actor)
        self.assertFalse(self.job.resolutions.exists())

    def test_cancel_during_response_keeps_body_but_no_usable_result_or_known_cost(self):
        result = self.worker(cancel=True)
        self.assertEqual(result.status, "cancelled")
        self.assertEqual(result.result_text, "")
        self.assertIsNone(result.output_asset_id)
        self.assertIsNone(result.actual_cost)
        self.assertIn("HTTP库序列化记录", self.note(result))

    def test_write_failure_stops_network_and_restores_observer_context(self):
        def invoke(*args):
            return _PinnedAddressAdapter(None, before_send=current_chat_body_observer()).send(self.wire())
        self.driver.invoke.side_effect = invoke
        with patch("studio.models.AssetGenerationJobResolution.objects.create", side_effect=OperationalError("audit unavailable")), \
                patch("requests.adapters.HTTPAdapter.send") as send, self.assertRaises(OperationalError):
            self.invoke()
        send.assert_not_called()
        self.assertIsNone(current_chat_body_observer())
        self.assertFalse(self.job.resolutions.exists())
        self.job.refresh_from_db()
        self.assertEqual(self.job.status, "submitting")
        self.assertIsNone(self.job.actual_cost)

    def test_current_claim_and_frozen_identity_are_required(self):
        claim = self.ready()
        for changes in ({"claim_token": None}, {"claim_token": uuid4()}, {"status": "cancel_requested"},
                {"ignore_late_result": True}, {"submission_started_at": None}, {"request_snapshot_sha256": "f" * 64}):
            with self.subTest(changes=changes), transaction.atomic():
                AssetGenerationJob.objects.filter(pk=claim.pk).update(**changes)
                with self.assertRaises(ValidationError):
                    self.record(claim)
                self.assertFalse(claim.resolutions.exists())
                transaction.set_rollback(True)
        for changes in ({"request_frozen_at": None}, {"request_sha256": "f" * 64}, {"status": "cancelled"}):
            # Frozen columns are immutable even in tests. Inject bad values at
            # the read boundary instead of bypassing that write protection.
            damaged = deepcopy(self.prep)
            for key, value in changes.items():
                setattr(damaged, key, value)
            with self.subTest(changes=changes), patch("studio.models.ExecutionPreparation.objects.select_for_update") as selected:
                selected.return_value.get.return_value = damaged
                with self.assertRaises(ValidationError):
                    self.record(claim)
                self.assertFalse(claim.resolutions.exists())

    def test_duplicate_and_audit_mutation_are_rejected(self):
        claim = self.ready()
        self.record(claim)
        row = claim.resolutions.get()
        with self.assertRaises(ValidationError):
            self.record(claim)
        with self.assertRaises(ValidationError):
            row.save()
        with self.assertRaises(ValidationError):
            row.delete()
        with self.assertRaises(ValidationError):
            claim.resolutions.all().update(note="changed")
        with self.assertRaises(ValidationError):
            claim.resolutions.all().delete()
        claim.refresh_from_db()
        self.assertEqual(claim.status, "submitting")
        self.assertIsNone(claim.actual_cost)

    def test_corrupt_absent_and_cross_request_records_are_explicit_not_rebuilt(self):
        claim = self.ready()
        self.assertIn("未记录", self.note(claim))
        self.record(claim)
        row = claim.resolutions.get()
        original = deepcopy(row.evidence)
        source = SimpleNamespace(resolutions=Mock())
        source.resolutions.field.attname = "job_id"
        selection = source.resolutions.filter.return_value.order_by.return_value.only.return_value
        selection.__getitem__ = Mock(return_value=[row])
        for changes in ({"preparation_id": str(uuid4())}, {"request_sha256": "f" * 64},
                {"body": {**original["body"], "size_bytes": False}}, {"PRIVATE": "NEVER-RECORD"}):
            value = json_contract({**original, **changes}, max_bytes=8192)
            row.evidence, row.evidence_size_bytes, row.evidence_sha256 = value.value, value.size_bytes, value.sha256
            note = self.note(source)
            self.assertIn("无法核验", note)
            self.assertNotIn("PRIVATE", note)
        row.evidence, row.evidence_sha256 = original, "f" * 64
        self.assertIn("无法核验", self.note(source))
        selection.__getitem__.return_value = [row, row]
        self.assertIn("无法核验", self.note(source))
        self.assertIn("HTTP库序列化记录", self.note(claim))


@override_settings(AI_PROVIDER_ALLOWED_HOSTS=("api.example.com", "cdn.example.com"))
class AssetAuxiliaryChatHttpEvidenceTests(TestCase):
    setUpTestData = auxiliary.ExecutionAssetAuxiliaryTests.__dict__["setUpTestData"]
    setUp = auxiliary.ExecutionAssetAuxiliaryTests.setUp
    write_contract = staticmethod(auxiliary.ExecutionAssetAuxiliaryTests.write_contract)
    input = staticmethod(auxiliary.ExecutionAssetAuxiliaryTests.input)
    bind = auxiliary.ExecutionAssetAuxiliaryTests.bind
    child = auxiliary.ExecutionAssetAuxiliaryTests.child

    def test_auxiliary_owns_its_evidence_and_parent_stop_blocks_network(self):
        from studio.tests.test_execution_asset_requests import freeze
        child, _ = self.child()
        prep = freeze(child, self.actor)
        AssetGenerationJob.objects.filter(pk=child.pk).update(status="queued")
        claim = claim_next_text_asset_job("isolated-child")
        permitted, claim = begin_asset_execution_submission(claim)
        self.assertTrue(permitted)
        wire = requests.Request("POST", "https://api.example.com/chat/completions", json={"messages": []}).prepare()
        for state in ("cancelled", "cancel_requested", "failed", "succeeded", "uncertain"):
            with self.subTest(state=state), transaction.atomic():
                AssetGenerationJob.objects.filter(pk=self.root.pk).update(status=state)
                with self.assertRaises(ValidationError):
                    record_asset_chat_body(claim, prep.pk, prep.request_sha256, prepared_json_body_facts(wire))
                self.assertFalse(claim.resolutions.exists())
                transaction.set_rollback(True)
        record_asset_chat_body(claim, prep.pk, prep.request_sha256, prepared_json_body_facts(wire))
        self.assertEqual(claim.resolutions.get().evidence["preparation_id"], str(prep.pk))
        self.assertFalse(self.root.resolutions.exists())
        self.assertFalse(self.prep.field_selections.exists())
