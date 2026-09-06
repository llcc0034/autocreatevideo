from __future__ import annotations

from dataclasses import dataclass
import hashlib
import ipaddress
import json
import math
from pathlib import Path, PurePosixPath
import re
import time
import unicodedata
import uuid
from urllib.parse import quote, urlparse

from django.conf import settings
from django.utils import timezone
import requests

from studio.services.security_boundaries import boundary_int, boundary_number


class ComfyUIError(RuntimeError):
    pass


class ComfyUIHTTPError(ComfyUIError):
    """A definitive HTTP response returned by ComfyUI.

    Unlike a transport timeout, a 4xx response proves that ComfyUI received
    and rejected the request; callers may safely mark it failed instead of
    leaving an idempotent submission in an uncertain state.
    """

    def __init__(self, status_code: int, detail: str):
        self.status_code = int(status_code)
        super().__init__(f"ComfyUI HTTP {self.status_code}: {detail}")


class ComfyUITimeout(ComfyUIError):
    pass


class ComfyUIExecutionError(ComfyUIError):
    """ComfyUI returned a definitive terminal failure for a submitted prompt."""

    def __init__(self, message: str, *, remote_status: dict | None = None):
        super().__init__(message)
        self.remote_status = remote_status or {}


class ComfyUIPreSubmitResourceReleaseError(ComfyUIError):
    """A definitive failure before ``POST /prompt`` is attempted.

    ComfyUI's ``/free`` endpoint changes process-global accelerator state.  A
    configured release therefore runs only while the platform media worker is
    exclusive.  Any failure is fail-closed and carries a bounded audit record;
    callers must never classify it as an uncertain prompt submission.
    """

    def __init__(self, code: str, message: str, *, audit: dict):
        self.code = str(code or "comfyui_pre_submit_resource_release_failed")
        self.audit = dict(audit or {})
        super().__init__(f"[{self.code}] {message}")


PRE_SUBMIT_RESOURCE_RELEASE_KEY = "pre_submit_resource_release"
_PRE_SUBMIT_RESOURCE_RELEASE_FIELDS = frozenset({
    "schema_version", "enabled", "unload_models", "free_memory",
})


def _stage_snapshot_max_bytes() -> int:
    return boundary_int("security.snapshot.stage_max_bytes")


def validate_pre_submit_resource_release_policy(value) -> dict | None:
    """Validate the generic deployment-owned ComfyUI release policy.

    Absence means no release request.  Presence is deliberately explicit: the
    four fields have no hidden defaults, so a copied deployment behaves the
    same on another host and a disabled policy cannot retain dormant actions.
    """

    if value is None:
        return None
    if not isinstance(value, dict):
        raise ComfyUIError("pre_submit_resource_release 必须是 JSON 对象")
    unknown = sorted(set(value) - _PRE_SUBMIT_RESOURCE_RELEASE_FIELDS)
    missing = sorted(_PRE_SUBMIT_RESOURCE_RELEASE_FIELDS - set(value))
    if unknown:
        raise ComfyUIError(
            "pre_submit_resource_release 包含未知字段: " + ", ".join(unknown),
        )
    if missing:
        raise ComfyUIError(
            "pre_submit_resource_release 缺少字段: " + ", ".join(missing),
        )
    if value.get("schema_version") != 1:
        raise ComfyUIError("pre_submit_resource_release.schema_version 只支持 1")
    for key in ("enabled", "unload_models", "free_memory"):
        if type(value.get(key)) is not bool:
            raise ComfyUIError(f"pre_submit_resource_release.{key} 必须是布尔值")
    enabled = value["enabled"]
    unload_models = value["unload_models"]
    free_memory = value["free_memory"]
    if enabled and not (unload_models or free_memory):
        raise ComfyUIError("启用 pre_submit_resource_release 时至少选择一项释放动作")
    if not enabled and (unload_models or free_memory):
        raise ComfyUIError("停用 pre_submit_resource_release 时释放动作必须同时为 false")
    return {
        "schema_version": 1,
        "enabled": enabled,
        "unload_models": unload_models,
        "free_memory": free_memory,
    }


@dataclass(frozen=True)
class QueueResult:
    prompt_id: str
    number: int | None
    node_errors: dict


