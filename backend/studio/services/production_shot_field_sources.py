"""Exact independent shot-field facts for the common input assembler.

No new field dictionary, composition language or prompt compiler lives here.
The production adapter verifies the original review first. Selection addresses
one M1 structure/node/definition version; values remain raw JSON. A repeated
node is ambiguous, not an instruction to take the first item or join its text.
"""
from copy import deepcopy
from uuid import UUID

from django.core.exceptions import ValidationError

from studio.services.json_contracts import JsonExpansionBudget, json_contract
from studio.services.shot_field_revisions import read_frozen_shot_field_revision


def validate_shot_field_selection(selection):
    """The same exact identity at authoring and at the frozen fact reader."""
    keys = {"structure_version_id", "node_key", "field_definition_version_id"}
    if (not isinstance(selection, dict) or set(selection) != keys
            or not isinstance(selection["node_key"], str) or not selection["node_key"]):
        raise ValidationError("字段来源须明确选择结构版本、节点和字段定义版本")
    for key in ("structure_version_id", "field_definition_version_id"):
        try:
            if not isinstance(selection[key], str):
                raise ValueError
            UUID(selection[key])
        except (TypeError, ValueError, AttributeError) as exc:
            raise ValidationError("字段来源版本必须为确切 UUID，不接受名称或最新版本") from exc


def select_shot_field(detail, selection, *, max_bytes):
    """Select a single already verified slot, keeping original/default evidence."""
    validate_shot_field_selection(selection)
    matches = [field for field in detail["fields"] if
        field["original"]["structure_version_id"] == selection["structure_version_id"]
        and field["original"]["node_key"] == selection["node_key"]
        and field["original"]["field_definition"]["field_definition_version_id"] == selection["field_definition_version_id"]]
    if not matches:
        raise ValidationError("本镜原结构没有匹配的字段身份；不能用同名字段或固定列替代")
    if len(matches) != 1:
        raise ValidationError("所选节点在本镜出现多次，不能默认取第一项；请选择其列表字段再显式逐条映射")
    field = matches[0]
    if not field["present"]:
        raise ValidationError("所选独立字段缺失；不会替换成 null、空文本或当前字典默认值")
    value = json_contract(field["value"], max_bytes=max_bytes)
    return deepcopy(value.value), {
        "source": deepcopy(detail["source"]), "source_sha256": detail["source_sha256"],
        "field_revision": deepcopy(detail["revision"]), "overridden": field["overridden"],
        "field": {key: deepcopy(item) for key, item in field["original"].items() if key != "value"},
        "value_schema": deepcopy(field["schema"]), "value_sha256": value.sha256,
    }


def read_reviewed_shot_field_sources(*, run, scope, eligible_stage_ids, decision_id, selections, max_bytes):
    """Read a group once under the existing production review/scope boundary.

    Return ordered facts for the shared assembler, not preassembled prompt
    text. All values are loaded only on demand, once per shot/review, with an
    aggregate budget; no audit, approval, preparation or model call is created.
    """
    from studio.services.production_execution_units import resolve_production_unit
    from studio.services.production_reviewed_content import read_reviewed_storyboard, reviewed_shot_row

    if not isinstance(selections, list) or not selections:
        raise ValidationError("需要明确选择至少一个独立字段，不自动读取整镜字段")
    try:
        if not isinstance(decision_id, str):
            raise ValueError
        UUID(decision_id)
    except (TypeError, ValueError, AttributeError) as exc:
        raise ValidationError("需要明确的原分镜审核决定，不查找最新审核") from exc
    if not isinstance(scope, dict) or set(scope) != {"kind", "id"} or scope["kind"] != "shot":
        raise ValidationError("独立字段来源需要明确的本轮镜头范围")
    json_contract(selections, max_bytes=max_bytes)
    unit = resolve_production_unit(run, scope)
    reviewed = read_reviewed_storyboard(run=run, eligible_stage_ids=eligible_stage_ids, decision_id=decision_id)
    row = reviewed_shot_row(reviewed, unit)
    detail = read_frozen_shot_field_revision(shot_id=scope["id"], project_id=run.project_id,
        source_reference=row.get("script_source"), field_revision=row.get("creative_field_revision"), max_bytes=max_bytes)
    results = []
    budget = JsonExpansionBudget(max_bytes, path="shot_field_sources")
    for selection in selections:
        value, origin = select_shot_field(detail, selection, max_bytes=max_bytes)
        result = {"value": value, "origin": {**origin, "review": deepcopy(reviewed["review"]), "unit": deepcopy(unit)}}
        budget.consume(result)
        results.append(result)
    return json_contract(results, max_bytes=max_bytes)
