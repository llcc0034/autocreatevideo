"""Actual prepared inputs: immutable values, distinct uses and current previews."""
import json
from types import SimpleNamespace
from unittest.mock import patch

from django.contrib.auth import get_user_model
from django.db import connection
from django.test import TestCase, override_settings
from django.test.utils import CaptureQueriesContext
from rest_framework.test import APIClient

from studio.models import Asset, GenerationAttempt, GenerationTask
from studio.serializers import GenerationTaskSerializer
from studio.services.execution_workflow_inspection import inspect_prepared_task_inputs
from studio.tests import test_execution_production_workflow_artifacts as fixtures


@override_settings(AI_PROVIDER_ALLOWED_HOSTS=("127.0.0.1",))
class ExecutionTaskInputsAPITests(TestCase):
    resolve = fixtures.ProductionWorkflowArtifactTests.resolve
    mark_submitted = fixtures.ProductionWorkflowArtifactTests.mark_submitted

    def setUp(self):
        fixtures.ProductionWorkflowArtifactTests.setUp(self)
        self.user.is_staff = False
        self.user.save(update_fields=["is_staff"])
        self.client = APIClient()
        self.client.force_authenticate(self.user)
        self.url = f"/api/v1/tasks/{self.task.pk}/"

    def test_detail_projects_frozen_values_uses_and_current_owned_preview_without_prompt_body(self):
        Asset.objects.filter(pk=self.asset.pk).update(name="Renamed after preparation")
        response = self.client.get(self.url)
        self.assertEqual(response.status_code, 200, response.data)
        row = response.data["input_parameters"]
        self.assertEqual(row["preparation_id"], str(self.preparation.pk))
        self.assertEqual(row["request_sha256"], self.preparation.request_sha256)
        self.assertEqual(row["workflow_version_id"], str(self.workflow.pk))
        parameters = {item["key"]: item for item in row["parameters"]}
        self.assertEqual(parameters["sampling.seed"]["value"], 7)
        self.assertEqual(parameters["sampling.seed"]["value_source"], "request")
        use, = row["media_uses"]
        self.assertEqual(use["item_key"], "picture-use")
        self.assertEqual(use["display_name"], "Actual reference")
        self.assertEqual(use["input_label"], "<Picture 1>")
        self.assertEqual(use["reference_binding"], {"position": 0, "original_name": "Actual reference",
            "reference_name": None, "name_disambiguated": None, "input_slot": None, "shot_reference": None})
        self.assertEqual(use["staging_status"], "staged")
        self.assertTrue(use["ports"])
        self.assertTrue(use["preview_url"].startswith("/api/v1/media/"))
        encoded = json.dumps(row)
        for forbidden in ("PRIVATE PROMPT", "WRONG OLD PROMPT", "local_path", "comfy_path", "capability_snapshot", "injected_values"):
            self.assertNotIn(forbidden, encoded)
        self.assertEqual(response.data["input_contract_status"], "available")
        self.assertEqual(response.data["input_contract_code"], "")
        self.assertEqual(response.data["execution"]["production_run_id"], str(self.run.pk))

    def test_task_center_projects_same_root_receipts_and_rejects_corruption(self):
        url = f"/api/v1/task-center/preparation/{self.preparation.pk}/"
        response = self.client.get(url)
        self.assertEqual(response.status_code, 200, response.data)
        data = response.data.get("data", response.data)
        self.assertEqual(data["input"]["state"], "frozen", data["validation"])
        trace = data["input"]["media_trace"]
        self.assertEqual(trace["root_id"], str(self.task.pk))
        self.assertEqual(trace["items"][0]["receipt_state"], "recorded")
        self.assertEqual(trace["items"][0]["reference_binding"],
            self.client.get(self.url).data["input_parameters"]["media_uses"][0]["reference_binding"])
        self.assertTrue(trace["items"][0]["graph_inputs"])
        GenerationTask.objects.filter(pk=self.task.pk).update(staged_inputs=[])
        response = self.client.get(url)
        data = response.data.get("data", response.data)
        self.assertEqual(data["input"]["media_trace"]["items"][0]["receipt_state"], "missing")
        bad = self.task.staged_inputs
        bad[0]["source_sha256"] = "0" * 64
        GenerationTask.objects.filter(pk=self.task.pk).update(staged_inputs=bad)
        response = self.client.get(url)
        data = response.data.get("data", response.data)
        self.assertEqual(data["input"]["state"], "error")
        self.assertIsNone(data["input"]["media_trace"])
        self.assertEqual(data["validation"]["error_code"], "task_center_input_invalid")

    def test_readonly_projection_never_reads_old_graphs_assets_or_uploads(self):
        self.assertNotIn("capability_snapshot", {field.name for field in GenerationTask._meta.fields})
        with CaptureQueriesContext(connection) as queries, patch(
                "studio.services.task_queue.comfy_client_for_task", side_effect=AssertionError("No peer")):
            row = inspect_prepared_task_inputs(self.task)
        self.assertEqual(row["media_uses"][0]["preview_url"], "")
        for query in queries:
            self.assertFalse(query["sql"].lstrip().upper().startswith(("INSERT", "UPDATE", "DELETE")))
            for table in ("studio_generatedworkflow", "studio_promptartifact", '"studio_asset"'):
                self.assertNotIn(table, query["sql"].lower())
        response = self.client.get(self.url)
        self.assertEqual(response.status_code, 200, response.data)
        self.assertEqual(response.data["execution"]["production_run_id"], str(self.run.pk))

    def test_list_projection_never_looks_up_preview_or_relabels_frozen_media(self):
        serializer = GenerationTaskSerializer(context={"view": SimpleNamespace(action="list"),
            "request": SimpleNamespace(user=self.user)})
        with CaptureQueriesContext(connection) as queries, patch.object(
                GenerationTaskSerializer, "_task_asset_preview_url", side_effect=AssertionError("No storage")):
            row = serializer.get_input_parameters(self.task)
        self.assertEqual(row["media_uses"][0]["preview_url"], "")
        self.assertFalse(any('"studio_asset"' in item["sql"].lower() for item in queries))

    def test_task_identity_and_filters_use_actual_relations_without_old_trace_joins(self):
        with CaptureQueriesContext(connection) as queries:
            response = self.client.get("/api/v1/tasks/", {"production_run_id": str(self.run.pk),
                "stage_run_id": str(self.stage.pk), "preparation_id": str(self.preparation.pk),
                "execution_instance_version_id": str(self.version.pk), "unit_key": "image-unit"})
        self.assertEqual(response.status_code, 200, response.data)
        row, = response.data["results"]
        self.assertEqual(row["execution"]["attempt_number"], self.attempt.attempt_number)
        self.assertEqual(row["execution"]["stage_run_id"], str(self.stage.pk))
        self.assertEqual(row["execution"]["preparation_id"], str(self.preparation.pk))
        for key in ("generated_workflow", "batch_id", "batch_slots", "shot_id", "retry_of", "seed", "max_attempts", "staged_inputs", "outputs"):
            self.assertNotIn(key, row)
        self.assertIsNone(row["episode_id"])
        for query in queries:
            self.assertNotIn('"studio_generatedworkflow"', query["sql"].lower())
            self.assertNotIn('"studio_promptartifact"', query["sql"].lower())
            self.assertNotIn('"capability_snapshot"', query["sql"].lower())
        self.assertEqual(self.client.get("/api/v1/tasks/", {"unit_key": "other-unit"}).data["results"], [])
        self.assertEqual(self.client.get("/api/v1/tasks/", {"production_run_id": "invalid"}).status_code, 400)
        self.assertEqual(self.client.get("/api/v1/tasks/", {"shot_id": str(self.project.pk)}).status_code, 400)
        self.assertEqual(self.client.get("/api/v1/tasks/", {"batch_id": "old-batch"}).status_code, 400)

    def test_missing_or_changed_primary_relation_never_infers_identity_from_legacy_snapshot(self):
        GenerationAttempt.objects.filter(pk=self.attempt.pk).update(generation_task=None)
        response = self.client.get("/api/v1/tasks/")
        self.assertEqual(response.status_code, 200, response.data)
        row, = response.data["results"]
        self.assertIsNone(row["execution"])
        self.assertEqual(row["input_contract_status"], "unavailable")
        self.assertEqual(self.client.get(self.url).status_code, 400)
        GenerationAttempt.objects.filter(pk=self.attempt.pk).update(generation_task=self.task, unit_key="wrong-unit")
        row, = self.client.get("/api/v1/tasks/").data["results"]
        self.assertIsNone(row["execution"])
        self.assertEqual(row["input_contract_status"], "unavailable")

    def test_relational_filter_cannot_expose_another_owners_task(self):
        other = get_user_model().objects.create_user(username="task-filter-other")
        self.client.force_authenticate(other)
        self.assertEqual(self.client.get("/api/v1/tasks/", {"production_run_id": str(self.run.pk)}).data["results"], [])
        self.assertEqual(self.client.get(self.url).status_code, 404)

    def test_missing_staging_is_explicit_but_corrupt_receipts_or_requests_are_rejected(self):
        original = self.task.staged_inputs
        GenerationTask.objects.filter(pk=self.task.pk).update(staged_inputs=[])
        response = self.client.get(self.url)
        self.assertEqual(response.status_code, 200, response.data)
        self.assertEqual(response.data["input_parameters"]["media_uses"][0]["staging_status"], "not_staged")
        original[0]["source_sha256"] = "0" * 64
        GenerationTask.objects.filter(pk=self.task.pk).update(staged_inputs=original)
        self.assertEqual(self.client.get(self.url).status_code, 400)
        GenerationTask.objects.filter(pk=self.task.pk).update(staged_inputs=[])
        GenerationAttempt.objects.filter(pk=self.attempt.pk).update(request_sha256="0" * 64)
        self.assertEqual(self.client.get(self.url).status_code, 400)

    def test_current_missing_changed_inactive_or_foreign_asset_never_gets_a_substitute_preview(self):
        other = get_user_model().objects.create_user(username="inputs-other")
        changes = [{"is_active": False}, {"is_active": True, "checksum": "0" * 64},
            {"checksum": self.asset.checksum, "owner": other}]
        for changed in changes:
            with self.subTest(changed=changed):
                Asset.objects.filter(pk=self.asset.pk).update(**changed)
                response = self.client.get(self.url)
                self.assertEqual(response.status_code, 200, response.data)
                use = response.data["input_parameters"]["media_uses"][0]
                self.assertEqual(use["preview_url"], "")
                self.assertEqual(use["display_name"], "Actual reference")
