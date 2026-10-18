"""Single visual-rule protocol with explicit JSON positions; no scripts or approvals."""
from copy import deepcopy
import re

from django.core.exceptions import ValidationError

VISUAL_RULES_SCHEMA_VERSION = 2
VISUAL_RULES_REVIEW_SCHEMA_VERSION = 3

COLOR_VALUES = (
    "black", "charcoal", "gray", "slate", "ivory", "white", "brown", "sepia",
    "navy", "blue", "cyan", "teal", "green", "amber", "gold", "copper", "orange",
    "red", "crimson", "magenta", "violet",
)
COLOR_DESIGN_CHOICES = {
    "accent_color": (*COLOR_VALUES, "none"),
    "palette_relation": (
        "monochromatic", "analogous", "complementary", "neutral", "cinematic",
    ),
    "lighting": (
        "soft_diffuse", "hard_directional", "low_key", "high_key", "practical",
        "volumetric", "rim_accent", "balanced_ambient",
    ),
    "contrast": ("low", "restrained", "medium", "high"),
    "saturation": ("desaturated", "muted", "natural", "selective", "saturated"),
    "exposure": ("dark", "balanced", "bright"),
    "progression": (
        "stable", "cool_to_warm", "warm_to_cool", "dark_to_bright",
        "bright_to_dark", "muted_to_saturated", "saturated_to_muted",
    ),
}
COLOR_DESIGN_FIELDS = frozenset({
    "base_colors", *COLOR_DESIGN_CHOICES, "preserve_natural_skin_exposure",
})

VISUAL_RULE_OUTPUT_FIELDS = (
    {"field": "continuity_summary", "label": "已审核连续性说明", "response_field": "continuity_summary",
     "transformation": "检查文本类型、必填和安全字符后原文保留；语言由策略指导，不检测正文语种，不自动截断。"},
    {"field": "color_script", "label": "已审核色彩脚本", "response_field": "color_design",
     "transformation": "按平台色彩枚举渲染为英文色彩脚本；不是模型直接返回的第二份正文。"},
    {"field": "negative_prompt", "label": "已审核负面约束", "response_field": "negative_constraints",
     "transformation": "模型负面约束与本版本建议短语去重合并；审核时可明确修改或移除。"},
)
OPTIONAL_VISUAL_RULE_OUTPUT_FIELDS = (
    {"field": "camera_guidance", "label": "已审核镜头指导", "response_field": "camera_guidance",
     "transformation": "检查完整镜头指导的类型和安全字符后保留，语言由策略指导；只保存到本轮审核结果，不写回项目三项规则。"},
)
VISUAL_RULE_SOURCE_FIELDS = {
    "reviewed_visual_rules." + row["field"]: (row["field"], row["label"])
    for row in (*VISUAL_RULE_OUTPUT_FIELDS, *OPTIONAL_VISUAL_RULE_OUTPUT_FIELDS)
}
VISUAL_RULE_REVIEW_FIELDS = frozenset(row["field"] for row in VISUAL_RULE_OUTPUT_FIELDS)


def visual_rule_field_source_options():
    """Closed, implemented transformations; not arbitrary paths or code."""
    types = {"continuity_summary": "text", "color_script": "object", "negative_prompt": "array"}
    result = [{"field": row["field"], "label": row["label"], "choices": [{
        "response_field": row["response_field"], "value_type": types[row["field"]],
        "transformation": row["transformation"], "example": visual_rule_response_example()[row["response_field"]],
    }]} for row in VISUAL_RULE_OUTPUT_FIELDS]
    result[1]["choices"].append({"response_field": "color_script", "value_type": "text",
        "transformation": "检查模型返回的完整色彩说明的类型和安全字符后原文保留，语言由策略指导；不推断色彩枚举或补造结构。",
        "example": "Muted teal and slate with restrained amber accents and soft diffuse lighting."})
    result[0]["choices"].append({"response_field": "continuity_points", "value_type": "array",
        "transformation": "至少一条非空文本要点；逐条检查类型和安全字符并去除首尾空白，按原顺序用一个空格连接为连续性说明，不限制语言、不删项、不截断。",
        "example": ["Keep character wardrobe consistent.", "Preserve lighting direction across shots."]})
    result[2]["choices"].append({"response_field": "negative_prompt", "value_type": "text",
        "transformation": "仅按英文逗号拆分短语；空字符串表示无负面约束。禁止空项、重复项、分号或换行；去除短语首尾空格和句点，与版本建议去重合并。数量与单条长度建议按拆分后的短语核对。",
        "example": "identity drift, inconsistent lighting"})
    for row in OPTIONAL_VISUAL_RULE_OUTPUT_FIELDS:
        result.append({"field": row["field"], "label": row["label"], "optional": True, "choices": [{
            "response_field": row["response_field"], "value_type": "text", "transformation": row["transformation"],
            "example": "Use restrained eye-level framing and consistent screen direction."}]})
    return result


