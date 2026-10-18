"""Persist small, typed execution checks under the original worker claim lock.

There is no frozen-input copy, unbounded JSON log, or remote call here. Records
belong to the original Task and are removed with its reviewed lifecycle cleanup.
"""
from django.core.exceptions import ValidationError
from django.utils import timezone
from django.utils.dateparse import parse_datetime

from studio.models import GenerationTaskObservation
from studio.services.execution_production_workflow_queue import (
    _linked_task_attempt, _locked_task_attempt, read_prepared_generation_task,
)


def _release_values(audit):
    keys = {"schema_version", "status", "policy_sha256", "unload_models", "free_memory",
        "attempted_at", "completed_at", "error_code", "http_status"}
    if not isinstance(audit, dict) or set(audit) != keys or type(audit["schema_version"]) is not int or audit["schema_version"] != 1:
        raise ValidationError("资源释放观察必须使用完整的版本1回执")
    if audit["status"] not in {"succeeded", "failed"} or any(type(audit[key]) is not bool for key in ("unload_models", "free_memory")):
        raise ValidationError("资源释放回执状态或操作声明无效")
    dates = []
    for key in ("attempted_at", "completed_at"):
        try:
            value = parse_datetime(audit[key]) if isinstance(audit[key], str) else None
        except ValueError:
            value = None
        if value is None or timezone.is_naive(value):
            raise ValidationError("资源释放回执必须记录带时区的真实起止时间")
        dates.append(value)
    if audit["http_status"] is not None and type(audit["http_status"]) is not int:
        raise ValidationError("资源释放HTTP状态无效")
    return {"status": "passed" if audit["status"] == "succeeded" else "failed",
        "started_at": dates[0], "finished_at": dates[1],
        **{key: audit[key] for key in ("policy_sha256", "unload_models", "free_memory", "http_status", "error_code")}}


def record_task_observation(task, *, kind, report=None, started_at=None, error_code=""):
    """False means the original claim was lost; it is not the new claim's failure."""
    attempt = _linked_task_attempt(task)
    with _locked_task_attempt(task, attempt) as (_, preparation, attempt, current):
        if (type(task.attempts) is not int or task.attempts < 1 or task.started_at is None
                or current.attempts != task.attempts or current.started_at != task.started_at
                or current.status not in {"running", "cancel_requested"}):
            return False
        # A corrupt or relabelled source cannot receive a valid-looking check.
        read_prepared_generation_task(current)
        if kind == "resource_release":
            values = _release_values(report)
        elif kind == "preflight":
            values = {"status": "failed" if error_code else "passed", "error_code": error_code,
                "started_at": started_at, "finished_at": timezone.now()}
            if error_code:
                if report is not None:
                    raise ValidationError("失败节点检查不能携带成功报告")
            else:
                if not isinstance(report, dict) or set(report) != {"node_count", "link_count", "class_types", "schema_sha256"}:
                    raise ValidationError("节点检查必须记录正式预检报告")
                if any(type(report[key]) is not int or report[key] < 0 for key in ("node_count", "link_count")):
                    raise ValidationError("节点/连接数量无效")
                values.update({key: report[key] for key in ("node_count", "link_count", "schema_sha256")})
        else:
            raise ValidationError("不支持的任务检查类型")
        observation = GenerationTaskObservation(task=current, kind=kind,
            claim_number=current.attempts, claim_started_at=current.started_at,
            request_sha256=preparation.request_sha256, **values)
        observation.full_clean()
        observation.save()
        return True
