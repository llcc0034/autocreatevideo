"""One deterministic ScriptRevision -> Episode/Shot writer for production.

Callers select the source and freeze parameters. This service never chooses a
model, rewrites the project manuscript, approves media, or deletes old content.
"""
from copy import deepcopy
from dataclasses import dataclass

from django.core.exceptions import ValidationError
from django.db import transaction
from django.utils import timezone

from studio.models import (Episode, ProductionRun, Shot,
    ShotCharacterRelation, ShotReferenceUse, ShotScriptSource)
from studio.services.configuration import project_creation_policy, video_shot_duration_policy
from studio.services.production_run_plans import get_run_execution_plan
from studio.services.json_contracts import JsonContract, json_contract, verify_json_contract
from studio.services.script_storyboard_plan import StoryboardSourcePlan, read_storyboard_source_plan
from studio.services.security_boundaries import boundary_int


@dataclass(frozen=True)
class StoryboardMaterializationPlan:
    """Read-only input for the encompassing writer; never a persisted receipt.

    Only ``contract`` belongs in the caller's original Stage/command snapshot.
    Bodies, resolved relations and effective timings are transient projections
    of that exact source/configuration, not another copy of the script.
    """
    source: StoryboardSourcePlan
    contract: JsonContract
    timings: dict


def declared_storyboard_parameters(*, values):
    """Only creation defaults still used by explicitly declared shots.

    No episode-count guess, text splitting, name aliases or generation mode.
    Supplying frozen values prevents the policy readers consulting live defaults.
    """
    if not isinstance(values, dict):
        raise ValidationError("本轮冻结配置缺少 values", code="config_snapshot_missing")
    booleans = {
        "multi_shot": "production.default_multi_shot",
        "include_audio": "production.default_include_audio",
        "default_first_frame_continuity": "production.default_first_frame_continuity",
        "default_last_frame_continuity": "production.default_last_frame_continuity",
    }
    if any(type(values.get(key)) is not bool for key in booleans.values()):
        raise ValidationError("冻结的连续性、音频和多镜头选项必须是明确布尔值", code="config_snapshot_missing")
    duration = video_shot_duration_policy(values=values)
    creation = project_creation_policy(values=values)
    return {"default_shot_duration_ms": duration["default_duration_ms"],
        "video_shot_min_duration_ms": duration["minimum_duration_ms"],
        "video_shot_max_duration_ms": duration["maximum_duration_ms"],
        "default_shot_media_type": creation["default_shot_media_type"],
        **{name: values[key] for name, key in booleans.items()}}


def declared_storyboard_timings(*, source, parameters):
    """Resolve validated M2 timing facts without clamping, splitting or renumbering.

    An explicit interval determines duration when duration itself is absent.
    Only when neither was authored is the frozen default used. Dialogue is
    checked against that effective duration, including when M2 had no duration.
    No missing dialogue/episode timing is invented.
    """
    timings, errors = {}, {}
    if not source.episodes or any(not episode["shots"] for episode in source.episodes):
        raise ValidationError("拆稿需要非空分集和镜头声明，不创建空剧集", code="storyboard_empty")
    for episode_index, episode in enumerate(source.episodes):
        for shot_index, shot in enumerate(episode["shots"]):
            body = shot["body"]
            start, end = body.get("episode_start_ms"), body.get("episode_end_ms")
            duration = body.get("duration_ms")
            if duration is None:
                duration = end - start if start is not None and end is not None else parameters["default_shot_duration_ms"]
            path = f"value.episodes[{episode_index}].shots[{shot_index}]"
            if (start is None) != (end is None) or (start is not None and end - start != duration):
                errors[path] = "镜头时长与明确的集内时间区间不一致"
            if not parameters["video_shot_min_duration_ms"] <= duration <= parameters["video_shot_max_duration_ms"]:
                errors[path + ".duration_ms"] = (f"{duration}ms不在本轮冻结时长范围"
                    f"{parameters['video_shot_min_duration_ms']}–{parameters['video_shot_max_duration_ms']}ms内")
            for key in ("start_ms", "end_ms"):
                value = body.get("dialogue", {}).get(key)
                if value is not None and value > duration:
                    errors[path + ".dialogue." + key] = f"对白时间超过本镜有效时长{duration}ms，不自动截断"
            timings[episode["number"], shot["number"]] = {"duration_ms": duration,
                "episode_start_ms": start, "episode_end_ms": end}
    if errors:
        raise ValidationError(errors, code="script_duration_policy_mismatch")
    return timings


