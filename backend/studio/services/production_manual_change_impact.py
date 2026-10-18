"""Read-only upstream-change review over original stages and frozen inputs.

Stage order defines the review range, NOT proof of a dependency. Only registered
provenance is called a recorded reference. No snapshot, task or choice is changed.
"""
from django.core.exceptions import ValidationError
from django.db import transaction
from django.db.models import Count, Exists, F, OuterRef, Q
from django.utils import timezone

from studio.models import (
    ApprovalDecision, ExecutionPreparation, ExecutionPreparationContract,
    GeneratedArtifact, GenerationAttempt, ProductionAssemblyLink, ProductionManualCommand,
    ProductionRun, StageRun,
)
from studio.services.json_contracts import DATABASE_JSON_FIELD_MAX_BYTES, verify_json_contract
from studio.services.execution_instances import execution_resolved_inputs_max_bytes
from studio.services.production_run_plans import get_run_execution_plan
from studio.services.production_stage_catalog import PROJECT_TEXT_REVIEW_SOURCES


# Platform content writers already using the pre-assembly change gate. These
# are not model names, routes, or configurable prompt-generation policies.
MANUAL_CONTENT_CHANGE_STAGES = (
    "script_input", "script_review", "bible_materialize", "bible_review",
    "visual_rules_review", "storyboard", "bible_image_review", "storyboard_review",
)
REREVIEWABLE_CONTENT_STAGES = ("bible_materialize", "bible_review", "storyboard", "bible_image_review", "storyboard_review", *PROJECT_TEXT_REVIEW_SOURCES)


def started_content_dependents(run, stage):
    later = StageRun.objects.filter(production_run=run, ordinal__gt=stage.ordinal)
    return later.annotate(
        has_preparations=Exists(ExecutionPreparation.objects.filter(stage_run_id=OuterRef("pk"))),
        # A selection receipt neither starts this stage nor creates a review or
        # execution. Actual preparations/decisions below still protect inputs.
        has_commands=Exists(ProductionManualCommand.objects.filter(stage_run_id=OuterRef("pk")).exclude(action="select")),
        has_decisions=Exists(ApprovalDecision.objects.filter(stage_run_id=OuterRef("pk"))),
        has_artifacts=Exists(GeneratedArtifact.objects.filter(stage_run_id=OuterRef("pk"))),
        has_attempts=Exists(GenerationAttempt.objects.filter(stage_run_id=OuterRef("pk"))),
    ).exclude(status="pending", has_preparations=False, has_commands=False, has_decisions=False,
        has_artifacts=False, has_attempts=False)


def _verify_review_only_dependents(run, rows):
    """Validate passive content reviews through their existing fact readers.

    These are platform stages with implemented rereview protocols, not model
    rules. Execution results require their own explicit reuse protocol below.
    """
    if rows.exclude(stage_key__in=REREVIEWABLE_CONTENT_STAGES, mode="manual", has_decisions=True,
            has_preparations=False, has_artifacts=False, has_attempts=False).exists():
        return False
    from studio.services.production_manual_bible import manual_bible_review_detail
    from studio.services.production_manual_storyboard import _content, _adoption
    from studio.services.production_manual_storyboard_review import _approval
    from studio.services.production_manual_bible_images import manual_bible_image_review_detail
    from studio.services.production_manual_scripts import manual_script_states
    from studio.services.production_reviewed_visual_rules import visual_rules_review_state

    plan, bible, content = get_run_execution_plan(run), None, None
    for row in rows.only("id", "production_run_id", "stage_key", "mode", "status", "output_snapshot",
            "output_snapshot_schema_version", "output_snapshot_size_bytes", "output_snapshot_sha256"):
        if row.stage_key in PROJECT_TEXT_REVIEW_SOURCES:
            scripts = manual_script_states(run)
            state = scripts["script_review"] if row.stage_key == "script_review" else visual_rules_review_state(
                run, row)
            _verify_text_review_candidate(run, row)
        elif row.stage_key in {"bible_materialize", "bible_review"}:
            if bible is None:
                bible = manual_bible_review_detail(run)
            state = bible["source" if row.stage_key == "bible_materialize" else "approval"]
        elif row.stage_key == "storyboard":
            if content is None:
                content = _content(run, plan)
            state = _adoption(row, content)
        elif row.stage_key == "bible_image_review":
            state = manual_bible_image_review_detail(run)["approval"]
        else:
            state, _ = _approval(row, None, plan)
        if not state["decision_id"] or state["revision"] <= 0:
            raise ValidationError("缺少原人工采用或审核决定")
    return True


