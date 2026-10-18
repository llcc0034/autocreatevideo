"""Inspect original saved requests and outcomes without evaluation or dispatch."""
from hashlib import sha256
import json

from django.core.exceptions import ObjectDoesNotExist, ValidationError as DjangoValidationError
from django.db.models import CharField, F, OuterRef, Q, Subquery, Value
from rest_framework.exceptions import NotFound, ValidationError

from studio.models import (AssemblyJob, AssetGenerationJob, ExecutionPreparation, GeneratedArtifact, GenerationAttempt,
                           GenerationTask, ModelDeploymentSmokeAttempt, WorkflowSmokeAttempt)
from studio.services.execution_preparations import read_execution_preparation_detail, read_frozen_execution_request
from studio.services.json_contracts import verify_json_contract
from studio.services.execution_instances import execution_configuration_max_bytes
from studio.services.security_boundaries import security_boundary_resolution_cache
from studio.task_center import detail_row, page_links, primary_attempts
from studio.task_center_public import public_value, public_media, public_response_text
from studio.task_center_prompt_trace import prompt_trace, protocol_notes
from studio.task_center_field_trace import field_batch_trace
from studio.task_center_api_mapping import api_mapping
from studio.task_center_native_inputs import native_input_trace
from studio.task_center_system_message import system_message_trace
from studio.task_center_submission import submission_evidence
from studio.task_center_serializers import CONTENT_FIELDS


INLINE_BYTES = 65536


def parameter_rows(transport, definition=None):
    parameters = transport["parameters"]
    rows = [{"key": key, "value": value, "source": "transport.parameters.values:" + str(parameters.get("sources", {}).get(key, "recorded"))}
            for key, value in public_value(parameters["values"]).items()]
    payload = transport["payload"]
    for key, value in public_value(payload).items():
        if key not in {"messages", "compiled_workflow", "media_preparation", "prompt", "negative_prompt", "positive_prompt",
                       "schema_version", "idempotency_key"}:
            rows.append({"key": key, "value": value, "source": "payload." + key})
    compiled = payload.get("compiled_workflow", {})
    prompt_keys = {channel["semantic_key"] for channel in (definition or {}).get("interface", {}).get("prompt", {}).get("channels", {}).values()}
    for key, value in public_value(compiled.get("resolved_semantics", {})).items():
        if key not in prompt_keys:
            rows.append({"key": key, "value": value, "source": "payload.compiled_workflow.resolved_semantics"})
    # Fixed widgets can affect sampling without a semantic binding. Keep their
    # actual graph identity; never invent a semantic alias or include links/files.
    from studio.services.workflow_media_tokens import is_workflow_media_use_token
    for node_id, node in compiled.get("graph", {}).items():
        for key, value in public_value(node["inputs"]).items():
            if isinstance(value, (dict, list)) or (isinstance(value, str) and (is_workflow_media_use_token(value) or value.startswith("[redacted"))):
                continue
            if key.endswith("_configured"):
                continue
            rows.append({"key": f"{node['class_type']}.{key}", "value": value, "source": f"graph.{node_id}.inputs.{key}"})
    return public_value(rows)


def prompt_rows(transport, definition=None):
    payload = transport["payload"]
    if "messages" in payload:
        if not isinstance(payload["messages"], list):
            raise DjangoValidationError("冻结 messages 不是数组")
        rows = []
        for index, message in enumerate(payload["messages"]):
            if not isinstance(message, dict) or not isinstance(message.get("role"), str) or "content" not in message:
                raise DjangoValidationError("冻结 messages 结构不完整")
            content = message["content"]
            rows.append({"key": f"messages.{index}", "role": message["role"],
                         "text": content if isinstance(content, str) else json.dumps(public_value(content), ensure_ascii=False),
                         "source": "payload.messages"})
        return public_value(rows)
    channels = transport["prompt_channels"]
    if "compiled_workflow" in payload:
        from studio.services.execution_workflow_inspection import frozen_workflow_prompt_rows
        rows = frozen_workflow_prompt_rows(definition, transport)
        return public_value([{"key": row["key"], "role": row["key"], "text": row["text"],
            "source": "transport.prompt_channels:" + row["value_source"]} for row in rows])
    if not isinstance(channels, dict) or not isinstance(channels.get("values"), dict):
        raise DjangoValidationError("冻结提示词通道缺失")
    return public_value([{"key": key, "role": key, "text": value, "source": "transport.prompt_channels.values"}
                         for key, value in channels["values"].items()])


