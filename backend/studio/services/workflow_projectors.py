from __future__ import annotations

from collections import Counter
from copy import deepcopy
from dataclasses import dataclass
from typing import Any

from django.core.exceptions import ValidationError

from studio.models import ModelArtifact, WorkflowProjectorVersion
from studio.services.json_contracts import (
    find_forbidden_secret_fields,
    json_contract,
    verify_json_contract,
)
from studio.services.security_boundaries import boundary_int


WORKFLOW_PROJECTOR_DEFINITION_SCHEMA_VERSION = 1
WORKFLOW_PROJECTOR_EDITOR_SCHEMA_VERSION = 1
WORKFLOW_PROJECTOR_ENGINES: dict[str, int] = {
    "generic.fixed": 1,
    "generic.references": 1,
    "generic.classified": 1,
}
WORKFLOW_PROJECTOR_ADAPTERS = {
    "compiler": {
        "generic.comfy/v1", "generic.comfy/v2", "generic.comfy/v3",
    },
    "output": {"comfy.primary/v1"},
    "dynamic": {"none/v1", "generic.asset_groups/v1", "generic.classified_references/v1"},
}
WORKFLOW_PROJECTOR_ENGINE_ADAPTER_CONTRACTS = {
    "generic.references": {"compiler": "generic.comfy/v2", "output": "comfy.primary/v1", "dynamic": "generic.asset_groups/v1"},
    "generic.fixed": {
        "compiler": "generic.comfy/v1",
        "output": "comfy.primary/v1",
        "dynamic": "none/v1",
    },
    "generic.classified": {
        "compiler": "generic.comfy/v3",
        "output": "comfy.primary/v1",
        "dynamic": "generic.classified_references/v1",
    },
}
_OUTPUT_KINDS = {"image", "video", "audio", "text"}
_SAFE_SCALAR_TYPES = (str, int, float, bool, type(None))

_WORKFLOW_PROJECTOR_ENGINE_LABELS = {
    "generic.references": "通用动态多素材接线",
    "generic.fixed": "Generic 固定槽位投影",
    "generic.classified": "通用分类参考素材接线",
}
_WORKFLOW_PROJECTOR_ENGINE_DESCRIPTIONS = {
    "generic.references": "按来源节点模板重复预处理，使用顺序端口或逐项合并算法；模型名称不参与接线。",
    "generic.fixed": (
        "按版本化节点、参数、模型 selector 与固定素材槽生成通用 ComfyUI 合同；"
        "也可描述 T2VA、FL2VA 等受审固定拓扑。"
    ),
    "generic.classified": (
        "按配置向目标节点接入图片、视频和音频，配套音频保持视频序号；模型与节点名称不参与算法选择。"
    ),
}
_WORKFLOW_PROJECTOR_PROJECTION_KINDS = {
    "generic.references": "generic_references",
    "generic.fixed": "generic_fixed",
    "generic.classified": "generic_classified",
}


def _projector_choice(value: str, label: str) -> dict[str, str]:
    return {"value": value, "label": label}


def _projector_editor_field(
    path: str,
    label: str,
    help_text: str,
    control: str,
    *,
    required: bool = True,
    read_only: bool = False,
) -> dict[str, Any]:
    return {
        "path": path,
        "label": label,
        "help": help_text,
        "control": control,
        "required": required,
        "read_only": read_only,
    }


