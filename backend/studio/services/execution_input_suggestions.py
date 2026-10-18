"""Explicit previews shared by editable copies and governed project snapshots.

The signed preview is a source receipt, not an authorization credential. The
literal path governs editable copies; project readers require an unedited,
signed snapshot and fresh authorization before saving the preparation.
"""
from copy import deepcopy

from django.core import signing
from django.core.exceptions import ValidationError
from django.utils import timezone

from studio.services.json_contracts import json_contract
from studio.services.execution_instances import execution_configuration_max_bytes


_SALT = "studio.execution-input-suggestion.v1"
_PROJECT_REFERENCES = {"project.logline": "使用项目梗概", "project.visual_style": "使用项目视觉风格"}


def suggestion_options(references, schemas):
    from studio.services.execution_bible_suggestions import BIBLE_INPUT_REFERENCES
    result = [{"key": key, "label": _PROJECT_REFERENCES[key], "requires_project": True}
        for key in references if key in _PROJECT_REFERENCES]
    result.extend({"key": key, "label": BIBLE_INPUT_REFERENCES[key], "requires_project": True,
        "requires_production_scope": True, "prefill": key == "production.entity.description"}
        for key in references if key in BIBLE_INPUT_REFERENCES)
    examples = list(dict.fromkeys(value for schema in schemas if isinstance(schema.get("examples"), list) for value in schema["examples"]
        if isinstance(value, str) and value.strip()))
    result.extend({"key": f"example:{index}", "label": "查看填写范例" if len(examples) == 1 else f"查看填写范例 {index + 1}",
        "requires_project": False, "example": value} for index, value in enumerate(examples))
    return result


def preview_execution_input_suggestion(contract, *, owner, project_id, input_key, suggestion_key,
        production_run_id=None, production_scope=None):
    options = contract["input_forms"].get(input_key, {}).get("suggestions", [])
    choice = next((row for row in options if row["key"] == suggestion_key), None)
    if choice is None:
        raise ValidationError("此输入没有登记所选引用或范例，请重新读取执行实例")
    from studio.services.execution_project_sources import input_project_reader, authorized_project, read_project_source
    spec = next(row for row in contract["definition"]["configuration"]["inputs"] if row["key"] == input_key)
    reader = input_project_reader(spec, contract["definition"]["interface"])
    if project_id is not None:
        authorized_project(owner, project_id)
    if choice["requires_project"] and project_id is None:
        raise ValidationError("引用项目内容需要明确当前项目")
    origin = {"selector": suggestion_key, "label": choice["label"]}
    if choice.get("requires_production_scope"):
        from studio.services.execution_bible_suggestions import read_bible_input_reference
        content, source_origin = read_bible_input_reference(owner=owner, project_id=project_id,
            run_id=production_run_id, scope=production_scope, selector=suggestion_key)
        origin.update(source_origin)
        origin["label"] = f'{choice["label"]} · {origin["source_name"]} · Bible 审核 v{origin["review"]["revision"]}'
        if "setting" in source_origin:
            origin["label"] += f' · 主图设定 v{source_origin["setting"]["revision"]}'
    elif reader or suggestion_key in _PROJECT_REFERENCES:
        selector = reader["selector"] if reader else {"project.visual_style": "style.visual_prompt"}.get(suggestion_key, suggestion_key)
        if reader and suggestion_key != selector:
            raise ValidationError("预览来源与冻结读取合同不一致")
        content, source_origin = read_project_source(owner, project_id, selector)
        origin.update(source_origin)
    else:
        content = choice["example"]
    if not content.strip():
        raise ValidationError("所选内容尚未填写，请先保存项目梗概或风格视觉提示词，也可直接手工填写本轮要求")
    receipt = {"schema_version": 1, "owner_id": str(owner.pk), "project_id": str(project_id) if project_id else None,
        "definition_sha256": contract["sha256"], "input_key": input_key, "origin": origin,
        "content": content, "content_sha256": json_contract(content, max_bytes=execution_configuration_max_bytes()).sha256,
        "previewed_at": timezone.now().isoformat()}
    if reader:
        receipt["source_reader"] = reader
    json_contract(receipt, max_bytes=execution_configuration_max_bytes())
    return {"input_key": input_key, "definition_sha256": contract["sha256"], "label": origin["label"],
        "content": content, "reference": signing.dumps(receipt, salt=_SALT), "origin": origin}


