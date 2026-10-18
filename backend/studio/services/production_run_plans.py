"""Freeze and read a Run's sole execution plan, never reconstruct history.

Recipe publication owns recipe executability. This boundary verifies its exact
published bytes and structural identity; it does not admit a model call, resolve
future inputs, adopt content, approve a stage or manufacture a Recipe.
"""
from copy import deepcopy
from hashlib import sha256
from uuid import UUID

from django.core.exceptions import PermissionDenied, ValidationError
from django.db import transaction

from studio.models import Episode, ProductionRecipeVersion, ProductionRun, ProductionRunPlan, ScriptRevision, SeriesProject
from studio.services.json_contracts import DATABASE_JSON_FIELD_MAX_BYTES, assign_json_contract, json_contract, verify_json_contract
from studio.services.production_recipe_contracts import recipe_stage_declarations
from studio.services.production_stage_catalog import (
    PRODUCTION_DELIVERY_KINDS, production_stage_catalog,
)


def _invalid(path, message):
    error = ValidationError({path: message})
    error.code = "run_plan_source_conflict"
    raise error


def _object(value, keys, path):
    if not isinstance(value, dict) or set(value) != set(keys):
        _invalid(path, "必须明确提供全部合同字段，不能混入旧字段或任意执行器")


def _id(value, path):
    if not isinstance(value, str):
        _invalid(path, "需要正式对象 UUID")
    try:
        return str(UUID(value))
    except ValueError:
        _invalid(path, "需要正式对象 UUID")


def _digest(value, path):
    if not isinstance(value, str) or len(value) != 64 or any(c not in "0123456789abcdef" for c in value):
        _invalid(path, "需要明确的 SHA-256 摘要")
    return value


def _scope_shape(scope):
    if not isinstance(scope, dict) or scope.get("kind") not in ("project", "episodes"):
        _invalid("scope", "请选择项目范围或明确剧集，不用空数组表示全部剧集")
    _object(scope, {"kind"} if scope["kind"] == "project" else {"kind", "episode_ids"}, "scope")
    if scope["kind"] == "project":
        return {"kind": "project"}
    ids = scope["episode_ids"]
    if not isinstance(ids, list) or not ids:
        _invalid("scope.episode_ids", "剧集范围必须是非空数组")
    ids = [_id(value, "scope.episode_ids") for value in ids]
    if len(set(ids)) != len(ids):
        _invalid("scope.episode_ids", "不能重复指定同一剧集")
    return {"kind": "episodes", "episode_ids": sorted(ids)}


def _delivery_shape(delivery):
    _object(delivery, {"kind"}, "delivery")
    if delivery["kind"] not in PRODUCTION_DELIVERY_KINDS:
        _invalid("delivery.kind", "当前交付合同仅支持逐集母版和正式发布包")
    return deepcopy(delivery)


def confirmed_script_reference(project_id, content):
    """A reference to a confirmed original, not a copied or auto-approved script."""
    if content is None:
        return None
    _object(content, {"kind", "id", "expected_sha256"}, "initial_content")
    if content["kind"] != "script_revision":
        _invalid("initial_content.kind", "初始剧本请选择正式已确认修订；其他内容在对应步骤采用")
    revision_id = _id(content["id"], "initial_content.id")
    expected = _digest(content["expected_sha256"], "initial_content.expected_sha256")
    row = ScriptRevision.objects.filter(pk=revision_id, source__project_id=project_id).values(
        "id", "source_id", "revision", "status", "confirmed_by_id", "confirmed_at", "content",
        "content_sha256", "content_size_bytes",
    ).first()
    if (row is None or row["status"] != "confirmed" or not row["confirmed_by_id"] or not row["confirmed_at"]):
        _invalid("initial_content", "所选剧本不存在、不属于本项目或尚未正式确认")
    data = row["content"].encode("utf-8")
    if not data.strip() or sha256(data).hexdigest() != expected or row["content_sha256"] != expected or len(data) != row["content_size_bytes"]:
        _invalid("initial_content", "剧本正文与确认的摘要不一致，请核对原始修订")
    return {"kind": "script_revision", "id": revision_id, "source_id": str(row["source_id"]),
        "revision": row["revision"], "sha256": expected, "size_bytes": len(data),
        "confirmed_by_id": str(row["confirmed_by_id"]), "confirmed_at": row["confirmed_at"].isoformat()}




def _recipe_stages_shape(definition, catalog):
    try:
        return recipe_stage_declarations(definition, catalog)
    except ValidationError as exc:
        _invalid("recipe.definition", exc.messages)