@dataclass(frozen=True)
class SavedWorkflowDocument:
    """One immutable observation from ComfyUI's managed workflow library."""

    source_path: str
    sha256: str
    size_bytes: int
    workflow: dict


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
            max_bytes=_stage_snapshot_max_bytes(),
        ).value
        deployment_config = verify_json_contract(
            deployment.runtime_config,
            schema_version=deployment.runtime_config_schema_version,
            size_bytes=deployment.runtime_config_size_bytes,
            sha256=deployment.runtime_config_sha256,
            max_bytes=_stage_snapshot_max_bytes(),
        ).value
    except Exception as exc:
        raise ComfyUIError("默认 ComfyUI deployment 配置合同无效") from exc
    config = {**(provider_config or {}), **(deployment_config or {})}
    if not str(config.get("base_url") or "").strip():
        raise ComfyUIError("默认 ComfyUI deployment 缺少 base_url")
    return config


class _SecurityBoundaryInt:
    def __init__(self, key: str):
        self.key = key

    def __get__(self, instance, owner) -> int:
        return boundary_int(self.key)


class ComfyUIClient:
    """Small API abstraction compatible with a local or remote ComfyUI server."""

    SAVED_WORKFLOW_MAX_BYTES = _SecurityBoundaryInt(
        "security.comfy.client.saved_graph_max_bytes",
    )
    SAVED_WORKFLOW_LIST_MAX_BYTES = _SecurityBoundaryInt(
        "security.comfy.client.list_response_max_bytes",
    )
    SAVED_WORKFLOW_LIST_MAX_ITEMS = _SecurityBoundaryInt(
        "security.comfy.client.list_max_items",
    )
    TEXT_OUTPUT_MAX_BYTES = _SecurityBoundaryInt(
        "security.comfy.client.text_response_max_bytes",
    )
    OBJECT_INFO_MAX_BYTES = _SecurityBoundaryInt(
        "security.comfy.client.object_info_max_bytes",
    )
    OBJECT_INFO_MAX_DEPTH = _SecurityBoundaryInt(
        "security.comfy.client.object_info_max_depth",
    )
    OBJECT_INFO_MAX_VALUES = _SecurityBoundaryInt(
        "security.comfy.client.object_info_max_entries",
    )
    MODEL_LIST_MAX_BYTES = _SecurityBoundaryInt(
        "security.comfy.client.model_list_max_bytes",
    )
    MODEL_LIST_MAX_ITEMS = _SecurityBoundaryInt(
        "security.comfy.client.model_list_max_items",
    )

    def __init__(
        self,
        base_url: str | None = None,
        timeout: float | None = None,
        session=None,
        *,
        endpoint_prevalidated: bool = False,
        pre_submit_resource_release_policy: dict | None = None,
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
        configured_timeout = (
            timeout if timeout is not None else database_config.get("timeout_seconds")
        )
        timeout_ceiling = boundary_number(
            "security.runtime.max_timeout_seconds",
            minimum=0.001,
        )
        if (
            isinstance(configured_timeout, bool)
            or not isinstance(configured_timeout, (int, float))
            or not math.isfinite(float(configured_timeout))
            or not 0 < float(configured_timeout) <= timeout_ceiling
        ):
            raise ComfyUIError(
                "ComfyUIClient 必须显式取得当前安全边界内的 timeout_seconds",
            )
        self.timeout = float(configured_timeout)
        self.session = session or requests.Session()
        self.pre_submit_resource_release_policy = (
            validate_pre_submit_resource_release_policy(
                pre_submit_resource_release_policy,
            )
        )

    def _request(self, method: str, path: str, **kwargs):
        try:
            kwargs.setdefault("allow_redirects", False)
            response = self.session.request(method, f"{self.base_url}{path}", timeout=self.timeout, **kwargs)
            response.raise_for_status()
            return response
        except requests.HTTPError as exc:
            response = exc.response
            status_code = int(getattr(response, "status_code", 0) or 0)
            try:
                detail = str(getattr(response, "text", "") or "").strip()
            except Exception:
                detail = ""
            if not detail:
                try:
                    detail = str(response.json()) if response is not None else ""
                except Exception:
                    detail = ""
            raise ComfyUIHTTPError(status_code, detail[:4000] or "request rejected") from exc
        except requests.RequestException as exc:
            raise ComfyUIError(f"ComfyUI 请求失败: {exc}") from exc

    def health(self) -> dict:
        return self._request("GET", "/system_stats").json()

    def object_info(self) -> dict:
        response = self._request("GET", "/object_info", stream=True)
        value, _payload = self._bounded_json_response(
            response,
            max_bytes=self.OBJECT_INFO_MAX_BYTES,
            label="object_info",
        )
        if not isinstance(value, dict):
            raise ComfyUIError("ComfyUI object_info 顶层必须是对象")
        self._assert_bounded_json_structure(
            value,
            max_depth=self.OBJECT_INFO_MAX_DEPTH,
            max_values=self.OBJECT_INFO_MAX_VALUES,
            label="object_info",
        )
        return value

    def features(self) -> dict:
        return self._request("GET", "/features").json()

    def models(self, folder: str) -> list[str]:
        if folder not in {"checkpoints", "diffusion_models", "text_encoders", "vae", "loras"}:
            raise ComfyUIError("不支持的模型目录")
        response = self._request("GET", f"/models/{folder}", stream=True)
        data, _payload = self._bounded_json_response(
            response,
            max_bytes=self.MODEL_LIST_MAX_BYTES,
            label=f"models/{folder}",
        )
        if not isinstance(data, list) or len(data) > self.MODEL_LIST_MAX_ITEMS:
            raise ComfyUIError("ComfyUI models 库存列表合同无效")
        from studio.services.comfyui_artifacts import safe_model_relative_filename

        values = set()
        for item in data:
            if not isinstance(item, str):
                raise ComfyUIError("ComfyUI models 库存含非字符串文件名")
            normalized = safe_model_relative_filename(item)
            if normalized is not None:
                values.add(normalized)
        return sorted(values, key=lambda item: (item.casefold(), item))

    @staticmethod
    def _saved_workflow_path(value: str) -> str:
        raw = unicodedata.normalize("NFC", str(value or "").strip())
        if (
            not raw
            or len(raw) > 500
            or "\\" in raw
            or ":" in raw
            or raw.startswith("/")
            or any(ord(character) < 32 or ord(character) == 127 for character in raw)
        ):
            raise ComfyUIError("ComfyUI workflow source_path 无效")
        parts = raw.split("/")
        if (
            not parts
            or any(part in {"", ".."} or len(part) > 255 for part in parts)
            or parts[0].casefold() == "workflows"
        ):
            raise ComfyUIError("ComfyUI workflow source_path 越过已配置扫描目录")
        normalized = str(PurePosixPath(*parts))
        if not normalized.casefold().endswith(".json"):
            raise ComfyUIError("ComfyUI workflow source_path 必须是 JSON 文件")
        return normalized

    @staticmethod
    def _bounded_json_response(
        response,
        *,
        max_bytes: int,
        label: str = "userdata",
    ):
        content_type = str((getattr(response, "headers", {}) or {}).get("Content-Type") or "")
        if content_type and "json" not in content_type.casefold():
            raise ComfyUIError(f"ComfyUI {label} 响应不是 JSON")
        length_header = str((getattr(response, "headers", {}) or {}).get("Content-Length") or "").strip()
        if length_header:
            try:
                length = int(length_header)
                if length < 0:
                    raise ValueError
                if length > max_bytes:
                    raise ComfyUIError(f"ComfyUI {label} 响应超过大小上限")
            except ValueError as exc:
                raise ComfyUIError(f"ComfyUI {label} Content-Length 无效") from exc
        payload = bytearray()
        iterator = getattr(response, "iter_content", None)
        chunks = iterator(chunk_size=64 * 1024) if callable(iterator) else (getattr(response, "content", b""),)
        for chunk in chunks:
            if not chunk:
                continue
            payload.extend(chunk)
            if len(payload) > max_bytes:
                raise ComfyUIError(f"ComfyUI {label} 响应超过大小上限")
        try:
            value = json.loads(bytes(payload).decode("utf-8"))
        except (UnicodeDecodeError, ValueError, RecursionError) as exc:
            raise ComfyUIError(f"ComfyUI {label} JSON 无效") from exc
        return value, bytes(payload)

    @staticmethod
    def _assert_bounded_json_structure(
        value,
        *,
        max_depth: int,
        max_values: int,
        label: str,
    ) -> None:
        stack = [(value, 1)]
        observed = 0
        while stack:
            child, depth = stack.pop()
            observed += 1
            if observed > max_values:
                raise ComfyUIError(f"ComfyUI {label} JSON 成员数超过上限")
            if depth > max_depth:
                raise ComfyUIError(f"ComfyUI {label} JSON 嵌套深度超过上限")
            if isinstance(child, dict):
                stack.extend((item, depth + 1) for item in child.values())
            elif isinstance(child, list):
                stack.extend((item, depth + 1) for item in child)

    def list_saved_workflows(self, directory: str) -> list[str]:
        """List JSON files relative to one validated ComfyUI userdata directory."""

        from studio.services.comfyui_workflow_library import (
            SavedWorkflowDirectoryError,
            normalize_saved_workflow_directory,
        )

        try:
            normalized_directory = normalize_saved_workflow_directory(directory)
        except SavedWorkflowDirectoryError as exc:
            raise ComfyUIError(str(exc)) from exc

        response = self._request(
            "GET",
            "/api/userdata",
            params={
                "dir": normalized_directory,
                "recurse": "true",
                "split": "false",
            },
            stream=True,
        )
        value, _payload = self._bounded_json_response(
            response, max_bytes=self.SAVED_WORKFLOW_LIST_MAX_BYTES,
        )
        if not isinstance(value, list) or len(value) > self.SAVED_WORKFLOW_LIST_MAX_ITEMS:
            raise ComfyUIError("ComfyUI workflow library 列表合同无效")
        paths = []
        for item in value:
            if not isinstance(item, str):
                raise ComfyUIError("ComfyUI workflow library 列表含非字符串路径")
            paths.append(self._saved_workflow_path(item))
        return sorted(set(paths), key=lambda item: (item.casefold(), item))

    def read_saved_workflow(
        self,
        source_path: str,
        directory: str,
    ) -> SavedWorkflowDocument:
        """Read one configured-root workflow and bind it to its raw byte hash."""

        from studio.services.comfyui_workflow_library import (
            SavedWorkflowDirectoryError,
            normalize_saved_workflow_directory,
        )

        normalized = self._saved_workflow_path(source_path)
        try:
            normalized_directory = normalize_saved_workflow_directory(directory)
        except SavedWorkflowDirectoryError as exc:
            raise ComfyUIError(str(exc)) from exc
        encoded_path = quote(f"{normalized_directory}/{normalized}", safe="")
        response = self._request("GET", f"/userdata/{encoded_path}", stream=True)
        workflow, payload = self._bounded_json_response(
            response, max_bytes=self.SAVED_WORKFLOW_MAX_BYTES,
        )
        from studio.services.comfyui_canvas import is_comfyui_ui_workflow

        if not isinstance(workflow, dict) or not is_comfyui_ui_workflow(workflow):
            raise ComfyUIError("ComfyUI userdata 文件不是受支持的 UI workflow canvas")
        return SavedWorkflowDocument(
            source_path=normalized,
            sha256=hashlib.sha256(payload).hexdigest(),
            size_bytes=len(payload),
            workflow=workflow,
        )

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
        if len(payload) > self.SAVED_WORKFLOW_MAX_BYTES:
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

    def release_resources_before_submit(self) -> dict | None:
        """Run one configured global release immediately before a new submit.

        This method never calls ``/prompt``.  It is safe only with the
        database-backed single media-worker gate; a wider concurrency setting
        fails before touching ComfyUI so one submit cannot unload another.
        """

        policy = self.pre_submit_resource_release_policy
        if not policy or not policy["enabled"]:
            return None
        from studio.services.json_contracts import json_contract

        attempted_at = timezone.now()
        policy_sha256 = json_contract(
            policy,
            max_bytes=_stage_snapshot_max_bytes(),
        ).sha256

        def audit(*, status: str, code: str = "", http_status: int | None = None) -> dict:
            return {
                "schema_version": 1,
                "status": status,
                "policy_sha256": policy_sha256,
                "unload_models": policy["unload_models"],
                "free_memory": policy["free_memory"],
                "attempted_at": attempted_at.isoformat(),
                "completed_at": timezone.now().isoformat(),
                "error_code": code,
                "http_status": http_status,
            }

        if int(settings.TASK_MAX_CONCURRENT) != 1:
            result = audit(
                status="failed",
                code="comfyui_resource_release_exclusive_worker_required",
            )
            raise ComfyUIPreSubmitResourceReleaseError(
                result["error_code"],
                "全局显存释放要求 TASK_MAX_CONCURRENT=1；未调用 ComfyUI /free 或 /prompt",
                audit=result,
            )
        try:
            response = self._request(
                "POST",
                "/free",
                json={
                    "unload_models": policy["unload_models"],
                    "free_memory": policy["free_memory"],
                },
            )
        except ComfyUIHTTPError as exc:
            result = audit(
                status="failed",
                code="comfyui_resource_release_rejected",
                http_status=exc.status_code,
            )
            raise ComfyUIPreSubmitResourceReleaseError(
                result["error_code"],
                "ComfyUI 明确拒绝提交前资源释放；未调用 /prompt",
                audit=result,
            ) from exc
        except ComfyUIError as exc:
            result = audit(
                status="failed",
                code="comfyui_resource_release_unconfirmed",
            )
            raise ComfyUIPreSubmitResourceReleaseError(
                result["error_code"],
                "无法确认 ComfyUI 提交前资源释放；未调用 /prompt",
                audit=result,
            ) from exc
        return audit(
            status="succeeded",
            http_status=int(getattr(response, "status_code", 200) or 200),
        )

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

    def wait_for_completion(
        self,
        prompt_id: str,
        *,
        timeout: float,
        poll_interval: float,
    ) -> dict:
        timeout_ceiling = boundary_number(
            "security.runtime.max_timeout_seconds",
            minimum=0.001,
        )
        interval_ceiling = boundary_number(
            "security.runtime.max_interval_seconds",
            minimum=0.001,
        )
        if (
            isinstance(timeout, bool)
            or not isinstance(timeout, (int, float))
            or not math.isfinite(float(timeout))
            or not 0 < float(timeout) <= timeout_ceiling
        ):
            raise ComfyUIError("wait_for_completion.timeout 必须是安全边界内的有限正数")
        if (
            isinstance(poll_interval, bool)
            or not isinstance(poll_interval, (int, float))
            or not math.isfinite(float(poll_interval))
            or not 0 < float(poll_interval) <= interval_ceiling
        ):
            raise ComfyUIError("wait_for_completion.poll_interval 必须是安全边界内的有限正数")
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
            time.sleep(float(poll_interval))
        raise ComfyUITimeout(f"等待 ComfyUI 任务 {prompt_id} 超时")

    @staticmethod
    def extract_outputs(history: dict) -> list[dict]:
        found = []
        for node_id, node_outputs in (history.get("outputs") or {}).items():
            if not isinstance(node_outputs, dict):
                continue
            text_values: list[str] = []
            text_contract_present = False
            for key in ("text", "strings"):
                if key not in node_outputs:
                    continue
                text_contract_present = True
                value = node_outputs.get(key)
                if isinstance(value, str):
                    text_values.append(value)
                elif isinstance(value, list) and all(isinstance(item, str) for item in value):
                    text_values.extend(value)
                else:
                    raise ComfyUIError("ComfyUI text 输出合同无效")
            if text_contract_present:
                text = "\n".join(text_values).strip()
                payload = text.encode("utf-8")
                if not payload:
                    raise ComfyUIError("ComfyUI text 输出为空")
                if len(payload) > ComfyUIClient.TEXT_OUTPUT_MAX_BYTES:
                    raise ComfyUIError("ComfyUI text 输出超过 64 KiB 上限")
                found.append({
                    "node_id": str(node_id),
                    "kind": "text",
                    "text": text,
                    "size_bytes": len(payload),
                    "sha256": hashlib.sha256(payload).hexdigest(),
                })
            for kind in ("images", "gifs", "videos", "audio"):
                for item in node_outputs.get(kind, []) or []:
                    if isinstance(item, dict):
                        found.append({"node_id": node_id, "kind": kind, **item})
                    else:
                        found.append({"node_id": node_id, "kind": kind, "value": item})
        return found
