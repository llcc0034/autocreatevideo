"""Registered, versioned resolution primitives; never infer size from model names."""
from __future__ import annotations

from copy import deepcopy
from pathlib import Path
import warnings

from django.core.exceptions import ValidationError
from PIL import Image, ImageOps, UnidentifiedImageError

from studio.services.security_boundaries import boundary_int


class WorkflowResolutionError(ValueError):
    pass


# ComfyUI comfy_extras/nodes_flux.py FluxKontextImageScale.execute. This is
# executable adapter semantics, not a model default or editable UI setting.
KONTEXT_RESOLUTIONS = (
    (672, 1568), (688, 1504), (720, 1456), (752, 1392), (800, 1328),
    (832, 1248), (880, 1184), (944, 1104), (1024, 1024), (1104, 944),
    (1184, 880), (1248, 832), (1328, 800), (1392, 752), (1456, 720),
    (1504, 688), (1568, 672),
)
SOURCE_RESOLUTION_ADAPTER = "comfy.flux_kontext_image_scale/v1"
SOURCE_DIMENSION_MULTIPLE = 16
CONFIGURED_RESOLUTION_ADAPTER = "comfy.image_scale_candidates/v1"
def image_resolution_limits() -> dict:
    limits = {key: boundary_int(f"security.workflow.image_resolution_{key}")
              for key in ("min_dimension", "max_dimension", "max_pixels", "max_candidates")}
    if limits["min_dimension"] > limits["max_dimension"]:
        raise WorkflowResolutionError("图片缩放安全边界的最小尺寸大于最大尺寸，请在安全边界中修正")
    return {**limits, "multiple": SOURCE_DIMENSION_MULTIPLE}


def image_resolution_options() -> dict:
    limits = image_resolution_limits()
    return {
        "native_adapter": SOURCE_RESOLUTION_ADAPTER,
        "configured_adapter": CONFIGURED_RESOLUTION_ADAPTER,
        "algorithm": "按输入图片宽高比选择误差最小的候选尺寸；等距时按宽、高升序选择。Lanczos 缩放并居中裁切，不拉伸。",
        "modes": [{"value": "native", "label": "跟随原节点规则"},
                  {"value": "configured", "label": "配置目标尺寸"}],
        "native_sizes": [{"width": w, "height": h} for w, h in KONTEXT_RESOLUTIONS],
        "presets": [{"width": w, "height": h, "label": f"{w} × {h}"}
                    for w, h in sorted(set(KONTEXT_RESOLUTIONS) | {(512, 512), (768, 768), (1024, 576), (576, 1024), (2048, 2048)})
                    if limits["min_dimension"] <= min(w, h) <= max(w, h) <= limits["max_dimension"]
                    and w * h <= limits["max_pixels"]],
        "limits": limits,
    }


def validate_image_resolution_policy(policy: dict) -> list[tuple[int, int]]:
    if not isinstance(policy, dict) or set(policy) != {"adapter", "sizes"} or policy.get("adapter") != CONFIGURED_RESOLUTION_ADAPTER:
        raise WorkflowResolutionError("尺寸配置必须引用已注册的候选尺寸缩放规则，并提供 sizes")
    limits = image_resolution_limits()
    sizes = policy["sizes"]
    if not isinstance(sizes, list) or not 1 <= len(sizes) <= limits["max_candidates"]:
        raise WorkflowResolutionError(f"请选择 1–{limits['max_candidates']} 组目标尺寸；单组为固定尺寸，多组按原图宽高比选择")
    result = []
    for row in sizes:
        if not isinstance(row, dict) or set(row) != {"width", "height"}:
            raise WorkflowResolutionError("目标尺寸只能包含 width、height")
        pair = (row["width"], row["height"])
        if any(type(value) is not int or not limits["min_dimension"] <= value <= limits["max_dimension"]
               or value % SOURCE_DIMENSION_MULTIPLE for value in pair) or pair[0] * pair[1] > limits["max_pixels"]:
            raise WorkflowResolutionError(f"目标宽高必须为 {limits['min_dimension']}–{limits['max_dimension']} 的 {SOURCE_DIMENSION_MULTIPLE} 倍数，总像素不能超过 {limits['max_pixels']}")
        if pair in result:
            raise WorkflowResolutionError("目标尺寸不能重复")
        result.append(pair)
    return result


