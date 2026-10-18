"""Real Artifact/Revision transactions with explicitly isolated call rows.

No native admission, provider, worker claim or deployment acceptance is claimed.
Outputs use the real version service; lifecycle rows are declared test fixtures.
"""
from copy import deepcopy
from hashlib import sha256
import json
from unittest.mock import patch
from uuid import uuid4

from django.contrib.auth import get_user_model
from django.core.exceptions import ValidationError
from django.db import IntegrityError, connection, transaction
from django.test import TestCase, override_settings
from django.test.utils import CaptureQueriesContext
from django.utils import timezone

from studio.models import (ApprovalDecision, ArtifactSelectionDecision, ExecutionPreparation, ExecutionTextResponseReview, GeneratedArtifact,
    GenerationAttempt, ModelDeployment, ModelExecutionInstance, ModelExecutionInstanceVersion, ModelProfileVersion,
    ProductionRun, ProviderProfile, ScriptRevision, ScriptRevisionStructure, ScriptSource, SeriesProject, StageRun)
from studio.services.business_outputs import output_reference, write_output
from studio.services.json_contracts import assign_json_contract, json_contract
# Storage-unit tests supply an explicit contract; integration tests exercise the
# public request-bound functions using actual frozen manifests and files.
from studio.services.script_semantic_candidates import (
    _read_script_semantic_candidate as read_script_semantic_candidate,
    _save_script_semantic_candidate as save_script_semantic_candidate,
)
from studio.tests.test_execution_context_inputs import PROJECT_CONFIG
from studio.tests import test_script_revision_structures as storage_fixtures


LIMIT = 1_000_000


