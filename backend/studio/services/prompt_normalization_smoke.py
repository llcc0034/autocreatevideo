from __future__ import annotations

import hashlib
import json
from types import SimpleNamespace
from typing import Any

from django.core.exceptions import ValidationError

from studio.models import ModelDeployment, PromptProfileBinding
from studio.services.json_contracts import json_contract, verify_json_contract
from studio.services.prompt_normalization_contract import (
    FIELD_GENERATION_TARGET_CONTRACT_KIND,
    FIELD_GENERATION_TARGET_CONTRACT_SCHEMA_VERSION,
    MODEL_PROCESSING_MODES,
    PROMPT_NORMALIZE_OPERATION,
    PROMPT_PROFILE_CONTRACT_SCHEMA_VERSION,
    TRANSPORT_CAPACITY_PROBE_KEY,
    TRANSPORT_CAPACITY_PROBE_KIND,
    prompt_normalization_chat_messages_input_bytes,
    prompt_normalization_chat_payload,
)
from studio.services.prompt_profiles import (
    assemble_prompt_profile,
    frozen_profile_reference,
    prompt_profile_contract_payload,
    text_profile_sources,
)
from studio.services.security_boundaries import boundary_int, security_boundary_resolution_cache


def _prompt_profile_contract_max_bytes() -> int:
    return boundary_int("security.prompt_profile.contract_max_bytes")


def _prompt_normalization_smoke_snapshot_max_bytes() -> int:
    return boundary_int("security.prompt_normalization.smoke_snapshot_max_bytes")


def _representative_profile_contract():
    bindings = list(
        PromptProfileBinding.objects.select_related("prompt_profile_version")
        .filter(
            is_active=True,
            operation__in=("image.generate", "video.generate"),
            prompt_profile_version__status="published",
            prompt_profile_version__contract_schema_version=(
                PROMPT_PROFILE_CONTRACT_SCHEMA_VERSION
            ),
        )
        .order_by("key")
    )
    representatives = []
    for binding in bindings:
        profile = binding.prompt_profile_version
        contract = prompt_profile_contract_payload(profile)
        generated_rules = [
            rule for rule in contract.get("rules") or []
            if isinstance(rule, dict)
            and rule.get("generation_mode") in MODEL_PROCESSING_MODES
        ]
        if generated_rules:
            representatives.append((
                (
                    len(generated_rules),
                    sum(int(rule.get("generation_max") or 0) for rule in generated_rules),
                    int(binding.priority),
                    int(profile.version),
                    str(binding.key),
                ),
                binding,
                contract,
                generated_rules,
            ))
    if not representatives:
        raise ValidationError(
            "prompt.normalize smoke 缺少已发布的 model_generated PromptProfile 合同",
        )
    _score, binding, contract, rules = max(representatives, key=lambda item: item[0])
    verify_json_contract(
        contract,
        schema_version=binding.prompt_profile_version.contract_schema_version,
        size_bytes=binding.prompt_profile_version.contract_size_bytes,
        sha256=binding.prompt_profile_version.contract_sha256,
        max_bytes=_prompt_profile_contract_max_bytes(),
    )
    return binding, contract, rules