def configure_source_image_resolution(strategy: dict, policy: dict) -> dict:
    validate_image_resolution_policy(policy)
    if not isinstance(strategy, dict) or strategy.get("adapter") != SOURCE_RESOLUTION_ADAPTER:
        raise WorkflowResolutionError("所选尺寸配置要求主输出由 LoadImage → FluxKontextImageScale 驱动；当前工作流不满足，不能忽略配置")
    return {**strategy, "adapter": CONFIGURED_RESOLUTION_ADAPTER, "sizes": deepcopy(policy["sizes"])}


def resolution_dimensions(strategy: dict, width: int, height: int) -> tuple[int, int]:
    if strategy.get("adapter") == SOURCE_RESOLUTION_ADAPTER:
        if "sizes" in strategy:
            raise WorkflowResolutionError("原节点尺寸规则不能携带自定义候选尺寸")
        return kontext_dimensions(width, height)
    pairs = validate_image_resolution_policy({"adapter": strategy.get("adapter"), "sizes": strategy.get("sizes")})
    if any(type(value) is not int or value < 1 for value in (width, height)):
        raise WorkflowResolutionError("输入图片宽高必须为正整数")
    _, w, h = min((abs(width / height - w / h), w, h) for w, h in pairs)
    return w, h


def materialize_resolution_graph(graph: dict, strategy: dict, width: int, height: int) -> dict:
    """Use the same rewrite for live probes and real execution; preserve source facts."""
    if strategy.get("adapter") != CONFIGURED_RESOLUTION_ADAPTER:
        return graph
    pairs = validate_image_resolution_policy({"adapter": strategy.get("adapter"), "sizes": strategy.get("sizes")})
    if (width, height) not in pairs:
        raise WorkflowResolutionError("执行尺寸不属于冻结的候选尺寸")
    result = deepcopy(graph)
    ids = strategy.get("node_ids")
    if not isinstance(ids, list) or len(ids) != 1:
        raise WorkflowResolutionError("尺寸执行规则缺少唯一缩放节点")
    node = result.get(ids[0])
    if not isinstance(node, dict) or node.get("class_type") != "FluxKontextImageScale":
        raise WorkflowResolutionError("冻结的缩放节点已漂移")
    node["class_type"] = "ImageScale"
    node["inputs"] = {"image": deepcopy(node["inputs"]["image"]), "width": width, "height": height,
                      "upscale_method": "lanczos", "crop": "center"}
    return result


def kontext_dimensions(width: int, height: int) -> tuple[int, int]:
    if any(type(value) is not int or value < 1 for value in (width, height)):
        raise WorkflowResolutionError("输入图片宽高必须为正整数")
    # Keep ComfyUI's tuple tie-break (distance, width, height), not list order.
    _, target_width, target_height = min(
        (abs(width / height - w / h), w, h) for w, h in KONTEXT_RESOLUTIONS
    )
    return target_width, target_height