def content_replacement_state(*, run, stage):
    """Shared read/write gate. Caller holds the original Run lock.

    Stopped unused preparations and verified passive content reviews may be
    left behind. Readers compare the original review basis; this
    gate never updates an old decision or claims a generated result is current.
    """
    def state(code, reason, allowed=False):
        return {"allowed": allowed, "code": code, "reason": reason}

    if run.status in {"succeeded", "failed", "cancelled"}:
        return state("run_read_only", "本轮已结束；更换内容请新建人工制作轮次。")
    if ProductionAssemblyLink.objects.filter(production_run=run).exists():
        return state("assembly_locked", "本轮已提交装配；更换内容请新建人工制作轮次。")
    later = started_content_dependents(run, stage)
    if not later.exists():
        return state("no_dependents", "尚无已开始的后续步骤，可返回对应采用/审核入口更换内容。", True)
    try:
        review_only = _verify_review_only_dependents(run, later)
    except ValidationError:
        return state("inconsistent_records", "后续人工采用/审核的原决定或依据无法核对，请先检查原记录。")
    if review_only:
        return state("review_only", "后续只有可核对的内容采用/审核；可更换内容，之后必须逐项重新采用/审核。文本候选仍校验原生成依据，依据变化须主动创建新准备，不会自动执行模型。", True)
    # Partition only passive original decisions. A reviewed stage that also
    # owns a preparation/result stays in the execution subset, never disappears
    # from the stopped-unused checks merely because it has a supported key.
    passive = Q(stage_key__in=REREVIEWABLE_CONTENT_STAGES, mode="manual", has_decisions=True,
        has_preparations=False, has_artifacts=False, has_attempts=False)
    reviews = later.filter(passive)
    try:
        if reviews.exists():
            _verify_review_only_dependents(run, reviews)
        execution = later.exclude(passive)
        media_keys = ("media_generation", "bible_image_generation", "artifact_selection", "bible_image_review")
        text_keys = tuple({key for keys in PROJECT_TEXT_REVIEW_SOURCES.values() for key in keys})
        text_results = Q(stage_key__in=text_keys, has_artifacts=True)
        media_results = Q(stage_key__in=media_keys)
        unused = execution.exclude(text_results | media_results)
        if unused.exists():
            code, reason, allowed = _unused_replacement_state(run, unused)
            if not allowed:
                return state(code, reason)
        text = execution.filter(text_results)
        if text.exists():
            failure = _text_replacement_failure(run, text)
            if failure:
                return state(*failure)
        media = execution.filter(media_results)
        if media.exists():
            result = _media_replacement_state(run, media)
            if result is None:
                raise ValidationError("媒体审核缺少原制作步骤")
            code, reason, allowed = result
            if not allowed:
                return state(code, reason)
        if text.exists():
            return state("text_review_required", "后续文本及其他生成调用均已确认结束，可更换上游。旧文本候选保留原生成依据，不自动变成新内容的有效结果；依据变化时需在原生成步骤主动创建新准备并审核新候选。其他内容/媒体也需逐项复核，旧调用和费用不变，不会自动重跑。", True)
        if media.exists():
            return state(*result)
    except ValidationError:
        return state("inconsistent_records", "后续文本、媒体或人工审核的原记录无法核对，请先检查原记录。")
    if reviews.exists():
        code, reason, allowed = _unused_replacement_state(run, execution)
        if not allowed:
            return state(code, reason)
        return state("review_and_stopped", "后续已有人工采用/审核，其余生成准备均已明确停止且未产生可用结果。可更换内容，随后逐项重新采用/审核，并主动创建新准备；旧记录不改写，不会自动续跑。", True)
    return state(*_unused_replacement_state(run, later))


