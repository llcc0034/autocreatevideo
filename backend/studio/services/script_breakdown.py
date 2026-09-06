from __future__ import annotations

from dataclasses import dataclass, field
from decimal import Decimal, InvalidOperation, ROUND_HALF_UP
from collections.abc import Mapping
import math
import re

from studio.services.configuration import resolve_configuration
from django.db.models import Q

from studio.models import (
    AssemblyJob,
    CharacterBible,
    ConsistencyLock,
    Episode,
    GeneratedWorkflow,
    GenerationTask,
    PromptArtifact,
    PromptFieldTranslation,
    PropBible,
    ReleaseBundle,
    SceneBible,
    SeriesProject,
    Shot,
)
from studio.services.dialogue import parse_delivery_annotation
from studio.services.script_bible import (
    longest_non_overlapping_entities,
    normalize_script_bible_name,
    parse_script_bible,
)


EPISODE_RE = re.compile(
    r"^\s*(?:第\s*(?P<zh>\d+)\s*集|EP(?:ISODE)?\s*(?P<en>\d+))\s*[:：.、\-]?\s*(?P<title>.*)$",
    re.IGNORECASE,
)
SHOT_RE = re.compile(
    r"^\s*(?:镜头|分镜|SHOT)\s*\d*\s*"
    r"(?P<body>(?:[【\[].*|[:：.、\-].*|))$",
    re.IGNORECASE,
)
DIALOGUE_RE = re.compile(
    r"^\s*(?P<name>[^：:（）()\n]+?)\s*"
    r"(?:[（(]\s*(?P<delivery>[^）)\n]+?)\s*[）)])?\s*"
    r"[：:]\s*(?P<text>.+?)\s*$"
)
DIRECTIVE_RE = re.compile(r"^[【\[]\s*(?P<body>[^】\]]+)\s*[】\]]\s*")
TIME_RANGE_RE = re.compile(
    r"^\s*[【\[]\s*(?P<start>-?(?:\d+:\d{1,2}(?:\.\d+)?|\d+(?:\.\d+)?)\s*(?:ms|毫秒|s|秒)?)\s*"
    r"(?:-|–|—|~|～|至|到)\s*"
    r"(?P<end>-?(?:\d+:\d{1,2}(?:\.\d+)?|\d+(?:\.\d+)?)\s*(?:ms|毫秒|s|秒)?)\s*"
    r"[】\]]\s*(?P<body>.*)$",
    re.IGNORECASE,
)
LABELED_TIME_RANGE_RE = re.compile(
    r"^\s*(?P<label>动作|ACTION|对白|DIALOGUE)\s*"
    r"[【\[]\s*(?P<start>-?(?:\d+:\d{1,2}(?:\.\d+)?|\d+(?:\.\d+)?)\s*(?:ms|毫秒|s|秒)?)\s*"
    r"(?:-|–|—|~|～|至|到)\s*"
    r"(?P<end>-?(?:\d+:\d{1,2}(?:\.\d+)?|\d+(?:\.\d+)?)\s*(?:ms|毫秒|s|秒)?)\s*"
    r"[】\]]\s*[:：]?\s*(?P<body>.*)$",
    re.IGNORECASE,
)
ACTION_LABEL_RE = re.compile(r"^\s*(?:动作|ACTION)\s*[:：]\s*", re.IGNORECASE)
DIALOGUE_LABEL_RE = re.compile(r"^\s*(?:对白|DIALOGUE)\s*[:：]\s*", re.IGNORECASE)

_TRUE_VALUES = {"1", "true", "yes", "on", "是", "需要", "连续"}
_FALSE_VALUES = {"0", "false", "no", "off", "否", "不需要", "无"}
_TRAILING_CJK_NAME_RE = re.compile(r"[\u3400-\u4dbf\u4e00-\u9fff]+$")
SCRIPT_PROP_ALIAS_POLICY_KEYS = frozenset({
    "schema_version",
    "minimum_core_characters",
    "maximum_prefix_trim_characters",
    "generic_aliases",
})


@dataclass
class BreakdownResult:
    episodes: list[Episode]
    warnings: list[str] = field(default_factory=list)
    method: str = "deterministic-script-breakdown-v4"


def normalize_default_conditioning_mode(value: object) -> str:
    """Normalize one model-agnostic conditioning mode key."""

    configured_mode = str(value or "").strip()
    if re.fullmatch(r"[A-Za-z][A-Za-z0-9_.-]{0,63}", configured_mode) is None:
        raise ValueError("项目缺少有效的 default_conditioning_mode")
    return configured_mode


def project_default_conditioning_mode(project: SeriesProject) -> str:
    """Return the current project setting when starting a new execution contract."""

    return normalize_default_conditioning_mode(
        (project.production_settings or {}).get("default_conditioning_mode"),
    )


@dataclass(frozen=True)
class ConfirmedScriptBibleAliases:
    """Trusted original declaration names resolved to current Bible rows."""

    characters: Mapping[str, CharacterBible] = field(default_factory=dict)
    scenes: Mapping[str, SceneBible] = field(default_factory=dict)
    props: Mapping[str, PropBible] = field(default_factory=dict)


