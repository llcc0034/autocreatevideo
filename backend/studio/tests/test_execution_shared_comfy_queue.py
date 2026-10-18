"""Original task, asset and Smoke queues arbitrate one actual instance target.

Every root has its own real frozen Preparation. Admission observations are
isolated; no upload, provider submission or GPU execution is performed here.
"""
from unittest.mock import patch

from django.test import TestCase, override_settings
from django.core.exceptions import ValidationError

from studio.models import AssetGenerationJob, GenerationTask
from studio.services.asset_generation import create_asset_generation_job, claim_next_comfy_asset_job
from studio.services.execution_asset_dispatch import dispatch_asset_execution
from studio.services.execution_advancement import advance_execution_preparation
from studio.services.execution_smoke import create_instance_smoke
from studio.services.execution_definitions import build_execution_definition
from studio.services.execution_instances import clone_execution_draft, update_execution_draft, publish_execution_version
from studio.services.json_contracts import json_contract
from studio.services.task_queue import claim_next_task
from studio.services.workflow_smoke_queue import claim_next_workflow_smoke
from studio.services.workflow_smoke_resolution import resolve_workflow_smoke_attempt
from studio.services.workflow_versions import import_workflow_version, workflow_version_execution_snapshot
from studio.tests.test_execution_asset_requests import freeze, isolated_observations
from studio.tests import test_execution_production_workflow_requests as fixtures


@override_settings(AI_PROVIDER_ALLOWED_HOSTS=("127.0.0.1",))
class ExecutionSharedComfyQueueTests(TestCase):
    _create_version = fixtures.ProductionWorkflowRequestTests._create_version
    resolve = fixtures.ProductionWorkflowRequestTests.resolve

    def setUp(self):
        fixtures.ProductionWorkflowRequestTests.setUp(self)
        self.task = self.attempt.generation_task
        self.assertEqual(self.task.status, "queued")
        self.enterContext(patch("studio.services.execution_asset_dispatch.collect_asset_execution_admission",
            side_effect=isolated_observations))
        self.enterContext(patch("studio.services.execution_smoke_admission.read_execution_resource_state",
            return_value=json_contract({"schema_version": 1, "blockers": [], "test_scope": "isolated queue resource facts",
                "capacity_runtime": {"kind": "unknown", "host_id": None, "fingerprint": None}})))
        self.job, _ = create_asset_generation_job(owner=self.user, execution_instance_version_id=self.version.pk,
            expected_definition_sha256=self.definition.sha256, job_type="asset", media_type="video",
            name="Shared queue fixture", **self.intent.value, field_values=[], allow_external_calls=False, budget_limit=None,
            client_idempotency_key="shared-asset")
        self.asset_preparation = freeze(self.job, self.user)
        self.job = dispatch_asset_execution(preparation_id=self.asset_preparation.pk,
            expected_preparation_revision=self.asset_preparation.revision, actor=self.user)
        self.assertEqual(self.job.status, "queued")

    def smoke(self, key, *, version=None, definition=None):
        smoke, preparation, _ = create_instance_smoke(version_id=(version or self.version).pk,
            expected_definition_sha256=(definition or self.definition).sha256, fixture=self.intent.value, field_values=[],
            client_idempotency_key=key, allow_external_calls=False, budget_limit=None, actor=self.user)
        advanced = advance_execution_preparation(preparation_id=preparation.pk,
            expected_preparation_revision=preparation.revision, actor=self.user)
        self.assertEqual(advanced.status, "dispatched", advanced.reason)
        smoke.refresh_from_db()
        self.assertEqual(smoke.status, "queued")
        return smoke

    def resolve_not_submitted(self, smoke):
        return resolve_workflow_smoke_attempt(smoke, outcome="confirmed_not_submitted", provider_request_id="",
            note="隔离队列测试明确核对对端未创建请求。", user=self.user)

    def test_claimed_asset_blocks_task_and_smoke(self):
        asset_claim = claim_next_comfy_asset_job("asset-resource-owner")
        self.assertEqual(asset_claim.pk, self.job.pk)
        self.smoke("smoke-after-asset")
        self.assertIsNone(claim_next_workflow_smoke("waiting-smoke"))
        self.assertIsNone(claim_next_task())
        self.task.refresh_from_db()
        self.assertEqual(self.task.status, "queued")

    def test_running_production_task_blocks_asset_and_smoke(self):
        claimed = claim_next_task()
        self.assertEqual(claimed.pk, self.task.pk)
        self.smoke("smoke-after-task")
        self.assertIsNone(claim_next_comfy_asset_job("waiting-asset"))
        self.assertIsNone(claim_next_workflow_smoke("waiting-smoke"))

    def test_claimed_smoke_blocks_asset_and_production_task(self):
        smoke = self.smoke("smoke-resource-owner")
        claimed = claim_next_workflow_smoke("smoke-owner")
        self.assertEqual(claimed.pk, smoke.pk)
        self.assertIsNone(claim_next_comfy_asset_job("waiting-asset"))
        self.assertIsNone(claim_next_task())

    def test_uncertain_smoke_blocks_asset_until_explicit_resolution(self):
        smoke = self.smoke("unresolved-smoke")
        smoke.status = "uncertain"
        smoke.save(update_fields=["status"])
        self.assertIsNone(claim_next_comfy_asset_job("waiting-asset"))
        self.job.refresh_from_db()
        self.assertEqual(self.job.status, "queued")
        self.resolve_not_submitted(smoke)
        self.assertEqual(claim_next_comfy_asset_job("released-asset").pk, self.job.pk)

    def test_uncertain_smoke_blocks_other_smoke_until_explicit_resolution(self):
        first = self.smoke("first-smoke")
        first.status = "uncertain"
        first.save(update_fields=["status"])
        with self.assertRaisesMessage(ValidationError, "远端状态未确定"):
            self.smoke("duplicate-resource-smoke")
        snapshot = workflow_version_execution_snapshot(self.workflow)
        workflow = import_workflow_version(template=self.workflow.template, graph=snapshot["graph"],
            input_contract=snapshot["input_contract"], semantic_bindings=snapshot["semantic_bindings"],
            model_requirements=snapshot["model_requirements"], provider=self.provider, model_profile_version=self.profile)
        # Explicit synthetic published dependency, not claimed GPU evidence.
        workflow.status, workflow.validated_deployment = "published", self.deployment
        workflow.save()
        version = clone_execution_draft(source_id=self.version.pk, expected_revision=self.version.revision,
            expected_definition_sha256=self.definition.sha256, actor=self.user)
        version = update_execution_draft(version_id=version.pk, expected_revision=version.revision,
            changes={"workflow_version": workflow}, actor=self.user)
        definition = build_execution_definition(version.pk)
        version = publish_execution_version(version_id=version.pk, expected_revision=version.revision,
            expected_definition_sha256=definition.sha256, actor=self.user)
        second = self.smoke("second-smoke", version=version, definition=definition)
        self.assertIsNone(claim_next_workflow_smoke("waiting-smoke"))
        self.resolve_not_submitted(first)
        self.assertEqual(claim_next_workflow_smoke("released-smoke").pk, second.pk)
        self.assertEqual(AssetGenerationJob.objects.get(pk=self.job.pk).status, "queued")
        self.assertEqual(GenerationTask.objects.get(pk=self.task.pk).status, "queued")
