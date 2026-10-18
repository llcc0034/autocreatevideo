"""Actual frozen preparation sources and API guards; no remote generation."""
from copy import deepcopy
from unittest.mock import patch
from uuid import uuid4

from django.core.exceptions import ValidationError
from django.db import connection
from django.test import TestCase, override_settings
from django.test.utils import CaptureQueriesContext
from rest_framework.test import APIClient

from studio.models import (Asset, AssetGenerationJob, ExecutionPreparationContract, GenerationAttempt,
    GenerationTask, ModelDeploymentSmokeAttempt, ProductionRun, StageRun, WorkflowSmokeAttempt)
from studio.services.execution_asset_references import active_asset_preparations, asset_has_active_execution_reference
from studio.services.execution_instances import execution_configuration_max_bytes, execution_resolved_inputs_max_bytes
from studio.services.execution_preparations import ResolvedExecutionPreparation, create_execution_instance_preparation
from studio.services.json_contracts import json_contract
from studio.tests import test_execution_production_workflow_requests as workflow_fixtures
from studio.tests import test_execution_preparation_creation as preparation_fixtures
from studio.tests.test_asset_management_api import image_upload


@override_settings(AI_PROVIDER_ALLOWED_HOSTS=("127.0.0.1",))
class ExecutionAssetReferenceTests(TestCase):
    _create_version = workflow_fixtures.ProductionWorkflowRequestTests._create_version
    resolve = workflow_fixtures.ProductionWorkflowRequestTests.resolve
    setUp = workflow_fixtures.ProductionWorkflowRequestTests.setUp

    def end_production(self):
        ProductionRun.objects.filter(pk=self.run.pk).update(status="cancelled")
        StageRun.objects.filter(pk=self.stage.pk).update(status="cancelled")
        GenerationAttempt.objects.filter(pk=self.attempt.pk).update(status="cancelled")
        GenerationTask.objects.filter(pk=self.attempt.generation_task_id).update(
            status="cancelled", submission_token="", submission_state="terminal_cancelled")

    def prepare_owner(self, key, owner):
        detail = self.preparation.contract
        return create_execution_instance_preparation(owner_ref=(key, owner.pk), version_id=self.version.pk,
            expected_definition_sha256=self.definition.sha256, unit_key="reference-guard", request_key=str(uuid4()),
            intent=self.intent, actor=self.user, resolve_facts=lambda *_: ResolvedExecutionPreparation(
                json_contract(deepcopy(detail.resolved_inputs)), json_contract(deepcopy(detail.review_contract))))

    def test_source_asset_delete_and_category_change_use_preparation_not_removed_workflow_fields(self):
        client = APIClient()
        client.force_authenticate(self.user)
        self.assertTrue(asset_has_active_execution_reference(self.asset))
        for method, data in ((client.delete, {}), (client.patch, {
            "kind": Asset.Kind.SCENE, "description": "场景基于这张明确参考图", "prompt_hint": "保持原图空间结构",
        })):
            response = method(f"/api/v1/assets/{self.asset.pk}/", data, format="json")
            self.assertEqual(response.status_code, 400, response.data)
            self.assertIn("执行准备", str(response.data))
        self.assertTrue(Asset.objects.filter(pk=self.asset.pk).exists())

    def test_cancelled_parent_does_not_release_live_or_unresolved_remote_call(self):
        self.end_production()
        self.assertFalse(asset_has_active_execution_reference(self.asset))
        for state in ("queued", "running", "cancel_requested"):
            GenerationTask.objects.filter(pk=self.attempt.generation_task_id).update(status=state)
            self.assertTrue(asset_has_active_execution_reference(self.asset), state)
        GenerationTask.objects.filter(pk=self.attempt.generation_task_id).update(status="cancelled")
        for state in ("queued", "running", "submitting", "submitted", "cancel_requested"):
            GenerationAttempt.objects.filter(pk=self.attempt.pk).update(status=state)
            self.assertTrue(asset_has_active_execution_reference(self.asset), state)
        GenerationAttempt.objects.filter(pk=self.attempt.pk).update(status="failed")
        GenerationTask.objects.filter(pk=self.attempt.generation_task_id).update(status="failed",
            submission_token="original-submission", submission_state="submitted", remote_state="missing")
        self.assertTrue(asset_has_active_execution_reference(self.asset))
        GenerationTask.objects.filter(pk=self.attempt.generation_task_id).update(submission_state="terminal_failed")
        self.assertFalse(asset_has_active_execution_reference(self.asset))

    def test_asset_and_workflow_smoke_roots_include_preparing_and_uncertain_until_terminal(self):
        self.end_production()
        asset_job = AssetGenerationJob.objects.create(owner=self.user, model_deployment=self.deployment,
            execution_instance_version=self.version, workflow_template_version=self.workflow,
            execution_kind="comfy_workflow", job_type="asset", media_type="video", prompt="",
            operation=self.version.operation, idempotency_key=str(uuid4()), request_snapshot_sha256="a" * 64)
        workflow_smoke = WorkflowSmokeAttempt.objects.create(deployment=self.deployment, workflow_version=self.workflow, created_by=self.user,
            execution_instance_version=self.version, scope="instance", status="preparing",
            graph_sha256="a" * 64, target_sha256="b" * 64, execution_contract_sha256="c" * 64,
            validation_evidence_sha256="d" * 64)
        for key, root in (("asset_job_id", asset_job), ("workflow_smoke_id", workflow_smoke)):
            self.prepare_owner(key, root)
            for state in ("preparing", "cancel_requested", "uncertain", "succeeded", "failed", "cancelled"):
                type(root).objects.filter(pk=root.pk).update(status=state)
                self.assertEqual(asset_has_active_execution_reference(self.asset), state not in {"succeeded", "failed", "cancelled"}, (key, state))

    def test_corrupt_source_is_explicit_failure_not_empty_reference_and_query_avoids_task_payloads(self):
        # This guard checks the persisted input envelope, not remote services or
        # duplicated staging/prompt JSON. Run SQL through the actual SQLite DB.
        with CaptureQueriesContext(connection) as captured:
            self.assertTrue(asset_has_active_execution_reference(self.asset))
        source_queries = [row["sql"] for row in captured if 'FROM "studio_executionpreparation"' in row["sql"]]
        self.assertEqual(len(source_queries), 1)
        for removed in ("generated_workflow", "capability_snapshot", '"staged_inputs"', '"prepared_request"'):
            self.assertNotIn(removed, source_queries[0])
        self.assertIn("INDEX", active_asset_preparations().explain().upper())
        ExecutionPreparationContract._base_manager.filter(preparation=self.preparation).update(resolved_inputs_sha256="0" * 64)
        client = APIClient()
        client.force_authenticate(self.user)
        response = client.delete(f"/api/v1/assets/{self.asset.pk}/")
        self.assertEqual(response.status_code, 400, response.data)
        self.assertIn("来源无法核对", str(response.data))
        self.assertTrue(Asset.objects.filter(pk=self.asset.pk).exists())


