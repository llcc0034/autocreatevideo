"""Explicit creation facts from the original Run request, never live defaults.

The Run's immutable request receipt owns these values. Recipes select a named
field; shared preparation resolves it with Run/project ownership and provenance.
This is not a second input table, a model-specific rule, or a new execution API.
"""
from copy import deepcopy

from django.core.exceptions import ValidationError

from studio.services.json_contracts import json_contract, verify_json_contract
from studio.services.security_boundaries import boundary_int


CREATION_TEXT_FIELDS = {"idea": "本轮创意", "style_hint": "本轮风格要求"}
CREATION_INTEGER_FIELDS = {"episode_count": "本轮计划集数", "shots_per_episode": "本轮每集计划镜数",
    "episode_duration_seconds": "本轮单集时长预算（秒）"}


def creation_input_options():
    return [{"selector_key": f"run.{key}", "label": label, "kind": kind,
        "scope_kind": "project", "revision_kind": "production_request",
        **({"value_schema": {"type": "integer", "minimum": 1}} if kind == "json" else {}),
        "description": "读取本轮创建时明确提交并冻结的值；配方按输入映射使用，不读取项目默认值或其他运行。"}
        for kind, fields in (("text", CREATION_TEXT_FIELDS), ("json", CREATION_INTEGER_FIELDS))
        for key, label in fields.items()]


def validate_creation_inputs(values):
    if not isinstance(values, dict) or set(values) != set(CREATION_TEXT_FIELDS) | set(CREATION_INTEGER_FIELDS):
        raise ValidationError("本轮创建输入必须明确声明创意、风格、集数、镜数和时长；未提供的数量用空值，不猜默认值")
    if any(not isinstance(values[key], str) for key in CREATION_TEXT_FIELDS):
        raise ValidationError("本轮创意和风格要求必须是文本")
    if any(values[key] is not None and (type(values[key]) is not int or values[key] < 1) for key in CREATION_INTEGER_FIELDS):
        raise ValidationError("本轮数量和时长必须是明确正整数或空值")
    return deepcopy(values)


def validate_recipe_creation_mappings(stages, values):
    """Every possible script route must consume the supplied planning facts.

    Explicit literals remain recipe facts; they never overwrite or impersonate
    user inputs. Missing or unused creation values fail before a root is saved.
    """
    from studio.services.execution_structure_mapping import structure_leaves
    from studio.services.production_recipe_routing import recipe_routing
    supplied = {key for key, value in values.items() if value is not None and value != ""}
    for stage in stages:
        if stage["key"] not in {"script_generate", "script_structure"} or stage["mode"] != "ai":
            continue
        for index, route in enumerate(recipe_routing(stage)["routes"]):
            leaves = [leaf for source in route["input_sources"].values()
                for leaf in (structure_leaves(source["template"]) if source["kind"] == "structured" else [source])]
            selected = {leaf["selector_key"].removeprefix("run.") for leaf in leaves
                if leaf.get("kind") == "context_field" and leaf["selector_key"].startswith("run.")}
            missing = {key for key in selected if key not in values or values[key] is None}
            if missing or supplied - selected:
                raise ValidationError({f"{stage['key']}.routes[{index}].input_sources":
                    "本轮填写内容必须由每条可执行路线明确使用；未映射：" + ", ".join(sorted(supplied - selected))
                    + "；未填写：" + ", ".join(sorted(missing))})


def verified_recipe_creation_request(run):
    """Only the new recipe creation receipt; no old root/snapshot fallback."""
    from studio.services.production_run_plans import get_run_execution_plan
    maximum = boundary_int("security.snapshot.stage_max_bytes")
    snapshot = verify_json_contract(run.input_snapshot, schema_version=run.input_snapshot_schema_version,
        size_bytes=run.input_snapshot_size_bytes, sha256=run.input_snapshot_sha256, max_bytes=maximum).value
    if (not isinstance(snapshot, dict) or set(snapshot) != {"schema_version", "request_intent", "run_plan_sha256"}
            or type(snapshot["schema_version"]) is not int or snapshot["schema_version"] != 4):
        raise ValidationError("本轮缺少正式创建请求回执，不能从旧快照或项目配置补造输入")
    intent = snapshot["request_intent"]
    if (not isinstance(intent, dict) or type(intent.get("schema_version")) is not int or intent["schema_version"] != 4
            or intent.get("plan_source") != "recipe" or run.plan_source != "recipe"
            or intent.get("project_id") != str(run.project_id) or intent.get("recipe_version_id") != str(run.recipe_version_id)
            or json_contract(intent, max_bytes=maximum).sha256 != run.request_sha256):
        raise ValidationError("本轮创建请求与正式运行身份不一致")
    plan = get_run_execution_plan(run)
    if plan.sha256 != snapshot["run_plan_sha256"]:
        raise ValidationError("创建请求回执与本轮冻结执行计划不一致")
    validate_creation_inputs(intent.get("creation_inputs"))
    return intent, plan, {
        "production_run_id": str(run.pk), "request_sha256": run.request_sha256,
        "input_snapshot_sha256": run.input_snapshot_sha256, "run_plan_sha256": plan.sha256}


def verified_creation_inputs(run):
    intent, _, evidence = verified_recipe_creation_request(run)
    return deepcopy(intent["creation_inputs"]), evidence


def verify_recipe_stage_rows(run, plan):
    expected = [(index, row["key"], row["mode"])
        for index, row in enumerate(plan.value["recipe"]["definition"]["stages"], 1)]
    if list(run.stages.order_by("ordinal").values_list("ordinal", "stage_key", "mode")) != expected:
        raise ValidationError("原运行阶段与冻结计划不完整或不一致，不能通过重放补造")


def recipe_unattended_authorized(run):
    from studio.services.production_run_plans import get_run_execution_plan
    plan = get_run_execution_plan(run)
    if plan.value["source"] != "recipe" or plan.value["recipe"]["definition"]["interaction_mode"] != "unattended":
        return False
    intent, _, _ = verified_recipe_creation_request(run)
    if type(intent.get("unattended_confirmed")) is not bool:
        raise ValidationError("本轮创建回执缺少明确的无人值守授权")
    return intent["unattended_confirmed"]
