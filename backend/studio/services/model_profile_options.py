from __future__ import annotations

from typing import Any

from django.core.exceptions import ValidationError

from studio.models import (
    ModelArtifact,
    ModelProfileVersion,
    PromptProfileVersion,
    ProviderProfile,
)
from studio.services.model_profile_contracts import (
    verified_model_profile_specification,
)
from studio.services.provider_registry import registered_driver_capabilities


MODEL_PROFILE_SPECIFICATION_SCHEMA_VERSION = 1


# This is the control-plane vocabulary for model business capabilities.  It is
# deliberately separate from Provider transport operations such as submit,
# poll, collect and chat_completion.  Execution still requires the published
# model contract, a compatible Deployment/Workflow and fresh Probe/Smoke
# evidence; selecting an entry here grants no executable capability by itself.
MODEL_CAPABILITY_CATALOG: tuple[dict[str, Any], ...] = (
    {
        "value": "text.structured",
        "label": "通用结构化文本",
        "modalities": ("text", "multimodal"),
        "driver_types": ("local_qwen", "deepseek_api", "openai_compatible"),
    },
    {
        "value": "script.generate",
        "label": "剧本生成",
        "modalities": ("text", "multimodal"),
        "driver_types": ("local_qwen", "deepseek_api", "openai_compatible"),
    },
    {
        "value": "script.structure",
        "label": "剧本结构化",
        "modalities": ("text", "multimodal"),
        "driver_types": ("local_qwen", "deepseek_api", "openai_compatible"),
    },
    {
        "value": "prompt.normalize",
        "label": "提示词规范化",
        "modalities": ("text", "multimodal"),
        "driver_types": ("local_qwen", "deepseek_api", "openai_compatible"),
    },
    {
        "value": "visual_rules.generate",
        "label": "全剧影视规则生成",
        "modalities": ("text", "multimodal"),
        "driver_types": ("local_qwen", "deepseek_api", "openai_compatible"),
    },
    {
        "value": "prompt.fields.generate",
        "label": "提示词字段生成",
        "modalities": ("text", "multimodal"),
        "driver_types": ("local_qwen", "deepseek_api", "openai_compatible"),
    },
    {
        "value": "prompt.fields.translate",
        "label": "提示词字段翻译",
        "modalities": ("text", "multimodal"),
        "driver_types": ("local_qwen", "deepseek_api", "openai_compatible"),
    },
    {
        "value": "prompt.draft",
        "label": "提示词起草",
        "modalities": ("text", "multimodal"),
        "driver_types": ("local_qwen", "deepseek_api", "openai_compatible"),
    },
    {
        "value": "image.text_to_image",
        "label": "文生图",
        "modalities": ("image", "multimodal"),
        "driver_types": ("comfyui", "async_rest"),
    },
    {
        "value": "image.image_to_image",
        "label": "图片生图",
        "modalities": ("image", "multimodal"),
        "driver_types": ("comfyui",),
    },
    {
        "value": "video.text_to_video",
        "label": "文生视频",
        "modalities": ("video", "multimodal"),
        "driver_types": ("comfyui", "async_rest"),
    },
    {
        "value": "video.image_to_video",
        "label": "图片生视频",
        "modalities": ("video", "multimodal"),
        "driver_types": ("comfyui",),
    },
    {
        "value": "video.reference_to_video",
        "label": "参考生视频",
        "modalities": ("video", "multimodal"),
        "driver_types": ("comfyui",),
    },
    {
        "value": "video.first_last_frame_to_video",
        "label": "首尾帧生视频",
        "modalities": ("video", "multimodal"),
        "driver_types": ("comfyui",),
    },
    {
        "value": "audio.text_to_audio",
        "label": "文生音频",
        "modalities": ("audio", "multimodal"),
        "driver_types": ("comfyui", "async_rest"),
    },
)


def _normalized_string_values(value: Any, *, field: str) -> tuple[str, ...]:
    if value is None or value == "":
        return ()
    raw_values = value if isinstance(value, list) else str(value).split(",")
    values: list[str] = []
    for raw_value in raw_values:
        if not isinstance(raw_value, str):
            raise ValidationError({field: f"{field} 必须只包含字符串"})
        normalized = raw_value.strip()
        if normalized and normalized not in values:
            values.append(normalized)
    return tuple(values)