def _verify_text_review_candidate(run, stage):
    """Original approved candidate remains evidence, not a new source baseline."""
    from studio.services.execution_production_candidates import read_production_text_candidate
    evidence = stage.output_snapshot.get("candidate") if stage.stage_key == "script_review" else stage.output_snapshot
    if not isinstance(evidence, dict):
        raise ValidationError("文本审核缺少原候选证据")
    artifact = GeneratedArtifact.objects.filter(pk=evidence.get("artifact_id"), production_run=run,
        stage_run__stage_key__in=PROJECT_TEXT_REVIEW_SOURCES[stage.stage_key], status="selected").first()
    detail = read_production_text_candidate(artifact) if artifact else None
    if (not detail or evidence.get("artifact_sha256") != artifact.sha256
            or evidence.get("preparation_id") != detail["preparation_id"]
            or evidence.get("result_sha256") != detail["result_sha256"]):
        raise ValidationError("文本审核与原生成候选不一致")


def _text_replacement_failure(run, rows):
    """Closed original text results may stay while the user creates new work.

    Do not relax either script/film-rule adoption's frozen-source checks. A
    changed source requires an explicit new preparation, not a rebound result.
    """
    from studio.services.execution_production_candidates import read_production_text_candidate
    from studio.services.production_stage_catalog import STAGE_EXECUTION_OPERATIONS
    plan = get_run_execution_plan(run)
    catalog = {item["key"]: item["ordinal"] for item in plan.value["stage_catalog"]["value"]["stages"]}
    for row in rows.values("stage_key", "ordinal", "mode", "has_preparations", "has_decisions"):
        if (row["mode"] != "manual" or catalog.get(row["stage_key"]) != row["ordinal"]
                or not row["has_preparations"] or row["has_decisions"]):
            raise ValidationError("文本生成步骤与冻结计划或原准备不一致")
    preparations = ExecutionPreparation.objects.filter(stage_run__in=rows)
    failure = _settled_result_preparations(rows, preparations)
    if failure:
        return (*failure, False)
    failure = _preparation_command_failure(run, rows, preparations)
    if failure:
        return (*failure, False)
    # Do not join the Run/Stage's large execution snapshots into every result.
    # The original candidate reader verifies each stored result on demand;
    # no model request, file probe or old input re-resolution belongs here.
    artifacts = GeneratedArtifact.objects.filter(stage_run__in=rows).select_related("stage_run", "production_run").only(
        "id", "production_run_id", "stage_run_id", "generation_attempt_id", "status", "storage_backend", "kind", "role",
        "manifest_metadata", "manifest_schema_version", "manifest_metadata_size_bytes", "manifest_metadata_sha256",
        "sha256", "size_bytes", "object_key", "stage_run__stage_key", "production_run__project_id")
    for artifact in artifacts.iterator(chunk_size=100):
        detail = read_production_text_candidate(artifact)
        if (artifact.production_run_id != run.pk or not detail
                or detail["operation"] != STAGE_EXECUTION_OPERATIONS[artifact.stage_run.stage_key]):
            raise ValidationError("文本候选不是原步骤的完整业务结果")
    if GenerationAttempt.objects.filter(stage_run__in=rows, role="primary", status="succeeded").exclude(
            pk__in=artifacts.values("generation_attempt_id")).exists():
        raise ValidationError("文本主调用成功但缺少原候选产物")
    return None


def _unused_replacement_state(run, later):
    """Original stop/receipt/terminal protocol, also used beside passive reviews."""
    if later.filter(Q(has_decisions=True) | Q(has_artifacts=True) | Q(status="succeeded")).exists():
        return ("dependent_results", "后续还包含尚未接通重新审核/重做的结果类型，本次不能更换；请查看具体后续步骤。", False)
    if later.filter(has_preparations=False).exists():
        return ("inconsistent_records", "后续步骤已有执行记录，但缺少可核对的原执行准备，请查看原记录。", False)
    preparations = ExecutionPreparation.objects.filter(stage_run__in=later)
    if preparations.exclude(status="cancelled", cancelled_at__isnull=False,
            next_check_at__isnull=True, claim_token__isnull=True, claim_until__isnull=True).exists():
        return ("active_preparations", "请先逐项停止不再使用的后续主准备及辅助任务，再重新查看改选影响；不会自动取消。", False)
    failure = _preparation_command_failure(run, later, preparations)
    if failure:
        return (*failure, False)
    failure = _closed_call_failure(later, preparations, allow_primary_success=False)
    if failure:
        return (*failure, False)
    return ("stopped_unused", "后续准备均已明确停止，未产生或采用下游产物，可以更换内容。旧准备与费用保留；更换后需主动创建新准备，不会自动重跑。", True)


