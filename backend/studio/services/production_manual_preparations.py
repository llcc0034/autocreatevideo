"""Manual actions create the same primary execution preparation.

No provider call, auxiliary dispatch, automatic adoption or hidden Recipe.
An explicit advance in the existing workbench performs the original gates.
"""
from django.core.exceptions import ValidationError
from django.utils import timezone

from studio.services.execution_field_review import production_field_review_contract
from studio.services.execution_instances import execution_configuration_max_bytes, selectable_execution_versions
from studio.services.execution_preparations import create_execution_instance_preparation
from studio.services.execution_request_preparation import resolve_execution_request_facts
from studio.services.json_contracts import json_contract
from studio.services.production_manual_commands import ManualActionResult
from studio.services.production_stage_catalog import STAGE_EXECUTION_OPERATIONS, STAGE_MEDIA_OUTPUT_KINDS, STAGE_MEDIA_SCOPE_KINDS, validate_stage_execution_definition
from studio.services.production_execution_units import resolve_production_unit
from studio.services.production_execution_sources import execution_source_stage_ids
from studio.services.security_boundaries import boundary_int, security_boundary_resolution_cache


def _no_parameter_source(_):
    raise ValidationError("人工调用直接填写实例允许的参数，不读取未声明的步骤参数来源")


def _reviewed_shot_pin(*, run, stage, request, definition, eligible):
    """Pin the original review, then let the common exact-use resolver freeze facts.

    The pin is only an optimistic read identity, never caller-authored approval.
    No selection, latest-version lookup or second provenance writer lives here.
    """
    from studio.services.execution_media_source_policy import select_execution_media_source
    from studio.services.production_reviewed_content import read_reviewed_storyboard
    reviewed = []
    sources = request["payload"]["inputs"]
    if not isinstance(sources, dict):
        raise ValidationError("生成输入必须是按槽位声明的对象")
    for spec in definition.value["configuration"]["inputs"]:
        if spec["kind"] not in {"image", "video", "audio"} or spec["key"] not in sources:
            continue
        source, _ = select_execution_media_source(spec, sources[spec["key"]])
        items = source.get("items", []) if isinstance(source, dict) else []
        if not isinstance(items, list):
            raise ValidationError("媒体来源必须提供有序 items")
        for item in items:
            if isinstance(item, dict) and str(item.get("item_key", "")).startswith("reviewed:"):
                reviewed.append(item)
    pin = request["payload"].get("expected_storyboard_review")
    if not reviewed:
        if "expected_storyboard_review" in request["payload"]:
            raise ValidationError("未选择本镜审核用途，不应携带逐镜审核引用")
        return
    if (stage.stage_key != "media_generation" or request["scope"]["kind"] != "shot"
            or any(item.get("kind") != "asset" or item.get("scope_ref") != request["scope"] for item in reviewed)
            or not isinstance(pin, dict) or set(pin) != {"decision_id", "snapshot_sha256"}
            or not isinstance(pin["decision_id"], str) or not pin["decision_id"]
            or not isinstance(pin["snapshot_sha256"], str)):
        raise ValidationError("本镜审核用途必须固定当前制作镜头和原审核决定、摘要，不接受调用方来源快照")
    saved = read_reviewed_storyboard(run=run, eligible_stage_ids=eligible, decision_id=pin["decision_id"])
    if (saved["review"]["snapshot_sha256"] != pin["snapshot_sha256"]
            or not any(shot["id"] == request["scope"]["id"] for episode in saved["canonical"]["episodes"] for shot in episode["shots"])):
        raise ValidationError("本镜审核用途已变化或不在原审核范围，请重新读取并选择", code="reference_review_changed")