def normalize_visual_rule_field_sources(value=None):
    if value is None:
        return {row["field"]: row["response_field"] for row in VISUAL_RULE_OUTPUT_FIELDS}
    options = {row["field"]: {choice["response_field"] for choice in row["choices"]}
        for row in visual_rule_field_source_options()}
    if (not isinstance(value, dict) or not VISUAL_RULE_REVIEW_FIELDS.issubset(value) or set(value) - set(options)
            or any(not isinstance(source, str) or source not in options[field] for field, source in value.items())):
        raise ValidationError("field_sources 必须包含三项基础字段，仅可增加目录支持的可选业务字段")
    return deepcopy(value)


def visual_rule_field_sources(parser):
    fields = parser.get("fields") if isinstance(parser, dict) else None
    if (not isinstance(fields, list) or not fields
            or any(not isinstance(row, dict) or not isinstance(row.get("field"), str) for row in fields)):
        raise ValidationError("输出字段映射缺失，不会回退到当前目录")
    if len({row["field"] for row in fields}) != len(fields):
        raise ValidationError("输出字段不得重复")
    return normalize_visual_rule_field_sources({row["field"]: row.get("response_field") for row in fields})


def visual_rule_output_mappings(field_sources=None):
    sources = normalize_visual_rule_field_sources(field_sources)
    options = {row["field"]: row["choices"] for row in visual_rule_field_source_options()} if field_sources is not None else None
    rows = deepcopy([row for row in (*VISUAL_RULE_OUTPUT_FIELDS, *OPTIONAL_VISUAL_RULE_OUTPUT_FIELDS) if row["field"] in sources])
    for row in rows:
        if options:
            choice = next(item for item in options[row["field"]] if item["response_field"] == sources[row["field"]])
            row.update(response_field=choice["response_field"], transformation=choice["transformation"])
        row["source_selector"] = "reviewed_visual_rules." + row["field"]
    return list(rows)


def validate_visual_rule_source_policy(field_sources, allowed_values, quality):
    sources = normalize_visual_rule_field_sources(field_sources)
    allowed = normalize_visual_rule_allowed_values(allowed_values)
    if sources["color_script"] == "color_script" and (allowed or
            {"base_colors_min_items", "base_colors_max_items"}.intersection(quality or {})):
        raise ValidationError("英文色彩说明没有结构化色彩字段；请明确清除色彩允许取值和基础色数量建议，或选择 color_design。不会自动删除配置。")
    return sources


def visual_rule_response_key_policy():
    return {"pattern": "^[a-z][a-z0-9_]{0,63}(\\.([a-z][a-z0-9_]{0,63}|0|[1-9][0-9]{0,63})){0,3}$", "max_length": 259,
        "reserved": ["schema_version", "constructor", "prototype"],
        "description": "配置本版本所选输出在响应 JSON 中的位置，点分隔，最多4层；对象字段每层1–64字符、小写字母开头，纯整数段表示从0开始的数组下标。数组位置必须从0连续完整声明，不可留空、混用对象字段或用前导零；不按内容搜索或猜测顺序。各层不得使用保留字，位置不得重复或互为父子。不支持通配符、脚本、字段别名或外部读取。空对象使用所选格式的顶层字段名。"}


def visual_rule_response_keys(field_sources=None, response_keys=None):
    sources = normalize_visual_rule_field_sources(field_sources)
    if response_keys is None or response_keys == {}:
        return sources
    policy = visual_rule_response_key_policy()
    if (not isinstance(response_keys, dict) or set(response_keys) != set(sources)
            or any(not isinstance(key, str) or not re.fullmatch(policy["pattern"], key)
                or any(part in policy["reserved"] for part in key.split(".")) for key in response_keys.values())
            or len(set(response_keys.values())) != len(sources)
            or any(left.startswith(right + ".") for left in response_keys.values() for right in response_keys.values())):
        raise ValidationError("response_keys 必须完整配置本版本所选字段的合法响应位置（最多4层对象／数组）；不得重复、互为父子或使用保留字、非规范下标及脚本")
    pack_visual_rule_response({field: None for field in sources}, response_keys)
    return deepcopy(response_keys)