def resolve_project_source_reference(source, *, reader, definition, input_key, owner, project_id):
    if set(source) != {"kind", "reference"} or source.get("kind") != "project_snapshot" or not isinstance(source.get("reference"), str):
        raise ValidationError({input_key: "项目来源必须先预览确认，不接受手工正文或任意来源路径"})
    try:
        receipt = signing.loads(source["reference"], salt=_SALT)
    except (signing.BadSignature, ValueError, TypeError) as exc:
        raise ValidationError({input_key: "项目来源凭据无效，请重新预览"}) from exc
    if (not isinstance(receipt, dict) or receipt.get("schema_version") != 1
            or receipt.get("owner_id") != str(owner.pk) or receipt.get("input_key") != input_key
            or receipt.get("definition_sha256") != definition.sha256 or receipt.get("source_reader") != reader):
        raise ValidationError({input_key: "项目来源凭据与当前用户、输入或精确读取合同不一致"})
    json_contract(receipt, max_bytes=execution_configuration_max_bytes())
    from studio.services.execution_project_sources import verify_project_source_access
    verify_project_source_access(owner, receipt, project_id)
    value = receipt["content"]
    if not isinstance(value, str) or not value.strip() or json_contract(value, max_bytes=execution_configuration_max_bytes()).sha256 != receipt["content_sha256"]:
        raise ValidationError({input_key: "项目预览正文与摘要不一致"})
    return value, {"kind": "project_snapshot", "source_reader": deepcopy(reader),
        "scope": {"kind": "project", "id": receipt["project_id"]}, "previewed_at": receipt["previewed_at"],
        "source_reference": deepcopy(receipt["origin"]), "content_sha256": receipt["content_sha256"], "edited": False}


def verify_input_suggestion_reference(token, *, definition, input_key, value, owner, project_id,
        production_run_id=None, production_scope=None, eligible_stage_ids=()):
    if not isinstance(token, str) or not isinstance(value, str):
        raise ValidationError("输入引用凭据和正文必须是文本")
    try:
        receipt = signing.loads(token, salt=_SALT)
    except (signing.BadSignature, ValueError, TypeError) as exc:
        raise ValidationError("引用凭据无效，请重新预览并引用内容") from exc
    if (receipt.get("schema_version") != 1 or receipt.get("owner_id") != str(owner.pk)
            or receipt.get("project_id") != (str(project_id) if project_id else None)
            or receipt.get("definition_sha256") != definition.sha256 or receipt.get("input_key") != input_key):
        raise ValidationError("引用凭据与当前用户、项目或精确输入合同不一致，请重新引用")
    from studio.services.execution_bible_suggestions import BIBLE_INPUT_REFERENCES, read_bible_input_reference
    origin = receipt["origin"]
    if isinstance(origin.get("selector"), str) and origin["selector"].startswith("production.entity."):
        if origin["selector"] not in BIBLE_INPUT_REFERENCES:
            raise ValidationError("Bible 引用字段已不在当前目录，请重新选择；不转换旧字段凭据")
        if (origin.get("production_run_id") != str(production_run_id)
                or origin.get("production_scope") != production_scope):
            raise ValidationError("Bible 引用不属于本次制作轮或对象，请重新读取")
        content, current = read_bible_input_reference(owner=owner, project_id=project_id,
            run_id=production_run_id, scope=production_scope, selector=origin["selector"],
            eligible_stage_ids=eligible_stage_ids, decision_id=origin["review"]["decision_id"])
        if any(origin.get(key) != val for key, val in current.items()) or content != receipt["content"]:
            raise ValidationError("Bible 来源或审核依据已变化，请重新读取并核对；未覆盖本次修改")
    # Preserve exactly what was previewed. The edited value is not a new
    # approval or a write back to its source.
    # The actual editable value remains the ordinary literal, never re-read here.
    json_contract(receipt, max_bytes=execution_configuration_max_bytes())
    return {"kind": "literal", "reference": {**deepcopy(receipt["origin"]),
        "project_id": receipt["project_id"], "previewed_at": receipt["previewed_at"],
        "content": receipt["content"], "content_sha256": receipt["content_sha256"],
        "edited": value != receipt["content"]}}
