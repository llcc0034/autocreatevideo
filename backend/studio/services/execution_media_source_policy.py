"""Instance-owned source choices, using the shared media policy contract.

Type, quantity and purpose remain owned by ExecutionInputSpec. This adapter
projects them into the shared selector; it does not read media a second time.
"""
from copy import deepcopy

from django.core.exceptions import ValidationError

from studio.services.input_source_policies import select_media_source, validate_media_source_policy


CHOICE_KEYS = frozenset(("schema_version", "allowed_sources", "default_source", "allow_override", "order"))


def execution_media_source_policy(spec):
    choices = spec.get("media_source_policy")
    if choices is None:
        return None
    path = f"inputs.{spec['key']}.media_source_policy"
    if not isinstance(choices, dict) or set(choices) != CHOICE_KEYS:
        raise ValidationError({path: "须明确提供来源方式、默认来源、是否允许改选和顺序；数量及用途沿用输入定义"})
    return validate_media_source_policy({**choices, "media_kind": spec["kind"],
        "min_items": spec["min_items"], "max_items": spec["max_items"],
        "allowed_purposes": spec["semantic_contract"]["allowed_purposes"]}, path=path)


def _selection(value, key):
    if not isinstance(value, dict) or set(value) != {"source", "items"} or not isinstance(value["items"], list):
        raise ValidationError({key: "须提供明确来源方式和有序素材列表"})
    expected_kind = "selected_output" if value["source"] == "selected_output" else "asset"
    identities = ("decision_id", "artifact_id") if expected_kind == "selected_output" else ("asset_id",)
    items = []
    for item in value["items"]:
        required = {"kind", "item_key", "purpose", *identities}
        if (not isinstance(item, dict) or item.get("kind") != expected_kind or not required <= item.keys()
                or item.keys() - required - {"subject_ref", "scope_ref", "paired_to", "order"}):
            raise ValidationError({key: "素材引用须与所选来源一致，不能混入其他来源或临时路径"})
        items.append({field: item[field] for field in ("item_key", "purpose", *identities)})
    return {"source": value["source"], "items": items}


def select_execution_media_source(spec, source):
    """Return original ordered items and choice evidence before any media I/O.

    An explicit override replaces the complete selection, including use context.
    Absence of a policy adds no restriction; normal input/target checks still run.
    """
    policy = execution_media_source_policy(spec)
    if policy is None:
        return source, None
    key = f"inputs.{spec['key']}"
    if not isinstance(source, dict) or set(source) not in ({"selection"}, {"selection", "override"}):
        raise ValidationError({key: "已配置来源策略，请明确提交selection；本次改选须另传完整override"})
    selected = source.get("override", source["selection"])
    compact = _selection(selected, key)
    try:
        if "override" in source:
            # The unused base is not read or resolved. Overrides replace, never merge.
            _, overridden = select_media_source(policy, source["selection"], override=compact)
        else:
            _, overridden = select_media_source(policy, compact)
    except ValidationError as exc:
        raise ValidationError({key: exc.messages}) from exc
    evidence = {"source": selected["source"], "overridden": overridden}
    return {"items": deepcopy(selected["items"])}, evidence
