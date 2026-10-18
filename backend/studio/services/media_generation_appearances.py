"""Exact appearance declarations for the original media preparation.

No target queue, setting writer, primary selection, or input remapping. A
declared primary use must reach a real image port in the ordinary resolved inputs.
"""
from copy import deepcopy
from uuid import UUID

from django.core.exceptions import ValidationError
from django.db.models import F, Q
from django.db.models.functions import Coalesce

from studio.models import AssetReferenceVersion, BibleVariantRevision
from studio.services.bible_setting_evidence import primary_setting_content_reference, read_setting_revisions
from studio.services.execution_media_sources import resolve_execution_asset_media
from studio.services.execution_instances import execution_configuration_max_bytes, execution_resolved_inputs_max_bytes
from studio.services.execution_field_contracts import verify_execution_field_request
from studio.services.execution_profile_inputs import project_execution_profile_sources
from studio.services.execution_prompts import normalize_manual_execution_channels
from studio.services.execution_workflow_media import prepare_execution_workflow_media
from studio.services.execution_workflow_uses import read_execution_workflow_uses
from studio.services.json_contracts import JsonContract, json_contract
from studio.services.reference_context_facts import read_reference_context_facts
from studio.services.prompt_compiler_contract import PromptBuildError
from studio.services.prompt_profile_structures import STRUCTURED_INPUT_SELECTOR, read_profile_path
from studio.services.security_boundaries import security_boundary_resolution_cache


def appearance_options(*, owner, role, search=""):
    """Bounded directory projection; no setting body, adoption or execution."""
    if role not in ("primary", "variant"):
        _invalid("设定目录必须明确主图或变体角色")
    rows = BibleVariantRevision.objects.filter(variant__project__owner=owner,
        variant__appearance_role=role, revision=F("variant__revision"))
    if search:
        rows = rows.filter(Q(name__icontains=search) | Q(reference_name__icontains=search)
            | Q(variant__project__title__icontains=search) | Q(variant__character__name__icontains=search)
            | Q(variant__scene__name__icontains=search) | Q(variant__prop__name__icontains=search))
    return rows.annotate(entity_name=Coalesce("variant__character__name", "variant__scene__name", "variant__prop__name"))\
        .values("id", "variant_id", "variant__project_id", "variant__project__title", "entity_name",
            "name", "reference_name", "revision").order_by("variant__project__title", "name", "id")


def _invalid(message):
    raise ValidationError({"appearance_target": message})


def _uuid(value):
    try:
        valid = isinstance(value, str) and str(UUID(value)) == value
    except ValueError:
        valid = False
    if not valid:
        _invalid("外观目标必须使用确切对象UUID，不接受名称或最新版别名")
    return value


def normalize_media_appearance_request(value):
    if value is None:
        return None
    keys = {"role", "setting_revision_id", "primary_version_id", "primary_input"}
    if isinstance(value, dict) and value.get("role") == "variant":
        keys.add("difference_input")
    if (not isinstance(value, dict) or set(value) != keys
            or value["role"] not in ("primary", "variant")):
        _invalid("外观目标须明确主图/变体角色、可空设定、确切主图版本及输入位置")
    if value["setting_revision_id"] is not None:
        _uuid(value["setting_revision_id"])
    if value["role"] == "primary":
        if value["primary_version_id"] is not None or value["primary_input"] is not None:
            _invalid("主图目标不能声明变体生成依据；其他参考图仍走普通输入")
    else:
        _uuid(value["primary_version_id"])
        use = value["primary_input"]
        if (not isinstance(use, dict) or set(use) != {"input_key", "item_key"}
                or any(not isinstance(item, str) or not item.strip() or len(item) > 191 for item in use.values())):
            _invalid("变体须明确主图在原输入中的逻辑键和使用标识，不能按图片名称猜测")
        _difference_source(value["difference_input"])
    return deepcopy(value)


def _difference_source(value):
    keys = {"kind", "key"}
    if isinstance(value, dict) and value.get("kind") == "reference_field":
        keys |= {"unit_key", "field_key"}
    elif isinstance(value, dict) and value.get("kind") == "structured_field":
        keys.add("field_key")
    if (not isinstance(value, dict) or set(value) != keys
            or value["kind"] not in ("manual_channel", "logical_input", "reference_field", "structured_field")
            or any(not isinstance(value[key], str) or not value[key].strip() or len(value[key]) > 191
                for key in keys - {"kind"})):
        _invalid("变体须引用原人工通道、已映射文字输入、结构化直通字段或确切参考单元字段作为差异，不另存正文")


