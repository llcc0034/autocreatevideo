"""Read-only saved prompt / labelled candidate preview, never approval or dispatch."""
from copy import deepcopy

from django.core.exceptions import ValidationError
from django.db.models import F, Window
from django.db.models.functions import RowNumber

from studio.models import ExecutionPreparation
from studio.services.execution_instances import execution_configuration_max_bytes
from studio.services.execution_preparation_fields import (
    _field_contexts, _verify_value, locked_execution_field_selection_snapshot,
)
from studio.services.execution_preparation_owners import locked_preparation_owners, preparation_owner_ref
from studio.services.execution_preparations import (
    preparation_execution_definition, read_execution_preparation_detail, read_frozen_execution_request,
)
from studio.services.execution_profile_preparation import _compile_preparation_profile
from studio.services.json_contracts import JsonContract, json_contract
from studio.services.reference_context_facts import read_reference_context_facts
from studio.services.security_boundaries import security_boundary_resolution_cache
from studio.services.prompt_profiles import PromptProfileChannelLengthError
from studio.services.execution_prompts import profile_channel_lengths
from studio.services.chat_body import CHAT_DRIVERS


def _sources(detail):
    facts = detail.resolved_inputs.get("profile_sources")
    if not isinstance(facts, dict) or "reference_context" not in facts:
        return {"references": [], "dialogue": []}
    context = read_reference_context_facts(JsonContract(**facts["reference_context"]), max_bytes=execution_configuration_max_bytes())
    return {
        "references": [{"label": row["input_label"], "name": row.get("entity_label", ""),
            "category": row["category"], "guidance": row.get("prompt_hint", "")}
            for binding in context.reference_bindings
            for row in [binding, *([binding["audio_use"]] if "audio_use" in binding else [])]],
        "dialogue": [{"unit_key": unit.unit_key, "subject": row["subject"], "text": row["text"],
            "start_ms": row["start_ms"], "end_ms": row["end_ms"], "voiceover": row["voiceover"],
            "delivery": row["delivery"], **({"language": row["language"]} if "language" in row else {})}
            for unit in context.ordered_units for row in unit.dialogue],
    }


def _channels(value):
    if not isinstance(value, dict) or not value or any(not isinstance(k, str) or not isinstance(v, str) for k, v in value.items()):
        raise ValidationError("保存的提示词通道不完整，不能重新组装替代")
    return [{"key": key, "text": text, "character_count": len(text)} for key, text in value.items()]


def _frozen_content(configuration, transport):
    """Project the saved representation selected by the frozen prompt contract."""
    channels = transport.get("prompt_channels")
    values = channels.get("values") if isinstance(channels, dict) else None
    if configuration["mode"] != "none":
        return {"content_kind": "channels", "channels": _channels(values), "messages": []}
    if not isinstance(values, dict) or values or configuration.get("channels") != {}:
        raise ValidationError("无提示词模式的保存通道与冻结定义不一致")
    if transport["driver_type"] not in CHAT_DRIVERS:
        return {"content_kind": "not_applicable", "channels": [], "messages": []}
    saved = transport["payload"].get("messages")
    if (not isinstance(saved, list) or not saved
            or any(not isinstance(row, dict) or set(row) != {"role", "content"}
                or not isinstance(row["role"], str) or row["role"] not in {"system", "user", "assistant"}
                or not isinstance(row["content"], str) for row in saved)
            or not any(row["role"] == "user" and row["content"].strip() for row in saved)):
        raise ValidationError("保存的文本请求消息不完整，不能重新组装替代")
    # Field protocols require both their saved system rules and user batch.
    from studio.services.execution_field_contracts import FIELD_OPERATION_MODES
    if transport["operation"] in FIELD_OPERATION_MODES and (
            [row["role"] for row in saved] != ["system", "user"]
            or any(not row["content"].strip() for row in saved)):
        raise ValidationError("保存的字段协议消息不完整，不能重新组装替代")
    return {"content_kind": "messages", "channels": [], "messages": [
        {"role": row["role"], "content": row["content"], "character_count": len(row["content"])}
        for row in saved]}


def _basis(row, origin, status):
    return {"unit_key": row["unit_key"], "field_key": row["field_key"], "field_value_id": row["field_value_id"],
        "revision": row["field_revision"], "origin": origin, "status": status}


def _assembly_display(assembly, configuration, channels):
    if (not isinstance(assembly, dict) or assembly.get("prompt_profile_version_id") != configuration["profile_id"]
            or assembly.get("contract_sha256") != configuration["profile_sha256"]
            or type(assembly.get("version")) is not int or assembly["version"] < 1
            or any(assembly.get("channels", {}).get(key) != text for key, text in channels.items())):
        raise ValidationError("保存的组装来源与确切策略/通道不一致")
    return {"profile_basis": {"version_id": assembly["prompt_profile_version_id"], "version": assembly["version"],
                "contract_sha256": assembly["contract_sha256"]},
        "length_diagnostics": profile_channel_lengths(channels=channels,
            lengths=assembly.get("length_diagnostics"), configuration=configuration)}