def provider_driver_selection_catalog() -> dict[str, dict[str, tuple[str, ...]]]:
    catalog: dict[str, dict[str, tuple[str, ...]]] = {}
    for raw_driver_type, declared in registered_driver_capabilities().items():
        driver_type = str(raw_driver_type)
        raw_modalities = declared.get("modalities")
        if not isinstance(raw_modalities, list):
            raw_modality = str(declared.get("modality") or "").strip()
            raw_modalities = [raw_modality] if raw_modality else []
        if driver_type == ProviderProfile.DriverType.ASYNC_REST:
            raw_modalities = ["image", "video", "audio"]
        catalog[driver_type] = {
            "modalities": tuple(sorted({
                str(item).strip() for item in raw_modalities if str(item).strip()
            })),
            "operations": tuple(sorted({
                str(item).strip()
                for item in (declared.get("operations") or [])
                if str(item).strip()
            })),
        }
    return catalog


def validate_provider_registry_selection(
    *,
    driver_type: str,
    modalities: Any,
    operations: Any,
    allowed_legacy_modalities: tuple[str, ...] = (),
    allowed_legacy_operations: tuple[str, ...] = (),
) -> tuple[tuple[str, ...], tuple[str, ...]]:
    driver_catalog = provider_driver_selection_catalog().get(str(driver_type))
    if driver_catalog is None:
        raise ValidationError({"driver_type": "未注册的 provider driver"})
    selected_modalities = _normalized_string_values(
        modalities,
        field="modalities",
    )
    selected_operations = _normalized_string_values(
        operations,
        field="operations",
    )
    if not selected_modalities:
        raise ValidationError({"modalities": "至少选择一个支持模态"})
    unknown_modalities = sorted(
        set(selected_modalities)
        - set(driver_catalog["modalities"])
        - set(allowed_legacy_modalities)
    )
    unknown_operations = sorted(
        set(selected_operations)
        - set(driver_catalog["operations"])
        - set(allowed_legacy_operations)
    )
    if unknown_modalities:
        raise ValidationError({
            "modalities": (
                "只能选择当前注册驱动提供的模态；未知值: "
                + ", ".join(unknown_modalities)
            ),
        })
    if unknown_operations:
        raise ValidationError({
            "operations": (
                "只能选择当前注册驱动提供的传输操作；未知值: "
                + ", ".join(unknown_operations)
            ),
        })
    return selected_modalities, selected_operations


def model_capability_values(specification: Any) -> tuple[str, ...]:
    if not isinstance(specification, dict):
        return ()
    raw_capabilities = specification.get("capabilities")
    if raw_capabilities is None:
        return ()
    if not isinstance(raw_capabilities, list):
        raise ValidationError({
            "specification": "specification.capabilities 必须是字符串数组",
        })
    return _normalized_string_values(
        raw_capabilities,
        field="specification.capabilities",
    )


def model_family_value(specification: Any) -> str:
    if not isinstance(specification, dict):
        return ""
    value = specification.get("model_family")
    if value is None or value == "":
        return ""
    if not isinstance(value, str):
        raise ValidationError({
            "specification": "specification.model_family 必须是字符串",
        })
    return value.strip()


def validate_model_profile_registry_selection(
    specification: dict[str, Any],
    *,
    profile_key: str,
    modality: str,
    driver_type: str,
    allowed_legacy_capabilities: tuple[str, ...] = (),
    allowed_model_families: tuple[str, ...] = (),
) -> None:
    capabilities = model_capability_values(specification)
    capability_catalog = {
        str(item["value"]): item for item in MODEL_CAPABILITY_CATALOG
    }
    unknown_capabilities = sorted(
        set(capabilities)
        - set(capability_catalog)
        - set(allowed_legacy_capabilities)
    )
    if unknown_capabilities:
        raise ValidationError({
            "specification": (
                "能力必须来自服务端受控目录；未知能力: "
                + ", ".join(unknown_capabilities)
            ),
        })
    incompatible_capabilities: list[str] = []
    for capability in capabilities:
        item = capability_catalog.get(capability)
        if item is None:
            # An unchanged legacy value remains visible and removable, but it
            # never grants a new executable capability.
            continue
        if (
            modality not in item["modalities"]
            or driver_type not in item["driver_types"]
        ):
            incompatible_capabilities.append(capability)
    if incompatible_capabilities:
        raise ValidationError({
            "specification": (
                "能力与所选 Provider 驱动或主模态不兼容: "
                + ", ".join(sorted(incompatible_capabilities))
            ),
        })

    family = model_family_value(specification)
    if not family:
        return
    _observed_capabilities, registered_families = _verified_model_facts()
    allowed_families = {
        str(value).strip() for value in allowed_model_families if str(value).strip()
    }
    allowed_families.update(registered_families)
    if profile_key:
        allowed_families.add(str(profile_key).strip())
    if family not in allowed_families:
        raise ValidationError({
            "specification": (
                "提示词策略族必须选择服务端已登记值；新模型仅可显式登记"
                "与模型版本族标识同名的策略族"
            ),
        })


