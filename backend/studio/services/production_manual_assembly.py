"""Manual authorization of the original episode assembly queue.

Reading is metadata-only. The first enqueue freezes one reviewed selection and
configuration for the Run; each episode and failed-job retry is explicit.
"""
from copy import deepcopy

from django.core.exceptions import ValidationError
from django.db import transaction
from django.utils import timezone

from studio.models import AssemblyJob, ProductionAssemblyLink, ProductionRun, SeriesProject, StageRun
from studio.services.assembly import AssemblyError, create_assembly, resolve_assembly_configuration
from studio.services.assembly_contracts import (
    assembly_output_schema, assembly_timing_schema, assembly_media_schema,
    assert_assembly_timing_matches, assert_assembly_media_matches, validate_assembly_configuration)
from studio.services.assembly_author_contracts import ASSEMBLY_AUTHOR_KEYS, assembly_author_projection
from studio.services.generated_artifacts import record_assembly_artifact
from studio.services.json_contracts import DATABASE_JSON_FIELD_MAX_BYTES, assign_json_contract, json_contract, verify_json_contract
from studio.services.production_manual_commands import ManualActionResult
from studio.services.production_manual_media_review import manual_media_review_detail
from studio.services.production_run_plans import get_run_execution_plan
from studio.services.shot_timing import validate_sequential_episode_timing


def _conflict(message):
    raise ValidationError(message, code="manual_selection_conflict")


def _stage(run):
    if run.plan_source != "manual" or run.recipe_version_id is not None:
        raise ValidationError("人工装配只用于无配方制作运行")
    stage = StageRun.objects.filter(production_run=run, stage_key="assembly", mode="manual").first()
    if stage is None:
        raise ValidationError("本轮缺少正式人工装配步骤")
    return stage


def _read_contract(stage):
    value = verify_json_contract(stage.input_snapshot, schema_version=stage.input_snapshot_schema_version,
        size_bytes=stage.input_snapshot_size_bytes, sha256=stage.input_snapshot_sha256,
        max_bytes=DATABASE_JSON_FIELD_MAX_BYTES)
    if value.schema_version != 1 or value.value.get("kind") != "manual_assembly":
        raise ValidationError("装配缺少本轮已冻结的选片与输出配置，不能回退读取当前配置")
    validate_assembly_configuration(value.value.get("configuration"), max_bytes=DATABASE_JSON_FIELD_MAX_BYTES)
    authors = value.value.get("authors", {})
    contracts = value.value.get("author_contracts", {})
    if (not isinstance(authors, dict) or not isinstance(contracts, dict)
            or set(authors) != set(contracts) or set(authors) - set(ASSEMBLY_AUTHOR_KEYS)
            or ("authors" in value.value) != ("author_contracts" in value.value)):
        raise ValidationError("冻结装配原文与确切映射合同不完整，不能补当前版本")
    for key, rule in authors.items():
        if not isinstance(contracts[key], dict):
            raise ValidationError("冻结装配映射缺失，不读取当前注册版本")
        result = assembly_author_projection(rule, key=key, frozen=contracts[key], max_bytes=DATABASE_JSON_FIELD_MAX_BYTES)
        output = result["projection"]["output"]
        if key == "assembly_timing":
            assert_assembly_timing_matches(output, value.value)
        elif key == "assembly_media":
            assert_assembly_media_matches(output, value.value, manual=True)
        elif output != {key: value.value["configuration"][key] for key in ("width", "height", "fps")}:
            raise ValidationError("冻结输出配置与装配映射不一致")
    return value


