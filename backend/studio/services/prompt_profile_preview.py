"""Read-only PromptProfile previews for persisted and unsaved candidates."""

from __future__ import annotations

from copy import copy, deepcopy
from typing import Any

from studio.models import PromptProfileFieldRule, PromptProfileVersion
from studio.services.prompt_profiles import (
    PromptProfileError,
    assemble_prompt_profile,
    validate_profile_rule,
)


def preview_prompt_profile(
    base_profile: PromptProfileVersion,
    *,
    sources: dict[str, Any],
    candidate: dict[str, Any] | None = None,
) -> dict[str, Any]:
    """Assemble a sample prompt without persisting profile or rule changes.

    Candidate rules are a complete replacement snapshot.  The caller has
    already validated their scalar shape and resolved field definitions.
    """

    if candidate is None:
        preview = assemble_prompt_profile(
            base_profile,
            sources=sources,
            allow_draft=True,
        )
    else:
        if base_profile.status != PromptProfileVersion.Status.DRAFT:
            raise PromptProfileError(
                "只有草稿提示词策略可以预览未保存配置；请先创建后继草稿",
                code="prompt_profile_preview_candidate_not_draft",
            )
        profile = copy(base_profile)
        for field_name in (
            "model_family",
            "operation",
            "mode",
            "source_projection_key",
            "source_projection_config",
            "channel_limits",
        ):
            setattr(profile, field_name, deepcopy(candidate[field_name]))
        profile.status = PromptProfileVersion.Status.DRAFT
        profile.input_structure_version = candidate.get("input_structure_version")

        persisted_rule_ids = {
            str(value)
            for value in base_profile.field_rules.values_list("id", flat=True)
        }
        seen_rule_ids: set[str] = set()
        seen_field_ids: set[str] = set()
        seen_positions: set[int] = set()
        candidate_rules: list[PromptProfileFieldRule] = []
        for index, raw_rule in enumerate(candidate["field_rules"], start=1):
            values = dict(raw_rule)
            rule_id = values.pop("id", None)
            if rule_id is not None:
                normalized_rule_id = str(rule_id)
                if normalized_rule_id not in persisted_rule_ids:
                    raise PromptProfileError(
                        f"第 {index} 条预览规则不属于当前提示词策略",
                        code="prompt_profile_preview_rule_unknown",
                    )
                if normalized_rule_id in seen_rule_ids:
                    raise PromptProfileError(
                        f"第 {index} 条预览规则 ID 重复",
                        code="prompt_profile_preview_rule_duplicate",
                    )
                seen_rule_ids.add(normalized_rule_id)
            field_definition = values["field_definition"]
            field_id = str(field_definition.pk)
            if field_id in seen_field_ids:
                raise PromptProfileError(
                    f"预览规则重复配置字段 {field_definition.key}",
                    code="prompt_profile_preview_field_duplicate",
                )
            seen_field_ids.add(field_id)
            position = int(values["position"])
            if position in seen_positions:
                raise PromptProfileError(
                    f"预览规则重复使用位置 {position}",
                    code="prompt_profile_preview_position_duplicate",
                )
            seen_positions.add(position)
            rule = PromptProfileFieldRule(
                id=rule_id,
                profile_version=profile,
                **values,
            )
            validate_profile_rule(rule)
            candidate_rules.append(rule)

        preview = assemble_prompt_profile(
            profile,
            sources=sources,
            allow_draft=True,
            rules=candidate_rules,
        )

    preview["ephemeral"] = True
    preview["preview_scope"] = "sample_final_assembly"
    preview["base_contract_sha256"] = base_profile.contract_sha256
    preview["executed_stages"] = {
        "field_generation": False,
        "source_projection": bool(preview.get("source_projection")),
        "final_assembly": True,
    }
    return preview
