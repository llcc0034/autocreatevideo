"""Original correction transaction through the M2 parser/writer dispatch.

The native registry remains an explicit fixture until authoring cutover.
Request files, parser and candidate dispatch, candidate writer,
review records, root locks and rollback all use their real implementations.
No model, live service or adoption is exercised.
"""
from unittest.mock import patch
from copy import deepcopy
import json

from django.contrib.auth import get_user_model
from django.core.exceptions import PermissionDenied, ValidationError
from django.core.files.base import ContentFile
from django.core.files.storage import default_storage
from django.db import IntegrityError
from django.test import TestCase, override_settings

from studio.models import (ApprovalDecision, ArtifactSelectionDecision, ExecutionTextResponseReview,
    GeneratedArtifact, GenerationAttempt, ScriptRevision)
from studio.services.execution_response_storage import ExecutionResponseStorageError, store_response
from studio.services.execution_text_response_review import complete_text_response
from studio.services.json_contracts import assign_json_contract
from studio.services.script_semantic_candidates import read_script_semantic_candidate
from studio.services.execution_text_contracts import parse_execution_text_response
from studio.services.script_semantics import ScriptSemanticValidationError
from studio.tests import test_script_semantic_candidate_requests as fixtures


@override_settings(STORAGES={
    "default": {"BACKEND": "django.core.files.storage.InMemoryStorage"},
    "staticfiles": {"BACKEND": "django.contrib.staticfiles.storage.StaticFilesStorage"},
})
class ScriptSemanticResponseRecoveryTests(TestCase):
    setUp = fixtures.ScriptSemanticCandidateRequestTests.setUp
    make_output = fixtures.ScriptSemanticCandidateRequestTests.make_output
    counts = fixtures.ScriptSemanticCandidateRequestTests.counts
    call = fixtures.ScriptSemanticCandidateRequestTests.call
    freeze = fixtures.ScriptSemanticCandidateRequestTests.freeze

    def retained(self, *, operation="script.generate"):
        _, self.transport = self.freeze(operation=operation)
        self.prep = self.attempt.preparation
        self.attempt.status = "awaiting_review"
        self.attempt.save(update_fields=("status",))
        key, size, digest = store_response(' {"incomplete": ')
        self.review = ExecutionTextResponseReview(attempt=self.attempt,
            original_file=key, original_size=size, original_sha256=digest)
        assign_json_contract(self.review, "diagnostics", {"schema_version": 1,
            "contract_error_code": "response_contract_invalid", "summary": "Isolated invalid response"})
        self.review.save()

    def complete(self, text=None, **changes):
        args = {"preparation_id": self.prep.pk, "expected_preparation_revision": self.prep.revision,
            "review_id": self.review.pk, "original_sha256": self.review.original_sha256,
            "text": self.text if text is None else text, "actor": self.actor, **changes}
        return complete_text_response(**args)

    def assert_unconfirmed(self):
        self.review.refresh_from_db(); self.attempt.refresh_from_db(); self.prep.refresh_from_db()
        self.assertIsNone(self.review.confirmed_at)
        self.assertIsNone(self.review.confirmed_by_id)
        self.assertFalse(self.review.corrected_file)
        self.assertFalse(self.review.corrected_sha256)
        self.assertEqual((self.attempt.status, self.attempt.result_sha256), ("awaiting_review", ""))
        self.assertEqual(self.prep.revision, 2)
        self.assertEqual(self.counts(), (0, 0, 0, 0))
        self.assertEqual(self.review.original_text, ' {"incomplete": ')

    def test_original_confirmation_transaction_commits_real_bytes_before_candidate_and_settlement(self):
        self.retained()
        fixed = self.complete()
        self.assertEqual(fixed.revision, 3)
        self.attempt.refresh_from_db(); self.review.refresh_from_db()
        self.assertEqual(self.attempt.status, "succeeded")
        artifact = GeneratedArtifact.objects.get(generation_attempt=self.attempt)
        detail = read_script_semantic_candidate(artifact, max_bytes=fixtures.LIMIT)
        self.assertEqual(detail["text"], self.text)
        self.assertEqual(detail["output_contract"], self.output_ref)
        self.assertEqual(detail["candidate"], self.raw)
        self.assertEqual(detail["human_response_recovery"]["corrected_sha256"], self.review.corrected_sha256)
        revision = ScriptRevision.objects.get(origin_generation_attempt=self.attempt)
        self.assertEqual((revision.source.source_type, revision.status), ("paste", "preview"))
        self.assertIsNone(revision.confirmed_at)
        self.assertEqual(self.counts(), (1, 1, 1, 1))
        self.assertFalse(ApprovalDecision.objects.exists())
        self.assertFalse(ArtifactSelectionDecision.objects.exists())
        self.project.refresh_from_db(); self.run.refresh_from_db()
        self.assertEqual(self.project.master_script, "")
        self.assertIsNone(self.run.script_revision_id)

    def test_structure_operation_uses_the_same_transaction_and_original_source_contract(self):
        self.retained(operation="script.structure")
        self.complete()
        detail = read_script_semantic_candidate(GeneratedArtifact.objects.get(generation_attempt=self.attempt),
            max_bytes=fixtures.LIMIT)
        self.assertEqual(detail["operation"], "script.structure")
        self.assertEqual(detail["text"], self.text)

    def test_bad_corrected_json_does_not_save_confirmation_or_candidate(self):
        self.retained()
        with self.assertRaises(ValidationError):
            self.complete(text="not JSON")
        self.assert_unconfirmed()

    def test_corrected_storage_failure_does_not_confirm_or_call_the_candidate_writer(self):
        self.retained()
        with patch("studio.services.execution_response_storage.store_response",
                side_effect=ExecutionResponseStorageError("isolated correction storage failure")), \
                patch("studio.services.execution_production_text_worker._candidate", side_effect=AssertionError("no candidate before stored correction")):
            with self.assertRaises(ExecutionResponseStorageError):
                self.complete()
        self.assert_unconfirmed()

    def test_candidate_failure_rolls_back_confirmation_and_all_business_rows(self):
        self.retained()
        with patch.object(GeneratedArtifact, "save", side_effect=IntegrityError("isolated candidate write failure")):
            with self.assertRaises(IntegrityError):
                self.complete()
        self.assert_unconfirmed()
        self.complete()  # Explicit repeat after failure, never another model call.
        self.assertEqual(self.counts(), (1, 1, 1, 1))

    def test_attempt_settlement_failure_rolls_back_confirmation_and_candidate(self):
        self.retained()
        original_save = GenerationAttempt.save
        def fail_settlement(attempt, *args, **kwargs):
            if attempt.status == "succeeded":
                raise IntegrityError("isolated settlement failure")
            return original_save(attempt, *args, **kwargs)
        with patch.object(GenerationAttempt, "save", fail_settlement), self.assertRaises(IntegrityError):
            self.complete()
        self.assert_unconfirmed()

    def test_identical_replay_is_read_only_and_a_different_correction_is_rejected(self):
        self.retained()
        fixed = self.complete()
        with patch("studio.services.execution_text_response_review.parse_execution_text_response", side_effect=AssertionError("no repeat parse")), \
                patch("studio.services.execution_production_text_worker._candidate", side_effect=AssertionError("no repeat candidate")), \
                patch("studio.services.execution_response_storage.store_response", side_effect=AssertionError("no repeat storage")):
            repeated = self.complete(expected_preparation_revision=fixed.revision)
            self.assertEqual(repeated.revision, fixed.revision)
            with self.assertRaises(ValidationError):
                self.complete(text=self.text + " ", expected_preparation_revision=fixed.revision)
        self.assertEqual(self.counts(), (1, 1, 1, 1))

    def test_stale_revision_and_wrong_original_digest_leave_the_original_pending(self):
        self.retained()
        for changes in ({"expected_preparation_revision": 1}, {"original_sha256": "f" * 64}):
            with self.subTest(changes=changes), self.assertRaises(ValidationError):
                self.complete(**changes)
            self.assert_unconfirmed()

    def test_common_candidate_writer_rejects_a_recovery_marker_without_confirmed_bytes(self):
        from studio.services.execution_production_candidates import new_production_text_candidate
        self.retained()
        with self.assertRaises(ValidationError):
            new_production_text_candidate(self.attempt, self.stage, self.run, result={}, max_bytes=fixtures.LIMIT,
                recovery={"review_id": str(self.review.pk), "original_sha256": self.review.original_sha256,
                    "corrected_sha256": "a" * 64, "confirmed_by": str(self.actor.pk)})
        self.assert_unconfirmed()

    def test_copied_request_file_is_not_the_original_request_reference(self):
        self.retained()
        with default_storage.open(self.attempt.request_snapshot_key, "rb") as stream:
            duplicate = default_storage.save("isolated-tests/copied-request.json", ContentFile(stream.read()))
        GenerationAttempt.objects.filter(pk=self.attempt.pk).update(request_snapshot_key=duplicate)
        with patch("studio.services.execution_text_response_review.parse_execution_text_response", side_effect=AssertionError("wrong request must not parse")), \
                self.assertRaises(ValidationError):
            self.complete()
        self.assert_unconfirmed()

    def test_other_user_cannot_confirm_or_create_a_candidate(self):
        self.retained()
        stranger = get_user_model().objects.create_user(username="not-the-script-owner")
        with self.assertRaises(PermissionDenied):
            self.complete(actor=stranger)
        self.assert_unconfirmed()

    def test_original_settlement_retains_semantic_paths_and_repair_does_not_adopt(self):
        from studio.services.execution_production_text_worker import finish_production_text
        from studio.services.execution_text_response_review import describe_text_response_review

        _, self.transport = self.freeze()
        self.prep = self.attempt.preparation
        bad = deepcopy(self.raw)
        bad["episodes"][0]["shots"][0]["references"][0]["variant_key"] = "not-declared-private-value"
        text = json.dumps(bad, ensure_ascii=False, indent=2) + "\r\n"
        with self.assertRaises(ScriptSemanticValidationError) as caught:
            parse_execution_text_response(self.attempt.operation, {"text": text}, operation_plan=self.transport["operation_plan"],
                request_payload=self.transport["payload"], expected_output=self.output_ref)
        result = finish_production_text(self.attempt, status="failed", rejected_response={"text": text},
            validation_error=caught.exception)
        self.assertEqual(result.status, "awaiting_review")
        self.review = ExecutionTextResponseReview.objects.get(attempt=self.attempt)
        view = describe_text_response_review(self.prep)
        self.assertEqual(view["original_text"], text)
        diagnostic = view["diagnostics"]
        self.assertEqual(diagnostic["validation_code"], "script_semantic_result_invalid")
        self.assertEqual(diagnostic["relation_errors"][0]["code"], "variant_unknown")
        source_path = "source.episodes[0].shots[0].references[0].variant_key"
        self.assertEqual(diagnostic["relation_errors"][0]["source_path"], source_path)
        self.assertIn(source_path, diagnostic["structure_errors"])
        self.assertNotIn("not-declared-private-value", json.dumps(diagnostic))
        self.assertEqual(self.counts(), (0, 0, 0, 0))
        self.complete()
        self.assertEqual(self.review.original_text, text)
        self.assertEqual(self.counts(), (1, 1, 1, 1))
        self.assertFalse(ApprovalDecision.objects.exists())
        self.assertFalse(ArtifactSelectionDecision.objects.exists())
