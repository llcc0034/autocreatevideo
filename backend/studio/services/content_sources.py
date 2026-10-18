"""Version authoring for implemented readers; never evaluates paths or scripts."""
from copy import deepcopy
from uuid import UUID
from django.core.exceptions import ValidationError
from django.utils import timezone

from studio.models import ContentSourceVersion, DataStructureVersion
from studio.services.data_structures import data_structure_snapshot, validate_data_structure_snapshot, validate_structure_value
from studio.services.execution_project_sources import PROJECT_SOURCE_READERS, project_source_reader
from studio.services.execution_source_readers import (REQUEST_TEXT_SOURCE_SELECTORS, VISUAL_RULE_SOURCE_FIELDS,
    implemented_source_reader, reviewed_visual_source_reader, validate_source_reader)
from studio.services.json_contracts import DATABASE_JSON_FIELD_MAX_BYTES, json_contract, verify_json_contract
from studio.services.platform_lifecycle_audit import record_platform_lifecycle_event
from studio.services.prompt_source_catalog import prompt_source_options
from studio.services.registry_reference_lock import registry_reference_writer


# Includes the exact shared structure, never the resolved business content.
CONTENT_SOURCE_CONTRACT_MAX_BYTES = DATABASE_JSON_FIELD_MAX_BYTES


def source_reader_options():
    # Reviewed output fields come from the very same output-reader definition.
    return [{**item, "reader": reader_contract(item["value"])} for item in
        prompt_source_options([*PROJECT_SOURCE_READERS, *VISUAL_RULE_SOURCE_FIELDS, *REQUEST_TEXT_SOURCE_SELECTORS])]


def reader_contract(selector):
    if selector in REQUEST_TEXT_SOURCE_SELECTORS:
        return implemented_source_reader(selector)
    if selector in PROJECT_SOURCE_READERS:
        return project_source_reader(selector)
    if selector in VISUAL_RULE_SOURCE_FIELDS:
        return reviewed_visual_source_reader(selector)
    raise ValidationError({"reader_selector": "请选择已实现的来源读者；不能输入数据库路径、URL或脚本"})


def source_contract(source):
    reader = validate_source_reader(source.reader_contract)
    if reader["selector"] != source.reader_selector:
        raise ValidationError({"reader_selector": "来源标识与已保存的读取合同不一致"})
    if not source.data_structure_version_id:
        raise ValidationError({"data_structure_version": "请选择已发布的结构版本；旧来源不会自动补配结构"})
    structure = data_structure_snapshot(source.data_structure_version)
    _validate_reader_structure(reader, structure)
    return json_contract({"schema_version": 2, "id": str(source.pk), "source_key": str(source.source_key),
        "version": source.version, "name": source.name, "description": source.description,
        "reader": reader, "data_structure": structure}, schema_version=2, max_bytes=CONTENT_SOURCE_CONTRACT_MAX_BYTES)


def _validate_reader_structure(reader, structure):
    validate_data_structure_snapshot(structure)
    root = next(node for node in structure["nodes"] if node["parent_key"] is None)
    if reader["value_kind"] == "text" and root["field_definition"]["value_type"] not in {"text", "long_text"}:
        raise ValidationError({"data_structure_version": "所选读取协议输出文字，需要 text/long_text 根节点结构"})


def verify_source_reference(reference):
    keys = {"schema_version", "id", "source_key", "version", "name", "description", "reader", "data_structure", "sha256"}
    if (not isinstance(reference, dict) or set(reference) != keys
            or type(reference["schema_version"]) is not int or reference["schema_version"] != 2
            or type(reference["version"]) is not int or reference["version"] < 1
            or not isinstance(reference["name"], str) or not reference["name"].strip()
            or not isinstance(reference["description"], str)):
        raise ValidationError("来源快照不完整，需要包含确切结构的 v2 合同")
    for key in ("id", "source_key"):
        try:
            if not isinstance(reference[key], str):
                raise ValueError
            UUID(reference[key])
        except ValueError as exc:
            raise ValidationError("来源快照标识无效") from exc
    contract = json_contract({key: value for key, value in reference.items() if key != "sha256"},
        schema_version=2, max_bytes=CONTENT_SOURCE_CONTRACT_MAX_BYTES)
    verify_json_contract(contract.value, schema_version=2, size_bytes=contract.size_bytes,
        sha256=reference["sha256"], max_bytes=CONTENT_SOURCE_CONTRACT_MAX_BYTES)
    reader = validate_source_reader(reference["reader"])
    _validate_reader_structure(reader, reference["data_structure"])
    return deepcopy(reference)


