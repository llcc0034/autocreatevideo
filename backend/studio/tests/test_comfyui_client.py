import json
from hashlib import sha256

import requests
import websocket

from django.test import TestCase, override_settings

from studio.services.comfyui import (
    ComfyUIClient,
    ComfyUIError,
    ComfyUIHTTPError,
    ComfyUIPreSubmitResourceReleaseError,
)


class _Response:
    headers = {"Content-Type": "application/json"}
    content = b'{"path":"workflows/autoprompt/example.json","size":10}'

    def raise_for_status(self):
        return None

    def json(self):
        return json.loads(self.content)

    def iter_content(self, chunk_size=64 * 1024):
        del chunk_size
        yield self.content


class _Session:
    def __init__(self, *responses):
        self.calls = []
        self.responses = list(responses)

    def request(self, method, url, **kwargs):
        self.calls.append((method, url, kwargs))
        return self.responses.pop(0) if self.responses else _Response()


class _TransportErrorSession:
    def request(self, method, url, **kwargs):
        del method, url, kwargs
        raise requests.ConnectionError("connection reset before response")


class _JsonResponse(_Response):
    def __init__(self, value, *, content_type="application/json"):
        self.content = json.dumps(value, ensure_ascii=False).encode("utf-8")
        self.headers = {"Content-Type": content_type, "Content-Length": str(len(self.content))}


class _HttpErrorResponse(_JsonResponse):
    def __init__(self, value, *, status_code=400):
        super().__init__(value)
        self.status_code = status_code
        self.text = self.content.decode("utf-8")

    def raise_for_status(self):
        error = requests.HTTPError(f"HTTP {self.status_code}")
        error.response = self
        raise error


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


