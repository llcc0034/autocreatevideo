"""Registered sources for Recipe parameter policies.

Authored selectors are relative to the actual Run project. Preparation binds
the exact project, observed revision and value once; replay never rereads it.
Only explicit registered conversions; no formulas, JSON paths or model rules.
"""
from copy import deepcopy
import re
from uuid import UUID

from django.core.exceptions import ValidationError
from django.db import transaction
from django.utils.dateparse import parse_datetime

from studio.models import SeriesProject


# Platform field accessors, not model defaults or editable numeric limits.
_PROJECT_SCALARS = {
    "project.width": {"field": "width", "label": "项目输出宽度", "unit": "px"},
    "project.height": {"field": "height", "label": "项目输出高度", "unit": "px"},
    "project.fps": {"field": "fps", "label": "项目帧率", "unit": "fps"},
    "project.default_shot_duration_ms": {"field": "default_shot_duration_ms", "label": "项目默认镜头时长", "unit": "ms"},
}


def recipe_parameter_source_options():
    return [{"selector_key": key, "label": row["label"], "scope_kind": "project", "type": "integer", "unit": row["unit"],
        "source_kind": "context_scalar", "parameter_key": None, "stage_key": None,
        "description": "按原始单位读取本轮项目字段，不自动换算；首次准备后固定。"}
        for key, row in _PROJECT_SCALARS.items()] + [{
        "selector_key": "shot.duration_ms", "label": "按已审核镜头时长计算帧数", "scope_kind": "shot",
        "type": "integer", "unit": "frames", "source_kind": "workflow_frames", "parameter_key": "video.frames",
        "stage_key": "media_generation",
        "description": "读取当前已审核镜头时长，使用本次实际帧率和冻结工作流的帧对齐规则计算；超出合同范围时报错。审核来源、规则和结果在首次准备时固定，重试不重算。"}]


def validate_recipe_parameter_source(source, *, parameter_type, require_complete=True, parameter_key=None,
        definition=None, stage_key=None):
    if isinstance(source, dict) and source.get("kind") == "workflow_frames":
        if source != {"kind": "workflow_frames", "selector_key": "shot.duration_ms", "scope": {"kind": "shot"}}:
            raise ValidationError("帧数计算只能选择当前已审核镜头时长，不能填写对象ID或公式")
        if parameter_type != "integer" or parameter_key != "video.frames" or stage_key != "media_generation":
            raise ValidationError("镜头时长换算只能用于媒体生成阶段的工作流帧数参数")
        _frame_interface(definition)
        return
    if (not isinstance(source, dict) or set(source) != {"kind", "selector_key", "scope"}
            or source["kind"] != "context_scalar" or source["scope"] != {"kind": "project"}
            or not isinstance(source["selector_key"], str)):
        raise ValidationError("参数来源必须选择目录中的本轮项目字段，不接受对象ID、公式或JSON路径")
    if parameter_type not in {"integer", "number"}:
        raise ValidationError("项目数值来源只能用于数值参数，不能改写文本、布尔值或模型选择器")
    if not source["selector_key"] and not require_complete:
        return
    if source["selector_key"] not in _PROJECT_SCALARS:
        raise ValidationError("参数来源尚未登记，请选择已提供的字段；不推断或读取任意属性")


