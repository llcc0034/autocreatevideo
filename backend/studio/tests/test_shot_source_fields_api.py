"""Official, permission-scoped lazy detail; no model or live business operations."""
from unittest.mock import patch
from uuid import uuid4

from django.db import connection
from django.test import TestCase, override_settings
from django.test.utils import CaptureQueriesContext

from studio.models import Shot, ShotScriptSource
from studio.shot_source_serializers import ShotSourceFieldsSerializer
from studio.tests import test_storyboard_declared_writer as fixtures


@override_settings(AI_PROVIDER_ALLOWED_HOSTS=("api.example.com",))
class ShotSourceFieldsApiTests(TestCase):
    create = fixtures.BoundStoryboardFieldTests.create
    post = fixtures.BoundStoryboardFieldTests.post
    read = fixtures.BoundStoryboardFieldTests.read
    adopt_source = fixtures.BoundStoryboardFieldTests.adopt_source
    approval = fixtures.BoundStoryboardFieldTests.approval
    setUp = fixtures.BoundStoryboardFieldTests.setUp

    def source_url(self, shot_id=None):
        return f"/api/v1/shots/{shot_id or self.shot_id}/source-fields/"

    def test_exact_original_response_is_read_only_private_and_narrow(self):
        with patch("studio.services.script_revision_structures.parse_script_semantic_output",
                side_effect=AssertionError("No reparse")), CaptureQueriesContext(connection) as queries:
            response = self.client.get(self.source_url())
        self.assertEqual(response.status_code, 200, response.data)
        self.assertEqual(response.data["fields"], self.original_fields)
        self.assertEqual(response.data["source"]["shot_id"], str(self.shot_id))
        self.assertEqual(response["Cache-Control"], "private, no-store")
        self.assertEqual(response["X-Content-Type-Options"], "nosniff")
        self.assertTrue(fixtures.no_dml(queries))
        for row in queries:
            self.assertNotIn('"studio_fielddefinition', row["sql"])
            self.assertNotIn('"master_script"', row["sql"])
        Shot.objects.filter(pk=self.shot_id).update(number=99)
        self.assertEqual(self.client.get(self.source_url()).data, response.data)

    def test_missing_or_damaged_source_is_conflict_not_empty_success(self):
        ShotScriptSource.objects.filter(pk=self.shot_id).update(document_sha256="0" * 64)
        result = self.client.get(self.source_url())
        self.assertEqual(result.status_code, 409)
        self.assertEqual(result.data["error"]["code"], "storyboard_source_conflict")
        ShotScriptSource.objects.filter(pk=self.shot_id).delete()
        result = self.client.get(self.source_url())
        self.assertEqual(result.status_code, 409)
        self.assertEqual(result.data["error"]["code"], "storyboard_source_missing")
        self.assertNotIn("fields", result.data)

    def test_permissions_and_no_write_methods(self):
        self.assertEqual(self.client.post(self.source_url(), {}, format="json").status_code, 405)
        self.assertEqual(self.client.get(self.source_url(uuid4())).status_code, 404)
        self.client.force_authenticate(self.other)
        self.assertEqual(self.client.get(self.source_url()).status_code, 404)
        self.other.is_staff = True
        self.other.save(update_fields=("is_staff",))
        self.assertEqual(self.client.get(self.source_url()).status_code, 200)
        self.client.force_authenticate(None)
        self.assertIn(self.client.get(self.source_url()).status_code, (401, 403))

    def test_ordinary_shot_reads_do_not_expand_source_body(self):
        with patch("studio.services.script_storyboard_fields.read_shot_source_fields",
                side_effect=AssertionError("Must be lazy")):
            response = self.client.get(f"/api/v1/shots/{self.shot_id}/")
            self.assertEqual(response.status_code, 200, response.data)
            self.assertNotIn("source_fields", response.data)

    def test_output_serializer_preserves_presence_and_all_json_value_types(self):
        original = self.client.get(self.source_url()).data
        field = original["fields"][0]
        cases = [{**field, "value": value} for value in (None, "", 0, False, [], {}, "  原文\n", {"custom": [None, False]})]
        missing = {**field, "present": False, "source_present": False}
        missing.pop("value", None)
        cases.append(missing)
        result = ShotSourceFieldsSerializer({**original, "fields": cases}).data
        self.assertEqual(result["fields"], cases)
        self.assertNotIn("value", result["fields"][-1])
        with patch("studio.services.script_storyboard_fields.read_shot_source_fields",
                return_value={**original, "fields": cases}):
            response = self.client.get(self.source_url())
        self.assertEqual(response.status_code, 200)
        self.assertEqual(response.json()["fields"], cases)
