"""Isolated protocol fixtures; no provider or real project is contacted."""
from copy import deepcopy

from django.core.exceptions import ValidationError
from django.test import SimpleTestCase, TestCase, override_settings
from rest_framework.test import APIClient

from studio.models import ConfigDefinition, ExecutionResourceConfirmation, GenerationAttempt
from studio.services.configuration import create_config_revision, publish_config_revision, validate_config_value
from studio.services.execution_advancement import advance_execution_preparation
from studio.services.execution_resource_policy import RESOURCE_POLICY, REVIEW_REQUIRED, confirm_resource_review, read_resource_policy
from studio.services.text_generation_queue import claim_next_text_attempt
from studio.tests import test_execution_production_dispatch as fixtures


@override_settings(AI_PROVIDER_ALLOWED_HOSTS=("api.example.com",))
class ResourceReviewTests(TestCase):
    setUpTestData = classmethod(fixtures.ProductionDispatchTests.setUpTestData.__func__)
    profile_instance, root, prepare = fixtures.ProductionDispatchTests.profile_instance, fixtures.ProductionDispatchTests.root, fixtures.ProductionDispatchTests.prepare
    structure_instance, pass_smoke, response = fixtures.ProductionDispatchTests.structure_instance, fixtures.ProductionDispatchTests.pass_smoke, fixtures.ProductionDispatchTests.response
    queued, process = fixtures.ProductionDispatchTests.queued, fixtures.ProductionDispatchTests.process

    def setUp(self):
        import tempfile
        self.response_storage = tempfile.TemporaryDirectory()
        self.addCleanup(self.response_storage.cleanup)
        storage = override_settings(MEDIA_ROOT=self.response_storage.name)
        storage.enable()
        self.addCleanup(storage.disable)
        fixtures.ProductionDispatchTests.setUp(self)
        self.policy()

    def policy(self, *, budget="1", calls=100, length=4096):
        value = {"budget_by_currency": {} if budget is None else {"USD": budget}, "call_count": calls, "output_length": length}
        revision = create_config_revision(scope_type="system", values={RESOURCE_POLICY: value},
            created_by=self.actor, change_summary="isolated resource review fixture")
        publish_config_revision(revision, published_by=self.actor)
        return read_resource_policy()

    def advance(self, prep, *, human=True):
        prep.refresh_from_db()
        return advance_execution_preparation(preparation_id=prep.pk, expected_preparation_revision=prep.revision,
            actor=self.actor, human_initiated=human)

    def confirm(self, prep):
        from studio.services.execution_preparations import read_execution_preparation_detail
        review = read_execution_preparation_detail(prep).evaluation["root_checks"]["resource_review"]
        return confirm_resource_review(preparation_id=prep.pk, expected_preparation_revision=prep.revision,
            fingerprint=review["fingerprint"], actor=self.actor)

    def waiting(self, **policy):
        self.pass_smoke()
        self.policy(**policy)
        prep = self.advance(self.prepare())
        self.assertEqual(prep.status, "awaiting_review", prep.reason)
        self.assertEqual(prep.reason_code, REVIEW_REQUIRED)
        self.assertIsNone(prep.next_check_at)
        self.assertFalse(GenerationAttempt.objects.filter(preparation=prep).exists())
        return prep

    def test_budget_overage_is_review_preserves_input_and_confirmation_does_not_dispatch(self):
        prep = self.waiting(budget="0")
        intent = prep.intent_sha256
        revision = prep.revision
        prep = self.confirm(prep)
        approved_basis = deepcopy(prep.contract.evaluation["root_checks"]["resource_review"]["basis"])
        self.assertEqual(prep.intent_sha256, intent)
        self.assertEqual(prep.revision, revision + 1)
        self.assertFalse(GenerationAttempt.objects.filter(preparation=prep).exists())
        self.confirm(prep)
        self.assertEqual(ExecutionResourceConfirmation.objects.filter(preparation=prep).count(), 1)
        self.assertEqual(self.advance(prep, human=False).status, "awaiting_review")
        prep = self.advance(prep)
        self.assertEqual(prep.status, "dispatched", str((approved_basis, prep.contract.evaluation["root_checks"]["resource_review"]["basis"])))
        self.assertEqual(GenerationAttempt.objects.filter(preparation=prep).count(), 1)
        attempt = claim_next_text_attempt("isolated-review")
        from studio.services.execution_production_dispatch import begin_production_execution_submission
        allowed, submitted = begin_production_execution_submission(attempt)
        self.assertTrue(allowed, submitted.error_message)
        self.assertEqual(submitted.status, "submitting")
        self.run.refresh_from_db()
        self.assertEqual(self.run.generation_task_count, 1)

    def test_dynamic_policy_invalidates_old_confirmation_without_automatic_spend(self):
        prep = self.waiting(budget="0")
        prep = self.confirm(prep)
        self.policy(budget="0", calls=99)
        with self.assertRaises(ValidationError):
            self.confirm(prep)
        self.assertEqual(self.advance(prep, human=False).status, "awaiting_review")
        prep = self.advance(prep)
        self.assertEqual(prep.status, "awaiting_review")
        self.assertFalse(GenerationAttempt.objects.filter(preparation=prep).exists())
        self.policy(budget="2")
        self.assertEqual(self.advance(prep, human=False).status, "awaiting_review")
        self.assertEqual(self.advance(prep).status, "dispatched")

    def test_under_limit_sibling_allocation_preserves_cost_confirmation(self):
        prep = self.confirm(self.waiting(budget="0"))
        self.run.generation_task_count = 7
        self.run.save(update_fields=("generation_task_count",))
        prep = self.advance(prep)
        self.assertEqual(prep.status, "dispatched", prep.reason)
        self.assertEqual(ExecutionResourceConfirmation.objects.filter(preparation=prep).count(), 1)

    def test_crossing_call_limit_invalidates_cost_only_confirmation(self):
        prep = self.confirm(self.waiting(budget="0", calls=10))
        self.run.generation_task_count = 10
        self.run.save(update_fields=("generation_task_count",))
        prep = self.advance(prep)
        self.assertEqual(prep.status, "awaiting_review")
        review = prep.contract.evaluation["root_checks"]["resource_review"]
        self.assertFalse(review["confirmed"])
        self.assertIn("call_count_exceeded", [row["code"] for row in review["issues"]])

    def test_count_and_output_overage_are_issues_not_terminal_failure(self):
        self.run.generation_task_count = 2
        self.run.save(update_fields=("generation_task_count",))
        self.pass_smoke()
        prep = self.prepare()
        self.policy(calls=2, length=1000)
        prep = self.advance(prep)
        review = prep.contract.evaluation["root_checks"]["resource_review"]
        self.assertEqual({row["code"] for row in review["issues"]}, {"call_count_exceeded", "output_tokens_exceeded"})
        prep = self.confirm(prep)
        self.assertEqual(self.advance(prep).status, "dispatched")

    def test_system_output_is_frozen_once_without_editing_the_published_instance(self):
        from studio.services.execution_preparations import read_execution_preparation_detail
        from studio.services.execution_parameter_preparation import verify_prepared_execution_parameters
        from studio.models import ConfigRevision
        self.assertFalse(ConfigDefinition.objects.get(key="production.text_contract_retry_limit").is_active)
        active = ConfigRevision.objects.get(scope_type="system", status="published", activation_state="active")
        self.assertFalse(active.values.filter(definition__key="production.text_contract_retry_limit").exists())
        self.assertIn("含字段辅助", ConfigDefinition.objects.get(key=RESOURCE_POLICY).description)
        original = deepcopy(self.definition.value)
        self.policy(length=8192)
        prep = self.prepare()
        facts = read_execution_preparation_detail(prep).resolved_inputs
        self.assertEqual(facts["parameters"]["values"]["max_tokens"], 8192)
        self.policy(length=16384)
        self.assertEqual(verify_prepared_execution_parameters(self.definition, facts["parameters"])["values"]["max_tokens"], 8192)
        self.assertEqual(self.definition.value, original)
        corrupt = deepcopy(facts["parameters"])
        corrupt["resolved_source_contracts"]["max_tokens"]["policy"]["values"]["output_length"] = 123
        with self.assertRaises(ValidationError):
            verify_prepared_execution_parameters(self.definition, corrupt)
        missing = deepcopy(facts["parameters"])
        missing["resolved_source_contracts"].pop("max_tokens")
        with self.assertRaises(ValidationError):
            verify_prepared_execution_parameters(self.definition, missing)

    def test_unknown_currency_threshold_needs_consent_not_an_invented_budget(self):
        prep = self.waiting(budget=None)
        self.assertEqual(prep.contract.evaluation["root_checks"]["resource_review"]["issues"][0]["code"], "budget_not_configured")

    def test_confirmation_cannot_bypass_external_authorization(self):
        prep = self.waiting(budget="0")
        prep = self.confirm(prep)
        self.run.allow_external_calls = False
        self.run.save(update_fields=("allow_external_calls",))
        prep = self.advance(prep)
        self.assertEqual(prep.status, "blocked")
        self.assertEqual(prep.reason_code, "execution_external_authorization_required")
        self.assertFalse(GenerationAttempt.objects.filter(preparation=prep).exists())

    def test_reduced_policy_after_allocation_pauses_original_queue_and_resumes_same_attempt(self):
        self.pass_smoke()
        prep = self.advance(self.prepare())
        attempt = GenerationAttempt.objects.get(preparation=prep)
        frozen = deepcopy(attempt.request_snapshot)
        self.policy(budget="0")
        claim = claim_next_text_attempt("isolated-first-check")
        from studio.services.execution_production_dispatch import begin_production_execution_submission
        allowed, paused = begin_production_execution_submission(claim)
        self.assertFalse(allowed)
        self.assertEqual(paused.status, "queued")
        self.assertIsNone(claim_next_text_attempt("must-not-spin"))
        prep.refresh_from_db()
        prep = self.confirm(prep)
        self.assertIsNone(claim_next_text_attempt("confirmation-does-not-spend"))
        prep = self.advance(prep)
        self.assertEqual(claim_next_text_attempt("explicit-resume").pk, attempt.pk)
        attempt.refresh_from_db()
        self.assertEqual(attempt.request_snapshot, frozen)
        self.assertEqual(GenerationAttempt.objects.filter(preparation=prep).count(), 1)

    def test_api_requires_exact_fingerprint_owner_revision_and_explicit_action(self):
        prep = self.waiting(budget="0")
        client = APIClient()
        url = f"/api/v1/execution-preparations/{prep.pk}/confirm-resources/"
        data = {"expected_preparation_revision": prep.revision, "fingerprint": "a" * 64}
        self.assertIn(client.post(url, data, format="json").status_code, (401, 403))
        client.force_authenticate(self.actor)
        self.assertEqual(client.post(url, data, format="json").status_code, 409)
        data["fingerprint"] = prep.contract.evaluation["root_checks"]["resource_review"]["fingerprint"]
        response = client.post(url, data, format="json")
        self.assertEqual(response.status_code, 200, response.data)
        self.assertTrue(response.data["resource_review"]["confirmed"])
        self.assertFalse(GenerationAttempt.objects.filter(preparation=prep).exists())
        self.assertEqual(client.post(url, data, format="json").status_code, 200)

    def test_schema_rejects_invalid_amounts_counts_and_unknown_keys(self):
        definition = ConfigDefinition.objects.get(key=RESOURCE_POLICY)
        for bad in ({"USD": "NaN"}, {"USD": "-1"}, {"USD": "1.12345"}, {"usd": "1"}):
            with self.subTest(bad=bad), self.assertRaises(ValidationError):
                validate_config_value(definition, {"budget_by_currency": bad, "call_count": 10, "output_length": 4096})
        for count in (0, -1, True, "5"):
            with self.subTest(count=count), self.assertRaises(ValidationError):
                validate_config_value(definition, {"budget_by_currency": {}, "call_count": count, "output_length": 4096})

    def test_truncated_response_is_retained_and_human_completion_does_not_call_provider(self):
        import json
        from studio.models import ExecutionTextResponseReview, GeneratedArtifact
        from studio.services.execution_text_response_review import complete_text_response
        from studio.services.provider_registry import ProviderDriverError
        prep, attempt = self.queued()
        frozen = deepcopy(attempt.request_snapshot)
        error = ProviderDriverError("isolated length", code="provider_response_invalid")
        error.metadata = {"contract_error_code": "response_output_budget_exhausted", "finish_reason": "length",
            "usage": {"completion_tokens": 2048}}
        error.partial_text = '{"episodes": ['
        result, driver = self.process(effect=error)
        driver.invoke.assert_called_once()
        self.assertEqual(result.status, "awaiting_review")
        row = ExecutionTextResponseReview.objects.get(attempt=result)
        self.assertEqual(row.original_text, error.partial_text)
        self.assertFalse(GeneratedArtifact.objects.filter(generation_attempt=result).exists())
        self.assertIsNone(claim_next_text_attempt("no-automatic-retry"))
        prep.refresh_from_db()
        kwargs = {"preparation_id": prep.pk, "expected_preparation_revision": prep.revision,
            "review_id": row.pk, "original_sha256": row.original_sha256, "actor": self.actor}
        with self.assertRaises(ValidationError):
            complete_text_response(**kwargs, text=row.original_text)
        row.refresh_from_db()
        self.assertIsNone(row.confirmed_at)
        corrected = self.response()["text"]
        complete_text_response(**kwargs, text=corrected)
        complete_text_response(**kwargs, text=corrected)
        result.refresh_from_db()
        self.assertEqual(result.status, "succeeded")
        self.assertEqual(result.request_snapshot, frozen)
        artifact = GeneratedArtifact.objects.get(generation_attempt=result)
        self.assertEqual(artifact.status, "available")
        self.assertEqual(artifact.manifest_metadata["human_response_recovery"]["review_id"], str(row.pk))
        row.refresh_from_db()
        self.assertEqual(row.original_text, error.partial_text)
        self.assertEqual(row.corrected_text, corrected)
        self.assertEqual(GenerationAttempt.objects.filter(preparation=prep).count(), 1)
        from studio.services.execution_production_candidates import read_production_text_candidate
        from unittest.mock import patch
        inspected = read_production_text_candidate(artifact, for_audit=True)
        self.assertTrue(inspected["text"])
        self.assertEqual(inspected["human_response_recovery"]["review_id"], str(row.pk))
        with patch.object(type(row), "corrected_text", new=property(lambda _: "tampered")):
            with self.assertRaises(ValidationError):
                read_production_text_candidate(artifact, for_audit=True)

    def test_provider_capacity_rejection_preserves_request_for_human_action_without_retry(self):
        from studio.models import ExecutionTextResponseReview
        from studio.services.provider_registry import ProviderDriverError
        prep, attempt = self.queued()
        frozen = deepcopy(attempt.request_snapshot)
        error = ProviderDriverError("服务商拒绝输出长度", code="provider_text_capacity_rejected")
        error.metadata = {"contract_error_code": "request_text_capacity_rejected"}
        error.partial_text = ""
        result, driver = self.process(effect=error)
        driver.invoke.assert_called_once()
        self.assertEqual(result.status, "awaiting_review")
        self.assertEqual(result.request_snapshot, frozen)
        row = ExecutionTextResponseReview.objects.get(attempt=result)
        self.assertEqual(row.original_text, "")
        self.assertIsNone(claim_next_text_attempt("capacity-must-not-retry"))
        client = APIClient()
        client.force_authenticate(self.actor)
        response = client.get(f"/api/v1/execution-preparations/{prep.pk}/")
        self.assertEqual(response.status_code, 200, response.data)
        self.assertTrue(response.data["text_response_review"]["editable"])
        self.assertEqual(response.data["text_response_review"]["diagnostics"]["contract_error_code"], "request_text_capacity_rejected")

    def test_invalid_json_keeps_original_and_can_be_corrected_without_model_retry(self):
        from studio.models import ExecutionTextResponseReview, GeneratedArtifact
        from studio.services.execution_text_response_review import complete_text_response, verified_diagnostics
        prep, attempt = self.queued()
        result, driver = self.process(response={"text": '{"broken":', "request_id": "invalid-json-fixture",
            "usage": {"completion_tokens": 5}})
        driver.invoke.assert_called_once()
        self.assertEqual(result.status, "awaiting_review")
        review = ExecutionTextResponseReview.objects.get(attempt=result)
        self.assertEqual(review.original_text, '{"broken":')
        self.assertEqual(verified_diagnostics(review)["validation_code"], "response_json_invalid")
        self.assertFalse(GeneratedArtifact.objects.filter(generation_attempt=result).exists())
        self.assertIsNone(claim_next_text_attempt("must-not-retry-invalid-json"))
        prep.refresh_from_db()
        complete_text_response(preparation_id=prep.pk, expected_preparation_revision=prep.revision,
            review_id=review.pk, original_sha256=review.original_sha256, actor=self.actor, text=self.response()["text"])
        result.refresh_from_db()
        self.assertEqual(result.status, "succeeded")
        self.assertEqual(GeneratedArtifact.objects.get(generation_attempt=result).status, "available")
        self.assertEqual(GenerationAttempt.objects.filter(preparation=prep).count(), 1)

    def test_uncertain_submission_cannot_be_reclassified_as_safe_capacity_recovery(self):
        from studio.models import ExecutionTextResponseReview
        from studio.services.provider_registry import ProviderDriverError
        self.queued()
        error = ProviderDriverError("uncertain request", code="provider_timeout", submission_uncertain=True)
        error.metadata = {"contract_error_code": "response_output_budget_exhausted"}
        error.partial_text = "not a verified response"
        result, driver = self.process(effect=error)
        driver.invoke.assert_called_once()
        self.assertEqual(result.error_code, "uncertain_remote_response")
        self.assertFalse(ExecutionTextResponseReview.objects.filter(attempt=result).exists())

    def test_human_response_repair_preserves_actual_bill_and_overage_confirmation(self):
        from decimal import Decimal
        from studio.models import ExecutionTextResponseReview, GeneratedArtifact
        from studio.services.execution_text_response_review import complete_text_response
        prep, _ = self.queued()
        result, driver = self.process(response={"text": "invalid JSON", "actual_cost": "2", "currency": "USD"})
        self.assertEqual(result.status, "awaiting_review")
        review = ExecutionTextResponseReview.objects.get(attempt=result)
        prep.refresh_from_db()
        kwargs = dict(preparation_id=prep.pk, expected_preparation_revision=prep.revision,
            review_id=review.pk, original_sha256=review.original_sha256, actor=self.actor, text=self.response()["text"])
        complete_text_response(**kwargs)
        complete_text_response(**kwargs)
        result.refresh_from_db()
        self.run.refresh_from_db()
        self.assertEqual(result.status, "succeeded")
        self.assertEqual(self.run.actual_cost_total, Decimal("2"))
        self.assertEqual(self.run.status, "awaiting_approval")
        self.assertEqual(self.run.blocked_code, "actual_budget_exceeded")
        self.assertEqual(GeneratedArtifact.objects.get(generation_attempt=result).status, "available")
        self.assertEqual(GenerationAttempt.objects.filter(preparation=prep).count(), 1)
        driver.invoke.assert_called_once()

    def test_actual_bill_above_estimate_preserves_success_and_requests_human_review(self):
        from studio.models import GeneratedArtifact
        prep, attempt = self.queued()
        reply = {**self.response(), "actual_cost": "2", "currency": "USD"}
        result, driver = self.process(response=reply)
        driver.invoke.assert_called_once()
        self.assertEqual(result.status, "succeeded")
        self.assertTrue(GeneratedArtifact.objects.filter(generation_attempt=result, status="available").exists())
        self.run.refresh_from_db()
        self.assertEqual(self.run.status, "awaiting_approval")
        self.assertEqual(self.run.blocked_code, "actual_budget_exceeded")
        from decimal import Decimal
        self.assertEqual(self.run.actual_cost_total, Decimal("2"))
        self.assertNotIn("必须提高预算", self.run.blocked_reason)
        self.assertEqual(self.run.current_stage_key, result.stage_run.stage_key)

    def test_manual_actual_bill_confirmation_keeps_candidate_and_does_not_restart(self):
        from studio.models import GeneratedArtifact, ProductionRun
        from studio.services.production_runs import authorize_stage_cost
        from unittest.mock import patch
        prep, attempt = self.queued()
        result, driver = self.process(response={**self.response(), "actual_cost": "2", "currency": "USD"})
        # Only the acknowledgement branch needs manual ownership here; the
        # immutable request and candidate retain their original fixture facts.
        ProductionRun.objects.filter(pk=self.run.pk).update(plan_source="manual", recipe_version=None)
        self.run.refresh_from_db()
        artifact = GeneratedArtifact.objects.get(generation_attempt=result)
        with patch("studio.services.production_runs.advance_production_run") as advance:
            run = authorize_stage_cost(self.run, stage_key=result.stage_run.stage_key,
                allow_external_calls=None, approved_estimated_cost=None,
                currency="USD", comment="已核对费用", user=self.actor)
        advance.assert_not_called()
        driver.invoke.assert_called_once()
        self.assertEqual(run.status, "awaiting_input")
        self.assertEqual(run.blocked_code, "")
        self.assertTrue(run.allow_external_calls)
        self.assertEqual(GenerationAttempt.objects.filter(preparation=prep).count(), 1)
        artifact.refresh_from_db()
        self.assertEqual(artifact.status, "available")
        self.assertIn("recorded_actual_cost=2", result.stage_run.approval_decisions.get().comment)


