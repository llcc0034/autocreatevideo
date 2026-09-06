from __future__ import annotations

from dataclasses import dataclass
from pathlib import Path, PurePosixPath
import re
import stat
import unicodedata

from django.conf import settings

from studio.models import ModelArtifact


DEFAULT_MODEL_FOLDER_BY_ROLE = {
    ModelArtifact.Role.CHECKPOINT: "checkpoints",
    ModelArtifact.Role.DIFFUSION_MODEL: "diffusion_models",
    ModelArtifact.Role.TEXT_ENCODER: "text_encoders",
    ModelArtifact.Role.VAE: "vae",
    ModelArtifact.Role.LORA: "loras",
}
_FOLDER = re.compile(r"^[A-Za-z0-9][A-Za-z0-9._-]{0,119}$")
_SELECTOR = re.compile(r"^[A-Za-z0-9][A-Za-z0-9_.:+-]{0,190}$")
_WINDOWS_INVALID = frozenset('<>:"|?*')
_WINDOWS_RESERVED = frozenset({
    "con", "prn", "aux", "nul",
    *(f"com{index}" for index in range(1, 10)),
    *(f"lpt{index}" for index in range(1, 10)),
})
_FILE_ATTRIBUTE_REPARSE_POINT = 0x400


@dataclass(frozen=True)
class ArtifactInventorySpec:
    folder: str
    source: str
    node_type: str = ""
    input_name: str = ""


def artifact_inventory_spec(artifact: ModelArtifact) -> ArtifactInventorySpec:
    default_folder = DEFAULT_MODEL_FOLDER_BY_ROLE.get(artifact.role, "")
    folder = str(artifact.inventory_folder or default_folder).strip()
    node_type = str(artifact.inventory_node_type or "").strip()
    input_name = str(artifact.inventory_input_name or "").strip()
    if not folder or _FOLDER.fullmatch(folder) is None:
        raise ValueError("artifact 缺少受控 ComfyUI inventory_folder")
    if bool(node_type) != bool(input_name):
        raise ValueError("inventory_node_type 与 inventory_input_name 必须同时配置")
    if node_type:
        if _SELECTOR.fullmatch(node_type) is None or _SELECTOR.fullmatch(input_name) is None:
            raise ValueError("artifact object_info inventory selector 无效")
        return ArtifactInventorySpec(
            folder=folder,
            source="object_info",
            node_type=node_type,
            input_name=input_name,
        )
    if folder != default_folder:
        raise ValueError("自定义 inventory_folder 必须配置 object_info 节点/input 选择器")
    return ArtifactInventorySpec(folder=folder, source="models_api")


def object_info_inventory_names(
    object_info: dict,
    spec: ArtifactInventorySpec,
) -> set[str]:
    if spec.source != "object_info" or not isinstance(object_info, dict):
        raise ValueError("artifact inventory 不是 object_info selector")
    node = object_info.get(spec.node_type)
    inputs = node.get("input") if isinstance(node, dict) else None
    if not isinstance(inputs, dict):
        raise ValueError("object_info 缺少自定义模型 loader")
    definition = None
    for category in ("required", "optional"):
        values = inputs.get(category)
        if isinstance(values, dict) and spec.input_name in values:
            definition = values[spec.input_name]
            break
    choices = safe_model_inventory_options(definition)
    if not choices:
        raise ValueError("object_info 自定义模型 input 未返回文件枚举")
    return set(choices)


def object_info_input_options(definition) -> tuple[str, ...]:
    """Return the two reviewed Comfy enum encodings as filename strings."""

    if not isinstance(definition, (list, tuple)) or not definition:
        return ()
    raw_type = definition[0]
    config = definition[1] if len(definition) > 1 and isinstance(definition[1], dict) else {}
    if isinstance(raw_type, (list, tuple)):
        raw_options = raw_type
    elif raw_type in {"COMBO", "COMFY_DYNAMICCOMBO_V3"}:
        raw_options = config.get("options")
    else:
        return ()
    if not isinstance(raw_options, (list, tuple)):
        return ()
    values = []
    for item in raw_options:
        if isinstance(item, dict):
            value = item.get("key")
        else:
            value = item
        if isinstance(value, str) and value and value not in values:
            values.append(value)
    return tuple(values)


