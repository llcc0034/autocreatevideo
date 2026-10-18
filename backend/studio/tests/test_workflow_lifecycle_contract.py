"""Published workflow lifecycle through real services, with isolated peers.

The original resource/combination evidence is produced in the test database.
External validation, transport and media probes are controlled fixtures; these
tests do not certify any current deployment or execute a real model.
"""
from copy import deepcopy
from unittest.mock import patch

from django.core.exceptions import ValidationError
from django.test import TestCase, override_settings

from studio.models import GenerationAttempt, WorkflowTemplateVersion
from studio.services.workflow_versions import (
    _binding_rows,
    _requirement_rows,
    import_workflow_version,
    publish_workflow_version,
    verify_published_workflow_capability,
    workflow_version_execution_snapshot,
)
from studio.services.json_contracts import assign_json_contract
from studio.tests import test_execution_workflow_admission_integration as fixtures


@override_settings(AI_PROVIDER_ALLOWED_HOSTS=("127.0.0.1",))
class WorkflowLifecycleContractTests(TestCase):
    _create_version = fixtures.WorkflowAdmissionIntegrationTests._create_version
    workflow_fixtures = fixtures.WorkflowAdmissionIntegrationTests.workflow_fixtures
    resource_fixture = fixtures.WorkflowAdmissionIntegrationTests.resource_fixture
    validate_resource_workflow = fixtures.WorkflowAdmissionIntegrationTests.validate_resource_workflow
    cycle = fixtures.WorkflowAdmissionIntegrationTests.cycle
    submit = fixtures.WorkflowAdmissionIntegrationTests.submit
    finish_smoke = fixtures.WorkflowAdmissionIntegrationTests.finish_smoke
    resolve = fixtures.WorkflowAdmissionIntegrationTests.resolve
    continue_api = fixtures.WorkflowAdmissionIntegrationTests.continue_api

    def setUp(self):
        for target in ("requests.sessions.Session.request", "socket.create_connection"):
            self.enterContext(patch(target, side_effect=AssertionError("No real workflow I/O")))
        fixtures.WorkflowAdmissionIntegrationTests.setUp(self)

    def import_arguments(self):
        return {
            "template": self.workflow.template,
            "graph": deepcopy(self.workflow.graph),
            "input_contract": deepcopy(self.workflow.input_contract),
            "semantic_bindings": _binding_rows(self.workflow),
            "model_requirements": _requirement_rows(self.workflow),
            "provider": self.workflow.provider,
            "model_profile_version": self.workflow.model_profile_version,
        }

    def published_facts(self):
        self.workflow.refresh_from_db()
        return deepcopy({
            "snapshot": workflow_version_execution_snapshot(self.workflow),
            "status": self.workflow.status,
            "is_current": self.workflow.is_current,
            "updated_at": self.workflow.updated_at,
            "validated_deployment_id": self.workflow.validated_deployment_id,
            "validation_state": self.workflow.validation_state,
            "smoke_test_state": self.workflow.smoke_test_state,
            "validation": self.workflow.validation_evidence,
            "smoke": self.workflow.smoke_test_evidence,
            "validation_sha256": self.workflow.validation_evidence_sha256,
            "smoke_sha256": self.workflow.smoke_test_evidence_sha256,
        })

    def test_rebuild_creates_draft_without_inheriting_published_acceptance(self):
        original = self.published_facts()
        successor = import_workflow_version(**self.import_arguments())
        self.assertNotEqual(successor.pk, self.workflow.pk)
        self.assertEqual(successor.version, self.workflow.version + 1)
        self.assertEqual(successor.status, "draft")
        self.assertFalse(successor.is_current)
        self.assertEqual(successor.validation_state, "not_run")
        self.assertEqual(successor.smoke_test_state, "not_run")
        self.assertEqual(successor.validation_evidence, {})
        self.assertEqual(successor.smoke_test_evidence, {})
        self.assertIsNone(successor.validated_deployment_id)
        denied = self.client.post(
            f"/api/v1/workflow-template-versions/{successor.pk}/publish/", {}, format="json",
        )
        self.assertEqual(denied.status_code, 400, denied.data)
        self.assertEqual(self.published_facts(), original)
        evidence, _ = verify_published_workflow_capability(
            self.workflow, self.deployment, operation="video.reference_to_video",
        )
        self.assertEqual(evidence.pk, self.resource_attempt.pk)
        with self.assertRaises(ValidationError):
            verify_published_workflow_capability(
                successor, self.deployment, operation="video.reference_to_video",
            )

    def test_reprojection_errors_preserve_original_contract_and_preparation(self):
        original = self.published_facts()
        original_inputs = deepcopy(self.preparation.contract.resolved_inputs)
        count = WorkflowTemplateVersion.objects.count()
        for broken in ("port", "projector"):
            arguments = self.import_arguments()
            if broken == "port":
                self.assertTrue(arguments["semantic_bindings"])
                arguments["semantic_bindings"][0]["node_id"] = "absent-reprojection-node"
            else:
                arguments["input_contract"].pop("contract_projector")
            with self.subTest(broken=broken), self.assertRaises(ValidationError):
                import_workflow_version(**arguments)
            self.assertEqual(WorkflowTemplateVersion.objects.count(), count)
            self.assertEqual(self.published_facts(), original)
        self.preparation.refresh_from_db()
        self.assertEqual(self.preparation.contract.resolved_inputs, original_inputs)
        self.continue_api()
        self.assertEqual(self.preparation.status, "dispatched", self.preparation.reason)
        attempt = GenerationAttempt.objects.get(preparation=self.preparation)
        self.assertIsNotNone(attempt.generation_task_id)
        self.assertEqual(self.published_facts(), original)

    def test_dependency_drift_rejects_original_evidence_without_rewriting_history(self):
        original = self.published_facts()
        original_inputs = deepcopy(self.preparation.contract.resolved_inputs)
        runtime = deepcopy(self.deployment.runtime_config)
        runtime["base_url"] = "http://127.0.0.1:8199"
        assign_json_contract(self.deployment, "runtime_config", runtime)
        self.deployment.save()
        with self.assertRaises(ValidationError) as rejected:
            verify_published_workflow_capability(
                self.workflow, self.deployment, operation="video.reference_to_video",
            )
        self.assertEqual(rejected.exception.code, "workflow_target_drifted")
        self.peer.invoke.reset_mock()
        self.continue_api()
        self.assertEqual(self.preparation.status, "blocked", self.preparation.reason)
        self.assertFalse(GenerationAttempt.objects.filter(preparation=self.preparation).exists())
        self.peer.invoke.assert_not_called()
        self.assertEqual(self.preparation.contract.resolved_inputs, original_inputs)
        self.assertEqual(self.published_facts(), original)

    def test_republish_after_dependency_drift_does_not_demote_published_version(self):
        original = self.published_facts()
        runtime = deepcopy(self.deployment.runtime_config)
        runtime["base_url"] = "http://127.0.0.1:8199"
        assign_json_contract(self.deployment, "runtime_config", runtime)
        self.deployment.save()
        denied = self.client.post(
            f"/api/v1/workflow-template-versions/{self.workflow.pk}/publish/", {}, format="json",
        )
        self.assertEqual(denied.status_code, 400, denied.data)
        self.assertEqual(self.published_facts(), original)
        # The HTTP transaction must not be the only immutability boundary:
        # callers of the shared publication service need the same guarantee.
        with self.assertRaises(ValidationError):
            publish_workflow_version(self.workflow, published_by=self.user)
        self.assertEqual(self.published_facts(), original)
