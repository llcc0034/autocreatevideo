"""Read-only configuration navigation, not runtime resolution or acceptance."""
from dataclasses import dataclass
from urllib.parse import urlencode

from django.db.models import BooleanField, CharField, Count, F, Func, IntegerField, JSONField, OuterRef, Q, Subquery, Value
from django.db.models.fields.json import KeyTextTransform
from django.db.models.functions import Cast, Coalesce, Lower, Trim

from studio.models import (ModelDeployment, ModelProfileVersion, ProductionRecipeVersion,
                           PromptProfileBinding, PromptProfileVersion,
                           WorkflowProjectorVersion, WorkflowTemplateVersion)
from studio.services.registry_deletion import RECIPE_UUID_REFERENCE_MODELS, RECIPE_KEY_REFERENCE_MODELS
from studio.services.model_profile_options import model_capability_values

MODELS = dict(model=ModelProfileVersion, deployment=ModelDeployment,
              workflow=WorkflowTemplateVersion, prompt=PromptProfileVersion,
              binding=PromptProfileBinding, projector=WorkflowProjectorVersion,
              recipe=ProductionRecipeVersion)
LABELS = dict(model="模型版本", deployment="运行实例", workflow="工作流版本",
              prompt="提示词策略", binding="提示词绑定", projector="Projector 版本", recipe="生产配方")


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


class RecipeReference(Func):
    """Exact allow-listed reference keys, including array values; never name search.

    Evaluated in SQL so filtering/count/pagination agree. This intentionally scans
    the small recipe registry, not ProductionRun history or external systems.
    """
    output_field = BooleanField()

    def __init__(self, definition, identifier, keys):
        self.keys = keys
        super().__init__(definition, identifier)

    def as_sqlite(self, compiler, connection, **extra_context):
        definition, dp = compiler.compile(self.source_expressions[0])
        identifier, ip = compiler.compile(self.source_expressions[1])
        placeholders = ",".join(["%s"] * len(self.keys))
        return (f"EXISTS (SELECT 1 FROM json_tree({definition}) r "
                f"LEFT JOIN json_tree({definition}) p ON p.id=r.parent "
                f"WHERE (r.key IN ({placeholders}) OR (p.type='array' AND p.key IN ({placeholders}))) "
                f"AND r.type='text' AND r.value={identifier})",
                dp + dp + list(self.keys) + list(self.keys) + ip)

    def as_postgresql(self, compiler, connection, **extra_context):
        definition, dp = compiler.compile(self.source_expressions[0])
        identifier, ip = compiler.compile(self.source_expressions[1])
        clauses, params = [], []
        for key in self.keys:
            clauses.append(f"jsonb_path_exists({definition}, %s::jsonpath, jsonb_build_object('target', {identifier}))")
            params.extend(dp + [f'$.**."{key}" ? (@ == $target)'] + ip)
        return "(" + " OR ".join(clauses) + ")", params