def inspect_input(preparation, source=None, source_kind=None):
    if preparation.request_frozen_at:
        detail, definition, transport = read_frozen_execution_request(preparation)
        parameters, prompts = parameter_rows(transport.value, definition.value), prompt_rows(transport.value, definition.value)
        trace = prompt_trace(transport.value, definition.value, detail.resolved_inputs)
        field_trace = public_value(field_batch_trace(transport.value, detail.resolved_inputs))
        api_trace = public_value(api_mapping(definition, transport))
        native_trace = native_input_trace(transport.value, definition.value)
        system_trace = system_message_trace(transport.value, definition.value)
        media = None
        if transport.value.get("driver_type") == "comfyui":
            from studio.task_center_media_trace import media_trace
            root = source
            if source_kind == "generation_attempt":
                root = source.generation_task if source else None
                if root is not None:
                    from studio.services.execution_production_workflow_queue import read_prepared_generation_task
                    if read_prepared_generation_task(root).attempt.pk != source.pk:
                        raise DjangoValidationError("媒体回执不属于所选的原生产调用")
            if root is not None and not hasattr(root, "staged_inputs"):
                raise DjangoValidationError("媒体解释必须属于当前准备的原根任务")
            owner_id = root.owner_id if root is not None and source_kind in {"generation_attempt", "asset_job"} else preparation.created_by_id
            media = public_value(media_trace(definition, transport, detail.resolved_inputs,
                owner_id=owner_id, root_id=root.pk if root else preparation.pk,
                receipts=root.staged_inputs if root else []))
            if root is None:
                media["root_id"] = None
        state, notes = "frozen", ["来自本次冻结请求的脱敏副本；密钥、内部地址与文件路径已隐藏。",
                                *protocol_notes(transport.value, definition.value)]
        if transport.value["driver_type"] != "comfyui":
            declared = any(row["kind"] in {"image", "video", "audio"}
                           for row in definition.value["configuration"]["inputs"])
            notes.insert(1, "原非工作流请求尚无可核对的媒体槽位绑定记录，不能认定为未使用素材。"
                         if declared else "本次冻结定义未声明图片、视频或音频输入，原请求没有媒体槽位绑定；当前分镜选择不会补入历史。")
    else:
        detail = read_execution_preparation_detail(preparation)
        parameters = public_value([{"key": key, "value": value, "source": "resolved_inputs.parameters:" +
                                   str(detail.resolved_inputs.get("parameters", {}).get("sources", {}).get(key, "recorded"))}
                                  for key, value in detail.resolved_inputs.get("parameters", {}).get("values", {}).items()])
        prompts, state, notes = [], "not_frozen", ["请求尚未冻结，无最终提示词；未重新编译当前策略。"]
        trace = None
        field_trace = None
        media = None
    submitted = detail.intent["request"].get("requested")
    if not isinstance(submitted, dict):
        raise DjangoValidationError("准备缺少原始提交请求")
    return {"state": state, "submitted": public_value(submitted), "resolved": public_value(detail.resolved_inputs),
            "parameters": parameters, "prompts": prompts, "prompt_trace": trace, "field_batch_trace": field_trace, "media_trace": media,
            "api_mapping": api_trace if preparation.request_frozen_at else None,
            "native_input_trace": native_trace if preparation.request_frozen_at else None,
            "system_message_trace": system_trace if preparation.request_frozen_at else None, "notes": notes}


CALL_FIELDS = ("kind", "id", "role", "status", "operation", "preparation_id", "created_at", "finished_at", "error_code", "retry_of_id", "generation_task_id")