WORKFLOW_PROJECTOR_EDITOR_SECTIONS = [
    {
        "key": "matching",
        "label": "工作流识别",
        "description": "用图结构事实识别工作流族；不按文件名或模型名称猜测。",
        "fields": [
            _projector_editor_field(
                "schema_version", "Definition Schema", "由平台维护的结构版本。",
                "readonly", read_only=True,
            ),
            _projector_editor_field(
                "priority", "匹配优先级", "多个精确方案均命中时取数值最高者；不是生产优先级。",
                "integer",
            ),
            _projector_editor_field(
                "detector.fallback", "兜底方案", "只在没有精确方案命中时使用；启用后不能保留图指纹。",
                "boolean",
            ),
            _projector_editor_field(
                "detector.allow_other_nodes", "允许额外节点", "关闭时，候选图不得包含 detector 未声明的节点类型。",
                "boolean",
            ),
            _projector_editor_field(
                "detector.node_counts", "必需节点", "节点类型及其精确数量。",
                "node_count_map",
            ),
            _projector_editor_field(
                "detector.optional_node_counts", "可选节点", "节点类型允许出现的最小与最大数量。",
                "node_range_map",
            ),
            _projector_editor_field(
                "detector.input_equals", "关键输入条件", "用于区分同一节点拓扑下的 operation 或模式。",
                "input_equals_list",
            ),
        ],
    },
    {
        "key": "outputs",
        "label": "输出与模型",
        "description": "声明主产物节点，以及工作流中需要模型库存证明的文件输入。",
        "fields": [
            _projector_editor_field(
                "output_nodes", "主输出节点", "至少一项；把 ComfyUI 输出节点类型映射为平台媒体类型。",
                "output_map",
            ),
            _projector_editor_field(
                "model_selectors", "模型文件识别", "从 Loader 节点输入提取模型文件并映射到 Artifact role 与库存目录。",
                "model_selector_list",
            ),
        ],
    },
    {
        "key": "graph",
        "label": "节点与连接",
        "description": "把易变的节点 ID 提升为稳定语义角色，并约束必须存在的关键连接。",
        "fields": [
            _projector_editor_field(
                "node_roles", "节点语义角色", "角色名由合同引用；节点 ID 变化不应改变语义。",
                "node_role_map",
            ),
            _projector_editor_field(
                "required_edges", "必要连接", "声明角色之间必须存在的输出索引与输入连接。",
                "edge_list",
            ),
        ],
    },
    {
        "key": "inputs",
        "label": "参数与素材",
        "description": "决定哪些输入被冻结、从 object_info 读取、由来源锁定或在运行时传入。",
        "fields": [
            _projector_editor_field(
                "parameter_policies", "参数策略", "把节点输入映射为稳定 semantic key；具体策略由 Engine 解释。Generic 的来源锁定只保证来源默认优先，不是统一的运行时不可覆盖锁。",
                "parameter_policy_list",
            ),
            _projector_editor_field(
                "asset_slots", "素材槽", "声明媒体类型与约束来源，不是素材文件来源。动态数量须由 Engine、模型合同与节点能力共同支持；Generic 仅声明 dynamic 不会扩展节点。",
                "asset_slot_list",
            ),
        ],
    },
    {
        "key": "execution",
        "label": "执行引擎与接线",
        "description": "Engine 身份、Adapter 与 projection kind 只读；当前引擎开放的业务接线配置在下方编辑并随版本冻结。",
        "fields": [
            _projector_editor_field(
                "adapters", "受审 Adapter（只读）", "由 Engine 唯一确定，负责把合同编译、取回产物并处理动态输入；不能自由拼接。",
                "adapter_contract", read_only=True,
            ),
            _projector_editor_field(
                "projection", "Engine 专属配置", "只有已注册 Engine 明确开放的版本化配置才可编辑；Engine 身份本身保持只读。",
                "projection_config",
            ),
        ],
    },
    {
        "key": "advanced",
        "label": "复核与 JSON",
        "description": "完整 JSON 与参数表单是同一个草稿；仅供高级复核与未来扩展。",
        "fields": [
            _projector_editor_field(
                "$", "完整 Definition JSON", "只有合法 JSON 对象才会替换当前参数草稿。",
                "advanced_json",
            ),
        ],
    },
]

WORKFLOW_PROJECTOR_PROJECTION_FIELDS = {
    "generic.fixed": [
        _projector_editor_field("projection.config.image_resolution", "图片输出尺寸", "仅适用于 LoadImage 经 FluxKontextImageScale 驱动的图片输出；单组固定尺寸，多组按原图比例选择。配置随版本冻结。", "image_resolution"),
    ],
    "generic.references": [
        _projector_editor_field("projection.config.groups", "动态素材组接线", "选择来源模板、目标角色和端口；数量由工作流或模型合同约束。在线节点不支持的接线会阻断。", "asset_group_list"),
    ],
    "generic.classified": [
        _projector_editor_field("projection.config", "分类参考素材接线", "配置目标角色、模型模式和各媒体输入端口；配套音频按视频序号接入。", "classified_reference_config"),
    ],
}


class WorkflowProjectorError(ValidationError):
    code = "workflow_projector_invalid"

    def __init__(self, message: str):
        super().__init__(message, code=type(self).code)


class WorkflowProjectorCatalogUnavailable(WorkflowProjectorError):
    code = "workflow_projector_catalog_unavailable"


class WorkflowProjectorAmbiguous(WorkflowProjectorError):
    code = "workflow_projector_ambiguous"


@dataclass(frozen=True)
class ProjectorContract:
    version_id: str
    key: str
    version: int
    provider_driver_type: str
    operation: str
    output_kind: str
    engine_key: str
    engine_version: int
    definition_sha256: str
    definition: dict[str, Any]

    @property
    def identity(self) -> dict[str, Any]:
        return {
            "id": self.key,
            "version": self.version,
            "version_id": self.version_id,
            "definition_sha256": self.definition_sha256,
            "provider_driver_type": self.provider_driver_type,
            "operation": self.operation,
            "output_kind": self.output_kind,
            "engine": {
                "key": self.engine_key,
                "version": self.engine_version,
            },
        }


def _projector_max_bytes() -> int:
    return boundary_int("security.snapshot.stage_max_bytes")


def _mapping(value: Any, label: str) -> dict[str, Any]:
    if not isinstance(value, dict):
        raise WorkflowProjectorError(f"{label} 必须是 JSON 对象")
    return value


def _bounded_node_counts(value: Any, label: str) -> dict[str, int]:
    rows = _mapping(value, label)
    result: dict[str, int] = {}
    for node_type, count in rows.items():
        if (
            not isinstance(node_type, str)
            or not node_type.strip()
            or len(node_type) > 191
            or isinstance(count, bool)
            or not isinstance(count, int)
            or count < 0
            or count > 1000
        ):
            raise WorkflowProjectorError(f"{label} 含无效节点类型或数量")
        result[node_type] = count
    return result




