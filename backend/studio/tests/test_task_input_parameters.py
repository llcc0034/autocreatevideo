"""Video input inspection through actual Preparation, not generated-graph caches.

Image inputs, per-use identity, tenant scope and read-only previews are covered
by ExecutionTaskInputsAPITests; these checks add video timing and corruption.
"""
import json
from copy import deepcopy

from django.test import TestCase, override_settings
from django.core.exceptions import ValidationError
from django.db.models import QuerySet
from rest_framework.test import APIClient

from studio.models import ExecutionPreparation, GenerationAttempt, GenerationTask
from studio.services.execution_production_media import read_prepared_production_media
from studio.tests import test_execution_production_workflow_requests as fixtures


@override_settings(AI_PROVIDER_ALLOWED_HOSTS=("127.0.0.1",))
class GenerationTaskInputParametersTests(TestCase):
    _create_version = fixtures.ProductionWorkflowRequestTests._create_version
    resolve = fixtures.ProductionWorkflowRequestTests.resolve

    def setUp(self):
        fixtures.ProductionWorkflowRequestTests.setUp(self)
        self.task = self.attempt.generation_task
        self.client = APIClient()
        self.client.force_authenticate(self.user)
        self.url = f"/api/v1/tasks/{self.task.pk}/"

    def test_real_video_inputs_keep_resolved_values_rules_sources_and_ordered_uses(self):
        response = self.client.get(self.url)
        self.assertEqual(response.status_code, 200, response.data)
        row = response.data["input_parameters"]
        self.assertEqual(row["preparation_id"], str(self.preparation.pk))
        self.assertEqual(row["request_sha256"], self.preparation.request_sha256)
        parameters = {item["key"]: item for item in row["parameters"]}
        frozen = self.preparation.contract.resolved_inputs["parameters"]
        self.assertEqual({key: value["value"] for key, value in parameters.items()}, frozen["values"])
        self.assertEqual({key: value["value_source"] for key, value in parameters.items()}, frozen["sources"])
        self.assertEqual(parameters["audio.enabled"]["rule"], {"const": True})
        self.assertEqual(parameters["audio.enabled"]["value_source"], "workflow.static_semantics")
        uses = row["media_uses"]
        self.assertTrue(uses)
        self.assertEqual(len({item["item_key"] for item in uses}), len(uses))
        self.assertTrue(all(item["staging_status"] == "not_staged" for item in uses))
        self.assertTrue(all(item["ports"] for item in uses))
        encoded = json.dumps(row)
        for forbidden in ("local_path", "comfy_path", "capability_snapshot", "injected_values", "generated_workflow"):
            self.assertNotIn(forbidden, encoded)

    def test_live_project_dimensions_do_not_rewrite_frozen_video_inputs(self):
        before = self.client.get(self.url).data["input_parameters"]
        self.project.width, self.project.height, self.project.fps = 1024, 1024, 60
        self.project.save(update_fields=("width", "height", "fps"))
        after = self.client.get(self.url)
        self.assertEqual(after.status_code, 200, after.data)
        actual = deepcopy(after.data["input_parameters"])
        for row in (before, actual):
            for use in row["media_uses"]:
                # Signed preview URLs carry request time, not frozen inputs.
                use.pop("preview_url", None)
        self.assertEqual(actual, before)
        media = read_prepared_production_media(self.attempt)
        values = {row["key"]: row["value"] for row in before["parameters"]}
        self.assertEqual(media["output_kind"], "video")
        self.assertEqual(media["parameters"]["video.frames"], values["video.frames"])
        self.assertEqual(media["parameters"]["video.fps"], values["video.fps"])

    def test_each_corrupt_frozen_request_component_fails_without_read_side_repair(self):
        original_key = self.attempt.request_snapshot_key
        original_hash = self.attempt.request_sha256
        for changes in ({"request_sha256": "0" * 64}, {"request_snapshot_sha256": "0" * 64},
                {"request_snapshot_key": "execution-requests/invalid.json"}):
            with self.subTest(changes=list(changes)):
                GenerationAttempt.objects.filter(pk=self.attempt.pk).update(**changes)
                before = GenerationAttempt.objects.filter(pk=self.attempt.pk).values().get()
                response = self.client.get(self.url)
                self.assertEqual(response.status_code, 400, response.data)
                self.assertEqual(GenerationAttempt.objects.filter(pk=self.attempt.pk).values().get(), before)
                GenerationAttempt.objects.filter(pk=self.attempt.pk).update(request_snapshot_key=original_key,
                    request_sha256=original_hash, request_snapshot_sha256=self.attempt.request_snapshot_sha256)

    def test_missing_preparation_request_cannot_be_rebuilt_from_task_or_project(self):
        query = ExecutionPreparation.objects.filter(pk=self.preparation.pk)
        with self.assertRaisesMessage(ValidationError, "冻结事实不能批量修改"):
            query.update(request_sha256="0" * 64)
        # Deliberately corrupt isolated storage below the application guard to
        # verify the read boundary too. Never expose this through an API.
        QuerySet.update(query, request_sha256="0" * 64)
        before = ExecutionPreparation.objects.filter(pk=self.preparation.pk).values().get()
        response = self.client.get(self.url)
        self.assertEqual(response.status_code, 400, response.data)
        self.assertEqual(ExecutionPreparation.objects.filter(pk=self.preparation.pk).values().get(), before)

    def test_mixed_list_keeps_missing_relation_visible_without_invented_inputs(self):
        orphan = GenerationTask.objects.create(owner=self.user, project=self.project, kind="video", status="failed")
        response = self.client.get("/api/v1/tasks/", {"project": str(self.project.pk)})
        self.assertEqual(response.status_code, 200, response.data)
        rows = response.data.get("results", response.data) if isinstance(response.data, dict) else response.data
        by_id = {row["id"]: row for row in rows}
        self.assertEqual(by_id[str(self.task.pk)]["input_contract_status"], "available")
        self.assertIsNone(by_id[str(orphan.pk)]["execution"])
        self.assertNotEqual(by_id[str(orphan.pk)]["input_contract_status"], "available")
        self.assertIsNone(by_id[str(orphan.pk)]["input_parameters"])
