"""Read original returned text and explicitly correct rejected output; no call or adoption."""
import hashlib
import json
import re

from django.core.exceptions import ValidationError
from django.utils import timezone

from studio.models import ExecutionTextResponseReview, GenerationAttempt, ProductionRun
from studio.services.execution_preparation_fields import _locked_preparations
from studio.services.execution_preparations import read_production_attempt_request
from studio.services.execution_text_contracts import parse_execution_text_response
from studio.services.json_contracts import verify_json_contract
from studio.services.execution_instances import execution_configuration_max_bytes


COLLECT_RECEIPT_KIND = "collect_receipt_json"


def read_collect_receipt(text, *, request_id):
    """Decode the retained adapter receipt; never fill in absent identity/output."""
    try:
        receipt = json.loads(text)
    except (ValueError, RecursionError) as exc:
        raise ValidationError("保留的收集回执不是有效 JSON") from exc
    if not isinstance(receipt, dict) or receipt.get("request_id") != request_id:
        raise ValidationError("收集回执与本次确切请求不一致")
    return receipt


FIELD_RESPONSE_HELP = {
    "empty": "返回的必填字段为空；请查看实际返回并补填对应字段。",
    "control_characters_invalid": "返回包含不可用于请求的控制字符；请查看原文，另存清理后的字段修订。",
    "json_invalid": "字段需要结构化内容，但实际返回无法解析；请核对原文并人工补正。",
    "json_type_invalid": "返回字段的结构类型不符合输入接口；请核对原文并人工补正。",
    "language_policy_invalid": "返回语言与本次冻结的输入语言要求不一致；原文保留，请核对并修订。",
    "length_out_of_range": "返回超出本次明确的字段长度约束；原文保留，请核对并修订。",
    "protected_token_mismatch": "原文中的受保护引用未完整保留；请核对引用对应关系后修订。",
    "single_line_invalid": "该输入接口要求单行，实际返回包含换行；原文保留，请另存修订。",
    "shot_header_forbidden": "镜头标题由策略模板统一装配；本次生成重复输出了镜头标题，请重新生成或修订贡献正文。",
    "unresolved_reference_or_placeholder": "返回含未解析占位符或未声明素材引用；请核对归属后修订。",
}


def field_warning_messages(warnings):
    """Safe UI wording from validator facts, not from model-authored messages."""
    result = []
    for warning in warnings:
        code = warning.get("code")
        if code == "recommended_length_out_of_range":
            message = "正文长度偏离建议范围，仅作提醒；可按实际可用性确认，不要求重新生成。"
        elif code == "generated_field_label":
            message = "正文自带字段标题，最终组装可能重复；建议人工检查或修改，不阻止确认。"
        else:
            message = "此字段存在生成建议，请核对实际正文；建议不代表内容不可用。"
        result.append({"code": str(code), "message": message})
    return result