def catalog(kind):
    qs = MODELS[kind].objects.order_by().select_related(None)
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
    def bindings(profile, family):
        return (Q(model_profile_version_id=profile) & Q(model_profile_version__isnull=False)) | (Q(model_profile_version__isnull=True, model_family=family) & ~Q(model_family=""))
    if kind == "model":
        add("deployments", "运行实例", "deployment", Q(profile_version_id=ref("pk")))
        add("workflows", "同版本工作流", "workflow", Q(model_profile_version_id=ref("pk")))
        add("bindings", "提示词绑定", "binding", bindings(ref("pk"), ref("family")))
    elif kind == "deployment":
        add("model", "所属模型版本", "model", Q(pk=ref("profile_version_id")))
        add("validated", "校验工作流", "workflow", Q(validated_deployment_id=ref("pk")))
        add("candidates", "其他同模型工作流", "workflow", Q(model_profile_version_id=ref("profile_version_id")) & ~Q(validated_deployment_id=ref("pk")))
        add("bindings", "提示词绑定", "binding", bindings(ref("profile_version_id"), ref("family")))
    elif kind == "workflow":
        add("model", "关联模型版本", "model", Q(pk=ref("model_profile_version_id")))
        add("deployment", "校验实例", "deployment", Q(pk=ref("validated_deployment_id")))
        add("bindings", "提示词绑定", "binding", bindings(ref("model_profile_version_id"), ref("family")))
        # UUID identity is a string in JSON, not the database's native UUID type.
        result.append(Group("projector", "引用 Projector", "projector", catalog("projector").annotate(
            identity=UUIDText(F("pk"))).filter(identity=ref("projector_identity"),
            definition_sha256=ref("projector_digest"))))
    elif kind == "prompt":
        add("bindings", "引用绑定", "binding", Q(prompt_profile_version_id=ref("pk")))
    elif kind == "binding":
        add("prompt", "提示词版本", "prompt", Q(pk=ref("prompt_profile_version_id")))
        add("models", "适用模型", "model", Q(pk=ref("model_profile_version_id")))
        # The exact/family branch must be correlated, not guessed from the name.
        exact = ref("model_profile_version_id")
        if isinstance(exact, OuterRef):
            from django.db.models.lookups import IsNull
            result[-1].queryset = catalog("model").filter(Q(pk=exact) | (Q(IsNull(exact, True)) & Q(family=ref("model_family")) & ~Q(family="")))
        elif exact is None:
            result[-1].queryset = catalog("model").filter(family=ref("model_family")).exclude(family="")
    elif kind == "projector":
        add("workflows", "引用工作流", "workflow", Q(
            projector_identity=UUIDText(ref("pk")) if isinstance(ref("pk"), OuterRef) else str(ref("pk")),
            projector_digest=ref("definition_sha256")))
    if kind == "recipe":
        definition = ref("definition")
        if not isinstance(definition, OuterRef):
            definition = Value(definition, output_field=JSONField())
        for target in ("model", "deployment", "workflow", "binding"):
            keys = [key for key, model in RECIPE_UUID_REFERENCE_MODELS.items() if model is MODELS[target]]
            query = catalog(target).filter(RecipeReference(definition, UUIDText(F("pk")), keys))
            if target == "binding":
                key_refs = list(RECIPE_KEY_REFERENCE_MODELS)
                query = catalog(target).filter(Q(RecipeReference(definition, UUIDText(F("pk")), keys)) | Q(RecipeReference(definition, F("key"), key_refs)))
            result.append(Group(target, "路由声明 · " + LABELS[target], target, query))
    elif kind in {"model", "deployment", "workflow", "binding"}:
        keys = [key for key, model in RECIPE_UUID_REFERENCE_MODELS.items() if model is MODELS[kind]]
        identifier = UUIDText(ref("pk")) if isinstance(ref("pk"), OuterRef) else Value(str(ref("pk")))
        predicate = Q(RecipeReference(F("definition"), identifier, keys))
        if kind == "binding":
            key = ref("key")
            predicate |= Q(RecipeReference(F("definition"), key if isinstance(key, OuterRef) else Value(key), list(RECIPE_KEY_REFERENCE_MODELS)))
        result.append(Group("recipes", "引用配方", "recipe", catalog("recipe").filter(predicate)))
    return result


def summary_queryset(kind):
    annotations = {}
    for group in groups(kind, OuterRef):
        count = group.queryset.annotate(bucket=Value(1)).values("bucket").annotate(total=Count("pk")).values("total")
        annotations["relation_" + group.key] = Coalesce(Subquery(count, output_field=IntegerField()), Value(0))
    return catalog(kind).annotate(**annotations)


def overview_url(kind, **query):
    return "/settings/acceptance-smoke?" + urlencode(dict(view="relations", kind=kind, **query))


def fields(kind):
    common = ["id", "status"] if kind != "binding" else ["id", "is_active"]
    return common + {
        "model": ["profile_key", "version", "modality", "provider__driver_type", "family", "specification__capabilities"],
        "deployment": ["name", "key", "profile_version_id", "profile_version__provider__driver_type", "profile_version__specification__capabilities"],
        "workflow": ["template__name", "version", "model_profile_version_id", "validation_state", "smoke_test_state"],
        "prompt": ["name", "version", "operation", "mode"],
        "binding": ["name", "key", "operation", "mode", "model_family", "model_profile_version_id", "prompt_profile_version_id"],
        "projector": ["projector__name", "projector__is_active", "version", "engine_key", "engine_version", "is_current"],
        "recipe": ["name", "recipe_key", "version"],
    }[kind]


