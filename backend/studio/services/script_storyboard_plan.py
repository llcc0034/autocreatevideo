"""Resolve authored relations from original script and explicit decision evidence.

Internal preflight for the deterministic writer, not another production API.
It performs no materialization, media selection, approval, or model invocation.
Only the small basis belongs in a frozen stage contract; the original script
remains the authority for the on-demand authored body.
"""
from copy import deepcopy
from dataclasses import dataclass
from hashlib import sha256

from django.core.exceptions import ValidationError
from django.db import transaction

from studio.models import ApprovalDecision, ProductionRun, ScriptBibleEntityBinding, SeriesProject
from studio.services.bible_setting_evidence import source_reference
from studio.services.json_contracts import canonical_json_bytes, JsonExpansionBudget
from studio.services.production_run_plans import confirmed_script_reference, get_run_execution_plan
from studio.services.production_reviewed_content import read_reviewed_bible
from studio.services.script_bible_materialization import _appearance_declarations, _reference_basis, read_script_bible_settings
from studio.services.script_semantic_relations import script_relation_errors, VALIDATION_SCOPE
from studio.services.script_semantics import ScriptSemanticValidationError
from studio.services.script_storyboard_fields import storyboard_source_fields


def _digest(value):
    return sha256(canonical_json_bytes(value)).hexdigest()


def _invalid(message):
    raise ValidationError(message, code="storyboard_declaration_binding_invalid")


@dataclass(frozen=True)
class StoryboardSourcePlan:
    basis: dict
    episodes: list
    provenance: list


def compare_storyboard_source_plans(previous, current, *, max_bytes):
    """On-demand diff of two verified previews, not a saved second source.

    Original coordinates, authored relation order and exact IDs are compared;
    names, array offsets and the old mixed participant pool never match shots.
    """
    budget = JsonExpansionBudget(max_bytes, path="storyboard.diff")
    changes = []

    def append(change):
        budget.consume(change)
        changes.append(deepcopy(change))

    for key in dict.fromkeys([*previous.basis, *current.basis]):
        if previous.basis.get(key) != current.basis.get(key):
            append({"scope": "basis", "field": key,
                "before": previous.basis.get(key), "after": current.basis.get(key)})
    before = {(ep["number"], shot["number"]): shot["relations"] for ep in previous.episodes for shot in ep["shots"]}
    after = {(ep["number"], shot["number"]): shot["relations"] for ep in current.episodes for shot in ep["shots"]}
    if list(before) != list(after):
        append({"scope": "relations", "field": "shot_order", "before": list(before), "after": list(after)})
    for coordinate in dict.fromkeys([*before, *after]):
        old, new = before.get(coordinate), after.get(coordinate)
        identity = {"scope": "relations", "episode": coordinate[0], "shot": coordinate[1]}
        if old is None or new is None:
            append({**identity, "field": "shot", "before": old, "after": new})
            continue
        for key in ("scene", "visible_characters", "props", "speaker", "uses"):
            if old[key] != new[key]:
                append({**identity, "field": key, "before": old[key], "after": new[key]})
    return changes


