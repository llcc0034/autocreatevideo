"""Read-only configuration navigation, not runtime resolution or acceptance."""
from dataclasses import dataclass
from urllib.parse import urlencode

from django.db.models import Case, CharField, Count, F, Func, IntegerField, JSONField, OuterRef, Q, Subquery, Value, When
from django.db.models.fields.json import KeyTextTransform
from django.db.models.functions import Cast, Coalesce, Lower, Trim

from studio.models import (BusinessOutputVersion, ContentSourceVersion, DataStructureVersion, FieldDefinition,
                           FieldDefinitionVersion, ModelDeployment, ModelProfileVersion, ProductionRecipeVersion,
                           ModelExecutionInstanceVersion, PromptProfileVersion,
                           WorkflowProjectorVersion, WorkflowTemplateVersion)
from studio.services.model_profile_options import model_capability_values
from studio.services.execution_registry_references import RecipeReference

MODELS = dict(model=ModelProfileVersion, deployment=ModelDeployment,
              workflow=WorkflowTemplateVersion, prompt=PromptProfileVersion,
              execution=ModelExecutionInstanceVersion, projector=WorkflowProjectorVersion,
              recipe=ProductionRecipeVersion, output=BusinessOutputVersion, field=FieldDefinition,
              field_version=FieldDefinitionVersion, structure=DataStructureVersion, source=ContentSourceVersion)
LABELS = dict(model="模型版本", deployment="模型部署", workflow="工作流版本",
              prompt="提示词策略", execution="执行实例版本", projector="Projector 版本", recipe="生产配方", output="业务输出版本",
              field="字段身份", field_version="字段定义版本", structure="输入／输出结构", source="内容来源版本")


class UUIDText(Func):
    """JSON identities use canonical UUID strings on both supported databases."""
    output_field = CharField()
    function = "CAST"
    template = "CAST(%(expressions)s AS TEXT)"

    def as_sqlite(self, compiler, connection, **extra_context):
        sql, params = compiler.compile(self.source_expressions[0])
        parts = [f"substr({sql},1,8)", f"substr({sql},9,4)", f"substr({sql},13,4)",
                 f"substr({sql},17,4)", f"substr({sql},21,12)"]
        return "('' || " + " || '-' || ".join(parts) + ")", params * 5




def catalog(kind):
    qs = MODELS[kind].objects.order_by().select_related(None)
    if kind == "field":
        return qs.annotate(status=Case(When(is_active=True, then=Value("active")), default=Value("inactive"), output_field=CharField()))
    if kind == "model":
        return qs.annotate(family=Lower(Trim(KeyTextTransform("model_family", "specification"))))
    if kind in {"deployment", "workflow"}:
        field = "profile_version" if kind == "deployment" else "model_profile_version"
        qs = qs.annotate(family=Lower(Trim(KeyTextTransform("model_family", f"{field}__specification"))))
        if kind == "workflow":
            qs = qs.annotate(projector_identity=Cast(KeyTextTransform("version_id", KeyTextTransform("contract_projector", "input_contract")), CharField()),
                             projector_digest=Cast(KeyTextTransform("definition_sha256", KeyTextTransform("contract_projector", "input_contract")), CharField()))
        return qs
    return qs


@dataclass
class Group:
    key: str
    label: str
    kind: str
    queryset: object


