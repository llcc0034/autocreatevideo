"""Joins between original approved content facts, never prompt generation."""
from copy import deepcopy

from django.core.exceptions import ValidationError
from studio.services.shot_character_relations import shot_character_relations


def linked_bible_content(shot, bible):
    relations = shot_character_relations(shot)
    def select(section, identities):
        if not isinstance(identities, list) or not all(isinstance(value, str) for value in identities) or len(set(identities)) != len(identities):
            raise ValidationError("已审核镜头的 Bible 关联标识无效")
        result = []
        for identity in identities:
            matches = [row for row in bible[section] if row["id"] == identity]
            if len(matches) != 1:
                raise ValidationError("镜头关联对象未被本轮 Bible 审核唯一覆盖")
            result.append(deepcopy(matches[0]))
        return result
    # The entity bodies are unique; role/position remains explicit alongside
    # them, so speaking-only characters are never projected as visible cast.
    return {"characters": select("characters", list(dict.fromkeys(row["character_id"] for row in relations))),
        "character_relations": deepcopy(relations),
        "scenes": select("scenes", [] if shot["scene_id"] is None else [shot["scene_id"]]),
        "props": select("props", shot["prop_ids"])}


def reviewed_linked_shot_value(field, shot, dependencies, *, run_id, project_id, cache):
    linked = linked_bible_content(shot, dependencies["bible"]["canonical"])
    if field == "linked_bible":
        from studio.services.production_reviewed_content import read_reviewed_entity_primaries, reviewed_entity_content

        decision_id = dependencies["bible"]["review"]["decision_id"]
        sections = {"characters": "character", "scenes": "scene", "props": "prop"}
        rows = {(kind, row["id"]): row for section, kind in sections.items() for row in linked[section]}
        cache_keys = {key: ("bible_primary_setting", str(run_id), decision_id, *key) for key in rows}
        pending = {key: row for key, row in rows.items() if cache_keys[key] not in cache}
        if pending:
            values = read_reviewed_entity_primaries(pending, project_id=project_id, allow_absent=True)
            cache.update({cache_keys[key]: value for key, value in values.items()})
        for section, kind in sections.items():
            linked[section] = [reviewed_entity_content(row, cache[cache_keys[(kind, row["id"])]][0])
                for row in linked[section]]
        return linked
    if field != "dialogue_with_speakers":
        raise ValidationError("关联内容字段未登记")
    result = []
    if not isinstance(shot["dialogue"], list):
        raise ValidationError("已审核对白不是有序列表")
    speakers = {row["position"]: row["character_id"] for row in linked["character_relations"] if row["role"] == "speaker"}
    characters = {row["id"]: row for row in linked["characters"]}
    for position, entry in enumerate(shot["dialogue"]):
        if not isinstance(entry, dict) or not isinstance(entry.get("speaker_id"), str) or not entry["speaker_id"]:
            raise ValidationError("已审核对白缺少明确说话人标识")
        speaker = characters[speakers[position]]
        if speaker.get("stable_speaker_id") != entry["speaker_id"]:
            raise ValidationError("原对白与当前镜头关联的确切发言者不一致，不能按说话人标识重选人物")
        if not all(isinstance(speaker.get(key), str) for key in ("name", "voice_profile")):
            raise ValidationError("已审核人物缺少姓名或声音设定字段")
        result.append({**deepcopy(entry), "subject": speaker["name"], "voice_profile": speaker["voice_profile"],
                       "speaker_subject_key": ["character", speaker["id"]]})
    return result