def infer_source_image_resolution(graph: dict, bindings: list, primary_id: str) -> dict:
    """Prove the actual output size path, excluding unrelated conditioning images.

    Only registered paths are accepted. In particular a scaler elsewhere in the
    reachable graph does not prove output size, nor do unknown switches/upscalers.
    """
    path = []
    node_id = primary_id

    def node():
        value = graph.get(node_id)
        if not isinstance(value, dict) or not isinstance(value.get("inputs"), dict):
            raise WorkflowResolutionError("主输出尺寸链包含不存在的节点")
        return value

    def follow(input_name):
        nonlocal node_id
        link = node()["inputs"].get(input_name)
        if (not isinstance(link, list) or len(link) != 2
                or not isinstance(link[0], str) or type(link[1]) is not int or link[1] != 0):
            raise WorkflowResolutionError(f"尺寸链 {node_id}.{input_name} 必须连接受支持节点的第 0 输出")
        if node_id in path:
            raise WorkflowResolutionError("主输出尺寸链存在循环")
        path.append(node_id)
        node_id = link[0]

    if node().get("class_type") not in {"SaveImage", "SaveImageAdvanced", "PreviewImage"}:
        raise WorkflowResolutionError("输入图推导尺寸目前只支持图片主输出")
    follow("images")
    if node().get("class_type") == "VAEDecode":
        vae = node()["inputs"].get("vae")
        follow("samples")
        if node().get("class_type") not in {"KSampler", "KSamplerAdvanced"}:
            raise WorkflowResolutionError("主输出 latent 尺寸链不是已注册的采样器")
        follow("latent_image")
        if node().get("class_type") != "VAEEncode" or not isinstance(vae, list) or node()["inputs"].get("vae") != vae:
            raise WorkflowResolutionError("尺寸链必须使用同一个 VAE 编码和解码")
        follow("pixels")
    if node().get("class_type") != "FluxKontextImageScale":
        raise WorkflowResolutionError("主输出尺寸链缺少直接驱动的 FluxKontextImageScale，或含未支持的缩放/分支节点")
    scale_id = node_id
    follow("image")
    if node().get("class_type") != "LoadImage" or node_id in path:
        raise WorkflowResolutionError("尺寸驱动节点必须直接读取受管 LoadImage 素材")
    source_id = node_id
    source_bindings = [row for row in bindings if str(row.get("node_id")) == source_id
                       and row.get("input_name") == "image" and row.get("value_type") == "asset_image"]
    if len(source_bindings) != 1 or not source_bindings[0].get("is_required", True):
        raise WorkflowResolutionError("决定尺寸的图片必须有唯一且必填的素材绑定")
    return {
        "mode": "source_image_derived",
        "adapter": SOURCE_RESOLUTION_ADAPTER,
        "node_ids": [scale_id],
        "asset_semantic_keys": [source_bindings[0]["semantic_key"]],
        "source_node_id": source_id,
        "primary_node_id": primary_id,
        "size_path": [*path, source_id],
    }


def validate_source_image_resolution(graph: dict, contract: dict, bindings: list) -> None:
    strategy = contract.get("resolution_strategy")
    primary = (contract.get("outputs") or {}).get("primary") or {}
    if primary.get("kind") != "image":
        raise WorkflowResolutionError("输入图推导尺寸目前只支持图片输出合同")
    expected = infer_source_image_resolution(graph, bindings, str(primary.get("node_id") or ""))
    if isinstance(strategy, dict) and strategy.get("adapter") == CONFIGURED_RESOLUTION_ADAPTER:
        expected = configure_source_image_resolution(expected, {"adapter": strategy["adapter"], "sizes": strategy.get("sizes")})
    if strategy != expected:
        raise WorkflowResolutionError("source_image_derived 缺少完整的已注册尺寸合同，或尺寸来源/路径已漂移；请重新生成合同草稿")
    if type(contract.get("required_dimension_multiple")) is not int or contract["required_dimension_multiple"] != SOURCE_DIMENSION_MULTIPLE:
        raise WorkflowResolutionError("输入图尺寸合同 required_dimension_multiple 必须与已注册算法一致")
    for field in ("parameters", "static_semantics"):
        if {"image.width", "image.height", "image.aspect_ratio", "image.megapixels"} & set(contract.get(field) or {}):
            raise WorkflowResolutionError("输入图推导尺寸不得被手填或静态宽高覆盖")
    if contract.get("asset_groups"):
        raise WorkflowResolutionError("输入图推导尺寸暂不支持动态素材组改写尺寸链；请保留固定的尺寸驱动图片槽")