def response_validation_diagnostic(error, response):
    """Only trusted validator explanations, never provider text in diagnostics."""
    from studio.services.production_visual_rules import VisualRulesError
    code = getattr(error, "code", None) or "response_contract_invalid"
    summary = "返回内容未通过结果格式校验，原始正文已保留；请核对并人工修正，不会自动重试。"
    if isinstance(error, VisualRulesError):
        summary = "；".join(error.messages)
    elif getattr(error.__cause__ or error, "code", None) == "json_text_invalid":
        code, summary = "response_json_invalid", "返回内容不是完整且无重复字段的 JSON；请检查格式、重复字段及额外说明。"
    elif error.messages == ["原生业务结果必须是对象"]:
        code, summary = "response_object_required", "返回结果必须是一个 JSON 对象，不能是列表或其他类型。"
    elif error.messages == ["字段响应必须是唯一合法 JSON，不接受重复字段或附加说明"]:
        code, summary = "response_json_invalid", "实际返回不是可直接解析的唯一 JSON；原文完整保留，请查看后人工补填字段。"
    else:
        from studio.services.prompt_normalization_contract import safe_prompt_field_validation_reason_code
        cause = error.__cause__ or error
        reason = safe_prompt_field_validation_reason_code(getattr(cause, "reason_code", None))
        if reason in FIELD_RESPONSE_HELP:
            code, summary = reason, FIELD_RESPONSE_HELP[reason]
        elif hasattr(error, "message_dict") and any(key.startswith("response.items") for key in error.message_dict):
            code, summary = "response_field_mapping_invalid", "实际返回的字段数量、标识或类型与请求不一致；不能猜测归属，原文保留供人工补填。"
    if not isinstance(code, str) or not re.fullmatch(r"[a-z][a-z0-9_]{0,99}", code):
        code = "response_contract_invalid"
    from studio.services.provider_registry import safe_chat_usage
    safe_usage = safe_chat_usage(response.get("usage"))
    from studio.services.execution_structured_text import StructuredTextValidationError
    from studio.services.script_semantics import ScriptSemanticValidationError
    structure_errors = {}
    if isinstance(error, ScriptSemanticValidationError):
        code = "script_semantic_result_invalid"
        summary = "剧本返回未通过所选结构、映射或引用关系校验；原文保留，请按字段路径修正，不会自动重试。"
        structure_errors = {"structure_errors": error.structure_errors,
            "relation_errors": error.relation_errors, "validation_scope": error.validation_scope}
    elif isinstance(error, StructuredTextValidationError):
        code = "structured_text_result_invalid"
        structure_errors = {"structure_errors": error.structure_errors}
    return {"schema_version": 1, "contract_error_code": "response_contract_invalid",
        "validation_code": code, "summary": summary, **structure_errors, **({"usage": safe_usage} if safe_usage else {})}


def verified_diagnostics(row):
    return verify_json_contract(row.diagnostics, schema_version=row.diagnostics_schema_version,
        size_bytes=row.diagnostics_size_bytes, sha256=row.diagnostics_sha256,
        max_bytes=execution_configuration_max_bytes()).value


