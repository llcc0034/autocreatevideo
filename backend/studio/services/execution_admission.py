"""One deterministic evidence/TTL/capacity decision for new execution callers.

This module does not discover evidence, infer defaults, waive root permissions
or authorize a payment. Source adapters must provide actual completed Smoke
facts and the already checked root's hard blockers. Worker integration and
versioned configuration registration are separate cutover work.
"""
from dataclasses import dataclass
from datetime import datetime

from django.core.exceptions import ValidationError
from django.utils import timezone


@dataclass(frozen=True)
class ExecutionEvidenceIdentity:
    version_id: str | None
    definition_sha256: str
    target_fingerprint: str
    operation: str
    mode: str
    scope: str


@dataclass(frozen=True)
class ExecutionSmokeFact:
    identity: ExecutionEvidenceIdentity
    attempt_id: str
    status: str
    finished_at: datetime | None
    expires_at: datetime | None


def _time(value):
    return isinstance(value, datetime) and timezone.is_aware(value)


def _issue(code, message):
    return {"code": code, "message": message}


def evaluate_execution_admission(*, identity, smoke_facts, purpose, smoke_policy, capacity_policy,
        capacity_state, hard_blockers, now):
    """Evaluate explicit facts only; a passed label alone never proves a match.

    Only a formal main Smoke may collect its own missing combination and
    capacity evidence. Newly invoked auxiliary leaves remain ordinary calls.
    An exemption never asserts coverage or waives a supplied hard blocker.
    """
    resource = purpose == "resource_smoke"
    if (not isinstance(identity, ExecutionEvidenceIdentity)
            or (resource and (identity.scope != "resource_smoke" or identity.version_id is not None))
            or (not resource and (identity.scope != "instance_smoke" or not identity.version_id))):
        raise ValidationError("准入用途必须与精确实例或基础资源证据范围一致")
    if smoke_policy not in {"require_passed", "require_fresh"} or capacity_policy not in {"warn", "require_evidence"}:
        raise ValidationError("执行准入配置缺失或非法，不能默认放行", code="execution_admission_configuration_invalid")
    if purpose not in {"production", "instance_smoke", "resource_smoke"} or capacity_state not in {"covered", "uncovered", "unknown"}:
        raise ValidationError("执行用途或本次规格观测状态无效")
    if not _time(now) or not isinstance(smoke_facts, (list, tuple)) or not isinstance(hard_blockers, list):
        raise ValidationError("执行准入需要明确时间、真实证据集合与硬门禁结果")
    if resource and (smoke_facts or capacity_state == "covered"):
        raise ValidationError("基础资源验收不读取或代替实例组合证据与容量覆盖")
    if any(not isinstance(row, dict) or set(row) != {"code", "message"}
            or any(not isinstance(row[key], str) or not row[key] for key in row) for row in hard_blockers):
        raise ValidationError("硬门禁必须返回明确的原因码及说明")
    applicable, ids = [], set()
    for fact in smoke_facts:
        if not isinstance(fact, ExecutionSmokeFact):
            raise ValidationError("证据必须来自正式Smoke事实适配器")
        if fact.identity != identity:
            continue
        if not isinstance(fact.attempt_id, str) or not fact.attempt_id or fact.attempt_id in ids:
            raise ValidationError("组合证据缺少唯一的真实任务身份")
        ids.add(fact.attempt_id)
        if fact.status in {"queued", "submitting", "submitted", "running", "preparing", "cancel_requested", "cancelled", "uncertain"}:
            continue
        if fact.status not in {"succeeded", "failed"} or not _time(fact.finished_at) or fact.finished_at > now:
            raise ValidationError("组合证据状态或真实完成时间无效")
        if fact.status == "succeeded" and (not _time(fact.expires_at) or fact.expires_at <= fact.finished_at):
            raise ValidationError("通过证据缺少有效的冻结到期时间，不能当作永久有效")
        applicable.append(fact)
    # Completed event ordering follows the existing stable time/id convention;
    # an in-flight/uncertain attempt never manufactures a failed Smoke result.
    latest = max(applicable, key=lambda row: (row.finished_at, row.attempt_id), default=None)
    evidence_state = "missing" if latest is None else (
        "failed" if latest.status == "failed" else "fresh" if latest.expires_at > now else "expired")
    blockers = [dict(row) for row in hard_blockers]
    warnings = []
    main_smoke = purpose in {"instance_smoke", "resource_smoke"}
    if not main_smoke:
        if evidence_state == "missing":
            blockers.append(_issue("execution_smoke_missing", "此精确组合尚无真实通过记录，请先执行实例验收"))
        elif evidence_state == "failed":
            blockers.append(_issue("execution_smoke_failed", "此精确组合最近一次完成的烟测失败，不能使用更早通过记录覆盖"))
        elif evidence_state == "expired":
            issue = _issue("execution_smoke_expired", "此精确组合曾通过，但证据已过期")
            (blockers if smoke_policy == "require_fresh" else warnings).append(issue)
    if capacity_state != "covered":
        issue = _issue(f"execution_capacity_{capacity_state}",
            "本次规格尚无相同规格的真实观测" if capacity_state == "uncovered" else "本次规格缺少完整容量观测维度")
        if main_smoke:
            issue["message"] += "；本次为主烟测，允许在硬合同和授权预算内取样，不代表容量已验证"
        (blockers if capacity_policy == "require_evidence" and not main_smoke else warnings).append(issue)
    return {"configuration_state": "blocked" if hard_blockers else "valid", "evidence_state": evidence_state,
        "capacity_state": capacity_state, "evidence_attempt_id": latest.attempt_id if latest else None,
        "evidence_finished_at": latest.finished_at.isoformat() if latest else None,
        "evidence_expires_at": latest.expires_at.isoformat() if latest and latest.expires_at else None,
        "own_smoke_gate_exempt": main_smoke,
        "own_capacity_gate_exempt": main_smoke and capacity_policy == "require_evidence" and capacity_state != "covered",
        "policies": {"smoke": smoke_policy, "capacity": capacity_policy},
        "warnings": warnings, "blockers": blockers, "can_submit": not blockers}