def _inventory(run, stage, plan, *, authors=None):
    if authors is not None and (not isinstance(authors, dict) or set(authors) - set(ASSEMBLY_AUTHOR_KEYS)):
        raise ValidationError("装配作者仅支持输出几何、时序和确切已采用片段映射")
    review = manual_media_review_detail(run)
    blockers, by_shot, episodes = [], {}, []
    if review["approval"]["status"] != "current":
        blockers.append("请先审核本轮完整选片与分镜交付信息；当前审核缺失或依据已变化。")
    for item in review["selection"]["items"]:
        if item["scope"]["kind"] != "shot" or item["kind"] not in {"image", "video"}:
            blockers.append("存在未映射到单镜头的媒体或独立音频；不能自动拆片或丢弃这些已采用媒体。")
            continue
        key = item["scope"]["id"]
        if key in by_shot:
            blockers.append("同一镜头采用了多个输出，请明确唯一选片。")
        by_shot[key] = item
    expected_ids = set()
    # Consume the exact delivery projection included in this review's digest;
    # do not independently reconstruct a second timeline after approval.
    for episode in review["selection"]["delivery_timeline"]:
        try:
            validate_sequential_episode_timing(episode["shots"])
        except ValueError as exc:
            blockers.append(f"第 {episode['number']} 集：{exc}")
        shots = []
        for shot in episode["shots"]:
            key = shot["shot_id"]
            expected_ids.add(key)
            selected = by_shot.get(key)
            if selected is None or selected["kind"] != shot["media_kind"] or type(shot["duration_ms"]) is not int or shot["duration_ms"] <= 0:
                blockers.append(f"第 {episode['number']} 集镜头 {shot['number']} 缺少类型、时长匹配的明确选片。")
            shots.append({**shot, "source": deepcopy(selected)})
        if not shots:
            blockers.append(f"第 {episode['number']} 集尚无正式镜头。")
        episodes.append({**episode, "shots": shots})
    if not episodes:
        blockers.append("本轮尚无正式剧集。")
    if set(by_shot) - expected_ids:
        blockers.append("已采用媒体包含本轮交付范围以外的镜头。")
    value = {"schema_version": 1, "kind": "manual_assembly", "run_plan_sha256": plan.sha256,
        "selection_sha256": review["selection_sha256"], "approval": review["approval"], "episodes": episodes}
    frozen = ProductionAssemblyLink.objects.filter(production_run=run).exists()
    if frozen:
        saved = _read_contract(stage)
        configuration = saved.value["configuration"]
        if authors is not None and authors != saved.value.get("authors", {}):
            _conflict("本轮装配映射已冻结；不能替换原文、版本或映射")
        if value != {key: item for key, item in saved.value.items() if key not in {"configuration", "authors", "author_contracts"}}:
            blockers.append("已封存选片或镜头内容已变化；本轮不能更换来源，请新建制作轮次。")
        contract = saved
    else:
        try:
            configuration = resolve_assembly_configuration(run.project)
        except AssemblyError as exc:
            raise ValidationError(str(exc)) from exc
        if authors:
            contracts = {key: assembly_author_projection(rule, key=key, require_published=True,
                max_bytes=DATABASE_JSON_FIELD_MAX_BYTES) for key, rule in authors.items()}
            if "assembly_output" in contracts:
                configuration = validate_assembly_configuration({**configuration,
                    **contracts["assembly_output"]["projection"]["output"]}, max_bytes=DATABASE_JSON_FIELD_MAX_BYTES)
            if "assembly_timing" in contracts:
                assert_assembly_timing_matches(contracts["assembly_timing"]["projection"]["output"], value)
            if "assembly_media" in contracts:
                assert_assembly_media_matches(contracts["assembly_media"]["projection"]["output"], value, manual=True)
            value.update(authors=deepcopy(authors), author_contracts=contracts)
        contract = json_contract({**value, "configuration": configuration}, max_bytes=DATABASE_JSON_FIELD_MAX_BYTES)
    return contract, blockers, frozen