@transaction.atomic
def read_storyboard_materialization_plan(*, run, stage):
    """Prepare both entry points from the exact M2/M3 source and frozen Run.

    Authored fields come from the saved shared structure, independently from
    finite relation/timing semantics. Reading never writes bodies/receipts.
    The writer must reread and compare this contract.
    """
    run = ProductionRun.objects.select_for_update().get(pk=run.pk)
    plan = get_run_execution_plan(run)
    expected_mode = "manual" if run.plan_source == "manual" else "deterministic"
    if not run.stages.filter(pk=stage.pk, stage_key="storyboard", mode=expected_mode).exists():
        raise ValidationError("分镜阶段不属于本轮明确物化入口", code="storyboard_scope_conflict")
    if plan.value["scope"]["kind"] != "project":
        raise ValidationError("本轮只包含指定剧集，不能将新分集加入冻结范围", code="storyboard_scope_conflict")
    source = read_storyboard_source_plan(run=run, stage=stage)
    limit = boundary_int("security.snapshot.stage_max_bytes")
    config = verify_json_contract(run.config_snapshot, schema_version=run.config_snapshot_schema_version,
        size_bytes=run.config_snapshot_size_bytes, sha256=run.config_snapshot_sha256, max_bytes=limit)
    parameters = declared_storyboard_parameters(values=config.value.get("values") if isinstance(config.value, dict) else None)
    timings = declared_storyboard_timings(source=source, parameters=parameters)
    contract = json_contract({"schema_version": 3, "source_basis": source.basis,
        "run_config_sha256": config.sha256, "parameters": parameters}, schema_version=3, max_bytes=limit)
    return StoryboardMaterializationPlan(source=source, contract=contract, timings=timings)


def _declared_dialogue(source_shot):
    """Project script semantics into the one persisted dialogue contract.

    This projection is shared with the body writer. It never converts
    a speaker into a visible character or invents optional timing/delivery.
    """
    if "dialogue" not in source_shot["body"]:
        return []
    dialogue = deepcopy(source_shot["body"]["dialogue"])
    dialogue.pop("speaker_entity_key", None)
    speaker = source_shot["relations"]["speaker"]
    # An explicit empty declaration is not a spoken line. Its empty/zero/false
    # values remain in the exact script source, not an invalid Shot row.
    if speaker is None and not dialogue.get("text"):
        return []
    if "is_voiceover" in dialogue:
        dialogue["voiceover"] = dialogue.pop("is_voiceover")
    if speaker is not None:
        dialogue["speaker_id"] = speaker["speaker_id"]
    return [dialogue]


def _relation_conflict(message):
    raise ValidationError(message, code="storyboard_relation_conflict")