def _decoded_dimensions(path: Path) -> tuple[int, int]:
    try:
        with warnings.catch_warnings(), path.open("rb") as stream:
            warnings.simplefilter("error", Image.DecompressionBombWarning)
            with Image.open(stream) as image:
                if getattr(image, "n_frames", 1) != 1:
                    raise WorkflowResolutionError("尺寸来源只支持静态单帧图片")
                image.load()
                # LoadImage also applies EXIF orientation before scaling.
                return ImageOps.exif_transpose(image).size
    except (OSError, ValueError, UnidentifiedImageError, Image.DecompressionBombError, Image.DecompressionBombWarning) as exc:
        if isinstance(exc, WorkflowResolutionError):
            raise
        raise WorkflowResolutionError("图片无法安全解码并证明尺寸") from exc




def source_image_resolution_facts(strategy, *, source, width, height):
    """Build output-size evidence using the one registered resolution rule."""
    target_width, target_height = resolution_dimensions(strategy, width, height)
    return {
        "adapter": strategy["adapter"],
        **({"sizes": deepcopy(strategy["sizes"])} if "sizes" in strategy else {}),
        "source_semantic_key": strategy["asset_semantic_keys"][0],
        **source,
        "source_width": width, "source_height": height,
        "width": target_width, "height": target_height,
    }


def resolve_prepared_image_resolution(strategy, asset_bindings):
    """Use already verified per-use observations, never original graph sizes."""
    key = strategy["asset_semantic_keys"][0]
    matches = [row for row in asset_bindings if row.get("semantic_key") == key]
    if len(matches) != 1:
        raise WorkflowResolutionError("缺少决定输出尺寸的唯一参考图片使用")
    binding = matches[0]
    media = binding.get("media_facts")
    observed = media.get("observed_media") if isinstance(media, dict) else None
    if (binding.get("kind") != "image" or not isinstance(observed, dict)
            or observed.get("observation_source") != "image_decoder" or type(observed.get("frame_count")) is not int
            or observed["frame_count"] != 1):
        raise WorkflowResolutionError("尺寸来源缺少真实单帧图片观测，不能按文件名或导入示例猜测")
    if not binding.get("item_key") or not (binding.get("asset_id") or binding.get("artifact_id")) or not media.get("sha256"):
        raise WorkflowResolutionError("尺寸来源缺少明确素材使用与内容身份")
    return source_image_resolution_facts(strategy, source={"source_item_key": binding["item_key"],
        "source_asset_id": binding.get("asset_id"), "source_artifact_id": binding.get("artifact_id"),
        "source_checksum": media["sha256"], "source_sha256": binding["source_sha256"]},
        width=observed.get("width"), height=observed.get("height"))


def validate_resolved_image_output(path: Path, output_contract: dict) -> None:
    """All three execution paths verify bytes against the same frozen size facts."""
    if output_contract.get("schema_version") != 2:
        return  # Historical v1 outputs never carried a derived-size assertion.
    evidence = output_contract.get("resolution")
    if (not isinstance(evidence, dict) or evidence.get("adapter") not in {SOURCE_RESOLUTION_ADAPTER, CONFIGURED_RESOLUTION_ADAPTER}
            or output_contract.get("primary", {}).get("kind") != "image"):
        raise ValidationError("冻结的输入图尺寸证据无效")
    try:
        expected = resolution_dimensions(evidence, evidence.get("source_width"), evidence.get("source_height"))
        if (evidence.get("width"), evidence.get("height")) != expected:
            raise WorkflowResolutionError("冻结输出宽高与尺寸算法不一致")
        if _decoded_dimensions(path) != expected:
            raise WorkflowResolutionError(f"实际图片尺寸与冻结合同不一致，预期 {expected[0]}×{expected[1]}")
    except WorkflowResolutionError as exc:
        raise ValidationError(str(exc)) from exc