def read_retained_text_response(row):
    """Caller owns visibility; retain the exact attempt/request/body relationship."""
    if row.asset_job_id is not None:
        from studio.services.asset_generation import _snapshot
        snapshot = _snapshot(row.asset_job)
        diagnostics = verified_diagnostics(row)
        if (diagnostics.get("preparation_id") != str(snapshot.preparation.pk)
                or diagnostics.get("request_sha256") != snapshot.preparation.request_sha256):
            raise ValidationError("素材原始返回与原冻结准备不一致，不能从当前请求重建")
        if row.asset_job.execution_kind == "comfy_workflow":
            if (diagnostics.get("response_source") != "comfyui_collect"
                    or diagnostics.get("provider_request_id") != str(snapshot.preparation.pk)
                    or row.asset_job.provider_request_id != str(snapshot.preparation.pk)
                    or row.asset_job.provider_request_id_sha256 != hashlib.sha256(str(snapshot.preparation.pk).encode()).hexdigest()
                    or diagnostics.get("output_node_id") != str(snapshot.output_contract["primary"]["node_id"])):
                raise ValidationError("工作流原始返回与原请求或冻结输出节点不一致")
        original = row.original_text
        if snapshot.output_kind in {"image", "video", "audio"} and diagnostics.get("body_kind") != COLLECT_RECEIPT_KIND:
            raise ValidationError("原媒体回执缺少确切正文类型，不能按普通文本解释")
        if diagnostics.get("body_kind") == COLLECT_RECEIPT_KIND:
            if snapshot.output_kind not in {"image", "video", "audio"}:
                raise ValidationError("收集回执必须属于原媒体输出")
            if row.asset_job.execution_kind == "deployment":
                if (snapshot.transport.value["driver_type"] != "async_rest"
                        or diagnostics.get("response_source") != "async_rest_collect"
                        or not row.asset_job.provider_request_id
                        or diagnostics.get("provider_request_id") != row.asset_job.provider_request_id
                        or row.asset_job.provider_request_id_sha256 != hashlib.sha256(row.asset_job.provider_request_id.encode()).hexdigest()):
                    raise ValidationError("REST 原始返回与原请求不一致")
            read_collect_receipt(original, request_id=row.asset_job.provider_request_id)
        return original, diagnostics, snapshot.definition, snapshot.transport
    attempt = row.attempt
    _, definition, transport = read_production_attempt_request(attempt)
    diagnostics = verified_diagnostics(row)
    original = row.original_text
    compiled = transport.value.get("payload", {}).get("compiled_workflow", {})
    primary = compiled.get("output_contract", {}).get("primary", {})
    driver_type = transport.value["driver_type"]
    output_kind = transport.value.get("output", {}).get("kind") if driver_type == "async_rest" else primary.get("kind")
    if output_kind in {"image", "video", "audio"} and diagnostics.get("body_kind") != COLLECT_RECEIPT_KIND:
        raise ValidationError("生产媒体回执缺少确切正文类型，不能按普通文本解释")
    if diagnostics.get("body_kind") == COLLECT_RECEIPT_KIND:
        if (attempt.role != "primary" or output_kind not in {"image", "video", "audio"}
                or not attempt.provider_request_id or attempt.submitted_at is None or attempt.submission_started_at is None
                or diagnostics.get("provider_request_id") != attempt.provider_request_id
                or diagnostics.get("preparation_id") != str(attempt.preparation_id)
                or diagnostics.get("request_sha256") != attempt.request_sha256):
            raise ValidationError("生产媒体回执与原调用或冻结请求不一致")
        if driver_type == "async_rest":
            if diagnostics.get("response_source") != "async_rest_collect" or attempt.generation_task_id is not None:
                raise ValidationError("生产 REST 回执与原执行类型不一致")
            read_collect_receipt(original, request_id=attempt.provider_request_id)
            return original, diagnostics, definition, transport
        task = attempt.generation_task
        token = str(attempt.pk)
        if (attempt.role != "primary" or transport.value["driver_type"] != "comfyui"
                or primary.get("kind") not in {"image", "video", "audio"} or task is None
                or task.submission_token != token or task.comfy_prompt_id != token
                or attempt.provider_request_id != token or attempt.submitted_at is None
                or attempt.submission_started_at is None
                or diagnostics.get("response_source") != "comfyui_history"
                or diagnostics.get("provider_request_id") != token
                or diagnostics.get("preparation_id") != str(attempt.preparation_id)
                or diagnostics.get("request_sha256") != attempt.request_sha256
                or diagnostics.get("output_node_id") != str(primary.get("node_id"))):
            raise ValidationError("生产媒体回执与原调用、任务或冻结输出合同不一致")
        history = read_collect_receipt(original, request_id=token).get("history")
        status = history.get("status") if isinstance(history, dict) else None
        if not isinstance(status, dict) or not (status.get("status_str") == "error" or (
                status.get("status_str") == "success" and status.get("completed") is True)):
            raise ValidationError("保留的生产媒体 history 不是原终态回执")
    return original, diagnostics, definition, transport


