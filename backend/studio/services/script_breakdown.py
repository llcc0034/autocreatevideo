from __future__ import annotations

from dataclasses import dataclass, field
import math
import re

from studio.models import Episode, SeriesProject, Shot
from studio.services.dialogue import parse_delivery_annotation


EPISODE_RE = re.compile(
    r"^\s*(?:第\s*(?P<zh>\d+)\s*集|EP(?:ISODE)?\s*(?P<en>\d+))\s*[:：.、\-]?\s*(?P<title>.*)$",
    re.IGNORECASE,
)
SHOT_RE = re.compile(r"^\s*(?:镜头|分镜|SHOT)\s*\d*\s*[:：.、\-]?\s*(?P<body>.*)$", re.IGNORECASE)
DIALOGUE_RE = re.compile(
    r"^\s*(?P<name>[^：:（）()\n]{1,160}?)\s*"
    r"(?:[（(]\s*(?P<delivery>[^）)\n]{1,500}?)\s*[）)])?\s*"
    r"[：:]\s*(?P<text>.+?)\s*$"
)


@dataclass
class BreakdownResult:
    episodes: list[Episode]
    warnings: list[str] = field(default_factory=list)
    method: str = "deterministic-script-breakdown-v1"


def _paragraphs(text: str) -> list[str]:
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
                current = [marker.group("body")] if marker.group("body") else []
            elif line.strip():
                current.append(line.strip())
        if current:
            shots.append("\n".join(current).strip())
        return [value for value in shots if value]
    blocks = [value.strip() for value in re.split(r"\n\s*\n+", normalized) if value.strip()]
    if len(blocks) == 1 and len(blocks[0]) > 320:
        sentences = [value.strip() for value in re.split(r"(?<=[。！？!?])", blocks[0]) if value.strip()]
        blocks, current, size = [], [], 0
        for sentence in sentences:
            if current and size + len(sentence) > 220:
                blocks.append("".join(current))
                current, size = [], 0
            current.append(sentence)
            size += len(sentence)
        if current:
            blocks.append("".join(current))
    return blocks


def _episode_sections(script: str, hint: int) -> tuple[list[tuple[str, list[str]]], bool]:
    lines = script.replace("\r\n", "\n").replace("\r", "\n").split("\n")
    headings = [(index, EPISODE_RE.match(line)) for index, line in enumerate(lines) if EPISODE_RE.match(line)]
    if headings:
        sections = []
        for position, (line_index, match) in enumerate(headings):
            end = headings[position + 1][0] if position + 1 < len(headings) else len(lines)
            number = int(match.group("zh") or match.group("en"))
            title = match.group("title").strip() or f"第 {number} 集"
            sections.append((title, _paragraphs("\n".join(lines[line_index + 1:end]))))
        return sections, True
    blocks = _paragraphs(script)
    if not blocks:
        return [], False
    count = min(hint, len(blocks))
    size = math.ceil(len(blocks) / count)
    return [(f"第 {index + 1} 集", blocks[index * size:(index + 1) * size]) for index in range(count)], False


