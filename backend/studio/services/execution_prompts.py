"""Prompt-channel and auxiliary reference checks for an execution definition.

Channel names below are the existing reviewed Comfy semantic protocol, not a
mapping between business operations or model names. The selected strategy keeps
its own operation. These checks never choose a strategy or call a language model.
"""
from copy import deepcopy
from uuid import UUID

from django.core.exceptions import ValidationError

from studio.models import PromptProfileFieldRule
from studio.services.execution_parameters import compile_execution_parameter_contract, resolve_execution_parameters, workflow_parameter_rule
from studio.services.prompt_profiles import verify_prompt_profile_contract
from studio.services.source_projection_primitives import REFERENCE_CONTEXT_ENGINE_KEY
from studio.services.security_boundaries import security_boundary_resolution_cache


WORKFLOW_PROMPT_CHANNELS = {"positive_prompt": "prompt.positive", "negative_prompt": "prompt.negative"}
AUXILIARY_OPERATIONS = {"generate_fields": "prompt.fields.generate", "translate_fields": "prompt.fields.translate"}
_GENERATION_PURPOSES = {
    PromptProfileFieldRule.GenerationMode.MODEL_GENERATED: "generate_fields",
    PromptProfileFieldRule.GenerationMode.TRANSLATE_ONLY: "translate_fields",
}


def workflow_prompt_channels(workflow):
    """Project only actual, already verified prompt semantic bindings."""
    parameters = workflow.input_contract.get("parameters")
    if not isinstance(parameters, dict):
        raise ValidationError({"prompt_mode": "工作流缺少参数合同"})
    bindings = list(workflow.semantic_bindings.values("semantic_key", "value_type", "is_required"))
    channels = {}
    for channel, key in WORKFLOW_PROMPT_CHANNELS.items():
        matched = [row for row in bindings if row["semantic_key"] == key]
        definition = parameters.get(key)
        if not matched and definition is None:
            continue
        if not matched or not isinstance(definition, dict) or definition.get("type") != "string" or any(row["value_type"] != "string" for row in matched):
            raise ValidationError({f"prompt_channels.{channel}": "提示词通道缺少类型一致的参数或实际接线"})
        declared_required = definition.get("required")
        if declared_required is not None and type(declared_required) is not bool:
            raise ValidationError({f"prompt_channels.{channel}": "通道必填标志必须为布尔值"})
        required = any(row["is_required"] for row in matched) or declared_required is True
        rule = workflow_parameter_rule(key, definition, required=required)
        # Imported examples are not defaults for either manual or assembled
        # prompt channels. Explicit constants remain part of the hard contract.
        rule.pop("default", None)
        channels[channel] = {
            "semantic_key": key,
            "rule": rule,
        }
    return channels


def profile_auxiliary_purposes(profile):
    """All configured model-generated fields need the matching explicit leaf."""
    contract = verify_prompt_profile_contract(profile)
    return sorted({
        _GENERATION_PURPOSES[row["generation_mode"]]
        for row in contract["rules"] if row.get("generation_mode") in _GENERATION_PURPOSES
    })


def validate_workflow_prompt_configuration(version):
    """Validate the prompt part of a complete definition, not its readiness."""
    return validate_execution_prompt_channels(
        version, workflow_prompt_channels(version.workflow_version),
        source=f"workflow.{version.workflow_version_id}", target_label="工作流",
        allowed_templates=version.workflow_version.input_contract.get("prompt_template_keys", []),
    )