@security_boundary_resolution_cache()
def prepare_manual_generation(*, run, stage, plan, request, actor):
    """Called under the original manual command's Run -> Stage transaction."""
    operation = STAGE_EXECUTION_OPERATIONS.get(stage.stage_key)
    media = stage.stage_key in STAGE_MEDIA_OUTPUT_KINDS
    if (operation is None and not media) or stage.mode != "manual":
        raise ValidationError("此人工生成步骤尚未接通", code="manual_action_not_ready")
    if media and request["scope"]["kind"] not in STAGE_MEDIA_SCOPE_KINDS[stage.stage_key]:
        raise ValidationError("此生成步骤需要明确匹配的制作对象；参考图必须定位具体角色、场景或道具")
    unit = resolve_production_unit(run, request["scope"])
    if request["unit_key"] != f'{unit["scope"]["kind"]}:{unit["scope"]["id"]}':
        raise ValidationError("请使用目录返回的明确生产单元，不使用自定义单元名称")
    if not media and request["scope"] != {"kind": "project", "id": str(run.project_id)}:
        raise ValidationError("项目文本生成必须明确定位本轮项目，不能推断镜头或使用自定义单元")
    if request["expected_selection_revision"] is not None:
        raise ValidationError("创建候选准备不改变采用结果，选择修订必须为空")
    if run.status not in {"awaiting_input", "running"} or stage.status not in {"pending", "running"}:
        raise ValidationError("本轮或此步骤已停止，不能通过新建准备隐式恢复或覆盖已采用结果")
    payload = request["payload"]
    keys = {"execution_instance_version_id", "expected_definition_sha256", "inputs", "parameters", "manual_channels",
        "subject_facts", "source_language", "target_language"}
    if not keys <= set(payload) or set(payload) - keys - {"expected_storyboard_review"}:
        raise ValidationError("生成准备需要精确实例、定义摘要、输入、参数、提示词通道、主体和语言；不接受快照或批准字段")
    version_id = payload["execution_instance_version_id"]
    versions = selectable_execution_versions(run.owner)
    if operation is not None:
        versions = versions.filter(operation=operation)
    if not isinstance(version_id, str) or not versions.filter(pk=version_id).exists():
        raise ValidationError("请选择本轮所有者可使用且能力与步骤一致的已发布实例")
    if run.project.owner_id != run.owner_id:
        raise ValidationError("本轮运行与项目归属已不一致，不能替换所有者继续生成")
    inputs = {key: payload[key] for key in keys - {"execution_instance_version_id", "expected_definition_sha256"}}
    eligible = execution_source_stage_ids(stage)

    def resolve(definition, locked_stage):
        validate_stage_execution_definition(stage.stage_key, definition)
        if not media and definition.value["target"]["workflow"] is not None:
            raise ValidationError("此项目文本步骤需要对应原生文本协议，不能改用媒体工作流")
        if media and definition.value["target"]["driver_type"] not in {"comfyui", "async_rest"}:
            raise ValidationError("媒体准备需要所选实例声明的ComfyUI或受限异步协议")
        _reviewed_shot_pin(run=run, stage=stage, request=request, definition=definition, eligible=eligible)
        return resolve_execution_request_facts(definition, inputs, owner=run.owner, unit_key=request["unit_key"],
            max_media_bytes=boundary_int("security.workflow_smoke.asset_max_file_bytes"),
            review_contract=production_field_review_contract(locked_stage), parameter_policies=[],
            resolve_parameter_source=_no_parameter_source, project_id=run.project_id,
            production_run_id=str(run.pk), eligible_stage_ids=eligible,
            production_scope=request["scope"] if media else None)

    preparation = create_execution_instance_preparation(owner_ref=("stage_run_id", stage.pk), version_id=version_id,
        expected_definition_sha256=payload["expected_definition_sha256"], unit_key=request["unit_key"],
        request_key=request["idempotency_key"], intent=json_contract({"schema_version": 1,
            "run_plan_sha256": plan.sha256, "stage_key": stage.stage_key, "scope": request["scope"], **inputs,
            **({"expected_storyboard_review": payload["expected_storyboard_review"]} if "expected_storyboard_review" in payload else {})},
            max_bytes=execution_configuration_max_bytes()),
        resolve_facts=resolve, actor=actor)
    # Creation is not external-call authorization. Do not let the due scan
    # advance even a field-free preparation before the user opens and submits it.
    preparation.next_check_at = None
    preparation.save(update_fields=("next_check_at", "updated_at"))
    now = timezone.now()
    stage.status = "running"
    stage.started_at = stage.started_at or now
    stage.save(update_fields=("status", "started_at", "updated_at"))
    run.status = "running"
    run.started_at = run.started_at or now
    run.save(update_fields=("status", "started_at", "updated_at"))
    return ManualActionResult("preparation", preparation.pk)
