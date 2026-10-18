"""Credential changes must not retarget an already prepared production task."""
from unittest.mock import patch

from django.test import TestCase, override_settings

from studio.models import ModelExecutionInstanceVersion
from studio.services.comfyui import ComfyUIError
from studio.services.execution_definitions import build_execution_definition
from studio.services.secret_store import rotate_secret_binding
from studio.services.task_queue import comfy_client_for_task
from studio.tests import test_execution_production_workflow_requests as fixtures
from studio.tests.secret_fixtures import create_test_secret, enable_test_keyring


@override_settings(AI_PROVIDER_ALLOWED_HOSTS=("127.0.0.1",))
class ExecutionTaskCredentialTests(TestCase):
    _create_version = fixtures.ProductionWorkflowRequestTests._create_version
    resolve = fixtures.ProductionWorkflowRequestTests.resolve

    def setUp(self):
        enable_test_keyring(self)

        def attach_credential_before_definition(version_id):
            version = ModelExecutionInstanceVersion.objects.select_related("deployment").get(pk=version_id)
            self.credential = create_test_secret(key="task-current-credential", actor=self.user,
                value="isolated-credential-v1")
            version.deployment.credential = self.credential
            version.deployment.save(update_fields=["credential", "updated_at"])
            return build_execution_definition(version_id)

        with patch("studio.tests.test_execution_workflow_request_media.build_execution_definition",
                side_effect=attach_credential_before_definition):
            fixtures.ProductionWorkflowRequestTests.setUp(self)
        self.task = self.attempt.generation_task

    def test_exact_credential_rotation_blocks_old_preparation_without_retargeting(self):
        original = self.preparation.contract.resolved_inputs_sha256
        with patch("studio.services.task_queue.ComfyUIClient") as peer:
            comfy_client_for_task(self.task)
            peer.assert_called_once()
            rotate_secret_binding(self.credential, secret_value="isolated-credential-v2",
                actor=self.user, reason="isolated_rotation")
            with self.assertRaises(ComfyUIError):
                comfy_client_for_task(self.task)
            peer.assert_called_once()
        self.preparation.contract.refresh_from_db()
        self.assertEqual(self.preparation.contract.resolved_inputs_sha256, original)
        self.assertEqual(self.task.submission_token, "")
