"""Read-only labels for exact authored relationships, outside approval hashes.

Relationship IDs/order come only from the supplied canonical snapshot. Current
Bible names are a labelled display aid, never a lookup key or an execution fact.
"""
from uuid import UUID

from django.core.exceptions import ValidationError

from studio.models import CharacterBible, PropBible, SceneBible


def _id(value):
    if not isinstance(value, str):
        raise ValueError
    return str(UUID(value))


def _number(value, minimum=1):
    if type(value) is not int or value < minimum:
        raise ValueError
    return value


def _title(value):
    if not isinstance(value, str):
        raise ValueError
    return value


def _list(value):
    if not isinstance(value, list):
        raise ValueError
    return value


def storyboard_review_display(*, project_id, canonical):
    """Three bounded ID/name queries at most, independent of shot count."""
    ids = {"character": set(), "scene": set(), "prop": set()}
    rows, episodes, shots = [], set(), set()
    try:
        for episode in _list(canonical["episodes"]):
            episode_id = _id(episode["id"])
            if episode_id in episodes:
                raise ValueError
            episodes.add(episode_id)
            for shot in _list(episode["shots"]):
                shot_id = _id(shot["id"])
                if shot_id in shots:
                    raise ValueError
                shots.add(shot_id)
                scene = _id(shot["scene_id"]) if shot["scene_id"] is not None else None
                if scene:
                    ids["scene"].add(scene)
                props = [_id(value) for value in _list(shot["prop_ids"])]
                if len(set(props)) != len(props):
                    raise ValueError
                ids["prop"].update(props)
                cast, positions, visible = [], set(), set()
                for relation in _list(shot["character_relations"]):
                    role, position = relation["role"], _number(relation["position"], 0)
                    character = _id(relation["character_id"])
                    if (role not in {"visible", "speaker"} or (role, position) in positions
                            or role == "visible" and character in visible):
                        raise ValueError
                    positions.add((role, position))
                    if role == "visible":
                        visible.add(character)
                    ids["character"].add(character)
                    cast.append({"role": role, "position": position, "character_id": character})
                rows.append({"shot_id": shot_id, "episode_id": episode_id,
                    "episode_number": _number(episode["number"]), "episode_title": _title(episode["title"]),
                    "number": _number(shot["number"]), "title": _title(shot["title"]),
                    "scene": scene, "characters": sorted(cast, key=lambda item: (item["role"], item["position"])),
                    "props": props})
    except (KeyError, TypeError, ValueError, AttributeError) as exc:
        raise ValidationError("原分镜排序或类型化关系缺失，不能按名字或旧人物集合补齐") from exc
    names = {kind: {str(pk): name for pk, name in model.objects.filter(
        project_id=project_id, pk__in=ids[kind]).order_by().values_list("id", "name")}
        for kind, model in (("character", CharacterBible), ("scene", SceneBible), ("prop", PropBible))}

    def entity(kind, pk):
        name = names[kind].get(pk)
        return {"id": pk, "name": name if name and name.strip() else None,
            "status": "available" if name and name.strip() else "unavailable"}

    for row in rows:
        row["scene"] = entity("scene", row["scene"]) if row["scene"] else None
        row["characters"] = [{"role": item["role"], "position": item["position"],
            "entity": entity("character", item["character_id"])} for item in row["characters"]]
        row["props"] = [entity("prop", pk) for pk in row["props"]]
    return {"name_basis": "current_bible_by_id", "shots": sorted(rows, key=lambda row: (
        row["episode_number"], row["episode_id"], row["number"], row["shot_id"]))}
