import json

import websocket

from django.test import SimpleTestCase, override_settings

from studio.services.comfyui import ComfyUIClient, ComfyUIError


class _Response:
    def raise_for_status(self):
        return None

    def json(self):
        return {"path": "workflows/autoprompt/example.json", "size": 10}


class _Session:
    def __init__(self):
        self.calls = []

    def request(self, method, url, **kwargs):
        self.calls.append((method, url, kwargs))
        return _Response()


class _ProgressSocket:
    def __init__(self, *events):
        self.events = list(events)
        self.timeout = None

    def settimeout(self, timeout):
        self.timeout = timeout

    def recv(self):
        if not self.events:
            raise websocket.WebSocketTimeoutException()
        event = self.events.pop(0)
        return json.dumps(event) if isinstance(event, dict) else event


class ComfyUIProgressSocketTests(SimpleTestCase):
    prompt_id = "prompt-123"

    def test_reads_legacy_progress_shape_and_keeps_fraction_wrapper(self):
        payload = {
            "type": "progress",
            "data": {
                "value": 3,
                "max": 12,
                "prompt_id": self.prompt_id,
                "node": "44",
            },
        }

        event = ComfyUIClient.read_progress_event(_ProgressSocket(payload), self.prompt_id)
        fraction = ComfyUIClient.read_progress_socket(_ProgressSocket(payload), self.prompt_id)

        self.assertEqual(event.telemetry_state, "receiving")
        self.assertEqual(event.event_type, "progress")
        self.assertEqual(event.node_id, "44")
        self.assertEqual(event.fraction, 0.25)
        self.assertEqual(fraction, 0.25)

    def test_progress_state_selects_running_node_and_preserves_real_zero(self):
        payload = {
            "type": "progress_state",
            "data": {
                "prompt_id": self.prompt_id,
                "nodes": {
                    "17": {"value": 1, "max": 1, "state": "finished", "node_id": "17"},
                    "44": {"value": 0, "max": 20, "state": "running", "node_id": "44"},
                },
            },
        }

        event = ComfyUIClient.read_progress_event(_ProgressSocket(payload), self.prompt_id)

        self.assertEqual(event.telemetry_state, "receiving")
        self.assertEqual(event.event_type, "progress_state")
        self.assertEqual(event.node_id, "44")
        self.assertEqual(event.fraction, 0.0)
        self.assertIsNone(
            ComfyUIClient.read_progress_event(_ProgressSocket(), self.prompt_id),
        )

    def test_mixed_frames_ignore_other_prompts_and_finished_nodes(self):
        socket = _ProgressSocket(
            b"binary-preview",
            {"type": "status", "data": {"status": {"exec_info": {"queue_remaining": 1}}}},
            {
                "type": "progress",
                "data": {"value": 9, "max": 10, "prompt_id": "other-prompt", "node": "5"},
            },
            {
                "type": "progress_state",
                "data": {
                    "prompt_id": self.prompt_id,
                    "nodes": {"17": {"value": 1, "max": 1, "state": "finished"}},
                },
            },
        )

        event = ComfyUIClient.read_progress_event(socket, self.prompt_id)

        self.assertEqual(event.telemetry_state, "connected")
        self.assertEqual(event.event_type, "progress_state")
        self.assertEqual(event.node_id, "")
        self.assertIsNone(event.fraction)

    def test_reattach_executing_without_prompt_id_reports_node_not_percentage(self):
        event = ComfyUIClient.read_progress_event(
            _ProgressSocket({"type": "executing", "data": {"node": "44"}}),
            self.prompt_id,
        )

        self.assertEqual(event.telemetry_state, "connected")
        self.assertEqual(event.event_type, "executing")
        self.assertEqual(event.prompt_id, self.prompt_id)
        self.assertEqual(event.node_id, "44")
        self.assertIsNone(event.fraction)


@override_settings(
    COMFYUI_BASE_URL="http://127.0.0.1:8188",
    COMFYUI_ALLOW_NON_LOOPBACK=False,
    COMFYUI_TIMEOUT_SECONDS=5,
)
class ComfyUIClientWorkflowLibraryTests(SimpleTestCase):
    def test_save_workflow_uses_fixed_encoded_autoprompt_path(self):
        session = _Session()
        client = ComfyUIClient(session=session)
        filename = "autoprompt-task-11111111-1111-1111-1111-111111111111-comfyui-ui.json"

        result = client.save_autoprompt_workflow({"version": 0.4, "nodes": []}, filename=filename)

        self.assertEqual(result["workflow_path"], f"workflows/autoprompt/{filename}")
        method, url, kwargs = session.calls[0]
        self.assertEqual(method, "POST")
        self.assertIn("workflows%2Fautoprompt%2F", url)
        self.assertIn("overwrite=true", url)
        self.assertEqual(json.loads(kwargs["data"].decode("utf-8"))["version"], 0.4)

    def test_save_workflow_rejects_caller_controlled_path(self):
        client = ComfyUIClient(session=_Session())
        with self.assertRaises(ComfyUIError):
            client.save_autoprompt_workflow({"version": 0.4}, filename="../outside.json")