@transaction.atomic
def manual_assembly_detail(run, *, authors=None, expected_run_plan_sha256=None):
    run = ProductionRun.objects.select_for_update().get(pk=run.pk)
    run.project = SeriesProject.objects.select_for_update().get(pk=run.project_id)
    plan, stage = get_run_execution_plan(run), _stage(run)
    if expected_run_plan_sha256 is not None and expected_run_plan_sha256 != plan.sha256:
        _conflict("装配预览不属于本轮冻结计划")
    contract, blockers, frozen = _inventory(run, stage, plan, authors=authors)
    config = contract.value["configuration"]
    links = {str(link.episode_id): link for link in ProductionAssemblyLink.objects.filter(production_run=run,
        stage_run=stage).select_related("assembly_job").only("id", "episode_id", "assembly_job_id",
        "assembly_job__id", "assembly_job__status", "assembly_job__attempts", "assembly_job__max_attempts",
        "assembly_job__error_message", "assembly_job__updated_at")}
    episodes = []
    for episode in contract.value["episodes"]:
        link = links.get(episode["episode_id"])
        job = link.assembly_job if link else None
        episodes.append({**episode, "assembly_link_id": str(link.pk) if link else None,
            "job": None if job is None else {"id": str(job.pk), "status": job.status,
                "attempts": job.attempts, "max_attempts": job.max_attempts,
                "error_message": job.error_message, "updated_at": job.updated_at}})
    return {"run_id": str(run.pk), "run_plan_sha256": plan.sha256, "contract_sha256": contract.sha256,
        "frozen": frozen, "blockers": blockers, "approval": contract.value["approval"], "episodes": episodes,
        "authors": contract.value.get("authors", {}), "author_contracts": contract.value.get("author_contracts", {}),
        "author_schemas": {"assembly_output": assembly_output_schema(), "assembly_timing": assembly_timing_schema(),
            "assembly_media": assembly_media_schema()},
        "output": {key: config[key] for key in ("width", "height", "fps", "media_profile", "media_profile_key",
            "media_profile_sha256", "max_attempts", "label_enabled", "label_text")}}


def assemble_manual_episode(*, run, stage, plan, request, actor, asset_guard):
    if stage.pk != _stage(run).pk:
        raise ValidationError("装配必须使用本轮正式装配步骤")
    scope = request["scope"]
    if scope["kind"] != "episode" or request["unit_key"] != f"episode:{scope['id']}":
        raise ValidationError("请明确选择本次要装配的剧集")
    payload = request["payload"]
    if set(payload) - {"authors"} != {"expected_contract_sha256", "expected_assembly_job_id"}:
        raise ValidationError("请提交已核对装配合同摘要，以及待重试的准确失败任务（首次为空）")
    run.project = SeriesProject.objects.select_for_update().get(pk=run.project_id)
    if "authors" in payload and payload["authors"] is None:
        raise ValidationError("装配作者必须是对象，不能为null")
    contract, blockers, frozen = _inventory(run, stage, plan, authors=payload.get("authors"))
    if payload["expected_contract_sha256"] != contract.sha256 or request["expected_selection_revision"] != contract.value["approval"]["revision"]:
        _conflict("选片、输出参数或审核修订已变化，请重新读取后确认装配")
    if blockers:
        raise ValidationError(blockers, code="manual_action_not_ready")
    episode = next((row for row in contract.value["episodes"] if row["episode_id"] == scope["id"]), None)
    if episode is None:
        raise ValidationError("此剧集不属于本轮冻结交付范围")
    link = ProductionAssemblyLink.objects.select_for_update().filter(production_run=run, episode_id=scope["id"]).select_related("assembly_job").first()
    expected_job = payload["expected_assembly_job_id"]
    if link:
        if link.stage_run_id != stage.pk:
            raise ValidationError("装配关联不属于本轮正式步骤")
        if expected_job != str(link.assembly_job_id):
            _conflict("剧集已经有装配任务或任务已变化，请重新读取，不重复创建")
        if link.assembly_job.status != "failed":
            raise ValidationError("仅失败装配可明确重试；在途或成功任务不重跑")
    elif expected_job is not None:
        _conflict("首次装配不得引用不存在的旧任务")
    from studio.services.production_runs import _selected_assembly_sources
    try:
        actual = _selected_assembly_sources(run)
        mapping = {row["shot_id"]: {"generated_artifact_id": row["source"]["artifact_id"]} for row in episode["shots"]}
        if any(actual.get(key) != value for key, value in mapping.items()):
            _conflict("来源与已审核选片不一致")
        # Project the reviewed snapshot, never reconstruct timing from Shot.
        sources = {"schema_version": 1, "episodes": [{"episode_id": episode["episode_id"], "number": episode["number"],
            "shots": [{**{key: row[key] for key in ("shot_id", "number", "duration_ms", "media_kind", "episode_start_ms", "episode_end_ms")},
                "source": {"generated_artifact_id": row["source"]["artifact_id"], "sha256": row["source"]["artifact_sha256"],
                    **{key: row["source"][key] for key in ("manifest_sha256", "decision_id", "decision_revision")}}}
                for row in episode["shots"]]}]}
        if not frozen:
            assign_json_contract(stage, "input_snapshot", contract.value, max_bytes=DATABASE_JSON_FIELD_MAX_BYTES)
            stage.save(update_fields=("input_snapshot", "input_snapshot_schema_version", "input_snapshot_size_bytes", "input_snapshot_sha256", "updated_at"))
        job = create_assembly(owner=run.project.owner, project=run.project,
            episode=run.project.episodes.get(pk=scope["id"]), dry_run=False, frozen_sources=sources,
            frozen_configuration=contract.value["configuration"], file_guard=asset_guard, consumer_stage=stage)
    except AssemblyError as exc:
        raise ValidationError(str(exc), code="manual_action_not_ready") from exc
    if link:
        link.assembly_job = job
        link.save(update_fields=("assembly_job", "updated_at"))
    else:
        link = ProductionAssemblyLink.objects.create(production_run=run, stage_run=stage, episode_id=scope["id"], assembly_job=job)
    refresh_manual_assembly(run)
    return ManualActionResult("assembly_link", link.pk)


