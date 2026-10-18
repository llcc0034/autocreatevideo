"""Original production queue with isolated provider protocol responses only."""
from copy import deepcopy
from decimal import Decimal
import json
from unittest.mock import Mock, patch

from django.test import TestCase, override_settings
from django.utils import timezone

from studio.models import AssetGenerationJob, GeneratedArtifact, GenerationAttempt, ScriptRevision
from studio.services.deployment_smoke_queue import claim_next_deployment_smoke, process_deployment_smoke
from studio.services.execution_advancement import advance_execution_preparation
from studio.services.execution_production_dispatch import dispatch_production_execution
from studio.services.execution_production_candidates import read_production_text_candidate
from studio.services.execution_smoke import create_instance_smoke
from studio.services.provider_registry import ProviderDriverError
from studio.services.text_generation_queue import claim_next_text_attempt, process_text_attempt, recover_stale_text_attempts
from studio.tests import test_production_recipe_preparations as fixtures


@override_settings(AI_PROVIDER_ALLOWED_HOSTS=("api.example.com",))
class ProductionDispatchTests(TestCase):
    def test_text_zero_bill_keeps_run_currency_and_positive_bill_sets_unset_currency(self):
        from studio.models import ProductionRun
        from studio.services.execution_production_text_worker import finish_production_text
        self.queued()
        claim = claim_next_text_attempt("budget-ledger-check")
        GenerationAttempt.objects.filter(pk=claim.pk).update(estimated_cost=0, committed_cost=0)
        ProductionRun.objects.filter(pk=self.run.pk).update(cost_currency="CNY", committed_cost_total=0)
        result = finish_production_text(claim, status="failed", code="isolated_response_invalid",
            response={"actual_cost": "0", "currency": "USD"})
        self.run.refresh_from_db()
        self.assertEqual(self.run.cost_currency, "CNY")
        self.assertEqual(self.run.actual_cost_total, Decimal("0"))
        # A separate isolated settlement starts without a declared budget currency.
        GenerationAttempt.objects.filter(pk=claim.pk).update(status="running", claim_token=claim.claim_token, actual_cost=None)
        ProductionRun.objects.filter(pk=self.run.pk).update(cost_currency="")
        result = finish_production_text(claim, status="failed", code="isolated_response_invalid",
            response={"actual_cost": "0.01", "currency": "USD"})
        self.run.refresh_from_db()
        self.assertEqual(self.run.cost_currency, "USD")
        self.assertEqual(self.run.actual_cost_total, Decimal("0.01"))

    setUpTestData = classmethod(fixtures.RecipePreparationTests.setUpTestData.__func__)
    profile_instance = fixtures.RecipePreparationTests.profile_instance
    root = fixtures.RecipePreparationTests.root
    prepare = fixtures.RecipePreparationTests.prepare
    structure_instance = fixtures.RecipePreparationTests.structure_instance

    def setUp(self):
        fixtures.RecipePreparationTests.setUp(self)
        self.run = self.root()
        self.run.allow_external_calls = True
        self.run.save(update_fields=("allow_external_calls",))

    def pass_smoke(self):
        request = deepcopy(self.fixture)
        request["manual_channels"] = {}
        request["source_language"] = request["target_language"] = "English"
        request.pop("allow_external_calls", None)
        request.pop("budget_limit", None)
        _, preparation, _ = create_instance_smoke(version_id=self.version.pk, expected_definition_sha256=self.definition.sha256,
            fixture=request, field_values=[], client_idempotency_key=f"production-protocol-fixture:{self.version.pk}", allow_external_calls=True,
            budget_limit=None, actor=self.actor)
        advanced = self.advance(preparation)
        self.assertEqual(advanced.status, "dispatched", advanced.reason)
        self.operation_plan = advanced.contract.prepared_request["transport"]["operation_plan"]["plan"]
        peer = Mock()
        peer.invoke.return_value = self.response()
        with patch("studio.services.execution_smoke_worker.driver_for", return_value=peer):
            outcome = process_deployment_smoke(claim_next_deployment_smoke("isolated-smoke"))
        self.assertEqual(outcome.status, "succeeded", outcome.error_code)

    def response(self):
        candidate = deepcopy(self.candidate)
        if self.version.operation == "visual_rules.generate":
            from types import SimpleNamespace
            from studio.tests import test_production_visual_rules as visual_fixtures
            candidate = visual_fixtures.ProductionVisualRulesContractTests._payload(SimpleNamespace(plan=self.operation_plan))
        return {"text": json.dumps(candidate, ensure_ascii=False), "request_id": "isolated-production-request"}

    def advance(self, preparation):
        preparation.refresh_from_db()
        return advance_execution_preparation(preparation_id=preparation.pk, expected_preparation_revision=preparation.revision, actor=self.actor)

    def queued(self):
        self.pass_smoke()
        preparation = self.advance(self.prepare())
        self.assertEqual(preparation.status, "dispatched", preparation.reason)
        return preparation, GenerationAttempt.objects.get(preparation=preparation)

    def process(self, response=None, effect=None):
        attempt = claim_next_text_attempt("production-original-worker")
        self.assertIsNotNone(attempt)
        self.operation_plan = attempt.request_snapshot["transport"]["operation_plan"]["plan"]
        peer = Mock()
        peer.invoke.return_value = self.response() if response is None else response
        peer.invoke.side_effect = effect
        with patch("studio.services.execution_production_text_worker.driver_for", return_value=peer):
            result = process_text_attempt(attempt)
        return result, peer

    def test_original_queue_has_one_exact_request_and_reservation_for_replay(self):
        prep, attempt = self.queued()
        frozen = deepcopy(prep.contract.prepared_request)
        replay = dispatch_production_execution(preparation_id=prep.pk, expected_preparation_revision=1, actor=self.actor)
        self.assertEqual(replay.pk, attempt.pk)
        self.assertEqual(attempt.request_snapshot, frozen)
        self.assertEqual(attempt.request_sha256, prep.request_sha256)
        self.assertEqual(attempt.prompt_profile_version_id, self.version.prompt_profile_version_id)
        self.assertIsNone(attempt.submission_started_at)
        self.assertIsNone(attempt.actual_cost)
        self.run.refresh_from_db()
        self.assertEqual(self.run.committed_cost_total, Decimal("0.1"))
        self.assertEqual(GenerationAttempt.objects.count(), 1)
        self.assertFalse(AssetGenerationJob.objects.exists())
        from django.db import connection
        from studio.services.text_generation_queue import queued_text_attempts
        if connection.vendor == "sqlite":
            explanation = queued_text_attempts().order_by("created_at", "pk").only("id", "stage_run_id", "preparation_id").explain()
            self.assertRegex(explanation, r"SEARCH studio_generationattempt USING INDEX")

    def set_system_budget(self, amount):
        from studio.services.configuration import create_config_revision, publish_config_revision
        revision = create_config_revision(scope_type="system", values={"execution.resources.review_policy": {
            "budget_by_currency": {"USD": amount}, "call_count": 100, "output_length": 4096}},
            created_by=self.actor, change_summary="isolated system budget change")
        publish_config_revision(revision, published_by=self.actor)

    def test_missing_smoke_or_system_budget_review_does_not_allocate_an_attempt(self):
        prep = self.advance(self.prepare())
        self.assertEqual(prep.reason_code, "execution_smoke_missing")
        self.assertFalse(GenerationAttempt.objects.exists())
        self.pass_smoke()
        self.set_system_budget("0.01")
        prep = self.advance(prep)
        self.assertEqual(prep.reason_code, "execution_resource_confirmation_required")
        self.assertFalse(GenerationAttempt.objects.exists())

    def test_submission_rechecks_budget_and_due_time_without_rebuilding_request(self):
        prep, attempt = self.queued()
        original = deepcopy(attempt.request_snapshot)
        self.set_system_budget("0.01")
        blocked, peer = self.process()
        self.assertEqual((blocked.status, blocked.error_code), ("queued", "execution_resource_confirmation_required"))
        peer.invoke.assert_not_called()
        self.assertIsNone(blocked.submission_started_at)
        self.assertIsNone(blocked.next_poll_at)
        self.assertIsNone(claim_next_text_attempt("no-busy-loop"))
        self.assertEqual(blocked.request_snapshot, original)
        self.run.refresh_from_db()
        self.assertEqual(self.run.committed_cost_total, Decimal("0.1"))

    def test_success_is_review_candidate_and_unknown_billing_is_not_estimated_actual(self):
        prep, attempt = self.queued()
        result, peer = self.process()
        self.assertEqual(result.status, "succeeded", result.error_code)
        peer.invoke.assert_called_once()
        self.assertEqual(peer.invoke.call_args.args[2], prep.contract.prepared_request["transport"]["payload"])
        artifact = GeneratedArtifact.objects.get(generation_attempt=result)
        revision = ScriptRevision.objects.get(pk=artifact.manifest_metadata["script_revision_id"])
        self.assertEqual((artifact.status, revision.status), ("available", "preview"))
        self.run.refresh_from_db()
        self.assertIsNone(self.run.script_revision_id)
        self.assertEqual((self.run.actual_cost_total, self.run.committed_cost_total), (Decimal("0"), Decimal("0.1")))
        self.assertIsNone(result.actual_cost)
        process_text_attempt(result)
        self.assertEqual(GeneratedArtifact.objects.count(), 1)
        self.stage.refresh_from_db()
        self.assertEqual(self.stage.status, "running")

    def test_success_retains_exact_primary_body_readonly_without_adoption_or_replay(self):
        from hashlib import sha256
        from django.db import connection
        from django.test.utils import CaptureQueriesContext
        from rest_framework.test import APIClient
        from studio.models import ExecutionTextResponseReview
        prep, _ = self.queued()
        raw = "\n" + json.dumps(json.loads(self.response()["text"]), ensure_ascii=False, indent=2) + "\n"
        result, peer = self.process({"text": raw})
        self.assertEqual(result.status, "succeeded", result.error_message)
        review = ExecutionTextResponseReview.objects.get(attempt=result)
        self.assertEqual(review.original_text, raw)
        self.assertEqual(review.original_sha256, sha256(raw.encode()).hexdigest())
        self.assertEqual(review.diagnostics["contract_error_code"], "response_candidate_recorded")
        self.assertIsNone(review.confirmed_at)
        self.assertIsNone(review.confirmed_by_id)
        self.assertIsNone(review.corrected_file)
        artifact = GeneratedArtifact.objects.get(generation_attempt=result)
        self.assertEqual(ScriptRevision.objects.get(origin_generation_attempt=result).content, raw)
        client = APIClient()
        client.force_authenticate(self.actor)
        url = f"/api/v1/execution-preparations/{prep.pk}/"
        with CaptureQueriesContext(connection) as queries:
            response = client.get(url)
        self.assertEqual(response.status_code, 200, response.data)
        detail = response.data["text_response_review"]
        self.assertEqual(detail["original_text"], raw)
        self.assertFalse(detail["editable"])
        self.assertIn("只读保留", detail["readonly_reason"])
        self.assertEqual(detail["notices"], [{"code": "candidate_advisory", "message": item}
            for item in review.diagnostics["warnings"]])
        self.assertFalse(any(row["sql"].lstrip().upper().startswith(("INSERT ", "UPDATE ", "DELETE ")) for row in queries))
        denied = client.post(url + "complete-text-response/", {
            "expected_preparation_revision": response.data["revision"], "review_id": str(review.pk),
            "original_sha256": review.original_sha256, "text": raw}, format="json")
        self.assertEqual(denied.status_code, 409)
        from django.contrib.auth import get_user_model
        client.force_authenticate(get_user_model().objects.create_user(username="other-primary-response-reader"))
        self.assertEqual(client.get(url).status_code, 404)
        process_text_attempt(result)
        self.assertEqual(ExecutionTextResponseReview.objects.filter(attempt=result).count(), 1)
        self.assertEqual(GeneratedArtifact.objects.filter(generation_attempt=result).count(), 1)
        artifact.refresh_from_db()
        self.assertEqual(artifact.status, "available")
        peer.invoke.assert_called_once()

    def test_primary_success_with_native_string_advisories_remains_readable(self):
        from rest_framework.test import APIClient
        prep, _ = self.queued()
        body = json.loads(self.response()["text"])
        body["未映射创作备注"] = {"enabled": False, "take": 0}
        result, peer = self.process({"text": json.dumps(body, ensure_ascii=False)})
        self.assertEqual(result.status, "succeeded", result.error_message)
        client = APIClient()
        client.force_authenticate(self.actor)
        response = client.get(f"/api/v1/execution-preparations/{prep.pk}/")
        self.assertEqual(response.status_code, 200, response.data)
        detail = response.data["text_response_review"]
        self.assertTrue(detail["notices"])
        self.assertTrue(all(row["code"] == "candidate_advisory" and isinstance(row["message"], str)
            for row in detail["notices"]))
        self.assertTrue(any("未映射创作备注" in row["message"] for row in detail["notices"]))
        candidate = read_production_text_candidate(GeneratedArtifact.objects.get(generation_attempt=result))
        self.assertEqual(candidate["candidate"]["未映射创作备注"], body["未映射创作备注"])
        self.assertFalse(detail["editable"])
        peer.invoke.assert_called_once()

    def test_retained_primary_reader_rejects_wrong_request_or_unreadable_original(self):
        from io import BytesIO
        from django.core.exceptions import ValidationError
        from django.core.files.storage import default_storage
        from studio.models import ExecutionTextResponseReview
        from studio.services.execution_text_response_review import read_retained_text_response
        self.queued()
        result, _ = self.process()
        review = ExecutionTextResponseReview.objects.select_related("attempt").get(attempt=result)
        request_sha = review.attempt.request_sha256
        review.attempt.request_sha256 = "0" * 64
        with self.assertRaisesMessage(ValidationError, "原任务与冻结生产请求不一致"):
            read_retained_text_response(review)
        review.attempt.request_sha256 = request_sha
        stored_open = default_storage.open
        for failed in (False, True):
            def read_file(name, mode, *args, **kwargs):
                if name == review.original_file:
                    if failed:
                        raise OSError("unavailable")
                    return BytesIO(b"changed response")
                return stored_open(name, mode, *args, **kwargs)
            with self.subTest(unreadable=failed), patch.object(default_storage, "open", side_effect=read_file):
                with self.assertRaisesMessage(ValidationError, "暂时无法读取" if failed else "文件与摘要不一致"):
                    read_retained_text_response(review)
        self.assertIsNone(review.confirmed_at)

    def test_response_storage_failure_records_actual_call_not_content_failure_or_retry(self):
        from studio.models import ExecutionTextResponseReview
        self.queued()
        with patch("studio.services.execution_response_storage.default_storage.save", side_effect=OSError("disk unavailable")):
            result, peer = self.process({**self.response(), "actual_cost": "0.03", "currency": "USD"})
        self.assertEqual((result.status, result.error_code), ("failed", "execution_response_storage_failed"))
        self.assertIn("模型已经返回", result.error_message)
        self.assertEqual(result.actual_cost, Decimal("0.03"))
        self.assertEqual(result.resolutions.filter(outcome="response_received").count(), 1)
        self.assertFalse(ExecutionTextResponseReview.objects.filter(attempt=result).exists())
        self.assertFalse(GeneratedArtifact.objects.filter(generation_attempt=result).exists())
        self.assertFalse(ScriptRevision.objects.filter(origin_generation_attempt=result).exists())
        self.assertIsNone(claim_next_text_attempt("no-storage-retry"))
        process_text_attempt(result)
        peer.invoke.assert_called_once()

    def test_response_readback_failure_never_marks_body_retained_or_candidate_created(self):
        from io import BytesIO
        from django.core.files.storage import default_storage
        from studio.models import ExecutionTextResponseReview
        self.queued()
        stored_open = default_storage.open
        def read_file(name, mode, *args, **kwargs):
            if name.startswith("execution-responses/"):
                return BytesIO(b"wrong bytes")
            return stored_open(name, mode, *args, **kwargs)
        with patch.object(default_storage, "open", side_effect=read_file):
            result, peer = self.process()
        self.assertEqual(result.error_code, "execution_response_storage_failed")
        self.assertFalse(ExecutionTextResponseReview.objects.filter(attempt=result).exists())
        self.assertFalse(GeneratedArtifact.objects.filter(generation_attempt=result).exists())
        peer.invoke.assert_called_once()

    def test_script_candidate_uses_original_request_and_does_not_adopt(self):
        from django.db import connection
        from django.test.utils import CaptureQueriesContext
        self.queued()
        result, _ = self.process()
        revision = ScriptRevision.objects.get(origin_generation_attempt=result)
        artifact = GeneratedArtifact.objects.get(generation_attempt=result)
        before = (GenerationAttempt.objects.count(), GeneratedArtifact.objects.count())
        with CaptureQueriesContext(connection) as queries, patch(
                "studio.services.script_revision_structures.parse_script_semantic_output", side_effect=AssertionError("no reparse")):
            document = read_production_text_candidate(artifact)
        self.assertEqual(document["projection"]["value"]["entities"][0]["key"], "person-1")
        self.assertEqual(document["revision"]["id"], str(revision.pk))
        self.assertEqual(document["preparation_id"], str(result.preparation_id))
        self.assertEqual(document["output_contract"], self.operation_plan["business_output"])
        self.assertEqual(document["candidate"], self.candidate)
        self.assertEqual(artifact.manifest_metadata["request_sha256"], result.request_sha256)
        self.assertFalse(any(row["sql"].lstrip().upper().startswith(("INSERT ", "UPDATE ", "DELETE ")) for row in queries))
        self.assertEqual(before, (GenerationAttempt.objects.count(), GeneratedArtifact.objects.count()))
        revision.refresh_from_db()
        self.assertEqual(revision.status, "preview")
        self.assertFalse(GeneratedArtifact.objects.filter(status="selected").exists())

    def test_twenty_four_shot_response_saves_candidate_above_configuration_size(self):
        from studio.models import ScriptRevisionStructure, ExecutionTextResponseReview
        self.queued()
        candidate = deepcopy(self.candidate)
        candidate["episodes"] = [candidate["episodes"][0]]
        shot = candidate["episodes"][0]["shots"][0]
        candidate["episodes"][0]["shots"] = [
            {**deepcopy(shot), "shot": index, "action": "雨夜车厢中人物缓慢转身。" * 20}
            for index in range(1, 25)]
        text = json.dumps(candidate, ensure_ascii=False)
        result, peer = self.process({"text": text, "request_id": "isolated-24-shot-response"})
        self.assertEqual(result.status, "succeeded", result.error_message)
        peer.invoke.assert_called_once()
        revision = ScriptRevision.objects.get(origin_generation_attempt=result)
        self.assertGreater(ScriptRevisionStructure.objects.get(revision=revision).document_size_bytes, 65536)
        artifact = GeneratedArtifact.objects.get(generation_attempt=result)
        detail = read_production_text_candidate(artifact)
        self.assertEqual(detail["candidate"], candidate)
        self.assertEqual(ExecutionTextResponseReview.objects.get(attempt=result).original_text, text)
        self.assertEqual(revision.status, "preview")
        self.assertNotEqual(artifact.status, "selected")

    def assert_native_explicit_relationships(self):
        self.queued()
        response = json.loads(self.response()["text"])
        # A quoted/display name is not the character's stable identity. This
        # speaker is explicitly offscreen and no media has been adopted.
        response["entities"][0]["name"] = "La voyageuse"
        shot = response["episodes"][0]["shots"][0]
        shot["dialogue"].update(speaker_entity_key="person-1", text="我在门外。",
            is_voiceover=True, start_ms=200, end_ms=1600)
        shot["references"] = []
        result, peer = self.process({"text": json.dumps(response, ensure_ascii=False)})
        self.assertEqual(result.status, "succeeded", result.error_code)
        peer.invoke.assert_called_once()
        revision = ScriptRevision.objects.get(origin_generation_attempt=result)
        document = read_production_text_candidate(GeneratedArtifact.objects.get(generation_attempt=result))
        projected = document["projection"]["value"]["episodes"][0]["shots"][0]
        self.assertEqual(document["candidate"], response)
        self.assertEqual(projected["dialogue"], shot["dialogue"])
        self.assertEqual(projected["references"], [])
        self.assertNotIn("character_keys", projected)
        self.assertIn("La voyageuse", revision.content)
        self.assertEqual(revision.status, "preview")
        self.assertFalse(self.project.characters.exists())
        self.assertFalse(self.project.episodes.exists())
        self.assertFalse(GeneratedArtifact.objects.filter(status="selected").exists())
        return revision


    def test_generation_keeps_explicit_offscreen_binding_through_original_queue_and_source_reader(self):
        self.assert_native_explicit_relationships()

    def test_structure_keeps_explicit_offscreen_binding_through_original_queue_and_source_reader(self):
        self.structure_instance()
        self.profile_instance()
        self.run = self.root(stage_key="script_structure")
        self.run.allow_external_calls = True
        self.run.save(update_fields=("allow_external_calls",))
        self.assert_native_explicit_relationships()

    def test_unknown_speaker_binding_retains_actual_response_for_review_without_creating_script(self):
        from studio.models import ExecutionTextResponseReview
        self.queued()
        response = json.loads(self.response()["text"])
        response["episodes"][0]["shots"][0]["dialogue"]["speaker_entity_key"] = "unknown-person"
        raw = json.dumps(response, ensure_ascii=False)
        result, peer = self.process({"text": raw})
        self.assertEqual((result.status, result.error_code), ("awaiting_review", "execution_output_review_required"))
        peer.invoke.assert_called_once()
        self.assertEqual(ExecutionTextResponseReview.objects.get(attempt=result).original_text, raw)
        self.assertFalse(GeneratedArtifact.objects.exists())
        self.assertFalse(ScriptRevision.objects.exists())

    def test_script_candidate_rejects_wrong_project_revision_body_and_request_file(self):
        from django.core.exceptions import ValidationError
        from uuid import uuid4
        from studio.services.script_revision_structures import read_script_revision_structure
        self.queued()
        result, _ = self.process()
        revision = ScriptRevision.objects.get(origin_generation_attempt=result)
        artifact = GeneratedArtifact.objects.get(generation_attempt=result)
        with self.assertRaises(ValidationError):
            read_script_revision_structure(revision_id=revision.pk, project_id=uuid4(), max_bytes=10**7)
        with self.assertRaises(ValidationError):
            read_script_revision_structure(revision_id=uuid4(), project_id=self.project.pk, max_bytes=10**7)
        ScriptRevision.objects.filter(pk=revision.pk).update(content=revision.content + "tampered")
        with self.assertRaises(ValidationError):
            read_production_text_candidate(artifact)
        ScriptRevision.objects.filter(pk=revision.pk).update(content=revision.content)
        result.request_snapshot_key = f"execution-requests/{result.preparation_id}/{uuid4()}.json"
        result.save(update_fields=("request_snapshot_key",))
        with self.assertRaises(ValidationError):
            read_production_text_candidate(artifact)
        self.assertFalse(GeneratedArtifact.objects.filter(status="selected").exists())

    def test_distinct_candidates_keep_independent_artifacts_and_provider_actual_cost(self):
        self.queued()
        self.advance(self.prepare(candidate_number=2))
        response = {**self.response(), "actual_cost": "0.03", "currency": "USD"}
        first, _ = self.process(response)
        second, _ = self.process(response)
        self.assertEqual((first.status, second.status), ("succeeded", "succeeded"))
        self.assertEqual(GeneratedArtifact.objects.count(), 2)
        self.assertEqual(set(GeneratedArtifact.objects.values_list("variant_index", flat=True)), {1, 2})
        self.assertEqual(GeneratedArtifact.objects.values("variant_group").distinct().count(), 1)
        self.run.refresh_from_db()
        self.assertEqual((self.run.actual_cost_total, self.run.committed_cost_total), (Decimal("0.06"), Decimal("0")))

    def assert_result_reference_evidence(self):
        """Inject self-consistent stored-result drift, never change live facts."""
        from django.core.exceptions import ValidationError
        from studio.services.json_contracts import json_contract
        from uuid import uuid4
        self.queued()
        result, _ = self.process()
        self.assertEqual(result.status, "succeeded", result.error_code)
        artifact = GeneratedArtifact.objects.get(generation_attempt=result)
        revision_id = artifact.manifest_metadata["script_revision_id"]
        original = deepcopy(artifact.manifest_metadata)
        def read():
            return read_production_text_candidate(GeneratedArtifact.objects.get(pk=artifact.pk))
        self.assertEqual(read()["revision"]["id"], revision_id)
        mutations = [lambda body: body.update(request_sha256="0" * 64),
            lambda body: body["result"].update(script_structure_sha256="0" * 64),
            lambda body: body["result"].pop("script_structure_sha256"),
            lambda body: body["result"].update(script_revision_id=str(uuid4())),
            lambda body: body.update(script_source_id=str(uuid4()))]
        for index, mutate in enumerate(mutations):
            metadata = deepcopy(original)
            mutate(metadata)
            result_contract = json_contract(metadata["result"], max_bytes=10**7)
            metadata["result_sha256"] = result_contract.sha256
            manifest = json_contract(metadata, max_bytes=10**7)
            # Fault injection is confined to the isolated test database. Both
            # outer hashes remain valid, so only cross-evidence checks reject it.
            GenerationAttempt.objects.filter(pk=result.pk).update(result_sha256=result_contract.sha256)
            GeneratedArtifact.objects.filter(pk=artifact.pk).update(manifest_metadata=manifest.value,
                manifest_metadata_sha256=manifest.sha256, manifest_metadata_size_bytes=manifest.size_bytes)
            with self.subTest(operation=result.operation, mutation=index), self.assertRaises(ValidationError):
                read()
        self.assertEqual(ScriptRevision.objects.get(pk=revision_id).status, "preview")
        self.assertFalse(GeneratedArtifact.objects.filter(status="selected").exists())

    def test_generation_candidate_reader_checks_source_not_just_outer_hashes(self):
        self.assert_result_reference_evidence()

    def test_structure_candidate_reader_checks_source_not_just_outer_hashes(self):
        self.structure_instance()
        self.profile_instance()
        self.run = self.root(stage_key="script_structure")
        self.run.allow_external_calls = True
        self.run.save(update_fields=("allow_external_calls",))
        self.assert_result_reference_evidence()

    def test_completed_text_wakes_original_recipe_after_candidate_commit_only_once(self):
        self.queued()
        with patch("studio.services.production_runs.advance_production_run") as advance:
            with self.captureOnCommitCallbacks(execute=True) as callbacks:
                result, peer = self.process()
                self.assertEqual(result.status, "succeeded", result.error_code)
                self.assertTrue(GeneratedArtifact.objects.filter(generation_attempt=result).exists())
                advance.assert_not_called()
            self.assertEqual(len(callbacks), 1)
            advance.assert_called_once()
            self.assertEqual(advance.call_args.args[0].pk, self.run.pk)
            self.assertTrue(advance.call_args.kwargs["automatic"])
            with self.captureOnCommitCallbacks(execute=True) as repeated:
                process_text_attempt(result)
            self.assertEqual(repeated, [])
        peer.invoke.assert_called_once()

    def test_invalid_response_never_creates_partial_artifact_or_adopts_a_script(self):
        self.queued()
        result, _ = self.process({"text": "not a valid structured result"})
        self.assertEqual((result.status, result.error_code), ("awaiting_review", "execution_output_review_required"))
        self.assertFalse(GeneratedArtifact.objects.exists())
        self.assertFalse(ScriptRevision.objects.exists())
        self.assertIsNone(result.actual_cost)

    def assert_missing_text_failure(self, response):
        from studio.models import ExecutionTextResponseReview
        self.queued()
        result, driver = self.process(response)
        self.assertEqual((result.status, result.error_code), ("failed", "execution_response_text_missing"))
        self.assertFalse(ExecutionTextResponseReview.objects.filter(attempt=result).exists())
        driver.invoke.assert_called_once()
        self.assertFalse(GeneratedArtifact.objects.filter(generation_attempt=result).exists())

    def test_missing_response_text_is_not_uncertain_submission(self):
        self.assert_missing_text_failure({"request_id": "no-text"})

    def test_null_response_text_is_not_uncertain_submission(self):
        self.assert_missing_text_failure({"text": None})

    def test_non_string_response_text_is_not_uncertain_submission(self):
        self.assert_missing_text_failure({"text": {"content": "not text"}})

    def test_uncertain_submission_is_never_requeued_or_reinvoked(self):
        prep, _ = self.queued()
        with patch("studio.services.production_runs.advance_production_run") as advance:
            with self.captureOnCommitCallbacks(execute=True):
                result, peer = self.process(effect=ProviderDriverError("isolated lost response", submission_uncertain=True))
            advance.assert_not_called()
        self.assertEqual((result.status, result.error_code), ("failed", "uncertain_remote_response"))
        self.run.refresh_from_db()
        self.assertEqual(self.run.status, "blocked")
        self.assertIsNotNone(result.submission_started_at)
        self.assertIsNone(claim_next_text_attempt("no-repeat"))
        self.assertEqual(dispatch_production_execution(preparation_id=prep.pk, expected_preparation_revision=1, actor=self.actor).pk, result.pk)
        self.assertEqual(GenerationAttempt.objects.count(), 1)
        peer.invoke.assert_called_once()

    def test_cancel_during_call_discards_output_without_claiming_unknown_charge_is_actual(self):
        from studio.models import ExecutionTextResponseReview
        self.queued()
        def cancel(*_):
            type(self.run).objects.filter(pk=self.run.pk).update(status="cancelled")
            return {**self.response(), "usage": {"prompt_tokens": 8, "completion_tokens": 2, "total_tokens": 10}}
        result, _ = self.process(effect=cancel)
        self.assertEqual(result.status, "cancelled")
        self.assertFalse(GeneratedArtifact.objects.exists())
        self.assertFalse(ExecutionTextResponseReview.objects.filter(attempt=result).exists())
        self.assertIsNone(result.actual_cost)
        self.assertEqual(result.committed_cost, Decimal("0.1"))
        self.assertEqual(result.resolutions.get(outcome="response_received").evidence["usage"]["total_tokens"], 10)

    def test_crash_after_submit_boundary_marks_uncertain_not_retryable(self):
        from datetime import timedelta
        _, attempt = self.queued()
        GenerationAttempt.objects.filter(pk=attempt.pk).update(status="submitting", submission_started_at=timezone.now(),
            heartbeat_at=timezone.now() - timedelta(minutes=10))
        result = recover_stale_text_attempts(5)
        self.assertEqual(result, {"requeued": 0, "uncertain": 1})
        self.assertIsNone(claim_next_text_attempt("no-crash-repeat"))

    def test_structure_creates_preview_from_frozen_literal_without_inventing_source_revision(self):
        from studio.models import ExecutionTextResponseReview
        self.structure_instance()
        self.profile_instance()
        self.run = self.root(stage_key="script_structure")
        self.run.allow_external_calls = True
        self.run.save(update_fields=("allow_external_calls",))
        self.queued()
        result, peer = self.process()
        self.assertEqual(result.status, "succeeded", result.error_code)
        artifact = GeneratedArtifact.objects.get(generation_attempt=result)
        source = self.operation_plan["source_revision"]
        self.assertIsNone(source["id"])
        self.assertEqual(self.operation_plan["source_kind"], "literal")
        self.assertEqual(read_production_text_candidate(artifact)["candidate"], self.candidate)
        self.assertEqual(artifact.role, "structured_script")
        self.assertEqual(ExecutionTextResponseReview.objects.get(attempt=result).original_text, peer.invoke.return_value["text"])
        self.run.refresh_from_db()
        self.assertIsNone(self.run.script_revision_id)

    def test_review_required_payload_keeps_real_billing_and_request_id(self):
        self.queued()
        result, _ = self.process({"text": "invalid", "request_id": "failed-result-receipt", "actual_cost": "0.02", "currency": "USD"})
        self.assertEqual((result.status, result.provider_request_id, result.actual_cost), ("awaiting_review", "failed-result-receipt", Decimal("0.02")))
        self.run.refresh_from_db()
        self.assertEqual((self.run.actual_cost_total, self.run.committed_cost_total), (Decimal("0.02"), Decimal("0")))

    def test_due_preparation_scan_rechecks_production_without_creating_asset_proxy(self):
        from studio.services.execution_preparation_queue import claim_next_execution_preparation, process_claimed_execution_preparation
        prep = self.advance(self.prepare())
        self.assertEqual(prep.reason_code, "execution_smoke_missing")
        self.pass_smoke()
        type(prep).objects.filter(pk=prep.pk).update(next_check_at=timezone.now())
        claim = claim_next_execution_preparation()
        self.assertIsNotNone(claim)
        self.assertEqual(claim.pk, prep.pk)
        process_claimed_execution_preparation(claim)
        self.assertEqual(GenerationAttempt.objects.filter(preparation=prep).count(), 1)
        self.assertFalse(AssetGenerationJob.objects.exists())

    def visual_root(self, *, following=None):
        from studio.tests.visual_rules_fixtures import configure_visual_instance
        configure_visual_instance(self)
        self.run = self.root(stage_key="visual_rules_generate", following=following)
        self.run.allow_external_calls = True
        self.run.save(update_fields=("allow_external_calls",))

    def test_visual_rules_keep_independent_candidate_without_overwriting_project(self):
        from studio.models import ExecutionTextResponseReview
        self.visual_root()
        self.queued()
        result, peer = self.process()
        self.assertEqual(result.status, "succeeded", result.error_code)
        artifact = GeneratedArtifact.objects.get(generation_attempt=result)
        self.assertEqual((artifact.kind, artifact.role, artifact.status), ("other", "visual_rules_candidate", "available"))
        self.assertIn("color_design", artifact.manifest_metadata["result"]["result"])
        self.assertIn("color_script", artifact.manifest_metadata["result"]["result"]["candidate"])
        self.project.refresh_from_db()
        self.assertEqual(self.project.continuity_summary, "")
        self.assertEqual(ExecutionTextResponseReview.objects.get(attempt=result).original_text, peer.invoke.return_value["text"])

    def test_original_continue_api_can_dispatch_owned_stage_preparation(self):
        from rest_framework.test import APIClient
        self.pass_smoke()
        prep = self.prepare()
        client = APIClient()
        client.force_authenticate(self.actor)
        response = client.post(f"/api/v1/execution-preparations/{prep.pk}/continue/",
            {"expected_preparation_revision": prep.revision}, format="json")
        self.assertEqual(response.status_code, 200, response.data)
        prep.refresh_from_db()
        self.assertEqual(prep.status, "dispatched")
        self.assertEqual(GenerationAttempt.objects.filter(preparation=prep).count(), 1)

    def test_original_worker_unhandled_error_cannot_enter_old_fallback_or_leak_error_text(self):
        from studio.services.text_generation_queue import fail_unhandled_text_attempt
        self.queued()
        claim = claim_next_text_attempt("isolated-crash-worker")
        peer = Mock()
        peer.invoke.side_effect = RuntimeError("private-provider-response-must-not-be-recorded")
        with patch("studio.services.execution_production_text_worker.driver_for", return_value=peer), self.assertRaises(RuntimeError):
            process_text_attempt(claim)
        from studio.services import text_generation_queue
        self.assertFalse(hasattr(text_generation_queue, "_fail_attempt"))
        fail_unhandled_text_attempt(claim, RuntimeError("private-provider-response-must-not-be-recorded"))
        claim.refresh_from_db()
        self.assertEqual((claim.status, claim.error_code), ("failed", "uncertain_remote_response"))
        self.assertNotIn("private-provider-response", claim.error_message)
        self.assertIsNone(claim.actual_cost)

    def test_stale_worker_cannot_fail_a_reclaimed_attempt(self):
        from datetime import timedelta
        from studio.services.text_generation_queue import fail_unhandled_text_attempt
        self.queued()
        old = claim_next_text_attempt("old-worker")
        GenerationAttempt.objects.filter(pk=old.pk).update(heartbeat_at=timezone.now() - timedelta(minutes=10))
        self.assertEqual(recover_stale_text_attempts(5), {"requeued": 1, "uncertain": 0})
        new = claim_next_text_attempt("new-worker")
        fail_unhandled_text_attempt(old, RuntimeError("stale exception"))
        new.refresh_from_db()
        self.assertEqual(new.status, "running")
        self.assertNotEqual(new.claim_token, old.claim_token)