def _recipe_snapshot(recipe_id, catalog):
    row = ProductionRecipeVersion.objects.filter(pk=recipe_id).only(
        "id", "status", "definition", "definition_schema_version", "definition_size_bytes", "definition_sha256",
    ).get()
    if row.status != "published" or row.definition_schema_version != 3:
        _invalid("recipe_version_id", "必须选择已发布的 schema3 精确配方版本")
    verified = verify_json_contract(row.definition, schema_version=row.definition_schema_version,
        size_bytes=row.definition_size_bytes, sha256=row.definition_sha256, max_bytes=DATABASE_JSON_FIELD_MAX_BYTES)
    _recipe_stages_shape(verified.value, catalog)
    return {"id": str(row.pk), "definition": deepcopy(verified.value), "schema_version": verified.schema_version,
        "size_bytes": verified.size_bytes, "sha256": verified.sha256}


def build_run_execution_plan(*, source, project_id, recipe_version_id, scope, delivery, initial_content,
        stage_catalog_key, expected_stage_catalog_sha256, actor):
    """One first-resolution builder. No writes, defaults, current picks or calls."""
    if not actor or not actor.is_authenticated or not actor.is_active:
        raise PermissionDenied("需要有效登录身份")
    if source not in ("recipe", "manual") or (source == "recipe") != (recipe_version_id is not None):
        _invalid("plan_source", "配方来源必须引用配方；人工来源不得引用或创建配方")
    project_id = _id(str(project_id), "project_id")
    owner_id = SeriesProject.objects.filter(pk=project_id).values_list("owner_id", flat=True).first()
    if owner_id is None or (owner_id != actor.pk and not actor.is_staff):
        raise PermissionDenied("无权为此项目创建生产计划")
    catalog = production_stage_catalog()
    if stage_catalog_key != catalog.value["key"] or expected_stage_catalog_sha256 != catalog.sha256:
        _invalid("stage_catalog", "生产阶段目录已变化，请重新读取后确认")
    # Bound hostile arrays before their values reach an SQL IN clause.
    json_contract([scope, delivery, initial_content], max_bytes=DATABASE_JSON_FIELD_MAX_BYTES)
    frozen_scope, frozen_delivery = _scope_shape(scope), _delivery_shape(delivery)
    if frozen_scope["kind"] == "episodes":
        ids = frozen_scope["episode_ids"]
        actual = {str(value) for value in Episode.objects.filter(project_id=project_id, pk__in=ids).values_list("id", flat=True)}
        if actual != set(ids):
            _invalid("scope.episode_ids", "存在不属于当前项目的剧集，不能扩大生产范围")
    recipe = _recipe_snapshot(_id(str(recipe_version_id), "recipe_version_id"), catalog.value) if source == "recipe" else None
    from studio.services.execution_structure_mapping import freeze_structure_source_schemas
    from studio.services.execution_input_catalog import execution_text_output_options
    text_outputs = execution_text_output_options(recipe["definition"]["stages"] if recipe else ())
    # The whole bounded catalog is frozen so historical reads don't substitute
    # today's catalog. Manual plans store no selected models and no stage wins.
    return json_contract({"schema_version": 1, "source": source, "project_id": project_id,
        "scope": frozen_scope, "delivery": frozen_delivery, "initial_content": confirmed_script_reference(project_id, initial_content),
        "stage_catalog": {"value": deepcopy(catalog.value), "schema_version": catalog.schema_version,
            "size_bytes": catalog.size_bytes, "sha256": catalog.sha256},
        "recipe": recipe, "structure_source_schemas": freeze_structure_source_schemas(text_outputs,
            stages=recipe["definition"]["stages"] if recipe else ()),
    }, max_bytes=DATABASE_JSON_FIELD_MAX_BYTES)