def validate_workflow_projector_definition(
    definition: Any,
    *,
    engine_key: str,
    engine_version: int,
) -> dict[str, Any]:
    definition = deepcopy(_mapping(definition, "工作流合同方案 definition"))
    if set(definition) != {
        "schema_version", "priority", "detector", "output_nodes",
        "model_selectors", "node_roles", "required_edges", "parameter_policies",
        "asset_slots", "adapters", "projection",
    }:
        raise WorkflowProjectorError("工作流合同方案 definition 字段集合无效")
    if definition.get("schema_version") != WORKFLOW_PROJECTOR_DEFINITION_SCHEMA_VERSION:
        raise WorkflowProjectorError(
            f"工作流合同方案仅支持 schema v{WORKFLOW_PROJECTOR_DEFINITION_SCHEMA_VERSION}",
        )
    if (
        isinstance(definition.get("priority"), bool)
        or not isinstance(definition.get("priority"), int)
        or not 0 <= definition["priority"] <= 1000
    ):
        raise WorkflowProjectorError("工作流合同方案 priority 必须在 0..1000")
    if WORKFLOW_PROJECTOR_ENGINES.get(engine_key) != engine_version:
        raise WorkflowProjectorError(
            f"工作流合同方案 engine {engine_key}/v{engine_version} 未经代码注册",
        )

    detector = _mapping(definition.get("detector"), "detector")
    if set(detector) != {
        "fallback", "allow_other_nodes", "node_counts",
        "optional_node_counts", "input_equals",
    }:
        raise WorkflowProjectorError("detector 字段集合无效")
    if not isinstance(detector.get("fallback"), bool) or not isinstance(
        detector.get("allow_other_nodes"), bool,
    ):
        raise WorkflowProjectorError("detector fallback/allow_other_nodes 必须是布尔值")
    node_counts = _bounded_node_counts(detector.get("node_counts"), "detector.node_counts")
    optional_counts = _mapping(
        detector.get("optional_node_counts"), "detector.optional_node_counts",
    )
    for node_type, bounds in optional_counts.items():
        bounds = _mapping(bounds, f"optional node {node_type}")
        if set(bounds) != {"min", "max"}:
            raise WorkflowProjectorError("optional node 数量范围字段无效")
        minimum, maximum = bounds.get("min"), bounds.get("max")
        if (
            not isinstance(node_type, str)
            or not node_type.strip()
            or isinstance(minimum, bool)
            or isinstance(maximum, bool)
            or not isinstance(minimum, int)
            or not isinstance(maximum, int)
            or minimum < 0
            or maximum < minimum
            or maximum > 1000
            or node_type in node_counts
        ):
            raise WorkflowProjectorError("optional node 数量范围无效或与固定节点重复")
    input_equals = detector.get("input_equals")
    if not isinstance(input_equals, list) or len(input_equals) > 64:
        raise WorkflowProjectorError("detector.input_equals 必须是最多 64 项的数组")
    for row in input_equals:
        row = _mapping(row, "detector.input_equals 项")
        if set(row) != {"node_type", "input_name", "value"}:
            raise WorkflowProjectorError("detector.input_equals 项字段无效")
        if (
            not isinstance(row.get("node_type"), str)
            or not row["node_type"].strip()
            or not isinstance(row.get("input_name"), str)
            or not row["input_name"].strip()
            or not isinstance(row.get("value"), _SAFE_SCALAR_TYPES)
        ):
            raise WorkflowProjectorError("detector.input_equals 项值无效")
    if detector["fallback"] and (node_counts or optional_counts or input_equals):
        raise WorkflowProjectorError("fallback detector 不能同时声明图族指纹")

    output_nodes = _mapping(definition.get("output_nodes"), "output_nodes")
    if not output_nodes:
        raise WorkflowProjectorError("工作流合同方案至少声明一个主输出节点类型")
    for node_type, output_kind in output_nodes.items():
        if not isinstance(node_type, str) or not node_type.strip() or output_kind not in _OUTPUT_KINDS:
            raise WorkflowProjectorError("output_nodes 含无效节点类型或媒体类型")

    selectors = definition.get("model_selectors")
    if not isinstance(selectors, list) or len(selectors) > 128:
        raise WorkflowProjectorError("model_selectors 必须是最多 128 项的数组")
    selector_targets: set[tuple[str, str]] = set()
    valid_roles = {value for value, _label in ModelArtifact.Role.choices}
    for row in selectors:
        row = _mapping(row, "model_selectors 项")
        if set(row) != {
            "node_type", "input_name", "role", "inventory_folder",
            "inventory_node_type", "inventory_input_name",
        }:
            raise WorkflowProjectorError("model_selectors 项字段集合无效")
        target = (str(row.get("node_type") or ""), str(row.get("input_name") or ""))
        if (
            not all(target)
            or target in selector_targets
            or row.get("role") not in valid_roles
            or any(
                not isinstance(row.get(key), str) or len(row[key]) > 191
                for key in (
                    "inventory_folder", "inventory_node_type", "inventory_input_name",
                )
            )
        ):
            raise WorkflowProjectorError("model_selectors 含重复或无效 selector")
        selector_targets.add(target)

    node_roles = _mapping(definition.get("node_roles"), "node_roles")
    for role, row in node_roles.items():
        row = _mapping(row, f"node_roles.{role}")
        if set(row) != {"node_type", "cardinality"} or not isinstance(role, str) or not role.strip():
            raise WorkflowProjectorError("node_roles 项字段无效")
        if not str(row.get("node_type") or "") or row.get("cardinality") not in {"one", "many"}:
            raise WorkflowProjectorError("node_roles 项值无效")
    required_edges = definition.get("required_edges")
    if not isinstance(required_edges, list) or len(required_edges) > 128:
        raise WorkflowProjectorError("required_edges 必须是最多 128 项的数组")
    for row in required_edges:
        row = _mapping(row, "required_edges 项")
        if set(row) != {"from_role", "output_index", "to_role", "input_name"}:
            raise WorkflowProjectorError("required_edges 项字段集合无效")
        if (
            row.get("from_role") not in node_roles
            or row.get("to_role") not in node_roles
            or node_roles[row["from_role"]]["cardinality"] != "one"
            or node_roles[row["to_role"]]["cardinality"] != "one"
            or isinstance(row.get("output_index"), bool)
            or not isinstance(row.get("output_index"), int)
            or row["output_index"] < 0
            or not str(row.get("input_name") or "")
        ):
            raise WorkflowProjectorError("required_edges 项引用或输出序号无效")
    parameter_policies = definition.get("parameter_policies")
    if not isinstance(parameter_policies, list) or len(parameter_policies) > 128:
        raise WorkflowProjectorError("parameter_policies 必须是最多 128 项的数组")
    parameter_semantic_keys: set[str] = set()
    parameter_targets: set[tuple[str, str]] = set()
    for row in parameter_policies:
        row = _mapping(row, "parameter_policies 项")
        if set(row) - {"constraints"} != {"semantic_key", "node_role", "input_name", "policy", "value"}:
            raise WorkflowProjectorError("parameter_policies 项字段集合无效")
        if "constraints" in row:
            from studio.services.workflow_parameter_ranges import validate_parameter_range, ParameterRangeError
            try:
                validate_parameter_range(row["constraints"])
                if row["constraints"] and row.get("policy") not in {"runtime", "object_info"}:
                    raise ParameterRangeError("仅运行时传入或 object_info 策略允许配置参数范围")
            except ParameterRangeError as exc:
                raise WorkflowProjectorError(str(exc)) from exc
        semantic_key = str(row.get("semantic_key") or "")
        target = (str(row.get("node_role") or ""), str(row.get("input_name") or ""))
        if (
            not semantic_key
            or semantic_key in parameter_semantic_keys
            or target in parameter_targets
            or row.get("node_role") not in node_roles
            or not str(row.get("input_name") or "")
            or row.get("policy") not in {"fixed", "object_info", "source_locked", "runtime"}
            or not isinstance(row.get("value"), _SAFE_SCALAR_TYPES)
        ):
            raise WorkflowProjectorError("parameter_policies 项值重复或无效")
        parameter_semantic_keys.add(semantic_key)
        parameter_targets.add(target)
    asset_slots = definition.get("asset_slots")
    if not isinstance(asset_slots, list) or len(asset_slots) > 64:
        raise WorkflowProjectorError("asset_slots 必须是最多 64 项的数组")
    asset_slot_keys: set[str] = set()
    for row in asset_slots:
        row = _mapping(row, "asset_slots 项")
        if set(row) != {"key", "media_kind", "dynamic", "contract_source"}:
            raise WorkflowProjectorError("asset_slots 项字段集合无效")
        key = str(row.get("key") or "")
        if (
            not key
            or key in asset_slot_keys
            or row.get("media_kind") not in {"image", "video", "audio"}
            or not isinstance(row.get("dynamic"), bool)
            or row.get("contract_source") not in {"workflow", "model_profile"}
        ):
            raise WorkflowProjectorError("asset_slots 项 key 重复或值无效")
        asset_slot_keys.add(key)
    adapters = _mapping(definition.get("adapters"), "adapters")
    if set(adapters) != set(WORKFLOW_PROJECTOR_ADAPTERS) or any(
        adapters.get(kind) not in values
        for kind, values in WORKFLOW_PROJECTOR_ADAPTERS.items()
    ):
        raise WorkflowProjectorError("adapters 必须只引用代码注册的 compiler/output/dynamic adapter")
    if adapters != WORKFLOW_PROJECTOR_ENGINE_ADAPTER_CONTRACTS[engine_key]:
        raise WorkflowProjectorError("adapters 与注册 engine 的受审执行合同不一致")

    projection = _mapping(definition.get("projection"), "projection")
    if set(projection) != {"kind", "config"}:
        raise WorkflowProjectorError("projection 字段集合无效")
    expected_kind = {
        "generic.fixed": "generic_fixed",
        "generic.references": "generic_references",
        "generic.classified": "generic_classified",
    }[engine_key]
    if projection.get("kind") != expected_kind:
        raise WorkflowProjectorError("projection.kind 与注册 engine 不一致")
    config = _mapping(projection.get("config"), "projection.config")
    if engine_key == "generic.references":
        from studio.services.workflow_asset_groups import AssetGroupError, validate_group_config
        try:
            validate_group_config(definition)
        except AssetGroupError as exc:
            raise WorkflowProjectorError(str(exc)) from exc
    if engine_key == "generic.fixed":
        if set(config) - {"image_resolution"}:
            raise WorkflowProjectorError("generic.fixed projection.config 只支持 image_resolution")
        if "image_resolution" in config:
            from studio.services.workflow_resolution import validate_image_resolution_policy, WorkflowResolutionError
            try:
                validate_image_resolution_policy(config["image_resolution"])
            except WorkflowResolutionError as exc:
                raise WorkflowProjectorError(str(exc)) from exc
    if engine_key == "generic.classified":
        from studio.services.workflow_classified_references import validate_config, ClassifiedReferenceError
        try:
            validate_config(definition)
        except ClassifiedReferenceError as exc:
            raise WorkflowProjectorError(str(exc)) from exc

    sensitive = find_forbidden_secret_fields(definition)
    if sensitive:
        raise WorkflowProjectorError(
            "工作流合同方案不能包含明文密钥字段: " + ", ".join(sensitive),
        )
    json_contract(definition, max_bytes=_projector_max_bytes())
    return definition


