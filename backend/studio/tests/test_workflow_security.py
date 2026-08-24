from django.test import TestCase
from unittest.mock import patch

from studio.models import PromptArtifact, WorkflowTemplate
from studio.services.h3_prompts import H3PromptOptions, build_h3_video_prompt
from studio.services.reviewed_workflows import REVIEWED_WORKFLOW_VERSIONS, VIDEO_WORKFLOW
from studio.services.system_workflow_contracts import video_output_contract
from studio.services.workflow import WorkflowBuildError, build_workflow
from studio.tests.factories import build_story


class WorkflowSecurityTests(TestCase):
    def setUp(self):
        self.user, self.project, _, _, self.episode, self.shot, _ = build_story()
        self.native_template = WorkflowTemplate.objects.create(
            owner=None,
            name="Reviewed MiniMax H3 T2VA security fixture",
            slug="minimax-h3-base-video",
            version=REVIEWED_WORKFLOW_VERSIONS["minimax-h3-base-video"],
            provider="comfyui",
            output_kind="video",
            model="minimax-h3",
            definition=VIDEO_WORKFLOW,
            input_schema={
                "workflow_mode": "text_to_video",
                "output_contract": video_output_contract(VIDEO_WORKFLOW),
                "h3_modes": ["T2VA"],
                "prompt_template_keys": ["h3_t2v_text_v1"],
                "reference_policy": "forbidden",
            },
            is_system=True,
            is_active=True,
        )

    def _prompt(self):
        result = build_h3_video_prompt(
            self.project,
            [self.shot],
            H3PromptOptions(
                duration_overrides={str(self.shot.id): 5000}, language_gate_mode="warn",
            ),
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

        workflow, _, _ = build_workflow(self.native_template, prompt, self.project)
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
