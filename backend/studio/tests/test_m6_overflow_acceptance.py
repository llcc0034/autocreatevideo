"""M6-A04: formal field approval leaves over-limit Unicode fully inspectable.

No live data, boundary changes, model submission or request-file fabrication.
"""
import json
import os
from pathlib import Path
from tempfile import TemporaryDirectory
from unittest.mock import patch

from django.test import TestCase, override_settings
from rest_framework.test import APIClient

from studio.models import GenerationAttempt, GenerationTask
from studio.services.execution_advancement import _finalize_or_record_invalid_request
from studio.services.execution_finalization import finalize_execution
from studio.services.execution_preparation_requests import compile_prepared_execution_transport
from studio.services.json_contracts import json_contract
from studio.services.prompt_profiles import PromptProfileChannelLengthError
from studio.tests import test_execution_reference_preparation as fixtures
from studio.tests.test_execution_prompt_length_display import assert_diagnostic


@override_settings(AI_PROVIDER_ALLOWED_HOSTS=("api.example.com", "127.0.0.1"))
class M6OverflowAcceptanceTests(TestCase):
    channel_limits = {"positive_prompt": {"max_chars": 4000, "warn_at_chars": None}}
    input = staticmethod(fixtures.ExecutionReferencePreparationTests.input)
    write = fixtures.ExecutionReferencePreparationTests.write
    resolve = fixtures.ExecutionReferencePreparationTests.resolve
    prepare = fixtures.ExecutionReferencePreparationTests.prepare

    def setUp(self):
        temporary = TemporaryDirectory()
        self.addCleanup(temporary.cleanup)
        self.media_root = Path(temporary.name)
        self.enterContext(override_settings(MEDIA_ROOT=temporary.name))
        for target in ("studio.services.provider_registry.resolve_credential",
                "studio.services.provider_registry._request_json", "studio.services.comfyui.ComfyUIClient"):
            self.enterContext(patch(target, side_effect=AssertionError("No remote execution in acceptance test")))
        fixtures.ExecutionReferencePreparationTests.setUp(self)
        self.client = APIClient()
        self.client.force_authenticate(self.user)

    def inspect(self):
        self.prep.refresh_from_db()
        base = f"/api/v1/execution-preparations/{self.prep.pk}/"
        responses = [self.client.get(base + suffix, {"expected_preparation_revision": self.prep.revision})
            for suffix in ("prompt-display/", "storage-metrics/")]
        for response in responses:
            self.assertEqual(response.status_code, 200, response.data)
        detail = self.client.get(base)
        self.assertEqual(detail.status_code, 200, detail.data)
        return {"preparation": detail.json(), "prompt": responses[0].json(), "metrics": responses[1].json()}

    def test_unicode_cumulative_overflow_remains_complete_after_formal_confirmation_and_block(self):
        self.prepare()
        originals = [("Café 😀 turns slowly. " * 99)[:2000], ("Résumé 😀 opens here. " * 99)[:2000]]
        self.assertEqual([len(text) for text in originals], [2000, 2000])
        fields = []
        before = (GenerationAttempt.objects.count(), GenerationTask.objects.count())
        for unit, text in zip(("first", "second"), originals):
            self.prep.refresh_from_db()
            response = self.client.post("/api/v1/execution-field-values/", {"preparation_id": str(self.prep.pk),
                "unit_key": unit, "field_key": "unit_description", "text": text, "revision": 1,
                "expected_preparation_revision": self.prep.revision}, format="json")
            self.assertEqual(response.status_code, 201, response.data)
            fields.append(response.data["field"])
        candidate = self.inspect()
        assert_diagnostic(self, candidate["prompt"])
        self.assertEqual({row["origin"] for row in candidate["prompt"]["field_basis"]}, {"candidate"})
        old_revision = self.prep.revision
        journey = {"candidate": candidate, "fields": [], "confirmations": []}
        for field in fields:
            response = self.client.get(f"/api/v1/execution-field-values/{field['id']}/")
            self.assertEqual(response.status_code, 200, response.data)
            journey["fields"].append(response.json())
        for field in fields:
            self.prep.refresh_from_db()
            response = self.client.post(f"/api/v1/execution-field-values/{field['id']}/confirm/", {
                "expected_text_sha256": field["text_sha256"], "expected_source_sha256": field["source_sha256"],
                "expected_preparation_revision": self.prep.revision, "select_for_execution": True}, format="json")
            self.assertEqual(response.status_code, 200, response.data)
            journey["confirmations"].append({"response": response.json(), "after": self.inspect()})
        adopted = self.inspect()
        assert_diagnostic(self, adopted["prompt"])
        self.assertEqual(adopted["prompt"]["channels"], candidate["prompt"]["channels"])
        self.assertEqual({row["origin"] for row in adopted["prompt"]["field_basis"]}, {"adopted"})
        self.assertEqual({row["field_value_id"] for row in adopted["prompt"]["field_basis"]}, {row["id"] for row in fields})
        text = next(row["text"] for row in adopted["prompt"]["channels"] if row["key"] == "positive_prompt")
        for original in originals:
            self.assertEqual(text.count(original), 1)
        self.assertGreater(len(text), 4000)
        self.assertGreater(len(text.encode("utf-8")), len(text))
        base = f"/api/v1/execution-preparations/{self.prep.pk}/"
        for suffix in ("prompt-display/", "storage-metrics/"):
            response = self.client.get(base + suffix, {"expected_preparation_revision": old_revision})
            self.assertEqual(response.status_code, 409, response.data)
        with patch("studio.services.execution_request_storage.default_storage.save",
                side_effect=AssertionError("Oversized compilation cannot write a request")) as save:
            with self.assertRaises(PromptProfileChannelLengthError):
                compile_prepared_execution_transport(preparation_id=self.prep.pk,
                    expected_preparation_revision=self.prep.revision, actor=self.user)
            collector = lambda *_: self.fail("Admission cannot run after length rejection")
            with self.assertRaises(PromptProfileChannelLengthError):
                finalize_execution(preparation_id=self.prep.pk, expected_preparation_revision=self.prep.revision,
                    actor=self.user, collect_admission_facts=collector)
            self.prep = _finalize_or_record_invalid_request(self.prep, self.user, collector)
            save.assert_not_called()
        self.assertEqual((self.prep.status, self.prep.reason_code), ("blocked", "execution_request_invalid"))
        self.assertIsNone(self.prep.next_check_at)
        self.assertIsNone(self.prep.request_frozen_at)
        blocked = self.inspect()
        assert_diagnostic(self, blocked["prompt"])
        self.assertEqual(blocked["prompt"]["channels"], adopted["prompt"]["channels"])
        self.assertEqual(blocked["prompt"]["field_basis"], adopted["prompt"]["field_basis"])
        rows = {row["key"]: row for row in blocked["metrics"]["items"]}
        self.assertEqual(rows["evaluation"]["state"], "recorded")
        self.assertEqual(blocked["preparation"]["evaluation"]["phase"], "compilation")
        self.assertEqual(rows["prepared_request"]["state"], "not_generated")
        for key in ("size_bytes", "sha256", "schema_version"):
            self.assertIsNone(rows["prepared_request"][key])
        previous = {row["key"]: row for row in candidate["metrics"]["items"]}
        for key in ("intent", "resolved_inputs", "review_contract"):
            self.assertEqual(rows[key], previous[key])
            actual = json_contract(blocked["preparation"][key])
            self.assertEqual((rows[key]["size_bytes"], rows[key]["sha256"]), (actual.size_bytes, actual.sha256))
        self.assertEqual((GenerationAttempt.objects.count(), GenerationTask.objects.count()), before)
        self.assertEqual(list(self.media_root.rglob("*")), [])
        if directory := os.environ.get("M6_ACCEPTANCE_RECEIPTS"):
            output = Path(directory)
            output.mkdir(parents=True, exist_ok=True)
            (output / "overflow.json").write_text(json.dumps([candidate, adopted, blocked], ensure_ascii=False, indent=2), encoding="utf-8")
            (output / "confirmation-journey.json").write_text(json.dumps(journey, ensure_ascii=False, indent=2), encoding="utf-8")
