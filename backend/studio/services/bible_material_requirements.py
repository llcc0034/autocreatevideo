"""Setting requirements frozen inside the original Bible decision receipt.

This is not a queue, media selection, or execution-readiness check. Callers
select explicit entity scopes or all declared requirements within the RunPlan;
the original Bible decision remains authoritative. M4 resolves media later.
"""
from copy import deepcopy
from uuid import UUID

from django.core.exceptions import ValidationError

from studio.services.bible_setting_evidence import (
    primary_setting_content_reference, read_setting_revisions, setting_reference,
)
from studio.services.bible_variants import BibleVariantConflict
from studio.models import ApprovalDecision
from studio.services.json_contracts import DATABASE_JSON_FIELD_MAX_BYTES, json_contract, verify_json_contract
from studio.services.production_execution_units import ENTITY_SCOPE_MODELS, production_entity_queryset
from studio.services.production_reviewed_content import read_reviewed_bible
from studio.services.production_run_plans import get_run_execution_plan


SECTIONS = {"character": "characters", "scene": "scenes", "prop": "props"}


def _invalid(message):
    raise ValidationError(message, code="bible_material_requirements_invalid")


def _reference_asset(setting):
    # An authored identity/reference input is NOT an adopted result, nor the
    # actual primary image required by a child. Preserve its original digest;
    # execution must still verify ownership, bytes and the mapped input port.
    return ({"asset_id": str(setting.basis_asset_id), "sha256": setting.basis_asset_sha256}
        if setting.basis_asset_id else None)


def project_bible_material_requirements(*, rows, project_id):
    """Project exact rows from a verified decision, with one bounded body read.

    No appearances means no target. A child retains its historical primary
    setting even if the same entity now has a newer primary revision. The
    dependency does not promise that a matching image already exists.
    """
    references = []
    for (kind, identifier), row in sorted(rows.items()):
        if (kind not in SECTIONS or row.get("id") != identifier
                or not isinstance(row.get("appearance_settings"), list)):
            _invalid("素材需求缺少明确实体及设定引用")
        looks = row["appearance_settings"]
        if any(not isinstance(ref, dict) or not isinstance(ref.get("setting_revision_id"), str)
                or ref.get("appearance_role") not in {"primary", "variant"} for ref in looks):
            _invalid("素材需求包含无效设定引用")
        if looks and sum(ref.get("appearance_role") == "primary" for ref in looks) != 1:
            _invalid("有外观设定的实体必须包含唯一明确主图")
        if len({ref.get("appearance_id") for ref in looks}) != len(looks):
            _invalid("同一实体的外观设定不能重复")
        references.extend((kind, identifier, ref) for ref in looks)
    try:
        settings = read_setting_revisions([ref["setting_revision_id"] for _, _, ref in references], project_id=project_id)
        targets = []
        for kind, identifier, ref in references:
            setting = settings[ref["setting_revision_id"]]
            if (str(getattr(setting.variant, f"{kind}_id")) != identifier
                    or setting_reference(setting) != ref):
                _invalid("需求设定与本次审核的确切实体、修订或来源不一致")
            primary = setting.primary_setting_revision
            if setting.variant.appearance_role == "variant" and primary is None:
                _invalid("变体需求缺少确切主图设定依赖，不能作为独立文生图")
            targets.append({"requirement_key": f"setting:{setting.pk}",
                "scope": {"kind": kind, "id": identifier}, "appearance_role": setting.variant.appearance_role,
                "setting": primary_setting_content_reference(setting),
                "reference_name": setting.reference_name,
                "primary_dependency": primary_setting_content_reference(primary),
                "declared_reference_asset": _reference_asset(setting)})
    except BibleVariantConflict as exc:
        raise ValidationError("素材需求所引用的设定缺失或损坏，不能改取其他修订",
            code="bible_material_requirements_invalid") from exc
    targets.sort(key=lambda item: (item["scope"]["kind"], item["scope"]["id"],
        item["appearance_role"] != "primary", item["requirement_key"]))
    return targets


