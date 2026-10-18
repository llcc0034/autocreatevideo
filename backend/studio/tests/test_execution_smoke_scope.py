"""Scope identities and original queue boundaries, without any provider call."""
from uuid import uuid4

from django.core.exceptions import ValidationError
from django.db import IntegrityError, transaction
from django.test import TestCase

from studio.models import (ExecutionPreparation, ModelDeploymentSmokeAttempt, ModelDeployment, ModelExecutionInstanceVersion,
    WorkflowSmokeAttempt, WorkflowTemplate, WorkflowTemplateVersion)
from studio.services.deployment_smoke_queue import claim_next_deployment_smoke, request_deployment_smoke_cancel
from studio.services.workflow_smoke_queue import claim_next_workflow_smoke, request_workflow_smoke_cancel
from studio.services.execution_deployment_resource_evidence import project_completed_deployment_smoke
from studio.services.execution_workflow_resource_evidence import project_completed_workflow_smoke
from studio.services.execution_task_links import validate_smoke_preparation_link
from studio.tests.test_execution_preparation_storage import ExecutionPreparationStorageTests


class ExecutionSmokeScopeTests(TestCase):
    setUpTestData = classmethod(ExecutionPreparationStorageTests.setUpTestData.__func__)

    def setUp(self):
        template = WorkflowTemplate.objects.create(slug="scope-test", name="Scope fixture", output_kind="video")
        self.workflow = WorkflowTemplateVersion.objects.create(template=template, version=1)
        self.workflow_instance = ModelExecutionInstanceVersion.objects.create(instance=self.instance, version=2,
            deployment=self.deployment, workflow_version=self.workflow, operation="video.generate", prompt_mode="none")

    def attempt(self, kind, **changes):
        if kind == "workflow":
            model = WorkflowSmokeAttempt
            fields = {"workflow_version": self.workflow, "created_by": self.actor,
                "execution_instance_version": self.workflow_instance, "graph_sha256": "a" * 64,
                "execution_contract_sha256": "b" * 64, "validation_evidence_sha256": "c" * 64}
        else:
            model = ModelDeploymentSmokeAttempt
            fields = {"operation": self.version.operation, "execution_instance_version": self.version, "request_sha256": "d" * 64}
        return model.objects.create(**{"deployment": self.deployment, "scope": "instance", "status": "preparing", "idempotency_key": str(uuid4()),
            "target_sha256": "e" * 64, **fields, **changes})

    def test_scope_and_instance_fk_must_agree_in_database(self):
        for kind in ("deployment", "workflow"):
            for changes in ({"scope": "resource"}, {"execution_instance_version": None}, {"scope": "caller_bypass"}):
                with self.subTest(kind=kind, changes=changes), self.assertRaises(IntegrityError), transaction.atomic():
                    self.attempt(kind, **changes)

    def test_preparing_has_no_remote_clock_and_reserves_original_active_slot(self):
        for kind in ("deployment", "workflow"):
            attempt = self.attempt(kind)
            for key in ("submission_started_at", "deadline_started_at", "hard_deadline_at", "idle_deadline_at"):
                self.assertIsNone(getattr(attempt, key))
            with self.subTest(kind=kind), self.assertRaises(IntegrityError), transaction.atomic():
                self.attempt(kind, scope="resource", execution_instance_version=None, status="queued")
        self.assertIsNone(claim_next_deployment_smoke("scope-test"))
        self.assertIsNone(claim_next_workflow_smoke("scope-test"))

    def test_resource_or_mismatched_target_cannot_create_instance_preparation(self):
        other = ModelDeployment.objects.create(key="other-scope-target", name="Other", profile_version=self.model)
        for kind, version in (("deployment", self.version), ("workflow", self.workflow_instance)):
            attempt = self.attempt(kind, status="failed")
            validate_smoke_preparation_link(attempt, version.pk)
            for changes in ({"scope": "resource"}, {"deployment_id": other.pk}, {"execution_instance_version_id": uuid4()}):
                row = type(attempt).objects.get(pk=attempt.pk)
                for key, value in changes.items():
                    setattr(row, key, value)
                with self.subTest(kind=kind, changes=changes), self.assertRaises(ValidationError):
                    validate_smoke_preparation_link(row, version.pk)
        attempt = self.attempt("deployment", operation="image.generate", status="failed")
        with self.assertRaises(ValidationError):
            validate_smoke_preparation_link(attempt, self.version.pk)
        attempt = self.attempt("deployment", execution_instance_version=self.workflow_instance, operation=self.workflow_instance.operation, status="failed")
        with self.assertRaises(ValidationError):
            validate_smoke_preparation_link(attempt, self.workflow_instance.pk)

    def test_combination_cannot_write_base_resource_evidence(self):
        claim = uuid4()
        attempt = self.attempt("deployment", status="submitting", claim_token=claim)
        project_completed_deployment_smoke(attempt, self.deployment)
        attempt.refresh_from_db()
        self.assertIsNone(attempt.final_snapshot_id)
        self.assertEqual(attempt.status, "submitting")
        workflow = self.attempt("workflow", status="running", claim_token=claim)
        project_completed_workflow_smoke(workflow, self.workflow)
        self.workflow.refresh_from_db()
        self.assertNotEqual(self.workflow.smoke_test_state, "passed")

    def test_workflow_overview_only_selects_resource_attempts(self):
        from studio.platform_views import AcceptanceSmokeView
        WorkflowTemplateVersion.objects.filter(pk=self.workflow.pk).update(validated_deployment=self.deployment)
        base = self.attempt("workflow", scope="resource", execution_instance_version=None, status="failed")
        newer_combination = self.attempt("workflow", status="failed")
        query = AcceptanceSmokeView._workflow_queryset(selected_deployment=self.deployment)
        version = query.get(pk=self.workflow.pk)
        self.assertEqual(version.latest_acceptance_attempt_id, base.pk)
        self.assertEqual(version.latest_validated_acceptance_attempt_id, base.pk)
        self.assertTrue(version.has_selected_smoke_attempt)
        # A damaged projection must not smuggle a combination into base evidence.
        version.smoke_test_evidence = {"smoke_attempt_id": str(newer_combination.pk)}
        self.assertEqual(set(AcceptanceSmokeView._workflow_attempt_map([version])), {base.pk})
        self.assertIn("INDEX", query.explain().upper())
        self.workflow = WorkflowTemplateVersion.objects.create(template=self.workflow.template, version=2,
            validated_deployment=self.deployment)
        self.workflow_instance = ModelExecutionInstanceVersion.objects.create(instance=self.instance, version=3,
            deployment=self.deployment, workflow_version=self.workflow, operation="video.generate", prompt_mode="none")
        self.attempt("workflow", status="failed")
        version = query.get(pk=self.workflow.pk)
        self.assertIsNone(version.latest_acceptance_attempt_id)
        self.assertFalse(version.has_selected_smoke_attempt)

    def test_cancelling_unsubmitted_combination_does_not_reset_base_pass(self):
        self.actor.is_staff = True
        self.actor.save(update_fields=["is_staff"])
        WorkflowTemplateVersion.objects.filter(pk=self.workflow.pk).update(smoke_test_state="passed")
        attempt = self.attempt("workflow")
        ExecutionPreparation.objects.create(workflow_smoke=attempt, execution_instance_version=self.workflow_instance,
            created_by=self.actor, status="preparing", purpose="instance_smoke", unit_key="smoke", request_key="scope-cancel", intent_sha256="a" * 64)
        cancelled = request_workflow_smoke_cancel(attempt, actor=self.actor)
        self.assertEqual(cancelled.status, "cancelled")
        self.assertIsNone(cancelled.submission_started_at)
        self.workflow.refresh_from_db()
        self.assertEqual(self.workflow.smoke_test_state, "passed")

    def test_deployment_preparing_cancel_closes_without_remote_deadline(self):
        self.actor.is_staff = True
        self.actor.save(update_fields=["is_staff"])
        attempt = self.attempt("deployment")
        ExecutionPreparation.objects.create(deployment_smoke=attempt, execution_instance_version=self.version,
            created_by=self.actor, status="preparing", purpose="instance_smoke", unit_key="smoke", request_key="scope-cancel", intent_sha256="a" * 64)
        cancelled = request_deployment_smoke_cancel(attempt, actor=self.actor)
        self.assertEqual(cancelled.status, "cancelled")
        self.assertIsNone(cancelled.submission_started_at)
        self.assertIsNone(cancelled.final_snapshot_id)
