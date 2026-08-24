from pathlib import Path
from types import SimpleNamespace
import tempfile
from unittest.mock import patch

from django.core.management import call_command
from django.test import TestCase, override_settings

from studio.management.commands import run_generation_worker
from studio.models import (
    GeneratedWorkflow,
    GenerationTask,
    PromptArtifact,
    WorkflowTemplate,
)
from studio.services import creative_drafts, prompt_translation
from studio.services.local_qwen_lock import local_qwen_runtime_lock
from studio.tests.factories import build_story


class GenerationWorkerCommandTests(TestCase):
    def setUp(self):
        self.temp_dir = tempfile.TemporaryDirectory()
        self.addCleanup(self.temp_dir.cleanup)
        self.lock_path = str(Path(self.temp_dir.name) / "shared-local-ai.lock")
        self.settings_override = override_settings(LOCAL_QWEN_LOCK_PATH=self.lock_path)
        self.settings_override.enable()
        self.addCleanup(self.settings_override.disable)
        comfy_client = patch(
            "studio.management.commands.run_generation_worker.ComfyUIClient",
            return_value=SimpleNamespace(),
        )
        comfy_client.start()
        self.addCleanup(comfy_client.stop)

    @patch("studio.management.commands.run_generation_worker.claim_next_task", return_value=None)
    @patch("studio.management.commands.run_generation_worker.recover_stale_tasks", return_value=0)
    def test_startup_recovery_can_use_a_separate_threshold(self, recover, _claim):
        call_command(
            "run_generation_worker",
            once=True,
            recover_minutes=30,
            startup_recover_minutes=0,
        )

        self.assertEqual(recover.call_args.args[0], 0)

    @patch("studio.management.commands.run_generation_worker.claim_next_task", return_value=None)
    @patch("studio.management.commands.run_generation_worker.recover_stale_tasks", return_value=0)
    def test_startup_recovery_defaults_to_periodic_threshold(self, recover, _claim):
        call_command("run_generation_worker", once=True, recover_minutes=30)

        self.assertEqual(recover.call_args.args[0], 30)

    @patch("studio.management.commands.run_generation_worker.claim_next_task")
    @patch("studio.management.commands.run_generation_worker.recover_stale_tasks", return_value=0)
    def test_qwen_owner_prevents_worker_from_claiming(self, _recover, claim):
        with local_qwen_runtime_lock():
            call_command("run_generation_worker", once=True, recover_minutes=30)

        claim.assert_not_called()

    @patch("studio.management.commands.run_generation_worker.process_task")
    @patch("studio.management.commands.run_generation_worker.claim_next_task")
    @patch("studio.management.commands.run_generation_worker.recover_stale_tasks", return_value=0)
    def test_claimed_media_task_blocks_local_ai_during_processing(
        self, _recover, claim, process,
    ):
        user, project, _character, _scene, episode, shot, _next_shot = build_story(
            username="worker-resource-gate",
        )
        prompt = PromptArtifact.objects.create(
            project=project,
            episode=episode,
            shot=shot,
            created_by=user,
            prompt_type=PromptArtifact.PromptType.VIDEO,
            prompt_text="resource gate fixture",
            content_hash="a" * 64,
        )
        template = WorkflowTemplate.objects.create(
            name="worker resource gate",
            slug="worker-resource-gate",
            output_kind=WorkflowTemplate.OutputKind.VIDEO,
            definition={"1": {"class_type": "SaveImage", "inputs": {}}},
        )
        workflow = GeneratedWorkflow.objects.create(
            project=project,
            episode=episode,
            prompt_artifact=prompt,
            template=template,
            created_by=user,
            workflow_json=template.definition,
            content_hash="b" * 64,
        )
        task = GenerationTask.objects.create(
            owner=user,
            project=project,
            episode=episode,
            generated_workflow=workflow,
            kind=GenerationTask.Kind.VIDEO,
            status=GenerationTask.Status.RUNNING,
            attempts=1,
        )
        claim.return_value = task

        def assert_qwen_is_blocked(current, **_kwargs):
            translation_config = {
                "runtime_python": Path("python"),
                "runtime_script": Path("runtime.py"),
                "timeout_seconds": 30,
            }
            with patch("studio.services.prompt_translation.subprocess.run") as translate_run:
                with self.assertRaises(prompt_translation.PromptTranslationResourceBusy):
                    prompt_translation._run_runtime({"text": "测试"}, translation_config)
                translate_run.assert_not_called()
            with patch("studio.services.creative_drafts.subprocess.run") as creative_run:
                with self.assertRaises(creative_drafts.CreativeResourceBusy):
                    creative_drafts.ensure_creative_resources_available()
                creative_run.assert_not_called()
            current.status = GenerationTask.Status.SUCCEEDED
            return current

        process.side_effect = assert_qwen_is_blocked
        call_command("run_generation_worker", once=True, recover_minutes=30)

        process.assert_called_once()

    @patch("studio.management.commands.run_generation_worker.process_deferred_request")
    @patch("studio.management.commands.run_generation_worker.claim_next_deferred_request")
    @patch("studio.management.commands.run_generation_worker.claim_next_task", return_value=None)
    @patch("studio.management.commands.run_generation_worker.recover_stale_deferred_requests", return_value=0)
    @patch("studio.management.commands.run_generation_worker.recover_stale_tasks", return_value=0)
    def test_worker_does_not_claim_retired_shot_detail_queue(
        self,
        _recover_tasks,
        _recover_deferred,
        _claim_task,
        claim_deferred,
        process_deferred,
    ):
        deferred_request = SimpleNamespace(
            id="deferred-1",
            attempts=1,
            max_attempts=3,
            status="waiting_upstream",
        )
        processed = SimpleNamespace(id="deferred-1", status="materialized")
        claim_deferred.return_value = deferred_request
        process_deferred.return_value = processed

        call_command("run_generation_worker", once=True, recover_minutes=30)

        claim_deferred.assert_called_once()
        process_deferred.assert_called_once_with(deferred_request)
        self.assertFalse(hasattr(run_generation_worker, "claim_next_shot_detail_item"))
        self.assertFalse(hasattr(run_generation_worker, "process_shot_detail_item"))
        self.assertFalse(hasattr(run_generation_worker, "recover_stale_shot_detail_items"))