def _requirement_rows(*, canonical, materialization):
    """Use declaration appearance identities, but the reviewed content revision.

    A source may reuse a manually edited appearance. Its original binding is
    not permission to revert that edit or include other looks of the entity.
    None is only used by the original project-Bible manual recipe review.
    """
    rows = {(kind, row["id"]): row for kind, section in SECTIONS.items() for row in canonical[section]}
    if materialization is None:
        return rows
    if (not isinstance(materialization, dict) or not isinstance(materialization.get("entities"), list)
            or not isinstance(materialization.get("source"), dict)
            or materialization["source"].get("kind") != "script_reference_bible"
            or materialization["source"].get("schema_version") != 3):
        _invalid("素材需求缺少本次声明物化回执")
    selected = {}
    for entity in materialization["entities"]:
        if (not isinstance(entity, dict) or not isinstance(entity.get("entity_kind"), str)
                or entity["entity_kind"] not in SECTIONS or not isinstance(entity.get("entity_id"), str)
                or not isinstance(entity.get("settings"), list)):
            _invalid("素材需求声明实体无效")
        key = (entity["entity_kind"], entity["entity_id"])
        if key in selected or key not in rows:
            _invalid("素材需求声明实体重复或不在本次审核中")
        uses = entity["settings"]
        if any(not isinstance(use, dict) or not isinstance(use.get("appearance_id"), str) for use in uses):
            _invalid("素材需求声明缺少明确外观身份")
        appearances = {use["appearance_id"] for use in uses}
        if len(appearances) != len(uses):
            _invalid("素材需求声明外观重复")
        refs = [ref for ref in rows[key]["appearance_settings"] if ref["appearance_id"] in appearances]
        if {ref["appearance_id"] for ref in refs} != appearances:
            _invalid("本次声明外观缺少已审核修订，不能猜测其他设定")
        selected[key] = {**rows[key], "appearance_settings": refs}
    return selected


def build_bible_material_requirements(*, project_id, canonical, materialization):
    """Compile the original decision's output; never persist a second fact row."""
    rows = _requirement_rows(canonical=canonical, materialization=materialization)
    return json_contract({"schema_version": 1, "kind": "bible_material_requirements",
        "project_id": str(project_id),
        "source": deepcopy(materialization["source"]) if materialization is not None else {"kind": "project_bible"},
        "targets": project_bible_material_requirements(rows=rows, project_id=project_id)},
        max_bytes=DATABASE_JSON_FIELD_MAX_BYTES)


def _saved_requirements(*, run, reviewed):
    # The shared reader above has verified the original human/policy decision
    # and freshness. Recheck that exact snapshot, not a latest-stage fallback.
    decision = ApprovalDecision.objects.select_related("stage_run").only("id", "actor", "output_snapshot_sha256",
        "stage_run__id", "stage_run__production_run_id", "stage_run__output_snapshot",
        "stage_run__output_snapshot_schema_version", "stage_run__output_snapshot_size_bytes",
        "stage_run__output_snapshot_sha256").filter(pk=reviewed["review"]["decision_id"]).first()
    if decision is None:
        _invalid("素材需求的原审核决定已缺失")
    stage = decision.stage_run
    saved = verify_json_contract(stage.output_snapshot, schema_version=stage.output_snapshot_schema_version,
        size_bytes=stage.output_snapshot_size_bytes, sha256=stage.output_snapshot_sha256,
        max_bytes=DATABASE_JSON_FIELD_MAX_BYTES)
    if (stage.production_run_id != run.pk or str(stage.pk) != reviewed["review"]["stage_run_id"]
            or saved.sha256 != reviewed["review"]["snapshot_sha256"]
            or decision.output_snapshot_sha256 != saved.sha256):
        _invalid("素材需求与原审核决定不一致")
    if run.plan_source == "manual":
        receipt = saved.value.get("materialization")
    else:
        receipt = saved.value.get("policy_evidence" if decision.actor == "recipe_policy" else "review_payload")
    expected_kind = "bible_content_policy" if decision.actor == "recipe_policy" else "bible_content_review"
    if (not isinstance(receipt, dict) or receipt.get("schema_version") != 1 or receipt.get("kind") != expected_kind
            or "materialization" not in receipt or not isinstance(receipt.get("material_requirements"), dict)):
        _invalid("原审核没有有效素材需求输出；不能在读取时补造")
    materialization = receipt["materialization"]
    if run.plan_source == "recipe":
        from studio.services.bible_content_review import recipe_bible_review_source
        frozen, _ = recipe_bible_review_source(run)
    else:
        from studio.services.production_manual_bible import _freeze_source
        frozen = _freeze_source(run)
    if (materialization is None) != (frozen is None) or (materialization is not None
            and (not isinstance(materialization, dict) or materialization.get("source") != frozen)):
        _invalid("素材需求声明与原来源阶段不一致")
    expected = build_bible_material_requirements(project_id=run.project_id,
        canonical=reviewed["canonical"], materialization=materialization)
    if receipt["material_requirements"] != expected.value:
        _invalid("原素材需求输出与已审核设定不一致；不能静默重算替换")
    return receipt["material_requirements"], _requirement_rows(
        canonical=reviewed["canonical"], materialization=materialization)