def visual_rule_response_value(payload, path):
    """Only walk explicit JSON object members/indices; never evaluate a selector."""
    value = payload
    for part in path.split("."):
        if re.fullmatch(r"0|[1-9][0-9]{0,63}", part):
            index = int(part)
            if not isinstance(value, list) or index >= len(value):
                raise ValidationError(f"响应字段 {path} 缺失或父级不是数组")
            value = value[index]
        else:
            if not isinstance(value, dict) or part not in value:
                raise ValidationError(f"响应字段 {path} 缺失或父级不是对象")
            value = value[part]
    return value


def pack_visual_rule_response(values, keys, *, schema_version=VISUAL_RULES_SCHEMA_VERSION):
    # Build before assigning any values: reject sparse/mixed containers without
    # allocating a list from an untrusted index, and allow atomic branch swaps.
    policy = visual_rule_response_key_policy()
    paths = list(keys.values())
    if (len(set(paths)) != len(paths) or any(not isinstance(path, str)
            or not re.fullmatch(policy["pattern"], path)
            or any(part in policy["reserved"] for part in path.split(".")) for path in paths)):
        raise ValidationError("响应位置重复或含非法字段／下标")

    def build(entries):
        if any(not parts for parts, _ in entries):
            if len(entries) != 1 or entries[0][0]:
                raise ValidationError("响应位置不得互为父子")
            return deepcopy(entries[0][1])
        groups = {}
        for parts, value in entries:
            groups.setdefault(parts[0], []).append((parts[1:], value))
        numeric = [part.isdecimal() for part in groups]
        if any(numeric):
            if not all(numeric) or {int(part) for part in groups} != set(range(len(groups))):
                raise ValidationError("响应数组下标必须从0连续完整声明，不能留空或混用对象字段")
            return [build(groups[str(index)]) for index in range(len(groups))]
        return {part: build(children) for part, children in groups.items()}

    return {"schema_version": schema_version, **build([(path.split("."), values[field]) for field, path in keys.items()])}


def unpack_visual_rule_response(payload, keys):
    """Require the entire declared tree; leaves keep their semantic validators."""
    tree = pack_visual_rule_response({field: None for field in keys}, keys, schema_version=None)

    def check(value, shape, prefix):
        if isinstance(shape, list):
            if not isinstance(value, list) or len(value) != len(shape):
                raise ValidationError(f"响应数组 {prefix} 长度不符或不是数组；不会跳过、补齐或重排")
            children = enumerate(shape)
        else:
            if not isinstance(value, dict) or set(value) != set(shape):
                raise ValidationError(f"响应对象 {prefix or '$'} 字段不完整、含未声明字段或不是对象")
            children = shape.items()
        for key, child in children:
            if isinstance(child, (dict, list)):
                check(value[key], child, f"{prefix}.{key}" if prefix else key)

    check(payload, tree, "")
    return {field: visual_rule_response_value(payload, path) for field, path in keys.items()}


def reconfigure_visual_rule_sources(parser, field_sources, response_keys=None):
    sources = normalize_visual_rule_field_sources(field_sources)
    result = deepcopy(parser)
    configured_keys = parser.get("response_keys", {}) if response_keys is None else response_keys
    keys = visual_rule_response_keys(sources, configured_keys)
    previous = visual_rule_field_sources(parser)
    previous_keys = visual_rule_response_keys(previous, parser.get("response_keys"))
    mappings = {row["field"]: row for row in visual_rule_output_mappings(sources)}
    result["fields"] = [row for row in result["fields"] if row["field"] in sources]
    result["fields"].extend(deepcopy(row) for field, row in mappings.items() if field not in previous)
    for row in result["fields"]:
        if row["response_field"] != sources[row["field"]]:
            row.update(response_field=sources[row["field"]], transformation=mappings[row["field"]]["transformation"])
    # Rebuild together, so even a key swap cannot overwrite another example.
    example = {}
    for row in visual_rule_field_source_options():
        field = row["field"]
        if field not in sources:
            continue
        if sources[field] != previous.get(field):
            example[field] = deepcopy(next(choice["example"] for choice in row["choices"]
                if choice["response_field"] == sources[field]))
        else:
            example[field] = deepcopy(visual_rule_response_value(parser["response_example"], previous_keys[field]))
    result["response_example"] = pack_visual_rule_response(example, keys,
        schema_version=parser["response_example"]["schema_version"])
    if configured_keys:
        result["response_keys"] = deepcopy(configured_keys)
    else:
        result.pop("response_keys", None)
    return result