def validate_execution_prompt_channels(version, channels, *, source, target_label, allowed_templates=(), prompt_not_required=False):
    """Common exact-channel composition for reviewed Workflow and API adapters.

    No operation aliases or strategy selection are involved. A no-prompt
    declaration must be provided by an authoritative capability adapter; an
    empty graph/channel list alone cannot grant it.
    """
    if version.prompt_mode == "none":
        # A graph containing prompt inputs is not evidence of a no-prompt
        # capability, even if its inputs happen to have example defaults.
        if channels:
            raise ValidationError({"prompt_mode": f"{target_label}仍声明提示词通道，不能选择无需提示词"})
        if prompt_not_required is not True:
            raise ValidationError({"prompt_mode": "能力合同未明确声明无需提示词，不能根据空通道推断"})
        return {"mode": "none", "channels": {}, "auxiliary_purposes": []}
    if not channels:
        raise ValidationError({"prompt_mode": f"{target_label}没有声明可接收提示词的通道"})
    if version.prompt_mode == "manual":
        return {"mode": "manual", "channels": channels, "auxiliary_purposes": []}
    if version.prompt_mode != "profile" or version.prompt_profile_version_id is None:
        raise ValidationError({"prompt_mode": "必须明确选择提示词方式和精确策略"})
    profile = version.prompt_profile_version
    contract = verify_prompt_profile_contract(profile)
    used = {row["channel"] for row in contract["rules"]}
    unsupported = sorted(used - channels.keys())
    if unsupported:
        raise ValidationError({"prompt_profile_version": f"策略输出通道未接入{target_label}：" + ", ".join(unsupported)})
    missing = sorted(channel for channel, target in channels.items() if target["rule"]["required"] and channel not in used)
    if missing:
        raise ValidationError({"prompt_profile_version": f"策略未提供{target_label}必需通道：" + ", ".join(missing)})
    if not used:
        raise ValidationError({"prompt_profile_version": "策略没有配置输出字段"})
    composed = {}
    for channel in sorted(used):
        base = channels[channel]
        layers = [{"source": f"{source}.{base['semantic_key']}", "rule": base["rule"]}]
        limit = contract.get("channel_limits", {}).get(channel)
        if limit is not None:
            layers.append({"source": f"profile.{profile.pk}.{channel}", "rule": {"type": "string", "required": False, "max_length": limit["max_chars"]}})
        compiled = compile_execution_parameter_contract({channel: layers})[channel]
        composed[channel] = {"semantic_key": base["semantic_key"], **compiled}
    if allowed_templates:
        options = contract.get("source_projection_config", {}).get("primitive_options", {})
        rendering = options.get(REFERENCE_CONTEXT_ENGINE_KEY, {}).get("rendering", {})
        if rendering.get("prompt_template_key") not in allowed_templates:
            raise ValidationError({"prompt_profile_version": f"策略的模板标识不在{target_label}明确允许的列表中"})
    return {
        "mode": "profile", "profile_id": str(profile.pk), "profile_sha256": profile.contract_sha256,
        "prompt_operation": contract["operation"], "prompt_mode": contract["mode"],
        "channels": deepcopy(composed), "auxiliary_purposes": profile_auxiliary_purposes(profile),
    }


@security_boundary_resolution_cache()
def normalize_manual_execution_channels(configuration, supplied, *, declared_reference_labels):
    """Resolve manual/none channels with no AI, strategy lookup or rewriting.

    This consumes the checked target's channel contract and actual labels from
    the shared input allocator/label adapter. Imported prompt examples are not
    manual defaults. Explicit frozen constants still constrain the real input.
    Profile assembly uses its own verified field pipeline, never this shortcut.
    """
    if not isinstance(configuration, dict) or not isinstance(configuration.get("mode"), str) or configuration["mode"] not in {"manual", "none"} or not isinstance(configuration.get("channels"), dict):
        raise ValidationError({"prompt_mode": "此入口仅处理已核对合同的人工或无提示词方式，不能绕过策略字段组装"})
    if not isinstance(supplied, dict):
        raise ValidationError({"prompt_channels": "提示词通道必须是对象"})
    if not isinstance(declared_reference_labels, list) or any(not isinstance(label, str) or not label for label in declared_reference_labels) or len(declared_reference_labels) != len(set(declared_reference_labels)):
        raise ValidationError({"reference_labels": "必须提供本次素材的明确且唯一的引用标签"})
    if configuration["mode"] == "none":
        if supplied or configuration["channels"]:
            raise ValidationError({"prompt_channels": "无需提示词的能力不能接收额外提示词"})
        return {"values": {}, "sources": {}, "contracts": {}}
    return _normalize_execution_channel_values(configuration["channels"], supplied, declared_reference_labels)


def normalize_compiled_profile_channels(configuration, compilation, *, declared_reference_labels):
    """Only a compiler result for this exact Profile may supply its channels."""
    from studio.services.prompt_profile_compiler import MediaPromptProfileCompilation

    if not isinstance(configuration, dict) or not isinstance(configuration.get("channels"), dict) or not isinstance(compilation, MediaPromptProfileCompilation) or configuration.get("mode") != "profile":
        raise ValidationError({"prompt_mode": "策略方式必须使用正式编译结果，不能提交人工通道冒充"})
    assembly = compilation.assembly
    if not isinstance(assembly, dict) or not configuration.get("profile_id") or not configuration.get("profile_sha256") or assembly.get("prompt_profile_version_id") != configuration["profile_id"] or assembly.get("contract_sha256") != configuration["profile_sha256"]:
        raise ValidationError({"prompt_profile_version": "编译结果与冻结的精确策略不一致"})
    channels = assembly.get("channels")
    if not isinstance(channels, dict) or any(not isinstance(value, str) for value in channels.values()):
        raise ValidationError({"prompt_channels": "编译结果缺少完整文本通道"})
    unknown = [key for key, value in channels.items() if value and key not in configuration["channels"]]
    if unknown:
        raise ValidationError({"prompt_channels": "编译结果包含目标不支持的非空通道：" + ", ".join(sorted(unknown))})
    supplied = {key: value for key, value in channels.items() if key in configuration["channels"]}
    if not isinstance(declared_reference_labels, list) or any(not isinstance(label, str) or not label for label in declared_reference_labels):
        raise ValidationError({"reference_labels": "必须提供本次素材的明确引用标签"})
    # Logical markers are compiler-derived aliases of actual supplied ports,
    # never discovered by scanning generated text or accepted from callers.
    labels = list(declared_reference_labels)
    for binding in (compilation.execution_facts or {}).get("reference_subject_bindings", []):
        if not binding.get("source_labels") or not set(binding["source_labels"]).issubset(declared_reference_labels):
            raise ValidationError({"reference_labels": "逻辑主体引用的素材标签与本次实际输入不一致"})
        if binding["label"] not in labels:
            labels.append(binding["label"])
    lengths = profile_channel_lengths(channels=supplied, lengths=assembly.get("length_diagnostics"), configuration=configuration)
    exceeded = [row for row in lengths if row["over_by"]]
    if exceeded:
        from studio.services.prompt_profiles import PromptProfileChannelLengthError
        raise PromptProfileChannelLengthError(
            [f"通道 {row['channel']} 装配后为 {row['character_count']} 字符，超过生效上限 {row['max_chars']}" for row in exceeded],
            channels=supplied, lengths=assembly["length_diagnostics"],
            profile_basis={"version_id": assembly["prompt_profile_version_id"], "version": assembly["version"],
                "contract_sha256": assembly["contract_sha256"]})
    return _normalize_execution_channel_values(configuration["channels"], supplied, labels)