@transaction.atomic
def _materialize_declared_relations(*, run, stage, expected_basis_sha256, shots_by_coordinate, allow_create=True):
    """Internal relation phase of the common writer, not an adoption endpoint.

    The caller owns Episode/Shot creation and the original Stage/command receipt
    in its encompassing transaction. This phase accepts an exact coordinate->ID
    set, rechecks the original source under Run/project locks, and only fills a
    completely empty relation set. Exact replay is read-only; partial/existing
    or subsequently selected media is never repaired, cleared or overwritten.

    Both formal entry points use the common body writer. Inferred name matches
    must never be passed off as authored relations to this internal phase.
    """
    run = ProductionRun.objects.select_for_update().only("id", "project_id").get(pk=run.pk)
    source = read_storyboard_source_plan(run=run, stage=stage)
    basis = json_contract(source.basis, max_bytes=boundary_int("security.snapshot.stage_max_bytes"))
    if basis.sha256 != expected_basis_sha256:
        _relation_conflict("分镜的原剧本、采用决定或设定来源已变化，请重新预检")
    planned = {(episode["number"], shot["number"]): shot
        for episode in source.episodes for shot in episode["shots"]}
    if (not isinstance(shots_by_coordinate, dict) or set(shots_by_coordinate) != set(planned)
            or any(not isinstance(key, tuple) or len(key) != 2 or any(type(n) is not int for n in key)
                for key in shots_by_coordinate)):
        _relation_conflict("必须明确提供原预检的全部集/镜坐标，不能遗漏或追加镜头")
    ids = list(shots_by_coordinate.values())
    if len({str(pk) for pk in ids}) != len(ids):
        _relation_conflict("同一个镜头不能占用两个声明坐标")
    try:
        shots = list(Shot.objects.select_for_update(of=("self",)).filter(pk__in=ids)
            .select_related("episode").only("id", "number", "episode_id", "episode__id", "episode__number",
                "episode__project_id", "scene_id", "dialogue"))
    except (ValueError, TypeError, ValidationError) as exc:
        raise ValidationError("镜头身份无效", code="storyboard_relation_conflict") from exc
    by_id = {str(shot.pk): shot for shot in shots}
    if set(by_id) != {str(pk) for pk in ids}:
        _relation_conflict("明确指定的镜头已缺失")

    def related_ids(field):
        relation = Shot._meta.get_field(field)
        rows = relation.remote_field.through.objects.filter(shot_id__in=ids).values_list(
            "shot_id", f"{relation.m2m_reverse_field_name()}_id")
        result = {str(pk): [] for pk in ids}
        for shot_id, target_id in rows:
            result[str(shot_id)].append(str(target_id))
        return {key: sorted(value) for key, value in result.items()}

    props = related_ids("props")
    character_fields = ("role", "position", "character_id")
    use_fields = ("use_key", "position", "media_kind", "purpose", "setting_revision_id",
        "source_mode", "reference_version_id", "asset_id", "asset_sha256")
    actual_characters, actual_uses = ({str(pk): [] for pk in ids} for _ in range(2))
    for row in ShotCharacterRelation.objects.filter(shot_id__in=ids).order_by("role", "position").values(
            "shot_id", *character_fields):
        actual_characters[str(row.pop("shot_id"))].append({**row, "character_id": str(row["character_id"])})
    for row in ShotReferenceUse.objects.filter(shot_id__in=ids).order_by("position").values("shot_id", *use_fields):
        shot_id = str(row.pop("shot_id"))
        for field in ("setting_revision_id", "reference_version_id", "asset_id"):
            row[field] = str(row[field]) if row[field] else None
        actual_uses[shot_id].append(row)

    desired, actual = {}, {}
    character_rows, use_rows, prop_rows, changed_shots = [], [], [], []
    prop_through = Shot.props.through
    prop_fk = Shot._meta.get_field("props").m2m_reverse_field_name() + "_id"
    for coordinate, declaration in planned.items():
        shot = by_id[str(shots_by_coordinate[coordinate])]
        shot_id = str(shot.pk)
        if shot.episode.project_id != run.project_id or (shot.episode.number, shot.number) != coordinate:
            _relation_conflict("目标镜头不属于本项目或其明确集/镜坐标不一致")
        if shot.dialogue != _declared_dialogue(declaration):
            _relation_conflict("目标镜头的对白/发言人/时间与原声明不一致，不能改写正文补关系")
        relations = declaration["relations"]
        characters = [{"role": "visible", "position": index, "character_id": row["entity_id"]}
            for index, row in enumerate(relations["visible_characters"])]
        if relations["speaker"] is not None:
            characters.append({"role": "speaker", "position": 0, "character_id": relations["speaker"]["entity_id"]})
        characters.sort(key=lambda row: (row["role"], row["position"]))
        uses = [{"use_key": row["use_key"], "position": index, "media_kind": row["media_kind"],
            "purpose": row["purpose"], "setting_revision_id": row["setting_revision_id"],
            "source_mode": "", "reference_version_id": None, "asset_id": None, "asset_sha256": ""}
            for index, row in enumerate(relations["uses"])]
        desired[shot_id] = {"scene_id": relations["scene"]["entity_id"] if relations["scene"] else None,
            "props": sorted(row["entity_id"] for row in relations["props"]), "characters": characters, "uses": uses}
        actual[shot_id] = {"scene_id": str(shot.scene_id) if shot.scene_id else None, "props": props[shot_id],
            "characters": actual_characters[shot_id], "uses": actual_uses[shot_id]}
        for row in characters:
            instance = ShotCharacterRelation(shot=shot, **row)
            # FK ownership/setting identity comes from the freshly verified
            # source plan. Do not repeat per-row FK queries over large plans.
            instance.clean_fields(exclude=("shot", "character"))
            character_rows.append(instance)
        for row in uses:
            instance = ShotReferenceUse(shot=shot, **row)
            instance.clean_fields(exclude=("shot", "setting_revision", "reference_version", "asset"))
            use_rows.append(instance)
        prop_rows.extend(prop_through(shot_id=shot.pk, **{prop_fk: pk}) for pk in desired[shot_id]["props"])
        if desired[shot_id] != actual[shot_id]:
            shot.scene_id = desired[shot_id]["scene_id"]
            changed_shots.append(shot)
    if actual == desired:
        return False
    if not allow_create:
        _relation_conflict("已有分镜的关系已变化或不完整，不能补写当成幂等重放")
    if any(row["scene_id"] is not None or row["props"] or row["characters"] or row["uses"] for row in actual.values()):
        _relation_conflict("已有关系与本次完整声明不一致，不能补齐半套关系或覆盖后续修改")
    # Validate the entire set before the first mutation, then commit together.
    updated_at = timezone.now()
    for shot in changed_shots:
        shot.updated_at = updated_at
    Shot.objects.bulk_update(changed_shots, ("scene", "updated_at"))
    prop_through.objects.bulk_create(prop_rows)
    ShotCharacterRelation.objects.bulk_create(character_rows)
    ShotReferenceUse.objects.bulk_create(use_rows)
    return True