def refresh_manual_assembly(run):
    """Reconcile already-authorized queue facts; never enqueue or deliver."""
    links = list(ProductionAssemblyLink.objects.filter(production_run=run).select_related("assembly_job"))
    if not links:
        return run
    stage = _stage(run)
    contract = _read_contract(stage)
    expected = {row["episode_id"] for row in contract.value["episodes"]}
    if any(link.stage_run_id != stage.pk or str(link.episode_id) not in expected for link in links):
        raise ValidationError("本轮装配任务超出冻结范围或步骤")
    failed = any(link.assembly_job.status == "failed" for link in links)
    complete = len(links) == len(expected) and all(link.assembly_job.status == "succeeded" for link in links)
    for link in links:
        job = link.assembly_job
        episode = next(row for row in contract.value["episodes"] if row["episode_id"] == str(link.episode_id))
        expected_sources = [(row["shot_id"], row["source"]["artifact_id"], row["duration_ms"]) for row in episode["shots"]]
        actual_sources = [(str(row.shot_id), str(row.generated_artifact_id), row.duration_ms)
            for row in job.source_items.order_by("ordinal").only("shot_id", "generated_artifact_id", "duration_ms")]
        if (job.project_id != run.project_id or job.episode_id != link.episode_id or job.owner_id != run.project.owner_id
                or job.status not in {"planned", "queued", "running", "succeeded", "failed"}
                or actual_sources != expected_sources):
            raise ValidationError("装配任务的来源、归属或状态与本轮冻结合同不一致")
        if link.assembly_job.status == "succeeded":
            from studio.services.provenance import ProvenanceError, verified_assembly_manifest
            try:
                verified_assembly_manifest(job)
            except (OSError, ProvenanceError) as exc:
                raise ValidationError("已完成装配的签名或媒体文件无法核验") from exc
            record_assembly_artifact(link.assembly_job, production_run=run, stage_run=stage)
    stage.status = "blocked" if failed else "succeeded" if complete else "running"
    stage.reason_code = "assembly_failed" if failed else ""
    stage.reason = "存在失败装配，请查看任务后明确重试。" if failed else ""
    stage.started_at = stage.started_at or timezone.now()
    stage.finished_at = timezone.now() if complete else None
    stage.output_reference = f"episode-assembly-jobs:{len(links)}"
    stage.save(update_fields=("status", "reason_code", "reason", "started_at", "finished_at", "output_reference", "updated_at"))
    run.status = "awaiting_input"
    run.save(update_fields=("status", "updated_at"))
    return run
