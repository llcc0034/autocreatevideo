"""Manual script approval over original revisions, selections and decisions.

Step approval revisions remain local to their own Stage. The current Run
script is not inferred from the newest candidate or the project's mother draft.
"""
from django.core.exceptions import ValidationError
from django.db import transaction
from django.utils import timezone

from studio.models import ApprovalDecision, GeneratedArtifact, ProductionRun, ScriptRevision, ScriptSource, StageRun
from studio.services.content_contracts import normalize_optional_audit_comment
from studio.services.json_contracts import DATABASE_JSON_FIELD_MAX_BYTES, assign_json_contract, verify_json_contract
from studio.services.production_manual_commands import ManualActionResult
from studio.services.production_run_plans import confirmed_script_reference, get_run_execution_plan
from studio.services.production_manual_text_candidates import manual_text_candidate
from studio.services.production_manual_change_impact import require_replaceable_content_dependents
from studio.services.script_review import apply_verified_script_candidate


def _conflict(message):
    raise ValidationError(message, code="manual_selection_conflict")


def script_step_adoption(stage, *, include_structure=False):
    decision = stage.approval_decisions.only("id", "stage_run_id", "revision", "output_snapshot_sha256",
        "actor", "decision", "decided_by_id").order_by("-revision").first()
    if decision is None:
        if stage.status != "pending":
            _conflict("剧本采用步骤缺少正式人工决策，不能从当前项目回填")
        return {"revision": 0, "decision_id": None, "content": None,
            **({"structure_source": None} if include_structure else {})}
    output = verify_json_contract(stage.output_snapshot, schema_version=stage.output_snapshot_schema_version,
        size_bytes=stage.output_snapshot_size_bytes, sha256=stage.output_snapshot_sha256,
        max_bytes=DATABASE_JSON_FIELD_MAX_BYTES)
    value = output.value
    if not isinstance(value, dict):
        _conflict("剧本采用快照不是完整对象")
    if (stage.status != "succeeded" or decision.actor != "human" or decision.decision != "approve"
            or not decision.decided_by_id or decision.output_snapshot_sha256 != output.sha256):
        _conflict("剧本采用决策与阶段事实不一致，请检查原记录")
    if stage.stage_key == "script_input":
        valid = output.schema_version == 4 and set(value) == {"schema_version", "content", "structure_source"}
        source = value.get("structure_source")
    elif stage.stage_key == "script_review":
        evidence = value.get("candidate")
        valid = (output.schema_version == 1 and set(value) == {"schema_version", "kind", "content", "candidate"}
            and value["kind"] == "manual_script_review" and isinstance(evidence, dict)
            and evidence.get("kind") == "script_review_decision"
            and isinstance(value.get("content"), dict)
            and evidence.get("script_revision_id") == value["content"].get("id")
            and evidence.get("script_content_sha256") == value["content"].get("sha256"))
        source = evidence.get("structure_source") if isinstance(evidence, dict) else None
        valid = valid and isinstance(source, dict)
    else:
        raise ValidationError("此阶段不是剧本采用步骤")
    content = value.get("content")
    if (not valid or not isinstance(content, dict) or set(content) != {
            "kind", "id", "source_id", "revision", "sha256", "size_bytes", "confirmed_by_id", "confirmed_at"}
            or content["kind"] != "script_revision"):
        _conflict("剧本采用快照缺少明确内容引用，不读取旧缓存或推断")
    if source is not None and (not isinstance(source, dict) or source.get("schema_version") != 1
            or source.get("kind") != "confirmed_script_structure" or source.get("script") != content):
        _conflict("剧本结构化来源不是本次采用的确切修订")
    return {"revision": decision.revision, "decision_id": str(decision.pk), "content": content,
        **({"structure_source": source} if include_structure else {})}


def read_manual_script_structure(run):
    """On-demand downstream export from the current original adoption decision.

    Ordinary options/list reads keep their small existing projection. Raw input
    remains adoptable, but cannot be advertised as materializable structure.
    """
    from studio.services.execution_instances import execution_configuration_max_bytes
    from studio.services.script_revision_structures import read_confirmed_script_structure

    states = manual_script_states(run)
    if states["current_script"] is None:
        raise ValidationError("本轮尚未采用剧本")
    stage = StageRun.objects.only("id", "stage_key", "status", "output_snapshot", "output_snapshot_schema_version",
        "output_snapshot_size_bytes", "output_snapshot_sha256").get(
            approval_decisions__id=states["current_script_decision_id"], production_run_id=run.pk)
    adopted = script_step_adoption(stage, include_structure=True)
    source = adopted["structure_source"]
    if source is None:
        actual = confirmed_script_reference(run.project_id, {"kind": "script_revision", "id": adopted["content"]["id"],
            "expected_sha256": adopted["content"]["sha256"]})
        if actual != adopted["content"]:
            _conflict("剧本确认事实与本次采用不一致")
        raise ValidationError("本次采用的剧本尚无结构化详情，请先完成结构化并采用结果", code="script_structure_required")
    return read_confirmed_script_structure(project_id=run.project_id, reference=adopted["content"],
        expected_source=source, max_bytes=execution_configuration_max_bytes())


