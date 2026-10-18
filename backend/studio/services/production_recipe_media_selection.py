"""Bind explicit recipe Assets to the original instance input resolver.

Recipes store only the selection. Media constraints and source permissions stay
in the exact instance; files, ownership and upload facts are read at preparation.
"""
from copy import deepcopy

from django.core.exceptions import ValidationError

from studio.services.execution_media_source_policy import execution_media_source_policy
from studio.services.input_source_policies import select_media_source


def bind_recipe_media_selection(source, spec):
    path = f"input_sources.{spec['key']}"
    if not isinstance(source, dict) or set(source) != {"kind", "selection"} or source["kind"] != "media_selection":
        raise ValidationError({path: "素材选择须明确提供kind及selection，不重复配置实例的数量或用途"})
    policy = execution_media_source_policy(spec)
    if policy is None:
        raise ValidationError({path: "请先在精确实例的媒体输入中配置允许来源"})
    semantic = spec["semantic_contract"]
    if "request" not in semantic["allowed_scope_kinds"] or semantic["paired_with_input_key"] is not None:
        raise ValidationError({path: "固定素材用于本次输入；逐镜主体和配对素材请使用对应参考映射"})
    selection = source["selection"]
    if not isinstance(selection, dict) or selection.get("source") not in {"asset_library", "asset_upload"}:
        raise ValidationError({path: "配方固定素材须选择素材库或已上传Asset；不能冻结另一轮运行的产物决定"})
    base = {"source": policy["default_source"], "items": []}
    overridden = selection["source"] != policy["default_source"]
    try:
        selected, _ = (select_media_source(policy, base, override=selection) if overridden
                       else select_media_source(policy, selection))
    except ValidationError as exc:
        raise ValidationError({path: exc.messages}) from exc
    selected["items"] = [{**deepcopy(item), "kind": "asset", "scope_ref": {"kind": "request"},
                          "subject_ref": None, "paired_to": None, "order": index}
                         for index, item in enumerate(selected["items"])]
    return {"selection": base, "override": selected} if overridden else {"selection": selected}
