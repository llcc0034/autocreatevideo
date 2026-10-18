"""On-demand comparison with the saved initial declaration, never a new writer."""
import hashlib

from django.core.exceptions import ValidationError

from studio.services.bible_setting_evidence import source_reference
from studio.services.bible_variants import BibleVariantConflict, verify_setting
from studio.services.json_contracts import canonical_json_bytes


def _under(path, parent):
    return path == parent or path.startswith(parent + ".") or path.startswith(parent + "[")


def declaration_field_origin(projection, path):
    # A whole-object path/literal also owns its descendants. Lookup emits
    # intermediate records at the same target path; its final record wins.
    candidates = {row["path"]: row for row in projection["provenance"] if _under(path, row["path"])}
    if not candidates:
        raise BibleVariantConflict("声明字段缺少保存的映射来源；不能猜测为原文或人工补充")
    ancestor = max(candidates, key=len)
    origin = candidates[ancestor]
    if origin["kind"] == "literal":
        return "configured_literal", None
    if origin["kind"] not in {"path", "lookup"} or not origin.get("source_path"):
        raise BibleVariantConflict("声明字段的映射来源不可识别")
    source_path = origin["source_path"] + path[len(ancestor):]
    defaulted = any(_under(source_path, row["path"]) for row in projection["defaults"])
    return ("configured_default" if defaulted else "source_value"), source_path


def read_setting_source(setting):
    """Compare one immutable revision to its original, verified M2 projection.

    Declaration values are not asserted to be quotations or model-independent
    facts. Explicit mapping constants/defaults remain distinct. Subsequent
    source-use receipts never relabel the appearance's initial authoring source.
    """
    verify_setting(setting)
    variant, binding = setting.variant, setting.variant.source_binding
    primary = variant.appearance_role == "primary"
    definitions = [("name", "设定名称"), ("reference_name", "素材引用名称")]
    if primary:
        definitions.append(("identity_anchor", "共同身份"))
    definitions.append(("appearance_anchor", "主图外观" if primary else "变体差异"))
    response = {"appearance_id": str(variant.pk), "setting_revision_id": str(setting.pk),
        "setting_sha256": setting.content_sha256, "source": source_reference(variant),
        "document_sha256": None, "fields": []}
    source_fields = {}
    if binding:
        from studio.services.execution_instances import execution_configuration_max_bytes
        from studio.services.script_revision_structures import read_script_revision_structure
        try:
            detail = read_script_revision_structure(revision_id=binding.script_revision_id,
                project_id=variant.project_id, max_bytes=execution_configuration_max_bytes())
        except ValidationError as exc:
            raise BibleVariantConflict("初始声明结构缺失或不一致；不能用当前剧本替代") from exc
        projection = detail["document"]["projection"]
        matches = [(index, row) for index, row in enumerate(projection["value"]["entities"])
            if row["key"] == binding.entity_key]
        if len(matches) != 1:
            raise BibleVariantConflict("初始声明实体key缺失或不唯一")
        index, entity = matches[0]
        kind = next(kind for kind in ("character", "scene", "prop") if getattr(variant, f"{kind}_id"))
        if (entity["kind"] != kind or hashlib.sha256(canonical_json_bytes(entity)).hexdigest()
                != binding.declaration_sha256):
            raise BibleVariantConflict("初始实体声明与来源绑定不一致")
        entity_path = f"value.entities[{index}]"
        if primary:
            appearance, appearance_path = entity.get("primary"), entity_path + ".primary"
        else:
            matches = [(index, row) for index, row in enumerate(entity.get("variants", []))
                if row["key"] == variant.source_variant_key]
            if len(matches) != 1:
                raise BibleVariantConflict("初始变体声明key缺失或不唯一")
            offset, appearance = matches[0]
            appearance_path = f"{entity_path}.variants[{offset}]"
        if appearance is None or (setting.source_declaration_sha256 and
                hashlib.sha256(canonical_json_bytes(appearance)).hexdigest() != setting.source_declaration_sha256):
            raise BibleVariantConflict("初始设定声明与所选修订不一致")
        response["document_sha256"] = detail["document_sha256"]
        source_fields = {"name": (entity if primary else appearance, "name", entity_path if primary else appearance_path),
            "reference_name": (appearance, "reference_name", appearance_path),
            "identity_anchor": (entity, "identity_anchor", entity_path),
            "appearance_anchor": (appearance, "anchor", appearance_path)}
    for key, label in definitions:
        value, source_path, origin, changed = None, None, "manual", None
        current = getattr(setting, key)
        if binding:
            owner, field, path = source_fields[key]
            if field in owner:
                value = owner[field]
                origin, source_path = declaration_field_origin(projection, f"{path}.{field}")
            else:
                origin = "missing"
            changed = current != (value if value is not None else "")
        response["fields"].append({"key": key, "label": label, "declaration_value": value,
            "setting_value": current, "origin": origin, "source_path": source_path,
            "changed_from_declaration": changed})
    return response