def groups(kind, ref):
    """Same predicate used for summary counts and paginated drill-down."""
    result = []
    def add(key, label, target, query):
        result.append(Group(key, label, target, catalog(target).filter(query)))
    if kind == "field":
        add("versions", "定义版本", "field_version", Q(field_definition_id=ref("pk")))
        add("structures", "引用结构", "structure", Q(nodes__field_definition_version__field_definition_id=ref("pk")))
        add("prompts", "字段规则策略", "prompt", Q(field_rules__field_definition_id=ref("pk")))
    elif kind == "field_version":
        add("field", "字段身份", "field", Q(pk=ref("field_definition_id")))
        add("structures", "引用结构", "structure", Q(nodes__field_definition_version_id=ref("pk")))
        add("prompts", "字段规则策略", "prompt", Q(field_rules__field_definition_version_id=ref("pk")))
    elif kind == "structure":
        add("fields", "节点字段版本", "field_version", Q(structure_nodes__structure_id=ref("pk")))
        add("sources", "内容来源版本", "source", Q(data_structure_version_id=ref("pk")))
        add("outputs", "业务输出版本", "output", Q(data_structure_version_id=ref("pk")))
        add("prompts", "输入结构策略", "prompt", Q(input_structure_version_id=ref("pk")))
        add("executions", "输入结构实例", "execution", Q(inputs__data_structure_version_id=ref("pk")))
    elif kind == "source":
        add("structure", "来源结构", "structure", Q(pk=ref("data_structure_version_id")))
        add("prompts", "引用策略", "prompt", Q(field_rules__source_version_id=ref("pk")))
    if kind == "model":
        add("deployments", "模型部署", "deployment", Q(profile_version_id=ref("pk")))
        add("workflows", "同版本工作流", "workflow", Q(model_profile_version_id=ref("pk")))
        add("executions", "执行实例版本", "execution", Q(deployment__profile_version_id=ref("pk")))
    elif kind == "deployment":
        add("model", "所属模型版本", "model", Q(pk=ref("profile_version_id")))
        add("validated", "校验工作流", "workflow", Q(validated_deployment_id=ref("pk")))
        add("candidates", "其他同模型工作流", "workflow", Q(model_profile_version_id=ref("profile_version_id")) & ~Q(validated_deployment_id=ref("pk")))
        add("executions", "执行实例版本", "execution", Q(deployment_id=ref("pk")))
    elif kind == "workflow":
        add("model", "关联模型版本", "model", Q(pk=ref("model_profile_version_id")))
        add("deployment", "校验部署", "deployment", Q(pk=ref("validated_deployment_id")))
        add("executions", "执行实例版本", "execution", Q(workflow_version_id=ref("pk")))
        # UUID identity is a string in JSON, not the database's native UUID type.
        result.append(Group("projector", "引用 Projector", "projector", catalog("projector").annotate(
            identity=UUIDText(F("pk"))).filter(identity=ref("projector_identity"),
            definition_sha256=ref("projector_digest"))))
    elif kind == "prompt":
        add("executions", "引用执行实例", "execution", Q(prompt_profile_version_id=ref("pk")))
        add("structure", "输入结构", "structure", Q(pk=ref("input_structure_version_id")))
        add("fields", "规则字段版本", "field_version", Q(profile_rules__profile_version_id=ref("pk")))
        add("sources", "内容来源版本", "source", Q(profile_rules__profile_version_id=ref("pk")))
    elif kind == "output":
        add("executions", "引用执行实例", "execution", Q(output_version_id=ref("pk")))
        add("structure", "输出结构", "structure", Q(pk=ref("data_structure_version_id")))
    elif kind == "execution":
        add("deployment", "模型部署", "deployment", Q(pk=ref("deployment_id")))
        add("workflow", "工作流版本", "workflow", Q(pk=ref("workflow_version_id")))
        add("prompt", "提示词策略", "prompt", Q(pk=ref("prompt_profile_version_id")))
        add("output", "业务输出版本", "output", Q(pk=ref("output_version_id")))
        add("structures", "输入结构", "structure", Q(execution_inputs__version_id=ref("pk")))
        add("auxiliaries", "辅助执行实例", "execution", Q(auxiliary_consumers__version_id=ref("pk")))
        add("consumers", "作为辅助被引用", "execution", Q(auxiliaries__target_version_id=ref("pk")))
    elif kind == "projector":
        add("workflows", "引用工作流", "workflow", Q(
            projector_identity=UUIDText(ref("pk")) if isinstance(ref("pk"), OuterRef) else str(ref("pk")),
            projector_digest=ref("definition_sha256")))
    if kind == "recipe":
        definition = ref("definition")
        if not isinstance(definition, OuterRef):
            definition = Value(definition, output_field=JSONField())
        result.append(Group("executions", "阶段执行实例", "execution",
            catalog("execution").filter(RecipeReference(definition, UUIDText(F("pk"))))))
    elif kind == "execution":
        identifier = UUIDText(ref("pk")) if isinstance(ref("pk"), OuterRef) else Value(str(ref("pk")))
        predicate = Q(RecipeReference(F("definition"), identifier))
        result.append(Group("recipes", "引用配方", "recipe", catalog("recipe").filter(predicate)))
    for group in result:
        group.queryset = group.queryset.distinct()
    return result


