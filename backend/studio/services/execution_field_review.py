"""Explicit RunPlan field approval, separate from stage approval and dispatch.

Manual roots never acquire this authority from an execution instance. The
registered policy validates and selects an unambiguous value; it neither
generates/rephrases text nor attests to a model's semantic quality.
"""
from copy import deepcopy
from hashlib import sha256
from inspect import unwrap
import marshal
from types import CodeType

from django.core.exceptions import ValidationError
from django.db.models import CharField, Count, Min, Q
from django.db.models.functions import Cast
from django.utils import timezone

from studio.models import ExecutionFieldSelection, GenerationAttempt
from studio.services.execution_preparation_fields import (
    _editable, _field_contexts, _touch, _validate_text, _verify_value,
    locked_execution_field_selection_snapshot,
)
from studio.services.json_contracts import DATABASE_JSON_FIELD_MAX_BYTES, json_contract
from studio.services.execution_instances import execution_configuration_max_bytes
from studio.services.security_boundaries import security_boundary_resolution_cache


def _invalid(message, code="field_review_stale"):
    raise ValidationError(message, code=code)


def field_review_policy_options():
    """Server-owned identities for the existing recipe editor, not user code."""
    return [{**deepcopy(_POLICY_IDENTITY), "label": "自动采用通过校验的字段",
        "description": "仅供明确授权的无人值守配方；按规则标识及业务版本核对来源和通用字段合同。实现摘要仅记录配置冻结时的代码，不作兼容门槛，不代表文案质量审核。"}]


def validate_field_review_choice(choice, *, interaction_mode, current=True):
    json_contract(choice, max_bytes=DATABASE_JSON_FIELD_MAX_BYTES)
    if choice == {"mode": "manual"}:
        return deepcopy(choice)
    if (not isinstance(choice, dict) or set(choice) != {"mode", "policy"} or choice["mode"] != "policy"
            or interaction_mode != "unattended"):
        _invalid("只有无人值守配方可以明确选择字段批准策略")
    policy = choice["policy"]
    if (not isinstance(policy, dict) or set(policy) != {"key", "version", "implementation_sha256"}
            or not isinstance(policy["key"], str) or not policy["key"]
            or type(policy["version"]) is not int or policy["version"] < 1
            or not isinstance(policy["implementation_sha256"], str) or len(policy["implementation_sha256"]) != 64
            or any(c not in "0123456789abcdef" for c in policy["implementation_sha256"])):
        _invalid("字段批准策略必须冻结精确版本及实现摘要")
    # Compatibility belongs to the declared business policy, not Python
    # bytecode (including shared validators). The frozen implementation hash
    # remains configuration-time provenance; it is not the code used now.
    if current and (policy["key"], policy["version"]) != (_POLICY_IDENTITY["key"], _POLICY_IDENTITY["version"]):
        _invalid("字段批准规则标识或业务版本未注册，请选择受支持的规则版本")
    return deepcopy(choice)


def verify_field_review_contract(review):
    """Storage validation does not substitute today's policy into history."""
    if review == {"schema_version": 1, "mode": "manual"} and type(review["schema_version"]) is int:
        return
    if (not isinstance(review, dict) or set(review) != {"schema_version", "mode", "policy", "run_plan_sha256", "stage_key"}
            or type(review["schema_version"]) is not int or review["schema_version"] != 1):
        _invalid("字段审核合同格式不完整")
    validate_field_review_choice({"mode": review["mode"], "policy": review["policy"]},
        interaction_mode="unattended", current=False)
    if (not isinstance(review["stage_key"], str) or not review["stage_key"]
            or not isinstance(review["run_plan_sha256"], str) or len(review["run_plan_sha256"]) != 64
            or any(c not in "0123456789abcdef" for c in review["run_plan_sha256"])):
        _invalid("字段批准必须引用真实运行计划及阶段")


