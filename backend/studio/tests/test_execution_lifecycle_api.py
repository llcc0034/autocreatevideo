from copy import deepcopy

from django.contrib.auth import get_user_model
from django.test import TestCase, override_settings
from rest_framework.test import APIClient

from studio.models import ExecutionVersionContract, PlatformLifecycleAuditEvent, ProviderProfile
from studio.tests import test_execution_definitions as definition_fixtures


@override_settings(AI_PROVIDER_ALLOWED_HOSTS=("api.example.com", "cdn.example.com"))
class ExecutionLifecycleApiTests(TestCase):
    write_contract = staticmethod(definition_fixtures.ExecutionDefinitionTests.write_contract)

    @classmethod
    def setUpTestData(cls):
        definition_fixtures.ExecutionDefinitionTests.setUpTestData.__func__(cls)
        cls.member = get_user_model().objects.create_user(username="definition-member")

    def setUp(self):
        definition_fixtures.ExecutionDefinitionTests.setUp(self)
        self.client = APIClient()
        self.client.force_authenticate(self.actor)
        self.url = f"/api/v1/model-execution-instance-versions/{self.version.pk}/"

    def read(self):
        response = self.client.get(self.url + "input-contract/")
        self.assertEqual(response.status_code, 200, response.data)
        return response.data

    def publish(self):
        reviewed = self.read()
        payload = {"expected_revision": reviewed["revision"], "expected_definition_sha256": reviewed["sha256"]}
        response = self.client.post(self.url + "publish/", payload, format="json")
        self.assertEqual(response.status_code, 200, response.data)
        return response, payload

    def test_authoring_catalog_is_readonly_and_not_a_second_definition(self):
        audits = PlatformLifecycleAuditEvent.objects.count()
        response = self.client.get(self.url + "authoring-options/")
        self.assertEqual(response.status_code, 200, response.data)
        self.assertEqual(response.data["version_id"], str(self.version.pk))
        self.assertEqual(response.data["revision"], self.version.revision)
        self.assertIn("positive_prompt", response.data["prompt_channels"])
        self.assertIn("width", response.data["parameters"])
        self.assertIn("subject_kinds", response.data["identity_options"])
        self.assertEqual(response.data["input_purposes"], {})
        for field in ("definition", "definition_sha256", "can_submit", "published"):
            self.assertNotIn(field, response.data)
        self.assertNotIn(self.config["base_url"], str(response.data))
        self.assertFalse(ExecutionVersionContract.objects.exists())
        self.assertEqual(PlatformLifecycleAuditEvent.objects.count(), audits)
        self.client.force_authenticate(self.member)
        self.assertEqual(self.client.get(self.url + "authoring-options/").status_code, 403)

    def test_published_authoring_uses_frozen_detail_not_current_catalog(self):
        self.publish()
        self.assertEqual(self.client.get(self.url + "authoring-options/").status_code, 409)
        self.assertTrue(self.read()["frozen"])

    def test_review_publish_read_retire_same_frozen_definition(self):
        first = self.read()
        self.assertFalse(first["frozen"])
        self.assertFalse(ExecutionVersionContract.objects.exists())
        response, payload = self.publish()
        frozen = self.read()
        self.assertTrue(frozen["frozen"])
        self.assertEqual(first["definition"], frozen["definition"])
        self.assertEqual(frozen["sha256"], first["sha256"])
        self.assertGreater(frozen["revision"], first["revision"])
        retired = self.client.post(self.url + "retire/", {
            **payload, "expected_revision": response.data["revision"], "reason": "调整输入规则",
        }, format="json")
        self.assertEqual(retired.status_code, 200, retired.data)
        self.assertEqual(retired.data["status"], "retired")
        self.assertEqual(self.read()["definition"], first["definition"])

    def test_drafts_are_private_and_lifecycle_actions_remain_admin_only(self):
        for user in (None, self.member):
            self.client.force_authenticate(user)
            response = self.client.get(self.url + "input-contract/")
            if user is None:
                self.assertIn(response.status_code, (401, 403))
            else:
                self.assertEqual(response.status_code, 404)
            for action in ("publish", "retire"):
                self.assertIn(self.client.post(self.url + action + "/", {}, format="json").status_code, (401, 403))
        self.assertFalse(ExecutionVersionContract.objects.exists())

    def test_action_payloads_reject_unknown_fields_missing_sha_and_coerced_revisions(self):
        first = self.read()
        good = {"expected_revision": first["revision"], "expected_definition_sha256": first["sha256"]}
        for payload in ({"expected_revision": 1}, {**good, "expected_revision": True},
                        {**good, "expected_revision": "1"}, {**good, "definition": {}}, {**good, "force": True}):
            response = self.client.post(self.url + "publish/", payload, format="json")
            self.assertEqual(response.status_code, 400, response.data)
        self.assertFalse(ExecutionVersionContract.objects.exists())

    def test_same_publish_request_replays_once_but_different_sha_conflicts(self):
        response, payload = self.publish()
        before = PlatformLifecycleAuditEvent.objects.count()
        replay = self.client.post(self.url + "publish/", payload, format="json")
        self.assertEqual(replay.status_code, 200, replay.data)
        self.assertEqual(replay.data["revision"], response.data["revision"])
        self.assertEqual(PlatformLifecycleAuditEvent.objects.count(), before)
        stale = self.client.post(self.url + "publish/", {**payload, "expected_definition_sha256": "0" * 64}, format="json")
        self.assertEqual(stale.status_code, 409, stale.data)

    def test_missing_mapping_returns_field_error_without_freezing(self):
        config = deepcopy(self.config)
        del config["request_field_map"]["duration_seconds"]
        self.write_contract(self.provider, "driver_config", config)
        response = self.client.get(self.url + "input-contract/")
        self.assertEqual(response.status_code, 400, response.data)
        self.assertIn("duration_seconds", str(response.data))
        self.assertFalse(ExecutionVersionContract.objects.exists())

    def test_published_read_is_frozen_even_when_current_provider_is_unavailable(self):
        _, payload = self.publish()
        frozen = self.read()
        self.write_contract(self.provider, "driver_config", {**self.config, "timeout_seconds": 29})
        self.assertEqual(self.read(), frozen)
        response = self.client.post(self.url + "publish/", payload, format="json")
        self.assertEqual(response.status_code, 409, response.data)
        self.assertEqual(response.data["error"]["code"], "execution_target_drift")
        ProviderProfile.objects.filter(pk=self.provider.pk).update(driver_config={})
        self.assertEqual(self.read(), frozen)

    def test_retirement_requires_reason_and_replay_cannot_change_it(self):
        _, payload = self.publish()
        payload["expected_revision"] = self.read()["revision"]
        for reason in ("", "  ", "x" * 192):
            response = self.client.post(self.url + "retire/", {**payload, "reason": reason}, format="json")
            self.assertEqual(response.status_code, 400, response.data)
        first = self.client.post(self.url + "retire/", {**payload, "reason": "替换配置"}, format="json")
        self.assertEqual(first.status_code, 200, first.data)
        replay = self.client.post(self.url + "retire/", {**payload, "reason": "替换配置"}, format="json")
        self.assertEqual(replay.status_code, 200, replay.data)
        changed = self.client.post(self.url + "retire/", {**payload, "reason": "另一原因"}, format="json")
        self.assertEqual(changed.status_code, 409, changed.data)

    def test_business_execution_is_not_a_registry_action_and_smoke_requires_explicit_request(self):
        for action in ("run", "submit"):
            self.assertEqual(self.client.post(self.url + action + "/", {}, format="json").status_code, 404)
        # The formal Smoke action now exists, but an empty request cannot infer
        # fixture, definition, budget or authorization from mutable defaults.
        self.assertEqual(self.client.post(self.url + "smoke-test/", {}, format="json").status_code, 400)

    def test_missing_frozen_contract_is_a_configuration_conflict_not_silent_rebuild(self):
        _, payload = self.publish()
        ExecutionVersionContract.objects.filter(version=self.version).delete()
        for response in (self.client.get(self.url + "input-contract/"),
                         self.client.post(self.url + "publish/", payload, format="json")):
            self.assertEqual(response.status_code, 409, response.data)
            self.assertEqual(response.data["error"]["code"], "execution_definition_conflict")
        self.assertFalse(ExecutionVersionContract.objects.exists())