def breakdown_script(
    project: SeriesProject,
    script: str,
    *, episode_count_hint: int = 1,
    default_shot_duration_ms: int = 5000,
    multi_shot: bool = True,
    include_audio: bool = True,
) -> BreakdownResult:
    sections, explicit_headings = _episode_sections(script, episode_count_hint)
    if not sections:
        raise ValueError("剧本为空或无法识别有效内容")
    characters = list(project.characters.order_by("name"))
    scenes = list(project.scenes.order_by("name"))
    props = list(project.props.order_by("name"))
    configured_mode = (project.production_settings or {}).get("default_h3_mode")
    default_h3_mode = (
        configured_mode
        if configured_mode in {Shot.H3Mode.T2VA, Shot.H3Mode.FL2VA, Shot.H3Mode.REF2VA}
        else Shot.H3Mode.T2VA
    )
    character_by_name = {item.name: item for item in characters}
    character_by_name.update({item.stable_speaker_id: item for item in characters})
    warnings = []
    created = []
    next_episode_number = (project.episodes.order_by("-number").values_list("number", flat=True).first() or 0) + 1
    for title, shot_blocks in sections:
        if not shot_blocks:
            warnings.append(f"{title} 没有可识别分镜，已跳过")
            continue
        episode = Episode.objects.create(
            project=project,
            number=next_episode_number + len(created),
            title=title[:200],
            synopsis=shot_blocks[0][:500],
            script="\n\n".join(shot_blocks),
            prompt_settings={
                "multi_shot": multi_shot, "include_audio": include_audio,
                "default_h3_mode": default_h3_mode,
                "breakdown_method": "deterministic-script-breakdown-v1",
            },
        )
        for shot_index, block in enumerate(shot_blocks, start=1):
            dialogue, narrative_lines, bound_characters = [], [], set()
            for line in block.split("\n"):
                match = DIALOGUE_RE.match(line)
                character = character_by_name.get(match.group("name").strip()) if match else None
                if match and character:
                    entry = {
                        "speaker_id": character.stable_speaker_id,
                        "text": match.group("text").strip(),
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
                    narrative_lines.append(line.strip())
                    if match and not character:
                        warnings.append(f"{title} 分镜 {shot_index}: 对白人物“{match.group('name').strip()}”未在角色 bible 中找到")
            for character in characters:
                if character.name in block:
                    bound_characters.add(character)
            scene = next((item for item in scenes if item.name in block), scenes[0] if len(scenes) == 1 else None)
            bound_props = [item for item in props if item.name in block]
            narrative = " ".join(filter(None, narrative_lines)).strip()
            if not narrative:
                names = "、".join(item.name for item in bound_characters)
                narrative = f"{names or '人物'}在当前场景完成对白与反应。"
            shot = Shot.objects.create(
                episode=episode,
                number=shot_index,
                title=(narrative or block).replace("\n", " ")[:80],
                narrative=narrative,
                camera="cinematic continuity-aware framing and motivated camera movement",
                dialogue=dialogue,
                duration_ms=default_shot_duration_ms,
                h3_mode=default_h3_mode,
                include_audio=include_audio,
                scene=scene,
            )
            shot.characters.set(bound_characters)
            shot.props.set(bound_props)
            if default_h3_mode == Shot.H3Mode.REF2VA:
                bindings = {"characters": [], "props": []}
                reference_assets = []

                def bind_entity(entity, category: str):
                    asset = entity.anchor_asset or entity.reference_assets.order_by(
                        "kind", "name", "id",
                    ).first()
                    if not asset:
                        warnings.append(
                            f"{title} 分镜 {shot_index}: 默认 Ref2VA 尚缺{category}“{entity.name}”参考图；"
                            "将使用 continuity Bible 文字定义，也可稍后补充分类参考图"
                        )
                        return None
                    reference_assets.append(asset)
                    return {"entity_id": str(entity.id), "asset_id": str(asset.id)}

                bindings["characters"] = [item for item in (
                    bind_entity(item, "人物")
                    for item in sorted(bound_characters, key=lambda value: value.stable_speaker_id)
                ) if item]
                if scene:
                    scene_binding = bind_entity(scene, "场景")
                    if scene_binding:
                        bindings["scene"] = scene_binding
                bindings["props"] = [
                    item for item in (bind_entity(value, "道具") for value in bound_props) if item
                ]
                bindings = {key: value for key, value in bindings.items() if value}
                if not bindings:
                    warnings.append(
                        f"{title} 分镜 {shot_index}: 默认 Ref2VA 暂无可用分类参考图；"
                        "将使用零参考图文字模式，也可在分镜编辑器补图"
                    )
                asset_ids = [str(item.id) for item in reference_assets]
                if len(asset_ids) != len(set(asset_ids)):
                    warnings.append(
                        f"{title} 分镜 {shot_index}: 自动参考图有重复，已清空以避免错误分类；请手动绑定"
                    )
                    bindings, reference_assets = {}, []
                if len(reference_assets) > 9:
                    warnings.append(
                        f"{title} 分镜 {shot_index}: 自动参考图超过 9 张，已清空；请精简后手动绑定"
                    )
                    bindings, reference_assets = {}, []
                shot.reference_bindings = bindings
                shot.save(update_fields=("reference_bindings", "updated_at"))
                shot.reference_assets.set(reference_assets)
        created.append(episode)
    if not created:
        raise ValueError("没有创建任何有效剧集")
    if explicit_headings and episode_count_hint != len(sections):
        warnings.append("检测到明确的集标题，已忽略 episode_count_hint")
    return BreakdownResult(created, list(dict.fromkeys(warnings)))
