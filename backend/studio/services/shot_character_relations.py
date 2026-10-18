"""Validate explicit Shot character roles without name or old-pool inference."""
from uuid import UUID

from django.core.exceptions import ValidationError


def shot_character_relations(shot):
    """Use authored roles/positions, never the old pool or speaker-name lookup."""
    relations, dialogue = shot.get("character_relations"), shot.get("dialogue")
    if not isinstance(relations, list) or not isinstance(dialogue, list):
        raise ValidationError("已审核镜头缺少明确的出镜/发声关系和原对白")
    positions, visible, speakers = set(), set(), set()
    for row in relations:
        if (not isinstance(row, dict) or set(row) != {"role", "position", "character_id"}
                or not isinstance(row["role"], str) or row["role"] not in {"visible", "speaker"} or type(row["position"]) is not int
                or row["position"] < 0 or not isinstance(row["character_id"], str)):
            raise ValidationError("已审核镜头的出镜/发声关系无效")
        try:
            if str(UUID(row["character_id"])) != row["character_id"]:
                raise ValueError
        except ValueError as exc:
            raise ValidationError("已审核镜头的人物身份无效") from exc
        key = (row["role"], row["position"])
        if key in positions or row["role"] == "visible" and row["character_id"] in visible:
            raise ValidationError("已审核镜头的出镜/发声关系重复")
        positions.add(key)
        if row["role"] == "visible":
            visible.add(row["character_id"])
        else:
            speakers.add(row["position"])
    if speakers != set(range(len(dialogue))):
        raise ValidationError("原对白与逐行发言者关系不完整，不能按名字或旧人物池补齐")
    return relations
