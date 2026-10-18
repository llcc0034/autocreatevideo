"""Isolated local evidence projection; no external calls or business writes."""
from copy import deepcopy
from datetime import datetime, timedelta, timezone
from hashlib import sha256
from types import SimpleNamespace

from django.test import SimpleTestCase

from studio.task_center_submission import submission_evidence, SUPPORTED_KINDS


class SubmissionEvidenceTests(SimpleTestCase):
    def source(self, **values):
        return SimpleNamespace(submission_started_at=None, submitted_at=None, provider_request_id="", **values)

    def test_no_source_or_unsupported_kind_is_not_a_claim_of_no_submission(self):
        for source, kind in ((None, "workflow_smoke"), (object(), "assembly"), (object(), "generation_task")):
            result = submission_evidence(source, kind)
            self.assertEqual(result["state"], "unavailable")
            self.assertEqual(result["identifier_state"], "unavailable")
            self.assertIn("不推断未发送", result["message"])

    def test_all_supported_models_have_local_fields_and_no_queries(self):
        from studio.models import GenerationAttempt, AssetGenerationJob, ModelDeploymentSmokeAttempt, WorkflowSmokeAttempt
        for cls, kind in ((GenerationAttempt, "generation_attempt"), (AssetGenerationJob, "asset_job"),
                          (ModelDeploymentSmokeAttempt, "deployment_smoke"), (WorkflowSmokeAttempt, "workflow_smoke")):
            result = submission_evidence(cls(), kind)
            self.assertEqual(result["state"], "recorded")
            self.assertIsNone(result["started_at"])
            self.assertIsNone(result["registered_at"])
            self.assertEqual(result["identifier_state"], "not_recorded")
            self.assertIn("不证明从未外呼", result["message"])

    def test_started_preassigned_id_and_registered_time_remain_separate(self):
        source = self.source()
        source.submission_started_at = datetime(2026, 9, 24, tzinfo=timezone.utc)
        source.provider_request_id = "preassigned-private-identifier"
        source.status = "failed"
        before = deepcopy(vars(source))
        result = submission_evidence(source, "workflow_smoke")
        self.assertEqual(result["state"], "recorded")
        self.assertIsNone(result["registered_at"])
        self.assertEqual(result["identifier_state"], "recorded")
        self.assertNotIn(source.provider_request_id, str(result))
        self.assertEqual(vars(source), before)
        self.assertIn("发送前分配", "".join(result["notes"]))
        # Registration from a recovery/operator is not backfilled into start.
        source.submission_started_at = None
        source.submitted_at = datetime(2026, 9, 24, tzinfo=timezone.utc)
        result = submission_evidence(source, "workflow_smoke")
        self.assertEqual(result["state"], "recorded")
        self.assertIsNone(result["started_at"])
        self.assertEqual(result["registered_at"], source.submitted_at)

    def test_digest_only_matching_and_mismatch_never_expose_identifiers(self):
        source = self.source(provider_request_id_sha256=sha256(b"private").hexdigest())
        self.assertEqual(submission_evidence(source, "asset_job")["identifier_state"], "digest_only")
        source.provider_request_id = "private"
        result = submission_evidence(source, "asset_job")
        self.assertEqual(result["state"], "recorded")
        self.assertNotIn("private", str(result))
        self.assertNotIn(source.provider_request_id_sha256, str(result))
        for digest in ("bad", "0" * 64, None):
            source.provider_request_id_sha256 = digest
            result = submission_evidence(source, "asset_job")
            self.assertEqual(result["state"], "inconsistent")
            self.assertEqual(result["identifier_state"], "inconsistent")

    def test_bad_chronology_or_types_explicit_without_synthesizing_dates(self):
        source = self.source()
        source.submission_started_at = datetime(2026, 9, 24, tzinfo=timezone.utc)
        source.submitted_at = source.submission_started_at - timedelta(seconds=1)
        result = submission_evidence(source, "deployment_smoke")
        self.assertEqual(result["state"], "inconsistent")
        self.assertEqual(result["registered_at"], source.submitted_at)
        source.submission_started_at = "private invalid date"
        result = submission_evidence(source, "deployment_smoke")
        self.assertEqual(result["state"], "inconsistent")
        self.assertIsNone(result["started_at"])
        self.assertNotIn("private", str(result))

    def test_task_status_cannot_invent_a_registration(self):
        for kind in SUPPORTED_KINDS:
            for status in ("queued", "uncertain", "cancelled", "succeeded", "failed"):
                result = submission_evidence(self.source(status=status), kind)
                self.assertIsNone(result["registered_at"])
                self.assertEqual(result["identifier_state"], "not_recorded")