class ScriptRebuildConflict(Exception):
    """A rebuild cannot safely discard the current production lineage."""

    def __init__(self, message: str, *, code: str, details: dict):
        super().__init__(message)
        self.code = code
        self.details = {"detail": message, **details}


def _orphan_translation_query(project: SeriesProject) -> Q:
    character_ids = CharacterBible.objects.filter(project=project).values_list("id", flat=True)
    scene_ids = SceneBible.objects.filter(project=project).values_list("id", flat=True)
    prop_ids = PropBible.objects.filter(project=project).values_list("id", flat=True)
    return (
        Q(scope_type=PromptFieldTranslation.ScopeType.SHOT)
        | (
            Q(scope_type=PromptFieldTranslation.ScopeType.CHARACTER)
            & ~Q(scope_id__in=character_ids)
        )
        | (
            Q(scope_type=PromptFieldTranslation.ScopeType.SCENE)
            & ~Q(scope_id__in=scene_ids)
        )
        | (
            Q(scope_type=PromptFieldTranslation.ScopeType.PROP)
            & ~Q(scope_id__in=prop_ids)
        )
    )


def _prompt_references_replaced_shots(
    prompt: PromptArtifact,
    *,
    episode_ids: set,
    shot_ids: set[str],
) -> bool:
    if prompt.episode_id in episode_ids or str(prompt.shot_id or "") in shot_ids:
        return True
    options = prompt.request_options if isinstance(prompt.request_options, dict) else {}
    requested_shot_ids = options.get("shot_ids")
    return bool(
        isinstance(requested_shot_ids, list)
        and shot_ids.intersection(str(value) for value in requested_shot_ids)
    )


def _rebuild_lineage_ids(
    project: SeriesProject,
    *,
    episode_ids: set,
    shot_ids: set[str],
) -> tuple[set, set, set]:
    """Resolve only prompt/workflow/task rows derived from replaced episodes."""

    prompts = list(project.prompt_artifacts.only(
        "id", "episode_id", "shot_id", "request_options",
    ))
    workflows = list(project.generated_workflows.only(
        "id", "episode_id", "prompt_artifact_id",
    ))
    tasks = list(project.generation_tasks.only(
        "id", "episode_id", "generated_workflow_id",
    ))
    prompt_ids = {
        prompt.id for prompt in prompts
        if _prompt_references_replaced_shots(
            prompt, episode_ids=episode_ids, shot_ids=shot_ids,
        )
    }
    workflow_ids: set = set()
    task_ids: set = set()

    # Follow both explicit episode links and materialized prompt/workflow links.
    # Iteration handles legacy project-level rows whose only episode identity is
    # the prompt's request_options.shot_ids snapshot.
    changed = True
    while changed:
        before = (len(prompt_ids), len(workflow_ids), len(task_ids))
        workflow_ids.update(
            workflow.id for workflow in workflows
            if workflow.episode_id in episode_ids
            or workflow.prompt_artifact_id in prompt_ids
        )
        task_ids.update(
            task.id for task in tasks
            if task.episode_id in episode_ids
            or task.generated_workflow_id in workflow_ids
        )
        target_workflow_ids = {
            task.generated_workflow_id for task in tasks if task.id in task_ids
        }
        workflow_ids.update(target_workflow_ids)
        prompt_ids.update(
            workflow.prompt_artifact_id
            for workflow in workflows
            if workflow.id in workflow_ids
        )
        changed = before != (len(prompt_ids), len(workflow_ids), len(task_ids))
    return prompt_ids, workflow_ids, task_ids


