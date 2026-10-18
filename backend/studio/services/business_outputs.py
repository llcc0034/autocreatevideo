"""Versioned business outputs. No generated results or review facts live here."""
from copy import deepcopy
from uuid import UUID

from django.core.exceptions import ValidationError
from django.utils import timezone

from studio.models import BusinessOutputVersion, DataStructureVersion
from studio.services.data_structures import data_structure_snapshot, validate_data_structure_snapshot, validate_structure_value
from studio.services.json_contracts import DATABASE_JSON_FIELD_MAX_BYTES, json_contract, verify_json_contract
from studio.services.platform_lifecycle_audit import record_platform_lifecycle_event
from studio.services.registry_reference_lock import registry_reference_writer
from studio.services.script_semantics import SEMANTICS_KEY, freeze_script_semantics
from studio.services.visual_rules_output_contract import (visual_rule_output_mappings, visual_rule_response_example,
    normalize_visual_rule_allowed_values, constrain_visual_rule_example, visual_rule_field_sources,
    reconfigure_visual_rule_sources, validate_visual_rule_source_policy, visual_rule_response_keys,
    visual_rule_field_source_options)

PARSER_KEY = "visual_rules.v2"
JSON_PARSER_KEY = "structured_json.v1"


def parser_options():
    return [
        {"key": JSON_PARSER_KEY, "label": "通用 JSON 结构", "configuration": "structure",
            "description": "字段、层级和缺失规则来自所选结构；只解析数据，不自动创建剧本、Bible或镜头。"},
        {"key": SEMANTICS_KEY, "label": "剧本结构与语义映射", "configuration": "script_semantics",
            "description": "自定义响应结构映射为实体、变体和分镜声明；当前提供配置与预览，不创建业务对象或调用模型。"},
        {"key": PARSER_KEY, "label": "全剧影视规则", "configuration": "visual_rules",
            "description": "现有影视规则业务转换；只供声明支持该协议的执行入口使用。"},
    ]


def parser_contract(key):
    if key == JSON_PARSER_KEY:
        return {"key": key, "format": "json"}
    if key == SEMANTICS_KEY:
        return {"key": key, "format": "json", "mapping": {}}
    if key != PARSER_KEY:
        raise ValidationError({"parser_key": "请选择已实现的输出协议；不执行自定义脚本或路径"})
    return {"key": key, "operation": "visual_rules.generate", "response_schema_version": 2,
        "fields": visual_rule_output_mappings(), "response_example": visual_rule_response_example()}


def normalize_quality(value):
    from studio.services.production_visual_rules import _normalize_visual_rules_quality_advisories
    if value == {}:
        return {}
    return _normalize_visual_rules_quality_advisories(value)


def validate_parser_contract(value):
    """Validate supported semantics, not today's authoring labels or examples."""
    json_contract(value, max_bytes=DATABASE_JSON_FIELD_MAX_BYTES)
    if isinstance(value, dict) and value.get("key") == SEMANTICS_KEY:
        if set(value) != {"key", "format", "mapping"} or value["format"] != "json" or not isinstance(value["mapping"], dict):
            raise ValidationError("剧本输出需要JSON协议及显式语义映射")
        # The shared mapping compiler validates against the exact structure below.
        return deepcopy(value)
    if isinstance(value, dict) and value.get("key") == JSON_PARSER_KEY:
        if value != parser_contract(JSON_PARSER_KEY):
            raise ValidationError("通用JSON协议只引用结构版本，不接受内联字段、转换脚本或业务操作")
        return deepcopy(value)
    if (not isinstance(value, dict) or set(value) - {"allowed_values", "response_keys"} != {
            "key", "operation", "response_schema_version", "fields", "response_example"}
            or value["key"] != PARSER_KEY or value["operation"] != "visual_rules.generate"
            or type(value["response_schema_version"]) is not int or value["response_schema_version"] != 2
            or not isinstance(value["fields"], list) or not value["fields"]):
        raise ValidationError("输出解析合同缺失或不受支持；不会回退到当前目录")
    sources = visual_rule_field_sources(value)
    if "response_keys" in value and value["response_keys"] is None:
        raise ValidationError("response_keys 不能为 null")
    visual_rule_response_keys(sources, value.get("response_keys"))
    expected = {row["field"]: row for row in visual_rule_output_mappings(sources)}
    seen = set()
    for row in value["fields"]:
        if (not isinstance(row, dict) or set(row) != {"field", "label", "response_field", "transformation", "source_selector"}
                or not all(isinstance(item, str) and item.strip() for item in row.values())
                or row["field"] not in expected or row["field"] in seen
                or any(row[key] != expected[row["field"]][key] for key in ("response_field", "source_selector"))):
            raise ValidationError("输出字段映射不受当前解析器及下游读取方支持")
        seen.add(row["field"])
    # The example must satisfy the actual parser, but its creative values are
    # frozen version data, not a mutable registry default or a generated result.
    from studio.services.production_visual_rules import parse_visual_rules_output
    allowed = normalize_visual_rule_allowed_values(value.get("allowed_values", {}))
    parse_visual_rules_output(payload=value["response_example"], allowed_values=allowed, field_sources=sources,
        response_keys=value.get("response_keys"))
    return deepcopy(value)