def _structured_field_difference(source, *, logical, projection, inputs):
    """Use the published Profile path, not a path supplied by the caller."""
    key, field_key = source["key"], source["field_key"]
    uses = [use for row in projection["provenance"] if row["input_key"] == key for use in row["uses"]
        if use["field_key"] == field_key and use["usage"] in {"assembly_source", "generation_source"}
        and use["selector"] == STRUCTURED_INPUT_SELECTOR and "source_path" in use]
    if len(uses) != 1:
        _invalid("差异须对应唯一的原结构化字段正文来源，不接受上下文或调用方另选路径")
    text, location, present = read_profile_path(logical[key], uses[0]["source_path"], field_key)
    if (uses[0]["usage"] == "assembly_source" and projection["assembly_sources"].get(STRUCTURED_INPUT_SELECTOR) != logical.get(key)
            or uses[0]["usage"] == "generation_source" and projection["generation_sources"].get(field_key) != text):
        _invalid("差异须对应本次实际输入字段，不能以生成结果替换原始差异来源")
    origins = [row for row in inputs.get("input_provenance", []) if row.get("input_key") == key]
    if len(origins) != 1 or not isinstance(origins[0].get("structure_resolution"), dict):
        _invalid("差异结构化字段缺少原输入结构解析证据")
    path = f"inputs.{key}" + location[len(STRUCTURED_INPUT_SELECTOR):]
    defaults = origins[0]["structure_resolution"].get("defaults")
    if (not present or not isinstance(defaults, list)
            or any(path == row["path"] or path.startswith(row["path"] + ".")
                or path.startswith(row["path"] + "[") for row in defaults)):
        _invalid("差异须来自原结构化来源的实际文字，不能从配置默认值补齐")
    return text


def _reference_field_difference(source, *, logical, projection, profile, resolved_inputs):
    """Read the existing per-unit field request, never a new selector/renderer."""
    key, unit_key, field_key = source["key"], source["unit_key"], source["field_key"]
    if not any(row["input_key"] == key and any(use["usage"] == "reference_units"
            and use["field_key"] == field_key for use in row["uses"]) for row in projection["provenance"]):
        _invalid("差异输入未映射到该参考字段；仅辅助上下文不能作为正文")
    reference = projection.get("reference")
    if not isinstance(reference, dict) or reference.get("unit_input") != logical.get(key):
        _invalid("差异参考单元与原输入投影不一致")
    try:
        context = read_reference_context_facts(JsonContract(**profile["reference_context"]),
            max_bytes=execution_configuration_max_bytes())
    except (KeyError, TypeError, ValueError, PromptBuildError):
        _invalid("差异缺少有效的冻结参考事实")
    units = [row for row in context.ordered_units if row.unit_key == unit_key]
    original = [row for row in reference["unit_input"]["ordered_units"] if row["unit_key"] == unit_key]
    if len(units) != 1 or len(original) != 1:
        _invalid("差异必须引用原输入中的确切单元，不能换单元或补写来源")
    from dataclasses import asdict
    unit_basis = {key: value for key, value in asdict(units[0]).items() if key != "render_values"}
    original_basis = {key: value for key, value in original[0].items() if key != "structured_input"}
    if json_contract(unit_basis, max_bytes=execution_configuration_max_bytes()).sha256 != json_contract(
            original_basis, max_bytes=execution_configuration_max_bytes()).sha256:
        _invalid("差异单元的时序或主体事实与原输入不一致")
    requests = resolved_inputs.get("field_requests")
    if not isinstance(requests, list):
        _invalid("差异缺少原参考字段准备")
    matches = []
    for envelope in requests:
        try:
            task = verify_execution_field_request(JsonContract(**envelope))
        except (TypeError, ValueError):
            _invalid("差异字段准备合同无效")
        if task["profile"] != profile["profile"]:
            _invalid("差异字段准备不属于本次确切策略")
        matches.extend(row for row in task["items"] if row["unit_key"] == unit_key
            and row["field_contract"]["field_key"] == field_key)
    if len(matches) != 1:
        _invalid("差异须对应唯一的已准备参考字段，不接受未消费或重复字段")
    row = matches[0]
    rule = row["field_contract"]
    if rule["generation_source_selector"] == STRUCTURED_INPUT_SELECTOR:
        if "structured_input" not in original[0]:
            _invalid("差异缺少本单元的结构化原值")
        original_text, _, present = read_profile_path(original[0]["structured_input"], rule["source_path"], field_key)
        if not present:
            _invalid("差异须来自结构化原值的实际路径，不能从配置默认补齐")
    else:
        _invalid("差异参考字段必须使用本单元的结构化来源路径")
    if (rule["source_selector"] != "projection.output" or rule["generation_output_semantics"] != "contribution"
            or row["source_text"] != original_text):
        _invalid("差异字段并非该单元的原始正文贡献来源")
    return row["source_text"]