def preparation_calls(preparation, query, request, operation):
    from django.db.models import UUIDField
    from studio.task_center import null
    sources = []
    attempts = GenerationAttempt.objects.filter(Q(preparation_id=preparation.pk) | Q(parent_preparation_id=preparation.pk))
    sources.append(attempts.annotate(call_kind=Value("generation_attempt"), call_id=F("id"), call_role=F("role"),
        call_operation=F("operation"), call_preparation_id=F("preparation_id"), call_retry_of_id=F("retry_of_id"),
        call_generation_task_id=F("generation_task_id")))
    assets = AssetGenerationJob.objects.filter(Q(pk=preparation.asset_job_id) | Q(parent_preparation_id=preparation.pk))
    from django.db.models import Case, When
    sources.append(assets.annotate(call_kind=Value("asset_job"), call_id=F("id"),
        call_role=Case(When(parent_preparation_id=preparation.pk, then=Value("auxiliary")), default=Value("primary")),
        call_operation=F("operation"), call_preparation_id=Subquery(ExecutionPreparation.objects.filter(asset_job_id=OuterRef("pk")).values("id")[:1]),
        call_retry_of_id=F("retry_of_id"), call_generation_task_id=null(UUIDField())))
    for kind, model, identifier in (("deployment_smoke", ModelDeploymentSmokeAttempt, preparation.deployment_smoke_id),
                                    ("workflow_smoke", WorkflowSmokeAttempt, preparation.workflow_smoke_id)):
        if identifier:
            sources.append(model.objects.filter(pk=identifier).annotate(call_kind=Value(kind), call_id=F("id"), call_role=Value("primary"),
                call_operation=F("operation") if kind == "deployment_smoke" else Value(operation, output_field=CharField()),
                call_preparation_id=Value(preparation.pk, output_field=UUIDField()), call_retry_of_id=null(UUIDField()), call_generation_task_id=null(UUIDField())))
    rows = [source.annotate(**{"call_" + key: F(key) for key in ("status", "created_at", "finished_at", "error_code")})
            .order_by().values(*("call_" + key for key in CALL_FIELDS)) for source in sources]
    combined = rows[0].union(*rows[1:], all=True)
    count = combined.count()
    page, size = query["call_page"], query["call_page_size"]
    selected = combined.order_by("call_created_at", "call_kind", "call_id")[(page - 1) * size:page * size]
    calls = [{key: row["call_" + key] for key in CALL_FIELDS} for row in selected]
    nxt, previous = page_links(request, "call_page", page, size, count)
    return {"calls": calls, "call_count": count, "calls_next": nxt, "calls_previous": previous}


def result_text(result):
    if isinstance(result, str):
        return result
    if not isinstance(result, dict):
        return ""
    for key in ("text", "master_script", "content", "prompt", "candidate", "result"):
        if key in result:
            found = result_text(result[key])
            if found:
                return found
    return ""


def artifact_output(attempt, query, request):
    from studio.platform_serializers import GeneratedArtifactListSerializer
    from studio.services.execution_production_candidates import read_production_text_candidate
    from studio.services.execution_production_text_contracts import PRODUCTION_TEXT_OPERATIONS
    artifacts = GeneratedArtifact.objects.filter(generation_attempt=attempt).select_related("production_run").order_by("created_at", "id")
    count = artifacts.count()
    page, size = query["output_page"], query["output_page_size"]
    results, media, texts = [], [], []
    for artifact in artifacts[(page - 1) * size:page * size]:
        if (artifact.production_run_id != attempt.stage_run.production_run_id
                or artifact.stage_run_id != attempt.stage_run_id
                or artifact.generation_task_id != attempt.generation_task_id):
            raise DjangoValidationError("产物归属与当前调用不一致，无法读取输出证据。")
        metadata = verify_json_contract(artifact.manifest_metadata, schema_version=artifact.manifest_schema_version,
            size_bytes=artifact.manifest_metadata_size_bytes, sha256=artifact.manifest_metadata_sha256,
            max_bytes=execution_configuration_max_bytes()).value
        if artifact.storage_backend == "database" and attempt.operation in PRODUCTION_TEXT_OPERATIONS:
            candidate = read_production_text_candidate(artifact, for_audit=True)
            content = candidate
        else:
            content = metadata
        results.append({"artifact_id": str(artifact.pk), "kind": artifact.kind, "status": artifact.status, "result": public_value(content)})
        text_value = result_text(content)
        if text_value:
            texts.append(public_value(text_value))
        if artifact.kind in {"image", "video", "audio", "assembly"}:
            media.append(public_media(GeneratedArtifactListSerializer, artifact,
                fields=("id", "kind", "status", "mime_type", "size_bytes", "sha256")))
    nxt, previous = page_links(request, "output_page", page, size, count)
    return ({"state": "available" if count else "unavailable", "result": {"artifacts": results}, "text": "\n\n".join(texts),
             "media": media, "notes": [] if count else ["原调用尚无 GeneratedArtifact 输出。"]},
            {"output_count": count, "output_next": nxt, "output_previous": previous})


