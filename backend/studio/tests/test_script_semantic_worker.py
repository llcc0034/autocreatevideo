"""Original queue/worker/repair with an isolated provider and definition fixture.

Only native protocol admission, stored instance authoring and remote submission
are fixtures. Saved request validation, parser dispatch, response retention,
candidate transactions and repair use their real implementations. No real model
or current business configuration is exercised.
"""
import json
from unittest.mock import Mock, patch

from django.test import TestCase, override_settings
from django.utils import timezone

from studio.models import (ApprovalDecision, ArtifactSelectionDecision, ExecutionTextResponseReview,
    GenerationAttempt, ScriptRevision)
from studio.services.business_outputs import output_reference, write_output
from studio.services.execution_response_storage import ExecutionResponseStorageError
from studio.services.execution_text_response_review import complete_text_response, describe_text_response_review
from studio.services.script_semantic_candidates import read_script_semantic_candidate
from studio.services.text_generation_queue import claim_next_text_attempt, process_text_attempt
from studio.tests import test_script_semantic_candidate_requests as fixtures


@override_settings(STORAGES={
    "default": {"BACKEND": "django.core.files.storage.InMemoryStorage"},
    "staticfiles": {"BACKEND": "django.contrib.staticfiles.storage.StaticFilesStorage"},
})
class ScriptSemanticWorkerTests(TestCase):
    setUp = fixtures.ScriptSemanticCandidateRequestTests.setUp
    make_output = fixtures.ScriptSemanticCandidateRequestTests.make_output
    counts = fixtures.ScriptSemanticCandidateRequestTests.counts
    call = fixtures.ScriptSemanticCandidateRequestTests.call
    freeze = fixtures.ScriptSemanticCandidateRequestTests.freeze

    def queued(self, **changes):
        _, self.transport = self.freeze(**changes)
        self.prep = self.attempt.preparation
        self.attempt.status, self.attempt.estimated_cost, self.attempt.committed_cost = "queued", 0, 0
        self.attempt.save(update_fields=("status", "estimated_cost", "committed_cost"))

    def process(self, text, *, late=False, expected_calls=1):
        claim = claim_next_text_attempt("isolated-script-semantic-worker")
        self.assertIsNotNone(claim)
        peer = Mock()

        def invoke(*args):
            if late:
                GenerationAttempt.objects.filter(pk=claim.pk).update(ignore_late_result=True)
            return {"text": text, "request_id": "isolated-script-semantic-response"}

        def admitted(attempt):
            attempt.submission_started_at, attempt.status = timezone.now(), "submitting"
            attempt.save(update_fields=("submission_started_at", "status", "updated_at"))
            return True, attempt

        peer.invoke.side_effect = invoke
        with patch("studio.services.execution_production_text_worker.begin_production_execution_submission", side_effect=admitted), \
                patch("studio.services.execution_production_text_worker.driver_for", return_value=peer), \
                patch("studio.services.execution_native_text.parse_json_object_text", side_effect=AssertionError("no legacy parser")):
            result = process_text_attempt(claim)
            # Re-delivery of the same completed claim must not call or save again.
            process_text_attempt(result)
        self.assertEqual(peer.invoke.call_count, expected_calls, f"{result.status}: {result.error_code}: {result.error_message}")
        if expected_calls:
            self.assertEqual(peer.invoke.call_args.args[1:], ("chat_completion", self.transport["payload"]))
        return result

    def assert_candidate(self, result):
        self.assertEqual(result.status, "succeeded", result.error_message)
        detail = read_script_semantic_candidate(result.artifacts.get(), max_bytes=fixtures.LIMIT)
        self.assertEqual(detail["candidate"], self.raw)
        self.assertEqual(detail["text"], self.text)
        self.assertEqual(detail["output_contract"], self.output_ref)
        self.assertEqual(self.counts(), (1, 1, 1, 1))
        self.assertEqual(ExecutionTextResponseReview.objects.filter(attempt=result).count(), 1)
        self.assertFalse(ApprovalDecision.objects.exists())
        self.assertFalse(ArtifactSelectionDecision.objects.exists())
        self.project.refresh_from_db(); self.run.refresh_from_db()
        self.assertEqual(self.project.master_script, "")
        self.assertIsNone(self.run.script_revision_id)
        return detail

    def test_generate_uses_frozen_request_and_retains_exact_original_once(self):
        self.queued()
        result = self.process(self.text)
        self.assert_candidate(result)
        review = describe_text_response_review(self.prep)
        self.assertEqual(review["original_text"], self.text)
        self.assertFalse(review["editable"])
        revision = ScriptRevision.objects.get(origin_generation_attempt=result)
        self.assertEqual((revision.source.source_type, revision.status), ("ai_generated", "preview"))
        self.assertIsNone(revision.confirmed_at)

    def test_structure_uses_other_configured_layout_without_rewriting_source(self):
        self.output, self.raw = self.make_output("b")
        self.output_ref = output_reference(self.output)
        self.text = " \r\n" + json.dumps(self.raw, ensure_ascii=False) + "\r\n "
        self.queued(operation="script.structure")
        result = self.process(self.text)
        self.assert_candidate(result)
        self.assertEqual(self.transport["operation_plan"]["plan"]["inputs"]["source_text"],
            " 原始对白\r\n不添加剧情 ")

    def test_array_root_and_custom_false_zero_null_are_not_object_only(self):
        from studio.tests.output_structure_fixtures import response_structure
        from studio.tests.script_semantics_fixtures import script_example

        raw, _, mapping = script_example()

        def wrap_root(node):
            if isinstance(node, dict):
                if node.get("kind") == "path" and node.get("scope") == "root":
                    node["path"] = [0, *node["path"]]
                else:
                    for value in node.values():
                        wrap_root(value)
            elif isinstance(node, list):
                for value in node:
                    wrap_root(value)

        wrap_root(mapping)
        structure = response_structure(example=[raw])
        self.output = write_output(actor=self.actor, data={"name": "Worker list root",
            "parser_key": "script_semantics.v1", "data_structure_version": str(structure.pk), "semantic_mapping": mapping})
        self.output = write_output(actor=self.actor, output_id=self.output.pk,
            expected_revision=self.output.revision, action="publish")
        self.output_ref = output_reference(self.output)
        raw["自定义.字段"] = {"score": 0, "flag": False, "empty": None}
        self.raw, self.text = [raw], " \r\n" + json.dumps([raw], ensure_ascii=False) + "\r\n "
        self.queued()
        detail = self.assert_candidate(self.process(self.text))
        self.assertTrue(detail["warnings"])
        self.assertNotIn("source_value", detail["projection"])

    def test_invalid_response_is_retained_and_real_repair_does_not_call_model_again(self):
        self.queued()
        invalid = ' {"incomplete": '
        result = self.process(invalid)
        self.assertEqual(result.status, "awaiting_review", result.error_message)
        self.assertEqual(self.counts(), (0, 0, 0, 0))
        retained = describe_text_response_review(self.prep)
        self.assertEqual(retained["original_text"], invalid)
        self.assertTrue(retained["diagnostics"]["structure_errors"])
        review = ExecutionTextResponseReview.objects.get(attempt=result)
        with patch("studio.services.execution_production_text_worker.driver_for", side_effect=AssertionError("no model on repair")):
            complete_text_response(preparation_id=self.prep.pk, expected_preparation_revision=self.prep.revision,
                review_id=review.pk, original_sha256=review.original_sha256, text=self.text, actor=self.actor)
        result.refresh_from_db(); review.refresh_from_db()
        self.assert_candidate(result)
        self.assertEqual((review.original_text, review.corrected_text), (invalid, self.text))
        self.assertEqual(review.confirmed_by_id, self.actor.pk)
        revision = ScriptRevision.objects.get(origin_generation_attempt=result)
        self.assertEqual((revision.source.source_type, revision.status), ("paste", "preview"))

    def test_missing_semantic_fields_never_fall_back_to_old_parser(self):
        self.queued()
        result = self.process('{"master_script":"looks like old output"}')
        self.assertEqual(result.status, "awaiting_review", result.error_message)
        self.assertEqual(self.counts(), (0, 0, 0, 0))
        self.assertTrue(describe_text_response_review(self.prep)["diagnostics"]["structure_errors"])

    def test_relation_error_retains_both_business_and_actual_source_location(self):
        self.queued()
        self.raw["episodes"][0]["shots"][0]["references"][0]["entity_key"] = "missing-entity"
        invalid = json.dumps(self.raw, ensure_ascii=False)
        result = self.process(invalid)
        self.assertEqual(result.status, "awaiting_review", result.error_message)
        self.assertEqual(self.counts(), (0, 0, 0, 0))
        retained = describe_text_response_review(self.prep)
        self.assertEqual(retained["original_text"], invalid)
        error = retained["diagnostics"]["relation_errors"][0]
        self.assertEqual(error["code"], "entity_unknown")
        self.assertIn("references[0].entity_key", error["path"])
        self.assertIn("references[0].entity_key", error["source_path"])

    def test_self_consistently_saved_changed_payload_fails_before_provider(self):
        def change(transport):
            transport["payload"]["messages"][-1]["content"] = "不是冻结字段装配的请求"
        self.queued(mutate_transport=change)
        result = self.process(self.text, expected_calls=0)
        self.assertEqual((result.status, result.error_code), ("failed", "execution_request_invalid"))
        self.assertEqual(self.counts(), (0, 0, 0, 0))
        self.assertFalse(ExecutionTextResponseReview.objects.exists())

    def test_late_result_is_not_retained_as_candidate(self):
        self.queued()
        result = self.process(self.text, late=True)
        self.assertEqual((result.status, result.error_code), ("cancelled", "late_result_ignored"))
        self.assertEqual(self.counts(), (0, 0, 0, 0))
        self.assertFalse(ExecutionTextResponseReview.objects.exists())

    def test_failed_original_storage_does_not_report_success_or_create_candidate(self):
        self.queued()
        with patch("studio.services.execution_response_storage.store_response",
                side_effect=ExecutionResponseStorageError("isolated storage failure")):
            result = self.process(self.text)
        self.assertEqual((result.status, result.error_code), ("failed", "execution_response_storage_failed"))
        self.assertEqual(self.counts(), (0, 0, 0, 0))
        self.assertFalse(ExecutionTextResponseReview.objects.exists())