def bind_recipe_parameter_sources(policies, *, run, definition=None, scope=None, eligible_stage_ids=(), stage_key=None):
    """Called only inside first-preparation resolution under original locks."""
    result = deepcopy(policies)
    sources = [row for row in result if row["policy"] == "locked_source"]
    if not sources:
        return result
    if not transaction.get_connection().in_atomic_block:
        raise ValidationError("参数来源必须在本轮正式准备事务内读取")
    derived = [row for row in sources if row["source"].get("kind") == "workflow_frames"]
    for row in sources:
        validate_recipe_parameter_source(row["source"], parameter_type="integer" if row in derived else "number",
            parameter_key=row["key"], definition=definition, stage_key=stage_key)
    scalars = [row for row in sources if row not in derived]
    fields = {_PROJECT_SCALARS[row["source"]["selector_key"]]["field"] for row in scalars}
    # One statement observes all requested fields and their timestamp together.
    # Freeze that observation, not a promise that the mutable project stays
    # unchanged. Do not acquire Project after Run/Preparation: Run creation
    # locks Project first, and reversing that order could deadlock on replay.
    project = SeriesProject.objects.only("id", "owner_id", "updated_at", *sorted(fields)).get(pk=run.project_id) if scalars else None
    if project is not None and project.owner_id != run.owner_id:
        raise ValidationError("项目参数来源与本轮所有者不一致")
    for row in scalars:
        selector = row["source"]["selector_key"]
        value = getattr(project, _PROJECT_SCALARS[selector]["field"])
        if type(value) is not int or value <= 0:
            raise ValidationError(f"{_PROJECT_SCALARS[selector]['label']} 缺少明确正整数，不采用参数默认值代替")
        row["source"] = {"schema_version": 1, "kind": "context_scalar", "selector_key": selector,
            "scope": {"kind": "project", "id": str(project.pk)},
            "observed_updated_at": project.updated_at.isoformat(), "value": value}
    if derived:
        from studio.services.production_execution_units import resolve_production_unit
        from studio.services.production_reviewed_content import read_reviewed_storyboard, reviewed_shot_row
        from studio.services.workflow import aligned_frames
        if not isinstance(scope, dict) or scope.get("kind") != "shot":
            raise ValidationError("镜头帧数计算需要当前明确的镜头，不读取项目默认时长")
        unit = resolve_production_unit(run, scope)
        reviewed = read_reviewed_storyboard(run=run, eligible_stage_ids=eligible_stage_ids)
        duration = reviewed_shot_row(reviewed, unit)["duration_ms"]
        if type(duration) is not int or duration <= 0:
            raise ValidationError("已审核镜头缺少明确的正整数时长")
        interface = _frame_interface(definition)
        fps = _resolved_fps(interface, [row for row in result if row not in derived])
        frame_policy = {**deepcopy(interface["frame_policy"]), "fps": fps["value"]}
        # Alignment is explicit; clipping a long shot to the workflow maximum
        # would lose its approved duration and must not masquerade as success.
        requested = duration * fps["value"] / 1000
        if not frame_policy["minimum_frames"] <= requested <= frame_policy["maximum_frames"]:
            raise ValidationError("已审核镜头时长超出工作流帧数范围，请调整镜头或选择适用实例")
        value = aligned_frames(duration, frame_policy)
        for row in derived:
            row["source"] = {"schema_version": 1, "kind": "workflow_frames", "selector_key": "shot.duration_ms",
                "scope": deepcopy(scope), "duration_ms": duration, "review": deepcopy(reviewed["review"]),
                "definition_sha256": definition.sha256, "workflow_version_id": definition.value["configuration"]["workflow_version_id"],
                "frame_policy": frame_policy, "fps": fps, "value": value}
    return result


def _frame_interface(definition):
    from studio.services.json_contracts import JsonContract
    from studio.services.workflow import validate_frame_policy, WorkflowBuildError
    if (not isinstance(definition, JsonContract) or not definition.value.get("configuration", {}).get("workflow_version_id")
            or "video.frames" not in definition.value.get("interface", {}).get("parameters", {})):
        raise ValidationError("帧数计算需要精确工作流版本及其可配置帧数合同")
    interface = definition.value["interface"]
    try:
        validate_frame_policy(interface.get("frame_policy"))
    except (WorkflowBuildError, TypeError) as exc:
        raise ValidationError("工作流缺少有效的冻结帧数对齐规则") from exc
    if not interface.get("media_semantic_keys", {}).get("fps"):
        raise ValidationError("工作流缺少明确的帧率语义，不推断参数名或采用默认帧率")
    return interface


def _resolved_fps(interface, policies):
    from studio.services.execution_step_parameters import compose_execution_step_parameters
    from studio.services.execution_parameters import resolve_execution_parameters
    key = interface["media_semantic_keys"]["fps"]
    if key in interface["parameters"]:
        step = compose_execution_step_parameters({key: deepcopy(interface["parameters"][key]["layers"])},
            [row for row in policies if row["key"] == key], resolve_source=resolved_recipe_parameter_source)
        resolved = resolve_execution_parameters({key: step["parameters"][key]["layers"]}, {})
        if key not in resolved["values"]:
            raise ValidationError("本次帧率尚未配置，不采用工作流示例帧率代替")
        fps = {"key": key, "value": resolved["values"][key], "source": resolved["sources"][key]}
    elif key in interface.get("static_semantics", {}):
        fps = {"key": key, "value": interface["static_semantics"][key], "source": "workflow.static_semantics"}
    else:
        raise ValidationError("工作流没有本次可解析的帧率")
    if type(fps["value"]) not in (int, float) or not 0 < fps["value"] < float("inf"):
        raise ValidationError("帧数计算需要明确的有限正数帧率")
    return fps


