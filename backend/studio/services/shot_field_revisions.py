"""One typed, append-only writer for independently revised shot inputs.

Original script/structure/default provenance is immutable. Overrides address
exact original node/path slots; editing an object does not rewrite child slots,
typed relationships, dialogue, timing or any already frozen execution input.
"""
from copy import deepcopy
from uuid import UUID

from django.core.exceptions import PermissionDenied, ValidationError
from django.db import transaction
from django.db.models import OuterRef, Subquery

from studio.models import Shot, ShotFieldRevision
from studio.services.data_structures import _value_errors, validate_data_structure_snapshot
from studio.services.json_contracts import DATABASE_JSON_FIELD_MAX_BYTES, canonical_json_bytes, json_contract, verify_json_contract
from studio.services.script_storyboard_fields import read_bound_shot_field_contract
from studio.services.structure_paths import nonnull_schema


def _conflict(message):
    raise ValidationError(message, code="shot_field_revision_conflict")


def _identity(row):
    return canonical_json_bytes([row["node_key"], row["value_path"]])


def _field_schema(schema, field):
    for part in field["value_path"]:
        base = nonnull_schema(schema)
        schema = base["items"] if type(part) is int else base["properties"][part]
    if nonnull_schema(schema).get("x-node-key") != field["node_key"]:
        _conflict("原字段的节点与路径不一致")
    return schema


def _validate_overrides(overrides, original, structure):
    if not isinstance(overrides, list):
        _conflict("独立字段修订载荷无效")
    fields = {_identity(row): row for row in original["fields"]}
    schema = validate_data_structure_snapshot(structure)
    found = set()
    for row in overrides:
        if (not isinstance(row, dict) or set(row) != {"node_key", "value_path", "value"}
                or not isinstance(row["node_key"], str) or not isinstance(row["value_path"], list)
                or any(not isinstance(part, str) and (type(part) is not int or part < 0) for part in row["value_path"])):
            raise ValidationError("修订必须提供确切节点、原值路径和原始 JSON 值")
        key = _identity(row)
        if key in found or key not in fields:
            raise ValidationError("修订字段重复或不在本镜原结构化字段中；不能猜测字段身份")
        found.add(key)
        field = fields[key]
        errors = _value_errors(_field_schema(schema, field), row["value"], path=field["source_path"])
        if errors:
            raise ValidationError(errors)


def revision_reference(row):
    if row is None:
        return None
    return {"id": str(row.pk), "revision": row.revision,
        "previous_id": str(row.previous_id) if row.previous_id else None,
        "source_sha256": row.source_sha256, "content_sha256": row.content_sha256,
        "created_by": row.created_by_id, "created_at": row.created_at.isoformat()}


def shot_field_revision_references(shot_ids):
    """One indexed metadata query for review freshness, never a JSON list load."""
    latest = ShotFieldRevision.objects.filter(shot_id=OuterRef("shot_id")).values("pk")[:1]
    rows = ShotFieldRevision.objects.filter(shot_id__in=shot_ids, pk=Subquery(latest)).only(
        "id", "shot_id", "revision", "previous_id", "source_sha256", "content_sha256", "created_by_id", "created_at")
    return {row.shot_id: revision_reference(row) for row in rows}


def _verified_overrides(row, original, structure, *, source_sha256, max_bytes):
    if row is None:
        return []
    if row.source_sha256 != source_sha256:
        _conflict("修订依据与镜头原绑定不一致，请检查来源；不会改用当前结构")
    try:
        if row.content_schema_version != 1:
            _conflict("独立字段修订版本不受支持")
        result = verify_json_contract(row.overrides, schema_version=row.content_schema_version, size_bytes=row.content_size_bytes,
            sha256=row.content_sha256, max_bytes=max_bytes).value
        _validate_overrides(result, original, structure)
    except ValidationError as exc:
        _conflict("保存的独立字段修订无效：" + "；".join(exc.messages))
    return result


def _detail(original, structure, row, *, max_bytes):
    basis = json_contract(original["source"], max_bytes=max_bytes).sha256
    schema = validate_data_structure_snapshot(structure)
    overrides = {_identity(item): item for item in _verified_overrides(row, original, structure,
        source_sha256=basis, max_bytes=max_bytes)}
    fields = []
    for field in original["fields"]:
        override = overrides.get(_identity(field))
        item = {"original": deepcopy(field), "schema": _field_schema(schema, field), "overridden": override is not None,
            "present": override is not None or field["present"]}
        if item["present"]:
            item["value"] = deepcopy(override["value"] if override is not None else field["value"])
        fields.append(item)
    # Original and overrides were independently bounded and are loaded on demand.
    result = {"source": original["source"], "source_sha256": basis,
        "revision": revision_reference(row), "fields": fields}
    return result


def read_shot_field_revision(*, shot_id, project_id, max_bytes, revision_id=None):
    original, structure = read_bound_shot_field_contract(shot_id=shot_id, project_id=project_id, max_bytes=max_bytes)
    rows = ShotFieldRevision.objects.filter(shot_id=shot_id)
    row = rows.filter(pk=revision_id).first() if revision_id else rows.first()
    if revision_id and row is None:
        _conflict("指定修订不存在或不属于本镜")
    return _detail(original, structure, row, max_bytes=max_bytes)