class ScriptSemanticCandidateTests(TestCase):
    make_output = storage_fixtures.ScriptRevisionStructureTests.make_output

    def setUp(self):
        self.actor = get_user_model().objects.create_user(username="script-candidate-fixture", is_staff=True)
        self.project = SeriesProject.objects.create(owner=self.actor, title="Isolated candidate", **PROJECT_CONFIG)
        self.output, self.raw = self.make_output()
        self.output_ref = output_reference(self.output)
        self.text = " \r\n" + json.dumps(self.raw, ensure_ascii=False, indent=2).replace("\n", "\r\n") + "\r\n "
        self.run = ProductionRun.objects.create(project=self.project, owner=self.actor, plan_source="manual",
            status="running", idempotency_key=str(uuid4()), request_sha256="a" * 64)
        self.stage = StageRun.objects.create(production_run=self.run, stage_key="script_generate", ordinal=1,
            mode="ai", status="running", idempotency_key=str(uuid4()))
        provider = ProviderProfile.objects.create(key="candidate-fixture", name="No network", driver_type="openai_compatible")
        model = ModelProfileVersion.objects.create(profile_key="candidate-fixture", version=1, provider=provider,
            model_identifier="storage-fixture-only", modality="text")
        deployment = ModelDeployment.objects.create(key="candidate-fixture", name="No network", profile_version=model)
        instance = ModelExecutionInstance.objects.create(key="candidate-fixture", name="Storage fixture only")
        self.version = ModelExecutionInstanceVersion.objects.create(instance=instance, version=1,
            deployment=deployment, operation="script.generate", prompt_mode="manual")
        self.counter = 0
        self.attempt = self.call()
        self.enterContext(patch("studio.services.provider_registry._request_json", side_effect=AssertionError("no network")))

    def call(self, operation="script.generate"):
        self.counter += 1
        digest = sha256(f"isolated-call-{self.counter}".encode()).hexdigest()
        version = self.version
        if operation != version.operation:
            version, _ = ModelExecutionInstanceVersion.objects.get_or_create(instance=version.instance,
                operation=operation, defaults={"version": 2, "deployment": version.deployment, "prompt_mode": "manual"})
        prep = ExecutionPreparation.objects.create(stage_run=self.stage, execution_instance_version=version,
            purpose="production", unit_key="script", request_key=str(uuid4()), status="dispatched", created_by=self.actor,
            intent_sha256=digest, request_sha256=digest, request_frozen_at=timezone.now())
        return GenerationAttempt.objects.create(stage_run=self.stage, execution_instance_version=version,
            preparation=prep, role="primary", unit_key="script", attempt_number=self.counter,
            operation=operation, status="running", request_sha256=digest)

    def write(self, **changes):
        return save_script_semantic_candidate(self.attempt, self.stage, self.run,
            **{"response_text": self.text, "expected_output": self.output_ref, "max_bytes": LIMIT, **changes})

    def finish_fixture(self, **changes):
        digest = self.write(**changes)
        # Explicit settlement fixture, not an executed model request.
        self.attempt.status, self.attempt.result_sha256 = "succeeded", digest
        self.attempt.save(update_fields=("status", "result_sha256"))
        return GeneratedArtifact.objects.get(generation_attempt=self.attempt)

    def read(self, artifact, **changes):
        return read_script_semantic_candidate(artifact,
            **{"expected_output": self.output_ref, "max_bytes": LIMIT, **changes})

    def counts(self):
        return tuple(model.objects.count() for model in (ScriptSource, ScriptRevision, ScriptRevisionStructure, GeneratedArtifact))

    def replace_metadata(self, artifact, value):
        result = json_contract(value["result"], max_bytes=LIMIT)
        value["result_sha256"] = result.sha256
        self.attempt.result_sha256 = result.sha256
        self.attempt.save(update_fields=("result_sha256",))
        assign_json_contract(artifact, "manifest_metadata", value, max_bytes=LIMIT)
        artifact.save()

    def test_both_layouts_and_operations_save_one_exact_source_and_pointer_without_adoption(self):
        for layout, operation in (("a", "script.generate"), ("b", "script.structure")):
            self.output, self.raw = self.make_output(layout)
            self.output_ref = output_reference(self.output)
            self.raw["creative_extra"] = {"flag": False, "score": 0}
            text = " \r\n" + json.dumps(self.raw, ensure_ascii=False) + "\r\n "
            self.attempt = self.call(operation)
            artifact = self.finish_fixture(response_text=text)
            revision = ScriptRevision.objects.get(origin_generation_attempt=self.attempt)
            detail = self.read(artifact)
            self.assertEqual(detail["text"], text)
            self.assertEqual(detail["candidate"], self.raw)
            self.assertNotIn("source_value", detail["projection"])
            self.assertEqual(detail["warnings"], [f'{row["path"]}：{row["message"]}' for row in detail["projection"]["warnings"]])
            self.assertEqual(detail["projection"]["value"]["episodes"][0]["shots"][0]["dialogue"]["is_voiceover"], False)
            self.assertTrue(detail["projection"]["warnings"])
            self.assertEqual(artifact.sha256, sha256(text.encode()).hexdigest())
            self.assertEqual(artifact.size_bytes, len(text.encode()))
            self.assertEqual(artifact.manifest_metadata["result"], {"schema_version": 1,
                "script_revision_id": str(revision.pk), "script_structure_sha256": revision.structured_content.document_sha256})
            self.assertNotIn("master_script", artifact.manifest_metadata)
            self.assertNotIn("source_value", artifact.manifest_metadata)
            self.assertEqual((artifact.status, revision.status), ("available", "preview"))
        self.assertEqual(self.counts(), (2, 2, 2, 2))
        self.project.refresh_from_db(); self.run.refresh_from_db(); self.stage.refresh_from_db()
        self.assertEqual(self.project.master_script, "")
        self.assertIsNone(self.run.script_revision_id)
        self.assertEqual((self.run.status, self.stage.status), ("running", "running"))
        self.assertFalse(ApprovalDecision.objects.exists())
        self.assertFalse(ArtifactSelectionDecision.objects.exists())

    def test_artifact_database_failure_rolls_back_all_four_rows(self):
        with patch.object(GeneratedArtifact, "save", side_effect=IntegrityError("isolated artifact failure")):
            with self.assertRaises(IntegrityError):
                self.write()
        self.assertEqual(self.counts(), (0, 0, 0, 0))
        self.attempt.refresh_from_db()
        self.assertEqual((self.attempt.status, self.attempt.result_sha256), ("running", ""))

    def test_actual_outer_settlement_failure_rolls_back_artifact_and_original_source(self):
        with self.assertRaises(RuntimeError), transaction.atomic():
            self.finish_fixture()
            raise RuntimeError("isolated caller settlement failure")
        self.assertEqual(self.counts(), (0, 0, 0, 0))
        self.attempt.refresh_from_db()
        self.assertEqual((self.attempt.status, self.attempt.result_sha256), ("running", ""))

    def test_duplicate_attempt_uses_original_unique_key_and_leaves_first_candidate_intact(self):
        artifact = self.finish_fixture()
        with self.assertRaises(IntegrityError):
            self.write()
        self.assertEqual(self.counts(), (1, 1, 1, 1))
        self.assertEqual(self.read(artifact)["text"], self.text)

    def test_separate_candidates_remain_independent_even_for_same_output_and_unit(self):
        first = self.finish_fixture()
        self.attempt = self.call()
        second_text = self.text + "\r\n "
        second = self.finish_fixture(response_text=second_text)
        self.assertEqual(first.variant_group, second.variant_group)
        self.assertNotEqual(first.variant_index, second.variant_index)
        self.assertNotEqual(first.manifest_metadata["script_revision_id"], second.manifest_metadata["script_revision_id"])
        self.assertEqual(self.read(first)["text"], self.text)
        self.assertEqual(self.read(second)["text"], second_text)

    def test_invalid_response_contract_and_limit_never_create_partial_candidate(self):
        invalid = deepcopy(self.raw)
        invalid["episodes"][0]["shots"][0]["references"][0]["variant_key"] = "missing"
        draft, _ = self.make_output(publish=False)
        from studio.services.business_outputs import verified_output_contract
        for args in ({"response_text": "not json"}, {"response_text": json.dumps(invalid)},
                {"expected_output": verified_output_contract(draft).value}, {"max_bytes": 20}):
            with self.subTest(args=args), self.assertRaises(ValidationError):
                self.write(**args)
            self.assertEqual(self.counts(), (0, 0, 0, 0))

    def test_writer_rejects_unrelated_stage_and_call_identity(self):
        for key, value in (("stage_run_id", uuid4()), ("role", "auxiliary"), ("operation", "text.structured"),
                ("request_sha256", "b" * 64), ("execution_instance_version_id", uuid4())):
            old = getattr(self.attempt, key)
            setattr(self.attempt, key, value)
            with self.subTest(key=key), self.assertRaises(ValidationError):
                self.write()
            setattr(self.attempt, key, old)
        self.assertEqual(self.counts(), (0, 0, 0, 0))

    def test_reads_are_bounded_and_do_not_reparse_or_query_current_output_configuration(self):
        artifact = self.finish_fixture()
        artifact.production_run = self.run
        self.output = write_output(actor=self.actor, output_id=self.output.pk,
            expected_revision=self.output.revision, action="retire")
        with patch("studio.services.script_revision_structures.parse_script_semantic_output", side_effect=AssertionError("no reparse")), \
                CaptureQueriesContext(connection) as captured:
            detail = self.read(artifact)
        self.assertEqual(len(captured), 3)
        for query in captured:
            self.assertNotIn('"studio_businessoutputversion"', query["sql"])
            self.assertNotIn('"studio_configrevision"', query["sql"])
        self.assertEqual(detail["output_contract"], self.output_ref)
        self.assertEqual(detail["text"], self.text)
        if connection.vendor == "sqlite":
            with connection.cursor() as cursor:
                for query in captured:
                    cursor.execute("EXPLAIN QUERY PLAN " + query["sql"])
                    self.assertIn("INDEX", str(cursor.fetchall()).upper())

    def test_reader_rejects_self_consistent_manifest_with_wrong_structure_digest(self):
        artifact = self.finish_fixture()
        value = deepcopy(artifact.manifest_metadata)
        value["result"]["script_structure_sha256"] = "f" * 64
        self.replace_metadata(artifact, value)
        with self.assertRaises(ValidationError):
            self.read(artifact)

    def test_reader_rejects_wrong_output_even_when_both_versions_are_valid(self):
        artifact = self.finish_fixture()
        other, _ = self.make_output("b")
        with self.assertRaises(ValidationError):
            self.read(artifact, expected_output=output_reference(other))

    def test_reader_rejects_missing_detail_changed_original_and_wrong_origin(self):
        artifact = self.finish_fixture()
        revision = ScriptRevision.objects.get(origin_generation_attempt=self.attempt)
        original = revision.content
        ScriptRevision.objects.filter(pk=revision.pk).update(content=original + " ")
        with self.assertRaises(ValidationError):
            self.read(artifact)
        ScriptRevision.objects.filter(pk=revision.pk).update(content=original, origin_generation_attempt=None)
        with self.assertRaises(ValidationError):
            self.read(artifact)
        ScriptRevision.objects.filter(pk=revision.pk).update(origin_generation_attempt=self.attempt)
        ScriptRevisionStructure.objects.filter(revision=revision).delete()
        with self.assertRaises(ValidationError):
            self.read(artifact)
        self.assertEqual(ScriptRevisionStructure.objects.count(), 0)

    def test_reader_checks_status_identity_and_original_artifact_bytes_even_for_audit(self):
        artifact = self.finish_fixture()
        for key, value in (("sha256", "f" * 64), ("size_bytes", 1), ("object_key", "not-script"),
                ("variant_index", 100), ("variant_group", "wrong"), ("mime_type", "text/other")):
            old = getattr(artifact, key)
            setattr(artifact, key, value)
            with self.subTest(key=key), self.assertRaises(ValidationError):
                self.read(artifact, for_audit=True)
            setattr(artifact, key, old)
        artifact.status = "rejected"
        with self.assertRaises(ValidationError):
            self.read(artifact)
        self.assertEqual(self.read(artifact, for_audit=True)["text"], self.text)
        GenerationAttempt.objects.filter(pk=self.attempt.pk).update(ignore_late_result=True)
        with self.assertRaises(ValidationError):
            self.read(artifact, for_audit=True)

    def test_unsettled_candidate_cannot_masquerade_as_success(self):
        self.write()
        artifact = GeneratedArtifact.objects.get(generation_attempt=self.attempt)
        with self.assertRaises(ValidationError):
            self.read(artifact)

    def test_human_marker_without_original_confirmed_review_cannot_create_a_candidate(self):
        for recovery in ("not-a-record", {}, {"review_id": str(uuid4()), "original_sha256": "a" * 64,
                "corrected_sha256": sha256(self.text.encode()).hexdigest(), "confirmed_by": str(self.actor.pk)}):
            with self.subTest(recovery=recovery), self.assertRaises(ValidationError):
                self.write(recovery=recovery)
            self.assertEqual(self.counts(), (0, 0, 0, 0))

    @override_settings(STORAGES={
        "default": {"BACKEND": "django.core.files.storage.InMemoryStorage"},
        "staticfiles": {"BACKEND": "django.contrib.staticfiles.storage.StaticFilesStorage"},
    })
    def test_explicit_recovery_uses_actual_confirmed_bytes_not_just_a_human_marker(self):
        from studio.services.execution_response_storage import store_response
        key, size, digest = store_response("isolated invalid original")
        corrected_key, corrected_size, corrected_digest = store_response(self.text)
        review = ExecutionTextResponseReview(attempt=self.attempt, original_file=key,
            original_size=size, original_sha256=digest, corrected_file=corrected_key,
            corrected_size=corrected_size, corrected_sha256=corrected_digest,
            confirmed_by=self.actor, confirmed_at=timezone.now())
        assign_json_contract(review, "diagnostics", {"schema_version": 1, "summary": "isolated confirmed recovery"})
        review.save()
        recovery = {"review_id": str(review.pk), "original_sha256": digest,
            "corrected_sha256": corrected_digest, "confirmed_by": str(self.actor.pk)}
        with self.assertRaises(ValidationError):
            self.write(response_text=self.text + " ", recovery=recovery)
        self.assertEqual(self.counts(), (0, 0, 0, 0))
        artifact = self.finish_fixture(recovery=recovery)
        self.assertEqual(self.read(artifact)["human_response_recovery"], recovery)
        self.assertEqual(self.read(artifact)["text"], self.text)
        revision = ScriptRevision.objects.get(origin_generation_attempt=self.attempt)
        self.assertEqual(revision.source.source_type, "paste")
        self.assertIsNone(revision.confirmed_at)
        self.assertFalse(ArtifactSelectionDecision.objects.exists())