def validate_output_structure(parser, structure):
    """Bind the implemented response reader to declared, typed structure nodes.

    This is not a second schema or an arbitrary business mapper: the shared
    compiler owns shape/use constraints; the parser owns business semantics.
    """
    validate_data_structure_snapshot(structure)
    if parser["key"] == SEMANTICS_KEY:
        freeze_script_semantics(source_contract=structure, mapping=parser["mapping"],
            max_bytes=DATABASE_JSON_FIELD_MAX_BYTES)
        return
    if parser["key"] == JSON_PARSER_KEY:
        return
    nodes = structure["nodes"]
    root = next(node for node in nodes if node["parent_key"] is None)
    if root["field_definition"]["value_type"] != "object":
        raise ValidationError({"data_structure_version": "当前 JSON 输出协议需要对象根节点"})
    children = {}
    for node in nodes:
        children.setdefault(node["parent_key"], []).append(node)
    formats = {choice["response_field"]: choice["value_type"]
        for row in visual_rule_field_source_options() for choice in row["choices"]}
    types = {"text": {"text", "long_text"}, "object": {"object"}, "array": {"list"}}
    sources = visual_rule_field_sources(parser)
    keys = visual_rule_response_keys(sources, parser.get("response_keys"))
    for field, path in keys.items():
        node = root
        for part in path.split("."):
            nested = children.get(node["node_key"], [])
            kind = node["field_definition"]["value_type"]
            node = (next((child for child in nested if child["property_name"] == part), None)
                if kind == "object" else nested[0] if kind == "list" and part.isdigit() else None)
            if node is None:
                raise ValidationError({f"response_keys.{field}": f"结构未声明响应位置 {path}，请调整结构或映射"})
        if node["field_definition"]["value_type"] not in types[formats[sources[field]]]:
            raise ValidationError({f"response_keys.{field}": f"{path} 的结构类型与所选响应格式不一致"})
    validate_structure_value(structure, parser["response_example"], path="parser.response_example")


def output_contract(output):
    if not isinstance(output.parser_contract, dict) or output.parser_key != output.parser_contract.get("key"):
        raise ValidationError("输出版本与解析合同不一致")
    validate_output_policy(output.parser_contract, output.quality_advisories)
    if not output.data_structure_version_id:
        raise ValidationError({"data_structure_version": "请选择已发布的结构版本；旧输出不会自动补配结构"})
    structure = data_structure_snapshot(output.data_structure_version)
    parser = validate_parser_contract(output.parser_contract)
    validate_output_structure(parser, structure)
    return json_contract({"schema_version": 2, "id": str(output.pk), "output_key": str(output.output_key),
        "version": output.version, "name": output.name, "description": output.description,
        "parser": parser, "data_structure": structure, "quality_advisories": normalize_quality(output.quality_advisories)},
        schema_version=2, max_bytes=DATABASE_JSON_FIELD_MAX_BYTES)


def output_reference(output, *, require_published=False):
    if output.status == "draft" or (require_published and output.status != "published"):
        raise ValidationError({"output_version": "请选择已发布的业务输出版本"})
    contract = verified_output_contract(output)
    return {**contract.value, "sha256": contract.sha256}


def verified_output_contract(output):
    contract = output_contract(output)
    verify_json_contract(contract.value, schema_version=2, size_bytes=output.contract_size_bytes,
        sha256=output.contract_sha256, max_bytes=DATABASE_JSON_FIELD_MAX_BYTES)
    return contract


