"""Atomic manual-action receipts around original tasks and human decisions.

The callback is a trusted in-process stage adapter, never an HTTP field or
plugin hook. It may persist original tasks/decisions, not perform remote work.
Action-specific prerequisites and payload validation belong to that adapter.
"""
from copy import deepcopy
from dataclasses import dataclass
from uuid import UUID

from django.core.exceptions import PermissionDenied, ValidationError
from django.db import transaction
from django.utils import timezone

from studio.models import (
    ApprovalDecision, ArtifactSelectionDecision, ExecutionPreparation, GeneratedArtifact,
    ProductionAssemblyLink, ProductionManualCommand, ProductionRun, ReleaseBundle, Shot, StageRun,
)
from studio.services.json_contracts import DATABASE_JSON_FIELD_MAX_BYTES, assign_json_contract, json_contract, verify_json_contract
from studio.services.production_run_plans import get_run_execution_plan
from studio.services.production_execution_units import PRODUCTION_SCOPE_KINDS, resolve_production_unit
from studio.services.registry_reference_lock import registry_reference_writer


@dataclass(frozen=True)
class ManualActionResult:
    kind: str
    target_id: UUID


def record_manual_approval(run, stage, *, value, revision, comment, actor):
    """Persist an already-validated content action in the command transaction."""
    if (not transaction.get_connection().in_atomic_block or stage.production_run_id != run.pk
            or run.plan_source != "manual" or stage.mode != "manual"):
        raise ValidationError("人工内容决策必须在本轮正式命令事务内写入")
    assign_json_contract(stage, "output_snapshot", value, schema_version=value["schema_version"], max_bytes=DATABASE_JSON_FIELD_MAX_BYTES)
    stage.status, stage.reason_code, stage.reason = "succeeded", "", ""
    stage.started_at = stage.started_at or timezone.now()
    stage.finished_at = timezone.now()
    stage.save(update_fields=("output_snapshot", "output_snapshot_schema_version", "output_snapshot_size_bytes",
        "output_snapshot_sha256", "status", "reason_code", "reason", "started_at", "finished_at", "updated_at"))
    decision = ApprovalDecision.objects.create(stage_run=stage, revision=revision + 1, decision="approve", actor="human",
        decided_by=actor, comment=comment, output_snapshot_sha256=stage.output_snapshot_sha256)
    stage.output_reference = f"approval:{decision.pk}"
    stage.save(update_fields=("output_reference", "updated_at"))
    run.status = "awaiting_input"
    run.save(update_fields=("status", "updated_at"))
    return ManualActionResult("approval_decision", decision.pk)


def _invalid(message, *, conflict=False):
    raise ValidationError(message, code="run_plan_source_conflict" if conflict else "manual_action_not_ready")


def _request(request):
    keys = {"action", "stage_key", "unit_key", "scope", "expected_run_plan_sha256", "expected_selection_revision", "idempotency_key", "payload"}
    if not isinstance(request, dict) or set(request) != keys:
        _invalid("人工动作必须明确提供步骤、范围、修订、幂等键和本次输入")
    if request["action"] not in ProductionManualCommand.Action.values:
        _invalid("人工动作未注册")
    for key, maximum in (("stage_key", 100), ("unit_key", 191), ("idempotency_key", 191)):
        if not isinstance(request[key], str) or not request[key].strip() or len(request[key]) > maximum:
            _invalid(f"{key} 必须是明确且长度有效的标识")
    expected = request["expected_run_plan_sha256"]
    if not isinstance(expected, str) or len(expected) != 64 or any(c not in "0123456789abcdef" for c in expected):
        _invalid("必须提供本次确认的执行计划摘要")
    revision = request["expected_selection_revision"]
    if revision is not None and (type(revision) is not int or revision < 0):
        _invalid("选择修订必须为空或明确的非负整数")
    if not isinstance(request["payload"], dict):
        _invalid("动作输入必须是明确对象")
    scope = request["scope"]
    if not isinstance(scope, dict) or set(scope) != {"kind", "id"} or scope["kind"] not in PRODUCTION_SCOPE_KINDS:
        _invalid("人工制作动作必须选择真实且已登记的制作对象")
    try:
        if not isinstance(scope["id"], str):
            raise ValueError
        UUID(scope["id"])
    except ValueError:
        _invalid("动作范围必须引用正式对象 UUID")
    return json_contract(deepcopy(request), max_bytes=DATABASE_JSON_FIELD_MAX_BYTES)




