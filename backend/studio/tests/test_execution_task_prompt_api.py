"""Actual frozen prompt channels, not legacy PromptArtifact projections."""
from copy import deepcopy
from unittest.mock import patch

from django.contrib.auth import get_user_model
from django.db import connection
from django.test import TestCase, override_settings
from django.test.utils import CaptureQueriesContext
from rest_framework.test import APIClient

from studio.models import GenerationAttempt, GenerationTask
from studio.services.execution_finalization import finalize_execution
from studio.services.execution_preparations import create_execution_instance_preparation
from studio.services.execution_production_dispatch import dispatch_production_execution
from studio.services.json_contracts import json_contract
from studio.tests import test_execution_production_workflow_artifacts as fixtures
from studio.tests.test_execution_production_auxiliaries import production_observations


@override_settings(AI_PROVIDER_ALLOWED_HOSTS=("127.0.0.1",))
class ExecutionTaskPromptAPITests(TestCase):
    resolve = fixtures.ProductionWorkflowArtifactTests.resolve
    mark_submitted = fixtures.ProductionWorkflowArtifactTests.mark_submitted

    def setUp(self):
        fixtures.ProductionWorkflowArtifactTests.setUp(self)
        self.client = APIClient()
        self.client.force_authenticate(self.user)
        self.url = f"/api/v1/tasks/{self.task.pk}/input-prompt/"

    def test_reads_exact_channels_identity_and_labels_without_old_rows_or_io(self):
        with patch("studio.services.task_queue.comfy_client_for_task", side_effect=AssertionError("Read only")), \
                CaptureQueriesContext(connection) as queries:
            response = self.client.get(self.url)
        self.assertEqual(response.status_code, 200, response.data)
        row = response.data
        self.assertEqual(row["preparation_id"], str(self.preparation.pk))
        self.assertEqual(row["attempt_id"], str(self.attempt.pk))
        self.assertEqual(row["request_sha256"], self.preparation.request_sha256)
        self.assertEqual(row["prompt_mode"], "manual")
        self.assertIsNone(row["prompt_profile_version_id"])
        self.assertEqual({item["key"]: item["text"] for item in row["channels"]}, self.intent.value["manual_channels"])
        self.assertEqual([item["key"] for item in row["channels"]], list(self.intent.value["manual_channels"]))
        self.assertEqual({item["semantic_key"] for item in row["channels"]}, {"prompt.negative", "prompt.positive"})
        self.assertEqual(row["declared_reference_labels"], ["<Picture 1>"])
        self.assertFalse(any(item["sql"].lstrip().upper().startswith(("INSERT", "UPDATE", "DELETE")) for item in queries))
        self.assertFalse(any("studio_generatedworkflow" in item["sql"].lower() or "studio_promptartifact" in item["sql"].lower() for item in queries))
        self.assertNotIn("workflow", row)
        self.assertNotIn("prompt_artifact_id", row)
        self.assertEqual(response["Cache-Control"], "private, no-store")

    def test_project_changes_and_missing_upload_receipts_do_not_rewrite_frozen_text(self):
        expected = self.client.get(self.url)
        self.assertEqual(expected.status_code, 200, expected.data)
        self.project.title = "Changed after request freeze"
        self.project.save(update_fields=("title", "updated_at"))
        GenerationTask.objects.filter(pk=self.task.pk).update(staged_inputs=[])
        self.assertEqual(self.client.get(self.url).data, expected.data)

    def test_corrupt_request_is_not_rescued_by_old_prompt(self):
        GenerationAttempt.objects.filter(pk=self.attempt.pk).update(request_sha256="0" * 64)
        response = self.client.get(self.url)
        self.assertEqual(response.status_code, 400)
        self.assertNotIn("WRONG OLD PROMPT", str(response.data))
        self.assertNotIn("PRIVATE PROMPT", str(response.data))

    def test_explicit_empty_channel_remains_empty_and_unsubmitted_request_can_be_inspected(self):
        body = deepcopy(self.intent.value)
        body["manual_channels"] = {"positive_prompt": "保留 <Picture 1> 的外观。", "negative_prompt": ""}
        self.intent = json_contract(body)
        preparation = create_execution_instance_preparation(owner_ref=("stage_run_id", self.stage.pk),
            version_id=self.version.pk, expected_definition_sha256=self.definition.sha256, unit_key="image-unit",
            request_key="empty-negative", intent=self.intent, resolve_facts=self.resolve, actor=self.user)
        preparation = finalize_execution(preparation_id=preparation.pk,
            expected_preparation_revision=preparation.revision, actor=self.user, collect_admission_facts=production_observations)
        with patch("studio.services.execution_production_dispatch.collect_production_execution_admission", side_effect=production_observations):
            attempt = dispatch_production_execution(preparation_id=preparation.pk,
                expected_preparation_revision=preparation.revision, actor=self.user)
        task = GenerationTask.objects.create(project=self.project, owner=self.user, kind="image", status="queued")
        attempt.generation_task = task
        attempt.save(update_fields=["generation_task", "updated_at"])
        response = self.client.get(f"/api/v1/tasks/{task.pk}/input-prompt/")
        self.assertEqual(response.status_code, 200, response.data)
        channels = {item["key"]: item for item in response.data["channels"]}
        self.assertEqual(channels["negative_prompt"]["text"], "")
        self.assertEqual(channels["negative_prompt"]["character_count"], 0)
        self.assertEqual(channels["positive_prompt"]["character_count"], len(body["manual_channels"]["positive_prompt"]))
        self.assertIsNone(attempt.submission_started_at)

    def test_prompt_read_preserves_owner_boundary(self):
        outsider = get_user_model().objects.create_user(username="prompt-outsider")
        self.client.force_authenticate(outsider)
        self.assertEqual(self.client.get(self.url).status_code, 404)
        outsider.is_staff = True
        outsider.save(update_fields=["is_staff"])
        self.assertEqual(self.client.get(self.url).status_code, 200)