def verify_output_reference(reference):
    if not isinstance(reference, dict) or set(reference) != {
        "schema_version", "id", "output_key", "version", "name", "description", "parser", "data_structure", "quality_advisories", "sha256"
    }:
        raise ValidationError("业务输出版本快照不完整")
    contract = json_contract({key: value for key, value in reference.items() if key != "sha256"},
        schema_version=2, max_bytes=DATABASE_JSON_FIELD_MAX_BYTES)
    try:
        if any(str(UUID(reference[key])) != reference[key] for key in ("id", "output_key")):
            raise ValueError()
    except (ValueError, TypeError, AttributeError):
        raise ValidationError("业务输出版本身份无效") from None
    if (type(reference["schema_version"]) is not int or reference["schema_version"] != 2
            or type(reference["version"]) is not int or reference["version"] < 1
            or not isinstance(reference["name"], str) or not reference["name"].strip() or len(reference["name"]) > 120
            or not isinstance(reference["description"], str) or len(reference["description"]) > 1000
            or not isinstance(reference["parser"], dict) or reference["sha256"] != contract.sha256
            or reference["parser"] != validate_parser_contract(reference["parser"])
            or normalize_quality(reference["quality_advisories"]) != reference["quality_advisories"]):
        raise ValidationError("业务输出版本快照或协议不一致")
    validate_output_policy(reference["parser"], reference["quality_advisories"])
    validate_output_structure(reference["parser"], reference["data_structure"])
    return deepcopy(reference)


def visual_rule_review_contract(reference):
    """Read-only projection of the exact producer contract, never today's catalog."""
    reference = verify_output_reference(reference)
    parser = reference["parser"]
    if parser["key"] != PARSER_KEY:
        raise ValidationError("所选输出不是影视规则业务协议；不能按固定影视字段解释通用结构")
    keys = visual_rule_response_keys(visual_rule_field_sources(parser), parser.get("response_keys"))
    return {"schema_version": 1,
        "output": {key: reference[key] for key in ("id", "output_key", "version", "name", "sha256")},
        "fields": [{"key": row["field"], "label": row["label"], "value_type": "text",
            "allow_blank": row["field"] == "negative_prompt", "source_selector": row["source_selector"],
            "response_path": keys[row["field"]], "response_format": row["response_field"],
            "transformation": row["transformation"]} for row in parser["fields"]]}


def _summary(output):
    return {"key": str(output.output_key), "version": output.version, "revision": output.revision,
        "status": output.status, "parser_key": output.parser_key, "contract_sha256": output.contract_sha256}


def validate_output_policy(parser, quality):
    if parser.get("key") in (JSON_PARSER_KEY, SEMANTICS_KEY):
        if quality != {}:
            raise ValidationError({"quality_advisories": "JSON结构协议使用结构中的字段规则，不接受影视规则专用质量配置"})
    else:
        validate_visual_rule_source_policy(visual_rule_field_sources(parser), parser.get("allowed_values", {}), quality)