def summary_queryset(kind):
    annotations = {}
    for group in groups(kind, OuterRef):
        count = group.queryset.annotate(bucket=Value(1)).values("bucket").annotate(total=Count("pk", distinct=True)).values("total")
        annotations["relation_" + group.key] = Coalesce(Subquery(count, output_field=IntegerField()), Value(0))
    return catalog(kind).annotate(**annotations)


def overview_url(kind, **query):
    return "/settings/acceptance-smoke?" + urlencode(dict(view="relations", kind=kind, **query))


def fields(kind):
    common = ["id", "status"]
    return common + {
        "model": ["profile_key", "version", "modality", "provider__driver_type", "family", "specification__capabilities"],
        "deployment": ["name", "key", "profile_version_id", "profile_version__provider__driver_type", "profile_version__specification__capabilities"],
        "workflow": ["template__name", "version", "model_profile_version_id", "validation_state", "smoke_test_state"],
        "prompt": ["name", "version", "operation", "mode"],
        "execution": ["instance_id", "instance__name", "instance__is_active", "version", "operation", "mode", "prompt_mode"],
        "projector": ["projector__name", "projector__is_active", "version", "engine_key", "engine_version", "is_current"],
        "recipe": ["name", "recipe_key", "version"],
        "output": ["name", "version", "parser_key"],
        "field": ["key", "is_active"],
        "field_version": ["label", "version", "field_definition_id", "field_definition__key", "value_type"],
        "structure": ["name", "version"],
        "source": ["name", "version", "reader_selector"],
    }[kind]


