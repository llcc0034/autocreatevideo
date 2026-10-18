"""Actual original-root admission using isolated completed Smoke reports.

No remote service is invoked. A test worker report is a protocol fixture, not
an assertion that a real model or the running platform passed acceptance.
"""
from copy import deepcopy
from unittest.mock import patch

from django.core.exceptions import ValidationError
from django.test import TestCase, override_settings
from django.utils import timezone

from studio.models import AssetGenerationJob, ModelDeployment, ModelDeploymentSmokeAttempt, RuntimeHost
from studio.services.asset_generation import create_asset_generation_job
from studio.services.execution_asset_admission import collect_asset_execution_admission
from studio.services.execution_finalization import finalize_execution
from studio.services.execution_definitions import execution_definition_version
from studio.services.execution_resource_admission import read_execution_resource_state
from studio.services.json_contracts import assign_json_contract
from studio.tests import test_execution_smoke_evidence as fixtures


@override_settings(AI_PROVIDER_ALLOWED_HOSTS=("api.example.com",))
class ExecutionAssetAdmissionTests(TestCase):
    setUpTestData = fixtures.ExecutionSmokeEvidenceTests.__dict__["setUpTestData"]
    version = fixtures.ExecutionSmokeEvidenceTests.version
    attempt = fixtures.ExecutionSmokeEvidenceTests.attempt
    finish = fixtures.ExecutionSmokeEvidenceTests.finish

    def setUp(self):
        fixtures.ExecutionSmokeEvidenceTests.setUp(self)
        from studio.services.configuration import create_config_revision, publish_config_revision
        from studio.services.execution_resource_policy import RESOURCE_POLICY
        revision = create_config_revision(scope_type="system", values={RESOURCE_POLICY: {
            "budget_by_currency": {"USD": "1.00"}, "call_count": 100, "output_length": 4096,
        }}, created_by=self.actor, change_summary="isolated asset review threshold")
        publish_config_revision(revision, published_by=self.actor)
        # Explicitly controlled local auth service, independent of the real
        # resource/evidence/budget reads below. Missing credentials are tested
        # separately with the actual driver validator.
        self.auth = self.enterContext(patch("studio.services.provider_registry.OpenAICompatibleDriver.validate_execution_credentials"))
        provider = self.deployment.provider
        config = deepcopy(provider.driver_config)
        config["pricing"] = {"fixed_per_request": "0.1", "currency": "USD"}
        assign_json_contract(provider, "driver_config", config)
        provider.save()
        self.job, _ = create_asset_generation_job(owner=self.actor, client_idempotency_key="admission-root",
            execution_instance_version_id=self.execution.pk, expected_definition_sha256=self.definition.sha256,
            job_type="prompt", media_type="video", name="Draft", parameters={"seed": 20},
            inputs={}, manual_channels={"positive_prompt": "Describe the supplied box."}, subject_facts=[],
            source_language="", target_language="", field_values=[], allow_external_calls=True, budget_limit=None)
        self.prep = self.job.execution_preparations.get()

    def finalize(self):
        self.prep.refresh_from_db()
        return finalize_execution(preparation_id=self.prep.pk, expected_preparation_revision=self.prep.revision,
            actor=self.actor, collect_admission_facts=collect_asset_execution_admission)

    def passed_fixture(self):
        attempt, _ = self.attempt()
        return self.finish(attempt)

    def test_missing_instance_evidence_blocks_without_using_a_resource_success(self):
        ModelDeploymentSmokeAttempt.objects.create(deployment=self.deployment, operation=self.execution.operation,
            status="succeeded", scope="resource", finished_at=timezone.now())
        result = self.finalize()
        self.assertEqual(result.status, "blocked")
        self.assertIsNone(result.request_frozen_at)
        decision = result.contract.evaluation["admission"]
        self.assertEqual(decision["evidence_state"], "missing")
        self.assertIn("execution_smoke_missing", {row["code"] for row in decision["blockers"]})
        self.job.refresh_from_db()
        self.assertEqual(self.job.status, "preparing")
        self.assertIsNone(self.job.estimated_cost)

    def test_exact_pass_and_root_budget_allow_freeze_but_not_queue_or_fake_capacity(self):
        passed = self.passed_fixture()
        result = self.finalize()
        self.assertEqual(result.status, "ready")
        decision = result.contract.evaluation["admission"]
        self.assertEqual(decision["evidence_attempt_id"], str(passed.pk))
        self.assertEqual(decision["capacity_state"], "unknown")
        root = result.contract.evaluation["root_checks"]
        self.assertEqual(root["budget"]["projected_total"], "0.100000")
        self.assertEqual(root["request_sha256"], result.request_sha256)
        self.assertTrue(root["dispatch_required"])
        self.job.refresh_from_db()
        self.assertEqual(self.job.status, "preparing")
        self.assertIsNone(self.job.submission_started_at)
        self.assertIsNone(self.job.estimated_cost)

    def test_disabled_resource_and_missing_external_authorization_remain_blocks_after_pass(self):
        self.passed_fixture()
        ModelDeployment.objects.filter(pk=self.deployment.pk).update(status="disabled")
        AssetGenerationJob.objects.filter(pk=self.job.pk).update(allow_external_calls=False, budget_limit=None)
        result = self.finalize()
        codes = {row["code"] for row in result.contract.evaluation["admission"]["blockers"]}
        self.assertTrue({"execution_resource_inactive", "execution_external_authorization_required"} <= codes)
        self.assertIsNone(result.request_frozen_at)

    def test_strict_capacity_still_blocks_production_with_a_real_combination_pass(self):
        from studio.services.configuration import create_config_revision, publish_config_revision
        revision = create_config_revision(scope_type="system", values={"execution.admission.capacity_policy": "require_evidence"},
            created_by=self.actor, change_summary="Isolated strict production policy")
        publish_config_revision(revision, published_by=self.actor)
        self.passed_fixture()
        result = self.finalize()
        self.assertEqual(result.reason_code, "execution_capacity_unknown")
        self.assertFalse(result.contract.evaluation["admission"]["own_capacity_gate_exempt"])
        self.assertIsNone(result.request_frozen_at)

    def test_budget_failure_then_continue_retains_prepared_seed_and_inputs(self):
        self.passed_fixture()
        from studio.services.configuration import create_config_revision, publish_config_revision
        from studio.services.execution_resource_policy import RESOURCE_POLICY, read_resource_policy
        def policy(amount):
            values = deepcopy(read_resource_policy().value["values"])
            values["budget_by_currency"] = {"USD": amount}
            revision = create_config_revision(scope_type="system", values={RESOURCE_POLICY: values}, created_by=self.actor, change_summary="isolated review policy")
            publish_config_revision(revision, published_by=self.actor)
        policy("0.01")
        before = deepcopy(self.prep.contract.resolved_inputs)
        result = self.finalize()
        self.assertEqual(result.reason_code, "execution_resource_confirmation_required")
        self.assertEqual(result.status, "awaiting_review")
        policy("1")
        result = self.finalize()
        self.assertEqual(result.status, "ready")
        self.assertEqual(result.contract.resolved_inputs, before)
        self.assertEqual(result.contract.prepared_request["transport"]["payload"]["seed"], 20)

    def test_owner_disable_or_uncertain_root_cannot_start_another_call(self):
        self.passed_fixture()
        self.actor.is_active = False
        self.actor.save(update_fields=["is_active"])
        # Staff reader may inspect another owner's root but cannot grant the
        # disabled actual owner permission to execute.
        from django.contrib.auth import get_user_model
        admin = get_user_model().objects.create_user(username="admission-other-admin", is_staff=True)
        result = finalize_execution(preparation_id=self.prep.pk, expected_preparation_revision=self.prep.revision,
            actor=admin, collect_admission_facts=collect_asset_execution_admission)
        self.assertEqual(result.reason_code, "execution_owner_unavailable")
        self.actor.is_active = True
        self.actor.save(update_fields=["is_active"])
        AssetGenerationJob.objects.filter(pk=self.job.pk).update(status="uncertain", submission_started_at=timezone.now())
        result = self.finalize()
        self.assertEqual(result.reason_code, "execution_root_not_submittable")
        self.assertIsNone(result.request_frozen_at)

    def test_credential_failure_is_redacted_not_success_or_raw_provider_error(self):
        self.auth.side_effect = ValidationError("sensitive-internal-diagnostic")
        result = read_execution_resource_state(execution_definition_version(self.execution.pk)).value
        self.assertIn("execution_transport_unavailable", {row["code"] for row in result["blockers"]})
        self.assertNotIn("sensitive-internal-diagnostic", str(result))

    def test_missing_or_disabled_managed_host_is_not_capacity_warning(self):
        host = RuntimeHost.objects.create(key="admission-host", name="Test host", status="disabled")
        ModelDeployment.objects.filter(pk=self.deployment.pk).update(runtime_host=host)
        result = read_execution_resource_state(execution_definition_version(self.execution.pk)).value
        self.assertTrue({"execution_host_unavailable", "execution_host_evidence_missing"} <= {row["code"] for row in result["blockers"]})
        self.assertEqual(result["runtime_host_id"], str(host.pk))
