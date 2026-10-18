"""Explicit text-resource API. Worker recovery and output contracts are covered
by the resource evidence, native text and field-worker suites, not Binding samples.
"""
from copy import deepcopy
from dataclasses import replace
from unittest.mock import patch

from django.db import connection
from django.test import TestCase, override_settings
from django.test.utils import CaptureQueriesContext
from rest_framework.test import APIClient

from studio.models import ExecutionPreparation, ModelDeploymentSmokeAttempt, ModelExecutionInstanceVersion
from studio.services.execution_preparation_evaluation import evaluate_preparation_fields
from studio.services.execution_preparation_requests import compile_prepared_execution_transport
from studio.services.execution_resource_smoke_definition import build_resource_smoke_definition
from studio.tests import test_execution_resource_selection as resource_fixtures


@override_settings(AI_PROVIDER_ALLOWED_HOSTS=("api.example.com",))
class DeploymentTextSmokeQueueTests(TestCase):
    setUpTestData = resource_fixtures.ExecutionResourceSelectionTests.__dict__["setUpTestData"]
    make_resource_profile = resource_fixtures.ExecutionResourceSelectionTests.make_resource_profile

    def setUp(self):
        resource_fixtures.ExecutionResourceSelectionTests.setUp(self)
        self.client = APIClient()
        self.client.force_authenticate(self.actor)
        self.endpoint = f"/api/v1/model-deployments/{self.deployment.pk}/smoke-test/"

    def body(self, profile=None):
        selection = replace(self.selection, prompt_mode="profile" if profile else "manual", prompt_profile_version=profile)
        definition = build_resource_smoke_definition(selection)
        fixture = deepcopy(self.fixture)
        fixture["inputs"] = {row["key"]: fixture["inputs"][row["targets"][0]["target_key"]]
            for row in definition.value["configuration"]["inputs"]}
        if profile:
            fixture["manual_channels"] = {}
        return {"selection": {"operation": selection.operation, "mode": "", "prompt_mode": selection.prompt_mode,
            "prompt_profile_version_id": str(profile.pk) if profile else None},
            "expected_definition_sha256": definition.sha256, "fixture": fixture,
            "field_values": [], "allow_external_calls": False}

    def post(self, body, key):
        return self.client.post(self.endpoint, body, format="json", HTTP_IDEMPOTENCY_KEY=key)

    def test_manual_and_exact_profile_create_original_preparation_without_binding_or_provider_calls(self):
        baseline = ModelExecutionInstanceVersion.objects.count()
        profile = self.make_resource_profile()
        for item in (None, profile):
            with self.subTest(profile=item), CaptureQueriesContext(connection) as queries, patch(
                    "studio.services.provider_registry._request_json", side_effect=AssertionError("No submission")):
                response = self.post(self.body(item), "profile" if item else "manual")
            self.assertEqual(response.status_code, 202, response.data)
            self.assertFalse(any("promptprofilebinding" in row["sql"].lower() for row in queries))
            attempt = ModelDeploymentSmokeAttempt.objects.get(pk=response.data["attempt_id"])
            prep = ExecutionPreparation.objects.get(pk=response.data["preparation"]["id"])
            self.assertEqual(prep.deployment_smoke_id, attempt.pk)
            self.assertIsNone(prep.execution_instance_version_id)
            self.assertEqual(attempt.prompt_profile_version_id, item.pk if item else None)
            self.assertNotIn("prompt_profile_binding", {field.name for field in attempt._meta.fields})
            self.assertIsNone(attempt.submission_started_at)
            self.assertIsNone(attempt.deadline_started_at)
            cancelled = self.client.post(f"/api/v1/model-deployment-smoke-attempts/{attempt.pk}/cancel/", {}, format="json")
            self.assertEqual(cancelled.status_code, 202, cancelled.data)
        self.assertEqual(ModelExecutionInstanceVersion.objects.count(), baseline)

    def test_exact_profile_change_is_a_different_intent_not_reused_under_same_key(self):
        original = self.body(self.make_resource_profile())
        changed = self.body(self.make_resource_profile())
        created = self.post(original, "exact-profile")
        self.assertEqual(created.status_code, 202, created.data)
        replay = self.post(original, "exact-profile")
        self.assertEqual(replay.status_code, 200, replay.data)
        self.assertEqual(replay.data["attempt_id"], created.data["attempt_id"])
        conflict = self.post(changed, "exact-profile")
        self.assertEqual(conflict.status_code, 409, conflict.data)
        self.assertEqual(ModelDeploymentSmokeAttempt.objects.count(), 1)
        self.assertEqual(ExecutionPreparation.objects.count(), 1)

    def test_manual_multiline_channels_and_business_inputs_reach_same_request_compiler_unchanged(self):
        body = self.body()
        prompt = "  保留第一段。\n保留第二段与所有间距。  "
        body["fixture"]["manual_channels"]["positive_prompt"] = prompt
        created = self.post(body, "explicit-input")
        self.assertEqual(created.status_code, 202, created.data)
        prep = ExecutionPreparation.objects.get(pk=created.data["preparation"]["id"])
        prep = evaluate_preparation_fields(preparation_id=prep.pk, expected_preparation_revision=prep.revision, actor=self.actor)
        preview = compile_prepared_execution_transport(preparation_id=prep.pk,
            expected_preparation_revision=prep.revision, actor=self.actor)
        self.assertEqual(preview.transport.value["prompt_channels"]["values"]["positive_prompt"], prompt)
        self.assertEqual(preview.transport.value["operation_plan"]["plan"]["request"]["idea"], "旅人放下一个盒子。")
        self.assertEqual(preview.transport.value["payload"]["seed"], 42)
        self.assertIsNone(preview.transport.value["execution_instance_version_id"])

    def test_missing_selection_legacy_binding_or_mixed_prompt_cannot_allocate_a_task(self):
        valid = self.body()
        profile = self.body(self.make_resource_profile())
        for body in ({"operation": "script.generate"}, {**valid, "prompt_profile_binding_id": "old-binding"},
                {**valid, "budget_limit": None}, {**valid, "budget_limit": "1.00"},
                {**profile, "fixture": {**profile["fixture"], "manual_channels": {"positive_prompt": "mixed"}}},
                {**valid, "selection": {**valid["selection"], "operation": "undeclared.operation"}}):
            with self.subTest(body=body):
                response = self.post(body, "invalid")
                self.assertEqual(response.status_code, 400, response.data)
        self.assertFalse(ModelDeploymentSmokeAttempt.objects.exists())
        self.assertFalse(ExecutionPreparation.objects.exists())
