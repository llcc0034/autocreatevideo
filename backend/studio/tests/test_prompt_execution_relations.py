"""Prompt lifecycle follows explicit execution references, not model-family selectors."""
from django.utils import timezone
from rest_framework.test import APIClient
from django.test import TestCase

from studio.models import ModelExecutionInstance, ModelExecutionInstanceVersion, PromptProfileVersion
from studio.tests import test_workflow_configuration_links as fixtures


class PromptExecutionRelationsTests(TestCase):
    @classmethod
    def setUpTestData(cls):
        fixtures.WorkflowConfigurationLinksTests._resource_data.__func__(cls)
        # Narrow lifecycle fixture; this is not a published execution admission test.
        cls.prompt.status = "published"
        cls.prompt.save(update_fields=("status", "updated_at"))
        cls.instance = ModelExecutionInstance.objects.create(key="prompt-owner", name="明确策略实例")
        cls.version = ModelExecutionInstanceVersion.objects.create(
            instance=cls.instance, version=1, deployment=cls.deployment,
            operation="image.generate", prompt_mode="profile", prompt_profile_version=cls.prompt,
        )

    def retire(self):
        client = APIClient()
        client.force_authenticate(self.user)
        return client.post(f"/api/v1/prompt-profile-versions/{self.prompt.id}/retire/", {}, format="json")

    def publish_reference(self):
        ModelExecutionInstanceVersion.objects.filter(pk=self.version.pk).update(
            status="published", published_at=timezone.now(), published_by=self.user,
        )

    def test_enabled_published_execution_blocks_retirement(self):
        self.publish_reference()
        response = self.retire()
        self.assertEqual(response.status_code, 400, response.data)
        self.assertIn("执行实例", str(response.data))
        self.prompt.refresh_from_db()
        self.assertEqual(self.prompt.status, "published")

    def test_draft_reference_does_not_block_retirement(self):
        response = self.retire()
        self.assertEqual(response.status_code, 200, response.data)
        self.version.refresh_from_db()
        self.assertEqual(self.version.prompt_profile_version_id, self.prompt.id)

    def test_disabled_execution_keeps_reference_without_blocking_retirement(self):
        self.publish_reference()
        ModelExecutionInstance.objects.filter(pk=self.instance.pk).update(is_active=False)
        response = self.retire()
        self.assertEqual(response.status_code, 200, response.data)
        self.version.refresh_from_db()
        self.assertEqual(self.version.prompt_profile_version_id, self.prompt.id)

    def test_retired_execution_does_not_block_retirement(self):
        self.publish_reference()
        ModelExecutionInstanceVersion.objects.filter(pk=self.version.pk).update(status="retired")
        response = self.retire()
        self.assertEqual(response.status_code, 200, response.data)

    def test_same_family_is_not_a_reference_to_another_prompt_version(self):
        self.publish_reference()
        other = PromptProfileVersion.objects.create(
            profile_key=self.prompt.profile_key, version=2, name="其他版本",
            model_family=self.prompt.model_family, operation=self.prompt.operation, status="published",
        )
        client = APIClient()
        client.force_authenticate(self.user)
        response = client.post(f"/api/v1/prompt-profile-versions/{other.id}/retire/", {}, format="json")
        self.assertEqual(response.status_code, 200, response.data)

    def test_old_binding_api_is_absent_not_a_redirect_or_alias(self):
        client = APIClient()
        client.force_authenticate(self.user)
        for suffix in ("", f"{self.prompt.id}/"):
            for method in ("get", "post", "patch", "delete"):
                with self.subTest(method=method, suffix=suffix):
                    response = getattr(client, method)(f"/api/v1/prompt-profile-bindings/{suffix}", {}, format="json")
                    self.assertEqual(response.status_code, 404)
