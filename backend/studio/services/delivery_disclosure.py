from __future__ import annotations

from dataclasses import dataclass

from studio.services.configuration import resolve_configuration
from studio.services.security_boundaries import boundary_int


class DeliveryDisclosureError(ValueError):
    """The published delivery disclosure configuration is unusable."""


@dataclass(frozen=True)
class DeliveryDisclosure:
    visible_label_enabled: bool
    base_label: str
    label: str
    description: str
    model_key: str
    model: str
    media_kind: str


def _configured_disclosure(values=None) -> tuple[bool, str]:
    if values is None:
        values = resolve_configuration(required_keys=(
            "delivery.ai_label_enabled",
            "delivery.ai_label_text",
        )).values
    if not isinstance(values, dict) or any(key not in values for key in (
            "delivery.ai_label_enabled", "delivery.ai_label_text")):
        raise DeliveryDisclosureError("冻结配置缺少交付披露，不补当前配置")
    if type(values["delivery.ai_label_enabled"]) is not bool or type(values["delivery.ai_label_text"]) is not str:
        raise DeliveryDisclosureError("交付披露开关必须为布尔值，正文必须为文本")
    label = str(values["delivery.ai_label_text"] or "").strip()
    maximum_characters = boundary_int(
        "security.delivery.disclosure_text_max_characters"
    )
    folded = label.casefold()
    if (
        not label
        or len(label) > maximum_characters
        or any(character in label for character in "\r\n")
        or "ai" not in folded
        or ("generated" not in folded and "生成" not in label)
    ):
        raise DeliveryDisclosureError(
            "delivery.ai_label_text 必须是不含换行的 AI 生成内容披露文本"
        )
    return bool(values["delivery.ai_label_enabled"]), label


def resolve_delivery_disclosure(
    *,
    values=None,
    model: str = "",
    model_key: str = "",
    media_kind: str = "",
) -> DeliveryDisclosure:
    """Resolve disclosure from explicit frozen values or versioned system config.

    Model identity is supplied by the frozen execution/profile contract.  This
    function deliberately has no registry of model names and therefore works
    unchanged for newly configured image and video models.
    """

    enabled, base_label = _configured_disclosure(values)
    normalized_model = str(model or "").strip()
    normalized_key = str(model_key or normalized_model).strip()
    normalized_kind = str(media_kind or "").strip().casefold()
    if normalized_kind and normalized_kind not in {"image", "video", "audio"}:
        raise DeliveryDisclosureError("交付披露媒体类型无效")
    if any(character in normalized_model for character in "\r\n"):
        raise DeliveryDisclosureError("交付披露模型标识不得换行")
    if any(character in normalized_key for character in "\r\n"):
        raise DeliveryDisclosureError("交付披露模型键不得换行")

    label = base_label
    if normalized_model:
        label = f"{base_label} · {normalized_model}"
    maximum_characters = boundary_int(
        "security.delivery.disclosure_text_max_characters"
    )
    if len(label) > maximum_characters:
        raise DeliveryDisclosureError("交付披露文本过长")
    description_parts = [base_label]
    if normalized_kind:
        description_parts.append(f"media={normalized_kind}")
    if normalized_model:
        description_parts.append(f"model={normalized_model}")
    description = "; ".join(description_parts)
    if len(description) > maximum_characters:
        raise DeliveryDisclosureError("交付披露描述过长")
    return DeliveryDisclosure(
        visible_label_enabled=enabled,
        base_label=base_label,
        label=label,
        description=description,
        model_key=normalized_key,
        model=normalized_model,
        media_kind=normalized_kind,
    )
