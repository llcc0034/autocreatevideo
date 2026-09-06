from django.test import SimpleTestCase
from unittest.mock import patch

from studio.services.workflow import WorkflowBuildError, validate_comfy_graph
from studio.tests.factories import h3_text_to_video_workflow_graph


class WorkflowSecurityTests(SimpleTestCase):
    def test_dangerous_nodes_are_rejected_and_native_h3_is_accepted(self):
        dangerous = {"1": {"class_type": "CheckpointSave", "inputs": {}}}
        with self.assertRaisesRegex(WorkflowBuildError, "Dangerous Comfy node"):
            validate_comfy_graph(dangerous)

        network = {"1": {"class_type": "PartnerAPIRequest", "inputs": {}}}
        with self.assertRaisesRegex(WorkflowBuildError, "Dangerous Comfy node"):
            validate_comfy_graph(network)

        workflow = h3_text_to_video_workflow_graph()
        validate_comfy_graph(workflow)
        self.assertEqual(workflow["5"]["class_type"], "MiniMaxH3ImageToVideo")

    def test_server_extra_allowlist_cannot_bypass_dangerous_node_block(self):
        reviewed_extension = {"1": {"class_type": "AuditedExtensionNode", "inputs": {}}}
        blocked_extension = {"1": {"class_type": "CheckpointSave", "inputs": {}}}
        with patch.dict(
            "os.environ",
            {"COMFYUI_EXTRA_ALLOWED_NODE_TYPES": "AuditedExtensionNode,CheckpointSave"},
        ):
            validate_comfy_graph(reviewed_extension)
            with self.assertRaisesRegex(WorkflowBuildError, "Dangerous Comfy node"):
                validate_comfy_graph(blocked_extension)