def _verify_result(result, *, run_id, stage_id, action):
    if not isinstance(result, ManualActionResult) or not isinstance(result.target_id, UUID):
        _invalid("动作适配器必须返回真实任务或决策引用，不能返回完成文案或模型输出")
    allowed = {"generate": {"preparation"}, "retry": {"preparation"}, "adopt": {"artifact", "approval_decision", "artifact_selection"},
        "approve": {"approval_decision", "artifact_selection"}, "assemble": {"assembly_link"}, "deliver": {"release_bundle"},
        "select": {"shot_reference_choices", "artifact_selection"}}
    if result.kind not in allowed.get(action, set()):
        _invalid("动作与其返回的正式结果职责不一致")
    # Results are original objects. The receipt never invents or mirrors their
    # queued/running/succeeded state. Assembly returns the existing Run link.
    targets = {
        "shot_reference_choices": (Shot, {"episode__project__production_runs__id": run_id}),
        "preparation": (ExecutionPreparation, {"stage_run_id": stage_id, "stage_run__production_run_id": run_id}),
        "approval_decision": (ApprovalDecision, {"stage_run_id": stage_id, "stage_run__production_run_id": run_id}),
        "artifact_selection": (ArtifactSelectionDecision, {"stage_run__production_run_id": run_id}),
        "artifact": (GeneratedArtifact, {"production_run_id": run_id}),
        "assembly_link": (ProductionAssemblyLink, {"production_run_id": run_id}),
        "release_bundle": (ReleaseBundle, {"production_run_id": run_id}),
    }
    if result.kind not in targets:
        _invalid("动作返回的正式结果类型未注册")
    model, filters = targets[result.kind]
    if action == "select" and result.kind == "artifact_selection":
        filters = {**filters, "stage_run_id": stage_id}
    if result.kind == "shot_reference_choices":
        run = ProductionRun.objects.get(pk=run_id)
        mode = {"manual": "manual", "recipe": "review"}.get(run.plan_source)
        if not mode or not StageRun.objects.filter(pk=stage_id, production_run_id=run_id,
                stage_key="storyboard_review", mode=mode).exists():
            _invalid("选材回执不是原运行的分镜参考步骤")
        resolve_production_unit(run, {"kind": "shot", "id": str(result.target_id)})
    if not model.objects.filter(pk=result.target_id, **filters).exists():
        _invalid("动作结果不存在或不属于此运行/步骤，不能记录成功回执")


def read_manual_command_result(receipt):
    refs = verify_json_contract(receipt.result_refs, schema_version=receipt.result_refs_schema_version,
        size_bytes=receipt.result_refs_size_bytes, sha256=receipt.result_refs_sha256, max_bytes=DATABASE_JSON_FIELD_MAX_BYTES)
    expected = {"schema_version": 1, "target": {"kind": receipt.result_kind, "id": str(receipt.target_id)}}
    if refs.schema_version != 1 or refs.value != expected:
        _invalid("动作回执与正式结果引用不一致")
    _verify_result(ManualActionResult(receipt.result_kind, receipt.target_id),
        run_id=receipt.run_id, stage_id=receipt.stage_run_id, action=receipt.action)
    if receipt.result_kind == "release_bundle":
        from studio.services.release_bundles import live_verified_release_bundle, ReleaseBundleError
        try:
            live_verified_release_bundle(ReleaseBundle.objects.get(pk=receipt.target_id))
        except ReleaseBundleError as exc:
            raise ValidationError(str(exc), code="manual_action_not_ready") from exc
    return refs.value["target"]