def current_projector_catalog(
    *,
    provider_driver_type: str = "comfyui",
    operation: str = "workflow.import",
    output_kind: str = "",
) -> list[ProjectorContract]:
    rows = list(WorkflowProjectorVersion.objects.select_related("projector").filter(
        status=WorkflowProjectorVersion.Status.PUBLISHED,
        is_current=True,
        projector__is_active=True,
        provider_driver_type=provider_driver_type,
        operation=operation,
    ).order_by("projector__key"))
    if output_kind:
        rows = [row for row in rows if row.output_kind in {"", output_kind}]
    catalog: list[ProjectorContract] = []
    for row in rows:
        definition_contract = verify_json_contract(
            row.definition,
            schema_version=row.definition_schema_version,
            size_bytes=row.definition_size_bytes,
            sha256=row.definition_sha256,
            max_bytes=_projector_max_bytes(),
        )
        definition = definition_contract.value
        validate_workflow_projector_definition(
            definition,
            engine_key=row.engine_key,
            engine_version=row.engine_version,
        )
        catalog.append(ProjectorContract(
            version_id=str(row.id),
            key=row.projector.key,
            version=row.version,
            provider_driver_type=row.provider_driver_type,
            operation=row.operation,
            output_kind=row.output_kind,
            engine_key=row.engine_key,
            engine_version=row.engine_version,
            definition_sha256=row.definition_sha256,
            definition=definition,
        ))
    if not catalog:
        raise WorkflowProjectorCatalogUnavailable(
            "没有可用的 published 工作流合同方案，请先在系统设置中发布方案",
        )
    return catalog