def _resolve_relations(document, bindings, reviewed, *, policy_selected=False):
    """Resolve IDs only; display names never participate in identity matching."""
    from studio.services.execution_instances import execution_configuration_max_bytes

    projection = document["structure"]["document"]["projection"]
    declarations = projection["value"]
    # Saved content has already passed shape validation. Reuse the same finite
    # semantic checker before dict indexing can hide duplicate/unknown keys.
    budget = JsonExpansionBudget(execution_configuration_max_bytes(), path="storyboard.relations")
    issues = script_relation_errors(declarations, provenance=projection["provenance"], consume=budget.consume)
    if issues:
        errors = {}
        for issue in issues:
            errors.setdefault(issue["path"], []).append(issue["message"])
        raise ScriptSemanticValidationError({"valid": False, "validation_scope": VALIDATION_SCOPE,
            "errors": errors, "relation_errors": issues})
    entities = {item["key"]: item for item in declarations["entities"]}
    if set(bindings) != set(entities):
        _invalid("剧本声明的Bible绑定不完整或包含其他声明；请先完成本轮设定审核")
    sections = {"character": "characters", "scene": "scenes", "prop": "props"}
    rows = {(kind, row["id"]): row for kind, section in sections.items()
        for row in reviewed["canonical"][section]}
    targets, appearances = {}, {}
    bound_settings = read_script_bible_settings(bindings)
    for key, declaration in entities.items():
        binding = bindings[key]
        kind = declaration["kind"]
        entity_id = getattr(binding, f"{kind}_id")
        row = rows.get((kind, str(entity_id)))
        if (entity_id is None or binding.declaration_sha256 != _digest(declaration) or row is None
                or not row["is_locked"]):
            _invalid("声明、实体身份或本轮已审核设定不一致；不能按同名对象替代")
        targets[key] = {"binding_id": str(binding.pk), "entity_key": key,
            "entity_kind": kind, "entity_id": str(entity_id),
            **({"speaker_id": row["stable_speaker_id"]} if kind == "character" else {})}
        expected = {key or None for key, _ in _appearance_declarations(declaration)}
        uses = bound_settings.get(binding.pk, {})
        if set(uses) != {key or "" for key in expected}:
            _invalid("本剧本的确切主图/变体设定引用不完整")
        authored = dict(_appearance_declarations(declaration))
        for variant_key, (use, _) in uses.items():
            if use.declaration_sha256 != _digest(authored[variant_key]):
                _invalid("设定引用与本剧本原始声明不一致")
        by_appearance = {str(setting.variant_id): (variant_key or None, source_reference(setting.variant))
            for variant_key, (_, setting) in uses.items()}
        found = set()
        for setting in row.get("appearance_settings", []):
            use = by_appearance.get(setting["appearance_id"])
            if use is None:  # Other revisions may declare other looks of this same entity.
                continue
            variant, origin = use
            if setting["source"] != origin:
                _invalid("已审核设定的原始来源与本剧本引用的实体/变体不符")
            if variant not in expected or variant in found:
                _invalid("同一实体的声明设定缺失、重复或包含未知变体")
            if setting["appearance_role"] != ("primary" if variant is None else "variant"):
                _invalid("主图/变体角色与声明不一致")
            review = setting["review"]
            if (review is not None and (review["decision"] != "approve" or not review["decided_by_id"])) or (review is None and not policy_selected):
                _invalid("声明使用的确切设定修订尚未由人工核对或本轮策略采用")
            found.add(variant)
            # No anchors, image-generation prompts, Asset IDs, or model slots.
            appearances[key, variant] = {"appearance_id": setting["appearance_id"],
                "setting_revision_id": setting["setting_revision_id"], "revision": setting["revision"],
                "setting_sha256": setting["content_sha256"], "review_id": review["id"] if review else None,
                "reference_name": setting["reference_name"], "primary_setting": deepcopy(setting["primary_setting"])}
        if found != expected:
            _invalid("声明的主图/变体尚未完整入库和审核；不自动选取其他设定")

    relations = []
    seen = set()
    shots = [(episode["episode"], shot) for episode in declarations["episodes"] for shot in episode["shots"]]
    coordinates = {(number, shot["shot"]) for number, shot in shots}

    def entity(key, kind=None):
        if key not in targets or (kind and targets[key]["entity_kind"] != kind):
            _invalid("镜头引用了未知或类型不符的实体key；不能扫描正文补齐")
        return deepcopy(targets[key])

    for episode_number, shot in shots:
        coordinate = episode_number, shot["shot"]
        if coordinate not in coordinates or coordinate in seen:
            _invalid("声明镜头坐标重复或不属于原正文")
        seen.add(coordinate)
        visible = [entity(key, "character") for key in shot.get("character_keys", [])]
        props = [entity(key, "prop") for key in shot.get("prop_keys", [])]
        if len({row["entity_id"] for row in visible}) != len(visible) or len({row["entity_id"] for row in props}) != len(props):
            _invalid("镜头出场关系不能重复")
        dialogue = shot.get("dialogue", {})
        speaker_key = dialogue.get("speaker_entity_key")
        speaker = entity(speaker_key, "character") if speaker_key is not None else None
        if bool(dialogue.get("text")) != (speaker is not None):
            _invalid("对白正文与明确发言人关系不一致；不能丢弃或猜测发言人")
        uses, used_keys = [], set()
        for use in shot.get("references", []):
            target = entity(use["entity_key"])
            appearance = appearances.get((use["entity_key"], use["variant_key"]))
            if appearance is None or use["use_key"] in used_keys:
                _invalid("素材使用标识重复或变体不属于该实体；不会回退主图")
            used_keys.add(use["use_key"])
            uses.append({"use_key": use["use_key"], **target, "variant_key": use["variant_key"],
                **deepcopy(appearance), "media_kind": use["media_kind"], "purpose": use.get("purpose", "")})
        relations.append({"episode": coordinate[0], "shot": coordinate[1],
            "scene": entity(shot["scene_key"], "scene") if shot.get("scene_key") is not None else None,
            "visible_characters": visible, "props": props, "speaker": speaker,
            "participant_character_ids": sorted({row["entity_id"] for row in visible}
                | ({speaker["entity_id"]} if speaker else set())), "uses": uses})
    if seen != coordinates:
        _invalid("实际镜头缺少明确关系声明")
    return relations