def manual_script_states(run, *, include_identity=False):
    stages = {row.stage_key: row for row in StageRun.objects.filter(production_run_id=run.pk,
        stage_key__in=("script_input", "script_review")).only("id", "stage_key", "mode", "status",
            "output_snapshot", "output_snapshot_schema_version", "output_snapshot_size_bytes", "output_snapshot_sha256").order_by()}
    if set(stages) != {"script_input", "script_review"} or any(row.mode != "manual" for row in stages.values()):
        raise ValidationError("本轮缺少正式人工剧本输入或审核步骤")
    initial, reviewed = (script_step_adoption(stages[key]) for key in ("script_input", "script_review"))
    selected_id = str(run.script_adoption_decision_id) if run.script_adoption_decision_id else None
    selected = [item for item in (initial, reviewed) if item["decision_id"] is not None and item["decision_id"] == selected_id]
    if selected_id is None:
        if initial["decision_id"] or reviewed["decision_id"] or run.script_revision_id:
            _conflict("本轮已有剧本采用记录，但缺少当前采用决定，不能按步骤优先级或时间回填")
        current = None
    elif len(selected) != 1:
        _conflict("本轮当前剧本决定不属于本轮合法步骤的当前采用记录")
    else:
        current = selected[0]["content"]
    if (None if current is None else current["id"]) != (str(run.script_revision_id) if run.script_revision_id else None):
        _conflict("本轮当前剧本与正式采用决策不一致，不能显示为已采用")
    revision = None
    if current is not None:
        revision = ScriptRevision.objects.filter(pk=current["id"], source__project_id=run.project_id,
            source_id=current["source_id"], revision=current["revision"], status="confirmed",
            content_sha256=current["sha256"], content_size_bytes=current["size_bytes"],
            confirmed_by_id=current["confirmed_by_id"], confirmed_at=current["confirmed_at"]).values(
                "source__original_name", "source__source_type", "source__source_format", "origin_generation_attempt_id").first()
    if current is not None and revision is None:
        _conflict("本轮当前剧本缺少对应已确认修订或摘要不一致")
    result = {"script_adoption": initial, "script_review": reviewed, "current_script": current,
        "current_script_decision_id": selected_id}
    if include_identity:
        identity = None
        if revision is not None:
            decision_stage = "script_review" if selected_id == reviewed["decision_id"] else "script_input"
            identity = {"source_name": revision["source__original_name"],
                "source_type_label": ScriptSource.SourceType(revision["source__source_type"]).label,
                "source_format_label": ScriptSource.SourceFormat(revision["source__source_format"]).label,
                "decision_stage_key": decision_stage, "candidate": None}
            if decision_stage == "script_review":
                evidence = stages[decision_stage].output_snapshot["candidate"]
                edited_from = evidence.get("edited_from_revision_id")
                origin = revision["origin_generation_attempt_id"]
                if edited_from is not None:
                    original = ScriptRevision.objects.filter(pk=edited_from, source_id=current["source_id"],
                        content_sha256=evidence.get("artifact_sha256")).values("origin_generation_attempt_id").first()
                    if original is None or not original["origin_generation_attempt_id"] or origin is not None:
                        _conflict("人工编辑修订缺少原候选来源，不能冒充模型返回")
                    origin = original["origin_generation_attempt_id"]
                artifact = GeneratedArtifact.objects.filter(pk=evidence.get("artifact_id"), production_run_id=run.pk,
                    stage_run_id=evidence.get("source_stage_id"), stage_run__stage_key=evidence.get("source_stage_key"),
                    generation_attempt_id=origin,
                    generation_attempt__preparation_id=evidence.get("preparation_id"),
                    sha256=evidence.get("artifact_sha256"), role=evidence.get("candidate_kind")).values(
                        "id", "variant_index", "stage_run_id", "generation_attempt_id", "generation_attempt__preparation_id").first()
                if artifact is None or (edited_from is None and evidence.get("artifact_sha256") != current["sha256"]):
                    _conflict("当前采用决定缺少对应的原剧本候选，不按名称或摘要猜测关联")
                identity["candidate"] = {"artifact_id": str(artifact["id"]), "variant_index": artifact["variant_index"],
                    "edited_from_revision_id": edited_from,
                    "stage_id": str(artifact["stage_run_id"]), "stage_key": evidence["source_stage_key"],
                    "attempt_id": str(artifact["generation_attempt_id"]),
                    "preparation_id": str(artifact["generation_attempt__preparation_id"])}
        result["current_script_identity"] = identity
    return result