def profile_channel_lengths(*, channels, lengths, configuration):
    """Project saved compiler counts and exact frozen limits, never render text."""
    if not isinstance(lengths, list) or any(not isinstance(row, dict) for row in lengths):
        raise ValidationError("策略组装缺少保存的长度明细，不重新组装补齐")
    indexed = {row.get("channel"): row for row in lengths}
    if len(indexed) != len(lengths):
        raise ValidationError("策略组装长度明细重复")
    result = []
    for key, text in channels.items():
        row = deepcopy(indexed.get(key))
        if (not isinstance(row, dict) or type(row.get("character_count")) is not int or row["character_count"] != len(text)
                or not isinstance(row.get("fragments"), list)):
            raise ValidationError("策略组装长度明细与原文不一致")
        total = 0
        for fragment in row["fragments"]:
            if (not isinstance(fragment, dict)
                    or any(type(fragment.get(name)) is not int or fragment[name] < 0 for name in ("text_chars", "separator_chars"))
                    or type(fragment.get("max_chars")) is not int or fragment["max_chars"] < 1
                    or fragment["text_chars"] > fragment["max_chars"]
                    or any(not isinstance(fragment.get(name), str) or not fragment[name]
                        for name in ("field_key", "field_definition_version_id", "source_selector"))):
                raise ValidationError("策略组装字段长度或版本来源不完整")
            try:
                UUID(fragment["field_definition_version_id"])
            except (ValueError, TypeError, AttributeError) as exc:
                raise ValidationError("策略组装字段版本无效") from exc
            total += fragment["text_chars"] + fragment["separator_chars"]
        if total != len(text):
            raise ValidationError("策略组装分项与通道总长不一致")
        target = configuration["channels"].get(key)
        if target is not None:
            row["max_chars"] = target["rule"].get("max_length")
            row["limit_sources"] = [{"source": item["source"], "max_chars": item["rule"].get("max_length")}
                for item in target["layers"]]
        else:
            row["limit_sources"] = []
        maximum = row["max_chars"]
        if maximum is not None and (type(maximum) is not int or maximum < 1):
            raise ValidationError("冻结通道上限无效")
        row["over_by"] = max(0, len(text) - maximum) if maximum is not None else 0
        result.append(row)
    return result


def _normalize_execution_channel_values(channels, supplied, declared_reference_labels):
    from studio.services.prompt_normalization_contract import (
        PromptNormalizationContractError, validate_director_text_characters,
    )

    if not isinstance(declared_reference_labels, list) or any(not isinstance(label, str) or not label for label in declared_reference_labels) or len(declared_reference_labels) != len(set(declared_reference_labels)):
        raise ValidationError({"reference_labels": "必须提供本次素材的明确且唯一的引用标签"})
    if not channels:
        raise ValidationError({"prompt_channels": "必须有实际提示词通道"})
    layers = {}
    for channel, target in channels.items():
        if not isinstance(target, dict) or not isinstance(target.get("rule"), dict) or target["rule"].get("type") != "string":
            raise ValidationError({"prompt_channels": "每个提示词通道必须有已核对的字符串合同"})
        rule = deepcopy(target["rule"])
        rule.pop("default", None)
        layers[channel] = [{"source": f"prompt_channel.{channel}", "rule": rule}]
    resolved = resolve_execution_parameters(layers, supplied)
    for channel, text in resolved["values"].items():
        try:
            validate_director_text_characters(text)
        except PromptNormalizationContractError as exc:
            raise ValidationError({f"prompt_channels.{channel}": str(exc)}) from exc
    return resolved