def discard_rebuildable_production(
    project: SeriesProject,
    *,
    discard_existing_production: bool,
) -> dict[str, int | bool]:
    """Discard one project's replaceable episode-production layer.

    The caller must hold the project row lock and wrap this operation plus the
    subsequent deterministic breakdown in one transaction.  Physical media
    files are deliberately retained; database lineage is removed only when no
    active remote work or immutable release evidence exists.
    """

    episodes = list(project.episodes.select_for_update().only("id"))
    if not episodes:
        return {
            "episodes": 0,
            "shots": 0,
            "generation_tasks": 0,
            "assembly_jobs": 0,
            "prompt_artifacts": 0,
            "generated_workflows": 0,
            "prompt_field_translations": 0,
            "draft_release_bundles": 0,
            "physical_outputs_retained": True,
        }

    episode_ids = {episode.id for episode in episodes}
    # Do not lock Shot before deleting its PromptFieldTranslation rows.  The
    # translation confirm path locks translation then Shot; keeping that same
    # order avoids a cross-request lock inversion during rebuild.
    shots = list(Shot.objects.filter(episode_id__in=episode_ids).only("id"))
    shot_ids = {str(shot.id) for shot in shots}
    prompt_ids, workflow_ids, task_ids = _rebuild_lineage_ids(
        project,
        episode_ids=episode_ids,
        shot_ids=shot_ids,
    )
    tasks = list(
        GenerationTask.objects.select_for_update().filter(id__in=task_ids)
    )
    assemblies = list(
        AssemblyJob.objects.select_for_update().filter(
            Q(episode_id__in=episode_ids) | Q(source_tasks__id__in=task_ids),
        ).distinct()
    )
    release_bundles = list(project.release_bundles.select_for_update())

    active_task_statuses = {
        GenerationTask.Status.QUEUED,
        GenerationTask.Status.RUNNING,
        GenerationTask.Status.CANCEL_REQUESTED,
    }
    terminal_submission_states = {
        GenerationTask.SubmissionState.TERMINAL_FAILED,
        GenerationTask.SubmissionState.TERMINAL_CANCELLED,
    }
    active_generation_tasks = sum(
        task.status in active_task_statuses for task in tasks
    )
    unresolved_remote_tasks = sum(
        bool(task.submission_token)
        and task.status != GenerationTask.Status.SUCCEEDED
        and task.submission_state not in terminal_submission_states
        and task.remote_state != GenerationTask.RemoteState.SUCCEEDED
        for task in tasks
    )
    active_assembly_jobs = sum(
        job.status in {AssemblyJob.Status.QUEUED, AssemblyJob.Status.RUNNING}
        for job in assemblies
    )
    protected_release_bundles = sum(
        bundle.status != ReleaseBundle.Status.DRAFT for bundle in release_bundles
    )
    conflict_details = {
        "active_generation_tasks": active_generation_tasks,
        "unresolved_remote_tasks": unresolved_remote_tasks,
        "active_assembly_jobs": active_assembly_jobs,
        "protected_release_bundles": protected_release_bundles,
    }
    if any(conflict_details.values()):
        raise ScriptRebuildConflict(
            "项目仍有运行中或远端未终结的生产任务，或已有不可删除的发行证据；当前数据未作任何修改。",
            code="script_rebuild_blocked",
            details=conflict_details,
        )

    translations = project.prompt_field_translations.filter(
        _orphan_translation_query(project),
    )
    counts: dict[str, int | bool] = {
        "episodes": len(episodes),
        "shots": len(shots),
        "generation_tasks": len(tasks),
        "assembly_jobs": len(assemblies),
        "prompt_artifacts": len(prompt_ids),
        "generated_workflows": len(workflow_ids),
        "prompt_field_translations": translations.count(),
        "draft_release_bundles": sum(
            bundle.status == ReleaseBundle.Status.DRAFT for bundle in release_bundles
        ),
        "physical_outputs_retained": True,
    }
    has_production_history = any(
        int(counts[key]) > 0
        for key in (
            "generation_tasks",
            "assembly_jobs",
            "prompt_artifacts",
            "generated_workflows",
            "prompt_field_translations",
            "draft_release_bundles",
        )
    )
    if has_production_history and not discard_existing_production:
        raise ScriptRebuildConflict(
            "重建会清理现有剧集的终态任务、提示词、草稿、工作流、组装记录和失效翻译；请确认后重试。",
            code="script_rebuild_confirmation_required",
            details={"clearable": counts},
        )

    draft_release_ids = [
        bundle.id for bundle in release_bundles
        if bundle.status == ReleaseBundle.Status.DRAFT
    ]
    if draft_release_ids:
        ReleaseBundle.objects.filter(id__in=draft_release_ids).delete()
    if assemblies:
        AssemblyJob.objects.filter(id__in=[job.id for job in assemblies]).delete()
    if tasks:
        GenerationTask.objects.filter(id__in=[task.id for task in tasks]).delete()

    # GenerationTask protects its materialized workflow.  Once every task is a
    # safe terminal row and has been removed, the remaining replaceable prompt
    # lineage can be discarded before the Episode cascade.
    GeneratedWorkflow.objects.filter(id__in=workflow_ids).delete()
    PromptArtifact.objects.filter(id__in=prompt_ids).delete()
    translations.delete()
    Episode.objects.filter(id__in=episode_ids).delete()

    if project.status != SeriesProject.Status.DRAFT or project.completed_at is not None:
        project.status = SeriesProject.Status.DRAFT
        project.completed_at = None
        project.save(update_fields=("status", "completed_at", "updated_at"))
    return counts


def _parse_boolean(value: str) -> bool | None:
    normalized = value.strip().casefold()
    if normalized in _TRUE_VALUES:
        return True
    if normalized in _FALSE_VALUES:
        return False
    return None


def _parse_time_ms(value: str) -> int:
    normalized = value.strip().casefold().replace(" ", "")
    if ":" in normalized:
        minute_text, second_text = normalized.split(":", 1)
        minutes = Decimal(minute_text)
        seconds = Decimal(re.sub(r"(?:s|秒)$", "", second_text))
        total = (minutes * 60 + seconds) * 1000
    elif normalized.endswith(("ms", "毫秒")):
        number = re.sub(r"(?:ms|毫秒)$", "", normalized)
        total = Decimal(number)
    else:
        number = re.sub(r"(?:s|秒)$", "", normalized)
        total = Decimal(number) * 1000
    return int(total.quantize(Decimal("1"), rounding=ROUND_HALF_UP))