def validate_source_value(reference, value, *, path):
    reference = verify_source_reference(reference)
    validate_structure_value(reference["data_structure"], value, path=path)


def verified_source_contract(source):
    contract = source_contract(source)
    verify_json_contract(contract.value, schema_version=2, size_bytes=source.contract_size_bytes,
        sha256=source.contract_sha256, max_bytes=CONTENT_SOURCE_CONTRACT_MAX_BYTES)
    return contract


def source_reference(source, *, require_published=False):
    if source.status == "draft" or (require_published and source.status != "published"):
        raise ValidationError({"source_version": "新规则只能选择已发布来源版本"})
    contract = verified_source_contract(source)
    return {**contract.value, "sha256": contract.sha256}


def validate_rule_source(rule, *, require_published=False):
    if not rule.source_version_id:
        return
    reference = source_reference(rule.source_version, require_published=require_published)
    selector = reference["reader"]["selector"]
    if rule.source_selector != selector or rule.generation_source_selector != selector:
        raise ValidationError({"source_version": "来源标识必须与所选来源版本一致"})
    from studio.services.field_definitions import profile_rule_definition
    if rule.generation_mode not in {"passthrough", "model_generated", "translate_only"} or profile_rule_definition(rule).value_type not in {"text", "long_text"}:
        raise ValidationError({"source_version": "此来源版本提供受控文字；请选择文字字段及直接使用、生成或翻译"})


def _summary(source):
    return {"key": str(source.source_key), "version": source.version, "revision": source.revision,
        "status": source.status, "source_selector": source.reader_selector, "contract_sha256": source.contract_sha256}


@registry_reference_writer
def write_source(*, actor, data=None, source_id=None, expected_revision=None, action="update"):
    data = dict(data or {})
    if action not in {"update", "publish", "successor", "retire"} or (not source_id and action != "update"):
        raise ValidationError("无效的来源版本操作")
    if action != "update" and data:
        raise ValidationError("状态操作不可同时修改内容，请先保存并核对")
    source = ContentSourceVersion.objects.select_for_update().get(pk=source_id) if source_id else ContentSourceVersion()
    before = _summary(source) if source_id else None
    if source_id and (type(expected_revision) is not int or source.revision != expected_revision):
        raise ValidationError({"expected_revision": "来源已变化，请重新读取后再操作"}, code="source_revision_conflict")
    if source_id:
        verified_source_contract(source)
    if action == "successor":
        source_reference(source)
        latest = ContentSourceVersion.objects.filter(source_key=source.source_key).order_by("-version").first()
        if latest.pk != source.pk:
            raise ValidationError("请从最新版本创建下一版；不能重复创建草稿")
        source = ContentSourceVersion(source_key=source.source_key, version=source.version + 1,
            name=source.name, description=source.description, reader_selector=source.reader_selector,
            reader_contract=deepcopy(source.reader_contract), data_structure_version=source.data_structure_version)
        before = None
    elif action == "retire":
        if source.status != "published":
            raise ValidationError("仅已发布来源可退役")
        source_reference(source)
        source.status = "retired"
    else:
        if source.status != "draft":
            raise ValidationError("已发布／退役来源不可编辑，请创建下一版本")
        if action == "publish":
            if data:
                raise ValidationError("发布不可同时修改内容，请先保存并核对")
            if source.data_structure_version.status != "published":
                raise ValidationError({"data_structure_version": "发布新来源须选择已发布、未退役的结构版本"})
            source.status = "published"
            source.published_at = timezone.now()
            source.published_by = actor
        else:
            if set(data) - {"name", "description", "reader_selector", "data_structure_version"}:
                raise ValidationError("版本、权限和读取协议由平台维护，不能改写")
            for key, value in data.items():
                if key == "data_structure_version":
                    try:
                        value = DataStructureVersion.objects.get(pk=value, status="published")
                    except (DataStructureVersion.DoesNotExist, ValidationError, ValueError, TypeError) as exc:
                        raise ValidationError({key: "请选择已发布的确切结构版本"}) from exc
                setattr(source, key, value)
            if not source_id or ("reader_selector" in data and source.reader_selector != source.reader_contract["selector"]):
                source.reader_contract = reader_contract(source.reader_selector)
    contract = source_contract(source)
    source.contract_sha256, source.contract_size_bytes = contract.sha256, contract.size_bytes
    if source_id and action != "successor":
        source.revision += 1
    source.full_clean()
    source.save()
    record_platform_lifecycle_event(source, actor=actor, action=action if source_id else "create",
        reason_code="content_source." + (action if source_id else "create"),
        before_summary=before, after_summary=_summary(source))
    return source
