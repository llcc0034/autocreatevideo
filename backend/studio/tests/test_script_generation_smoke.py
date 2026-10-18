"""Native model publication checks used by the shared resource Smoke contract.

Execution/protocol outcomes live in test_execution_native_text and the common
resource-worker suites. No synthetic concept request or implicit Binding route.
"""
from copy import deepcopy
from uuid import uuid4

from django.test import TestCase, override_settings
from rest_framework.test import APIClient

from studio.models import ModelProfileVersion
from studio.services.json_contracts import assign_json_contract
from studio.tests import test_execution_native_text as fixtures


@override_settings(AI_PROVIDER_ALLOWED_HOSTS=("api.example.com",))
class ScriptGenerationDeploymentSmokeTests(TestCase):
    setUpTestData = classmethod(fixtures.ExecutionNativeTextTests.setUpTestData.__func__)

    def setUp(self):
        fixtures.ExecutionNativeTextTests.setUp(self)
        self.client = APIClient()
        self.client.force_authenticate(self.actor)

    def draft(self, specification):
        model = ModelProfileVersion(profile_key="native-validation-" + uuid4().hex, version=1,
            model_identifier="declared-native-model", provider=self.deployment.provider, modality="text")
        assign_json_contract(model, "specification", specification)
        model.save()
        return model

    def test_publish_rejects_unsupported_native_script_schema(self):
        model = self.draft({**self.deployment.profile_version.specification, "schema_version": 2})
        response = self.client.post(f"/api/v1/model-profile-versions/{model.pk}/publish/", {}, format="json")
        self.assertEqual(response.status_code, 400, response.data)
        self.assertIn("schema_version", str(response.data))
        model.refresh_from_db()
        self.assertEqual(model.status, "draft")

    def test_publication_does_not_infer_capability_alias_or_missing_native_policy(self):
        base = deepcopy(self.deployment.profile_version.specification)
        cases = [
            {**base, "capabilities": ["script.generate "], "operations": ["script.generate"]},
            {**base, "series_concept": {key: value for key, value in base["series_concept"].items() if key != "timeout_seconds"}},
            {**base, "series_concept": {key: value for key, value in base["series_concept"].items() if key != "chat_request_options"}},
            {**base, "series_concept": {**base["series_concept"], "field_contract": {}}},
        ]
        for specification in cases:
            model = self.draft(specification)
            response = self.client.post(f"/api/v1/model-profile-versions/{model.pk}/publish/", {}, format="json")
            self.assertEqual(response.status_code, 400, response.data)
            model.refresh_from_db()
            self.assertEqual(model.status, "draft")