def _format_time_ms(value: int) -> str:
    seconds = Decimal(value) / Decimal(1000)
    return format(seconds.quantize(Decimal("0.001")).normalize(), "f")


def _canonical_timed_action(start_ms: int, end_ms: int, text: str) -> str:
    return f"[{_format_time_ms(start_ms)}s–{_format_time_ms(end_ms)}s] {text.strip()}"


def _shot_prompt_fact_scaffold(
    *,
    scene,
    characters,
    props,
    narrative: str,
    dialogue: list[dict],
) -> tuple[str, str, str]:
    """Materialize grounded layer-one facts for later H3 field generation."""

    character_names = "、".join(
        item.name for item in sorted(characters, key=lambda value: (value.slug, str(value.id)))
    ) or "无已绑定人物"
    prop_names = "、".join(
        item.name for item in sorted(props, key=lambda value: (value.slug, str(value.id)))
    ) or "无已绑定道具"
    scene_name = scene.name if scene is not None else "未指定场景"
    scene_facts = []
    if scene is not None:
        scene_facts = [
            str(getattr(scene, field, "") or "").strip()
            for field in ("location", "time_of_day", "lighting", "color_palette")
        ]
    scene_detail = "；".join(value for value in scene_facts if value) or "以场景 Bible 为准"
    spoken = sum(1 for item in dialogue if not bool(item.get("voiceover")))
    voiceover = sum(1 for item in dialogue if bool(item.get("voiceover")))
    dialogue_fact = (
        f"结构化对白 {spoken} 条、画外音 {voiceover} 条；原文、说话人和时间窗由对白字段单独锁定"
        if dialogue else "本镜头无结构化对白或画外音"
    )
    detail = "\n".join((
        f"场景与光线事实：{scene_name}；{scene_detail}。",
        f"主体与道具事实：人物={character_names}；道具={prop_names}。",
        f"动作时间线与末态事实：{narrative}",
        f"声音与对白事实：{dialogue_fact}。",
        "连续性事实：动作按正向时间发展，呈现初态、连续中间过程和可衔接的稳定末态。",
    ))
    sound = (
        "依据当前场景和可见动作生成同步画内声景：保留环境底噪、人物移动、衣料、脚步和道具接触声；"
        "对白或画外音出现时不得用音效遮盖人声。"
    )
    music = (
        "非画内音乐只依据已确认的全剧风格和本镜头动作节奏配置；不得新增剧情信息，不得遮盖对白、"
        "画外音或同步画内声；剧本未明确要求音乐时保持克制，并允许近乎无配乐。"
    )
    return detail, sound, music


def _parse_shot_header(
    block: str,
    *,
    title: str,
    shot_index: int,
    default_duration_ms: int,
    minimum_duration_ms: int,
    maximum_duration_ms: int,
    default_first_frame_continuity: bool,
    default_last_frame_continuity: bool,
    warnings: list[str],
) -> tuple[str, int, bool, bool]:
    lines = block.split("\n")
    first = lines[0].strip() if lines else ""
    duration_ms = default_duration_ms
    first_frame = default_first_frame_continuity
    last_frame = default_last_frame_continuity
    while True:
        match = DIRECTIVE_RE.match(first)
        if not match or not re.search(r"[=：:]", match.group("body")):
            break
        first = first[match.end():].lstrip()
        for raw_directive in re.split(r"[;；]", match.group("body")):
            if not raw_directive.strip():
                continue
            pair = re.split(r"[=：:]", raw_directive, maxsplit=1)
            if len(pair) != 2:
                warnings.append(
                    f"{title} 分镜 {shot_index}: 无法识别指令“{raw_directive.strip()}”，已使用默认值"
                )
                continue
            key, value = pair[0].strip().casefold(), pair[1].strip()
            if key in {"时长", "duration"}:
                try:
                    parsed = _parse_time_ms(value)
                except (InvalidOperation, ValueError):
                    parsed = 0
                if minimum_duration_ms <= parsed <= maximum_duration_ms:
                    duration_ms = parsed
                else:
                    warnings.append(
                        f"{title} 分镜 {shot_index}: 时长“{value}”不在配置范围 "
                        f"{minimum_duration_ms}–{maximum_duration_ms}ms，"
                        f"已使用默认 {default_duration_ms}ms"
                    )
            elif key in {"首帧连续", "承接上一镜", "first_frame_continuity"}:
                parsed = _parse_boolean(value)
                if parsed is None:
                    warnings.append(
                        f"{title} 分镜 {shot_index}: 首帧连续值“{value}”无效，已使用默认值"
                    )
                else:
                    first_frame = parsed
            elif key in {"尾帧连续", "衔接下一镜", "last_frame_continuity"}:
                parsed = _parse_boolean(value)
                if parsed is None:
                    warnings.append(
                        f"{title} 分镜 {shot_index}: 尾帧连续值“{value}”无效，已使用默认值"
                    )
                else:
                    last_frame = parsed
            elif key in {"连续", "continuity"}:
                normalized = value.strip().casefold()
                mapping = {
                    "无": (False, False), "none": (False, False),
                    "承接上一镜": (True, False), "from_previous": (True, False),
                    "衔接下一镜": (False, True), "to_next": (False, True),
                    "承上启下": (True, True), "bridge": (True, True), "both": (True, True),
                }
                if normalized not in mapping:
                    warnings.append(
                        f"{title} 分镜 {shot_index}: 连续值“{value}”无效，已使用默认值"
                    )
                else:
                    first_frame, last_frame = mapping[normalized]
            else:
                warnings.append(
                    f"{title} 分镜 {shot_index}: 未知指令“{pair[0].strip()}”，已忽略"
                )
    first = re.sub(r"^\s*[:：.、\-]\s*", "", first)
    cleaned = "\n".join([first, *lines[1:]]).strip()
    return cleaned, duration_ms, first_frame, last_frame


