from copy import deepcopy
from uuid import uuid4

from django.contrib.auth import get_user_model
from django.db import connection
from django.test import TestCase
from django.test.utils import CaptureQueriesContext
from django.utils import timezone
from rest_framework.test import APIClient

from studio.models import (
    ModelDeployment, ModelProfileVersion, ModelExecutionInstance, ModelExecutionInstanceVersion, PromptProfileVersion,
    ProviderProfile, WorkflowProjector, WorkflowProjectorVersion,
    WorkflowTemplate, WorkflowTemplateVersion,
)
from studio.services.workflow_configuration_links import workflow_configuration_links


class WorkflowConfigurationLinksTests(TestCase):
    @classmethod
    def _resource_data(cls):
        cls.user = get_user_model().objects.create_user(username="links-staff", is_staff=True)
        cls.provider = ProviderProfile.objects.create(key="links-provider", name="ComfyUI", driver_type="comfyui")
        cls.model = ModelProfileVersion.objects.create(
            profile_key="links-model", version=1, provider=cls.provider,
            model_identifier="model", modality="image", specification={"model_family": "links-family"},
        )
        cls.other_model = ModelProfileVersion.objects.create(
            profile_key="links-model", version=2, provider=cls.provider,
            model_identifier="model", modality="image", specification={"model_family": "links-family"},
        )
        cls.deployment = ModelDeployment.objects.create(key="links-deployment", name="本机图片实例", profile_version=cls.model)
        ModelDeployment.objects.create(key="links-retired", name="历史实例", profile_version=cls.model, status="retired")
        cls.projector = WorkflowProjector.objects.create(key="links.projector", name="图片接线方案")
        cls.projector_version = WorkflowProjectorVersion.objects.create(
            projector=cls.projector, version=1, engine_key="generic.fixed", engine_version=1,
            definition_sha256="a" * 64, status="retired", is_current=False,
        )
        WorkflowProjectorVersion.objects.create(
            projector=cls.projector, version=2, engine_key="generic.fixed", engine_version=1,
            definition_sha256="b" * 64, status="published", is_current=True,
        )
        cls.identity = {
            "id": cls.projector.key, "version": 1, "version_id": str(cls.projector_version.id),
            "definition_sha256": "a" * 64, "provider_driver_type": "comfyui", "operation": "workflow.import",
            "output_kind": "", "engine": {"key": "generic.fixed", "version": 1},
        }
        cls.template = WorkflowTemplate.objects.create(slug="links-workflow", name="图片工作流", output_kind="image")
        cls.workflow = WorkflowTemplateVersion.objects.create(
            template=cls.template, version=1, model_profile_version=cls.model, provider=cls.provider,
            validated_deployment=cls.deployment, input_contract={"contract_projector": cls.identity},
        )
        cls.prompt = PromptProfileVersion.objects.create(
            profile_key="links-prompt", version=1, name="多图编辑策略", model_family="links-family", operation="image.generate",
        )
    @classmethod
    def setUpTestData(cls):
        cls._resource_data()
        cls.execution_family = ModelExecutionInstance.objects.create(key='links-execution', name='图像执行实例')
        cls.executions = [ModelExecutionInstanceVersion.objects.create(instance=cls.execution_family, version=index+1,
            deployment=cls.deployment, workflow_version=cls.workflow, prompt_mode='profile', prompt_profile_version=cls.prompt,
            operation='image.generate', status='published' if index < 5 else 'draft',
            published_at=timezone.now() if index < 5 else None, published_by=cls.user if index < 5 else None)
            for index in range(6)]
        # Same model/deployment without this workflow must never enter the list.
        ModelExecutionInstanceVersion.objects.create(instance=cls.execution_family, version=7,
            deployment=cls.deployment, prompt_mode='none', operation='image.generate')

    def test_exact_execution_links_are_bounded_and_do_not_select_a_runtime_route(self):
        links = workflow_configuration_links([self.workflow.id])[str(self.workflow.id)]
        self.assertEqual(links["model"]["instance_count"], 2)
        self.assertEqual(links["validated_deployment"]["name"], "本机图片实例")
        self.assertEqual(links["projector"]["version_id"], str(self.projector_version.id))
        self.assertEqual(links["projector"]["status"], "retired")
        self.assertFalse(links["projector"]["is_current"])
        self.assertEqual(links["execution_instances"]["total"], 6)
        self.assertEqual(links["execution_instances"]["active_total"], 5)
        preview = links["execution_instances"]["preview"]
        self.assertEqual(len(preview), 3)
        self.assertEqual([item['version'] for item in preview], [5, 4, 3])
        self.assertTrue(all(item["status"] == 'published' for item in preview))
        self.assertNotIn('prompt_bindings', links)

    def test_missing_and_invalid_projector_never_fall_back_to_current_version(self):
        for identity in ("invalid", {**self.identity, "version_id": str(uuid4())}, {**self.identity, "definition_sha256": "b" * 64}):
            WorkflowTemplateVersion.objects.filter(pk=self.workflow.id).update(input_contract={"contract_projector": identity})
            link = workflow_configuration_links([self.workflow.id])[str(self.workflow.id)]["projector"]
            self.assertEqual(link["state"], "invalid")
            self.assertIsNone(link["version_id"])
        WorkflowTemplateVersion.objects.filter(pk=self.workflow.id).update(input_contract={})
        self.assertEqual(workflow_configuration_links([self.workflow.id])[str(self.workflow.id)]["projector"]["state"], "not_recorded")

    def test_unvalidated_draft_does_not_claim_any_model_instance(self):
        WorkflowTemplateVersion.objects.filter(pk=self.workflow.id).update(validated_deployment=None)
        link = workflow_configuration_links([self.workflow.id])[str(self.workflow.id)]
        self.assertIsNone(link["validated_deployment"])
        self.assertEqual(link["model"]["instance_count"], 2)
        WorkflowTemplateVersion.objects.filter(pk=self.workflow.id).update(model_profile_version=None)
        link = workflow_configuration_links([self.workflow.id])[str(self.workflow.id)]
        self.assertIsNone(link["model"])
        self.assertEqual(link["execution_instances"]['total'], 6)  # FK relation remains explicit, not a validity verdict.

    def test_missing_family_does_not_change_exact_execution_relations(self):
        ModelProfileVersion.objects.filter(pk=self.model.id).update(specification={})
        links = workflow_configuration_links([self.workflow.id])[str(self.workflow.id)]
        self.assertEqual(links["execution_instances"]["total"], 6)
        self.assertEqual(links["execution_instances"]["active_total"], 5)

    def test_drafts_retired_and_disabled_versions_do_not_hide_published_choices(self):
        ModelExecutionInstanceVersion.objects.filter(pk__in=[row.pk for row in self.executions[2:5]]).update(status='retired')
        link = workflow_configuration_links([self.workflow.id])[str(self.workflow.id)]["execution_instances"]
        self.assertEqual(link["total"], 6)
        self.assertEqual(link["active_total"], 2)
        self.assertEqual([item['version'] for item in link['preview']], [2, 1])
        ModelExecutionInstance.objects.filter(pk=self.execution_family.pk).update(is_active=False)
        link = workflow_configuration_links([self.workflow.id])[str(self.workflow.id)]["execution_instances"]
        self.assertEqual(link["total"], 6)
        self.assertEqual(link["active_total"], 0)
        self.assertEqual(link["preview"], [])

    def test_batch_queries_are_constant_and_payload_free(self):
        ids = [self.workflow.id]
        for version in range(2, 12):
            ids.append(WorkflowTemplateVersion.objects.create(
                template=self.template, version=version, model_profile_version=self.model,
                input_contract={"contract_projector": deepcopy(self.identity)},
            ).id)
        with CaptureQueriesContext(connection) as one:
            workflow_configuration_links(ids[:1])
        with CaptureQueriesContext(connection) as many:
            result = workflow_configuration_links(ids)
        self.assertEqual(len(result), 11)
        self.assertEqual(len(one), len(many))
        self.assertEqual(len(many), 5)
        for query in many:
            self.assertNotIn('"graph"', query["sql"])
            self.assertNotIn('"definition",', query["sql"])
            self.assertNotIn('"runtime_config"', query["sql"])

    def test_list_and_detail_use_same_links_and_staff_permission(self):
        client = APIClient()
        url = "/api/v1/workflow-template-versions/"
        self.assertIn(client.get(url).status_code, (401, 403))
        client.force_authenticate(self.user)
        WorkflowTemplateVersion.objects.create(template=self.template, version=2)
        first_page = client.get(url, {"template": str(self.template.id), "page_size": 1})
        self.assertEqual(first_page.status_code, 200, first_page.data)
        first_payload = first_page.data.get("data", first_page.data)
        self.assertEqual(first_payload["count"], 2)
        self.assertTrue(first_payload["next"])
        response = client.get(url, {"template": str(self.template.id), "page_size": 1, "page": 2})
        self.assertEqual(response.status_code, 200, response.data)
        payload = response.data.get("data", response.data)
        row = payload["results"][0]
        self.assertNotIn("input_contract", row)
        self.assertNotIn("graph", row)
        detail = client.get(f"{url}{self.workflow.id}/")
        self.assertEqual(detail.status_code, 200, detail.data)
        detail_payload = detail.data.get("data", detail.data)
        self.assertEqual(row["configuration_links"], detail_payload["configuration_links"])