def verify_run_execution_plan(record, run):
    verified = verify_json_contract(record.definition, schema_version=record.definition_schema_version,
        size_bytes=record.definition_size_bytes, sha256=record.definition_sha256, max_bytes=DATABASE_JSON_FIELD_MAX_BYTES)
    value = verified.value
    _object(value, {"schema_version", "source", "project_id", "scope", "delivery", "initial_content", "stage_catalog", "recipe", "structure_source_schemas"}, "run_plan")
    from studio.services.execution_structure_mapping import validate_structure_source_schemas
    validate_structure_source_schemas(value["structure_source_schemas"])
    if type(value["schema_version"]) is not int or value["schema_version"] != 1 or verified.schema_version != 1:
        _invalid("run_plan", "执行计划版本无效")
    if (value["source"] not in ("manual", "recipe") or value["source"] != record.source or record.source != run.plan_source
            or value["project_id"] != str(run.project_id) or record.run_id != run.pk):
        _invalid("run_plan", "执行计划与正式运行来源或项目不一致")
    _scope_shape(value["scope"])
    _delivery_shape(value["delivery"])
    envelope = value["stage_catalog"]
    _object(envelope, {"value", "schema_version", "size_bytes", "sha256"}, "run_plan.stage_catalog")
    catalog = verify_json_contract(**envelope, max_bytes=DATABASE_JSON_FIELD_MAX_BYTES).value
    _object(catalog, {"schema_version", "key", "version", "stages"}, "run_plan.stage_catalog.value")
    if (type(catalog["schema_version"]) is not int or catalog["schema_version"] != 1 or envelope["schema_version"] != 1
            or type(catalog["version"]) is not int or catalog["version"] < 1
            or not isinstance(catalog["stages"], list) or not catalog["stages"]):
        _invalid("run_plan.stage_catalog", "冻结阶段目录格式无效")
    seen = set()
    for ordinal, row in enumerate(catalog["stages"], start=1):
        keys = {"key", "ordinal", "modes", "requires_routing", "manual_actions"}
        if isinstance(row, dict) and "repeatable" in row:
            keys.add("repeatable")
            if row["repeatable"] is not True:
                _invalid("run_plan.stage_catalog.stages", "可重复职责必须明确声明为true，单次职责不添加别名")
        _object(row, keys, "run_plan.stage_catalog.stages")
        if (not isinstance(row["key"], str) or not row["key"] or row["key"] in seen
                or type(row["ordinal"]) is not int or row["ordinal"] != ordinal or type(row["requires_routing"]) is not bool
                or not isinstance(row["modes"], list) or not row["modes"] or any(not isinstance(mode, str) for mode in row["modes"])
                or not isinstance(row["manual_actions"], list) or any(not isinstance(action, str) for action in row["manual_actions"])):
            _invalid("run_plan.stage_catalog.stages", "冻结阶段职责或顺序无效")
        seen.add(row["key"])
    if (catalog["key"] != record.stage_catalog_key or catalog["version"] != record.stage_catalog_version
            or envelope["sha256"] != record.stage_catalog_sha256):
        _invalid("run_plan.stage_catalog", "阶段目录索引与冻结内容不一致")
    recipe = value["recipe"]
    if value["source"] == "manual":
        if recipe is not None or run.recipe_version_id is not None:
            _invalid("run_plan.recipe", "人工计划不能包含配方或从配方补齐执行目标")
    else:
        _object(recipe, {"id", "definition", "schema_version", "size_bytes", "sha256"}, "run_plan.recipe")
        if recipe["id"] != str(run.recipe_version_id) or recipe["schema_version"] != 3:
            _invalid("run_plan.recipe", "冻结配方与正式来源不一致")
        contract = verify_json_contract(recipe["definition"], schema_version=recipe["schema_version"],
            size_bytes=recipe["size_bytes"], sha256=recipe["sha256"], max_bytes=DATABASE_JSON_FIELD_MAX_BYTES)
        _recipe_stages_shape(contract.value, catalog)
    return verified


def get_run_execution_plan(run):
    """Exact immutable reader. Missing means failure, never a Recipe fallback."""
    identity = ProductionRun.objects.only("id", "project_id", "plan_source", "recipe_version_id").get(pk=run.pk)
    try:
        record = ProductionRunPlan.objects.get(run_id=identity.pk)
    except ProductionRunPlan.DoesNotExist:
        _invalid("run_plan", "运行缺少正式冻结执行计划，不能从配方或当前目录补造")
    return verify_run_execution_plan(record, identity)


@transaction.atomic
def freeze_run_execution_plan(*, run, scope, delivery, initial_content, stage_catalog_key,
        expected_stage_catalog_sha256, actor):
    """Called in root creation's transaction, exactly once for that real Run."""
    run = ProductionRun.objects.select_for_update().only("id", "owner_id", "project_id", "plan_source", "recipe_version_id").get(pk=run.pk)
    if not actor or not actor.is_active or not actor.is_authenticated or (run.owner_id != actor.pk and not actor.is_staff):
        raise PermissionDenied("无权冻结此运行的计划")
    if ProductionRunPlan.objects.filter(run_id=run.pk).exists():
        _invalid("run_plan", "运行已有冻结计划；重复请求应读取原计划，不重新求值")
    definition = build_run_execution_plan(source=run.plan_source, project_id=run.project_id, recipe_version_id=run.recipe_version_id,
        scope=scope, delivery=delivery, initial_content=initial_content, stage_catalog_key=stage_catalog_key,
        expected_stage_catalog_sha256=expected_stage_catalog_sha256, actor=actor)
    catalog = definition.value["stage_catalog"]
    record = ProductionRunPlan(run=run, source=run.plan_source, created_by=actor, stage_catalog_key=catalog["value"]["key"],
        stage_catalog_version=catalog["value"]["version"], stage_catalog_sha256=catalog["sha256"])
    assign_json_contract(record, "definition", definition.value, max_bytes=DATABASE_JSON_FIELD_MAX_BYTES)
    record.full_clean()
    record.save()
    return record