@security_boundary_resolution_cache()
def media_appearance_difference(source, *, definition, resolved_inputs, request):
    """Project a small identity from the original request/source, not new text.

    This establishes prepared input consumption, not a generated result or a
    guarantee that an auxiliary model preserves every word. Structured reference
    units refer to their original prepared field, never stringify a structure.
    """
    _difference_source(source)
    prompt = definition.value["interface"]["prompt"]
    inputs = resolved_inputs.get("inputs")
    if not isinstance(inputs, dict):
        _invalid("差异来源缺少原准备输入")
    key = source["key"]
    if source["kind"] == "manual_channel":
        if prompt["mode"] != "manual" or key not in prompt["channels"]:
            _invalid("差异人工通道未被当前实例声明消费")
        supplied = request.get("manual_channels") if isinstance(request, dict) else None
        # Require explicitly supplied text, never a channel default/constant.
        if not isinstance(supplied, dict) or key not in supplied:
            _invalid("差异须来自原请求明确提供的人工通道，不能从配置补齐")
        labels = []
        if definition.value["target"]["workflow"] is not None:
            labels = prepare_execution_workflow_media(definition,
                json_contract(inputs, max_bytes=execution_resolved_inputs_max_bytes())).value["declared_reference_labels"]
        channels = normalize_manual_execution_channels(prompt, supplied, declared_reference_labels=labels)
        text = channels["values"].get(key)
    else:
        if prompt["mode"] != "profile":
            _invalid("差异文字输入必须通过当前实例的策略映射消费")
        logical = inputs.get("logical_values")
        spec = next((row for row in definition.value["configuration"]["inputs"] if row["key"] == key), None)
        expected_kind = "text" if source["kind"] == "logical_input" else "json"
        if not spec or spec["kind"] != expected_kind or not isinstance(logical, dict):
            _invalid("差异文字输入不存在或不是明确文本，不能字符串化结构或媒体")
        projection = project_execution_profile_sources(definition, logical)
        profile = resolved_inputs.get("profile_sources")
        if (not isinstance(profile, dict) or profile.get("projection") != projection
                or profile.get("profile") != {"version_id": prompt["profile_id"], "contract_sha256": prompt["profile_sha256"]}):
            _invalid("差异策略来源与本次冻结投影不一致")
        if source["kind"] == "reference_field":
            text = _reference_field_difference(source, logical=logical, projection=projection,
                profile=profile, resolved_inputs=resolved_inputs)
        elif source["kind"] == "structured_field":
            text = _structured_field_difference(source, logical=logical, projection=projection, inputs=inputs)
        elif not any(row["input_key"] == key and any(use["usage"] in ("assembly_source", "generation_source")
                for use in row["uses"]) for row in projection["provenance"]):
            _invalid("差异文字未映射到策略正文来源；仅上下文输入不能充当差异正文")
        else:
            text = logical.get(key)
    if not isinstance(text, str) or not text.strip():
        _invalid("变体差异必须是非空文本；不能仅凭主图或设定关系提交")
    return {**source, "value_sha256": json_contract(text, max_bytes=execution_configuration_max_bytes()).sha256}


def resolve_media_appearance_target(value, *, owner, max_media_bytes, definition, resolved_inputs, request,
        project_id=None, production_scope=None):
    declaration = normalize_media_appearance_request(value)
    if declaration is None:
        return None
    difference = (media_appearance_difference(declaration["difference_input"], definition=definition,
        resolved_inputs=resolved_inputs, request=request) if declaration["role"] == "variant" else None)
    request = declaration
    setting = None
    if request["setting_revision_id"] is not None:
        rows = BibleVariantRevision.objects.filter(pk=request["setting_revision_id"], variant__project__owner=owner)
        if project_id is not None:
            rows = rows.filter(variant__project_id=project_id)
        identity = rows.values("variant__project_id").first()
        if identity is None:
            _invalid("目标设定不存在或不属于当前所有者及项目")
        setting = read_setting_revisions([request["setting_revision_id"]],
            project_id=identity["variant__project_id"])[request["setting_revision_id"]]
        if setting.variant.appearance_role != request["role"]:
            _invalid("目标角色与所选确切设定不一致")
        if production_scope is not None and (production_scope.get("kind") not in {"character", "scene", "prop"}
                or str(getattr(setting.variant, f'{production_scope["kind"]}_id')) != production_scope.get("id")):
            _invalid("目标设定不属于本次制作实体")
    primary = None
    if request["role"] == "variant":
        row = AssetReferenceVersion.objects.select_related("series", "asset").filter(
            pk=request["primary_version_id"], series__owner=owner, series__role="primary").first()
        if row is None:
            _invalid("变体须引用本所有者的确切主图素材版本，不能引用兄弟变体")
        if setting is not None and (setting.primary_setting_revision_id is None
                or row.setting_revision_id != setting.primary_setting_revision_id):
            _invalid("所选主图素材版本与变体固定的主图设定修订不一致")
        media = resolve_execution_asset_media(owner=owner, asset_id=row.asset_id,
            expected_kind="image", max_media_bytes=max_media_bytes)
        primary = {"version_id": str(row.pk), "series_id": str(row.series_id),
            "asset_id": str(media.asset.pk), "sha256": media.media_facts["sha256"]}
    return {"role": request["role"], "setting": primary_setting_content_reference(setting),
        "primary": primary, "primary_input": request["primary_input"],
        **({"difference": difference} if difference is not None else {})}


