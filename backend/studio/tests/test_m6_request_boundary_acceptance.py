"""M6-A04: real Unicode field assembly at the full request envelope boundary.

Only the in-process byte limit and admission observations are test fixtures.
Compiler, field confirmation, request writer/readback and detail APIs are real;
no live limits, Smoke evidence, model calls or root queue dispatch are changed.
"""
from unittest.mock import Mock, patch

from django.core.files.storage import default_storage
from django.test import TestCase, override_settings

from studio.models import GenerationAttempt, GenerationTask, ModelDeploymentSmokeAttempt, WorkflowSmokeAttempt
from studio.services.execution_advancement import _finalize_or_record_invalid_request, advance_execution_preparation
from studio.services.execution_finalization import finalize_execution
from studio.services.execution_preparation_requests import compile_prepared_execution_transport
from studio.services.execution_request_storage import ExecutionRequestStorageError
from studio.services.json_contracts import canonical_json_bytes, json_contract
from studio.services.security_boundaries import boundary_int
from studio.tests import test_m6_overflow_acceptance as fixtures
from studio.tests.test_execution_asset_requests import isolated_observations


@override_settings(AI_PROVIDER_ALLOWED_HOSTS=("api.example.com", "127.0.0.1"))
class M6RequestBoundaryAcceptanceTests(TestCase):
    channel_limits = fixtures.M6OverflowAcceptanceTests.channel_limits
    input = staticmethod(fixtures.M6OverflowAcceptanceTests.input)
    write = fixtures.M6OverflowAcceptanceTests.write
    resolve = fixtures.M6OverflowAcceptanceTests.resolve
    prepare = fixtures.M6OverflowAcceptanceTests.prepare
    setUp = fixtures.M6OverflowAcceptanceTests.setUp
    inspect = fixtures.M6OverflowAcceptanceTests.inspect

    def confirmed_request(self):
        self.prepare()
        self.originals = ["Café 😀 turns slowly. " * 8, "Résumé 😀 opens here. " * 8]
        for unit, text in zip(("first", "second"), self.originals):
            self.prep.refresh_from_db()
            response = self.client.post("/api/v1/execution-field-values/", {
                "preparation_id": str(self.prep.pk), "unit_key": unit, "field_key": "unit_description",
                "text": text, "revision": 1, "expected_preparation_revision": self.prep.revision}, format="json")
            self.assertEqual(response.status_code, 201, response.data)
            field = response.data["field"]
            self.prep.refresh_from_db()
            response = self.client.post(f"/api/v1/execution-field-values/{field['id']}/confirm/", {
                "expected_text_sha256": field["text_sha256"], "expected_source_sha256": field["source_sha256"],
                "expected_preparation_revision": self.prep.revision, "select_for_execution": True}, format="json")
            self.assertEqual(response.status_code, 200, response.data)
        self.before = self.inspect()
        prompt = self.before["prompt"]
        self.assertEqual(prompt["mode"], "preview")
        self.assertEqual({row["origin"] for row in prompt["field_basis"]}, {"adopted"})
        text = next(row["text"] for row in prompt["channels"] if row["key"] == "positive_prompt")
        self.assertLess(len(text), 4000)
        self.assertGreater(len(text.encode("utf-8")), len(text))
        for original in self.originals:
            self.assertEqual(text.count(original), 1)
        self.preview = compile_prepared_execution_transport(preparation_id=self.prep.pk,
            expected_preparation_revision=self.prep.revision, actor=self.user)
        self.request = json_contract({"schema_version": 1, "preparation_id": str(self.prep.pk),
            "basis_preparation_revision": self.prep.revision, "intent_sha256": self.prep.intent_sha256,
            "field_selections": self.preview.field_selections.value, "transport": self.preview.transport.value})
        self.assertLess(self.preview.transport.size_bytes, self.request.size_bytes - 1)
        self.baseline_counts = self.execution_counts()

    @staticmethod
    def execution_counts():
        return tuple(model.objects.count() for model in
            (GenerationAttempt, GenerationTask, ModelDeploymentSmokeAttempt, WorkflowSmokeAttempt))

    def limit(self, maximum):
        # Patch the defining module's lookup: imported max-bytes helpers used by
        # transport, envelope, readback and metrics must observe the same limit.
        return patch("studio.services.execution_request_storage.boundary_int",
            side_effect=lambda key: maximum if key == "security.snapshot.stage_max_bytes" else boundary_int(key))

    def finalize(self, collector):
        return finalize_execution(preparation_id=self.prep.pk, expected_preparation_revision=self.prep.revision,
            actor=self.user, collect_admission_facts=collector)

    def test_one_byte_short_rejects_only_full_envelope_without_storage_or_admission(self):
        self.confirmed_request()
        basis_revision = self.prep.revision
        collector = Mock(side_effect=AssertionError("Envelope rejection must precede admission"))
        with self.limit(self.request.size_bytes - 1), patch.object(default_storage, "save",
                side_effect=AssertionError("Rejected request must not create a file")) as save:
            transport = compile_prepared_execution_transport(preparation_id=self.prep.pk,
                expected_preparation_revision=basis_revision, actor=self.user)
            self.assertEqual(transport, self.preview)
            with self.assertRaises(ExecutionRequestStorageError):
                self.finalize(collector)
            self.prep.refresh_from_db()
            self.assertEqual(self.prep.revision, basis_revision)
            self.prep = _finalize_or_record_invalid_request(self.prep, self.user, collector)
            blocked = self.inspect()
            collector.assert_not_called()
            save.assert_not_called()
        self.assertEqual((self.prep.status, self.prep.reason_code), ("blocked", "execution_request_storage_invalid"))
        self.assertEqual(self.prep.revision, basis_revision + 1)
        self.assertIsNone(self.prep.request_frozen_at)
        self.assertIsNone(self.prep.next_check_at)
        self.assertEqual(blocked["preparation"]["evaluation"]["phase"], "request_storage")
        self.assertEqual(blocked["prompt"]["mode"], "preview")
        for key in ("channels", "field_basis", "length_diagnostics"):
            self.assertEqual(blocked["prompt"][key], self.before["prompt"][key])
        metrics = {row["key"]: row for row in blocked["metrics"]["items"]}
        self.assertEqual(metrics["prepared_request"]["state"], "not_generated")
        self.assertEqual(metrics["prepared_request"]["current_limit_bytes"], self.request.size_bytes - 1)
        for key in ("size_bytes", "sha256", "schema_version"):
            self.assertIsNone(metrics["prepared_request"][key])
        with patch("studio.services.execution_advancement.finalize_execution",
                side_effect=AssertionError("Blocked envelope cannot be automatically retried")):
            unchanged = advance_execution_preparation(preparation_id=self.prep.pk,
                expected_preparation_revision=self.prep.revision, actor=self.user)
        self.assertEqual(unchanged.revision, self.prep.revision)
        self.assertEqual(list(self.media_root.rglob("*")), [])
        self.assertEqual(self.execution_counts(), self.baseline_counts)

    def test_exact_full_envelope_boundary_freezes_original_bytes_and_replays_once(self):
        self.confirmed_request()
        basis_revision = self.prep.revision
        collector = Mock(side_effect=isolated_observations)
        with self.limit(self.request.size_bytes), patch.object(default_storage, "save", wraps=default_storage.save) as save:
            self.prep = self.finalize(collector)
            self.assertEqual(self.prep.status, "ready", self.prep.reason)
            collector.assert_called_once()
            self.assertEqual(collector.call_args.args[2], self.request)
            save.assert_called_once()
            detail = self.prep.contract
            request_file = self.media_root / detail.prepared_request_key
            self.assertEqual(request_file.read_bytes(), canonical_json_bytes(self.request.value))
            self.assertEqual(detail.prepared_request, self.request.value)
            frozen = self.inspect()
            self.assertEqual(frozen["prompt"]["mode"], "frozen")
            for key in ("channels", "field_basis", "length_diagnostics", "profile_basis"):
                self.assertEqual(frozen["prompt"][key], self.before["prompt"][key])
            metrics = next(row for row in frozen["metrics"]["items"] if row["key"] == "prepared_request")
            self.assertEqual((metrics["state"], metrics["size_bytes"], metrics["current_limit_bytes"],
                metrics["over_current_limit_bytes"], metrics["sha256"]),
                ("recorded", self.request.size_bytes, self.request.size_bytes, 0, self.request.sha256))
            self.assertEqual(self.prep.request_sha256, self.request.sha256)
            with patch("studio.services.execution_finalization.compile_prepared_execution_transport",
                    side_effect=AssertionError("Frozen replay must not compile")):
                replay = finalize_execution(preparation_id=self.prep.pk, expected_preparation_revision=basis_revision,
                    actor=self.user, collect_admission_facts=collector)
            self.assertEqual((replay.revision, replay.request_sha256), (self.prep.revision, self.request.sha256))
            collector.assert_called_once()
            save.assert_called_once()
        self.assertEqual(list(self.media_root.rglob("*.json")), [request_file])
        self.assertEqual(self.execution_counts(), self.baseline_counts)
        self.job.refresh_from_db()
        self.assertEqual(self.job.status, "preparing")
