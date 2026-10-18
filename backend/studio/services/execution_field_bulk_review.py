"""Explicit, bounded human approval of existing shot fields; never dispatch."""
from hashlib import sha256
import json
from uuid import UUID

from django.core.exceptions import ValidationError
from django.db.models import Count, Min, CharField, prefetch_related_objects
from django.db.models.functions import Cast

from studio.models import ExecutionFieldValue, Shot
from studio.services.execution_preparation_fields import (
    _locked_preparations, _field_contexts, _verify_value, _validate_text, _revision, _conflict,
    confirm_execution_field,
)
from studio.services.prompt_normalization_contract import PromptNormalizationContractError
from studio.services.security_boundaries import security_boundary_resolution_cache

BULK_FIELD_REVIEW_LIMIT = 50


def _ids(values):
    if not isinstance(values, list) or not 1 <= len(values) <= BULK_FIELD_REVIEW_LIMIT:
        raise ValidationError("每次明确选择1–50个镜头准备")
    try:
        ids = [str(UUID(str(value))) for value in values]
    except (ValueError, TypeError, AttributeError) as exc:
        raise ValidationError("准备标识无效") from exc
    if len(set(ids)) != len(ids):
        raise ValidationError("不能重复选择同一准备")
    return ids


def _scope(rows):
    from studio.services.model_profile_options import MODEL_CAPABILITY_CATALOG
    media_operations = {item["value"] for item in MODEL_CAPABILITY_CATALOG if {"image", "video"}.intersection(item["modalities"])}
    prefetch_related_objects(list(rows.values()), "stage_run__production_run", "execution_instance_version__prompt_profile_version", "contract")
    stages = {row.stage_run_id for row in rows.values()}
    if None in stages or len(stages) != 1:
        raise ValidationError("批量确认只支持同一生产阶段的镜头主准备")
    stage = next(iter(rows.values())).stage_run
    if stage.status in {"succeeded", "failed", "cancelled"} or stage.production_run.status in {"succeeded", "failed", "cancelled"}:
        _conflict("运行或阶段已结束，不能批量确认")
    shot_ids = []
    for row in rows.values():
        if (row.purpose != "production" or not row.execution_instance_version_id
                or row.execution_instance_version.operation not in media_operations
                or not row.unit_key.startswith("shot:")):
            raise ValidationError("只支持镜头媒体主准备，不包含辅助任务或验收准备")
        try:
            shot_ids.append(UUID(row.unit_key[5:]))
        except ValueError as exc:
            raise ValidationError("镜头标识不可核验") from exc
    if len(set(shot_ids)) != len(shot_ids):
        raise ValidationError("同一镜头只能选择一份准备；请明确候选版本")
    shots = {str(row["id"]): row for row in Shot.objects.filter(pk__in=shot_ids,
        episode__project_id=stage.production_run.project_id).values("id", "number", "episode__number")}
    if len(shots) != len(shot_ids):
        raise ValidationError("镜头不属于本轮项目或已不可用")
    return shots


def _preview(row, shot):
    from studio.services.execution_text_response_review import field_warning_messages
    contexts = _field_contexts(row)
    selected = {(item["unit_key"], item["field_key"]): item for item in row.field_selections.values(
        "unit_key", "field_key", "field_value_id", "revision")}
    # Aggregate identities, not every historical body. Multiple revisions require
    # an explicit individual choice; never pick the newest text on the user's behalf.
    groups = {(item["unit_key"], item["field_key"]): item for item in row.field_values
        .filter(status__in=("pending", "confirmed")).order_by().values("unit_key", "field_key")
        .annotate(count=Count("id"), value_id=Min(Cast("id", CharField())))}
    candidates = {value.pk: value for value in ExecutionFieldValue.objects.filter(
        pk__in=[item["value_id"] for key, item in groups.items() if item["count"] == 1 and key not in selected])}
    fields = []
    for identity, context in contexts.items():
        unit, key = identity
        item = {"unit_key": unit, "field_key": key, "label": context["item"]["field_contract"]["field_definition"]["label"],
            "state": "blocked", "reason": "", "value_id": None, "revision": None, "text": "", "warnings": []}
        if identity in selected:
            item.update(state="selected", reason="已有采用记录，保持不变", value_id=str(selected[identity]["field_value_id"]))
        elif identity not in groups:
            item["reason"] = "没有待确认正文，请单独查看或处理"
        elif groups[identity]["count"] != 1:
            item["reason"] = "存在多个可选修订，请在单字段审核中明确选择"
        else:
            value = candidates[UUID(groups[identity]["value_id"])]
            item.update(value_id=str(value.pk), revision=value.revision, text=value.text)
            try:
                _verify_value(context, value)
                item["warnings"] = [warning["message"] for warning in field_warning_messages(_validate_text(context, value.text))]
                item["state"] = "candidate"
            except (ValidationError, PromptNormalizationContractError) as exc:
                item["reason"] = "正文或来源校验未通过：" + str(exc)
        fields.append(item)
    reason = ""
    if row.request_frozen_at or row.status == "cancelled":
        reason = "准备已冻结或取消，不能改变采用记录"
    elif not fields:
        reason = "没有需要人工确认的提示词字段"
    elif any(item["state"] == "blocked" for item in fields):
        reason = "有字段需要单独处理，本镜头不能一键采用"
    elif not any(item["state"] == "candidate" for item in fields):
        reason = "全部字段已采用，无需重复确认"
    result = {"preparation_id": str(row.pk), "expected_preparation_revision": row.revision,
        "label": f"第{shot['episode__number']}集 · 镜头{shot['number']}", "fields": fields,
        "ready": not reason, "reason": reason}
    result["preview_sha256"] = sha256(json.dumps(result, ensure_ascii=False, sort_keys=True, separators=(",", ":")).encode()).hexdigest()
    return result


@security_boundary_resolution_cache()
def preview_bulk_field_review(*, preparation_ids, actor):
    ids = _ids(preparation_ids)
    with _locked_preparations(ids, actor) as rows:
        shots = _scope(rows)
        return {"items": [_preview(rows[pk], shots[rows[pk].unit_key[5:]]) for pk in ids]}


@security_boundary_resolution_cache()
def confirm_bulk_field_review(*, items, actor):
    """One transaction across selected shots; all validate before any approval.

    Existing field confirmation/selection owns human audit and validation. A
    stale receipt fails closed; retrying cannot create duplicate approvals.
    """
    ids = _ids([item["preparation_id"] for item in items])
    with _locked_preparations(ids, actor) as rows:
        shots = _scope(rows)
        plans = []
        for pk, requested in zip(ids, items):
            row = rows[pk]
            _revision(row.revision, requested["expected_preparation_revision"], "expected_preparation_revision")
            preview = _preview(row, shots[row.unit_key[5:]])
            if not preview["ready"] or preview["preview_sha256"] != requested["preview_sha256"]:
                _conflict("待确认字段、采用状态或预览已变化，请重新预览；本批未采用任何字段")
            plans.append((row, preview))
        count, results = 0, []
        for row, preview in plans:
            for field in preview["fields"]:
                if field["state"] != "candidate":
                    continue
                value = row.field_values.get(pk=field["value_id"])
                confirm_execution_field(preparation_id=row.pk, field_value_id=value.pk,
                    expected_preparation_revision=row.revision, expected_text_sha256=value.text_sha256,
                    expected_source_sha256=value.source_sha256, select_for_execution=True,
                    expected_selection_revision=None, replace=False, actor=actor)
                row.refresh_from_db()
                count += 1
            results.append({"preparation_id": str(row.pk), "revision": row.revision})
        return {"confirmed_field_count": count, "items": results}