def _line_timing(line: str) -> tuple[tuple[int, int] | None, str, str | None, bool]:
    """Return parsed range, body, forced kind and whether timing was declared."""
    labeled = LABELED_TIME_RANGE_RE.match(line)
    match = labeled or TIME_RANGE_RE.match(line)
    if match:
        try:
            timing = (_parse_time_ms(match.group("start")), _parse_time_ms(match.group("end")))
        except (InvalidOperation, ValueError):
            timing = None
        label = match.groupdict().get("label")
        kind = (
            "action" if label and label.casefold() in {"动作", "action"}
            else "dialogue" if label else None
        )
        return timing, match.group("body").strip(), kind, True
    bracket = DIRECTIVE_RE.match(line)
    if bracket and re.search(r"\d\s*(?:-|–|—|~|～|至|到)", bracket.group("body")):
        return None, line[bracket.end():].strip(), None, True
    return None, line.strip(), None, False


def _effective_range(
    timing: tuple[int, int] | None,
    *,
    timing_declared: bool,
    duration_ms: int,
    title: str,
    shot_index: int,
    line_number: int,
    warnings: list[str],
) -> tuple[int, int]:
    if timing and 0 <= timing[0] < timing[1] <= duration_ms:
        return timing
    if timing_declared:
        warnings.append(
            f"{title} 分镜 {shot_index} 第 {line_number} 行时间范围无效或超出镜头时长，"
            "已回退为整镜范围"
        )
    return 0, duration_ms


def _paragraphs(
    text: str,
    *,
    auto_split_trigger_chars: int,
    auto_split_target_chars: int,
) -> list[str]:
    normalized = text.replace("\r\n", "\n").replace("\r", "\n").strip()
    if not normalized:
        return []
    lines = normalized.split("\n")
    shots, current = [], []
    saw_marker = any(SHOT_RE.match(line) for line in lines)
    if saw_marker:
        for line in lines:
            marker = SHOT_RE.match(line)
            if marker:
                if current:
                    shots.append("\n".join(current).strip())
                body = re.sub(r"^\s*[:：.、\-]\s*", "", marker.group("body") or "")
                current = [body] if body else []
            elif line.strip():
                current.append(line.strip())
        if current:
            shots.append("\n".join(current).strip())
        return [value for value in shots if value]
    blocks = [value.strip() for value in re.split(r"\n\s*\n+", normalized) if value.strip()]
    if len(blocks) == 1 and len(blocks[0]) > auto_split_trigger_chars:
        sentences = [value.strip() for value in re.split(r"(?<=[。！？!?])", blocks[0]) if value.strip()]
        blocks, current, size = [], [], 0
        for sentence in sentences:
            if current and size + len(sentence) > auto_split_target_chars:
                blocks.append("".join(current))
                current, size = [], 0
            current.append(sentence)
            size += len(sentence)
        if current:
            blocks.append("".join(current))
    return blocks


def _episode_sections(
    script: str,
    hint: int,
    *,
    auto_split_trigger_chars: int,
    auto_split_target_chars: int,
) -> tuple[list[tuple[str, list[str]]], bool]:
    lines = script.replace("\r\n", "\n").replace("\r", "\n").split("\n")
    headings = [(index, EPISODE_RE.match(line)) for index, line in enumerate(lines) if EPISODE_RE.match(line)]
    if headings:
        sections = []
        for position, (line_index, match) in enumerate(headings):
            end = headings[position + 1][0] if position + 1 < len(headings) else len(lines)
            number = int(match.group("zh") or match.group("en"))
            title = match.group("title").strip() or f"第 {number} 集"
            sections.append((title, _paragraphs(
                "\n".join(lines[line_index + 1:end]),
                auto_split_trigger_chars=auto_split_trigger_chars,
                auto_split_target_chars=auto_split_target_chars,
            )))
        return sections, True
    blocks = _paragraphs(
        script,
        auto_split_trigger_chars=auto_split_trigger_chars,
        auto_split_target_chars=auto_split_target_chars,
    )
    if not blocks:
        return [], False
    count = min(hint, len(blocks))
    size = math.ceil(len(blocks) / count)
    return [(f"第 {index + 1} 集", blocks[index * size:(index + 1) * size]) for index in range(count)], False