def row_summary(kind, row):
    object_id = str(row["id"])
    name = row.get("instance__name") or row.get("profile_key") or row.get("template__name") or row.get("projector__name") or row.get("name") or row.get("label") or row.get("field_definition__key") or row.get("key")
    label = f"{name} · v{row['version']}" if "version" in row else name
    query = {"section": {"model": "models", "deployment": "models", "workflow": "workflows", "prompt": "prompts", "execution": "models", "projector": "workflows", "recipe": "routing", "output": "models", "field": "fields", "field_version": "fields", "structure": "fields", "source": "fields"}[kind]}
    if kind == "field": query.update(field_definition=object_id)
    if kind == "field_version": query.update(field_definition=str(row["field_definition_id"]), field_version=object_id)
    if kind == "structure": query.update(structure_version=object_id)
    if kind == "source": query.update(source_version=object_id)
    if kind == "model": query.update(model_profile_version=object_id)
    if kind == "deployment": query.update(model_profile_version=str(row["profile_version_id"]), deployment=object_id, focus="deployments")
    if kind == "prompt": query.update(prompt_profile_version=object_id)
    if kind == "execution": query.update(execution_version=object_id)
    if kind == "projector": query.update(projector_version=object_id)
    if kind == "recipe": query.update(recipe_version=object_id)
    if kind == "output": query.update(output_version=object_id)
    config_url = f"/settings/workflows/{object_id}" if kind == "workflow" else "/settings?" + urlencode(query)
    relation_groups = [{"key": group.key, "label": group.label, "kind": group.kind,
                        "count": row["relation_" + group.key],
                        "url": overview_url(group.kind, source_kind=kind, source_id=object_id, relation=group.key)}
                       for group in groups(kind, OuterRef)]
    notices = []
    capabilities = []
    capability_field = {"model": "specification__capabilities", "deployment": "profile_version__specification__capabilities"}.get(kind)
    if capability_field:
        from django.core.exceptions import ValidationError
        try:
            capabilities = list(model_capability_values({"capabilities": row[capability_field]}))
        except ValidationError:
            notices.append("模型能力声明格式不合法，请在原模型版本中核对；不推测能力。")
    elif kind in {"prompt", "execution"}:
        capabilities = [row["operation"] + (" / " + row["mode"] if row["mode"] else "")]
    elif kind == "projector":
        capabilities = [f"{row['engine_key']}/v{row['engine_version']}"]
    elif kind == "output":
        capabilities = [row["parser_key"]]
    elif kind == "field_version":
        capabilities = [row["value_type"]] if row["value_type"] else []
    elif kind == "source":
        capabilities = [row["reader_selector"]]
    if kind in {"field", "field_version", "structure", "source"}:
        notices.append("按确切版本关联逐层查看使用处；同名版本不合并。沿结构、来源／输出或策略可继续查看执行实例及配方。")
        notices.append("这里展示当前保存的配置关系，不是冻结请求、历史产物或全部运行期消费者清单；修改或新版本不会自动替换已有引用。")
    if kind == "model":
        driver = row["provider__driver_type"]
        retired = row["status"] == "retired"
        if not row["relation_deployments"]:
            notices.append("历史版本未记录模型部署。" if retired else "尚无模型部署；模型版本发布后可在原页面新增。")
        if driver == "comfyui" and not row["relation_workflows"]:
            notices.append("历史版本未记录关联工作流。" if retired else "ComfyUI 尚无关联工作流；发布模型版本后到工作流页导入并绑定。")
        if driver != "comfyui": notices.append("非 ComfyUI 模型不统一要求 Workflow / Projector；以 operation 合同为准。")
    if kind in {"model", "deployment", "workflow", "prompt"} and not row["relation_executions"]:
        notices.append("尚无执行实例版本引用；业务调用需在模型页配置精确执行实例，不通过模型族推测组合。")
    if row.get("status") == "retired":
        notices.insert(0, "已退役记录仅供历史追溯；不要求为此版本补齐配置，新配置应使用可用版本。")
    if kind == "workflow":
        if not row["relation_model"]: notices.append("未关联模型版本。")
        if not row["relation_deployment"]: notices.append("尚未记录校验部署；导入完成不等于在线验收通过。")
        if not row["relation_projector"]: notices.append("Projector 引用缺失或不一致，不能执行；请从候选重新生成新版本。历史合同不可补挂当前版本。")
    if kind == "deployment" and not row["relation_validated"] and row["profile_version__provider__driver_type"] == "comfyui":
        notices.append("尚无以此部署为校验目标的工作流；从候选工作流进入验收。")
    if kind == "projector" and not row["projector__is_active"]: notices.append("方案族已停用；历史冻结引用仍保留。")
    if kind == "execution":
        if not row["instance__is_active"]: notices.append("执行实例已停用，不能创建新调用。")
        notices.append({"profile": "使用固定版本的提示词策略；辅助实例按已配置用途参与字段生成或翻译。",
            "manual": "人工提交完整提示词，不使用提示词策略。", "none": "此执行版本不需要提示词。"}[row["prompt_mode"]])
        notices.append("此处是配置关系，不是烟测结论或运行次数；实际选路与请求见原任务记录。")
    acceptance_url = ""
    if kind == "workflow": acceptance_url = "/settings/acceptance-smoke?" + urlencode(dict(subject="workflow", workflow_id=object_id))
    if kind == "deployment": acceptance_url = "/settings/acceptance-smoke?" + urlencode(dict(subject="deployment", deployment_id=object_id))
    if kind == "execution" and row["status"] != "draft": acceptance_url = "/settings?" + urlencode(dict(section="models", execution_version=object_id, execution_view="smoke"))
    if kind == "recipe": notices.append("只展示当前配方各阶段引用的精确执行实例；实际选路见本轮 ProductionRun。")
    if kind == "output":
        notices.append("按精确输出版本外键列出引用实例，包含草稿、已发布、退役及停用实例；不是成功调用或已审核采用次数。")
        notices.append("后继版本不会替换这些引用；退役后不能被新组合引用。已有请求、输出及审核事实保留，实际调用仍核对任务冻结合同。")
        notices.append("进入引用执行实例可继续查看配方和辅助调用方；这不是全部运行期下游数据消费者的影响清单。")
    return dict(id=object_id, kind=kind, label=label, status=row.get("status") or ("active" if row.get("is_active") else "inactive"),
                config_url=config_url, overview_url=overview_url(kind, ids=object_id), acceptance_url=acceptance_url,
                groups=relation_groups, notices=notices, capabilities=capabilities)