def resolved_recipe_parameter_source(source):
    """Read only the bound scalar; all target constraints remain shared."""
    if isinstance(source, dict) and source.get("kind") == "workflow_frames":
        # The Preparation detail digest protects this snapshot. Replay reads
        # its result, never current reviews, workflow versions or a new formula.
        if (set(source) != {"schema_version", "kind", "selector_key", "scope", "duration_ms", "review",
                "definition_sha256", "workflow_version_id", "frame_policy", "fps", "value"}
                or type(source["schema_version"]) is not int or source["schema_version"] != 1
                or source["selector_key"] != "shot.duration_ms" or type(source["value"]) is not int or source["value"] <= 0
                or type(source["duration_ms"]) is not int or source["duration_ms"] <= 0
                or not isinstance(source["scope"], dict) or set(source["scope"]) != {"kind", "id"} or source["scope"]["kind"] != "shot"
                or not isinstance(source["definition_sha256"], str) or not re.fullmatch(r"[a-f0-9]{64}", source["definition_sha256"])
                or not isinstance(source["review"], dict) or set(source["review"]) != {"stage_run_id", "decision_id", "revision", "snapshot_sha256"}
                or type(source["review"]["revision"]) is not int or source["review"]["revision"] < 1
                or not isinstance(source["review"]["snapshot_sha256"], str) or not re.fullmatch(r"[a-f0-9]{64}", source["review"]["snapshot_sha256"])
                or not isinstance(source["fps"], dict) or set(source["fps"]) != {"key", "value", "source"}
                or any(not isinstance(source["fps"][key], str) or not source["fps"][key] for key in ("key", "source"))):
            raise ValidationError("帧数来源缺少首次准备时冻结的审核、计算规则或结果")
        from studio.services.workflow import validate_frame_policy, WorkflowBuildError
        try:
            for identifier in (source["scope"]["id"], source["workflow_version_id"], source["review"]["stage_run_id"], source["review"]["decision_id"]):
                if not isinstance(identifier, str) or str(UUID(identifier)) != identifier:
                    raise ValueError
            validate_frame_policy(source["frame_policy"])
            policy = source["frame_policy"]
            if (type(source["fps"]["value"]) not in (int, float) or source["fps"]["value"] != policy["fps"]
                    or not policy["minimum_frames"] <= source["value"] <= policy["maximum_frames"]
                    or source["value"] % policy["alignment_modulus"] != policy["alignment_remainder"]):
                raise ValueError
        except (ValueError, TypeError, WorkflowBuildError) as exc:
            raise ValidationError("冻结帧数来源的对象、帧率或对齐结果无效") from exc
        return source["value"]
    if (not isinstance(source, dict) or set(source) != {"schema_version", "kind", "selector_key", "scope", "observed_updated_at", "value"}
            or type(source["schema_version"]) is not int or source["schema_version"] != 1 or source["kind"] != "context_scalar"
            or not isinstance(source["selector_key"], str) or source["selector_key"] not in _PROJECT_SCALARS
            or not isinstance(source["scope"], dict) or set(source["scope"]) != {"kind", "id"}
            or source["scope"]["kind"] != "project" or not isinstance(source["scope"]["id"], str)
            or not isinstance(source["observed_updated_at"], str) or not source["observed_updated_at"]
            or type(source["value"]) is not int or source["value"] <= 0):
        raise ValidationError("固定参数来源缺少已核对的项目、字段、读取修订和原始数值")
    try:
        UUID(source["scope"]["id"])
        observed = parse_datetime(source["observed_updated_at"])
        if observed is None or observed.tzinfo is None:
            raise ValueError
    except ValueError as exc:
        raise ValidationError("固定参数来源项目标识或读取修订无效") from exc
    return source["value"]
