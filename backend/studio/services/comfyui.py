from __future__ import annotations

from dataclasses import dataclass
import ipaddress
import json
from pathlib import Path
import re
import time
import uuid
from urllib.parse import quote, urlparse

from django.conf import settings
import requests


class ComfyUIError(RuntimeError):
    pass


class ComfyUITimeout(ComfyUIError):
    pass


class ComfyUIExecutionError(ComfyUIError):
    """ComfyUI returned a definitive terminal failure for a submitted prompt."""

    def __init__(self, message: str, *, remote_status: dict | None = None):
        super().__init__(message)
        self.remote_status = remote_status or {}


@dataclass(frozen=True)
class QueueResult:
    prompt_id: str
    number: int | None
    node_errors: dict


@dataclass(frozen=True)
class ProgressSocketEvent:
    """One task-scoped ComfyUI WebSocket observation.

    ``fraction`` is deliberately nullable: an ``executing`` event proves that
    the task socket is live and identifies the current node, but it does not
    contain a quantitative progress value.  ``None`` from
    :meth:`read_progress_event` means that no applicable event arrived before
    the short read deadline; a zero fraction remains a real observation.
    """

    telemetry_state: str
    event_type: str = ""
    prompt_id: str = ""
    node_id: str = ""
    fraction: float | None = None
    error: str = ""


def default_comfyui_runtime_config() -> dict:
    """Read the single default ComfyUI endpoint from the database catalog."""

    from studio.models import ModelDeployment, ModelProfileVersion, ProviderProfile
    from studio.services.json_contracts import verify_json_contract

    deployment = ModelDeployment.objects.select_related(
        "profile_version__provider",
    ).filter(
        status=ModelDeployment.Status.ACTIVE,
        profile_version__status=ModelProfileVersion.Status.PUBLISHED,
        profile_version__provider__driver_type=ProviderProfile.DriverType.COMFYUI,
        profile_version__provider__is_active=True,
    ).order_by("-is_default", "-priority", "key").first()
    if deployment is None:
        raise ComfyUIError("数据库中没有可用的 ComfyUI ModelDeployment")
    provider = deployment.profile_version.provider
    try:
        provider_config = verify_json_contract(
            provider.driver_config,
            schema_version=provider.driver_config_schema_version,
            size_bytes=provider.driver_config_size_bytes,
            sha256=provider.driver_config_sha256,
        ).value
        deployment_config = verify_json_contract(
            deployment.runtime_config,
            schema_version=deployment.runtime_config_schema_version,
            size_bytes=deployment.runtime_config_size_bytes,
            sha256=deployment.runtime_config_sha256,
        ).value
    except Exception as exc:
        raise ComfyUIError("默认 ComfyUI deployment 配置合同无效") from exc
    config = {**(provider_config or {}), **(deployment_config or {})}
    if not str(config.get("base_url") or "").strip():
        raise ComfyUIError("默认 ComfyUI deployment 缺少 base_url")
    return config