def retain_asset_text_response(*, job, preparation_id, request_sha256, text, diagnostics, collection_request_id=None,
                               collection_body_kind="text"):
    """Reuse the one response store under the original asset claim/owner locks.

    Retaining bytes is not a candidate, a human correction or permission to
    retry. Lost claims and ignored/cancelled results cannot create a response.
    """
    from django.core.files.storage import default_storage
    from studio.models import AssetGenerationJobResolution, ExecutionPreparation, ExecutionTextResponseReview
    from studio.services.execution_preparation_owners import locked_preparation_owner_rows
    from studio.services.execution_response_storage import store_response
    from studio.services.json_contracts import assign_json_contract

    with locked_preparation_owner_rows([("asset_job_id", job.pk)]) as owners:
        current = owners[("asset_job_id", str(job.pk))]
        collecting = collection_request_id is not None
        eligible = (current.status in {"submitted", "running"} and current.submitted_at is not None
            and collection_request_id == current.provider_request_id
            and current.provider_request_id_sha256 == hashlib.sha256(collection_request_id.encode()).hexdigest()) if collecting else (
                current.execution_kind == "deployment" and current.status == "submitting")
        if (job.claim_token is None or current.claim_token != job.claim_token or not eligible
                or current.submission_started_at is None or current.cancel_requested_at is not None
                or current.ignore_late_result):
            return False
        resolution = current.resolutions.filter(
            outcome__in=AssetGenerationJobResolution.OPERATOR_OUTCOMES).order_by("-revision").first()
        if resolution is not None and not (
                collecting and collection_body_kind == COLLECT_RECEIPT_KIND
                and resolution.outcome == AssetGenerationJobResolution.Outcome.CONFIRMED_REMOTE_SUCCEEDED):
            return False
        for owner in owners.values():
            if owner is not current and (owner.status in {"succeeded", "failed", "cancelled", "cancel_requested", "uncertain"}
                    or owner.cancel_requested_at is not None or getattr(owner, "ignore_late_result", False)):
                return False
        prep = ExecutionPreparation.objects.select_for_update().get(pk=preparation_id, asset_job=current)
        if (prep.request_frozen_at is None or prep.status == "cancelled" or not request_sha256
                or prep.request_sha256 != request_sha256
                or prep.execution_instance_version_id != current.execution_instance_version_id
                or current.request_snapshot_sha256 != job.request_snapshot_sha256):
            raise ValidationError("素材实际返回与原冻结请求不一致")
        if ExecutionTextResponseReview.objects.filter(asset_job=current).exists():
            raise ValidationError("本次素材调用已保存原始返回，不能重复写入或覆盖")
        if collecting:
            from studio.services.asset_generation import _snapshot, _verify_live_target
            snapshot = _snapshot(current)
            if current.execution_kind == "comfy_workflow":
                if current.remote_status != "succeeded" or collection_request_id != str(preparation_id):
                    raise ValidationError("工作流回执缺少确切成功提交事实")
                source = {"response_source": "comfyui_collect",
                    "output_node_id": str(snapshot.output_contract["primary"]["node_id"])}
            else:
                from studio.services.provider_registry import _combined_config
                if snapshot.transport.value["driver_type"] != "async_rest" or collection_body_kind != COLLECT_RECEIPT_KIND:
                    raise ValidationError("REST 收集回执必须属于原异步媒体调用")
                deployment = _verify_live_target(current, snapshot, require_executable_route=False)
                if current.remote_status not in _combined_config(deployment)["terminal_success"]:
                    raise ValidationError("REST 收集回执缺少原调用的已知成功终态")
                source = {"response_source": "async_rest_collect"}
            if collection_body_kind == COLLECT_RECEIPT_KIND:
                if snapshot.output_kind not in {"image", "video", "audio"}:
                    raise ValidationError("媒体收集回执必须属于冻结的媒体主输出")
                read_collect_receipt(text, request_id=collection_request_id)
                diagnostics = {**diagnostics, "body_kind": COLLECT_RECEIPT_KIND}
            elif collection_body_kind != "text" or snapshot.output_kind != "text":
                raise ValidationError("工作流原始正文只能归属冻结的文本主输出")
            diagnostics = {**diagnostics, **source, "provider_request_id": collection_request_id}
        row = ExecutionTextResponseReview(asset_job=current)
        assign_json_contract(row, "diagnostics", {**diagnostics, "preparation_id": str(prep.pk),
            "request_sha256": request_sha256}, max_bytes=execution_configuration_max_bytes())
        key, row.original_size, row.original_sha256 = store_response(text)
        row.original_file = key
        try:
            row.save()
        except Exception:
            # Only the object created by this failed write, never an existing response.
            default_storage.delete(key)
            raise
        return True