def read_bible_material_requirements(*, run, eligible_stage_ids, scopes):
    """Original approved content + explicit RunPlan-scoped targets; no writes.

    This internal boundary is shared by future manual/recipe M4 callers. It
    does not infer a declaration list from names or from today's whole Bible.
    """
    if run.project.owner_id != run.owner_id:
        _invalid("素材需求的运行与项目归属不一致")
    if not isinstance(scopes, list):
        _invalid("素材需求必须指定明确的实体范围")
    keys = set()
    for scope in scopes:
        if (not isinstance(scope, dict) or set(scope) != {"kind", "id"}
                or not isinstance(scope["kind"], str) or scope["kind"] not in ENTITY_SCOPE_MODELS
                or not isinstance(scope["id"], str)):
            _invalid("素材需求范围只接受人物、场景或道具及其确切ID")
        try:
            valid_id = str(UUID(scope["id"])) == scope["id"]
        except ValueError:
            valid_id = False
        if not valid_id or (scope["kind"], scope["id"]) in keys:
            _invalid("素材需求范围包含无效或重复ID")
        keys.add((scope["kind"], scope["id"]))
    return _read_requirements(run=run, eligible_stage_ids=eligible_stage_ids, keys=keys)


def read_run_bible_material_requirements(*, run, eligible_stage_ids):
    """All declared requirements inside the original RunPlan, not all entities.

    This explicit mode is for automatic stage planning. An empty explicit
    scope list in the other reader still means no targets, never all targets.
    """
    return _read_requirements(run=run, eligible_stage_ids=eligible_stage_ids, keys=None)


def _read_requirements(*, run, eligible_stage_ids, keys):
    if run.project.owner_id != run.owner_id:
        _invalid("素材需求的运行与项目归属不一致")
    plan = get_run_execution_plan(run)
    reviewed = read_reviewed_bible(run=run, eligible_stage_ids=eligible_stage_ids)
    requirements, rows = _saved_requirements(run=run, reviewed=reviewed)
    declared = set(rows) if keys is None else keys
    included = set()
    for kind in ENTITY_SCOPE_MODELS:
        identifiers = {identifier for scope, identifier in declared if scope == kind}
        actual = {str(pk) for pk in production_entity_queryset(run, plan, kind)
            .filter(pk__in=identifiers).values_list("pk", flat=True)} if identifiers else set()
        if keys is not None and actual != identifiers:
            _invalid("素材需求实体不属于本次冻结运行范围")
        included.update((kind, identifier) for identifier in actual)
    if not declared.issubset(rows):
        _invalid("素材需求实体不在本次已采用声明或项目设定审核范围中")
    return json_contract({**deepcopy(requirements), "run_plan_sha256": plan.sha256,
        "bible_review": deepcopy(reviewed["review"]),
        "targets": [deepcopy(row) for row in requirements["targets"] if (row["scope"]["kind"], row["scope"]["id"]) in included]},
        max_bytes=DATABASE_JSON_FIELD_MAX_BYTES)
