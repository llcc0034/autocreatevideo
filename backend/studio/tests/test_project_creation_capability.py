from unittest.mock import patch

from django.db import connection
from django.test import TestCase
from django.test.utils import CaptureQueriesContext
from rest_framework.test import APIClient

from studio.services.configuration import ConfigurationResolutionError
from studio.tests.factories import build_story


class ProjectCreationCapabilityTests(TestCase):
    def setUp(self):
        self.user, *_ = build_story(username="creation-capability")
        self.client = APIClient()
        self.client.force_authenticate(self.user)

    def test_same_policy_as_production_without_execution_or_task_scan(self):
        with patch("studio.capability_views.configured_video_conditioning_modes", return_value=[]):
            full = self.client.get("/api/v1/capabilities/production/")
        self.assertEqual(full.status_code, 200, full.data)
        with patch("studio.capability_views.configured_video_conditioning_modes", side_effect=AssertionError("must not scan executions")), CaptureQueriesContext(connection) as queries:
            response = self.client.get("/api/v1/capabilities/project-creation/")
        self.assertEqual(response.status_code, 200, response.data)
        self.assertEqual(response.data["creation_defaults"], full.data["creation_defaults"])
        self.assertRegex(response.data["configuration_public_sha256"], r"^[0-9a-f]{64}$")
        self.assertLess(len(queries), 15, [query["sql"] for query in queries])
        for query in queries:
            for table in ("studio_generationtask", "studio_executioninstance", "studio_workflowtemplate", "studio_modeldeployment"):
                self.assertNotIn(table, query["sql"].lower())
        again = self.client.get("/api/v1/capabilities/project-creation/")
        self.assertEqual(response.data, again.data)

    def test_missing_configuration_is_explicit_and_not_defaulted(self):
        error = ConfigurationResolutionError("缺少创建策略", code="required_config_missing", missing_keys=["production.project_creation_policy"])
        with patch("studio.capability_views.project_creation_policy", side_effect=error):
            response = self.client.get("/api/v1/capabilities/project-creation/")
        self.assertEqual(response.status_code, 409)
        self.assertEqual(response.data["code"], "required_config_missing")
        self.assertNotIn("creation_defaults", response.data)

    def test_authentication_required(self):
        response = APIClient().get("/api/v1/capabilities/project-creation/")
        self.assertEqual(response.status_code, 401)