def describe_text_response_review(preparation):
    if preparation.asset_job_id:
        return _describe_asset_text_response(preparation)
    rows = list(ExecutionTextResponseReview.objects.select_related("attempt").filter(attempt__preparation=preparation)[:2])
    if not rows:
        return None
    # Media observations belong to exact Attempt task details, not the text
    # correction editor. Multiple retries may each have a retained receipt.
    if all(verified_diagnostics(row).get("body_kind") == COLLECT_RECEIPT_KIND for row in rows):
        return None
    if len(rows) != 1:
        raise ValidationError("准备关联了多条待处理响应，不能隐式选择其中一条")
    row = rows[0]
    original, diagnostics, _, transport = read_retained_text_response(row)
    readonly_reason = ""
    if row.attempt.operation == "visual_rules.generate":
        readonly_reason = _visual_protocol_readonly_reason(transport.value)
    if row.attempt.role == GenerationAttempt.Role.PRIMARY and row.attempt.status == "succeeded":
        readonly_reason = "这是该次调用的原始返回，只读保留；候选的确认与采用请在对应业务步骤处理。"
    fields = []
    if row.attempt.role == GenerationAttempt.Role.AUXILIARY:
        from studio.services.execution_auxiliary_requests import read_attempt_field_batch
        batch = read_attempt_field_batch(row.attempt)
        expected = {item["request_id"]: item for item in batch.value["items"]}
        # Presentation only: no fuzzy JSON repair, positional matching or
        # partial/duplicate ID interpretation; never makes invalid text usable.
        def unique(pairs):
            value = {}
            for key, item in pairs:
                if key in value:
                    raise ValueError("duplicate key")
                value[key] = item
            return value
        try:
            payload = json.loads(original, object_pairs_hook=unique)
            if (not isinstance(payload, dict) or set(payload) != {"items"}
                    or not isinstance(payload["items"], list) or len(payload["items"]) != len(expected)):
                raise ValueError("unmapped")
            mapped = {}
            for item in payload["items"]:
                if (not isinstance(item, dict) or set(item) != {"request_id", "text"}
                        or not isinstance(item["request_id"], str) or item["request_id"] not in expected
                        or item["request_id"] in mapped or not isinstance(item["text"], str)):
                    raise ValueError("unmapped")
                mapped[item["request_id"]] = item["text"]
            fields = [{"unit_key": item["unit_key"], "field_key": item["field_contract"]["field_key"],
                "label": item["field_contract"]["field_definition"]["label"],
                "text": mapped[key]} for key, item in expected.items()]
        except (ValueError, RecursionError):
            pass  # Raw body is still displayed with its persisted diagnostic.
        readonly_reason = "这是模型实际返回，尚不代表采用。请在主准备核对字段；需要修改时另存人工修订，不改写原文。"
    warnings = diagnostics.get("warnings", [])
    if row.attempt.role == GenerationAttempt.Role.PRIMARY:
        if row.attempt.operation == "text.structured":
            from studio.services.execution_structured_text import structured_text_warning_messages
            warnings = structured_text_warning_messages(warnings)
        if not isinstance(warnings, list) or any(not isinstance(item, str) for item in warnings):
            raise ValidationError("原主调用的候选提醒格式不一致")
        notices = [{"code": "candidate_advisory", "message": item} for item in warnings]
    else:
        notices = field_warning_messages(warnings)
    return {"id": str(row.pk), "attempt_id": str(row.attempt_id), "asset_job_id": None,
        **_correction_detail(row), "original_text": original, "fields": fields,
        "notices": notices,
        "original_sha256": row.original_sha256, "diagnostics": diagnostics,
        "confirmed_at": row.confirmed_at.isoformat() if row.confirmed_at else None,
        "readonly_reason": readonly_reason,
        "editable": not readonly_reason and row.attempt.role == GenerationAttempt.Role.PRIMARY and row.attempt.status == "awaiting_review" and preparation.status != "cancelled" and row.confirmed_at is None}