class ComfyUIClient:
    """Small API abstraction compatible with a local or remote ComfyUI server."""

    def __init__(
        self,
        base_url: str | None = None,
        timeout: float | None = None,
        session=None,
        *,
        endpoint_prevalidated: bool = False,
    ):
        database_config = default_comfyui_runtime_config() if base_url is None else {}
        self.base_url = str(base_url or database_config.get("base_url") or "").rstrip("/")
        parsed = urlparse(self.base_url)
        if parsed.scheme not in {"http", "https"} or not parsed.hostname:
            raise ComfyUIError("COMFYUI_BASE_URL 必须是有效的 http(s) URL")
        if not settings.COMFYUI_ALLOW_NON_LOOPBACK and not endpoint_prevalidated:
            is_loopback = parsed.hostname.lower() == "localhost"
            try:
                is_loopback = is_loopback or ipaddress.ip_address(parsed.hostname).is_loopback
            except ValueError:
                pass
            if not is_loopback:
                raise ComfyUIError("出于 SSRF 安全考虑，默认只允许 loopback ComfyUI；远程地址需显式授权")
        self.timeout = timeout or float(database_config.get("timeout_seconds") or 30)
        self.session = session or requests.Session()

    def _request(self, method: str, path: str, **kwargs):
        try:
            kwargs.setdefault("allow_redirects", False)
            response = self.session.request(method, f"{self.base_url}{path}", timeout=self.timeout, **kwargs)
            response.raise_for_status()
            return response
        except requests.RequestException as exc:
            raise ComfyUIError(f"ComfyUI 请求失败: {exc}") from exc

    def health(self) -> dict:
        return self._request("GET", "/system_stats").json()

    def object_info(self) -> dict:
        return self._request("GET", "/object_info").json()

    def features(self) -> dict:
        return self._request("GET", "/features").json()

    def models(self, folder: str) -> list[str]:
        if folder not in {"checkpoints", "diffusion_models", "text_encoders", "vae", "loras"}:
            raise ComfyUIError("不支持的模型目录")
        data = self._request("GET", f"/models/{folder}").json()
        return data if isinstance(data, list) else []

    def open_progress_socket(self, client_id: str, *, timeout: float = 0.1):
        """Open ComfyUI's documented low-level progress WebSocket."""
        try:
            import websocket
        except ImportError as exc:  # pragma: no cover - guarded by requirements
            raise ComfyUIError("websocket-client 未安装，无法读取 ComfyUI 实时进度") from exc
        parsed = urlparse(self.base_url)
        scheme = "wss" if parsed.scheme == "https" else "ws"
        websocket_url = f"{scheme}://{parsed.netloc}/ws?clientId={quote(str(client_id), safe='')}"
        try:
            return websocket.create_connection(
                websocket_url,
                timeout=max(0.01, float(timeout)),
                http_no_proxy=[parsed.hostname] if parsed.hostname else None,
            )
        except Exception as exc:
            raise ComfyUIError(f"ComfyUI 进度 WebSocket 连接失败: {exc}") from exc

    @staticmethod
    def _progress_fraction(data: dict) -> float | None:
        try:
            value = float(data["value"])
            total = float(data["max"])
        except (KeyError, TypeError, ValueError):
            return None
        if total <= 0:
            return None
        return max(0.0, min(1.0, value / total))

    @classmethod
    def read_progress_event(
        cls,
        progress_socket,
        prompt_id: str,
        *,
        timeout: float = 0.05,
    ) -> ProgressSocketEvent | None:
        """Drain a short WebSocket window and return task-scoped telemetry.

        ComfyUI 0.30 emits both the legacy ``progress`` event and the newer
        ``progress_state`` node map.  Non-progress frames are drained so a
        preceding status/preview message cannot indefinitely hide sampler
        updates.  The latest non-quantitative task observation is returned when
        no fraction is available.
        """
        try:
            import websocket
        except ImportError as exc:  # pragma: no cover - guarded by requirements
            return ProgressSocketEvent(
                telemetry_state="unavailable",
                prompt_id=str(prompt_id),
                error=f"websocket-client is unavailable: {exc}",
            )

        timeout_seconds = max(0.01, float(timeout))
        deadline = time.monotonic() + timeout_seconds
        fallback: ProgressSocketEvent | None = None
        while True:
            remaining = deadline - time.monotonic()
            if remaining <= 0:
                return fallback
            try:
                progress_socket.settimeout(max(0.01, remaining))
                raw = progress_socket.recv()
            except websocket.WebSocketTimeoutException:
                return fallback
            except Exception as exc:
                return ProgressSocketEvent(
                    telemetry_state="unavailable",
                    prompt_id=str(prompt_id),
                    error=f"ComfyUI progress WebSocket read failed: {exc}",
                )

            if raw is None or raw == "":
                return ProgressSocketEvent(
                    telemetry_state="unavailable",
                    prompt_id=str(prompt_id),
                    error="ComfyUI progress WebSocket closed while reading",
                )

            # Binary preview frames carry no task percentage. Keep draining
            # until the same short deadline instead of losing this poll cycle.
            if not isinstance(raw, str):
                continue
            try:
                event = json.loads(raw)
            except (TypeError, ValueError):
                continue
            if not isinstance(event, dict) or not isinstance(event.get("data"), dict):
                continue

            event_type = str(event.get("type") or "")
            data = event["data"]
            event_prompt_id = str(data.get("prompt_id") or "")
            if event_prompt_id and event_prompt_id != str(prompt_id):
                continue

            if event_type == "progress":
                # Legacy progress must carry the prompt id because multiple
                # prompt events can share server infrastructure.
                if not event_prompt_id:
                    continue
                fraction = cls._progress_fraction(data)
                if fraction is None:
                    fallback = ProgressSocketEvent(
                        telemetry_state="connected",
                        event_type=event_type,
                        prompt_id=str(prompt_id),
                        node_id=str(data.get("node") or data.get("node_id") or ""),
                    )
                    continue
                return ProgressSocketEvent(
                    telemetry_state="receiving",
                    event_type=event_type,
                    prompt_id=str(prompt_id),
                    node_id=str(data.get("node") or data.get("node_id") or ""),
                    fraction=fraction,
                )

            if event_type == "progress_state":
                if not event_prompt_id:
                    continue
                nodes = data.get("nodes")
                if not isinstance(nodes, dict):
                    continue
                # Finished nodes often report 1/1. They are explicitly ignored
                # so they cannot be mistaken for current-task completion.
                running_nodes = [
                    (str(node_id), state)
                    for node_id, state in nodes.items()
                    if isinstance(state, dict) and str(state.get("state") or "").lower() == "running"
                ]
                if not running_nodes:
                    fallback = ProgressSocketEvent(
                        telemetry_state="connected",
                        event_type=event_type,
                        prompt_id=str(prompt_id),
                    )
                    continue
                node_id, state = running_nodes[-1]
                node_id = str(state.get("node_id") or node_id)
                fraction = cls._progress_fraction(state)
                return ProgressSocketEvent(
                    telemetry_state="receiving" if fraction is not None else "connected",
                    event_type=event_type,
                    prompt_id=str(prompt_id),
                    node_id=node_id,
                    fraction=fraction,
                )

            if event_type == "executing":
                # On task-specific WebSocket reattachment ComfyUI may replay an
                # executing event with only ``node`` and no prompt_id.
                fallback = ProgressSocketEvent(
                    telemetry_state="connected",
                    event_type=event_type,
                    prompt_id=str(prompt_id),
                    node_id=str(data.get("node") or data.get("display_node") or ""),
                )
                continue

            # A status/lifecycle frame still proves the socket is readable, but
            # does not supply quantitative task progress.
            fallback = ProgressSocketEvent(
                telemetry_state="connected",
                event_type=event_type,
                prompt_id=str(prompt_id),
            )

    @classmethod
    def read_progress_socket(cls, progress_socket, prompt_id: str, *, timeout: float = 0.05) -> float | None:
        """Backward-compatible fraction-only wrapper for older callers."""
        observation = cls.read_progress_event(progress_socket, prompt_id, timeout=timeout)
        return observation.fraction if observation is not None else None

    def save_autoprompt_workflow(self, workflow: dict, *, filename: str) -> dict:
        """Persist an importable UI canvas in ComfyUI's user workflow library.

        The destination is deliberately fixed below ``workflows/autoprompt``;
        callers cannot select arbitrary user-data paths.  Saving is idempotent
        for a task-derived filename and never queues or executes the graph.
        """
        if not isinstance(workflow, dict) or not workflow:
            raise ComfyUIError("待同步的 ComfyUI UI 画布不是有效对象")
        if not re.fullmatch(r"autoprompt-task-[0-9a-f-]{36}-comfyui-ui\.json", filename):
            raise ComfyUIError("ComfyUI 工作流文件名不符合平台固定格式")
        payload = json.dumps(workflow, ensure_ascii=False, separators=(",", ":")).encode("utf-8")
        if len(payload) > 5 * 1024 * 1024:
            raise ComfyUIError("ComfyUI UI 画布超过 5 MiB 同步上限")
        relative_path = f"workflows/autoprompt/{filename}"
        encoded_path = quote(relative_path, safe="")
        response = self._request(
            "POST",
            f"/userdata/{encoded_path}?overwrite=true&full_info=true",
            data=payload,
            headers={"Content-Type": "application/json; charset=utf-8"},
        ).json()
        return {
            "workflow_path": relative_path,
            "server_record": response if isinstance(response, dict) else {},
        }

    def upload_input_file(self, path: str | Path, *, subfolder: str, filename: str | None = None) -> dict:
        """Upload any pre-validated image/video/audio into ComfyUI input.

        ComfyUI's generic binary input route is historically named
        ``/upload/image``; server.py writes the multipart file without image
        decoding and LoadImage/LoadVideo/LoadAudio all consume the returned
        relative input path.
        """
        source = Path(path)
        if not source.is_file():
            raise ComfyUIError(f"参考输入文件不存在: {source}")
        upload_name = filename or f"{uuid.uuid4().hex}{source.suffix.lower() or '.png'}"
        try:
            with source.open("rb") as stream:
                response = self._request(
                    "POST", "/upload/image",
                    files={"image": (upload_name, stream, "application/octet-stream")},
                    data={"type": "input", "subfolder": subfolder, "overwrite": "false"},
                ).json()
        except OSError as exc:
            raise ComfyUIError(f"无法读取参考输入文件: {source}") from exc
        if not response.get("name"):
            raise ComfyUIError(f"ComfyUI 上传参考输入失败: {response}")
        return response

    def upload_image(self, path: str | Path, *, subfolder: str, filename: str | None = None) -> dict:
        """Backward-compatible alias for older callers/tests."""
        return self.upload_input_file(path, subfolder=subfolder, filename=filename)

    def queue_prompt(
        self, workflow: dict, client_id: str | None = None, prompt_id: str | None = None,
    ) -> QueueResult:
        client_id = client_id or f"autoprompt-studio-{uuid.uuid4()}"
        payload = {"prompt": workflow, "client_id": client_id}
        if prompt_id is not None:
            try:
                prompt_id = str(uuid.UUID(prompt_id))
            except (ValueError, AttributeError) as exc:
                raise ComfyUIError("预生成 prompt_id 必须是规范 UUID") from exc
            payload["prompt_id"] = prompt_id
        data = self._request("POST", "/prompt", json=payload).json()
        returned_prompt_id = data.get("prompt_id")
        if not returned_prompt_id:
            raise ComfyUIError(f"ComfyUI 未返回 prompt_id: {data}")
        return QueueResult(returned_prompt_id, data.get("number"), data.get("node_errors", {}))

    def history(self, prompt_id: str) -> dict | None:
        data = self._request("GET", f"/history/{prompt_id}").json()
        return data.get(prompt_id)

    def queue(self) -> dict:
        return self._request("GET", "/queue").json()

    def cancel(self, prompt_id: str, interrupt_running: bool = True) -> None:
        self._request("POST", "/queue", json={"delete": [prompt_id]})
        if interrupt_running:
            self._request("POST", "/interrupt", json={"prompt_id": prompt_id})

    def is_queued(self, prompt_id: str) -> bool:
        queue = self.queue()
        for key in ("queue_running", "queue_pending"):
            for item in queue.get(key, []) or []:
                if isinstance(item, (list, tuple)) and len(item) > 1 and str(item[1]) == str(prompt_id):
                    return True
        return False

    def wait_for_completion(self, prompt_id: str, timeout: float = 900, poll_interval: float = 1.0) -> dict:
        deadline = time.monotonic() + timeout
        while time.monotonic() < deadline:
            history = self.history(prompt_id)
            if history is not None:
                status = history.get("status", {})
                if status.get("status_str") == "error" or status.get("completed") is False:
                    messages = status.get("messages", [])
                    raise ComfyUIExecutionError(
                        f"ComfyUI 执行失败: {messages}", remote_status=status,
                    )
                return history
            time.sleep(max(0.1, poll_interval))
        raise ComfyUITimeout(f"等待 ComfyUI 任务 {prompt_id} 超时")

    @staticmethod
    def extract_outputs(history: dict) -> list[dict]:
        found = []
        for node_id, node_outputs in (history.get("outputs") or {}).items():
            for kind in ("images", "gifs", "videos", "audio"):
                for item in node_outputs.get(kind, []) or []:
                    if isinstance(item, dict):
                        found.append({"node_id": node_id, "kind": kind, **item})
                    else:
                        found.append({"node_id": node_id, "kind": kind, "value": item})
        return found