def visual_rule_response_example():
    # A format example, never a default, generated result or reviewed fact.
    return {"schema_version": VISUAL_RULES_SCHEMA_VERSION,
        "color_design": {"base_colors": ["teal", "slate"], "accent_color": "amber",
            "palette_relation": "cinematic", "lighting": "soft_diffuse", "contrast": "restrained",
            "saturation": "muted", "exposure": "balanced", "progression": "stable",
            "preserve_natural_skin_exposure": True},
        "negative_constraints": ["story-specific visual defect phrase"],
        "continuity_summary": "English project-wide visual continuity rules"}


def visual_rule_output_enums():
    return {"color_design.base_colors": {"allowed": list(COLOR_VALUES), "uniqueness": "required"},
        **{f"color_design.{key}": list(values) for key, values in COLOR_DESIGN_CHOICES.items()},
        "color_design.preserve_natural_skin_exposure": [True, False]}


def visual_rule_negative_grammar(response_field="negative_constraints"):
    grammar = {"type": "array", "uniqueness": "required_case_insensitive",
        "item": "non_empty_printable_ascii_english_defect_phrase",
        "forbidden_item_delimiters": [",", ";", "CR", "LF"]}
    if response_field == "negative_prompt":
        grammar.update(type="string", separator=",", empty_string="no_constraints",
            empty_items="forbidden", item_boundary_trim="spaces_and_periods")
    elif response_field != "negative_constraints":
        raise ValidationError("未实现的负面约束响应来源")
    return grammar


def visual_rule_allowed_value_fields():
    """Only implemented color fields; keys are not executable object paths."""
    labels = {"base_colors": "基础色", "accent_color": "强调色", "palette_relation": "配色关系",
        "lighting": "光线", "contrast": "对比度", "saturation": "饱和度", "exposure": "曝光",
        "progression": "色彩演进", "preserve_natural_skin_exposure": "保持自然肤色曝光"}
    return [{"key": key, "label": labels[key], "choices": list(choices)} for key, choices in {
        "base_colors": COLOR_VALUES, **COLOR_DESIGN_CHOICES, "preserve_natural_skin_exposure": (True, False)}.items()]


def normalize_visual_rule_allowed_values(value):
    if not isinstance(value, dict):
        raise ValidationError("allowed_values 必须为对象")
    fields = {row["key"]: row["choices"] for row in visual_rule_allowed_value_fields()}
    if set(value) - set(fields):
        raise ValidationError("allowed_values 包含未声明的色彩字段；不支持任意路径")
    for key, choices in value.items():
        valid = fields[key]
        if (not isinstance(choices, list) or not choices or len(choices) > len(valid)
                or any(not any(type(item) is type(option) and item == option for option in valid) for item in choices)
                or len({str(item) for item in choices}) != len(choices)):
            raise ValidationError(f"allowed_values.{key} 请选择不重复的合法选项；空数组不是不限制")
    return deepcopy(value)


def validate_visual_rule_allowed_values(design, allowed_values):
    for key, choices in normalize_visual_rule_allowed_values(allowed_values).items():
        actual = design[key] if key == "base_colors" else [design[key]]
        if any(item not in choices for item in actual):
            raise ValidationError(f"color_design.{key} 不符合此输出版本允许的取值；保留原响应供人工处理，不自动替换",
                code="visual_rules_output_value_not_allowed")


def configured_visual_rule_output_enums(allowed_values, response_field="color_design"):
    result = visual_rule_output_enums()
    for key, choices in normalize_visual_rule_allowed_values(allowed_values).items():
        result["color_design." + key] = ({"allowed": choices, "uniqueness": "required"} if key == "base_colors" else choices)
    return {response_field + key[len("color_design"):]: value for key, value in result.items()}


def constrain_visual_rule_example(example, allowed_values, response_field="color_design"):
    """A format example only; never used to fill or repair an actual response."""
    result = deepcopy(example)
    for key, choices in normalize_visual_rule_allowed_values(allowed_values).items():
        visual_rule_response_value(result, response_field)[key] = choices[:2] if key == "base_colors" else choices[0]
    return result
