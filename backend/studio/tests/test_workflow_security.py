from django.contrib.auth import get_user_model
from django.core.management import call_command
from django.test import TestCase
from rest_framework.test import APIClient
from unittest.mock import patch

from studio.models import GeneratedWorkflow, PromptArtifact, WorkflowTemplate
from studio.services.h3_prompts import H3PromptOptions, build_h3_video_prompt
from studio.services.workflow import WorkflowBuildError, build_workflow
from studio.tests.factories import build_story


class WorkflowSecurityTests(TestCase):
    def setUp(self):
        call_command("seed_data", verbosity=0)
        self.user, self.project, _, _, self.episode, self.shot, _ = build_story()
        self.client = APIClient()
        self.client.force_authenticate(self.user)

    def _prompt(self):
        result = build_h3_video_prompt(
            self.project,
            [self.shot],
            H3PromptOptions(duration_overrides={str(self.shot.id): 5000}),
        )
        return PromptArtifact.objects.create(
            project=self.project, episode=self.episode, shot=self.shot,
            created_by=self.user, prompt_type="video", model="minimax-h3",
            h3_mode="T2VA", include_audio=True,
            total_duration_ms=result.total_duration_ms, prompt_text=result.text,
            structured_payload=result.payload,
            consistency_snapshot=result.consistency_snapshot,
            content_hash=result.content_hash,
        )

    def _legacy_template(self):
        return WorkflowTemplate.objects.create(
            owner=self.user, name="legacy", slug="legacy", output_kind="video",
            definition={"1": {"class_type": "SaveImage", "inputs": {"images": "${PROMPT}"}}},
        )

    def test_non_staff_templates_are_read_only_but_staff_can_write(self):
        payload = {
            "name": "unreviewed", "slug": "unreviewed", "output_kind": "video",
            "definition": {"1": {"class_type": "SaveImage", "inputs": {}}},
        }
        self.assertEqual(self.client.post("/api/v1/workflow-templates/", payload, format="json").status_code, 403)
        global_template = WorkflowTemplate.objects.get(slug="minimax-h3-base-video", owner=None)
        self.assertEqual(self.client.patch(
            f"/api/v1/workflow-templates/{global_template.id}/", {"name": "nope"}, format="json"
        ).status_code, 403)
        self.assertEqual(self.client.delete(f"/api/v1/workflow-templates/{global_template.id}/").status_code, 403)

        staff = get_user_model().objects.create_user(
            username="operator", password="StrongPass123!", is_staff=True,
        )
        self.client.force_authenticate(staff)
        self.assertEqual(self.client.post("/api/v1/workflow-templates/", payload, format="json").status_code, 201)

    def test_legacy_user_template_cannot_generate_build_production_or_enqueue(self):
        prompt = self._prompt()
        legacy = self._legacy_template()
        generated = self.client.post("/api/v1/workflows/generate/", {
            "prompt_artifact_id": str(prompt.id), "template_id": str(legacy.id),
        }, format="json")
        self.assertEqual(generated.status_code, 404)

        production = self.client.post(f"/api/v1/projects/{self.project.id}/build-production/", {
            "template_id": str(legacy.id), "enqueue": False,
        }, format="json")
        self.assertEqual(production.status_code, 404)

        legacy_workflow = GeneratedWorkflow.objects.create(
            project=self.project, episode=self.episode, prompt_artifact=prompt,
            template=legacy, created_by=self.user, workflow_json=legacy.definition,
            injected_values={}, content_hash="a" * 64,
        )
        enqueue = self.client.post("/api/v1/tasks/", {
            "generated_workflow_id": str(legacy_workflow.id),
        }, format="json")
        self.assertEqual(enqueue.status_code, 400)

    def test_dangerous_nodes_are_rejected_and_native_h3_is_accepted(self):
        prompt = self._prompt()
        dangerous = WorkflowTemplate.objects.create(
            owner=None, is_system=True, name="unsafe", slug="unsafe", output_kind="video",
            definition={"1": {"class_type": "CheckpointSave", "inputs": {}}},
        )
        with self.assertRaisesRegex(WorkflowBuildError, "Dangerous Comfy node"):
            build_workflow(dangerous, prompt, self.project)

        network = WorkflowTemplate.objects.create(
            owner=None, is_system=True, name="network", slug="network", output_kind="video",
            definition={"1": {"class_type": "PartnerAPIRequest", "inputs": {}}},
        )
        with self.assertRaisesRegex(WorkflowBuildError, "Dangerous Comfy node"):
            build_workflow(network, prompt, self.project)

        native = WorkflowTemplate.objects.get(owner=None, slug="minimax-h3-base-video")
        workflow, _, _ = build_workflow(native, prompt, self.project)
        self.assertEqual(workflow["5"]["class_type"], "MiniMaxH3ImageToVideo")

    def test_server_extra_allowlist_cannot_bypass_dangerous_node_block(self):
        prompt = self._prompt()
        reviewed_extension = WorkflowTemplate.objects.create(
            owner=None, is_system=True, name="extension", slug="extension", output_kind="video",
            definition={"1": {"class_type": "AuditedExtensionNode", "inputs": {}}},
        )
        blocked_extension = WorkflowTemplate.objects.create(
            owner=None, is_system=True, name="blocked-extension", slug="blocked-extension", output_kind="video",
            definition={"1": {"class_type": "CheckpointSave", "inputs": {}}},
        )
        with patch.dict(
            "os.environ",
            {"COMFYUI_EXTRA_ALLOWED_NODE_TYPES": "AuditedExtensionNode,CheckpointSave"},
        ):
            workflow, _, _ = build_workflow(reviewed_extension, prompt, self.project)
            self.assertEqual(workflow["1"]["class_type"], "AuditedExtensionNode")
            with self.assertRaisesRegex(WorkflowBuildError, "Dangerous Comfy node"):
                build_workflow(blocked_extension, prompt, self.project)