def _entity_name_pairs(
    rows: list,
    aliases: Mapping[str, object],
) -> list[tuple[str, object]]:
    """Merge current names with trusted aliases, with aliases taking precedence."""

    rows_by_id = {row.pk: row for row in rows}
    by_name = {
        normalize_script_bible_name(row.name): (row.name, row)
        for row in rows
        if normalize_script_bible_name(row.name)
    }
    for alias, row in aliases.items():
        normalized_alias = normalize_script_bible_name(alias)
        current = rows_by_id.get(getattr(row, "pk", None))
        if (
            not normalized_alias
            or current is None
            or type(current) is not type(row)
        ):
            raise ValueError("已确认 SCRIPT_BIBLE_V1 别名不属于当前项目或类型不匹配")
        by_name[normalized_alias] = (alias, current)
    return list(by_name.values())


def _validated_prop_alias_policy(value: object) -> dict:
    if not isinstance(value, dict) or set(value) != SCRIPT_PROP_ALIAS_POLICY_KEYS:
        raise ValueError("production.script_prop_alias_policy 合同字段无效")
    if value.get("schema_version") != 1:
        raise ValueError("production.script_prop_alias_policy schema_version 无效")
    minimum = value.get("minimum_core_characters")
    maximum_trim = value.get("maximum_prefix_trim_characters")
    generic = value.get("generic_aliases")
    if (
        type(minimum) is not int
        or minimum < 1
        or type(maximum_trim) is not int
        or maximum_trim < 0
        or not isinstance(generic, (list, tuple, frozenset))
        or any(not isinstance(item, str) or not item.strip() for item in generic)
    ):
        raise ValueError("production.script_prop_alias_policy 合同值无效")
    return {
        "schema_version": 1,
        "minimum_core_characters": minimum,
        "maximum_prefix_trim_characters": maximum_trim,
        "generic_aliases": frozenset(
            normalize_script_bible_name(item) for item in generic
        ),
    }


def _prop_core_alias_candidates(
    name: object,
    policy: dict,
) -> tuple[str, ...]:
    """Derive conservative Chinese prop short names from a reviewed full name.

    The inferred form may remove only the configured number of leading
    modifier characters from the final CJK span (``发光怀表`` -> ``怀表``), or
    use a trailing CJK span that follows an explicit non-CJK boundary
    (``USB-钥匙`` -> ``钥匙``).  This deliberately avoids arbitrary suffix
    matching such as treating ``录音机`` as ``音机``.
    """

    normalized = normalize_script_bible_name(name)
    match = _TRAILING_CJK_NAME_RE.search(normalized)
    if match is None:
        return ()
    cjk_name = match.group(0)
    validated = _validated_prop_alias_policy(policy)
    minimum_core_characters = validated["minimum_core_characters"]
    offsets: set[int] = set()
    if match.start() > 0:
        offsets.add(0)
    modifier_chars = min(
        validated["maximum_prefix_trim_characters"],
        len(cjk_name) - minimum_core_characters,
    )
    if modifier_chars > 0:
        offsets.add(modifier_chars)
    candidates = {
        cjk_name[offset:]
        for offset in offsets
        if (
            len(cjk_name[offset:]) >= minimum_core_characters
            and cjk_name[offset:] != normalized
            and cjk_name[offset:] not in validated["generic_aliases"]
        )
    }
    return tuple(sorted(candidates, key=lambda value: (-len(value), value)))


def _prop_name_pairs(
    rows: list[PropBible],
    aliases: Mapping[str, PropBible],
    policy: dict,
) -> list[tuple[str, PropBible]]:
    """Add only project-unique, non-conflicting inferred prop short names."""

    pairs = _entity_name_pairs(rows, aliases)
    exact_owners = {
        normalize_script_bible_name(name): row
        for name, row in pairs
        if normalize_script_bible_name(name)
    }
    inferred_owners: dict[str, dict[object, PropBible]] = {}
    for row in rows:
        identity = getattr(row, "pk", None) or id(row)
        for alias in _prop_core_alias_candidates(row.name, policy):
            inferred_owners.setdefault(alias, {})[identity] = row
    for alias in sorted(inferred_owners, key=lambda value: (-len(value), value)):
        owners = inferred_owners[alias]
        if len(owners) != 1:
            continue
        owner = next(iter(owners.values()))
        exact_owner = exact_owners.get(alias)
        if exact_owner is not None:
            # An explicit current/reviewed name always wins. Reusing the same
            # alias for another row would turn a short mention into a guess.
            continue
        pairs.append((alias, owner))
    return pairs