def validate_frozen_workflow_projector_contract(
    input_contract: dict[str, Any],
) -> ProjectorContract:
    """Verify one frozen projector identity without consulting the current catalog."""

    identity = input_contract.get("contract_projector")
    if identity is None:
        raise WorkflowProjectorError(
            "工作流缺少冻结的 Projector 引用；请从候选工作流重新生成合同并导入新版本，"
            "完成在线校验和真实烟测后再发布。历史验收不能用于新版本。",
        )
    if not isinstance(identity, dict):
        raise WorkflowProjectorError("workflow contract_projector 必须是对象")
    version_id = identity.get("version_id")
    try:
        row = WorkflowProjectorVersion.objects.select_related("projector").get(
            pk=version_id,
            status__in=(
                WorkflowProjectorVersion.Status.PUBLISHED,
                WorkflowProjectorVersion.Status.RETIRED,
            ),
        )
    except (WorkflowProjectorVersion.DoesNotExist, ValueError, TypeError) as exc:
        raise WorkflowProjectorError("workflow 冻结的 projector 版本不存在") from exc
    definition = verify_json_contract(
        row.definition,
        schema_version=row.definition_schema_version,
        size_bytes=row.definition_size_bytes,
        sha256=row.definition_sha256,
        max_bytes=_projector_max_bytes(),
    ).value
    validate_workflow_projector_definition(
        definition,
        engine_key=row.engine_key,
        engine_version=row.engine_version,
    )
    contract = ProjectorContract(
        version_id=str(row.id),
        key=row.projector.key,
        version=row.version,
        provider_driver_type=row.provider_driver_type,
        operation=row.operation,
        output_kind=row.output_kind,
        engine_key=row.engine_key,
        engine_version=row.engine_version,
        definition_sha256=row.definition_sha256,
        definition=definition,
    )
    if identity != contract.identity:
        raise WorkflowProjectorError("workflow 冻结的 projector identity 或 SHA 已漂移")
    frozen_adapters = input_contract.get("workflow_adapters")
    if frozen_adapters != definition["adapters"]:
        raise WorkflowProjectorError("workflow 冻结的 adapter keys 与 projector 版本不一致")
    compiler = input_contract.get("compiler")
    compiler_version = compiler.get("version") if isinstance(compiler, dict) else None
    if (
        not isinstance(compiler, dict)
        or not isinstance(compiler_version, int)
        or isinstance(compiler_version, bool)
        or compiler_version < 1
    ):
        raise WorkflowProjectorError("workflow compiler identity 无效")
    compiler_identity = (
        str(compiler.get("id") or ""),
        compiler_version,
    )
    compiler_adapter = definition["adapters"]["compiler"]
    compatible = compiler_adapter == f"{compiler_identity[0]}/v{compiler_identity[1]}"
    if not compatible:
        raise WorkflowProjectorError("workflow compiler 与 projector adapter 不兼容")
    from studio.services.workflow_resolution import CONFIGURED_RESOLUTION_ADAPTER
    strategy = input_contract.get("resolution_strategy") or {}
    policy = definition["projection"]["config"].get("image_resolution")
    if policy is not None or strategy.get("adapter") == CONFIGURED_RESOLUTION_ADAPTER:
        if policy != {"adapter": strategy.get("adapter"), "sizes": strategy.get("sizes")}:
            raise WorkflowProjectorError("workflow 冻结的尺寸配置与 Projector 版本不一致")
    if contract.engine_key == "generic.references":
        from studio.services.workflow_asset_groups import AssetGroupError, validate_frozen_groups
        try:
            validate_frozen_groups(input_contract, definition)
        except AssetGroupError as exc:
            raise WorkflowProjectorError(str(exc)) from exc
    return contract