@security_boundary_resolution_cache()
def verify_media_appearance_target(value, inputs, *, definition, resolved_inputs=None, request=None):
    """Validate original frozen evidence only; never reread current selections."""
    keys = {"role", "setting", "primary", "primary_input"}
    if isinstance(value, dict) and value.get("role") == "variant":
        keys.add("difference")
    if not isinstance(value, dict) or set(value) != keys:
        _invalid("冻结外观目标结构无效")
    setting, primary = value["setting"], value["primary"]
    if setting is not None:
        if (not isinstance(setting, dict) or set(setting) != {"appearance_id", "setting_revision_id", "revision", "content_sha256"}
                or type(setting["revision"]) is not int or setting["revision"] < 1):
            _invalid("冻结设定缺少确切修订身份")
        _uuid(setting["appearance_id"])
        _uuid(setting["setting_revision_id"])
        _digest(setting["content_sha256"])
    if primary is not None:
        if not isinstance(primary, dict) or set(primary) != {"version_id", "series_id", "asset_id", "sha256"}:
            _invalid("冻结主图缺少确切素材版本及字节身份")
        for key in ("version_id", "series_id", "asset_id"):
            _uuid(primary[key])
        _digest(primary["sha256"])
    difference = value.get("difference")
    if value["role"] == "variant":
        if not isinstance(difference, dict) or "value_sha256" not in difference:
            _invalid("冻结差异缺少确切来源和文字摘要")
        _digest(difference["value_sha256"])
    source = {k: v for k, v in difference.items() if k != "value_sha256"} if difference is not None else None
    declared = normalize_media_appearance_request({"role": value["role"],
        "setting_revision_id": setting["setting_revision_id"] if setting else None,
        "primary_version_id": primary["version_id"] if primary else None, "primary_input": value["primary_input"],
        **({"difference_input": source} if source is not None else {})})
    if value["role"] == "primary":
        return
    if not isinstance(request, dict) or request.get("appearance_target") != declared:
        _invalid("冻结变体主图及差异来源与原请求声明不一致")
    use = value["primary_input"]
    logical = inputs.get("logical_values") if isinstance(inputs, dict) else None
    rows = logical.get(use["input_key"]) if isinstance(logical, dict) else None
    matches = [row for row in rows if isinstance(row, dict) and row.get("item_key") == use["item_key"]] if isinstance(rows, list) else []
    if len(matches) != 1:
        _invalid("变体的确切主图未出现在声明的实际图片输入中；不能回退独立文生图")
    row = matches[0]
    media_facts = row.get("media_facts")
    if (row.get("kind") != "image" or row.get("input_key") != use["input_key"]
            or row.get("asset_id") != primary["asset_id"] or not isinstance(media_facts, dict)
            or media_facts.get("sha256") != primary["sha256"]):
        _invalid("变体主图声明与原解析输入的Asset或实际字节不一致")
    # A logical/context use is not proof that the model receives the image.
    # Reuse the original frozen-use/slot verifier, also used by graph staging;
    # do not infer a port from asset identity, names or current configuration.
    uses = read_execution_workflow_uses(definition, json_contract(inputs, max_bytes=execution_resolved_inputs_max_bytes()))
    bound = uses.items.get(use["item_key"])
    target_key = uses.primary_targets.get(use["item_key"])
    target = uses.targets.get(target_key)
    if (bound != row or not target or target["kind"] != "image"
            or not any(slot["item_key"] == use["item_key"] for slot in uses.slots.get(target_key, []))):
        _invalid("变体的确切主图未映射到实际图片端口；上下文素材不能充当生成依据")
    if not isinstance(resolved_inputs, dict) or resolved_inputs.get("inputs") != inputs:
        _invalid("差异校验须使用同一原准备的完整解析事实")
    if difference != media_appearance_difference(source, definition=definition, resolved_inputs=resolved_inputs, request=request):
        _invalid("冻结差异摘要与原输入不一致，不能重选或补写正文")


def _digest(value):
    if not isinstance(value, str) or len(value) != 64 or any(char not in "0123456789abcdef" for char in value):
        _invalid("目标引用须包含原始SHA256，不接受空摘要或猜测来源")
