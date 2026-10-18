"""Real resource -> combination Smoke -> production admission with isolated peers.

Only external transport/output inspection is controlled. Resource facts are
explicit test-database observations, not certification of the running system.
"""
from unittest.mock import patch
from uuid import uuid4

from django.test import TestCase, override_settings
from rest_framework.test import APIClient

from studio.models import GenerationAttempt, ModelExecutionInstanceVersion, ProductionRun, SeriesProject, StageRun
from studio.services.configuration import resolve_configuration
from studio.services.execution_advancement import advance_execution_preparation
from studio.services.execution_definitions import build_execution_definition
from studio.services.execution_instances import publish_execution_version
from studio.services.execution_preparations import create_execution_instance_preparation
from studio.services.execution_request_preparation import resolve_execution_request_facts
from studio.services.execution_smoke import create_instance_smoke
from studio.services.json_contracts import assign_json_contract, json_contract
from studio.services.production_run_plans import freeze_run_execution_plan
from studio.services.production_stage_catalog import production_stage_catalog
from studio.services.security_boundaries import boundary_int
from studio.services.workflow_versions import publish_workflow_version
from studio.tests import test_execution_resource_smoke_media as resource_fixtures
from studio.tests.test_execution_context_inputs import PROJECT_CONFIG


@override_settings(AI_PROVIDER_ALLOWED_HOSTS=("127.0.0.1",))
class WorkflowAdmissionIntegrationTests(TestCase):
    _create_version = resource_fixtures.ResourceComfyWorkerTests._create_version
    workflow_fixtures = resource_fixtures.ResourceComfyWorkerTests.workflow_fixtures
    resource_fixture = resource_fixtures.ResourceComfyWorkerTests.resource_fixture
    validate_resource_workflow = resource_fixtures.ResourceComfyWorkerTests.validate_resource_workflow
    cycle = resource_fixtures.ResourceComfyWorkerTests.cycle
    submit = resource_fixtures.ResourceComfyWorkerTests.submit

    def finish_smoke(self):
        self.peer.invoke.side_effect = None
        self.submit()
        self.peer.invoke.side_effect = [{"status": "succeeded"}, {"outputs": []}]
        with patch("studio.services.execution_workflow_smoke_worker.verify_comfy_primary_output",
                return_value=(None, {"kind": "video", "sha256": "a" * 64})):
            done = self.cycle()
        self.assertEqual(done.status, "succeeded", done.error_code)
        return done

    def setUp(self):
        resource_fixtures.ResourceComfyWorkerTests.setUp(self)
        self.resource_attempt = self.finish_smoke()
        self.workflow.refresh_from_db()
        self.workflow = publish_workflow_version(self.workflow, published_by=self.user)
        self.workflow.template.is_active = True
        self.workflow.template.save(update_fields=("is_active",))
        self.version = ModelExecutionInstanceVersion.objects.get(workflow_version=self.workflow)
        self.definition = build_execution_definition(self.version.pk)
        self.version = publish_execution_version(version_id=self.version.pk, expected_revision=self.version.revision,
            expected_definition_sha256=self.definition.sha256, actor=self.user)
        self.definition = build_execution_definition(self.version.pk)
        self.fixture = self.resource_fixture(self.definition)
        self.attempt, self.prep, _ = create_instance_smoke(version_id=self.version.pk,
            expected_definition_sha256=self.definition.sha256, fixture=self.fixture, field_values=[],
            client_idempotency_key="isolated-combination", allow_external_calls=False, budget_limit=None, actor=self.user)
        self.prep = advance_execution_preparation(preparation_id=self.prep.pk,
            expected_preparation_revision=self.prep.revision, actor=self.user)
        self.assertEqual(self.prep.status, "dispatched", self.prep.reason)
        self.upload.uploads.clear()
        self.combination_attempt = self.finish_smoke()
        self.project = SeriesProject.objects.create(owner=self.user, title="Isolated admission", **PROJECT_CONFIG)
        self.run = ProductionRun.objects.create(owner=self.user, project=self.project, plan_source="manual",
            status="running", allow_external_calls=False,
            idempotency_key=str(uuid4()), request_sha256="a" * 64)
        assign_json_contract(self.run, "config_snapshot", resolve_configuration(run=self.run).snapshot(),
            max_bytes=boundary_int("security.snapshot.stage_max_bytes"))
        self.run.save()
        catalog = production_stage_catalog()
        freeze_run_execution_plan(run=self.run, scope={"kind": "project"}, delivery={"kind": "episode_masters"},
            initial_content=None, stage_catalog_key=catalog.value["key"],
            expected_stage_catalog_sha256=catalog.sha256, actor=self.user)
        media = next(row for row in catalog.value["stages"] if row["key"] == "media_generation")
        self.stage = StageRun.objects.create(production_run=self.run, stage_key=media["key"], ordinal=media["ordinal"],
            mode="manual", status="running", idempotency_key=str(uuid4()))
        self.preparation = create_execution_instance_preparation(owner_ref=("stage_run_id", self.stage.pk),
            version_id=self.version.pk, expected_definition_sha256=self.definition.sha256,
            unit_key="integration-unit", request_key="integration-call", intent=json_contract(self.fixture),
            resolve_facts=self.resolve, actor=self.user)
        self.client = APIClient()
        self.client.force_authenticate(self.user)

    def resolve(self, definition, stage):
        return resolve_execution_request_facts(definition, self.fixture, owner=self.user, unit_key="integration-unit",
            max_media_bytes=65536, parameter_policies=[], resolve_parameter_source=lambda _: self.fail("No implicit source"),
            review_contract=json_contract({"schema_version": 1, "mode": "manual"}), project_id=self.project.pk,
            production_run_id=str(self.run.pk), production_scope={"kind": "project", "id": str(self.project.pk)})

    def continue_api(self):
        self.preparation.refresh_from_db()
        result = self.client.post(f"/api/v1/execution-preparations/{self.preparation.pk}/continue/",
            {"expected_preparation_revision": self.preparation.revision}, format="json")
        self.assertEqual(result.status_code, 200, result.data)
        self.preparation.refresh_from_db()
        return result

    def test_combination_evidence_allows_formal_continue_and_real_submission_gate(self):
        self.continue_api()
        self.assertEqual(self.preparation.status, "dispatched", self.preparation.reason)
        attempt = GenerationAttempt.objects.get(preparation=self.preparation)
        self.assertIsNotNone(attempt.generation_task_id)
        from studio.services.execution_production_dispatch import current_production_gate
        _, _, decision, _ = current_production_gate(self.preparation)
        self.assertTrue(decision["can_submit"], decision)
        # A response replay must not allocate a second Attempt or reserve twice.
        self.run.refresh_from_db()
        before = self.run.generation_task_count
        self.continue_api()
        self.assertEqual(GenerationAttempt.objects.filter(preparation=self.preparation).count(), 1)
        self.run.refresh_from_db()
        self.assertEqual(self.run.generation_task_count, before)
        from studio.services.task_queue import claim_next_task, prepare_workflow
        from studio.services.execution_production_workflow_queue import begin_prepared_generation_submission
        task = claim_next_task()
        self.assertIsNotNone(task)
        self.assertEqual(task.pk, attempt.generation_task_id)
        prepare_workflow(task, self.upload)
        self.assertTrue(begin_prepared_generation_submission(task))
        self.assertEqual(task.submission_token, str(attempt.pk))
        self.assertNotEqual(task.submission_token, str(self.preparation.pk))

    def test_stopped_host_blocks_before_original_task_allocation(self):
        host = self.deployment.runtime_host
        host.status = "inactive"
        host.save(update_fields=("status", "updated_at"))
        self.continue_api()
        self.assertEqual(self.preparation.status, "blocked")
        self.assertFalse(GenerationAttempt.objects.filter(preparation=self.preparation).exists())