def _detectors_may_overlap(left: dict[str, Any], right: dict[str, Any]) -> bool:
    if left["fallback"] or right["fallback"]:
        return True
    left_required = left["node_counts"]
    right_required = right["node_counts"]
    left_optional = left["optional_node_counts"]
    right_optional = right["optional_node_counts"]
    for node_type in set(left_required) | set(right_required):
        if node_type in left_required and node_type in right_required:
            if left_required[node_type] != right_required[node_type]:
                return False
        if node_type in left_required and node_type in right_optional:
            bounds = right_optional[node_type]
            if not bounds["min"] <= left_required[node_type] <= bounds["max"]:
                return False
        if node_type in right_required and node_type in left_optional:
            bounds = left_optional[node_type]
            if not bounds["min"] <= right_required[node_type] <= bounds["max"]:
                return False
    if not left["allow_other_nodes"]:
        allowed = set(left_required) | set(left_optional)
        if any(count > 0 and node_type not in allowed for node_type, count in right_required.items()):
            return False
    if not right["allow_other_nodes"]:
        allowed = set(right_required) | set(right_optional)
        if any(count > 0 and node_type not in allowed for node_type, count in left_required.items()):
            return False
    left_inputs = {
        (row["node_type"], row["input_name"]): row["value"]
        for row in left["input_equals"]
    }
    right_inputs = {
        (row["node_type"], row["input_name"]): row["value"]
        for row in right["input_equals"]
    }
    if any(left_inputs[key] != right_inputs[key] for key in set(left_inputs) & set(right_inputs)):
        return False
    return True


def validate_workflow_projector_publish_conflicts(
    version: WorkflowProjectorVersion,
) -> None:
    definition = validate_workflow_projector_definition(
        version.definition,
        engine_key=version.engine_key,
        engine_version=version.engine_version,
    )
    priority = int(definition["priority"])
    detector = definition["detector"]
    peers = WorkflowProjectorVersion.objects.select_related("projector").filter(
        status=WorkflowProjectorVersion.Status.PUBLISHED,
        is_current=True,
        projector__is_active=True,
        provider_driver_type=version.provider_driver_type,
        operation=version.operation,
    ).exclude(projector=version.projector)
    for peer in peers:
        if version.output_kind and peer.output_kind and version.output_kind != peer.output_kind:
            continue
        peer_definition = verify_json_contract(
            peer.definition,
            schema_version=peer.definition_schema_version,
            size_bytes=peer.definition_size_bytes,
            sha256=peer.definition_sha256,
            max_bytes=_projector_max_bytes(),
        ).value
        validate_workflow_projector_definition(
            peer_definition,
            engine_key=peer.engine_key,
            engine_version=peer.engine_version,
        )
        if (
            int(peer_definition["priority"]) == priority
            and _detectors_may_overlap(peer_definition["detector"], detector)
        ):
            raise WorkflowProjectorAmbiguous(
                "发布会与现有 current projector 形成同优先级且不能证明互斥的 detector 冲突: "
                f"{peer.projector.key} v{peer.version}",
            )