def _preparation_command_failure(run, stages, preparations):
    # Receipt identity is verified in batches; no request/response JSON or N+1
    # preparation reads. Auxiliary preparations need no separate manual receipt.
    prep_stages = dict(preparations.values_list("id", "stage_run_id"))
    commands = ProductionManualCommand.objects.filter(stage_run__in=stages).only(
        "run_id", "stage_run_id", "action", "result_kind", "target_id", "result_refs",
        "result_refs_schema_version", "result_refs_size_bytes", "result_refs_sha256")
    for receipt in commands.iterator(chunk_size=200):
        if (receipt.run_id != run.pk or receipt.action != "generate" or receipt.result_kind != "preparation"
                or prep_stages.get(receipt.target_id) != receipt.stage_run_id):
            return ("inconsistent_records", "后续生成动作与原执行准备归属不一致，请核对原动作与结果。")
        try:
            refs = verify_json_contract(receipt.result_refs, schema_version=receipt.result_refs_schema_version,
                size_bytes=receipt.result_refs_size_bytes, sha256=receipt.result_refs_sha256,
                max_bytes=DATABASE_JSON_FIELD_MAX_BYTES)
            if refs.schema_version != 1 or refs.value != {"schema_version": 1,
                    "target": {"kind": "preparation", "id": str(receipt.target_id)}}:
                raise ValidationError("动作引用不一致")
        except ValidationError:
            return ("inconsistent_records", "后续动作的原结果引用无法核对，不能据此允许更换。")
    return None


def _closed_call_failure(stages, preparations, *, allow_primary_success):
    """One terminal/ownership check for unused and explicitly reviewed results."""
    attempts = GenerationAttempt.objects.filter(Q(stage_run__in=stages)
        | Q(preparation__in=preparations) | Q(parent_preparation__in=preparations))
    valid_identity = (Q(stage_run__in=stages, preparation__in=preparations,
        stage_run_id=F("preparation__stage_run_id")) & (
        Q(role="primary", parent_preparation__isnull=True)
        | Q(role="auxiliary", parent_preparation__in=preparations,
            stage_run_id=F("parent_preparation__stage_run_id"))))
    if attempts.exclude(valid_identity).exists():
        return ("inconsistent_records", "后续调用与原主/辅助准备归属不一致，请核对原记录。")
    if not allow_primary_success and attempts.filter(role="primary", status="succeeded").exists():
        return ("dependent_results", "后续主调用已经成功；需要通过重新审核或新准备重做，不能按未使用处理。")
    ended = Q(status__in=("failed", "cancelled")) | Q(role="auxiliary", status="succeeded")
    if allow_primary_success:
        ended |= Q(role="primary", status="succeeded")
    if (attempts.exclude(ended).exists() or attempts.filter(Q(error_code="uncertain_remote_response")
            | Q(finished_at__isnull=True) | Q(claim_token__isnull=False) | Q(next_poll_at__isnull=False)).exists()):
        return ("unresolved_calls", "后续调用仍在执行、取消中或结果尚未确认；停止准备不等于远端结束，请查看原任务。")
    tasks = attempts.filter(generation_task__isnull=False)
    if (tasks.exclude(generation_task__status=F("status"), generation_task__finished_at__isnull=False).exists()
            or tasks.exclude(generation_task__submission_token="").exclude(
                Q(generation_task__submission_state__in=("terminal_succeeded", "terminal_failed", "terminal_cancelled"))
                | Q(error_code="execution_submission_rejected")).exists()):
        return ("unresolved_calls", "原工作流任务尚未确认结束或与调用记录不一致，请查看原任务。")
    return None