def read_frozen_shot_field_revision(*, shot_id, project_id, source_reference, field_revision, max_bytes):
    """Resolve a verified review's references, never the editor's latest row.

    The caller owns review authorization/freshness. An explicit null revision
    means the bound original, even after a manual revision has been appended.
    Full metadata equality prevents a changed binding or revision receipt from
    being interpreted as the previously approved input.
    """
    original, structure = read_bound_shot_field_contract(shot_id=shot_id, project_id=project_id, max_bytes=max_bytes)
    expected_source = {key: value for key, value in original["source"].items() if key != "shot_id"}
    if not isinstance(source_reference, dict) or canonical_json_bytes(source_reference) != canonical_json_bytes(expected_source):
        _conflict("原审核的结构化来源与本镜确切绑定不一致；不能改读当前来源")
    row = None
    if field_revision is not None:
        if not isinstance(field_revision, dict):
            _conflict("原审核的独立字段修订引用不完整")
        try:
            identifier = UUID(field_revision["id"])
        except (KeyError, ValueError, TypeError, AttributeError):
            _conflict("原审核的独立字段修订身份无效")
        row = ShotFieldRevision.objects.filter(shot_id=shot_id, pk=identifier).first()
        if row is None or canonical_json_bytes(revision_reference(row)) != canonical_json_bytes(field_revision):
            _conflict("原审核的独立字段修订不存在或引用不一致；不能改读最新修订")
    return _detail(original, structure, row, max_bytes=max_bytes)


def _request(payload, *, max_bytes):
    expected = {"request_id", "source_sha256", "expected_revision_id", "node_key", "value_path", "operation"}
    if not isinstance(payload, dict) or payload.get("operation") not in {"set", "reset"}:
        raise ValidationError("请选择另存字段值或恢复原字段值")
    if set(payload) != expected | ({"value"} if payload["operation"] == "set" else set()):
        raise ValidationError("修订请求字段不完整或包含未知字段")
    try:
        UUID(payload["request_id"])
        if payload["expected_revision_id"] is not None:
            UUID(payload["expected_revision_id"])
    except (ValueError, TypeError, AttributeError) as exc:
        raise ValidationError("请求身份与预期修订必须是 UUID") from exc
    if (not isinstance(payload["node_key"], str) or not isinstance(payload["value_path"], list)
            or any(not isinstance(part, str) and (type(part) is not int or part < 0) for part in payload["value_path"])):
        raise ValidationError("请选择原结构中的确切字段节点和路径")
    return json_contract(payload, max_bytes=max_bytes)


@transaction.atomic
def revise_shot_field(*, shot_id, project_id, user, payload, max_bytes):
    maximum = min(max_bytes, DATABASE_JSON_FIELD_MAX_BYTES)
    request = _request(payload, max_bytes=maximum)
    shot = Shot.objects.select_for_update().select_related("episode__project").only(
        "id", "episode_id", "episode__project_id", "episode__project__owner_id").get(
        pk=shot_id, episode__project_id=project_id)
    if not user.is_authenticated or not (user.is_staff or shot.episode.project.owner_id == user.pk):
        raise PermissionDenied("不能修改其他用户项目的镜头字段")
    original, structure = read_bound_shot_field_contract(shot_id=shot_id, project_id=project_id, max_bytes=maximum)
    basis = json_contract(original["source"], max_bytes=maximum).sha256
    if payload["source_sha256"] != basis:
        _conflict("原来源依据已变化，请重新读取后再修改")
    rows = ShotFieldRevision.objects.filter(shot_id=shot_id)
    replay = rows.filter(request_id=payload["request_id"]).first()
    if replay:
        if replay.request_sha256 != request.sha256 or replay.created_by_id != user.pk:
            _conflict("同一请求身份不能用于不同内容或操作人")
        return _detail(original, structure, replay, max_bytes=maximum), True
    current = rows.first()
    if payload["expected_revision_id"] != (str(current.pk) if current else None):
        _conflict("独立字段已有新修订，请重新读取并核对草稿；不会覆盖他人修改")
    overrides = _verified_overrides(current, original, structure, source_sha256=basis, max_bytes=maximum)
    key = _identity(payload)
    if key not in {_identity(field) for field in original["fields"]}:
        raise ValidationError("所选字段不属于本镜的原结构化来源")
    updated = [deepcopy(item) for item in overrides if _identity(item) != key]
    if payload["operation"] == "set":
        updated.append({name: deepcopy(payload[name]) for name in ("node_key", "value_path", "value")})
    _validate_overrides(updated, original, structure)
    contract = json_contract(updated, max_bytes=maximum)
    row = ShotFieldRevision.objects.create(shot=shot, revision=current.revision + 1 if current else 1,
        previous=current, source_sha256=basis, overrides=updated, content_size_bytes=contract.size_bytes,
        content_sha256=contract.sha256, request_id=payload["request_id"], request_sha256=request.sha256,
        created_by=user)
    # Validate readable detail before committing; errors leave no partial row.
    return _detail(original, structure, row, max_bytes=maximum), False
