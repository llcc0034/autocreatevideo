"""Shared frozen primary-output verification for original Comfy workers."""
from pathlib import Path, PurePosixPath
import hashlib

from django.conf import settings
from django.core.exceptions import ValidationError

from studio.media_signing import classify_task_output
from studio.services.generated_artifacts import verified_local_media_facts
from studio.services.provider_registry import ProviderDriverError


def frozen_comfy_output_prefix(compiled, *, preparation_id):
    """File outputs need a task namespace; inline text has no file prefix."""
    primary = compiled.get("output_contract", {}).get("primary")
    semantics = compiled.get("resolved_semantics")
    if not isinstance(primary, dict) or not isinstance(semantics, dict):
        raise ValidationError("冻结工作流缺少输出合同或已解析参数")
    prefix = semantics.get("output.prefix")
    if prefix is None and "output.prefix" not in semantics and primary.get("kind") == "text":
        return None
    if prefix != f"autoprompt/execution/{preparation_id}":
        raise ValidationError("工作流输出前缀必须属于本次冻结请求")
    return prefix


def comfy_output_path(item: dict, *, expected_prefix: str) -> Path:
    output_root_raw = str(getattr(settings, "COMFYUI_OUTPUT_DIR", "") or "").strip()
    if not output_root_raw:
        raise ProviderDriverError(
            "ComfyUI 执行需要可访问的受控输出目录",
            code="comfyui_output_unavailable",
        )
    filename_raw = str(item.get("filename") or "").replace("\\", "/")
    subfolder_raw = str(item.get("subfolder") or "").replace("\\", "/")
    filename = PurePosixPath(filename_raw).name
    subfolder = PurePosixPath(subfolder_raw)
    if (
        not filename
        or filename != filename_raw
        or subfolder.is_absolute()
        or ".." in subfolder.parts
        or str(item.get("type") or "") != "output"
    ):
        raise ProviderDriverError(
            "ComfyUI 主输出路径或存储类型不受信",
            code="comfyui_output_path_invalid",
        )
    expected_prefix = str(expected_prefix or "").replace("\\", "/").strip("/")
    expected_path = PurePosixPath(expected_prefix)
    expected_parent = PurePosixPath(*expected_path.parts[:-1])
    expected_name = expected_path.name
    invalid_windows_name = any(
        ord(character) < 32
        or ord(character) == 127
        or character in '<>:"|?*'
        for character in filename
    )
    if (
        not expected_prefix
        or expected_path.is_absolute()
        or ".." in expected_path.parts
        or subfolder != expected_parent
        or not expected_name
        or not filename.startswith(f"{expected_name}_")
        or invalid_windows_name
    ):
        raise ProviderDriverError(
            "ComfyUI 主输出未绑定本任务的冻结输出前缀",
            code="comfyui_output_path_invalid",
        )
    root = Path(output_root_raw).resolve()
    candidate = (root / Path(*subfolder.parts) / filename).resolve()
    if not candidate.is_relative_to(root) or not candidate.is_file():
        raise ProviderDriverError(
            "ComfyUI 主输出文件不存在或越过受控目录",
            code="comfyui_output_path_invalid",
        )
    return candidate


def select_comfy_primary_output(collected, *, output_contract, expected_kind):
    """Identify one frozen output without treating its content as valid."""
    primary = output_contract.get("primary")
    if not isinstance(primary, dict) or primary.get("kind") != expected_kind or not primary.get("node_id"):
        raise ValidationError("冻结主输出合同缺少明确节点或类型")
    outputs = collected.get("outputs") if isinstance(collected, dict) else None
    if not isinstance(outputs, list):
        raise ValidationError("ComfyUI 输出回执必须包含文件列表")
    matching = [item for item in outputs if isinstance(item, dict)
        and str(item.get("node_id", "")) == str(primary["node_id"])
        and ((expected_kind == "text" and item.get("kind") == "text") or classify_task_output(item) == expected_kind)]
    if len(matching) != 1:
        raise ProviderDriverError("ComfyUI 冻结主输出节点必须且只能产生一个类型匹配的文件", code="comfyui_primary_output_invalid")
    return matching[0]


def verify_comfy_primary_output(collected, *, output_contract, expected_kind, expected_prefix, resolved_semantics, max_bytes):
    """Match the exact frozen node and inspect actual bytes, not receipt claims."""
    item = select_comfy_primary_output(collected, output_contract=output_contract, expected_kind=expected_kind)
    node_id = str(output_contract["primary"]["node_id"])
    if expected_kind == "text":
        text = item.get("text")
        if not isinstance(text, str) or not text.strip():
            raise ValidationError("工作流文本输出为空或格式无效")
        encoded = text.encode("utf-8")
        digest = hashlib.sha256(encoded).hexdigest()
        if len(encoded) > max_bytes or item.get("size_bytes") != len(encoded) or item.get("sha256") != digest:
            raise ValidationError("工作流文本输出大小或摘要不一致")
        return None, {"kind": "text", "text": text, "size_bytes": len(encoded), "sha256": digest, "node_id": node_id}
    source = comfy_output_path(item, expected_prefix=expected_prefix)
    facts = verified_local_media_facts(source, expected_kind=expected_kind, max_bytes=max_bytes,
        include_observed_media=True)
    from studio.services.workflow_resolution import validate_resolved_image_output
    validate_resolved_image_output(source, output_contract)
    if expected_kind in {"image", "video"}:
        observed = facts.get("observed_media")
        if not isinstance(observed, dict):
            raise ValidationError("输出缺少真实媒体探测事实")
        mismatches = []
        dimensions = (("image.width", "width"), ("image.height", "height"))
        observed_parameters = dimensions + ((("video.frames", "frames"), ("video.fps", "fps")) if expected_kind == "video" else ())
        for semantic, key in observed_parameters:
            expected = resolved_semantics.get(semantic)
            if isinstance(expected, (int, float)) and not isinstance(expected, bool) and expected > 0 and observed.get(key) != expected:
                mismatches.append(semantic)
        if expected_kind == "video" and resolved_semantics.get("audio.enabled") is True and observed.get("has_audio") is not True:
            mismatches.append("audio.enabled")
        if mismatches:
            raise ValidationError("工作流输出未满足冻结参数: " + ", ".join(mismatches))
    # Carry the verified contract identity into the evidence envelope, just as
    # inline text does. The byte probe supplies MIME/hash/media observations,
    # but deliberately has no knowledge of this workflow's primary node.
    return source, {**facts, "kind": expected_kind, "node_id": node_id}
