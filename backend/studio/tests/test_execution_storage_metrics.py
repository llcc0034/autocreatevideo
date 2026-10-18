"""Recorded metadata API: isolated database/media; no model or live service."""
import json
from unittest.mock import patch
from uuid import uuid4

from django.contrib.auth import get_user_model
from django.core.files.storage import default_storage
from django.db import connection
from django.test import TestCase
from django.test.utils import CaptureQueriesContext
from django.utils import timezone
from rest_framework.test import APIClient

from studio.models import AssetGenerationJob, ExecutionPreparation, ExecutionPreparationContract
from studio.services.execution_request_storage import store_prepared_request
from studio.services.execution_storage_metrics import PARTS, storage_metrics_queryset
from studio.services.json_contracts import assign_json_contract, canonical_json_bytes, json_contract
from studio.tests import test_execution_preparation_storage as fixtures


class ExecutionStorageMetricsTests(TestCase):
    setUpTestData = classmethod(fixtures.ExecutionPreparationStorageTests.setUpTestData.__func__)
    preparation = fixtures.ExecutionPreparationStorageTests.preparation
    contract = fixtures.ExecutionPreparationStorageTests.contract

    def setUp(self):
        fixtures.ExecutionPreparationStorageTests.setUp(self)
        self.enterContext(patch("studio.services.execution_request_storage.boundary_int", return_value=8 * 1024 * 1024))
        for target in ("studio.services.provider_registry._request_json", "studio.services.provider_registry.resolve_credential", "studio.services.comfyui.ComfyUIClient"):
            self.enterContext(patch(target, side_effect=AssertionError("No remote execution")))
        self.job = AssetGenerationJob.objects.create(owner=self.actor, model_deployment=self.deployment,
            execution_instance_version=self.version, job_type="prompt", media_type="script", prompt="Isolated fixture",
            operation="prompt.draft", idempotency_key="metrics", request_snapshot_sha256=json_contract({}).sha256)
        self.prep = self.preparation(deployment_smoke=None, asset_job=self.job, purpose="production", execution_instance_version=self.version)
        self.detail = self.contract(self.prep)
        self.client = APIClient()
        self.client.force_authenticate(self.actor)
        self.url = f"/api/v1/execution-preparations/{self.prep.pk}/storage-metrics/"

    def read(self, **query):
        return self.client.get(self.url, {"expected_preparation_revision": self.prep.revision, **query})

    def test_metadata_only_single_indexed_query_no_body_file_or_mutation(self):
        before = ExecutionPreparation.objects.values().get(pk=self.prep.pk)
        with patch.object(default_storage, "open", side_effect=AssertionError("No file read")), CaptureQueriesContext(connection) as queries:
            response = self.read()
        self.assertEqual(response.status_code, 200, response.data)
        self.assertEqual(response["Cache-Control"], "private, no-store")
        items = response.json()["items"]
        self.assertEqual([item["key"] for item in items], [key for key, _ in PARTS])
        for item in items[:3]:
            self.assertEqual(item["size_bytes"], getattr(self.detail, item["key"] + "_size_bytes"))
            self.assertEqual(item["sha256"], getattr(self.detail, item["key"] + "_sha256"))
        for item in items[3:]:
            self.assertEqual(item["state"], "not_generated")
            self.assertIsNone(item["size_bytes"])
            self.assertIsNone(item["over_current_limit_bytes"])
        sql = [row["sql"] for row in queries.captured_queries]
        business = [query for query in sql if 'FROM "studio_executionpreparation"' in query]
        self.assertEqual(len(business), 1, sql)
        for name in ("intent", "resolved_inputs", "review_contract", "evaluation"):
            self.assertNotIn(f'"studio_executionpreparationcontract"."{name}"', business[0])
        self.assertFalse(any(query.lstrip().upper().startswith(("INSERT", "UPDATE", "DELETE")) for query in sql), sql)
        self.assertNotIn("studio_generationattempt", " ".join(sql))
        self.assertEqual(ExecutionPreparation.objects.values().get(pk=self.prep.pk), before)
        plan = storage_metrics_queryset(self.actor, self.prep.pk).explain()
        self.assertIn("USING INDEX", plan)
        self.assertNotIn("SCAN studio_executionpreparation", plan)
        self.assertLess(len(response.content), 4096)

    def test_long_unicode_stored_bytes_and_request_manifest_keep_original_values(self):
        value = {"preparation_id": str(self.prep.pk), "text": "汉😀<>" * 8000, "empty": "", "flag": False}
        contract = store_prepared_request(self.detail, value)
        self.detail.save()
        self.prep.request_frozen_at = timezone.now()
        self.prep.request_sha256 = contract.sha256
        self.prep.status = "ready"
        self.prep.save()
        self.assertGreater(contract.size_bytes, 65536)
        self.assertNotEqual(contract.size_bytes, len(json.dumps(value)))
        # Current configuration below the old recorded size is reported, not used to rewrite history.
        with patch("studio.services.execution_storage_metrics.execution_request_max_bytes", return_value=contract.size_bytes - 1), \
                patch.object(default_storage, "open", side_effect=AssertionError("Metadata is not file verification")):
            response = self.read()
        self.assertEqual(response.status_code, 200, response.data)
        item = response.json()["items"][-1]
        self.assertEqual(item["size_bytes"], len(canonical_json_bytes(value)))
        self.assertEqual(item["sha256"], contract.sha256)
        self.assertEqual(item["over_current_limit_bytes"], 1)
        self.assertEqual(item["basis"], "canonical_request_file_utf8")
        self.assertNotIn(self.detail.prepared_request_key, response.content.decode())
        self.assertNotIn(value["text"], response.content.decode())

    def test_evaluation_is_recorded_not_a_missing_zero(self):
        assign_json_contract(self.detail, "evaluation", {"ready": False, "issues": ["待补充"]})
        self.detail.save()
        ExecutionPreparation.objects.filter(pk=self.prep.pk).update(evaluation_revision=self.prep.revision, evaluated_at=timezone.now())
        response = self.read()
        self.assertEqual(response.status_code, 200, response.data)
        self.assertEqual(response.json()["items"][3]["state"], "recorded")
        self.assertEqual(response.json()["items"][3]["size_bytes"], self.detail.evaluation_size_bytes)

    def test_strict_query_revision_and_read_only_methods(self):
        self.assertEqual(self.read(expected_preparation_revision=self.prep.revision + 1).status_code, 409)
        for query in ("", "?expected_preparation_revision=0", "?expected_preparation_revision=1.0",
                "?expected_preparation_revision=1&extra=x", "?expected_preparation_revision=1&expected_preparation_revision=1"):
            self.assertEqual(self.client.get(self.url + query).status_code, 400, query)
        self.assertEqual(self.client.post(self.url, {}, format="json").status_code, 405)
        self.assertEqual(self.client.get(self.url.replace(str(self.prep.pk), str(uuid4())), {"expected_preparation_revision": 1}).status_code, 404)

    def test_owner_visibility_inactive_and_anonymous(self):
        other = get_user_model().objects.create_user(username="other-metrics")
        self.client.force_authenticate(other)
        self.assertEqual(self.read().status_code, 404)
        self.actor.is_active = False
        self.client.force_authenticate(self.actor)
        self.assertEqual(self.read().status_code, 403)
        self.client.force_authenticate(None)
        self.assertIn(self.read().status_code, (401, 403))

    def test_missing_or_invalid_metadata_reports_error_without_repair(self):
        original = storage_metrics_queryset(self.actor, self.prep.pk).get()
        # Inject malformed metadata at the projection seam; do not weaken immutable writers or DB constraints.
        for change in ({"contract__intent_size_bytes": 0}, {"contract__intent_sha256": "a" * 64},
                {"contract__intent_sha256": "invalid"}, {"contract__resolved_inputs_schema_version": 0},
                {"contract__evaluation_sha256": "a" * 64}, {"evaluation_revision": 1},
                {"request_sha256": "a" * 64}, {"request_frozen_at": timezone.now()}):
            with self.subTest(change=change):
                with patch("studio.services.execution_storage_metrics.storage_metrics_queryset") as query:
                    query.return_value.get.return_value = {**original, **change}
                    self.assertEqual(self.read().status_code, 400)
                self.assertEqual(storage_metrics_queryset(self.actor, self.prep.pk).get(), original)
        # Removing only the isolated fixture detail explicitly tests the missing-contract path.
        self.detail.delete()
        self.assertEqual(self.read().status_code, 400)