@security_boundary_resolution_cache()
def read_preparation_prompt_display(*, preparation_id, expected_preparation_revision, actor):
    # The frozen branch never recompiles or reads current candidates as actual.
    observed = ExecutionPreparation.objects.get(pk=preparation_id)
    with locked_preparation_owners([preparation_owner_ref(observed)], actor):
        prep = ExecutionPreparation.objects.select_for_update().get(pk=preparation_id)
        if prep.revision != expected_preparation_revision:
            raise ValidationError("准备已更新，请刷新后查看完整提示词", code="execution_preparation_conflict")
        if prep.request_frozen_at is not None:
            detail, definition, transport = read_frozen_execution_request(prep)
            fields = detail.prepared_request["field_selections"]["fields"]
            configuration = definition.value["interface"]["prompt"]
            content = _frozen_content(configuration, transport.value)
            diagnostics = {"profile_basis": None, "length_diagnostics": []}
            if configuration["mode"] == "profile":
                compilation = transport.value["profile_compilation"]
                diagnostics = _assembly_display(compilation.get("assembly") if isinstance(compilation, dict) else None,
                    configuration, transport.value["prompt_channels"]["values"])
            return {"preparation_id": str(prep.pk), "preparation_revision": prep.revision, "mode": "frozen",
                "request_sha256": prep.request_sha256, "frozen_at": prep.request_frozen_at,
                "prompt_mode": configuration["mode"], **content,
                "field_basis": [_basis(row, "adopted", "confirmed") for row in fields],
                "issues": [], **diagnostics, "sources": _sources(detail)}
    # Reacquire the full existing source-root lock set, including reused fields.
    with locked_execution_field_selection_snapshot(preparation_id=preparation_id,
            expected_preparation_revision=expected_preparation_revision, actor=actor) as (prep, selected):
        detail = read_execution_preparation_detail(prep)
        result = {"preparation_id": str(prep.pk), "preparation_revision": prep.revision, "mode": "unavailable",
            "request_sha256": None, "frozen_at": None, "content_kind": "unavailable",
            "channels": [], "messages": [], "field_basis": [],
            "issues": [], "profile_basis": None, "length_diagnostics": [], "sources": _sources(detail)}
        definition = preparation_execution_definition(prep, detail=detail)
        result["prompt_mode"] = definition.value["interface"]["prompt"]["mode"]
        if detail.intent["request"].get("definition_sha256") != definition.sha256:
            raise ValidationError("准备引用的冻结定义与精确实例不一致")
        # Manual/none preparations deliberately persist an empty source object.
        # Its shape does not identify Profile mode; only the frozen definition does.
        if definition.value["configuration"]["prompt_mode"] in {"manual", "none"}:
            result["issues"] = ["当前为人工通道或无提示词模式，没有策略组装预览；可在“输入与运行参数”核对已保存输入。请求冻结后可查看保存的实际提示词，勿为预览而提交模型调用。"]
            return result
        if not isinstance(detail.resolved_inputs.get("profile_sources"), dict):
            raise ValidationError("策略准备缺少冻结来源，不能用人工通道替代")
        fields = deepcopy(selected.value["fields"])
        result["field_basis"] = [_basis(row, "adopted", "confirmed") for row in fields]
        contexts = _field_contexts(prep)
        # Bounded by frozen slots: only fetch one latest candidate per slot.
        latest = {(row.unit_key, row.field_key): row for row in prep.field_values.order_by().annotate(
            display_rank=Window(expression=RowNumber(), partition_by=[F("unit_key"), F("field_key")],
                order_by=[F("revision").desc(), F("id").desc()])).filter(display_rank=1)}
        missing = []
        for slot in selected.value["unselected_fields"]:
            value = latest.get((slot["unit_key"], slot["field_key"]))
            if value is None or value.status not in {"pending", "confirmed"}:
                missing.append(slot)
                if slot["required"]:
                    result["issues"].append(f'{slot["field_key"]} 尚无可预览的候选（缺失或已拒绝）；不会用旧版本填补。')
                continue
            context = contexts[(slot["unit_key"], slot["field_key"])]
            _verify_value(context, value)
            rule = context["item"]["field_contract"]
            row = {"unit_key": value.unit_key, "field_key": value.field_key,
                "field_value_id": str(value.pk), "field_revision": value.revision,
                "source_preparation_id": str(value.preparation_id), "text": value.text,
                "text_sha256": value.text_sha256, "source_sha256": value.source_sha256,
                "field_contract_sha256": value.field_contract_sha256,
                "generation_output_semantics": rule["generation_output_semantics"],
                "generation_source_selector": rule["generation_source_selector"]}
            fields.append(row)
            result["field_basis"].append(_basis(row, "candidate", value.status))
        if result["issues"]:
            return result
        display_fields = json_contract({**selected.value, "fields": fields, "unselected_fields": missing},
            max_bytes=execution_configuration_max_bytes())
        try:
            compiled = _compile_preparation_profile(prep, display_fields, evidence_key="preview_fields")
        except PromptProfileChannelLengthError as exc:
            # No retry with changed limits, no second renderer, no valid request
            # object. This is the exact rejected assembly for diagnosis only.
            result.update(mode="diagnostic", content_kind="channels", channels=_channels(exc.diagnostic["channels"]),
                issues=exc.messages, profile_basis=exc.diagnostic["profile_basis"],
                length_diagnostics=profile_channel_lengths(channels=exc.diagnostic["channels"],
                    lengths=exc.diagnostic["lengths"], configuration=definition.value["interface"]["prompt"]))
            return result
        result.update(mode="preview", content_kind="channels", channels=_channels(compiled.resolved_channels["values"]),
            **_assembly_display(compiled.compilation.assembly, definition.value["interface"]["prompt"],
                compiled.resolved_channels["values"]))
        return result