def inspect_output(source, kind, query, request):
    empty = {"state": "unavailable", "result": None, "text": "", "media": [], "notes": []}
    paging = {"output_count": 0, "output_next": None, "output_previous": None}
    if source is None:
        empty["notes"] = ["尚无主调用。"]
        return empty, paging
    if kind == "generation_attempt":
        from studio.models import ExecutionTextResponseReview
        from studio.services.execution_text_response_review import read_retained_text_response, _correction_detail, verified_diagnostics, COLLECT_RECEIPT_KIND
        review = ExecutionTextResponseReview.objects.filter(attempt=source).first()
        if review is not None and verified_diagnostics(review).get("body_kind") == COLLECT_RECEIPT_KIND:
            original, diagnostics, _, _ = read_retained_text_response(review)
            output, paging = artifact_output(source, query, request)
            output.update(state="available", text=public_response_text(original, diagnostics),
                result={**(output["result"] or {}), "response_review": public_value(diagnostics)})
            output["notes"].append("这是原调用实际终态回执（已脱敏），只读保留；不等于有效产物或人工采用，不能补正为文本结果。")
            return output, paging
        if review is not None and source.status == "awaiting_review":
            original, diagnostics, _, _ = read_retained_text_response(review)
            empty.update(state="awaiting_review", result={"response_review": public_value(diagnostics)},
                text=public_value(original), notes=["原始返回尚未通过校验，不是可采用候选。" + (
                    "请返回主准备，人工填写并确认字段。" if source.role == "auxiliary" else
                    "请打开执行准备，人工修正后校验保存。") + "不会自动重跑。"])
            paging["output_count"] = 1
            return empty, paging
        if source.role == "auxiliary":
            if source.status != "succeeded":
                empty["notes"] = ["此辅助调用尚未保存成功字段结果。"]
                return empty, paging
            from studio.services.execution_auxiliary_results import verify_production_field_result
            values = verify_production_field_result(source)
            rows = [{"unit_key": v.unit_key, "field_key": v.field_key, "text": public_value(v.text),
                     "field_value_id": str(v.pk)} for v in values]
            empty.update(state="available", result={"fields": rows}, text="\n\n".join(v["text"] for v in rows),
                         notes=["辅助调用已生成待确认字段；不代表人工采用。"])
            paging["output_count"] = len(rows)
            return empty, paging
        return artifact_output(source, query, request)
    if kind in {"deployment_smoke", "workflow_smoke"}:
        if not source.result_sha256:
            # Both Smoke models initialize the absent result as {} (two JSON
            # bytes). Its declared size alone is not evidence of an outcome.
            if (source.result != {} or source.result_size_bytes != 2
                    or source.result_schema_version != 1 or source.status == "succeeded"):
                raise DjangoValidationError("Smoke 输出缺少结果摘要，不能视为空结果")
            empty["notes"] = ["原烟测尚未记录输出结果；不代表校验失败、执行成功或没有外呼。"]
            return empty, paging
        from studio.services.execution_smoke_evidence import read_prepared_smoke_fact
        read_prepared_smoke_fact(source)
        result = source.result
        if not isinstance(result, dict) or not isinstance(result.get("evidence"), dict):
            raise DjangoValidationError("Smoke 结果缺少 evidence 对象")
        evidence = result["evidence"]
        facts = evidence.get("artifact_facts", [])
        if kind == "workflow_smoke" and (not isinstance(facts, list) or any(not isinstance(f, dict) for f in facts)):
            raise DjangoValidationError("Smoke 产物证据结构不完整")
        # Smoke stores probed evidence, not a durable playable Asset. Do not fabricate URLs.
        texts = [result_text(evidence.get("result"))] if kind == "deployment_smoke" else [
            result_text(fact.get("result")) for fact in facts]
        notes = ["Smoke 媒体探测证据位于 result.evidence；未生成可播放素材时不提供预览地址。"]
        if kind == "deployment_smoke" and "response_body" in evidence:
            from studio.services.execution_smoke_responses import read_smoke_response
            original = read_smoke_response(source, evidence["response_body"])
            if original is not None:
                texts = [original]
                notes = ["这是本次模型返回的原始正文（已脱敏），不是人工修正结果；烟测通过与否以原任务状态为准。"]
            else:
                notes = ["本次响应没有可保存的文本正文；请查看校验诊断，不能视为有效空结果。"]
        empty.update(state="available", result=public_value(result), text=public_value("\n\n".join(t for t in texts if t)),
                     notes=notes)
        paging["output_count"] = 1
        return empty, paging
    if kind == "asset_job":
        from studio.models import ExecutionTextResponseReview
        from studio.services.execution_text_response_review import read_retained_text_response, _correction_detail
        retained = ExecutionTextResponseReview.objects.filter(asset_job=source).first()
        if retained is not None:
            original, diagnostics, _, _ = read_retained_text_response(retained)
            empty.update(state="available", result={"response_review": public_value(diagnostics)},
                text=public_response_text(original, diagnostics), notes=["这是本次独立素材调用实际返回的原文，不等于有效候选、素材或人工采用。"
                    "本页只读；可在执行准备中查看补正资格，补正不调用模型；另行重试会再次调用模型。"])
            from studio.services.execution_text_response_review import COLLECT_RECEIPT_KIND
            if diagnostics.get("body_kind") == COLLECT_RECEIPT_KIND:
                empty["notes"] = ["这是本次驱动 collect 返回对象的 JSON 脱敏副本，不是远端原始 HTTP 字节、可用素材或成功证明；"
                    "只读，不支持正文补正，不会自动重跑。任务结果以原任务状态及产物校验为准。"]
            correction = _correction_detail(retained)
            if retained.confirmed_at is not None:
                empty["result"]["human_correction"] = public_value({**correction,
                    "review_id": str(retained.pk), "confirmed_at": retained.confirmed_at.isoformat()})
                empty["notes"].append("当前结果来自已确认的人工修订，不是模型重新返回，不代表已采用。")
            paging["output_count"] = 1
            # Successful result/media still appears below; original bytes are
            # never replaced with a normalized candidate or an empty fallback.
        text_value = source.result_text
        if text_value or source.result_text_sha256:
            encoded = text_value.encode("utf-8")
            if len(encoded) != source.result_text_size_bytes or sha256(encoded).hexdigest() != source.result_text_sha256:
                raise DjangoValidationError("素材文本结果正文与摘要不一致")
        media = []
        if source.output_asset_id:
            from studio.serializers import AssetSerializer
            # Call the media method only; the full Asset representation includes private paths.
            asset = source.output_asset
            url = AssetSerializer().get_preview_url(asset)
            media = [{"id": str(asset.pk), "kind": asset.kind, "name": public_value(asset.name), "mime_type": asset.mime_type,
                      "preview_url": url if url.startswith("/api/v1/media/") else ""}]
        empty["media"] = media
        if retained is None:
            empty.update(state="available" if text_value or media else "unavailable", result=None, text=public_value(text_value))
            paging["output_count"] = int(bool(text_value or media))
            if not text_value and not media:
                empty["notes"] = ["本次素材任务没有已保存的响应正文或可用产物；不根据错误消息补造模型返回。"]
        elif text_value:
            empty["result"]["candidate_text"] = public_value(text_value)
        return empty, paging
    if kind == "assembly":
        from studio.serializers import AssemblyJobSerializer
        serializer = AssemblyJobSerializer()
        url = serializer.get_output_url(source)
        empty.update(state="available" if url else "unavailable", media=[{"id": str(source.pk), "kind": "video",
            "preview_url": url, "download_url": serializer.get_download_url(source)}] if url else [])
        paging["output_count"] = int(bool(url))
    elif kind == "generation_task":
        from studio.serializers import GenerationTaskSerializer
        # Existing output serializer issues signed URLs rather than raw remote receipts.
        rows = GenerationTaskSerializer().get_output_media(source)
        empty.update(state="available" if rows else "unavailable", media=rows,
                     notes=["缺少正式准备关联，仅展示原 GenerationTask 输出记录。"])
        paging["output_count"] = len(rows)
    return empty, paging