def _visual_protocol_readonly_reason(transport):
    if transport.get("operation") == "visual_rules.generate":
        from studio.services.execution_native_text import NATIVE_TEXT_ADAPTERS
        if transport.get("output", {}).get("adapter") != NATIVE_TEXT_ADAPTERS["visual_rules.generate"]:
            return "此响应使用旧影视规则协议，仅保留查看；请使用新执行实例版本重新创建准备，不会转换或重跑旧请求。"
    return ""


def complete_text_response(*, preparation_id, expected_preparation_revision, review_id, original_sha256, text, actor):
    if not isinstance(text, str) or not text.strip():
        raise ValidationError("请补全本次输出，不能提交空内容")
    from studio.models import ExecutionPreparation
    if ExecutionPreparation.objects.filter(pk=preparation_id, asset_job__isnull=False).exists():
        return _complete_asset_text_response(preparation_id=preparation_id,
            expected_preparation_revision=expected_preparation_revision, review_id=review_id,
            original_sha256=original_sha256, text=text, actor=actor)
    try:
        parent_id = GenerationAttempt.objects.filter(preparation_id=preparation_id,
            text_response_review__pk=review_id).values_list("parent_preparation_id", flat=True).get()
    except GenerationAttempt.DoesNotExist as exc:
        raise ValidationError("当前准备不包含该待处理响应", code="execution_preparation_conflict") from exc
    lock_ids = [preparation_id] + ([parent_id] if parent_id else [])
    with _locked_preparations(lock_ids, actor) as rows:
        prep = rows[str(preparation_id)]
        row = ExecutionTextResponseReview.objects.select_for_update().select_related("attempt").get(pk=review_id, attempt__preparation=prep)
        original, diagnostics, definition, transport = read_retained_text_response(row)
        if diagnostics.get("body_kind") == COLLECT_RECEIPT_KIND:
            raise ValidationError("媒体回执只读，不能补正为文本候选", code="execution_preparation_conflict")
        if original_sha256 != row.original_sha256 or hashlib.sha256(original.encode("utf-8")).hexdigest() != original_sha256:
            raise ValidationError("原始响应已变化", code="execution_preparation_conflict")
        if row.confirmed_at is not None:
            if row.corrected_text != text:
                raise ValidationError("此响应已处理，不能覆盖已确认结果", code="execution_preparation_conflict")
            return prep
        attempt = GenerationAttempt.objects.select_for_update().get(pk=row.attempt_id)
        if attempt.role != GenerationAttempt.Role.PRIMARY:
            raise ValidationError("辅助输出请在主准备中人工填写并确认字段；不能将人工修订标为模型字段结果")
        stage = prep.stage_run
        run = ProductionRun.objects.get(pk=stage.production_run_id)
        if (prep.revision != expected_preparation_revision or prep.status == "cancelled" or attempt.status != "awaiting_review"
                or attempt.ignore_late_result or run.status in {"cancelled", "cancelling", "succeeded"} or stage.status == "cancelled"):
            raise ValidationError("准备或运行已改变，请重新读取", code="execution_preparation_conflict")
        readonly_reason = _visual_protocol_readonly_reason(transport.value)
        if readonly_reason:
            raise ValidationError(readonly_reason, code="execution_response_protocol_retired")
        if attempt.request_sha256 != prep.request_sha256 or attempt.request_snapshot_sha256 != prep.request_sha256:
            raise ValidationError("原调用与冻结请求不一致，不能补全到另一请求")
        parsed = parse_execution_text_response(attempt.operation, {"text": text},
            operation_plan=transport.value.get("operation_plan"), request_payload=transport.value["payload"],
            expected_output=definition.value["interface"].get("business_output"))
        from studio.services.execution_response_storage import store_response
        row.corrected_file, row.corrected_size, row.corrected_sha256 = store_response(text)
        row.confirmed_by, row.confirmed_at = actor, timezone.now()
        row.save()
        # The candidate must read actual confirmed bytes, not a promise that a
        # review will be saved later. Root locks keep this and settlement atomic:
        # a candidate/settlement failure rolls back the confirmation as well.
        from studio.services.execution_production_text_worker import _candidate
        result = _candidate(attempt, stage, run, parsed, recovery={"review_id": str(row.pk),
            "original_sha256": row.original_sha256, "corrected_sha256": row.corrected_sha256,
            "confirmed_by": str(actor.pk)})
        attempt.status, attempt.error_code, attempt.error_message, attempt.result_sha256 = "succeeded", "", "", result
        attempt.save(update_fields=("status", "error_code", "error_message", "result_sha256", "updated_at"))
        if attempt.actual_cost is not None:
            from studio.services.execution_resource_policy import pause_production_actual_overage
            pause_production_actual_overage(run, stage)
        prep.revision += 1
        prep.save(update_fields=("revision", "updated_at"))
        # Leave candidate adoption and recipe continuation to the caller's
        # separate explicit action. Never auto-select human-corrected output.
        return prep