@transaction.atomic
def read_storyboard_source_plan(*, run, stage):
    """Read under original Run -> project locks; callers freeze ``basis`` only.

    Eligibility is restricted to this storyboard stage's earlier Bible review.
    Source is selected by the original manual/recipe adoption, never the latest
    project manuscript. The later writer must re-read and compare this basis.
    """
    from studio.services.execution_instances import execution_configuration_max_bytes

    run = ProductionRun.objects.select_for_update(of=("self",)).select_related("script_revision__source").get(pk=run.pk)
    run.project = SeriesProject.objects.select_for_update().get(pk=run.project_id)
    plan = get_run_execution_plan(run)
    stage = run.stages.filter(pk=stage.pk, stage_key="storyboard").first()
    if stage is None:
        _invalid("分镜阶段不属于当前运行")
    if run.plan_source == "manual":
        from studio.services.production_manual_scripts import manual_script_states, read_manual_script_structure
        if stage.mode != "manual":
            _invalid("人工制作的分镜模式不一致")
        source = manual_script_states(run)
        reference = source["current_script"]
        script_decision_id = source["current_script_decision_id"]
        document = read_manual_script_structure(run)
    else:
        from studio.services.script_revision_structures import read_recipe_script_structure
        document = read_recipe_script_structure(stage=stage, max_bytes=execution_configuration_max_bytes())
        reference = document["source"]["script"]
        script_decision_id = None  # The original recipe source reader verifies the prior stage.
    if reference is None or run.script_revision_id is None or reference["id"] != str(run.script_revision_id):
        _invalid("本轮尚未明确采用该声明剧本")
    actual = confirmed_script_reference(run.project_id, {"kind": reference["kind"],
        "id": reference["id"], "expected_sha256": reference["sha256"]})
    if actual != reference:
        _invalid("原剧本确认身份已变化")
    eligible = list(run.stages.filter(ordinal__lt=stage.ordinal).values_list("pk", flat=True))
    reviewed = read_reviewed_bible(run=run, eligible_stage_ids=eligible)
    decision = ApprovalDecision.objects.filter(pk=reviewed["review"]["decision_id"],
        stage_run__production_run=run, stage_run_id=reviewed["review"]["stage_run_id"],
        decision="approve").first()
    if decision is None or not ((decision.actor == "human" and decision.decided_by_id)
            or (run.plan_source == "recipe" and decision.actor == "recipe_policy" and decision.decided_by_id is None)):
        _invalid("分镜需要本轮真实人工或已冻结策略决定；两类来源不能混用")
    bindings = {row.entity_key: row for row in ScriptBibleEntityBinding.objects.filter(
        script_revision_id=run.script_revision_id).only("id", "script_revision_id", "entity_key",
            "declaration_sha256", "character_id", "scene_id", "prop_id").order_by("entity_key")}
    relations = _resolve_relations(document, bindings, reviewed, policy_selected=decision.actor == "recipe_policy")
    by_coordinate = {(row["episode"], row["shot"]): row for row in relations}
    from studio.services.script_revision_structures import script_structure_max_bytes
    fields = storyboard_source_fields(document, max_bytes=script_structure_max_bytes())
    episodes = [{"number": episode["episode"], "title": episode.get("title", ""), "shots": [
        {"number": shot["shot"], "body": deepcopy(shot), "relations": by_coordinate[episode["episode"], shot["shot"]],
            "source_fields": fields[episode["episode"], shot["shot"]]}
        for shot in episode["shots"]]}
        for episode in document["structure"]["document"]["projection"]["value"]["episodes"]]
    return StoryboardSourcePlan(basis={"schema_version": 2, "run_plan_sha256": plan.sha256,
        "script": reference, "script_decision_id": script_decision_id,
        "script_stages": [{"stage_id": str(row.pk), "stage_key": row.stage_key,
            "output_snapshot_sha256": row.output_snapshot_sha256}
            for row in run.stages.filter(ordinal__lt=stage.ordinal, status="succeeded",
                stage_key__in=("script_input", "script_structure", "script_generate", "script_review"))
                .only("id", "stage_key", "output_snapshot_sha256").order_by("ordinal")],
        "source": _reference_basis(document), "bible_review": reviewed["review"],
        "resolved_relations_sha256": _digest(relations)}, episodes=episodes,
        provenance=deepcopy(document["structure"]["document"]["projection"]["provenance"]))