def _media_replacement_state(run, rows):
    """Permit explicit content readoption before assembly, never auto-reuse.

    Other completed producers remain closed until their own review protocols
    are implemented. Existing media is covered by the original whole-selection
    review, whose content basis changes without rewriting generation inputs.
    """
    producer_keys = {"media_generation", "bible_image_generation"}
    review_keys = {"artifact_selection", "bible_image_review"}
    media_keys = producer_keys | review_keys
    media = rows.filter(stage_key__in=media_keys)
    if not media.filter(stage_key__in=producer_keys).exists():
        return None
    plan = get_run_execution_plan(run)
    catalog = {item["key"]: item["ordinal"] for item in plan.value["stage_catalog"]["value"]["stages"]}
    for row in media.values("stage_key", "ordinal", "mode", "has_preparations", "has_attempts", "has_artifacts"):
        if row["mode"] != "manual" or catalog.get(row["stage_key"]) != row["ordinal"]:
            raise ValidationError("媒体阶段与冻结人工计划不一致")
        if row["stage_key"] in review_keys and any(row[key] for key in ("has_preparations", "has_attempts", "has_artifacts")):
            raise ValidationError("人工参考图/选片审核不能承担生成职责")
        if row["stage_key"] in producer_keys and not (row["has_preparations"] or row["has_artifacts"]):
            raise ValidationError("媒体步骤已开始但缺少原准备或已采用素材，不能按没有在途工作处理")
    preparations = ExecutionPreparation.objects.filter(stage_run__in=media)
    failure = _settled_result_preparations(media, preparations)
    if failure:
        return (*failure, False)
    from studio.services.production_manual_media_review import manual_media_review_detail
    from studio.services.production_manual_bible_images import manual_bible_image_review_detail
    # Original readers check selections/candidates and current review context.
    # The media inventory is metadata-only; Bible candidates retain their
    # original integrity checks. Neither re-resolves old preparation inputs.
    if media.filter(stage_key__in=("media_generation", "artifact_selection")).exists():
        manual_media_review_detail(run)
    if media.filter(stage_key__in=("bible_image_generation", "bible_image_review")).exists():
        manual_bible_image_review_detail(run)
    return ("media_review_required", "后续媒体调用均已结束，可以更换上游内容。旧生成记录不变；需重新采用/审核上游，并在参考图/选片审核中明确确认复用，或主动创建新候选；不会自动重跑。", True)


def _settled_result_preparations(stages, preparations):
    failure = _closed_call_failure(stages, preparations, allow_primary_success=True)
    if failure:
        return failure
    completed = Exists(GenerationAttempt.objects.filter(preparation_id=OuterRef("pk"), status="succeeded"))
    settled = Q(status="cancelled", cancelled_at__isnull=False) | Q(status="dispatched", has_success=True)
    if preparations.annotate(has_success=completed).exclude(settled,
            next_check_at__isnull=True, claim_token__isnull=True, claim_until__isnull=True).exists():
        return ("active_preparations", "仍有可继续执行的准备，请先明确停止不再使用的准备。已成功结果无需取消，但更换内容后必须重新审核或新建候选。")
    return None


def require_replaceable_content_dependents(*, run, stage):
    state = content_replacement_state(run=run, stage=stage)
    if not state["allowed"]:
        raise ValidationError(f"请查看“改选影响”：{state['reason']}", code="manual_action_not_ready")


def _object(value):
    if not isinstance(value, dict):
        raise ValidationError("来源记录不是完整对象")
    return value


