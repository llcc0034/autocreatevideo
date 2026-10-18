"""Frozen result-source policy, distinct from model inputs and candidate review."""
from django.core.exceptions import ValidationError


MATERIAL_SOURCE_OPTIONS = (
    {"value": "generate", "label": "模型生成（默认）"},
    {"value": "existing", "label": "已有成图（零调用）"},
    {"value": "upload", "label": "上传成图（零调用）"},
)
MATERIAL_SELECTION_OPTIONS = (
    {"value": "manual", "label": "人工选择"},
    {"value": "unique_version", "label": "自动使用唯一确切版本，否则人工接管"},
)
ROLES = {"primary": "routing", "variant": "variant_routing"}


def validate_material_sources(stage):
    if "material_sources" not in stage:
        if "material_selection" in stage:
            raise ValidationError({"material_selection": "选材策略需要明确的成图来源"})
        return
    value = stage["material_sources"]
    if (stage.get("key") != "bible_image_generation" or stage.get("mode") != "existing_pipeline"
            or not isinstance(value, dict) or set(value) != set(ROLES)):
        raise ValidationError({"material_sources": "仅素材生成阶段可明确配置主图和变体的成图来源"})
    for role, routing in ROLES.items():
        source = value[role]
        if not isinstance(source, str) or source not in {row["value"] for row in MATERIAL_SOURCE_OPTIONS}:
            raise ValidationError({f"material_sources.{role}": "请选择已接通的成图来源；不推断上游或自动选材规则"})
        if source != "generate" and routing in stage:
            raise ValidationError({routing: "已有/上传成图不配置闲置模型路线，请明确移除对应路线"})
    if "material_selection" in stage:
        policies = stage["material_selection"]
        if not isinstance(policies, dict) or not policies or set(policies) - set(ROLES):
            raise ValidationError({"material_selection": "仅为已有/上传角色明确配置选材策略"})
        for role, policy in policies.items():
            if (value[role] == "generate" or not isinstance(policy, str)
                    or policy not in {row["value"] for row in MATERIAL_SELECTION_OPTIONS}):
                raise ValidationError({"material_selection": "选材策略不能代替模型候选选择，也不接受未注册规则"})


def recipe_material_source(stage, role):
    validate_material_sources(stage)
    if role not in ROLES:
        raise ValidationError("素材角色必须是主图或变体")
    # This is the documented generation duty default, never a fallback after
    # an explicit source fails. No stored configuration is rewritten on read.
    return stage["material_sources"][role] if "material_sources" in stage else "generate"


def recipe_material_selection_policy(stage, role):
    recipe_material_source(stage, role)
    return stage.get("material_selection", {}).get(role, "manual")


def material_source_version_candidates(stage, target, source):
    """At most two exact identities suffice to distinguish unique/ambiguous.

    Never sort by recency, inspect arbitrary Assets, or infer a setting by name.
    File and original version integrity are checked by the fulfillment writer.
    """
    from studio.models import AssetReferenceVersion
    rows = AssetReferenceVersion.objects.filter(setting_revision_id=target["setting"]["setting_revision_id"],
        series__owner_id=stage.production_run.owner_id, series__bible_variant_id=target["setting"]["appearance_id"],
        series__role=target["appearance_role"], source__in=material_source_version_origins(source))
    if target["primary_dependency"] is not None:
        rows = rows.filter(series__primary_series__bible_variant_id=target["primary_dependency"]["appearance_id"],
            series__primary_series__role="primary", series__primary_series__owner_id=stage.production_run.owner_id)
    return rows.order_by().values_list("id", flat=True)[:2]


def material_source_version_origins(source):
    # "existing" is a reuse path, not permission to rewrite origin. An upload
    # policy still requires a genuinely uploaded version, not a generated one.
    if source == "existing":
        return ("existing", "upload", "generated")
    if source == "upload":
        return ("upload",)
    raise ValidationError("只有已有或上传成图来源可查询确切版本")


def recipe_material_source_snapshot(stage, declaration, targets):
    """Freeze eligible identities once in the existing stage input snapshot.

    New library versions cannot silently repair an ambiguous/missing source on
    later polls. Manual takeover uses the original explicit selection action.
    """
    from studio.services.production_runs import _verify_stage_snapshot
    expected = {f'appearance:{target["setting"]["appearance_id"]}': target for target in targets
        if recipe_material_selection_policy(declaration, target["appearance_role"]) == "unique_version"}
    if not expected:
        return {}
    if stage.input_snapshot_sha256:
        saved = _verify_stage_snapshot(stage, "input_snapshot").get("material_source_versions")
        if not isinstance(saved, dict) or set(saved) != set(expected):
            raise ValidationError("自动选材缺少原冻结来源，不能重新搜索素材库")
        from uuid import UUID
        for value in saved.values():
            if not isinstance(value, dict) or set(value) != {"status", "version_id"}:
                raise ValidationError("自动选材冻结来源格式无效")
            if value["status"] == "unique":
                try:
                    valid = isinstance(value["version_id"], str) and str(UUID(value["version_id"])) == value["version_id"]
                except ValueError:
                    valid = False
                if not valid:
                    raise ValidationError("自动选材需要确切版本身份")
            elif value["status"] not in ("missing", "ambiguous") or value["version_id"] is not None:
                raise ValidationError("自动选材不能从未知来源状态推断结果")
        return saved
    result = {}
    for key, target in expected.items():
        ids = list(material_source_version_candidates(stage, target,
            recipe_material_source(declaration, target["appearance_role"])))
        result[key] = {"status": "unique" if len(ids) == 1 else "ambiguous" if ids else "missing",
            "version_id": str(ids[0]) if len(ids) == 1 else None}
    return result