def _matches(projector: ProjectorContract, graph: dict[str, Any]) -> bool:
    detector = projector.definition["detector"]
    if detector["fallback"]:
        return True
    counts = Counter(
        str(node.get("class_type") or "")
        for node in graph.values()
        if isinstance(node, dict)
    )
    required = detector["node_counts"]
    optional = detector["optional_node_counts"]
    if any(counts.get(node_type, 0) != count for node_type, count in required.items()):
        return False
    if any(
        not bounds["min"] <= counts.get(node_type, 0) <= bounds["max"]
        for node_type, bounds in optional.items()
    ):
        return False
    if not detector["allow_other_nodes"]:
        if set(counts) - set(required) - set(optional):
            return False
    for condition in detector["input_equals"]:
        matching_nodes = [
            node for node in graph.values()
            if isinstance(node, dict)
            and node.get("class_type") == condition["node_type"]
        ]
        if len(matching_nodes) != 1:
            return False
        inputs = matching_nodes[0].get("inputs")
        if not isinstance(inputs, dict) or inputs.get(condition["input_name"]) != condition["value"]:
            return False
    role_nodes: dict[str, list[tuple[str, dict[str, Any]]]] = {}
    for role, definition in projector.definition["node_roles"].items():
        rows = [
            (str(node_id), node) for node_id, node in graph.items()
            if isinstance(node, dict) and node.get("class_type") == definition["node_type"]
        ]
        if definition["cardinality"] == "one" and len(rows) != 1:
            return False
        if definition["cardinality"] == "many" and not rows:
            return False
        role_nodes[role] = rows
    for edge in projector.definition["required_edges"]:
        from_rows = role_nodes[edge["from_role"]]
        to_rows = role_nodes[edge["to_role"]]
        if len(from_rows) != 1 or len(to_rows) != 1:
            return False
        inputs = to_rows[0][1].get("inputs")
        if not isinstance(inputs, dict) or inputs.get(edge["input_name"]) != [
            from_rows[0][0], edge["output_index"],
        ]:
            return False
    return True


def select_workflow_projector(
    graph: dict[str, Any],
    *,
    catalog: list[ProjectorContract] | None = None,
) -> ProjectorContract:
    catalog = catalog if catalog is not None else current_projector_catalog()
    specific = [row for row in catalog if not row.definition["detector"]["fallback"] and _matches(row, graph)]
    candidates = specific or [
        row for row in catalog if row.definition["detector"]["fallback"] and _matches(row, graph)
    ]
    if not candidates:
        raise WorkflowProjectorCatalogUnavailable("没有匹配当前图结构的工作流合同方案")
    top_priority = max(int(row.definition["priority"]) for row in candidates)
    selected = [row for row in candidates if int(row.definition["priority"]) == top_priority]
    if len(selected) != 1:
        raise WorkflowProjectorAmbiguous(
            "多个 published 工作流合同方案以相同优先级命中当前图结构",
        )
    return selected[0]


def projector_output_kind(
    node_type: str,
    *,
    catalog: list[ProjectorContract] | None = None,
) -> str:
    catalog = catalog if catalog is not None else current_projector_catalog()
    values = {
        str(row.definition["output_nodes"].get(node_type) or "")
        for row in catalog
        if row.definition["output_nodes"].get(node_type)
    }
    if len(values) > 1:
        raise WorkflowProjectorAmbiguous(f"节点 {node_type} 的输出媒体类型配置冲突")
    return next(iter(values), "")


def projector_model_selector(
    node_type: str,
    input_name: str,
    *,
    catalog: list[ProjectorContract] | None = None,
) -> tuple[str, str, str, str] | None:
    catalog = catalog if catalog is not None else current_projector_catalog()
    values: set[tuple[str, str, str, str]] = set()
    for projector in catalog:
        for row in projector.definition["model_selectors"]:
            if row["node_type"] == node_type and row["input_name"] == input_name:
                values.add((
                    row["role"], row["inventory_folder"],
                    row["inventory_node_type"], row["inventory_input_name"],
                ))
    if len(values) > 1:
        raise WorkflowProjectorAmbiguous(
            f"节点 {node_type}.{input_name} 的模型 selector 配置冲突",
        )
    return next(iter(values), None)


