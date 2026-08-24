from django.test import TestCase

from studio.models import PromptArtifact, WorkflowTemplate
from studio.services.h3_prompts import H3PromptOptions, build_h3_video_prompt
from studio.services.workflow import build_workflow
from studio.tests.factories import build_story


class WorkflowTests(TestCase):
    def test_injection_preserves_number_bool_and_list_types(self):
        user, project, _, _, episode, shot1, _ = build_story()
        result = build_h3_video_prompt(
            project,
            [shot1],
            H3PromptOptions(
                duration_overrides={str(shot1.id): 5000}, language_gate_mode="warn",
            ),
        )
        prompt = PromptArtifact.objects.create(
            project=project, episode=episode, shot=shot1, created_by=user,
            prompt_type="video", model="minimax-h3", h3_mode="T2VA", revision=1,
            total_duration_ms=result.total_duration_ms, prompt_text=result.text,
            structured_payload=result.payload, consistency_snapshot=result.consistency_snapshot,
            content_hash=result.content_hash,
        )
        template = WorkflowTemplate.objects.create(
            name="test", slug="test", output_kind="video",
            definition={"1": {"class_type": "SaveImage", "inputs": {
                "prompt": "${PROMPT}", "seed": "${SEED}", "frames": "${FRAMES}",
                "audio": "${AUDIO_ENABLED}", "refs": "${REFERENCE_IMAGES}",
                "label": "run-${MODEL}",
            }}},
        )
        workflow, values, digest = build_workflow(template, prompt, project)
        inputs = workflow["1"]["inputs"]
        self.assertIsInstance(inputs["seed"], int)
        self.assertIsInstance(inputs["frames"], int)
        self.assertIs(inputs["audio"], True)
        self.assertIsInstance(inputs["refs"], list)
        self.assertEqual(inputs["label"], "run-minimax-h3")
        self.assertEqual(len(digest), 64)
