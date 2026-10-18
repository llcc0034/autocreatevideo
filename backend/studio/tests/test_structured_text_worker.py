"""Original worker + isolated provider and frozen rows; not recipe admission.

The G03 stage/route choice is deliberately outside this fixture. The preparation
and request use real compilers/storage, while submission admission is an explicit
test boundary. No real Provider, current database, auto-selection or business writer.
"""
from copy import deepcopy
import json
from unittest.mock import Mock, patch
from uuid import uuid4

from django.core.exceptions import ValidationError
from django.db import OperationalError
from django.test import TestCase, override_settings
from django.utils import timezone
from rest_framework.test import APIClient

from studio.models import (ArtifactSelectionDecision, ExecutionTextResponseReview, GeneratedArtifact,
    GenerationAttempt, ProductionRun, ScriptRevision, SeriesProject, StageRun)
from studio.services.execution_definitions import build_execution_definition
from studio.services.execution_instances import create_execution_instance, create_execution_draft, publish_execution_version
from studio.services.execution_preparation_requests import compile_prepared_execution_transport
from studio.services.execution_preparations import _persist_preparation
from studio.services.execution_production_candidates import read_production_text_candidate
from studio.services.execution_request_preparation import resolve_execution_request_facts
from studio.services.execution_request_storage import reference_prepared_request, store_prepared_request
from studio.services.execution_response_storage import ExecutionResponseStorageError
from studio.services.execution_text_response_review import complete_text_response, describe_text_response_review
from studio.services.json_contracts import assign_json_contract, json_contract
from studio.services.text_generation_queue import claim_next_text_attempt, process_text_attempt
from studio.tests.test_execution_context_inputs import PROJECT_CONFIG
from studio.tests import test_execution_structured_text as fixtures