def _choice_options(choices) -> list[dict[str, str]]:
    return [{"value": value, "label": str(label)} for value, label in choices]


def _verified_model_facts() -> tuple[set[str], set[str]]:
    capabilities: set[str] = set()
    model_families: set[str] = set(
        PromptProfileVersion.objects.filter(status__in=(
            PromptProfileVersion.Status.PUBLISHED,
            PromptProfileVersion.Status.RETIRED,
        ))
        .exclude(model_family="")
        .values_list("model_family", flat=True)
    )
    for profile in ModelProfileVersion.objects.filter(status__in=(
        ModelProfileVersion.Status.PUBLISHED,
        ModelProfileVersion.Status.RETIRED,
    )).only(
        "specification",
        "specification_schema_version",
        "specification_size_bytes",
        "specification_sha256",
    ):
        try:
            specification = verified_model_profile_specification(profile)
        except ValidationError:
            # Invalid historical rows remain visible in the registry, but they
            # cannot become an option source for new contracts.
            continue
        raw_capabilities = specification.get("capabilities")
        if isinstance(raw_capabilities, list):
            capabilities.update(
                str(item).strip() for item in raw_capabilities
                if isinstance(item, str) and item.strip()
            )
        family = str(specification.get("model_family") or "").strip().casefold()
        if family:
            model_families.add(family)
    return capabilities, model_families


def model_profile_registry_options() -> dict[str, Any]:
    from studio.services.execution_monitoring import model_execution_monitoring_editor_options
    from studio.services.media_capabilities import conditioning_contract_editor_options, media_constraints_editor_options
    from studio.services.execution_text_contracts import text_execution_editor_options
    from studio.services.model_profile_contracts import chat_request_options_editor_options
    from studio.services.script_model_policies import script_contract_editor_options
    from studio.services.production_visual_rules import visual_rules_output_editor_options
    observed_capabilities, model_families = _verified_model_facts()
    known_capabilities = {str(item["value"]) for item in MODEL_CAPABILITY_CATALOG}
    capability_options = [
        {
            **item,
            "modalities": list(item["modalities"]),
            "driver_types": list(item["driver_types"]),
            "legacy": False,
        }
        for item in MODEL_CAPABILITY_CATALOG
    ]
    capability_options.extend({
        "value": value,
        "label": f"历史已登记能力 · {value}",
        "modalities": [],
        "driver_types": [],
        "legacy": True,
    } for value in sorted(observed_capabilities - known_capabilities))

    driver_capabilities = provider_driver_selection_catalog()
    driver_labels = dict(ProviderProfile.DriverType.choices)
    provider_drivers = []
    for value, declared in driver_capabilities.items():
        provider_drivers.append({
            "value": value,
            "label": str(driver_labels.get(value, value)),
            "modalities": list(declared["modalities"]),
            "operations": list(declared["operations"]),
        })

    from studio.services.async_media_schema import async_media_editor_options

    return {
        "async_media": async_media_editor_options(),
        "specification_schema_version": MODEL_PROFILE_SPECIFICATION_SCHEMA_VERSION,
        "provider_drivers": provider_drivers,
        "modalities": _choice_options(ModelProfileVersion.Modality.choices),
        "artifact_roles": _choice_options(ModelArtifact.Role.choices),
        "capabilities": capability_options,
        "model_families": sorted({str(value) for value in model_families if value}),
        "conditioning_contract": conditioning_contract_editor_options(),
        "media_constraints": media_constraints_editor_options(),
        "execution_monitoring": model_execution_monitoring_editor_options(),
        "text_execution": text_execution_editor_options(MODEL_CAPABILITY_CATALOG),
        "chat_request_options": chat_request_options_editor_options(),
        "script_contracts": script_contract_editor_options(),
        "visual_rules_output": visual_rules_output_editor_options(),
    }