def row_summary(kind, row):
    object_id = str(row["id"])
    name = row.get("profile_key") or row.get("template__name") or row.get("projector__name") or row.get("name") or row.get("key")
    label = f"{name} · v{row['version']}" if "version" in row else name
    query = {"section": {"model": "models", "deployment": "models", "workflow": "workflows", "prompt": "prompts", "binding": "prompts", "projector": "workflows", "recipe": "routing"}[kind]}
    if kind == "model": query.update(model_profile_version=object_id)
    if kind == "deployment": query.update(model_profile_version=str(row["profile_version_id"]), deployment=object_id, focus="deployments")
    if kind == "prompt": query.update(prompt_profile_version=object_id)
    if kind == "binding": query.update(prompt_profile_version=str(row["prompt_profile_version_id"]), binding=object_id)
    if kind == "projector": query.update(projector_version=object_id)
    if kind == "recipe": query.update(recipe_version=object_id)
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
    elif kind in {"prompt", "binding"}:
        capabilities = [row["operation"] + (" / " + row["mode"] if row["mode"] else "")]
    elif kind == "projector":
        capabilities = [f"{row['engine_key']}/v{row['engine_version']}"]
    if kind == "model":
        driver = row["provider__driver_type"]
        retired = row["status"] == "retired"
        if not row["relation_deployments"]:
            notices.append("历史版本未记录运行实例。" if retired else "尚无运行实例；模型版本发布后可在原页面新增。")
        if driver == "comfyui" and not row["relation_workflows"]:
            notices.append("历史版本未记录关联工作流。" if retired else "ComfyUI 尚无关联工作流；发布模型版本后到工作流页导入并绑定。")
        if driver != "comfyui": notices.append("非 ComfyUI 模型不统一要求 Workflow / Projector；以 operation 合同为准。")
        if not row["family"]: notices.append("模型族缺失，无法列出模型族通用提示词绑定。")
    if kind in {"model", "deployment", "workflow", "prompt"} and not row["relation_bindings"]:
        notices.append("历史版本未记录提示词绑定。" if row.get("status") == "retired" else "尚无提示词绑定；是否必需取决于操作与模式，可到提示词页配置。")
    if row.get("status") == "retired":
        notices.insert(0, "已退役记录仅供历史追溯；不要求为此版本补齐配置，新配置应使用可用版本。")
    if kind == "workflow":
        if not row["relation_model"]: notices.append("未关联模型版本。")
        if not row["relation_deployment"]: notices.append("尚未记录校验实例；导入完成不等于在线验收通过。")
        if not row["relation_projector"]: notices.append("Projector 引用缺失或不一致，不能执行；请从候选重新生成新版本。历史合同不可补挂当前版本。")
    if kind == "deployment" and not row["relation_validated"] and row["profile_version__provider__driver_type"] == "comfyui":
        notices.append("尚无以此实例为校验目标的工作流；从候选工作流进入验收。")
    if kind == "projector" and not row["projector__is_active"]: notices.append("方案族已停用；历史冻结引用仍保留。")
    if kind == "binding": notices.append(f"适用范围：{'指定模型版本' if row['model_profile_version_id'] else '同模型族'} · {row['operation']} / {row['mode'] or '未限定模式'}。")
    acceptance_url = ""
    if kind == "workflow": acceptance_url = "/settings/acceptance-smoke?" + urlencode(dict(subject="workflow", workflow_id=object_id))
    if kind == "deployment": acceptance_url = "/settings/acceptance-smoke?" + urlencode(dict(subject="deployment", deployment_id=object_id))
    if kind == "recipe": notices.append("这里只展示配方显式引用；继承或运行时解析请在配方审核详情、ProductionRun 中核对。")
    return dict(id=object_id, kind=kind, label=label, status=row.get("status") or ("active" if row.get("is_active") else "inactive"),
                config_url=config_url, overview_url=overview_url(kind, ids=object_id), acceptance_url=acceptance_url,
                groups=relation_groups, notices=notices, capabilities=capabilities)