def _correction_detail(row):
    if row.confirmed_at is None:
        if any(getattr(row, key) is not None for key in row._WRITE_ONCE_FIELDS):
            raise ValidationError("人工修订记录不完整，不能伪装未处理或已确认")
        return {"corrected_text": None, "corrected_sha256": None, "confirmed_by": None}
    if any(getattr(row, key) is None for key in row._WRITE_ONCE_FIELDS):
        raise ValidationError("人工修订缺少确认或正文事实")
    return {"corrected_text": row.corrected_text, "corrected_sha256": row.corrected_sha256,
        "confirmed_by": str(row.confirmed_by_id)}


def _asset_response_readonly_reason(preparation, row, diagnostics):
    from studio.models import AssetGenerationJobResolution
    job = row.asset_job
    if diagnostics.get("body_kind") == COLLECT_RECEIPT_KIND:
        return "这是驱动实际收集回执，不是可编辑正文或可用素材；仅供只读核对，不支持人工补正或自动重试。"
    if job.parent_preparation_id or job.job_type == "field":
        return "字段辅助返回只读；请在父准备人工填写并确认字段，不将人工修订标为模型字段输出。"
    if row.confirmed_at is not None:
        return "已保存人工修订；原文和原校验记录保留，不能覆盖，不代表已采用。"
    if (preparation.status == "cancelled" or job.status != "failed" or job.claim_token is not None
            or job.cancel_requested_at is not None or job.ignore_late_result
            or job.output_asset_id is not None or job.result_text or job.result_text_sha256):
        return "仅明确失败且无产物、未取消且不在处理的原调用可补正；成功、未知及停止的调用只读。"
    if job.retries.exists() or job.resolutions.filter(outcome__in=AssetGenerationJobResolution.OPERATOR_OUTCOMES).exists():
        return "原调用已有重试或人工处置，请查看确切后继任务；不能同时补正原调用。"
    if diagnostics.get("contract_error_code") not in {"response_contract_invalid", "response_output_budget_exhausted"}:
        return "原返回没有可补正的正文校验失败事实；不会用人工编辑修复传输或保存故障。"
    return ""


def _describe_asset_text_response(preparation):
    row = ExecutionTextResponseReview.objects.select_related("asset_job").filter(asset_job_id=preparation.asset_job_id).first()
    if row is None:
        return None
    original, diagnostics, _, _ = read_retained_text_response(row)
    from studio.task_center_public import public_response_text
    readonly = _asset_response_readonly_reason(preparation, row, diagnostics)
    return {"id": str(row.pk), "attempt_id": None, "asset_job_id": str(row.asset_job_id),
        "original_text": public_response_text(original, diagnostics) if diagnostics.get("body_kind") == COLLECT_RECEIPT_KIND else original,
        "original_sha256": row.original_sha256, "diagnostics": diagnostics,
        **_correction_detail(row), "confirmed_at": row.confirmed_at.isoformat() if row.confirmed_at else None,
        "editable": not readonly, "readonly_reason": readonly, "fields": [], "notices": []}