class ProviderCapacityClassificationTests(SimpleTestCase):
    def test_shared_json_decoder_errors_keep_safe_response_diagnostic_categories(self):
        from unittest.mock import patch
        from studio.services.json_contracts import parse_json_object_text
        from studio.services.execution_text_response_review import response_validation_diagnostic
        self.enterContext(patch("studio.services.provider_registry.boundary_int", return_value=100000))
        for text in ('{"broken":', '{"a":1,"a":2}', '{"a":NaN}'):
            with self.subTest(text=text), self.assertRaises(ValidationError) as raised:
                parse_json_object_text(text, max_bytes=1024)
            diagnostic = response_validation_diagnostic(raised.exception, {"text": text})
            self.assertEqual(diagnostic["validation_code"], "response_json_invalid")
            self.assertNotIn(text, diagnostic["summary"])
        with self.assertRaises(ValidationError) as raised:
            parse_json_object_text("[]", max_bytes=1024)
        self.assertEqual(response_validation_diagnostic(raised.exception, {"text": "[]"})["validation_code"], "response_object_required")

    def test_retired_visual_protocol_is_readonly_not_silently_converted(self):
        from studio.services.execution_text_response_review import _visual_protocol_readonly_reason
        self.assertIn("旧影视规则协议", _visual_protocol_readonly_reason({
            "operation": "visual_rules.generate", "output": {"adapter": "text.visual_rules/v1"}}))
        self.assertEqual(_visual_protocol_readonly_reason({
            "operation": "visual_rules.generate", "output": {"adapter": "text.visual_rules/v2"}}), "")
        self.assertEqual(_visual_protocol_readonly_reason({"operation": "script.generate"}), "")

    def test_http_boundary_only_classifies_explicit_client_capacity_errors(self):
        from types import SimpleNamespace
        from unittest.mock import Mock, patch
        from studio.services.provider_registry import _request_json, ProviderDriverError
        boundary = "studio.services.provider_registry."
        resolved = SimpleNamespace(scheme="https", peer_addresses=("192.0.2.1",))
        for status, enabled, error_body, expected in (
            (400, True, {"code": "context_length_exceeded", "message": "private input"}, "provider_text_capacity_rejected"),
            (422, True, {"code": "invalid_request_error", "param": "max_tokens"}, "provider_text_capacity_rejected"),
            (400, False, {"code": "context_length_exceeded"}, "provider_request_rejected"),
            (500, True, {"code": "context_length_exceeded"}, "provider_request_temporary_failure"),
            (400, True, {"code": "content_filter", "message": "maximum tokens"}, "provider_request_rejected"),
        ):
            with self.subTest(status=status, enabled=enabled, error=error_body):
                response, session = Mock(status_code=status), Mock()
                session.request.return_value = response
                with patch(boundary + "_resolved_provider_endpoint", return_value=resolved), \
                        patch(boundary + "_PinnedAddressAdapter"), patch(boundary + "requests.Session", return_value=session), \
                        patch(boundary + "_peer_address", return_value="192.0.2.1"), \
                        patch(boundary + "_response_json", return_value={"error": error_body}):
                    with self.assertRaises(ProviderDriverError) as caught:
                        _request_json("POST", "https://api.example.com/chat", timeout=1, submission=True, inspect_text_capacity=enabled)
                self.assertEqual(caught.exception.code, expected)
                self.assertNotIn("private input", str(caught.exception.metadata))
                self.assertEqual(hasattr(caught.exception, "partial_text"), expected == "provider_text_capacity_rejected")
                session.request.assert_called_once()
                session.close.assert_called_once()

    def test_only_explicit_capacity_codes_or_output_parameters_are_recoverable(self):
        from studio.services.provider_registry import _text_capacity_rejection
        for error in ({"code": "context_length_exceeded"}, {"code": "max_tokens_exceeded"},
                {"type": "invalid_request_error", "param": "max_tokens"}):
            self.assertTrue(_text_capacity_rejection(error))
        for error in (None, "length", {"code": "unauthorized"}, {"code": "content_filter"},
                {"message": "maximum tokens exceeded"}, {"code": "invalid_request_error", "param": "model"}):
            self.assertFalse(_text_capacity_rejection(error))
