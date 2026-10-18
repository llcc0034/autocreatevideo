from copy import deepcopy
from unittest.mock import patch

from django.contrib.auth import get_user_model
from django.db import connection
from django.test import TestCase
from django.test.utils import CaptureQueriesContext
from rest_framework.test import APIClient

from studio.models import ModelExecutionInstance, ModelExecutionInstanceVersion, PlatformLifecycleAuditEvent


class ExecutionInstanceApiTests(TestCase):
    @classmethod
    def setUpTestData(cls):
        from studio.tests.test_execution_instances import ExecutionRegistryTests

        # Reuse resource-only fixtures. Graph verification is isolated below;
        # these API tests do not certify the synthetic Workflow for execution.
        ExecutionRegistryTests.setUpTestData.__func__(cls)
        cls.member = get_user_model().objects.create_user(username="execution-api-member")

    def setUp(self):
        self.client = APIClient()
        self.client.force_authenticate(self.actor)
        verifier = patch("studio.services.execution_instances.verify_workflow_version_integrity")
        verifier.start()
        self.addCleanup(verifier.stop)
        external = patch("studio.services.provider_registry._request_json", side_effect=AssertionError("Registration must not execute"))
        external.start()
        self.addCleanup(external.stop)
        self.instances_url = "/api/v1/model-execution-instances/"
        self.versions_url = "/api/v1/model-execution-instance-versions/"

    def payload(self, **changes):
        value = {
            "instance_id": str(self.instance.pk), "expected_revision": 1,
            "deployment_id": str(self.deployment.pk), "operation": "video.reference_to_video", "mode": "Ref2VA",
            "prompt_mode": "profile", "prompt_profile_version_id": str(self.profile.pk),
            "workflow_version_id": str(self.workflow.pk),
        }
        return {**value, **changes}

    def test_selection_catalog_is_readonly_and_uses_schema_and_exact_target(self):
        before = (ModelExecutionInstanceVersion.objects.count(), PlatformLifecycleAuditEvent.objects.count())
        url = self.versions_url + "selection-options/"
        base = self.client.get(url)
        self.assertEqual(base.status_code, 200, base.data)
        self.assertIsNone(base.data["target"])
        self.assertEqual([row["value"] for row in base.data["prompt_modes"]], ["profile", "manual", "none"])
        response = self.client.get(url, {"deployment_id": str(self.deployment.pk), "workflow_version_id": str(self.workflow.pk)})
        self.assertEqual(response.status_code, 200, response.data)
        target = response.data["target"]
        self.assertEqual(target["operations"], [{"value": "video.reference_to_video", "label": "参考生视频", "output_parser_keys": []}])
        self.assertEqual(target["workflow"]["id"], str(self.workflow.pk))
        self.assertTrue(target["workflow_required"])
        self.assertIn("Ref2VA", [row["value"] for row in target["modes"]])
        for hidden in ("runtime_config", "base_url", "can_submit", "credential"):
            self.assertNotIn(hidden, str(response.data))
        self.assertNotIn("definition_sha256", response.data)
        self.assertEqual(before, (ModelExecutionInstanceVersion.objects.count(), PlatformLifecycleAuditEvent.objects.count()))

    def test_selection_catalog_rejects_incomplete_unknown_and_unauthorized_queries(self):
        url = self.versions_url + "selection-options/"
        for query in ({"workflow_version_id": str(self.workflow.pk)}, {"deployment_id": "not-uuid"}, {"operation": "video.generate"}):
            self.assertEqual(self.client.get(url, query).status_code, 400)
        self.assertEqual(self.client.get(url + "?deployment_id=x&deployment_id=y").status_code, 400)
        self.client.force_authenticate(self.member)
        self.assertEqual(self.client.get(url).status_code, 403)

    def test_selection_catalog_never_substitutes_a_workflow_from_another_deployment(self):
        self.workflow.validated_deployment = None
        self.workflow.save(update_fields=("validated_deployment",))
        response = self.client.get(self.versions_url + "selection-options/", {
            "deployment_id": str(self.deployment.pk), "workflow_version_id": str(self.workflow.pk),
        })
        self.assertEqual(response.status_code, 400, response.data)

    def create_draft(self, **changes):
        response = self.client.post(self.versions_url, self.payload(**changes), format="json")
        self.assertEqual(response.status_code, 201, response.data)
        return response.data

    def test_both_collections_require_administrator(self):
        for user in (None, self.member):
            self.client.force_authenticate(user)
            for url in (self.instances_url, self.versions_url):
                with self.subTest(user=user, url=url):
                    self.assertIn(self.client.get(url).status_code, (401, 403))
                    self.assertIn(self.client.post(url, {}, format="json").status_code, (401, 403))

    def test_instance_create_update_revision_and_safe_audit(self):
        response = self.client.post(self.instances_url, {"key": "new-execution", "name": "New execution"}, format="json")
        self.assertEqual(response.status_code, 201, response.data)
        identity = response.data["id"]
        url = f"{self.instances_url}{identity}/"
        response = self.client.patch(url, {"expected_revision": 1, "name": "Renamed", "is_active": False}, format="json")
        self.assertEqual(response.status_code, 200, response.data)
        self.assertEqual(response.data["revision"], 2)
        self.assertFalse(response.data["is_active"])
        stale = self.client.patch(url, {"expected_revision": 1, "name": "Stale"}, format="json")
        self.assertEqual(stale.status_code, 409)
        self.assertEqual(stale.data["error"]["code"], "execution_definition_conflict")
        self.assertEqual(ModelExecutionInstance.objects.get(pk=identity).name, "Renamed")
        self.assertEqual(PlatformLifecycleAuditEvent.objects.filter(object_id=identity).count(), 2)

    def test_readonly_and_unknown_fields_are_not_ignored(self):
        for extra in ({"status": "published"}, {"key": "overwrite"}, {"definition": {}}, {"binding_id": "ignored"}):
            response = self.client.patch(f"{self.instances_url}{self.instance.pk}/", {"expected_revision": 1, **extra}, format="json")
            self.assertEqual(response.status_code, 400, response.data)
        for extra in ({"status": "published"}, {"version": 99}, {"target_fingerprint": "a" * 64}):
            response = self.client.post(self.versions_url, self.payload(**extra), format="json")
            self.assertEqual(response.status_code, 400, response.data)
        self.assertFalse(ModelExecutionInstanceVersion.objects.exists())

    def test_revision_is_required_and_not_coerced(self):
        for revision in (None, True, "1", 1.0, 0):
            response = self.client.post(self.versions_url, self.payload(expected_revision=revision), format="json")
            self.assertEqual(response.status_code, 400, response.data)
        response = self.client.patch(f"{self.instances_url}{self.instance.pk}/", {"name": "Missing revision"}, format="json")
        self.assertEqual(response.status_code, 400)

    def test_version_keeps_execution_operation_separate_from_prompt_purpose(self):
        draft = self.create_draft()
        self.assertEqual(draft["operation"], "video.reference_to_video")
        self.assertEqual(draft["prompt_operation"], "video.generate")
        self.assertEqual(draft["status"], "draft")
        self.assertIsNone(draft["published_at"])
        self.assertNotIn("can_submit", draft)
        self.assertEqual(draft["configuration"]["inputs"], [])

    def test_initial_children_are_atomic_with_parent_version_allocation(self):
        invalid = [{
            "key": "sample", "value_type": "integer", "minimum": 0, "maximum": 10,
            "enum_values": [], "step": None, "remainder": None, "policy": "locked",
            "default_value": None, "locked_value": True, "seed_policy": None,
        }]
        before = PlatformLifecycleAuditEvent.objects.count()
        response = self.client.post(self.versions_url, self.payload(parameters=invalid), format="json")
        self.assertEqual(response.status_code, 400, response.data)
        self.assertFalse(ModelExecutionInstanceVersion.objects.exists())
        self.instance.refresh_from_db()
        self.assertEqual(self.instance.revision, 1)
        self.assertEqual(PlatformLifecycleAuditEvent.objects.count(), before)
        self.assertEqual(self.create_draft()["version"], 1)

    def test_detail_and_list_are_readonly_and_list_omits_configuration_body(self):
        draft = self.create_draft()
        before = PlatformLifecycleAuditEvent.objects.count()
        detail = self.client.get(f"{self.versions_url}{draft['id']}/")
        self.assertEqual(detail.status_code, 200)
        self.assertEqual(detail.data["configuration"], draft["configuration"])
        listing = self.client.get(self.versions_url, {"instance": str(self.instance.pk), "page_size": 1})
        self.assertEqual(listing.status_code, 200, listing.data)
        self.assertEqual(listing.data["count"], 1)
        self.assertNotIn("configuration", listing.data["results"][0])
        self.assertEqual(PlatformLifecycleAuditEvent.objects.count(), before)

    def test_list_loads_only_display_fields_and_does_not_grow_queries_per_row(self):
        draft = self.create_draft()
        self.client.get(self.versions_url)  # Resolve configuration/pagination caches first.
        with CaptureQueriesContext(connection) as first:
            self.assertEqual(self.client.get(self.versions_url).status_code, 200)
        for _ in range(3):
            response = self.client.post(f"{self.versions_url}{draft['id']}/clone-successor/", {
                "expected_revision": 1, "expected_definition_sha256": None,
            }, format="json")
            self.assertEqual(response.status_code, 201, response.data)
        with CaptureQueriesContext(connection) as second:
            response = self.client.get(self.versions_url)
            self.assertEqual(response.data["count"], 4)
        self.assertEqual(len(first), len(second))
        queries = [row["sql"] for row in second if 'FROM "studio_modelexecutioninstanceversion"' in row["sql"]]
        for sql in queries:
            for column in ('"source_projection_config"', '"driver_config"', '"specification"', '"input_contract"', '"channel_limits"'):
                self.assertNotIn(column, sql)

    def test_create_requires_explicit_seed_metadata_for_parameter_rows(self):
        parameter = {"key": "integer.value", "value_type": "integer", "minimum": 0, "maximum": 10,
                     "enum_values": [], "step": None, "remainder": None, "policy": "overridable",
                     "default_value": 1, "locked_value": None}
        response = self.client.post(self.versions_url, self.payload(parameters=[parameter]), format="json")
        self.assertEqual(response.status_code, 400)
        self.assertIn("seed_policy", str(response.data))
        self.assertFalse(ModelExecutionInstanceVersion.objects.exists())

    def test_patch_does_not_change_family_or_lifecycle_and_rejects_stale_revision(self):
        draft = self.create_draft()
        url = f"{self.versions_url}{draft['id']}/"
        response = self.client.patch(url, {"expected_revision": 1, "prompt_mode": "manual", "prompt_profile_version_id": None}, format="json")
        self.assertEqual(response.status_code, 200, response.data)
        self.assertIsNone(response.data["prompt_operation"])
        self.assertEqual(response.data["revision"], 2)
        self.assertEqual(self.client.patch(url, {"expected_revision": 1, "mode": ""}, format="json").status_code, 409)
        self.assertEqual(self.client.patch(url, {"expected_revision": 2, "instance_id": str(self.instance.pk)}, format="json").status_code, 400)

    def test_clone_is_a_new_draft_and_requires_explicit_source_revision(self):
        original = self.create_draft()
        url = f"{self.versions_url}{original['id']}/clone-successor/"
        response = self.client.post(url, {"expected_revision": 1, "expected_definition_sha256": None}, format="json")
        self.assertEqual(response.status_code, 201, response.data)
        self.assertEqual(response.data["version"], 2)
        self.assertEqual(response.data["status"], "draft")
        self.assertNotEqual(response.data["id"], original["id"])
        self.assertEqual(response.data["configuration"], original["configuration"])
        stale = self.client.post(url, {"expected_revision": 5, "expected_definition_sha256": None}, format="json")
        self.assertEqual(stale.status_code, 409)

    def test_no_direct_delete_put_or_implicit_publish(self):
        draft = self.create_draft()
        for url in (f"{self.instances_url}{self.instance.pk}/", f"{self.versions_url}{draft['id']}/"):
            self.assertEqual(self.client.delete(url).status_code, 405)
            self.assertEqual(self.client.put(url, {}, format="json").status_code, 405)
        response = self.client.post(f"{self.versions_url}{draft['id']}/publish/", {}, format="json")
        self.assertEqual(response.status_code, 400, response.data)
        self.assertEqual(ModelExecutionInstanceVersion.objects.get(pk=draft["id"]).status, "draft")

    def test_failed_partial_configuration_does_not_save_other_changes(self):
        draft = self.create_draft()
        original = deepcopy(draft["configuration"])
        response = self.client.patch(f"{self.versions_url}{draft['id']}/", {
            "expected_revision": 1, "prompt_mode": "manual", "prompt_profile_version_id": None,
            "inputs": [{"key": "missing-contract"}],
        }, format="json")
        self.assertEqual(response.status_code, 400)
        current = self.client.get(f"{self.versions_url}{draft['id']}/")
        self.assertEqual(current.data["configuration"], original)