@registry_reference_writer
def write_output(*, actor, data=None, output_id=None, expected_revision=None, action="update"):
    data = dict(data or {})
    output = BusinessOutputVersion.objects.select_for_update().get(pk=output_id) if output_id else BusinessOutputVersion()
    before = _summary(output) if output_id else None
    if output_id and (type(expected_revision) is not int or output.revision != expected_revision):
        raise ValidationError({"expected_revision": "输出定义已变化，请重新读取"}, code="output_revision_conflict")
    if output_id:
        verified_output_contract(output)
    if action == "successor":
        output_reference(output)
        latest = BusinessOutputVersion.objects.filter(output_key=output.output_key).order_by("-version").first()
        if latest.pk != output.pk:
            raise ValidationError("请从最新版本创建下一版，不能重复创建草稿")
        output = BusinessOutputVersion(output_key=output.output_key, version=output.version + 1,
            name=output.name, description=output.description, parser_key=output.parser_key,
            data_structure_version=output.data_structure_version,
            parser_contract=deepcopy(output.parser_contract), quality_advisories=deepcopy(output.quality_advisories))
        before = None
    elif action == "retire":
        if output.status != "published":
            raise ValidationError("仅已发布的输出版本可退役")
        output_reference(output)
        output.status = "retired"
    else:
        if output.status != "draft":
            raise ValidationError("已发布／退役的输出版本不可修改，请创建下一版")
        if action == "publish":
            if data:
                raise ValidationError("发布前请先保存，不可同时修改内容")
            if output.data_structure_version.status != "published":
                raise ValidationError({"data_structure_version": "发布新输出须选择已发布、未退役的结构版本"})
            output.status, output.published_at, output.published_by = "published", timezone.now(), actor
        elif action == "update":
            if set(data) - {"name", "description", "parser_key", "data_structure_version", "quality_advisories", "allowed_values", "field_sources", "response_keys", "semantic_mapping"}:
                raise ValidationError("身份、版本与解析协议结构由平台维护")
            has_semantic_mapping = "semantic_mapping" in data
            semantic_mapping = data.pop("semantic_mapping", None)
            has_allowed_values = "allowed_values" in data
            allowed_values = data.pop("allowed_values", None)
            has_field_sources = "field_sources" in data
            field_sources = data.pop("field_sources", None)
            has_response_keys = "response_keys" in data
            response_keys = data.pop("response_keys", None)
            if has_response_keys and response_keys is None:
                raise ValidationError("response_keys 不能为 null")
            if has_field_sources and field_sources is None:
                raise ValidationError("field_sources 不能为 null")
            if output_id and "parser_key" in data and data["parser_key"] != output.parser_key:
                raise ValidationError({"parser_key": "已有输出身份的协议不可切换；请新建独立输出定义"})
            for key, value in data.items():
                if key == "data_structure_version":
                    try:
                        value = DataStructureVersion.objects.get(pk=value, status="published")
                    except (DataStructureVersion.DoesNotExist, ValidationError, ValueError, TypeError) as exc:
                        raise ValidationError({key: "请选择已发布的确切结构版本"}) from exc
                setattr(output, key, value)
            if not output_id:
                output.parser_contract = parser_contract(output.parser_key)
            if has_semantic_mapping:
                if output.parser_key != SEMANTICS_KEY or not isinstance(semantic_mapping, dict):
                    raise ValidationError({"semantic_mapping": "仅剧本语义协议接受显式映射对象，不能为null"})
                output.parser_contract = {**output.parser_contract, "mapping": deepcopy(semantic_mapping)}
            if output.parser_key in (JSON_PARSER_KEY, SEMANTICS_KEY) and (has_allowed_values or has_field_sources or has_response_keys):
                raise ValidationError("JSON结构协议不接受影视规则专用字段映射")
            if has_field_sources or has_response_keys:
                output.parser_contract = reconfigure_visual_rule_sources(output.parser_contract,
                    field_sources if has_field_sources else visual_rule_field_sources(output.parser_contract), response_keys)
            if output.parser_key == PARSER_KEY:
                validate_visual_rule_source_policy(visual_rule_field_sources(output.parser_contract),
                    allowed_values if has_allowed_values else output.parser_contract.get("allowed_values", {}), output.quality_advisories)
            if has_allowed_values:
                allowed_values = normalize_visual_rule_allowed_values(allowed_values)
                output.parser_contract = deepcopy(output.parser_contract)
                if allowed_values:
                    output.parser_contract["allowed_values"] = allowed_values
                else:
                    output.parser_contract.pop("allowed_values", None)
                output.parser_contract["response_example"] = constrain_visual_rule_example(
                    output.parser_contract["response_example"], allowed_values,
                    visual_rule_response_keys(visual_rule_field_sources(output.parser_contract),
                        output.parser_contract.get("response_keys"))["color_script"])
        else:
            raise ValidationError("不支持的输出版本操作")
    if not isinstance(output.parser_contract, dict) or output.parser_key != output.parser_contract.get("key"):
        raise ValidationError({"parser_key": "输出协议与已保存的解析合同不一致"})
    output.quality_advisories = normalize_quality(output.quality_advisories)
    contract = output_contract(output)
    output.contract_sha256, output.contract_size_bytes = contract.sha256, contract.size_bytes
    if output_id and action != "successor":
        output.revision += 1
    output.full_clean()
    output.save()
    record_platform_lifecycle_event(output, actor=actor, action=action if output_id else "create",
        reason_code="business_output." + (action if output_id else "create"), before_summary=before,
        after_summary=_summary(output))
    return output