class ComfyUIProgressSocketTests(TestCase):
    prompt_id = "prompt-123"

    def test_reads_task_scoped_progress_shape(self):
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

        self.assertEqual(event.telemetry_state, "receiving")
        self.assertEqual(event.event_type, "progress")
        self.assertEqual(event.node_id, "44")
        self.assertEqual(event.fraction, 0.25)

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
    COMFYUI_ALLOW_NON_LOOPBACK=False,
)
class ComfyUIClientWorkflowLibraryTests(TestCase):
    release_policy = {
        "schema_version": 1,
        "enabled": True,
        "unload_models": True,
        "free_memory": True,
    }

    @override_settings(TASK_MAX_CONCURRENT=1)
    def test_pre_submit_resource_release_posts_exact_free_contract(self):
        session = _Session(_JsonResponse({"ok": True}))
        client = ComfyUIClient(
            base_url="http://127.0.0.1:8188",
            timeout=30,
            session=session,
            pre_submit_resource_release_policy=self.release_policy,
        )

        audit = client.release_resources_before_submit()

        self.assertEqual(audit["status"], "succeeded")
        self.assertEqual(len(audit["policy_sha256"]), 64)
        self.assertFalse(audit["error_code"])
        self.assertEqual(len(session.calls), 1)
        method, url, kwargs = session.calls[0]
        self.assertEqual((method, url), ("POST", "http://127.0.0.1:8188/free"))
        self.assertEqual(kwargs["json"], {
            "unload_models": True,
            "free_memory": True,
        })
        self.assertNotIn("/prompt", url)

    @override_settings(TASK_MAX_CONCURRENT=2)
    def test_pre_submit_resource_release_fails_before_http_without_exclusive_worker(self):
        session = _Session()
        client = ComfyUIClient(
            base_url="http://127.0.0.1:8188",
            timeout=30,
            session=session,
            pre_submit_resource_release_policy=self.release_policy,
        )

        with self.assertRaises(ComfyUIPreSubmitResourceReleaseError) as raised:
            client.release_resources_before_submit()

        self.assertEqual(
            raised.exception.code,
            "comfyui_resource_release_exclusive_worker_required",
        )
        self.assertEqual(raised.exception.audit["status"], "failed")
        self.assertEqual(session.calls, [])

    @override_settings(TASK_MAX_CONCURRENT=1)
    def test_pre_submit_resource_release_rejection_is_definitive_and_never_prompts(self):
        session = _Session(_HttpErrorResponse({"error": "free rejected"}, status_code=409))
        client = ComfyUIClient(
            base_url="http://127.0.0.1:8188",
            timeout=30,
            session=session,
            pre_submit_resource_release_policy=self.release_policy,
        )

        with self.assertRaises(ComfyUIPreSubmitResourceReleaseError) as raised:
            client.release_resources_before_submit()

        self.assertEqual(raised.exception.code, "comfyui_resource_release_rejected")
        self.assertEqual(raised.exception.audit["http_status"], 409)
        self.assertEqual(len(session.calls), 1)
        self.assertTrue(session.calls[0][1].endswith("/free"))

    def test_pre_submit_resource_release_policy_has_no_hidden_defaults(self):
        for policy in (
            {"schema_version": 1, "enabled": True},
            {
                "schema_version": 1, "enabled": True,
                "unload_models": False, "free_memory": False,
            },
            {
                "schema_version": 1, "enabled": False,
                "unload_models": True, "free_memory": False,
            },
        ):
            with self.subTest(policy=policy), self.assertRaises(ComfyUIError):
                ComfyUIClient(
                    base_url="http://127.0.0.1:8188",
                    timeout=30,
                    session=_Session(),
                    pre_submit_resource_release_policy=policy,
                )

    def test_prompt_http_rejection_preserves_definitive_status_and_detail(self):
        client = ComfyUIClient(
            base_url="http://127.0.0.1:8188",
            timeout=30,
            session=_Session(_HttpErrorResponse({"error": "invalid prompt"})),
        )

        with self.assertRaises(ComfyUIHTTPError) as raised:
            client.queue_prompt({"1": {"class_type": "Broken", "inputs": {}}})

        self.assertEqual(raised.exception.status_code, 400)
        self.assertIn("invalid prompt", str(raised.exception))

    def test_prompt_transport_failure_remains_non_http_error(self):
        client = ComfyUIClient(
            base_url="http://127.0.0.1:8188",
            timeout=30,
            session=_TransportErrorSession(),
        )

        with self.assertRaises(ComfyUIError) as raised:
            client.queue_prompt({"1": {"class_type": "SaveImage", "inputs": {}}})

        self.assertNotIsInstance(raised.exception, ComfyUIHTTPError)
        self.assertIn("connection reset before response", str(raised.exception))

    def test_models_inventory_is_streamed_bounded_and_path_normalized(self):
        response = _JsonResponse([
            "nested\\model.safetensors",
            "nested/model.safetensors",
            "../escape.safetensors",
            "file.safetensors:stream",
        ])
        session = _Session(response)
        client = ComfyUIClient(base_url="http://127.0.0.1:8188", timeout=30, session=session)

        self.assertEqual(client.models("diffusion_models"), [
            "nested/model.safetensors",
        ])
        method, url, kwargs = session.calls[0]
        self.assertEqual((method, url), (
            "GET", "http://127.0.0.1:8188/models/diffusion_models",
        ))
        self.assertIs(kwargs["stream"], True)

        oversized = _JsonResponse([])
        oversized.headers["Content-Length"] = str(ComfyUIClient.MODEL_LIST_MAX_BYTES + 1)
        with self.assertRaises(ComfyUIError):
            ComfyUIClient(
                base_url="http://127.0.0.1:8188", timeout=30, session=_Session(oversized),
            ).models("vae")
        with self.assertRaises(ComfyUIError):
            ComfyUIClient(
                base_url="http://127.0.0.1:8188",
                timeout=30,
                session=_Session(_JsonResponse(["a"] * (ComfyUIClient.MODEL_LIST_MAX_ITEMS + 1))),
            ).models("vae")
        with self.assertRaises(ComfyUIError):
            ComfyUIClient(
                base_url="http://127.0.0.1:8188", timeout=30, session=_Session(_JsonResponse({})),
            ).models("vae")

    def test_models_inventory_rejects_chunked_byte_overflow(self):
        class ChunkedResponse(_Response):
            headers = {"Content-Type": "application/json"}

            def iter_content(self, chunk_size=64 * 1024):
                del chunk_size
                chunk = b" " * (ComfyUIClient.MODEL_LIST_MAX_BYTES // 2 + 1)
                yield chunk
                yield chunk

        with self.assertRaises(ComfyUIError):
            ComfyUIClient(
                base_url="http://127.0.0.1:8188", timeout=30, session=_Session(ChunkedResponse()),
            ).models("loras")

    def test_object_info_is_streamed_and_bounded_by_bytes_and_structure(self):
        valid = _JsonResponse({"SaveImage": {"input": {"required": {}}}})
        session = _Session(valid)
        client = ComfyUIClient(base_url="http://127.0.0.1:8188", timeout=30, session=session)

        self.assertEqual(client.object_info(), {"SaveImage": {"input": {"required": {}}}})
        method, url, kwargs = session.calls[0]
        self.assertEqual((method, url), ("GET", "http://127.0.0.1:8188/object_info"))
        self.assertIs(kwargs["stream"], True)

        oversized = _JsonResponse({})
        oversized.headers["Content-Length"] = str(ComfyUIClient.OBJECT_INFO_MAX_BYTES + 1)
        with self.assertRaises(ComfyUIError):
            ComfyUIClient(
                base_url="http://127.0.0.1:8188", timeout=30, session=_Session(oversized),
            ).object_info()

        nested: dict = {}
        cursor = nested
        for _index in range(ComfyUIClient.OBJECT_INFO_MAX_DEPTH + 1):
            cursor["child"] = {}
            cursor = cursor["child"]
        with self.assertRaises(ComfyUIError):
            ComfyUIClient(
                base_url="http://127.0.0.1:8188", timeout=30, session=_Session(_JsonResponse(nested)),
            ).object_info()

    def test_object_info_rejects_cumulative_stream_overflow_without_content_length(self):
        class ChunkedResponse(_Response):
            headers = {"Content-Type": "application/json"}

            def iter_content(self, chunk_size=64 * 1024):
                del chunk_size
                chunk = b" " * (ComfyUIClient.OBJECT_INFO_MAX_BYTES // 2 + 1)
                yield chunk
                yield chunk

        with self.assertRaises(ComfyUIError):
            ComfyUIClient(
                base_url="http://127.0.0.1:8188",
                timeout=30,
                session=_Session(ChunkedResponse()),
            ).object_info()

    def test_save_workflow_uses_fixed_encoded_autoprompt_path(self):
        session = _Session()
        client = ComfyUIClient(base_url="http://127.0.0.1:8188", timeout=30, session=session)
        filename = "autoprompt-task-11111111-1111-1111-1111-111111111111-comfyui-ui.json"

        result = client.save_autoprompt_workflow({"version": 0.4, "nodes": []}, filename=filename)

        self.assertEqual(result["workflow_path"], f"workflows/autoprompt/{filename}")
        method, url, kwargs = session.calls[0]
        self.assertEqual(method, "POST")
        self.assertIn("workflows%2Fautoprompt%2F", url)
        self.assertIn("overwrite=true", url)
        self.assertEqual(json.loads(kwargs["data"].decode("utf-8"))["version"], 0.4)

    def test_save_workflow_rejects_caller_controlled_path(self):
        client = ComfyUIClient(base_url="http://127.0.0.1:8188", timeout=30, session=_Session())
        with self.assertRaises(ComfyUIError):
            client.save_autoprompt_workflow({"version": 0.4}, filename="../outside.json")

    def test_list_workflows_uses_configured_userdata_directory_and_normalizes_paths(self):
        session = _Session(_JsonResponse([
            "z-image-turbo.json",
            "nested/组合工作流.json",
            "nested/./duplicate.json",
        ]))
        client = ComfyUIClient(base_url="http://127.0.0.1:8188", timeout=30, session=session)

        paths = client.list_saved_workflows("workflows/team-a")

        self.assertEqual(paths, ["nested/duplicate.json", "nested/组合工作流.json", "z-image-turbo.json"])
        method, url, kwargs = session.calls[0]
        self.assertEqual(method, "GET")
        self.assertEqual(url, "http://127.0.0.1:8188/api/userdata")
        self.assertEqual(kwargs["params"], {
            "dir": "workflows/team-a", "recurse": "true", "split": "false",
        })
        self.assertIs(kwargs["stream"], True)
        self.assertIs(kwargs["allow_redirects"], False)

    def test_list_workflows_rejects_server_paths_outside_configured_library(self):
        for invalid in (
            "../outside.json", "/absolute.json", "C:/drive.json", "nested\\backslash.json",
            "nested/not-json.txt", "nested/\u0000bad.json", "workflows/double-prefix.json",
        ):
            with self.subTest(invalid=invalid):
                client = ComfyUIClient(
                    base_url="http://127.0.0.1:8188",
                    timeout=30,
                    session=_Session(_JsonResponse([invalid])),
                )
                with self.assertRaises(ComfyUIError):
                    client.list_saved_workflows("workflows")

    def test_list_workflows_rejects_unsafe_configured_directory(self):
        client = ComfyUIClient(
            base_url="http://127.0.0.1:8188",
            timeout=30,
            session=_Session(_JsonResponse([])),
        )
        for invalid in ("../workflows", "C:/workflows", "workflows/../outside", "other"):
            with self.subTest(invalid=invalid), self.assertRaises(ComfyUIError):
                client.list_saved_workflows(invalid)

    def test_read_workflow_encodes_only_valid_relative_path_and_returns_raw_digest(self):
        value = {
            "version": 0.4,
            "nodes": [{
                "id": 1, "type": "PreviewAny", "inputs": [], "outputs": [],
                "widgets_values": [],
            }],
            "links": [],
            "extra": {},
        }
        response = _JsonResponse(value)
        session = _Session(response)
        client = ComfyUIClient(base_url="http://127.0.0.1:8188", timeout=30, session=session)

        document = client.read_saved_workflow(
            "nested/组合工作流.json",
            "workflows/team-a",
        )

        self.assertEqual(document.source_path, "nested/组合工作流.json")
        self.assertEqual(document.workflow, value)
        self.assertEqual(document.size_bytes, len(response.content))
        self.assertEqual(document.sha256, sha256(response.content).hexdigest())
        method, url, kwargs = session.calls[0]
        self.assertEqual(method, "GET")
        self.assertEqual(
            url,
            "http://127.0.0.1:8188/userdata/workflows%2Fteam-a%2Fnested%2F%E7%BB%84%E5%90%88%E5%B7%A5%E4%BD%9C%E6%B5%81.json",
        )
        self.assertIs(kwargs["stream"], True)

    def test_read_workflow_fails_closed_on_type_content_type_and_size(self):
        invalid_canvas = _JsonResponse([{"version": 0.4}])
        invalid_type = _JsonResponse({"version": 0.4, "nodes": [], "links": []}, content_type="text/plain")
        oversized = _JsonResponse({"version": 0.4, "nodes": [], "links": [], "pad": "x" * 4096})
        oversized.headers["Content-Length"] = str(6 * 1024 * 1024)
        for response in (invalid_canvas, invalid_type, oversized):
            with self.subTest(response=response):
                client = ComfyUIClient(
                    base_url="http://127.0.0.1:8188", timeout=30, session=_Session(response),
                )
                with self.assertRaises(ComfyUIError):
                    client.read_saved_workflow("workflow.json", "workflows")

    def test_extract_outputs_includes_bounded_text_contract(self):
        outputs = ComfyUIClient.extract_outputs({"outputs": {
            "7": {"text": ["first", "second"]},
            "8": {"images": [{"filename": "x.png", "subfolder": ""}]},
        }})

        self.assertEqual(outputs[0], {
            "node_id": "7", "kind": "text", "text": "first\nsecond",
            "size_bytes": len("first\nsecond".encode("utf-8")),
            "sha256": sha256("first\nsecond".encode("utf-8")).hexdigest(),
        })
        self.assertEqual(outputs[1]["kind"], "images")

    def test_extract_outputs_rejects_empty_or_oversized_text(self):
        with self.assertRaises(ComfyUIError):
            ComfyUIClient.extract_outputs({"outputs": {"7": {"text": ["   "]}}})
        with self.assertRaises(ComfyUIError):
            ComfyUIClient.extract_outputs({"outputs": {"7": {"text": ["x" * (64 * 1024 + 1)]}}})