def exact_object_info_model_option(
    object_info: dict,
    *,
    node_type: str,
    input_name: str,
    expected_filename: str,
) -> str:
    """Resolve one safe model filename to the exact Comfy enum spelling.

    ``ModelArtifact.filename`` and ``WorkflowModelRequirement.expected_filename``
    deliberately use a platform-neutral, forward-slash representation.  A
    ComfyUI node can nevertheless expose the same file with Windows
    backslashes and validates the submitted widget against that exact string.
    Resolve only through the target deployment's ``object_info`` enum and fail
    closed when the canonical filename is absent or has more than one spelling.
    """

    expected = safe_model_relative_filename(expected_filename)
    if expected is None:
        raise ValueError("model artifact filename 不是安全的 models 相对路径")
    if not isinstance(object_info, dict):
        raise ValueError("object_info 响应无效")
    node = object_info.get(str(node_type or ""))
    inputs = node.get("input") if isinstance(node, dict) else None
    if not isinstance(inputs, dict):
        raise ValueError(f"object_info 缺少模型节点 {node_type}")
    definitions = []
    for category in ("required", "optional"):
        values = inputs.get(category)
        if isinstance(values, dict) and input_name in values:
            definitions.append(values[input_name])
    if len(definitions) != 1:
        raise ValueError(
            f"object_info 模型输入 {node_type}.{input_name} 必须唯一声明",
        )
    matches = []
    for raw_value in object_info_input_options(definitions[0]):
        if (
            safe_model_relative_filename(raw_value) == expected
            and raw_value not in matches
        ):
            matches.append(raw_value)
    if not matches:
        raise ValueError(
            f"object_info 模型输入 {node_type}.{input_name} 缺少 {expected_filename}",
        )
    if len(matches) != 1:
        raise ValueError(
            f"object_info 模型输入 {node_type}.{input_name} 对 {expected_filename} "
            "返回多个等价枚举值",
        )
    return matches[0]


def safe_model_inventory_options(definition) -> tuple[str, ...]:
    """Normalize model-only enum values without changing general Comfy enums."""

    values = []
    for raw_value in object_info_input_options(definition):
        normalized = safe_model_relative_filename(raw_value)
        if normalized is not None and normalized not in values:
            values.append(normalized)
    return tuple(values)


def safe_model_relative_filename(value: str) -> str | None:
    """Return one canonical models-relative name or fail closed.

    Comfy inventory values eventually become Windows paths on the supported
    portable runtime.  Validate Windows semantics on every platform so NTFS
    alternate data streams, device aliases and control characters cannot be
    introduced on one host and consumed on another.
    """

    raw = str(value or "")
    normalized = unicodedata.normalize("NFC", raw).replace("\\", "/")
    relative = PurePosixPath(normalized)
    if (
        not normalized
        or len(normalized) > 500
        or normalized.startswith("//")
        or normalized != relative.as_posix()
        or relative.is_absolute()
        or not relative.parts
    ):
        return None
    for part in relative.parts:
        if (
            part in {"", ".", ".."}
            or part.endswith((" ", "."))
            or any(
                ord(character) < 32
                or ord(character) == 127
                or character in _WINDOWS_INVALID
                for character in part
            )
            or part.split(".", 1)[0].casefold() in _WINDOWS_RESERVED
        ):
            return None
    return relative.as_posix()


def _is_link_or_reparse(path: Path) -> bool:
    value = path.lstat()
    return bool(
        stat.S_ISLNK(value.st_mode)
        or (
            int(getattr(value, "st_file_attributes", 0))
            & _FILE_ATTRIBUTE_REPARSE_POINT
        )
    )


def _plain_existing_path(path: Path) -> bool:
    """Reject symlinks/junctions in every existing path component."""

    absolute = Path(path)
    if not absolute.is_absolute():
        return False
    current = Path(absolute.anchor)
    try:
        for part in absolute.parts[1:]:
            current = current / part
            if _is_link_or_reparse(current):
                return False
    except OSError:
        return False
    return True


def controlled_local_models_root() -> Path | None:
    output_raw = str(getattr(settings, "COMFYUI_OUTPUT_DIR", "") or "").strip()
    if not output_raw:
        return None
    output_path = Path(output_raw)
    if not output_path.is_absolute():
        return None
    lexical_root = output_path.parent / "models"
    if not _plain_existing_path(lexical_root):
        return None
    try:
        root = lexical_root.resolve(strict=True)
    except OSError:
        return None
    if not root.is_dir():
        return None
    return root


def controlled_local_model_path(folder: str, filename: str) -> Path | None:
    models_root = controlled_local_models_root()
    if models_root is None or _FOLDER.fullmatch(str(folder or "")) is None:
        return None
    safe_filename = safe_model_relative_filename(filename)
    if safe_filename is None:
        return None
    relative = PurePosixPath(safe_filename)
    # Custom-node object_info enums commonly return a models-root-relative
    # value such as ``SEEDVR2/model.safetensors`` even though the inventory
    # selector already declares folder=SEEDVR2. Avoid joining the folder twice.
    if relative.parts[0].casefold() == str(folder).casefold():
        if len(relative.parts) == 1:
            return None
        relative = PurePosixPath(*relative.parts[1:])
    folder_root = models_root / folder
    candidate = folder_root / Path(*relative.parts)
    if (
        not candidate.is_relative_to(folder_root)
        or not _plain_existing_path(candidate)
    ):
        return None
    try:
        candidate_stat = candidate.lstat()
    except OSError:
        return None
    if not stat.S_ISREG(candidate_stat.st_mode):
        return None
    return candidate