@dataclass(frozen=True)
class DeclaredStoryboardResult:
    episodes: list
    shots_by_coordinate: dict
    created: bool


def _creation_rows(source, parameters, timings):
    """Finite scheduling/display facts only. Creative values stay in M2 detail."""
    episodes, shots = {}, {}
    for episode in source.episodes:
        episodes[episode["number"]] = {"number": episode["number"], "title": episode["title"],
            "synopsis": "", "script": "", "continuity_from_previous": "", "continuity_to_next": "",
            "status": Episode.Status.DRAFT,
            "prompt_settings": {"multi_shot": parameters["multi_shot"],
                "include_audio": parameters["include_audio"]}}
        for shot in episode["shots"]:
            coordinate = (episode["number"], shot["number"])
            shots[coordinate] = {"number": shot["number"], "title": "",
                "dialogue": _declared_dialogue(shot), **timings[coordinate],
                "media_type": parameters["default_shot_media_type"], "conditioning_mode": "",
                "include_audio": parameters["include_audio"] if parameters["default_shot_media_type"] == "video" else False,
                "requires_first_frame_continuity": parameters["default_first_frame_continuity"],
                "requires_last_frame_continuity": parameters["default_last_frame_continuity"],
                "metadata": {}}
    return episodes, shots


@transaction.atomic
def materialize_declared_storyboard(*, run, stage, expected_contract_sha256):
    """Atomic body/source/relation phase for the original entry-point switch.

    No adoption, stage completion, generation or approval is synthesized here.
    The enclosing command/stage transaction must include its own final receipt.
    """
    run = ProductionRun.objects.select_for_update().only("id", "project_id").get(pk=run.pk)
    proposed = read_storyboard_materialization_plan(run=run, stage=stage)
    if proposed.contract.sha256 != expected_contract_sha256:
        raise ValidationError("分镜来源或冻结配置已变化，请重新预检", code="storyboard_source_conflict")
    desired_episodes, desired_shots = _creation_rows(proposed.source, proposed.contract.value["parameters"], proposed.timings)
    reference = proposed.source.basis["source"]["structure_source"]
    binding = {"structure_id": proposed.source.basis["script"]["id"], "stage_id": str(stage.pk),
        "document_sha256": reference["document_sha256"], "materialization_sha256": proposed.contract.sha256}
    # The source reader holds the project lock. Compare a complete existing set
    # before any mutation; no repair of half-written or later-edited storyboards.
    actual_episodes = list(Episode.objects.select_for_update(of=("self",)).filter(project_id=run.project_id).values(
        "id", *next(iter(desired_episodes.values())).keys()))
    actual_shots = list(Shot.objects.select_for_update(of=("self",)).filter(episode__project_id=run.project_id).values(
        "id", "episode__number", *next(iter(desired_shots.values())).keys()))
    sources = list(ShotScriptSource.objects.select_for_update(of=("self",)).filter(shot__episode__project_id=run.project_id).values(
        "shot_id", "structure_id", "stage_id", "episode_number", "shot_number", "document_sha256", "materialization_sha256"))
    # Also catch a damaged/out-of-project binding claiming one of this Stage's
    # coordinates before bulk_create would hit a uniqueness constraint.
    if ShotScriptSource.objects.filter(stage_id=stage.pk).exclude(shot__episode__project_id=run.project_id).exists():
        _relation_conflict("本阶段已有跨项目来源绑定，不能创建或覆盖分镜")
    created = not actual_episodes
    episode_ids, shots_by_coordinate = {}, {}
    if not created:
        actual_by_coordinate = {}
        for row in actual_episodes:
            episode_ids[row["number"]] = row.pop("id")
        for row in actual_shots:
            coordinate = (row.pop("episode__number"), row["number"])
            shots_by_coordinate[coordinate] = row.pop("id")
            actual_by_coordinate[coordinate] = row
        if ({row["number"]: row for row in actual_episodes} != desired_episodes
                or actual_by_coordinate != desired_shots
                or len(sources) != len(desired_shots)):
            _relation_conflict("项目已有不同或不完整的分镜，不覆盖、不补齐半套数据")
        for source in sources:
            coordinate = (source.pop("episode_number"), source.pop("shot_number"))
            if (str(source.pop("shot_id")) != str(shots_by_coordinate.get(coordinate))
                    or {key: str(value) for key, value in source.items()} != binding):
                _relation_conflict("分镜的确切来源绑定与本次原始创建合同不一致")
        _materialize_declared_relations(run=run, stage=stage,
            expected_basis_sha256=json_contract(proposed.source.basis,
                max_bytes=boundary_int("security.snapshot.stage_max_bytes")).sha256,
            shots_by_coordinate=shots_by_coordinate, allow_create=False)
    else:
        episodes = [Episode(project_id=run.project_id, **row) for row in desired_episodes.values()]
        for episode in episodes:
            episode.clean_fields(exclude=("project", "title"))
            # Empty titles are permitted by the shared semantic contract. A
            # supplied label must still fit its real column, without truncation.
            Episode._meta.get_field("title").run_validators(episode.title)
        Episode.objects.bulk_create(episodes)
        episode_ids = {episode.number: episode.pk for episode in episodes}
        shots = [Shot(episode_id=episode_ids[coordinate[0]], **row) for coordinate, row in desired_shots.items()]
        for shot in shots:
            shot.clean_fields(exclude=("episode",))
        Shot.objects.bulk_create(shots)
        shots_by_coordinate = {coordinate: shot.pk for coordinate, shot in zip(desired_shots, shots)}
        _materialize_declared_relations(run=run, stage=stage,
            expected_basis_sha256=json_contract(proposed.source.basis,
                max_bytes=boundary_int("security.snapshot.stage_max_bytes")).sha256,
            shots_by_coordinate=shots_by_coordinate)
        ShotScriptSource.objects.bulk_create([ShotScriptSource(shot_id=pk,
            episode_number=coordinate[0], shot_number=coordinate[1], **binding)
            for coordinate, pk in shots_by_coordinate.items()])
    # Return in authored order, not model ordering (which sorts display numbers).
    returned = Episode.objects.only("id", "number", "project_id").in_bulk(episode_ids.values())
    return DeclaredStoryboardResult(episodes=[returned[episode_ids[number]]
        for number in desired_episodes], shots_by_coordinate={coordinate: shots_by_coordinate[coordinate]
            for coordinate in desired_shots}, created=created)


def validate_automatic_storyboard_target(project):
    if project.episodes.exists():
        raise ValidationError("项目已存在剧集或分镜，不能自动覆盖；请使用 storyboard/manual 核对现有内容，或使用新项目。",
            code="existing_storyboard_requires_manual_resolution")