def asset_text_correction(job, text):
    """Materializers read the actual immutable human confirmation, never a flag."""
    row = ExecutionTextResponseReview.objects.select_related("asset_job").filter(asset_job=job, confirmed_at__isnull=False).first()
    if row is None:
        return None
    _, _, definition, transport = read_retained_text_response(row)
    correction = _correction_detail(row)
    parsed = parse_execution_text_response(job.operation, {"text": correction["corrected_text"]},
        operation_plan=transport.value.get("operation_plan"), request_payload=transport.value["payload"],
        expected_output=definition.value["interface"].get("business_output"))
    from studio.services.execution_native_text import NATIVE_TEXT_OPERATIONS
    expected = parsed["text"] if job.operation in NATIVE_TEXT_OPERATIONS else correction["corrected_text"]
    if job.parent_preparation_id or job.job_type == "field" or text != expected:
        raise ValidationError("产物必须来自本次确切人工修订，不能改写或冒充模型原文")
    return {"review_id": str(row.pk), "original_sha256": row.original_sha256,
        "corrected_sha256": row.corrected_sha256, "confirmed_by": str(row.confirmed_by_id),
        "confirmed_at": row.confirmed_at.isoformat()}


def _complete_asset_text_response(*, preparation_id, expected_preparation_revision, review_id, original_sha256, text, actor):
    from django.core.files.storage import default_storage
    from studio.services.asset_generation import _finish_text_asset_result
    from studio.services.execution_native_text import NATIVE_TEXT_OPERATIONS
    from studio.services.execution_response_storage import store_response
    from studio.services.registry_reference_lock import registry_reference_write_guard
    created_files = []
    try:
        # Same registry/root/Preparation order as retry and completion. A retry
        # cannot be created between the eligibility check and human correction.
        with registry_reference_write_guard(), _locked_preparations([preparation_id], actor) as rows:
            prep = rows[str(preparation_id)]
            row = ExecutionTextResponseReview.objects.select_for_update().select_related("asset_job").filter(
                pk=review_id, asset_job_id=prep.asset_job_id).first()
            if row is None:
                raise ValidationError("当前准备不包含该待处理响应", code="execution_preparation_conflict")
            _, diagnostics, definition, transport = read_retained_text_response(row)
            if original_sha256 != row.original_sha256:
                raise ValidationError("原始响应已变化", code="execution_preparation_conflict")
            correction = _correction_detail(row)
            if row.confirmed_at is not None:
                if correction["corrected_text"] != text:
                    raise ValidationError("此响应已处理，不能覆盖已确认结果", code="execution_preparation_conflict")
                return prep
            reason = _asset_response_readonly_reason(prep, row, diagnostics)
            if type(expected_preparation_revision) is not int or prep.revision != expected_preparation_revision or reason:
                raise ValidationError(reason or "准备已改变，请重新读取", code="execution_preparation_conflict")
            job = row.asset_job
            parsed = parse_execution_text_response(job.operation, {"text": text},
                operation_plan=transport.value.get("operation_plan"), request_payload=transport.value["payload"],
                expected_output=definition.value["interface"].get("business_output"))
            row.corrected_file, row.corrected_size, row.corrected_sha256 = store_response(text)
            created_files.append(row.corrected_file)
            row.confirmed_by, row.confirmed_at = actor, timezone.now()
            row.save()
            result = _finish_text_asset_result(job, parsed["text"] if job.operation in NATIVE_TEXT_OPERATIONS else text,
                created_files=created_files)
            if result.status != "succeeded":
                raise ValidationError("原任务已变化，人工修订未保存", code="execution_preparation_conflict")
            prep.revision += 1
            prep.save(update_fields=("revision", "updated_at"))
            return prep
    except Exception:
        # Only files written by this transaction, never original response/assets.
        for key in created_files:
            default_storage.delete(key)
        raise