@security_boundary_resolution_cache()
def representative_prompt_normalization_smoke_payload(
    deployment: ModelDeployment,
    *,
    policy: dict[str, Any],
    text_binding,
) -> tuple[dict[str, Any], dict[str, Any]]:
    """Build one near-capacity generated-field probe from published contracts."""

    binding, profile_contract, generated_rules = _representative_profile_contract()
    generated_rules = generated_rules[: int(policy["max_items_per_batch"])]
    profile = binding.prompt_profile_version
    selection = {
        "schema_version": 1,
        "binding_key": binding.key,
        "prompt_profile_version_id": str(profile.id),
        "operation": binding.operation,
        "model_family": binding.model_family,
        "mode": binding.mode,
        "contract_sha256": profile.contract_sha256,
    }
    selection_sha256 = json_contract(
        selection,
        max_bytes=_prompt_normalization_smoke_snapshot_max_bytes(),
    ).sha256
    items: list[dict[str, Any]] = []
    item_rules: list[dict[str, Any]] = []
    context_fields: list[dict[str, Any]] = []
    for index, rule in enumerate(generated_rules, start=1):
        request_id = f"prompt-normalize-smoke-generated-{index:02d}"
        selector = str(rule["generation_source_selector"])
        protected_markers = (
            ["<Subject 1>", "<Picture 1>", "<Picture 2>", "<Picture 3>", "<Video 1>"]
            if index == 1 else []
        )
        source_text = (
            f"冻结镜头字段 {selector} 的已确认事实：单镜头总时长 4.000 秒。"
            "<Subject 1> 是年轻女侦探，身份、脸部、短黑发与深色风衣由 <Picture 1> 锁定；"
            "她全程位于画面左侧三分之一处，右手握着由 <Picture 3> 锁定的旧怀表。"
            "环境是由 <Picture 2> 锁定的废弃站台：雨夜、青绿色顶灯、湿润地面反光，"
            "远端轨道消失在黑暗中，空间结构和道具数量不得改变。"
            "[Shot 1] 00:00.000 时以中景开始；00:00.000 至 00:01.000 她保持戒备并低头看怀表；"
            "00:01.000 至 00:03.200 她把怀表抬到眼前，暖琥珀色表光照亮脸部，远处只出现一个"
            "模糊人形剪影；00:03.200 至 00:04.000 她保持姿势，镜头停止推进，不切镜。"
            "镜头运动只参考 <Video 1> 的平稳缓慢推进与自然运动节奏，不复制其中人物、场景或事件。"
            "对白仅有一句：<Subject 1>（低声、克制，00:01.200 至 00:02.600）说"
            "‘最后一班车没有乘客。’；原文、说话人和时点必须保持。"
            "画内声只有雨打顶棚、远处铁轨振动和怀表机械滴答；非画内音乐只有低频悬疑脉冲，"
            "在 00:04.000 前淡出。不得新增人物、动作、对白、文字、道具、切镜或声音层。"
        )
        items.append({
            "request_id": request_id,
            "text": source_text,
            "source_lang": "Chinese",
            "target_lang": "English",
            "field_key": str(rule["field_key"]),
            "label": f"capacity smoke: {rule['field_key']}",
            "context": json.dumps(
                {
                    "schema_version": 1,
                    "selector": selector,
                    "profile_key": profile_contract["profile_key"],
                },
                ensure_ascii=False,
                sort_keys=True,
                separators=(",", ":"),
            ),
            "protected_tokens": protected_markers,
        })
        item_rules.append({
            "request_id": request_id,
            "shot_id": f"smoke-shot-{index:02d}",
            "scope_type": "shot",
            "scope_id": f"smoke-shot-{index:02d}",
            "profile_selection_sha256": selection_sha256,
            "profile_field_key": str(rule["field_key"]),
            "profile_source_selector": str(rule["source_selector"]),
            "profile_rule_position": int(rule["position"]),
            "generation_mode": str(rule["generation_mode"]),
            "generation_source_selector": selector,
            "generation_instruction": str(rule["generation_instruction"]),
            "generation_output_semantics": str(rule["generation_output_semantics"]),
            "generation_required": bool(rule["generation_required"]),
            "language_policy": str(rule["language_policy"]),
            "length_unit": str(rule["length_unit"]),
            "generation_min": rule["generation_min"],
            "generation_max": rule["generation_max"],
            "generation_validator_keys": list(rule["generation_validator_keys"]),
            "generation_context_selectors": list(
                rule["generation_context_selectors"],
            ),
            "source_sha256": hashlib.sha256(source_text.encode("utf-8")).hexdigest(),
        })
        context_fields.append({
            "scope_type": "shot",
            "scope_id": f"smoke-shot-{index:02d}",
            "field_key": selector,
            "value": source_text,
            "source_sha256": hashlib.sha256(source_text.encode("utf-8")).hexdigest(),
        })

    profile_snapshot = {
        "prompt_profile_version_id": str(profile.id),
        "binding_key": binding.key,
        "frozen_selection_sha256": selection_sha256,
        "contract_schema_version": int(profile.contract_schema_version),
        "contract_size_bytes": int(profile.contract_size_bytes),
        "contract_sha256": profile.contract_sha256,
        "contract": profile_contract,
    }
    frozen_context: dict[str, Any] = {
        "schema_version": 2,
        "project": {
            "project_id": "prompt-normalization-smoke-project",
            "title": "Prompt normalization representative smoke",
            "logline": "A continuity-sensitive rain-night platform scene.",
            "language": "zh-CN",
            "aspect_ratio": "9:16",
            "continuity_summary": "One continuous four-second shot.",
            "style": {
                "name": "Rain-night detective noir",
                "description": "Cold teal platform with restrained amber accents.",
                "visual_prompt": "Cinematic rain-night detective noir.",
                "negative_prompt": "No identity, wardrobe, or architecture drift.",
            },
        },
        "fields": context_fields,
        "shots": [{
            "shot_id": f"smoke-shot-{index:02d}",
            "episode_id": "smoke-episode-01",
            "episode_number": 1,
            "shot_number": index,
            "title": f"Representative shot {index}",
            "narrative": "The detective raises the watch on the rainy platform.",
            "detailed_description": str(items[index - 1]["text"]),
            "camera": "One restrained slow push-in.",
            "action": "The detective raises the watch once.",
            "sound_effects": "Rain, rail vibration, and watch ticks.",
            "music": "Low suspense pulse fading before the cut.",
            "duration_ms": 4000,
            "media_type": "video",
            "mode": "Ref2VA",
            "scene_id": "smoke-scene-01",
            "character_ids": ["smoke-character-01"],
            "prop_ids": ["smoke-prop-01"],
            "dialogue": [{
                "speaker_id": "S1",
                "text": "最后一班车没有乘客。",
                "delivery": "低声、克制",
                "start_ms": 1200,
                "end_ms": 2600,
                "voiceover": False,
            }],
            "linked_bible": [{
                "entity_type": "character",
                "entity_id": "smoke-character-01",
                "name": "Young female detective",
                "visual_identity": "Short black hair and dark trench coat.",
                "stable_speaker_id": "S1",
            }],
        } for index in range(1, len(items) + 1)],
        "continuity_facts": (
            "同一已确认镜头只包含一个连续动作弧、一个说话人和一句对白；上述时间、位置、"
            "引用身份、画内声与非画内音乐边界是全部叙事事实。"
        ),
    }
    synthetic_project = SimpleNamespace(
        title="Prompt normalization representative smoke",
        logline="A continuity-sensitive rain-night platform scene.",
        master_script="",
        language="zh-CN",
        aspect_ratio="9:16",
    )
    probe_padding = ""

    target_contract = {
        "schema_version": FIELD_GENERATION_TARGET_CONTRACT_SCHEMA_VERSION,
        "kind": FIELD_GENERATION_TARGET_CONTRACT_KIND,
        "workflow_selection_sha256": json_contract({
            "schema_version": 1,
            "representative_binding_key": binding.key,
        }, max_bytes=_prompt_normalization_smoke_snapshot_max_bytes()).sha256,
        "profiles": [profile_snapshot],
        "frozen_context": frozen_context,
        "frozen_context_sha256": json_contract(
            frozen_context,
            max_bytes=_prompt_normalization_smoke_snapshot_max_bytes(),
        ).sha256,
        "item_generation_rules": item_rules,
    }
    prompt_assembly = assemble_prompt_profile(
        text_binding.profile_version,
        sources=text_profile_sources(
            project=synthetic_project,
            inputs={"schema_version": 1, "items": items},
            operation=PROMPT_NORMALIZE_OPERATION,
        ),
    )

    def assemble() -> tuple[dict[str, Any], int]:
        payload = {
            "schema_version": 1,
            "items": items,
            "target_prompt_contract": target_contract,
            TRANSPORT_CAPACITY_PROBE_KEY: {
                "schema_version": 1,
                "kind": TRANSPORT_CAPACITY_PROBE_KIND,
                "padding": probe_padding,
                "padding_bytes": len(probe_padding.encode("ascii")),
                "padding_sha256": hashlib.sha256(
                    probe_padding.encode("ascii"),
                ).hexdigest(),
            },
        }
        payload["prompt_profile_assembly"] = prompt_assembly
        provider_payload = prompt_normalization_chat_payload(payload, policy=policy)
        input_bytes = prompt_normalization_chat_messages_input_bytes(
            provider_payload["messages"],
        )
        return payload, input_bytes

    payload, input_bytes = assemble()
    base_input_bytes = input_bytes
    target_floor = max(1024, int(policy["max_input_bytes"]) - 1024)
    probe_padding_bytes = 0
    if input_bytes < target_floor:
        # ASCII zeroes add exactly one serialized byte each. The decimal
        # padding_bytes field can add a few more bytes, within the existing
        # 1024-byte safety margin. Revalidate the actual provider messages;
        # never reduce the capacity acceptance target to speed up preparation.
        probe_padding_bytes = target_floor - input_bytes
        probe_padding = "0" * probe_padding_bytes
        payload, input_bytes = assemble()
    if input_bytes < target_floor:
        raise ValidationError(
            "prompt.normalize representative smoke 未达到冻结 max_input_bytes 的边界覆盖要求",
        )
    target_contract = payload["target_prompt_contract"]
    evidence = {
        "schema_version": 1,
        "base_input_bytes": base_input_bytes,
        "input_bytes": input_bytes,
        "max_input_bytes": int(policy["max_input_bytes"]),
        "target_floor_bytes": target_floor,
        "item_count": len(items),
        "max_items_per_batch": int(policy["max_items_per_batch"]),
        "target_contract_sha256": json_contract(
            target_contract,
            max_bytes=_prompt_normalization_smoke_snapshot_max_bytes(),
        ).sha256,
        "prompt_profile_binding_id": str(binding.id),
        "prompt_profile_binding_key": binding.key,
        "prompt_profile_version_id": str(profile.id),
        "assembly_prompt_profile": frozen_profile_reference(
            text_binding,
            model_profile_version=deployment.profile_version,
            operation=PROMPT_NORMALIZE_OPERATION,
        ),
        "field_keys": [str(rule["field_key"]) for rule in generated_rules],
        "generation_modes": [str(rule["generation_mode"]) for rule in generated_rules],
        "narrative_shot_count": 1,
        "transport_probe_padding_bytes": probe_padding_bytes,
        "transport_probe_padding_sha256": payload[
            TRANSPORT_CAPACITY_PROBE_KEY
        ]["padding_sha256"],
        "capacity_utilization_ratio": round(
            input_bytes / int(policy["max_input_bytes"]),
            6,
        ),
    }
    return payload, evidence