def bound_content(detail, query):
    """Large fields are explicit, with bounded JSON-text reads from the same GET."""
    deferred = []
    selected = query.get("content_field")
    for path in CONTENT_FIELDS:
        section, key = path.split(".")
        value = detail[section][key]
        encoded = json.dumps(value, ensure_ascii=False, separators=(",", ":"))
        size = len(encoded.encode("utf-8"))
        if selected == path:
            offset, limit = query["content_offset"], query["content_limit"]
            if offset > len(encoded):
                raise ValidationError({"content_offset": "偏移超过字段长度"})
            chunk = encoded[offset:offset + limit]
            # Offsets count Unicode characters; limits also cap UTF-8 response bytes.
            while len(chunk.encode("utf-8")) > limit:
                chunk = chunk[:len(chunk) // 2]
            if not chunk and offset < len(encoded):
                raise ValidationError({"content_limit": "字节上限不足以读取下一个字符"})
            detail["content"] = {"field": path, "encoding": "json", "offset_unit": "unicode_character",
                "offset": offset, "next_offset": offset + len(chunk) if offset + len(chunk) < len(encoded) else None,
                "total_characters": len(encoded), "size_bytes": size, "sha256": sha256(encoded.encode("utf-8")).hexdigest(), "text": chunk}
        if size > INLINE_BYTES:
            deferred.append({"field": path, "size_bytes": size, "total_characters": len(encoded), "encoding": "json",
                             "sha256": sha256(encoded.encode("utf-8")).hexdigest()})
            detail[section][key] = None if key in {"submitted", "resolved", "result", "prompt_trace", "field_batch_trace", "media_trace"} else "" if key == "text" else []
            if detail[section]["state"] != "error":
                detail[section]["state"] = "deferred"
            detail[section]["notes"].append(f"{path} 超过内联上限，使用 content_field 按需读取完整脱敏内容。")
    detail["deferred_fields"] = deferred
    return detail


@security_boundary_resolution_cache()
def task_detail(actor, kind, identifier, query, request):
    row = detail_row(actor, kind, identifier)
    detail = {**row, "outcome": None, "purpose": None, "preparation_status": None, "request_frozen_at": None, "request_sha256": None,
        "input": {"state": "unavailable", "submitted": None, "resolved": None, "parameters": [], "prompts": [], "prompt_trace": None, "field_batch_trace": None, "media_trace": None, "api_mapping": None, "native_input_trace": None, "system_message_trace": None, "notes": []},
        "output": {"state": "unavailable", "result": None, "text": "", "media": [], "notes": []},
        "validation": {"status": "unavailable", "warnings": [], "error_code": "", "error_message": ""},
        "calls": [], "call_count": 0, "calls_next": None, "calls_previous": None,
        "output_count": 0, "output_next": None, "output_previous": None,
        "links": {"generation_task_id": None, "asset_job_id": None}, "actions": {"cancel_smoke": False, "cancel_asset": False}}
    source, source_kind = None, row["source_kind"]
    if kind == "preparation":
        prep = ExecutionPreparation.objects.get(pk=identifier)
        detail.update(purpose=prep.purpose, preparation_status=prep.status, request_frozen_at=prep.request_frozen_at, request_sha256=prep.request_sha256)
        detail.update(preparation_calls(prep, query, request, row["operation"]))
        if prep.stage_run_id:
            attempts = primary_attempts(prep.pk)
            source = attempts.filter(pk=query["attempt_id"]).first() if query.get("attempt_id") else attempts.first()
            if query.get("attempt_id") and source is None:
                raise NotFound()
            if source:
                detail.update(source_id=str(source.pk), source_kind="generation_attempt", status=source.status,
                    finished_at=source.finished_at, estimated_cost=source.estimated_cost, actual_cost=source.actual_cost,
                    cost_currency=source.cost_currency)
                source_kind = "generation_attempt"
                detail["links"]["generation_task_id"] = str(source.generation_task_id) if source.generation_task_id else None
        elif prep.asset_job_id:
            source = AssetGenerationJob.objects.select_related("output_asset").get(pk=prep.asset_job_id)
            detail["links"]["asset_job_id"] = str(source.pk)
            detail["actions"]["cancel_asset"] = source.status in {"preparing", "queued", "submitting", "submitted", "running"}
        elif prep.deployment_smoke_id:
            source = ModelDeploymentSmokeAttempt.objects.get(pk=prep.deployment_smoke_id)
        else:
            source = WorkflowSmokeAttempt.objects.get(pk=prep.workflow_smoke_id)
        if source_kind in {"deployment_smoke", "workflow_smoke"}:
            detail["actions"]["cancel_smoke"] = bool(actor.is_staff and (
                (source.submission_started_at is None and source.status in {"preparing", "queued"}) or
                (source_kind == "workflow_smoke" and source.status in {"submitting", "submitted", "running"})))
        if query.get("attempt_id") and not prep.stage_run_id:
            raise ValidationError({"attempt_id": "此记录不是生产调用"})
        from studio.services.execution_preparation_presentation import selected_preparation_outcome
        detail["outcome"] = selected_preparation_outcome(prep, source)
        try:
            detail["input"] = inspect_input(prep, source, source_kind)
            detail["validation"]["status"] = "valid" if prep.request_frozen_at else "not_frozen"
            if source and source_kind in {"generation_attempt", "deployment_smoke"} and prep.request_frozen_at and source.request_sha256 != prep.request_sha256:
                raise DjangoValidationError("主调用请求摘要与准备不一致")
        except (DjangoValidationError, ObjectDoesNotExist, KeyError, TypeError, ValueError) as exc:
            detail["input"].update(state="error", notes=["冻结输入校验失败，未返回未校验的正文。"])
            detail["input"].update(submitted=None, resolved=None, parameters=[], prompts=[], prompt_trace=None, field_batch_trace=None, media_trace=None, api_mapping=None, native_input_trace=None, system_message_trace=None)
            detail["validation"].update(status="invalid", error_code="task_center_input_invalid", error_message=public_value(str(exc)))
    else:
        source = ((AssemblyJob.objects.select_related("consumer_stage") if kind == "assembly"
            else GenerationTask.objects.all()).get(pk=identifier))
        detail["input"]["notes"] = ["此记录没有 ExecutionPreparation；无最终冻结提示词。"]
        if kind == "assembly":
            detail["input"].update(state="recorded", submitted={"scope": source.scope, "episode_id": str(source.episode_id) if source.episode_id else None,
                "dry_run": source.dry_run, "has_audio": source.has_audio,
                "consumer_stage_id": str(source.consumer_stage_id) if source.consumer_stage_id else None,
                "consumer_input_sha256": source.consumer_input_sha256 or None,
                "source_signature": source.source_signature,
                "consumer_input_status": ("current" if source.consumer_input_sha256 == source.consumer_stage.input_snapshot_sha256
                    else "changed") if source.consumer_stage_id else "unrecorded"})
            detail["input"]["notes"] = ["装配消费阶段与输入摘要取自创建时记录，不是源片段的生成阶段；重试不覆盖旧任务。"]
            if not source.consumer_stage_id:
                detail["input"]["notes"].append("此任务未记录消费阶段；不从当前链接、项目或片段来源补造历史归属。")
            elif source.consumer_input_sha256 != source.consumer_stage.input_snapshot_sha256:
                detail["input"]["notes"].append("消费阶段的当前输入已变化；此处保留原任务摘要，不用当前输入替换历史版本。")
        else:
            detail["links"]["generation_task_id"] = str(source.pk)
        detail["calls"] = [{"kind": kind, "id": str(source.pk), "role": "primary", "status": source.status,
            "operation": row["operation"], "preparation_id": None, "created_at": source.created_at, "finished_at": source.finished_at,
            "error_code": "", "retry_of_id": None, "generation_task_id": str(source.pk) if kind == "generation_task" else None}]
        detail["call_count"] = 1
    detail["submission"] = submission_evidence(source, source_kind)
    if (kind == "preparation" and source_kind == "asset_job"
            and any(prompt["source"] == "payload.messages" for prompt in detail["input"]["prompts"])):
        from studio.services.chat_http_evidence import read_chat_body_note
        detail["submission"]["notes"].append(read_chat_body_note(source,
            preparation_id=prep.pk, request_sha256=prep.request_sha256))
    from studio.services.execution_production_text_contracts import PRODUCTION_TEXT_OPERATIONS
    from studio.services.execution_field_contracts import FIELD_OPERATION_MODES
    if (source is not None and source_kind == "generation_attempt" and source.generation_task_id is None
            and source.operation in {*PRODUCTION_TEXT_OPERATIONS, *FIELD_OPERATION_MODES}):
        from studio.services.chat_http_evidence import read_chat_body_note
        detail["submission"]["notes"].append(read_chat_body_note(source, preparation_id=source.preparation_id))
    if kind == "preparation" and source_kind == "deployment_smoke":
        from studio.services.execution_native_text import NATIVE_TEXT_OPERATIONS
        from studio.services.execution_text_contracts import TEXT_EXECUTION_OPERATIONS
        if source.operation in {*NATIVE_TEXT_OPERATIONS, *TEXT_EXECUTION_OPERATIONS}:
            from studio.services.chat_http_evidence import read_chat_body_note
            detail["submission"]["notes"].append(read_chat_body_note(source, preparation_id=prep.pk))
    try:
        output, paging = inspect_output(source, source_kind, query, request)
        if (source is not None and source_kind == "generation_attempt" and source.generation_task_id is None
                and source.operation in {*PRODUCTION_TEXT_OPERATIONS, *FIELD_OPERATION_MODES}):
            from studio.services.execution_text_response_usage import read_text_response_usage
            receipt, note = read_text_response_usage(source)
            output["result"] = {**(output["result"] or {}), "provider_response": receipt}
            output["notes"].append(note)
        if source is not None and source_kind == "asset_job":
            from studio.services.execution_native_text import NATIVE_TEXT_OPERATIONS
            from studio.services.execution_text_contracts import TEXT_EXECUTION_OPERATIONS
            if source.operation in {*NATIVE_TEXT_OPERATIONS, *TEXT_EXECUTION_OPERATIONS}:
                from studio.services.execution_text_response_usage import read_text_response_usage
                receipt, note = read_text_response_usage(source, preparation_id=prep.pk, request_sha256=prep.request_sha256)
                output["result"] = {**(output["result"] or {}), "provider_response": receipt}
                output["notes"].append(note)
        detail["output"], detail["output_count"] = output, paging["output_count"]
        detail.update(paging)
    except (DjangoValidationError, ObjectDoesNotExist, KeyError, TypeError, ValueError) as exc:
        detail["output"].update(state="error", notes=["输出事实校验失败，未伪装为空结果。"])
        detail["validation"].update(status="invalid", error_code="task_center_output_invalid", error_message=public_value(str(exc)))
    if source:
        if not detail["validation"]["error_code"]:
            detail["validation"].update(error_code=public_value(getattr(source, "error_code", "")),
                error_message=public_value(source.error_message))
    return bound_content(detail, query)
