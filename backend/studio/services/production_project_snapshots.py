"""Recipe project inputs use the original signed preview and Run receipt.

No live read, implicit confirmation, new snapshot store or provider dispatch.
The recipe declares only the reader kind; its exact instance owns the selector.
"""
from copy import deepcopy
from uuid import UUID

from django.core.exceptions import ValidationError

from studio.services.execution_definitions import stored_execution_contract, verify_current_execution_definition
from studio.services.execution_input_suggestions import resolve_project_source_reference
from studio.services.execution_project_sources import input_project_reader
from studio.services.json_contracts import DATABASE_JSON_FIELD_MAX_BYTES, json_contract


def normalize_project_source_confirmations(rows):
    json_contract(rows, max_bytes=DATABASE_JSON_FIELD_MAX_BYTES)
    if not isinstance(rows, list):
        raise ValidationError({"project_source_confirmations": "项目来源确认必须是列表"})
    result, seen = [], set()
    for row in rows:
        if (not isinstance(row, dict) or set(row) != {"execution_instance_version_id", "input_key", "reference"}
                or not isinstance(row["input_key"], str) or not row["input_key"]
                or not isinstance(row["reference"], str) or not row["reference"]):
            raise ValidationError({"project_source_confirmations": "需要精确实例版本、输入标识和预览确认凭据，不接收正文"})
        try:
            if not isinstance(row["execution_instance_version_id"], str):
                raise ValueError
            version = str(UUID(row["execution_instance_version_id"]))
        except (ValueError, TypeError, AttributeError) as exc:
            raise ValidationError({"project_source_confirmations": "实例版本必须是正式 UUID"}) from exc
        identity = (version, row["input_key"])
        if identity in seen:
            raise ValidationError({"project_source_confirmations": "同一精确实例输入不能重复确认"})
        seen.add(identity)
        result.append({**deepcopy(row), "execution_instance_version_id": version})
    return sorted(result, key=lambda row: (row["execution_instance_version_id"], row["input_key"]))


def validate_recipe_project_confirmations(stages, confirmations, *, owner, project_id):
    """Require exactly the snapshots used by every possible frozen route."""
    expected, definitions = {}, {}
    from studio.services.production_recipe_contracts import recipe_stage_routings
    for stage in stages:
        for route in (route for _, routing in recipe_stage_routings(stage) for route in routing["routes"]):
            keys = [key for key, source in route["input_sources"].items() if source.get("kind") == "project_snapshot"]
            if not keys:
                continue
            version = route["execution_instance_version_id"]
            if version not in definitions:
                definitions[version] = verify_current_execution_definition(stored_execution_contract(version))
            definition = definitions[version]
            if definition.sha256 != route["expected_definition_sha256"]:
                raise ValidationError("项目来源的执行合同已变化，请重新读取并确认")
            specs = {row["key"]: row for row in definition.value["configuration"]["inputs"]}
            for key in keys:
                spec = specs.get(key)
                reader = input_project_reader(spec, definition.value["interface"]) if spec else None
                if route["input_sources"][key] != {"kind": "project_snapshot"} or reader is None:
                    raise ValidationError("配方项目来源必须由精确输入合同声明，不接收自填路径或凭据")
                expected[(version, key)] = (definition, reader)
    actual = {(row["execution_instance_version_id"], row["input_key"]): row for row in confirmations}
    if actual.keys() != expected.keys():
        raise ValidationError({"project_source_confirmations": "请预览并确认配方全部项目来源；不能缺少确认或附加未使用的来源"})
    for identity, (definition, reader) in expected.items():
        resolve_project_source_reference({"kind": "project_snapshot", "reference": actual[identity]["reference"]},
            reader=reader, definition=definition, input_key=identity[1], owner=owner, project_id=project_id)


def recipe_project_snapshot_source(*, run, plan, definition, input_key):
    """Bind only the original creation receipt; the shared resolver checks access."""
    from studio.services.production_creation_inputs import verified_recipe_creation_request
    intent, saved_plan, _ = verified_recipe_creation_request(run)
    if saved_plan.sha256 != plan.sha256:
        raise ValidationError("项目来源确认不属于此冻结运行计划")
    rows = normalize_project_source_confirmations(intent.get("project_source_confirmations"))
    matches = [row for row in rows if row["execution_instance_version_id"] == definition.value["version_id"]
        and row["input_key"] == input_key]
    if len(matches) != 1:
        raise ValidationError({f"input_sources.{input_key}": "本轮缺少对应项目来源确认，不能读取最新项目或以固定内容代替"})
    return {"kind": "project_snapshot", "reference": matches[0]["reference"]}