def recorded_input_references(facts, *, source_stage_id, script_revision_id):
    """Read only known protocol positions, never search arbitrary JSON/text."""
    inputs = _object(_object(facts).get("inputs"))
    origins = inputs.get("input_provenance")
    if not isinstance(origins, list):
        raise ValidationError("缺少输入来源清单，不能推断影响")
    found = set()

    def add(path, kind):
        found.add((path, kind))

    def origin(value, path):
        value = _object(value)
        kind = value.get("kind")
        if kind == "structured":
            leaves = value.get("leaves")
            if not isinstance(leaves, list):
                raise ValidationError("结构输入缺少来源明细")
            for leaf in leaves:
                leaf = _object(leaf)
                if not isinstance(leaf.get("path"), str):
                    raise ValidationError("结构来源缺少字段位置")
                origin(leaf, leaf["path"])
        elif kind == "context_field":
            if not isinstance(value.get("selector_key"), str) or not value.get("content_revision_id"):
                raise ValidationError("上下文来源缺少目录字段或修订")
            if script_revision_id and value["content_revision_id"] == script_revision_id:
                add(path, "script_input")
            review = value.get("review")
            if review is not None and _object(review).get("stage_run_id") == source_stage_id:
                add(path, "reviewed_content")
            for related in _object(value.get("related_reviews", {})).values():
                if _object(related).get("stage_run_id") == source_stage_id:
                    add(path, "related_review")
        elif kind == "selected_output":
            if not value.get("stage_run_id") or not value.get("decision_id"):
                raise ValidationError("采用产物来源缺少原步骤或决定")
            if value["stage_run_id"] == source_stage_id or value.get("approval_stage_id") == source_stage_id:
                add(path, "selected_output")
            if value.get("context") is not None:
                origin(value["context"], path)
        elif kind not in {"literal", "asset", "item_field", "unit_key", "reference_subjects"}:
            raise ValidationError("来源类型未登记，不能推断为无影响")

    nonmedia_keys = set()
    for item in origins:
        item = _object(item)
        if not isinstance(item.get("input_key"), str):
            raise ValidationError("输入来源缺少字段标识")
        nonmedia_keys.add(item["input_key"])
        origin(item, item["input_key"])
    # The source writer puts every non-media input in input_provenance. Media
    # items have their own origin. Never inspect inside literal/structured data.
    for key, values in _object(inputs.get("logical_values")).items():
        if key in nonmedia_keys:
            continue
        if not isinstance(values, list):
            raise ValidationError("输入值缺少对应来源记录")
        for value in values:
            value = _object(value)
            if not {"origin", "source_sha256", "item_key", "input_key"} <= value.keys() or value["input_key"] != key:
                raise ValidationError("媒体条目缺少对应来源记录")
            origin(value["origin"], f'{key}/{value["item_key"]}')
    baseline = facts.get("script_adoption_baseline")
    if baseline is not None and script_revision_id and _object(baseline).get("script_revision_id") == script_revision_id:
        add("script_adoption_baseline", "adoption_baseline")
    return [{"input_path": path, "kind": kind} for path, kind in sorted(found)]


def _references(preparation, detail, source):
    if detail is None:
        raise ValidationError("准备缺少冻结详情")
    intent = verify_json_contract(detail.intent, schema_version=detail.intent_schema_version,
        size_bytes=detail.intent_size_bytes, sha256=detail.intent_sha256,
        max_bytes=execution_resolved_inputs_max_bytes())
    resolved = verify_json_contract(detail.resolved_inputs, schema_version=detail.resolved_inputs_schema_version,
        size_bytes=detail.resolved_inputs_size_bytes, sha256=detail.resolved_inputs_sha256,
        max_bytes=execution_resolved_inputs_max_bytes())
    if (intent.sha256 != preparation.intent_sha256 or _object(intent.value).get("resolved_inputs_sha256") != resolved.sha256
            or intent.schema_version != 1 or resolved.schema_version != 1):
        raise ValidationError("准备意图与冻结输入摘要不一致")
    return recorded_input_references(resolved.value, source_stage_id=source["stage_run_id"],
        script_revision_id=source["script_revision_id"])