def replay_manual_command(*, run, request_key, request_sha256, actor):
    """Read an exact receipt under the caller's Run lock, after authorization."""
    if not transaction.get_connection().in_atomic_block:
        _invalid("动作回执必须在运行锁事务内读取")
    previous = ProductionManualCommand.objects.filter(run_id=run.pk, idempotency_key=request_key).first()
    if previous is not None:
        if previous.request_sha256 != request_sha256 or previous.created_by_id != actor.pk:
            _invalid("该幂等键已用于不同人工动作或操作者，请重新读取或使用新请求键", conflict=True)
        read_manual_command_result(previous)
    return previous


def store_manual_command_result(*, run, stage, action, request_key, request_sha256, actor, result):
    """Persist only a reference to the original result in its writer transaction."""
    if not transaction.get_connection().in_atomic_block or stage.production_run_id != run.pk:
        _invalid("动作回执必须与原运行步骤在同一事务内写入")
    _verify_result(result, run_id=run.pk, stage_id=stage.pk, action=action)
    receipt = ProductionManualCommand(run=run, stage_run=stage, action=action,
        idempotency_key=request_key, request_sha256=request_sha256, created_by=actor,
        result_kind=result.kind, target_id=result.target_id)
    assign_json_contract(receipt, "result_refs", {"schema_version": 1, "target": {"kind": result.kind, "id": str(result.target_id)}},
        max_bytes=DATABASE_JSON_FIELD_MAX_BYTES)
    receipt.full_clean()
    receipt.save()
    return receipt


@registry_reference_writer
@transaction.atomic
def execute_manual_command(*, run_id, request, actor, execute):
    """Run lock -> idempotency -> plan/scope -> stage -> original operation.

    The API dispatches only to registered adapters. This transaction boundary
    deliberately doesn't own a dispatcher, scheduler or second task queue.
    """
    requested = _request(request)
    if not actor or not actor.is_authenticated or not actor.is_active:
        raise PermissionDenied("需要有效登录身份")
    run = ProductionRun.objects.select_for_update().only(
        "id", "owner_id", "project_id", "plan_source", "recipe_version_id", "status",
    ).get(pk=run_id)
    if run.owner_id != actor.pk and not actor.is_staff:
        raise PermissionDenied("无权操作此制作运行")
    manual = run.plan_source == "manual" and run.recipe_version_id is None
    recipe_selection = (run.plan_source == "recipe" and run.recipe_version_id is not None
        and request["stage_key"] == "storyboard_review" and request["action"] == "select")
    if not manual and not recipe_selection:
        _invalid("配方仅可复用本入口的逐镜选材命令；其他动作仍使用原生产控制入口")
    previous = replay_manual_command(run=run, request_key=requested.value["idempotency_key"],
        request_sha256=requested.sha256, actor=actor)
    if previous is not None:
        return previous, False
    plan = get_run_execution_plan(run)
    if plan.sha256 != request["expected_run_plan_sha256"]:
        _invalid("执行计划与本次确认不一致，请重新读取", conflict=True)
    if run.status in ("succeeded", "cancelled") or (run.status == "failed" and request["action"] != "retry"):
        _invalid("此运行已结束；开始新一轮制作，不修改原结果")
    stage_contract = next((row for row in plan.value["stage_catalog"]["value"]["stages"] if row["key"] == request["stage_key"]), None)
    if stage_contract is None or request["action"] not in stage_contract["manual_actions"]:
        _invalid("此步骤不支持该人工动作")
    resolve_production_unit(run, request["scope"])
    if request["action"] in ("generate", "retry", "adopt", "approve", "select") and ProductionAssemblyLink.objects.filter(production_run_id=run.pk).exists():
        _invalid("已提交装配并冻结选片；更换内容请新建人工制作轮次")
    try:
        stage = StageRun.objects.select_for_update().get(production_run_id=run.pk, stage_key=request["stage_key"])
    except StageRun.DoesNotExist:
        _invalid("此运行缺少正式阶段，不能临时补造阶段或隐藏配方")
    result = execute(run=run, stage=stage, plan=plan, request=deepcopy(requested.value), actor=actor)
    receipt = store_manual_command_result(run=run, stage=stage, action=request["action"],
        request_key=request["idempotency_key"], request_sha256=requested.sha256, actor=actor, result=result)
    return receipt, True