@override_settings(AI_PROVIDER_ALLOWED_HOSTS=("api.example.com",), STORAGES={
    "default": {"BACKEND": "django.core.files.storage.InMemoryStorage"},
    "staticfiles": {"BACKEND": "django.contrib.staticfiles.storage.StaticFilesStorage"},
})
class StructuredTextWorkerTests(TestCase):
    setUpTestData = classmethod(fixtures.StructuredTextExecutionTests.setUpTestData.__func__)
    setUp = fixtures.StructuredTextExecutionTests.setUp
    node = fixtures.StructuredTextExecutionTests.node
    create = fixtures.StructuredTextExecutionTests.create
    publish = fixtures.StructuredTextExecutionTests.publish
    output = fixtures.StructuredTextExecutionTests.output

    def queued(self, nodes):
        output = self.output(nodes)
        instance = create_execution_instance(key=f"structured-{uuid4()}", name="Isolated structured output", description="", actor=self.actor)
        version = create_execution_draft(instance_id=instance.pk, expected_revision=instance.revision, actor=self.actor,
            selection={"deployment": self.deployment, "operation": "text.structured", "mode": "", "prompt_mode": "manual",
                "workflow_version": None, "prompt_profile_version": None, "output_version": output})
        definition = build_execution_definition(version.pk)
        version = publish_execution_version(version_id=version.pk, expected_revision=version.revision,
            expected_definition_sha256=definition.sha256, actor=self.actor)
        project = SeriesProject.objects.create(owner=self.actor, title="Isolated structured worker", **PROJECT_CONFIG)
        run = ProductionRun(owner=self.actor, project=project, plan_source="manual", status="running",
            idempotency_key=str(uuid4()), request_sha256="a" * 64)
        for name in ("input_snapshot", "config_snapshot"):
            assign_json_contract(run, name, {"schema_version": 1})
        run.save()
        # Explicit storage fixture, not a new registered production stage.
        stage = StageRun.objects.create(production_run=run, stage_key="structured_worker_fixture", ordinal=1,
            mode="ai", status="running", idempotency_key=str(uuid4()))
        requested = {"inputs": {}, "parameters": {}, "manual_channels": {"positive_prompt": "Use supplied facts only."},
            "subject_facts": [], "source_language": "", "target_language": ""}
        review = json_contract({"schema_version": 1, "mode": "manual"})
        facts = resolve_execution_request_facts(definition, requested, owner=self.actor, unit_key="structured",
            max_media_bytes=65536, parameter_policies=[], resolve_parameter_source=lambda _: self.fail("No sources"),
            review_contract=review)
        prep = _persist_preparation(lookup={"stage_run_id": stage.pk, "unit_key": "structured", "request_key": "one"},
            version=version, purpose="production", unit_key="structured", request_key="one",
            request=json_contract({"definition_sha256": definition.sha256, "requested": requested}),
            resolved=facts.resolved_inputs, review=review, actor=self.actor)
        preview = compile_prepared_execution_transport(preparation_id=prep.pk,
            expected_preparation_revision=prep.revision, actor=self.actor)
        detail = prep.contract
        frozen = store_prepared_request(detail, {"schema_version": 1, "preparation_id": str(prep.pk),
            "basis_preparation_revision": prep.revision, "intent_sha256": prep.intent_sha256,
            "field_selections": preview.field_selections.value, "transport": preview.transport.value})
        detail.save()
        prep.status, prep.revision, prep.request_sha256, prep.request_frozen_at = "dispatched", 2, frozen.sha256, timezone.now()
        prep.save()
        attempt = GenerationAttempt(stage_run=stage, preparation=prep, execution_instance_version=version,
            model_deployment=self.deployment, role="primary", operation="text.structured", unit_key="structured",
            attempt_number=1, status="queued", request_sha256=prep.request_sha256, estimated_cost=0, committed_cost=0)
        reference_prepared_request(attempt, detail)
        attempt.save()
        return prep, attempt

    def process(self, text, *, late=False):
        claim = claim_next_text_attempt("isolated-structured-worker")
        self.assertIsNotNone(claim)
        peer = Mock()
        def invoke(*args):
            if late:
                GenerationAttempt.objects.filter(pk=claim.pk).update(ignore_late_result=True)
            return {"text": text, "request_id": "isolated-structured-response"}
        peer.invoke.side_effect = invoke
        def admitted(attempt):
            attempt.submission_started_at = timezone.now()
            attempt.status = "submitting"
            attempt.save(update_fields=("submission_started_at", "status", "updated_at"))
            return True, attempt
        with patch("studio.services.execution_production_text_worker.begin_production_execution_submission", side_effect=admitted), \
                patch("studio.services.execution_production_text_worker.driver_for", return_value=peer):
            result = process_text_attempt(claim)
        self.assertEqual(peer.invoke.call_count, 1, f"{result.status}: {result.error_code}: {result.error_message}")
        return result

    def test_worker_retains_body_and_typed_roots_once_without_business_writes(self):
        for kind, nullable, value in (("text", False, "说明"), ("integer", False, 0), ("boolean", False, False),
                ("text", True, None), ("object", False, {}), ("list", False, [])):
            with self.subTest(kind=kind, value=value):
                nodes = [self.node("root", kind, allow_null=nullable, allow_blank=True, allow_empty=True)]
                if kind == "list":
                    nodes.append(self.node("item", "text", "root"))
                prep, _ = self.queued(nodes)
                raw = "\n" + json.dumps(value, ensure_ascii=False) + "\n"
                result = self.process(raw)
                self.assertEqual(result.status, "succeeded", result.error_message)
                artifact = result.artifacts.get()
                self.assertEqual((artifact.kind, artifact.role), ("other", "structured_text_candidate"))
                detail = read_production_text_candidate(artifact)
                self.assertEqual(detail["candidate"]["value"], value)
                self.assertEqual(detail["output_contract"]["id"], detail["candidate"]["output_version_id"])
                retained = describe_text_response_review(prep)
                self.assertEqual(retained["original_text"], raw)
                self.assertFalse(retained["editable"])
                process_text_attempt(result)
                self.assertEqual(result.artifacts.count(), 1)
        self.assertFalse(ScriptRevision.objects.exists())
        self.assertFalse(ArtifactSelectionDecision.objects.exists())

    def test_defaults_and_unknown_fields_preserve_provenance_and_original_text(self):
        prep, _ = self.queued([self.node("root", "object"),
            self.node("count", "integer", "root", property_name="计数", has_default=True, default_value=0)])
        raw = '{"额外信息":false}'
        result = self.process(raw)
        self.assertEqual(result.status, "succeeded", result.error_message)
        detail = read_production_text_candidate(result.artifacts.get())
        self.assertEqual(detail["candidate"]["value"], {"计数": 0, "额外信息": False})
        self.assertEqual(detail["candidate"]["defaults"][0]["source"], "configured_default")
        self.assertTrue(detail["structure_warnings"])
        self.assertTrue(detail["warnings"])
        self.assertEqual(describe_text_response_review(prep)["original_text"], raw)
        self.assertTrue(describe_text_response_review(prep)["notices"])

    def test_invalid_json_duplicate_keys_and_types_preserve_original_and_path_errors(self):
        for raw in ('{"count":', '{"count":1,"count":2}', '{"count":"wrong"}'):
            prep, _ = self.queued([self.node("root", "object"),
                self.node("count", "integer", "root", property_name="count", required=True)])
            result = self.process(raw)
            self.assertEqual(result.status, "awaiting_review", result.error_message)
            self.assertFalse(result.artifacts.exists())
            detail = describe_text_response_review(prep)
            self.assertEqual(detail["original_text"], raw)
            self.assertEqual(detail["diagnostics"]["validation_code"], "structured_text_result_invalid")
            self.assertTrue(detail["diagnostics"]["structure_errors"])

    def test_explicit_correction_uses_same_contract_and_does_not_select(self):
        prep, _ = self.queued([self.node("root", "boolean")])
        result = self.process('"wrong type"')
        review = ExecutionTextResponseReview.objects.get(attempt=result)
        fixed = complete_text_response(preparation_id=prep.pk, expected_preparation_revision=prep.revision,
            review_id=review.pk, original_sha256=review.original_sha256, text="false", actor=self.actor)
        result.refresh_from_db()
        self.assertEqual(result.status, "succeeded")
        artifact = result.artifacts.get()
        self.assertIs(read_production_text_candidate(artifact)["candidate"]["value"], False)
        review.refresh_from_db()
        self.assertEqual(review.original_text, '"wrong type"')
        self.assertEqual(review.corrected_text, "false")
        self.assertEqual(review.confirmed_by_id, self.actor.pk)
        complete_text_response(preparation_id=prep.pk, expected_preparation_revision=fixed.revision,
            review_id=review.pk, original_sha256=review.original_sha256, text="false", actor=self.actor)
        self.assertEqual(result.artifacts.count(), 1)
        self.assertFalse(ArtifactSelectionDecision.objects.exists())

    def test_storage_failure_or_late_result_never_creates_a_candidate(self):
        prep, _ = self.queued([self.node("root", "text")])
        with patch("studio.services.execution_response_storage.store_response", side_effect=ExecutionResponseStorageError("isolated failure")):
            result = self.process('"received"')
        self.assertEqual((result.status, result.error_code), ("failed", "execution_response_storage_failed"))
        self.assertFalse(result.artifacts.exists())
        self.assertFalse(ExecutionTextResponseReview.objects.filter(attempt=result).exists())
        self.queued([self.node("root", "text")])
        late = self.process('"late"', late=True)
        self.assertEqual(late.status, "cancelled")
        self.assertFalse(late.artifacts.exists())

    def test_candidate_reader_rejects_self_consistent_wrong_output_identity(self):
        self.queued([self.node("root", "text")])
        result = self.process('"original"')
        artifact = result.artifacts.get()
        metadata = deepcopy(artifact.manifest_metadata)
        metadata["result"]["result"]["output_version_id"] = str(uuid4())
        changed = json_contract(metadata["result"])
        metadata["result_sha256"] = changed.sha256
        assign_json_contract(artifact, "manifest_metadata", metadata)
        artifact.sha256, artifact.size_bytes = changed.sha256, changed.size_bytes
        artifact.save()
        result.result_sha256 = changed.sha256; result.save()
        with self.assertRaisesMessage(ValidationError, "确切输出合同不一致"):
            read_production_text_candidate(artifact)

    def test_candidate_database_failure_rolls_back_and_does_not_repeat_submission(self):
        prep, attempt = self.queued([self.node("root", "text")])
        with patch.object(GeneratedArtifact, "save", side_effect=OperationalError("isolated unavailable database")):
            with self.assertRaises(OperationalError):
                self.process('"received"')
        attempt.refresh_from_db()
        self.assertEqual(attempt.status, "submitting")
        self.assertFalse(attempt.artifacts.exists())
        self.assertFalse(ExecutionTextResponseReview.objects.filter(attempt=attempt).exists())
        with patch("studio.services.execution_production_text_worker.driver_for") as provider:
            recovered = process_text_attempt(attempt)
        provider.assert_not_called()
        self.assertEqual((recovered.status, recovered.error_code), ("failed", "uncertain_remote_response"))
        self.assertEqual(GenerationAttempt.objects.count(), 1)

    def test_existing_artifact_and_preparation_apis_read_result_and_raw_without_side_effects(self):
        prep, _ = self.queued([self.node("root", "list"), self.node("item", "integer", "root")])
        result = self.process('[0, 2]')
        artifact = result.artifacts.get()
        client = APIClient(); client.force_authenticate(self.actor)
        before = (GenerationAttempt.objects.count(), GeneratedArtifact.objects.count())
        reply = client.get(f"/api/v1/generated-artifacts/{artifact.pk}/")
        self.assertEqual(reply.status_code, 200, reply.data)
        self.assertEqual(reply.data["text_candidate"]["candidate"]["value"], [0, 2])
        from studio.services.data_structures import validate_data_structure_snapshot
        detail = reply.data["text_candidate"]
        self.assertEqual(detail["json_schema"], validate_data_structure_snapshot(detail["output_contract"]["data_structure"]))
        self.assertEqual(detail["json_schema"]["type"], "array")
        self.assertEqual(detail["json_schema"]["items"]["type"], "integer")
        response = client.get(f"/api/v1/execution-preparations/{prep.pk}/")
        self.assertEqual(response.status_code, 200, response.data)
        self.assertEqual(response.data["text_response_review"]["original_text"], '[0, 2]')
        center = client.get(f"/api/v1/task-center/preparation/{prep.pk}/")
        self.assertEqual(center.status_code, 200, center.data)
        data = center.data.get("data", center.data)
        self.assertEqual(data["output"]["state"], "available", data["validation"])
        candidate = data["output"]["result"]["artifacts"][0]["result"]
        self.assertEqual(candidate["candidate"]["value"], [0, 2])
        self.assertEqual(candidate["output_contract"]["id"], candidate["candidate"]["output_version_id"])
        self.assertEqual((GenerationAttempt.objects.count(), GeneratedArtifact.objects.count()), before)
        self.assertFalse(ArtifactSelectionDecision.objects.exists())