def breakdown_script(
    project: SeriesProject,
    script: str,
    *,
    episode_count_hint: int,
    multi_shot: bool,
    include_audio: bool,
    default_shot_media_type: str,
    default_shot_duration_ms: int | None = None,
    default_first_frame_continuity: bool | None = None,
    default_last_frame_continuity: bool | None = None,
    video_shot_min_duration_ms: int | None = None,
    video_shot_max_duration_ms: int | None = None,
    auto_split_trigger_chars: int | None = None,
    auto_split_target_chars: int | None = None,
    default_conditioning_mode: str | None = None,
    confirmed_bible_aliases: ConfirmedScriptBibleAliases | None = None,
    prop_alias_policy: dict | None = None,
) -> BreakdownResult:
    if default_shot_media_type not in {
        Shot.MediaType.VIDEO,
        Shot.MediaType.IMAGE,
    }:
        raise ValueError("default_shot_media_type 必须来自项目创建策略")
    required_config = (
        "production.default_shot_duration_ms",
        "production.default_first_frame_continuity",
        "production.default_last_frame_continuity",
        "production.video_shot_min_duration_ms",
        "production.video_shot_max_duration_ms",
        "production.script_auto_split_trigger_chars",
        "production.script_auto_split_target_chars",
    )
    configured = resolve_configuration(required_keys=required_config).values
    if default_shot_duration_ms is None:
        default_shot_duration_ms = configured["production.default_shot_duration_ms"]
    if default_first_frame_continuity is None:
        default_first_frame_continuity = (
            configured["production.default_first_frame_continuity"]
        )
    if default_last_frame_continuity is None:
        default_last_frame_continuity = (
            configured["production.default_last_frame_continuity"]
        )
    if video_shot_min_duration_ms is None:
        video_shot_min_duration_ms = configured["production.video_shot_min_duration_ms"]
    if video_shot_max_duration_ms is None:
        video_shot_max_duration_ms = configured["production.video_shot_max_duration_ms"]
    if auto_split_trigger_chars is None:
        auto_split_trigger_chars = configured["production.script_auto_split_trigger_chars"]
    if auto_split_target_chars is None:
        auto_split_target_chars = configured["production.script_auto_split_target_chars"]
    if not 1 <= video_shot_min_duration_ms <= default_shot_duration_ms <= video_shot_max_duration_ms:
        raise ValueError("默认镜头时长必须位于配置的视频镜头时长范围内")
    if not 1 <= auto_split_target_chars <= auto_split_trigger_chars:
        raise ValueError("自动分段目标字符数不能超过触发字符数")
    parsed_script = parse_script_bible(script)
    sections, explicit_headings = _episode_sections(
        parsed_script.body,
        episode_count_hint,
        auto_split_trigger_chars=auto_split_trigger_chars,
        auto_split_target_chars=auto_split_target_chars,
    )
    if not sections:
        raise ValueError("剧本为空或无法识别有效内容")
    characters = list(project.characters.order_by("name"))
    scenes = list(project.scenes.order_by("name"))
    props = list(project.props.order_by("name"))
    aliases = confirmed_bible_aliases or ConfirmedScriptBibleAliases()
    if prop_alias_policy is None:
        prop_alias_policy = resolve_configuration(
            project=project,
            required_keys=("production.script_prop_alias_policy",),
        ).values["production.script_prop_alias_policy"]
    prop_alias_policy = _validated_prop_alias_policy(prop_alias_policy)
    character_name_pairs = _entity_name_pairs(characters, aliases.characters)
    character_names = {
        normalize_script_bible_name(name): (name, item)
        for name, item in character_name_pairs
    }
    # One authoritative character-name map serves both dialogue and generic
    # scans. Stable speaker IDs must win over an identical current/alias name.
    character_names.update({
        normalize_script_bible_name(item.stable_speaker_id): (
            item.stable_speaker_id, item,
        )
        for item in characters
    })
    character_name_pairs = list(character_names.values())
    scene_name_pairs = _entity_name_pairs(scenes, aliases.scenes)
    prop_name_pairs = _prop_name_pairs(props, aliases.props, prop_alias_policy)
    default_conditioning_mode = (
        project_default_conditioning_mode(project)
        if default_conditioning_mode is None
        else normalize_default_conditioning_mode(default_conditioning_mode)
    )
    character_by_name = {
        normalize_script_bible_name(name): item
        for name, item in character_name_pairs
    }
    warnings = []
    created = []
    episode_title_max_length = Episode._meta.get_field("title").max_length
    shot_title_max_length = Shot._meta.get_field("title").max_length
    next_episode_number = (project.episodes.order_by("-number").values_list("number", flat=True).first() or 0) + 1
    for title, shot_blocks in sections:
        if not shot_blocks:
            warnings.append(f"{title} 没有可识别分镜，已跳过")
            continue
        episode = Episode.objects.create(
            project=project,
            number=next_episode_number + len(created),
            # Titles are display identifiers and must fit the database schema;
            # all authored text remains lossless in synopsis/script/narrative.
            title=title[:episode_title_max_length],
            synopsis=shot_blocks[0],
            script="\n\n".join(shot_blocks),
            prompt_settings={
                "multi_shot": multi_shot, "include_audio": include_audio,
                "default_conditioning_mode": default_conditioning_mode,
                "breakdown_method": "deterministic-script-breakdown-v4",
                "default_shot_duration_ms": default_shot_duration_ms,
                "default_first_frame_continuity": default_first_frame_continuity,
                "default_last_frame_continuity": default_last_frame_continuity,
                "video_shot_min_duration_ms": video_shot_min_duration_ms,
                "video_shot_max_duration_ms": video_shot_max_duration_ms,
                "auto_split_trigger_chars": auto_split_trigger_chars,
                "auto_split_target_chars": auto_split_target_chars,
            },
        )
        for shot_index, block in enumerate(shot_blocks, start=1):
            (
                parsed_block,
                shot_duration_ms,
                first_frame_continuity,
                last_frame_continuity,
            ) = _parse_shot_header(
                block,
                title=title,
                shot_index=shot_index,
                default_duration_ms=default_shot_duration_ms,
                minimum_duration_ms=video_shot_min_duration_ms,
                maximum_duration_ms=video_shot_max_duration_ms,
                default_first_frame_continuity=default_first_frame_continuity,
                default_last_frame_continuity=default_last_frame_continuity,
                warnings=warnings,
            )
            dialogue, narrative_lines, plain_narrative_lines, bound_characters = [], [], [], set()
            for line_number, line in enumerate(parsed_block.split("\n"), start=1):
                timing, body, forced_kind, timing_declared = _line_timing(line)
                if forced_kind == "dialogue":
                    body = DIALOGUE_LABEL_RE.sub("", body)
                match = DIALOGUE_RE.match(body) if forced_kind != "action" else None
                character = character_by_name.get(
                    normalize_script_bible_name(match.group("name"))
                ) if match else None
                if match and character:
                    start_ms, end_ms = _effective_range(
                        timing,
                        timing_declared=timing_declared,
                        duration_ms=shot_duration_ms,
                        title=title,
                        shot_index=shot_index,
                        line_number=line_number,
                        warnings=warnings,
                    )
                    entry = {
                        "speaker_id": character.stable_speaker_id,
                        "text": match.group("text").strip(),
                        "start_ms": start_ms,
                        "end_ms": end_ms,
                    }
                    annotation = (match.group("delivery") or "").strip()
                    if annotation:
                        delivery, voiceover = parse_delivery_annotation(annotation)
                        if delivery:
                            entry["delivery"] = delivery
                        if voiceover:
                            entry["voiceover"] = True
                    dialogue.append(entry)
                    bound_characters.add(character)
                else:
                    action_text = ACTION_LABEL_RE.sub("", body).strip()
                    if action_text:
                        start_ms, end_ms = _effective_range(
                            timing,
                            timing_declared=timing_declared,
                            duration_ms=shot_duration_ms,
                            title=title,
                            shot_index=shot_index,
                            line_number=line_number,
                            warnings=warnings,
                        )
                        narrative_lines.append(
                            _canonical_timed_action(start_ms, end_ms, action_text)
                        )
                        plain_narrative_lines.append(action_text)
                    if match and not character:
                        warnings.append(f"{title} 分镜 {shot_index}: 对白人物“{match.group('name').strip()}”未在角色 bible 中找到")
            bound_characters.update(longest_non_overlapping_entities(
                block, character_name_pairs,
            ))
            matched_scenes = longest_non_overlapping_entities(
                block, scene_name_pairs,
            )
            scene = matched_scenes[0] if matched_scenes else (
                scenes[0] if len(scenes) == 1 else None
            )
            bound_props = longest_non_overlapping_entities(
                block, prop_name_pairs,
            )
            narrative = "\n".join(filter(None, narrative_lines)).strip()
            if not narrative:
                names = "、".join(item.name for item in bound_characters)
                fallback = f"{names or '人物'}在当前场景完成对白与反应。"
                narrative = _canonical_timed_action(0, shot_duration_ms, fallback)
                plain_narrative_lines.append(fallback)
            detailed_description, sound_effects, music = _shot_prompt_fact_scaffold(
                scene=scene,
                characters=bound_characters,
                props=bound_props,
                narrative=narrative,
                dialogue=dialogue,
            )
            shot = Shot.objects.create(
                episode=episode,
                number=shot_index,
                title=(" ".join(plain_narrative_lines) or parsed_block or block).replace(
                    "\n", " ",
                )[:shot_title_max_length],
                narrative=narrative,
                detailed_description=detailed_description,
                camera="cinematic continuity-aware framing and motivated camera movement",
                action=" ".join(plain_narrative_lines).strip(),
                dialogue=dialogue,
                sound_effects=sound_effects,
                music=music,
                duration_ms=shot_duration_ms,
                media_type=default_shot_media_type,
                conditioning_mode=(
                    default_conditioning_mode
                    if default_shot_media_type == Shot.MediaType.VIDEO
                    else ""
                ),
                include_audio=(
                    include_audio
                    if default_shot_media_type == Shot.MediaType.VIDEO
                    else False
                ),
                scene=scene,
                requires_first_frame_continuity=first_frame_continuity,
                requires_last_frame_continuity=last_frame_continuity,
            )
            shot.characters.set(bound_characters)
            shot.props.set(bound_props)
        created.append(episode)
    if not created:
        raise ValueError("没有创建任何有效剧集")
    if explicit_headings and episode_count_hint != len(sections):
        warnings.append("检测到明确的集标题，已忽略 episode_count_hint")
    return BreakdownResult(created, list(dict.fromkeys(warnings)))