def describe_field_review_contract(review):
    """Read-only wording from the frozen choice; no current-policy substitution."""
    verify_field_review_contract(review)
    if review["mode"] == "manual":
        return {"mode": "manual", "label": "人工确认字段",
            "description": "校验通过后仍需确认并采用。保存字段不会自动批准，也不会覆盖已采用内容。"}
    return {"mode": "policy", "label": "按本轮配方自动批准字段",
        "description": "无人值守授权后，按冻结规则标识及业务版本核对来源并采用唯一候选；实现摘要仅记录配置冻结时的代码。只检查通用执行合同，不代替阶段审核或文案质量审核。"}


def production_field_review_contract(stage, *, current=True):
    """Called under the original Run/Stage lock when preparing or approving."""
    from studio.services.production_run_plans import get_run_execution_plan

    run = stage.production_run
    plan = get_run_execution_plan(run)
    manual = json_contract({"schema_version": 1, "mode": "manual"}, max_bytes=execution_configuration_max_bytes())
    if plan.value["source"] == "manual":
        return manual
    recipe = plan.value["recipe"]["definition"]
    stages = [item for item in recipe["stages"] if item["key"] == stage.stage_key]
    if len(stages) != 1 or stages[0]["mode"] != stage.mode:
        _invalid("阶段与冻结运行计划不一致")
    choice = validate_field_review_choice(stages[0].get("field_review", {"mode": "manual"}),
        interaction_mode=recipe["interaction_mode"], current=current)
    if choice["mode"] == "manual":
        return manual
    from studio.services.production_creation_inputs import recipe_unattended_authorized
    if not recipe_unattended_authorized(run):
        _invalid("本轮未明确授权无人值守，不能自动批准字段")
    if stage.mode in {"manual", "skip", "review"}:
        _invalid("人工、跳过或阶段审核职责不能自动批准生成字段")
    return json_contract({"schema_version": 1, **choice, "run_plan_sha256": plan.sha256, "stage_key": stage.stage_key},
        max_bytes=execution_configuration_max_bytes())


def verify_review_owner(review, owner_key, owner, *, current=True):
    verify_field_review_contract(review)
    expected = (production_field_review_contract(owner, current=current).value if owner_key == "stage_run_id"
        else {"schema_version": 1, "mode": "manual"})
    if review != expected:
        _invalid("字段审核方式必须来自本轮真实计划；素材库和单独烟测只允许人工确认")


def _candidate_values(preparation, contexts, unselected):
    """No latest-revision heuristic: ambiguity requires an explicit choice."""
    if not unselected:
        return []
    scope = Q(pk__in=[])
    for item in unselected:
        scope |= Q(unit_key=item["unit_key"], field_key=item["field_key"])
    # Aggregate revision identities first. History can grow, but automatic
    # review must not materialize every historical text in memory or pick Max.
    groups = {(row["unit_key"], row["field_key"]): row for row in preparation.field_values.filter(scope)
        .exclude(status="rejected").order_by().values("unit_key", "field_key")
        .annotate(count=Count("id"), value_id=Min(Cast("id", CharField())))}
    identifiers, missing = [], False
    for item in unselected:
        key = (item["unit_key"], item["field_key"])
        group = groups.get(key)
        if group and group["count"] > 1:
            _invalid("同一字段存在多个未采用修订，请明确采用后继续；不会自动选择最新版本", "field_selection_conflict")
        if group is None:
            missing = missing or item["required"]
            continue
        identifiers.append(group["value_id"])
    # Avoid partial policy approval while required sources/results are missing.
    if missing:
        return []
    result = list(preparation.field_values.select_for_update().filter(pk__in=identifiers).order_by("id"))
    if len(result) != len(identifiers):
        _invalid("字段修订在批准前已变化")
    for value in result:
        key = (value.unit_key, value.field_key)
        _verify_value(contexts[key], value)
        if value.status not in {"pending", "confirmed"}:
            _invalid("字段状态不支持批准")
    attempts = {row["id"]: row for row in GenerationAttempt.objects.filter(
        pk__in=[value.origin_attempt_id for value in result if value.origin_attempt_id]
    ).values("id", "parent_preparation_id", "stage_run_id", "role", "status")}
    for value in result:
        if value.origin_kind != "model":
            continue
        attempt = attempts.get(value.origin_attempt_id)
        if (value.origin_asset_job_id or attempt is None or attempt["parent_preparation_id"] != preparation.pk
                or attempt["stage_run_id"] != preparation.stage_run_id or attempt["role"] != "auxiliary"
                or attempt["status"] != "succeeded"):
            _invalid("模型字段必须来自本准备已成功的真实辅助任务")
    from studio.services.execution_auxiliary_results import verify_production_field_result
    for attempt in GenerationAttempt.objects.filter(pk__in=attempts):
        verify_production_field_result(attempt)
    return result


