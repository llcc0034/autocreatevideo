"""Editable M2 examples only. No runtime defaults, registered people or writes."""
from copy import deepcopy

from studio.tests.output_structure_fixtures import frozen_response_structure


def path(*tokens, scope="item"):
    return {"kind": "path", "scope": scope, "path": list(tokens)}


def obj(**fields):
    return {"kind": "object", "fields": fields}


def each(source, item):
    return {"kind": "each", "source": source, "item": item}


def literal(value):
    return {"kind": "literal", "value": value}


def script_example(layout="a"):
    # Each fixture declares the same meanings but different external property
    # names/nesting. Names, languages and creative values can all be edited.
    entity = {"key": "person-1", "kind": "character", "name": "测试人物", "identity_anchor": "同一身份",
        "primary": {"reference_name": "测试人物·主图", "anchor": "主图设定"},
        "variants": [{"key": "uniform", "name": "校服", "reference_name": "测试人物·校服", "anchor": "换成校服"},
            {"key": "coat", "name": "外套", "reference_name": "person-1 · coat", "anchor": "换成外套"}]}
    shots = [{"shot": index, "action": "转身" if index == 1 else "走向门口", "duration_ms": 5000,
        "dialogue": {"speaker_entity_key": "person-1", "text": "等一下！", "is_voiceover": False, "start_ms": 0, "end_ms": 1000},
        "references": [{"use_key": "appearance", "entity_key": "person-1", "variant_key": variant,
            "media_kind": "image", "purpose": "appearance"}], "mood_note": "紧张"}
        for index, variant in enumerate(("uniform", "coat"), 1)]
    raw = {"entities": [entity], "episodes": [{"episode": 1, "shots": shots}, {"episode": 2, "shots": deepcopy(shots)}]}
    entity_mapping = obj(key=path("key"), kind=path("kind"), name=path("name"),
        identity_anchor=path("identity_anchor"), primary=path("primary"), variants=path("variants"))
    shot_mapping = obj(shot=path("shot"), action=path("action"), duration_ms=path("duration_ms"),
        dialogue=path("dialogue"), references=path("references"))
    node = obj(entities=each(path("entities", scope="root"), entity_mapping),
        episodes=each(path("episodes", scope="root"), obj(episode=path("episode"), shots=each(path("shots"), shot_mapping))))
    if layout == "b":
        raw = {"world": {"cast": [{"id": entity["key"], "type": entity["kind"], "label": entity["name"],
            "identity": entity["identity_anchor"], "looks": {"base": entity["primary"], "alternatives": entity["variants"]}}]},
            "chapters": [{"number": episode["episode"], "frames": [{"index": shot["shot"], "motion": shot["action"],
                "timing": {"milliseconds": shot["duration_ms"]}, "speech": shot["dialogue"],
                "uses": shot["references"], "tone": shot["mood_note"]} for shot in episode["shots"]]} for episode in raw["episodes"]]}
        node = obj(entities=each(path("world", "cast", scope="root"), obj(key=path("id"), kind=path("type"),
            name=path("label"), identity_anchor=path("identity"), primary=path("looks", "base"), variants=path("looks", "alternatives"))),
            episodes=each(path("chapters", scope="root"), obj(episode=path("number"), shots=each(path("frames"),
                obj(shot=path("index"), action=path("motion"), duration_ms=path("timing", "milliseconds"),
                    dialogue=path("speech"), references=path("uses"))))))
    elif layout != "a":
        raise ValueError("unknown isolated layout")
    return raw, frozen_response_structure(raw), {"schema_version": 1, "node": node}