@transaction.atomic
def manual_script_structure_check(run):
    """On-demand evidence from the adopted detail, never a new parse or write."""
    run = ProductionRun.objects.select_for_update().only(
        "id", "project_id", "plan_source", "recipe_version_id", "script_revision_id",
        "script_adoption_decision_id").get(pk=run.pk)
    plan = get_run_execution_plan(run)
    if plan.value["source"] != "manual":
        raise ValidationError("此检查仅用于无配方人工制作轮次")
    states = manual_script_states(run)
    reference = states["current_script"]
    result = {"run_id": str(run.pk), "current_script": reference,
        "current_script_decision_id": states["current_script_decision_id"],
        "status": "no_script", "reason_code": "", "reason": "本轮尚未采用已确认剧本。",
        "episode_count": None, "shot_count": None}
    if reference is None:
        return result
    try:
        detail = read_manual_script_structure(run)
    except ValidationError as exc:
        if getattr(exc, "code", None) != "script_structure_required":
            raise
        return {**result, "status": "unstructured", "reason_code": exc.code,
            "reason": "本次采用的修订尚未保存结构化结果；这不代表原文有误，也不要求一定调用模型。"}
    episodes = detail["structure"]["document"]["projection"]["value"]["episodes"]
    return {**result, "status": "structured", "reason": "本次采用的修订已有按冻结字段映射保存的结构化结果，无需重复解析。",
        "episode_count": len(episodes), "shot_count": sum(len(row["shots"]) for row in episodes)}


def approve_manual_script(*, run, stage, plan, request, actor):
    payload = request["payload"]
    if (stage.stage_key != "script_review" or stage.mode != "manual"
            or request["scope"] != {"kind": "project", "id": str(run.project_id)}
            or request["unit_key"] != f"project:{run.project_id}"):
        raise ValidationError("剧本审核必须定位本轮项目的正式人工审核步骤")
    if set(payload) - {"script_content"} != {"text_candidate", "expected_current_script", "comment"}:
        raise ValidationError("请明确提交已阅读候选、当前剧本引用及审核说明；采用剧本不修改项目标题和梗概")
    states = manual_script_states(run)
    if (request["expected_selection_revision"] != states["script_review"]["revision"]
            or payload["expected_current_script"] != states["current_script"]):
        _conflict("本轮当前剧本或审核修订已变化，请重新读取后确认")
    artifact, detail = manual_text_candidate(run=run, stage=stage, plan=plan,
        selection=payload["text_candidate"], unit_key=request["unit_key"])
    require_replaceable_content_dependents(run=run, stage=stage)
    comment = normalize_optional_audit_comment(payload["comment"])
    if "script_content" in payload and not isinstance(payload["script_content"], str):
        raise ValidationError({"script_content": "编辑稿必须是完整JSON文本，不能为空值。"})
    evidence = apply_verified_script_candidate(run=run, artifact=artifact, detail=detail, user=actor,
        script_content=payload.get("script_content"))
    content = confirmed_script_reference(str(run.project_id), {"kind": "script_revision", "id": evidence["script_revision_id"],
        "expected_sha256": evidence["script_content_sha256"]})
    assign_json_contract(stage, "output_snapshot", {"schema_version": 1, "kind": "manual_script_review",
        "content": content, "candidate": evidence}, max_bytes=DATABASE_JSON_FIELD_MAX_BYTES)
    stage.status, stage.reason_code, stage.reason = "succeeded", "", ""
    stage.started_at = stage.started_at or timezone.now()
    stage.finished_at = timezone.now()
    stage.save(update_fields=("output_snapshot", "output_snapshot_schema_version", "output_snapshot_size_bytes",
        "output_snapshot_sha256", "status", "reason_code", "reason", "started_at", "finished_at", "updated_at"))
    decision = ApprovalDecision.objects.create(stage_run=stage, revision=states["script_review"]["revision"] + 1,
        decision="approve", actor="human", decided_by=actor, comment=comment, output_snapshot_sha256=stage.output_snapshot_sha256)
    stage.output_reference = f"approval:{decision.pk}"
    stage.save(update_fields=("output_reference", "updated_at"))
    run.status = "awaiting_input"
    run.script_adoption_decision = decision
    run.save(update_fields=("status", "script_adoption_decision", "updated_at"))
    return ManualActionResult("approval_decision", decision.pk)