@security_boundary_resolution_cache()
def apply_preparation_field_policy(*, preparation_id, expected_preparation_revision, actor):
    """Formal advance only. Returns the revision to use for field evaluation.

    The shared selection reader also locks reused field owners; existing
    selections and their original approvals are never replaced or reapproved.
    All writes roll back if any value/selection fails. No stage wins or calls.
    """
    from studio.services.execution_preparations import read_execution_preparation_detail

    with locked_execution_field_selection_snapshot(preparation_id=preparation_id,
            expected_preparation_revision=expected_preparation_revision, actor=actor) as (preparation, selected):
        _editable(preparation)
        detail = read_execution_preparation_detail(preparation)
        review = detail.review_contract
        verify_field_review_contract(review)
        if review["mode"] == "manual":
            return preparation.revision
        if not preparation.stage_run_id or preparation.purpose != "production":
            _invalid("自动批准只能属于明确授权的生产阶段")
        stage = preparation.stage_run
        # Reusing an already selected, verified value is not a new approval.
        # Keep its historical policy evidence even after a code deployment.
        verify_review_owner(review, "stage_run_id", stage, current=False)
        if (stage.production_run.status in {"succeeded", "failed", "cancelled"}
                or stage.status in {"succeeded", "failed", "cancelled"}):
            _invalid("已结束或取消的运行/阶段不能自动批准新字段")
        candidates = _candidate_values(preparation, _field_contexts(preparation), selected.value["unselected_fields"])
        if candidates:
            validate_field_review_choice({"mode": "policy", "policy": review["policy"]}, interaction_mode="unattended")
        # This identifies the original authorization, not current bytecode.
        policy_sha = json_contract(review["policy"], max_bytes=execution_configuration_max_bytes()).sha256
        now = timezone.now()
        for value in candidates:
            if value.status == "pending":
                value.status, value.approval_kind = "confirmed", "policy"
                value.approval_policy_sha256 = policy_sha
                value.reviewed_by, value.reviewed_at = None, now
                value.save(update_fields=("status", "approval_kind", "approval_policy_sha256", "reviewed_by", "reviewed_at", "updated_at"))
            ExecutionFieldSelection.objects.create(preparation=preparation, unit_key=value.unit_key, field_key=value.field_key,
                field_value=value, revision=1, selected_by=actor, selected_at=now)
        if candidates:
            _touch(preparation)
        return preparation.revision


def _normalized_code(code):
    # Configuration-time diagnostic fingerprint, never an admission gate.
    # Hash loaded code, not possibly changed source files. Ignore checkout
    # paths and line offsets, not bytecode or literal constants.
    return code.replace(co_filename="", co_firstlineno=1, co_linetable=b"", co_consts=tuple(
        _normalized_code(value) if isinstance(value, CodeType) else value for value in code.co_consts))


def _implementation_sha():
    from studio.services.execution_field_contracts import validate_execution_field
    from studio.services.execution_auxiliary_results import verify_production_field_result
    from studio.services.execution_auxiliary_requests import read_production_field_context, read_preparation_field_batch
    from studio.services.execution_task_links import validate_execution_task_links

    functions = (_invalid, validate_field_review_choice, verify_field_review_contract, production_field_review_contract,
        verify_review_owner, _candidate_values, apply_preparation_field_policy, _verify_value, _validate_text, validate_execution_field,
        verify_production_field_result, read_production_field_context, read_preparation_field_batch, validate_execution_task_links)
    return sha256(b"".join(marshal.dumps(_normalized_code(unwrap(fn).__code__)) for fn in functions)).hexdigest()


_POLICY_IDENTITY = {"key": "execution_fields.accept_valid_v1", "version": 1, "implementation_sha256": _implementation_sha()}