@transaction.atomic
def manual_content_change_impact(run, *, stage_key, offset=0, limit=20):
    run = ProductionRun.objects.select_for_update().only("id", "plan_source", "recipe_version_id", "status").get(pk=run.pk)
    if run.plan_source != "manual" or run.recipe_version_id is not None:
        raise ValidationError("改选影响入口仅用于无配方人工制作")
    if stage_key not in MANUAL_CONTENT_CHANGE_STAGES or type(offset) is not int or offset < 0 or type(limit) is not int or not 1 <= limit <= 50:
        raise ValidationError("请选择已登记的内容步骤及有效分页范围")
    plan = get_run_execution_plan(run)
    fields = ["id", "stage_key", "ordinal", "mode"]
    if stage_key in {"script_input", "script_review"}:
        fields.extend(["output_snapshot", "output_snapshot_schema_version", "output_snapshot_size_bytes", "output_snapshot_sha256"])
    stage = StageRun.objects.filter(production_run=run, stage_key=stage_key).only(*fields).first()
    if stage is None:
        raise ValidationError("运行缺少正式内容步骤")
    expected = next((row for row in plan.value["stage_catalog"]["value"]["stages"] if row["key"] == stage_key), None)
    if expected is None or stage.ordinal != expected["ordinal"] or stage.mode != "manual":
        raise ValidationError("内容步骤与冻结计划不一致")
    approval = ApprovalDecision.objects.filter(stage_run=stage).order_by("-revision").values("id", "revision", "output_snapshot_sha256").first()
    script_id = None
    if approval and stage_key in {"script_input", "script_review"}:
        output = verify_json_contract(stage.output_snapshot, schema_version=stage.output_snapshot_schema_version,
            size_bytes=stage.output_snapshot_size_bytes, sha256=stage.output_snapshot_sha256,
            max_bytes=DATABASE_JSON_FIELD_MAX_BYTES)
        if approval["output_snapshot_sha256"] != output.sha256:
            raise ValidationError("当前采用决定与内容快照不一致")
        script_id = _object(_object(output.value).get("content")).get("id")
        if not script_id:
            raise ValidationError("已采用剧本缺少原修订引用")
    source = {"stage_run_id": str(stage.pk), "stage_key": stage_key,
        "decision_id": str(approval["id"]) if approval else None,
        "revision": approval["revision"] if approval else 0, "script_revision_id": script_id}
    stages = list(started_content_dependents(run, stage).order_by("ordinal", "id").values("id", "stage_key", "status"))
    preparations = ExecutionPreparation.objects.filter(stage_run_id__in=[row["id"] for row in stages])
    counts = dict(preparations.order_by().values("stage_run_id").annotate(total=Count("id")).values_list("stage_run_id", "total"))
    for row in stages:
        row["preparation_count"] = counts.get(row["id"], 0)
        row["id"] = str(row["id"])
    count = preparations.count()
    page = list(preparations.order_by("stage_run__ordinal", "created_at", "id").only(
        "id", "stage_run_id", "unit_key", "status", "revision", "intent_sha256", "created_at" )[offset:offset + limit])
    details = {row.preparation_id: row for row in ExecutionPreparationContract.objects.filter(preparation__in=page).only(
        "preparation_id", "intent", "intent_schema_version", "intent_sha256", "intent_size_bytes",
        "resolved_inputs", "resolved_inputs_schema_version", "resolved_inputs_sha256", "resolved_inputs_size_bytes")}
    attempts = {}
    for row in GenerationAttempt.objects.filter(preparation__in=page).order_by().values("preparation_id", "role", "status").annotate(count=Count("id")):
        attempts.setdefault(row.pop("preparation_id"), []).append(row)
    stage_keys = {row["id"]: row["stage_key"] for row in stages}
    results = []
    for prep in page:
        try:
            refs = _references(prep, details.get(prep.pk), source)
            state = "recorded_reference" if refs else "manual_review_required"
        except ValidationError:
            refs, state = [], "unverifiable"
        results.append({"id": str(prep.pk), "stage_run_id": str(prep.stage_run_id), "stage_key": stage_keys[str(prep.stage_run_id)],
            "unit_key": prep.unit_key, "status": prep.status, "revision": prep.revision,
            "reference_state": state, "references": refs, "attempts": sorted(attempts.get(prep.pk, []), key=lambda row: (row["role"], row["status"]))})
    return {"run_id": str(run.pk), "run_plan_sha256": plan.sha256, "checked_at": timezone.now(),
        "source": source, "assembly_locked": ProductionAssemblyLink.objects.filter(production_run=run).exists(),
        "run_read_only": run.status in {"succeeded", "failed", "cancelled"},
        "replacement": content_replacement_state(run=run, stage=stage), "stages": stages,
        "count": count, "offset": offset, "limit": limit,
        "next_offset": offset + limit if offset + limit < count else None, "results": results}