def workflow_projector_creation_template(engine_key: str) -> dict[str, Any]:
    """Unsaved editing skeleton, not an executable or publishable contract.

    This depends only on registered engine identity, never on registry records.
    Empty business fields must pass the normal full validation before saving.
    """
    if engine_key not in WORKFLOW_PROJECTOR_ENGINES:
        raise WorkflowProjectorError("空白新建引擎未注册")
    config: dict[str, Any] = {}
    if engine_key == "generic.references":
        config = {"groups": []}
    elif engine_key == "generic.classified":
        from studio.services.workflow_classified_references import MODEL_INPUTS
        config = {
            "capability": "", "conditioning_mode": "", "target_role": "",
            "prompt_template_keys": [], "static_semantics": {},
            "dynamic_groups": {
                key: {"input_names": [], "prefix": "", "mode_input": value, "minimum_items": None, "maximum_items": None}
                for key, value in MODEL_INPUTS.items()
            },
        }
    return {
        "provider_driver_type": "comfyui", "operation": "workflow.import", "output_kind": "",
        "definition": {
            "schema_version": WORKFLOW_PROJECTOR_DEFINITION_SCHEMA_VERSION, "priority": 0,
            "detector": {"fallback": False, "allow_other_nodes": False, "node_counts": {}, "optional_node_counts": {}, "input_equals": []},
            "output_nodes": {}, "model_selectors": [], "node_roles": {}, "required_edges": [],
            "parameter_policies": [], "asset_slots": [],
            "adapters": deepcopy(WORKFLOW_PROJECTOR_ENGINE_ADAPTER_CONTRACTS[engine_key]),
            "projection": {"kind": _WORKFLOW_PROJECTOR_PROJECTION_KINDS[engine_key], "config": config},
        },
    }


def workflow_projector_engine_options() -> list[dict[str, Any]]:
    return [
        {
            "key": key,
            "version": version,
            "label": _WORKFLOW_PROJECTOR_ENGINE_LABELS[key],
            "description": _WORKFLOW_PROJECTOR_ENGINE_DESCRIPTIONS[key],
            "projection_kind": _WORKFLOW_PROJECTOR_PROJECTION_KINDS[key],
            "adapter_contract": deepcopy(
                WORKFLOW_PROJECTOR_ENGINE_ADAPTER_CONTRACTS[key],
            ),
            "locked_fields": [
                "schema_version", "adapters", "projection.kind",
            ],
            "projection_fields": deepcopy(
                WORKFLOW_PROJECTOR_PROJECTION_FIELDS[key],
            ),
            "creation_template": workflow_projector_creation_template(key),
        }
        for key, version in sorted(WORKFLOW_PROJECTOR_ENGINES.items())
    ]


def workflow_projector_configuration_options() -> dict[str, Any]:
    from studio.services.workflow_asset_groups import WIRING_CHOICES
    from studio.services.workflow_resolution import image_resolution_options
    return {
        "image_resolution": image_resolution_options(),
        "editor_schema_version": WORKFLOW_PROJECTOR_EDITOR_SCHEMA_VERSION,
        "provider_driver_types": [
            {"value": "comfyui", "label": "Local ComfyUI"},
        ],
        "operations": [
            {"value": "workflow.import", "label": "ComfyUI 保存工作流导入"},
        ],
        "output_kinds": [
            {"value": "", "label": "由主输出节点决定"},
            {"value": "image", "label": "图片"},
            {"value": "video", "label": "视频"},
            {"value": "audio", "label": "音频"},
            {"value": "text", "label": "文本"},
        ],
        "adapters": {
            kind: [{"value": value, "label": value} for value in sorted(values)]
            for kind, values in WORKFLOW_PROJECTOR_ADAPTERS.items()
        },
        "definition_editor": {
            "sections": deepcopy(WORKFLOW_PROJECTOR_EDITOR_SECTIONS),
            "choices": {
                "asset_group_wirings": deepcopy(WIRING_CHOICES),
                "cardinalities": [
                    _projector_choice("one", "唯一节点"),
                    _projector_choice("many", "允许多个节点"),
                ],
                "parameter_policies": [
                    _projector_choice("fixed", "固定值"),
                    _projector_choice("object_info", "由实时 object_info 约束"),
                    _projector_choice("source_locked", "锁定导入来源值"),
                    _projector_choice("runtime", "每次运行时传入"),
                ],
                "artifact_roles": [
                    _projector_choice(value, label)
                    for value, label in ModelArtifact.Role.choices
                ],
                "media_kinds": [
                    _projector_choice("image", "图片"),
                    _projector_choice("video", "视频"),
                    _projector_choice("audio", "音频"),
                ],
                "output_kinds": [
                    _projector_choice("image", "图片"),
                    _projector_choice("video", "视频"),
                    _projector_choice("audio", "音频"),
                    _projector_choice("text", "文本"),
                ],
                "contract_sources": [
                    _projector_choice("workflow", "Projector 中配置"),
                    _projector_choice("model_profile", "模型版本合同"),
                ],
                "role_selectors": [
                    _projector_choice("single", "唯一文件"),
                    _projector_choice("video_vae", "视频 VAE"),
                    _projector_choice("audio_vae", "音频 VAE"),
                ],
                "scalar_types": [
                    _projector_choice("string", "文本"),
                    _projector_choice("integer", "整数"),
                    _projector_choice("number", "小数"),
                    _projector_choice("boolean", "布尔值"),
                    _projector_choice("null", "空值"),
                ],
            },
            "limits": {
                "priority_min": 0,
                "priority_max": 1000,
                "node_count_max": 1000,
                "input_equals_max_items": 64,
                "model_selectors_max_items": 128,
                "required_edges_max_items": 128,
                "parameter_policies_max_items": 128,
                "asset_slots_max_items": 64,
            },
        },
    }
