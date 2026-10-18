"""Real request persistence and queue claims, with isolated completion reports.

No external model is invoked; these are not operational acceptance results.
"""
from copy import deepcopy
from datetime import timedelta
from unittest.mock import patch
from uuid import uuid4

from django.core.exceptions import ValidationError
from django.db import connection
from django.test import TestCase, override_settings
from django.utils import timezone

from studio.models import ModelDeploymentSmokeAttempt, ModelDeploymentCapabilitySnapshot
from studio.services.execution_admission import evaluate_execution_admission
from studio.services.execution_definitions import build_execution_definition
from studio.services.execution_finalization import ExecutionAdmissionFacts, finalize_execution
from studio.services.execution_instances import publish_execution_version
from studio.services.execution_preparations import create_execution_instance_preparation
from studio.services.execution_request_preparation import resolve_execution_request_facts
from studio.services.execution_smoke_evidence import (
    finish_prepared_smoke, read_instance_smoke_facts, instance_smoke_identity, instance_smoke_candidates,
)
from studio.services.json_contracts import assign_json_contract, json_contract
from studio.tests.test_execution_text_contracts import ExecutionTextContractTests


@override_settings(AI_PROVIDER_ALLOWED_HOSTS=("api.example.com",))
class ExecutionSmokeEvidenceTests(TestCase):
    setUpTestData = classmethod(ExecutionTextContractTests.setUpTestData.__func__)
    version = ExecutionTextContractTests.version

    def setUp(self):
        ExecutionTextContractTests.setUp(self)
        self.execution = self.version()
        self.definition = build_execution_definition(self.execution.pk)
        publish_execution_version(version_id=self.execution.pk, expected_revision=self.execution.revision,
            expected_definition_sha256=self.definition.sha256, actor=self.actor)
        self.identity = instance_smoke_identity(self.execution.pk, self.definition)

    def attempt(self, frozen=True, request=None):
        attempt = ModelDeploymentSmokeAttempt.objects.create(deployment=self.deployment, scope="instance",
            idempotency_key=str(uuid4()),
            execution_instance_version=self.execution, operation=self.execution.operation, status="preparing",
            request_sha256="", target_sha256=self.definition.value["target_fingerprint"])
        intent = json_contract(request if request is not None else {"inputs": {}, "parameters": {"seed": 20},
            "manual_channels": {"positive_prompt": "Describe a blue box."},
            "subject_facts": [], "source_language": "", "target_language": ""})
        def resolve(definition, owner):
            return resolve_execution_request_facts(definition, intent.value, owner=self.actor, unit_key="smoke",
                max_media_bytes=65536, review_contract=json_contract({"schema_version": 1, "mode": "manual"}),
                parameter_policies=[], resolve_parameter_source=lambda _: self.fail("No source parameters"))
        preparation = create_execution_instance_preparation(owner_ref=("deployment_smoke_id", attempt.pk),
            version_id=self.execution.pk, expected_definition_sha256=self.definition.sha256,
            unit_key="smoke", request_key="main", intent=intent, resolve_facts=resolve, actor=self.actor)
        if frozen:
            def isolated_root_facts(prep, definition, request):
                return ExecutionAdmissionFacts((), "unknown", [], json_contract({"preparation_id": str(prep.pk),
                    "request_sha256": request.sha256, "test_scope": "isolated root checks; no remote submission"}))
            preparation = finalize_execution(preparation_id=preparation.pk, expected_preparation_revision=preparation.revision,
                actor=self.actor, collect_admission_facts=isolated_root_facts)
            self.assertEqual(preparation.status, "ready")  # Own Smoke gate only; no fake passed fact.
            attempt.request_sha256 = preparation.request_sha256
            attempt.status, attempt.claim_token = "submitting", uuid4()
            attempt.save()
        return attempt, preparation

    def use_field_leaf(self):
        self.execution = self.version("prompt.fields.generate")
        self.definition = build_execution_definition(self.execution.pk)
        publish_execution_version(version_id=self.execution.pk, expected_revision=self.execution.revision,
            expected_definition_sha256=self.definition.sha256, actor=self.actor)
        self.identity = instance_smoke_identity(self.execution.pk, self.definition)
        batch = ExecutionTextContractTests.field_request(self)
        return batch, {"inputs": {"fields": {"kind": "literal", "value": deepcopy(batch.value)}},
            "parameters": {}, "manual_channels": {}, "subject_facts": [], "source_language": "", "target_language": ""}

    def finish(self, attempt, status="succeeded"):
        return finish_prepared_smoke(attempt, attempt.claim_token, status=status,
            evidence=json_contract({"protocol_fixture": "isolated validated worker outcome", "response_sha256": "f" * 64}))

    def test_result_keeps_exact_request_identity_and_does_not_create_resource_snapshot(self):
        attempt, preparation = self.attempt()
        before = deepcopy(preparation.contract.prepared_request)
        result = self.finish(attempt)
        facts = read_instance_smoke_facts(self.execution, self.definition)
        self.assertEqual(len(facts), 1)
        self.assertEqual((facts[0].identity, facts[0].attempt_id, facts[0].status), (self.identity, str(attempt.pk), "succeeded"))
        self.assertEqual(facts[0].expires_at - facts[0].finished_at, timedelta(seconds=3600))
        self.assertEqual(result.result["request_sha256"], preparation.request_sha256)
        self.assertIsNone(result.final_snapshot_id)
        self.assertFalse(ModelDeploymentCapabilitySnapshot.objects.exists())
        preparation.refresh_from_db()
        self.assertEqual(preparation.contract.prepared_request, before)

    def test_latest_failure_is_not_hidden_by_earlier_pass_or_inflight_attempt(self):
        first, _ = self.attempt()
        self.finish(first)
        second, _ = self.attempt()
        self.finish(second, "failed")
        self.attempt(frozen=False)
        facts = read_instance_smoke_facts(self.execution, self.definition)
        self.assertEqual((len(facts), facts[0].status, facts[0].attempt_id), (1, "failed", str(second.pk)))
        decision = evaluate_execution_admission(identity=self.identity, smoke_facts=facts, purpose="production",
            smoke_policy="require_passed", capacity_policy="warn", capacity_state="unknown", hard_blockers=[], now=timezone.now())
        self.assertFalse(decision["can_submit"])
        self.assertEqual(decision["evidence_state"], "failed")

    def test_ttl_is_frozen_and_expiry_is_policy_not_fake_failure(self):
        attempt, _ = self.attempt()
        self.finish(attempt)
        facts = read_instance_smoke_facts(self.execution, self.definition)
        provider = self.deployment.profile_version.provider
        changed = {**provider.driver_config, "capability_ttl_seconds": 7200}
        assign_json_contract(provider, "driver_config", changed)
        provider.save()
        self.assertEqual(read_instance_smoke_facts(self.execution, self.definition), facts)
        for policy, allowed in (("require_passed", True), ("require_fresh", False)):
            decision = evaluate_execution_admission(identity=self.identity, smoke_facts=facts, purpose="production", smoke_policy=policy,
                capacity_policy="warn", capacity_state="unknown", hard_blockers=[], now=facts[0].expires_at + timedelta(seconds=1))
            self.assertEqual((decision["can_submit"], decision["evidence_state"]), (allowed, "expired"))

    def test_stale_claim_or_cancel_cannot_append_pass_evidence(self):
        attempt, preparation = self.attempt()
        stale = deepcopy(attempt)
        ModelDeploymentSmokeAttempt.objects.filter(pk=attempt.pk).update(claim_token=uuid4())
        result = self.finish(stale)
        self.assertEqual(result.result_sha256, "")
        self.assertEqual(read_instance_smoke_facts(self.execution, self.definition), ())
        attempt.refresh_from_db()
        attempt.status = "cancel_requested"
        attempt.save(update_fields=("status",))
        result = self.finish(attempt)
        self.assertEqual(result.status, "cancelled")
        self.assertIsNone(result.evidence_expires_at)
        self.assertEqual(read_instance_smoke_facts(self.execution, self.definition), ())

    def test_resource_pass_does_not_authorize_an_instance_and_scope_is_immutable(self):
        resource = ModelDeploymentSmokeAttempt.objects.create(deployment=self.deployment, scope="resource",
            operation=self.execution.operation, status="succeeded", request_sha256="a" * 64, target_sha256=self.identity.target_fingerprint,
            finished_at=timezone.now())
        self.assertEqual(read_instance_smoke_facts(self.execution, self.definition), ())
        with self.assertRaises(ValidationError):
            ModelDeploymentSmokeAttempt.objects.filter(pk=resource.pk).update(scope="instance", execution_instance_version=self.execution)
        resource.scope, resource.execution_instance_version = "instance", self.execution
        with self.assertRaises(ValidationError):
            resource.save()

    def test_unfrozen_or_corrupt_result_is_rejected_without_old_pass_fallback(self):
        attempt, preparation = self.attempt(frozen=False)
        attempt.status, attempt.claim_token = "submitting", uuid4()
        attempt.save()
        with self.assertRaisesMessage(ValidationError, "尚未完成准备"):
            self.finish(attempt)
        attempt.refresh_from_db()
        self.assertEqual(attempt.result_sha256, "")
        attempt.status = "failed"
        attempt.save(update_fields=("status",))
        later, _ = self.attempt()
        self.finish(later)
        ModelDeploymentSmokeAttempt.objects.filter(pk=later.pk).update(result={"tampered": True})
        with self.assertRaises(ValidationError):
            read_instance_smoke_facts(self.execution, self.definition)

    def test_evidence_lookup_uses_partial_instance_index_without_loading_json(self):
        attempt, _ = self.attempt()
        self.finish(attempt)
        candidates = instance_smoke_candidates(self.execution, self.identity)
        self.assertNotIn('"result"', str(candidates.query))
        self.assertEqual(set(candidates.first()), {"id", "status", "finished_at", "evidence_expires_at"})
        if connection.vendor == "sqlite":
            self.assertIn("idx_deploy_instance_smoke", candidates[:1].explain())

    def test_field_leaf_smoke_uses_exact_profile_rules_without_a_fake_parent_job(self):
        import json
        from studio.models import AssetGenerationJob, ExecutionFieldValue
        batch, request = self.use_field_leaf()
        attempt, preparation = self.attempt(request=request)
        transport = preparation.contract.prepared_request["transport"]
        self.assertEqual(transport["field_request_sha256"], batch.sha256)
        self.assertEqual(json.loads(transport["payload"]["messages"][1]["content"]), batch.value)
        self.finish(attempt)
        self.assertEqual(read_instance_smoke_facts(self.execution, self.definition)[0].status, "succeeded")
        self.assertFalse(AssetGenerationJob.objects.exists())
        self.assertFalse(ExecutionFieldValue.objects.exists())

    def test_field_smoke_cannot_substitute_caller_authored_rules(self):
        batch, request = self.use_field_leaf()
        request["inputs"]["fields"]["value"]["items"][0]["field_contract"]["generation_instruction"] = "Use different rules."
        with self.assertRaisesMessage(ValidationError, "不能自编规则"):
            self.attempt(request=request)