@override_settings(AI_PROVIDER_ALLOWED_HOSTS=("api.example.com", "cdn.example.com"))
class DeploymentPreparationReferenceTests(TestCase):
    setUpTestData = classmethod(preparation_fixtures.ExecutionPreparationCreationTests.setUpTestData.__func__)
    write_contract = staticmethod(preparation_fixtures.ExecutionPreparationCreationTests.write_contract)
    setUp = preparation_fixtures.ExecutionPreparationCreationTests.setUp
    job_row = preparation_fixtures.ExecutionPreparationCreationTests.job_row
    resolve = preparation_fixtures.ExecutionPreparationCreationTests.resolve
    create = preparation_fixtures.ExecutionPreparationCreationTests.create

    def prepare_large_smoke(self):
        self.intent = json_contract({**self.intent.value,
            "subject_facts": [{"id": "11111111-1111-4111-8111-111111111111", "category": "character",
                "name": "Declared source", "description": "Complete source. " * 4500}]},
            max_bytes=execution_resolved_inputs_max_bytes())
        smoke = ModelDeploymentSmokeAttempt.objects.create(deployment=self.deployment, scope="instance",
            execution_instance_version=self.version, operation=self.version.operation, status="preparing",
            request_sha256="a" * 64, target_sha256="b" * 64)
        original = self.resolve(self.definition, smoke)
        payload = deepcopy(original.resolved_inputs.value)
        payload["profile_sources"] = {"context": "界" * 24000}
        resolved = json_contract(payload, max_bytes=execution_resolved_inputs_max_bytes())
        self.assertGreater(resolved.size_bytes, execution_configuration_max_bytes())
        return self.create(owner_ref=("deployment_smoke_id", smoke.pk),
            resolve_facts=lambda *_: ResolvedExecutionPreparation(resolved, original.review_contract))

    def test_large_unrelated_smoke_allows_asset_category_changes_and_delete(self):
        preparation = self.prepare_large_smoke()
        self.assertTrue(active_asset_preparations().filter(pk=preparation.pk).exists())
        from studio.services.production_manual_change_impact import _references
        with patch("studio.services.production_manual_change_impact.recorded_input_references", return_value=[]) as reader:
            self.assertEqual(_references(preparation, preparation.contract,
                {"stage_run_id": None, "script_revision_id": None}), [])
        reader.assert_called_once()
        client = APIClient()
        client.force_authenticate(self.actor)
        uploaded = client.post("/api/v1/assets/", {"kind": Asset.Kind.REFERENCE, "name": "Independent image",
            "description": "Uploaded portrait", "prompt_hint": "Keep appearance", "file": image_upload()}, format="multipart")
        self.assertEqual(uploaded.status_code, 201, uploaded.data)
        asset = Asset.objects.get(pk=uploaded.data["id"])
        for kind in (Asset.Kind.CHARACTER, Asset.Kind.REFERENCE):
            response = client.patch(f"/api/v1/assets/{asset.pk}/", {"kind": kind}, format="json")
            self.assertEqual(response.status_code, 200, response.data)
            asset.refresh_from_db()
            self.assertEqual(asset.kind, kind)
        response = client.delete(f"/api/v1/assets/{asset.pk}/")
        self.assertEqual(response.status_code, 204, response.data)
        self.assertFalse(Asset.objects.filter(pk=asset.pk).exists())

    def test_large_source_still_enforces_runtime_limit_and_digest(self):
        preparation = self.prepare_large_smoke()
        asset = Asset.objects.create(owner=self.actor, kind=Asset.Kind.REFERENCE, name="Unrelated image")
        self.assertFalse(asset_has_active_execution_reference(asset))
        with patch("studio.services.execution_request_storage.boundary_int", return_value=65536):
            with self.assertRaises(ValidationError) as caught:
                asset_has_active_execution_reference(asset)
        self.assertEqual(caught.exception.code, "execution_asset_reference_invalid")
        self.assertIn("65536", str(caught.exception.__cause__))
        ExecutionPreparationContract._base_manager.filter(preparation=preparation).update(resolved_inputs_sha256="0" * 64)
        with self.assertRaises(ValidationError) as caught:
            asset_has_active_execution_reference(asset)
        self.assertEqual(caught.exception.code, "execution_asset_reference_invalid")

    def test_native_deployment_smoke_is_considered_but_literal_id_is_not_a_media_reference(self):
        asset = Asset.objects.create(owner=self.actor, kind="reference", name="Unrelated source")
        self.intent = json_contract({**self.intent.value,
            "manual_channels": {"positive_prompt": '{"asset_id": "' + str(asset.pk) + '"}'}})
        smoke = ModelDeploymentSmokeAttempt.objects.create(deployment=self.deployment, scope="instance",
            execution_instance_version=self.version, operation=self.version.operation, status="preparing",
            request_sha256="a" * 64, target_sha256="b" * 64)
        preparation = self.create(owner_ref=("deployment_smoke_id", smoke.pk))
        for state in ("preparing", "cancel_requested", "uncertain", "succeeded", "failed", "cancelled"):
            ModelDeploymentSmokeAttempt.objects.filter(pk=smoke.pk).update(status=state)
            self.assertEqual(active_asset_preparations().filter(pk=preparation.pk).exists(), state not in {"succeeded", "failed", "cancelled"})
            self.assertFalse(asset_has_active_execution_reference(asset))
