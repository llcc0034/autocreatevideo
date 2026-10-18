"""Relations over already type-checked script semantics, independent of layout.

No name inference, default injection, sorting, source rewriting or DB access.
Creative requirements and missing-field policies remain in the frozen structure.
"""
from studio.services.bible_reference_contract import bible_reference_name_key


VALIDATION_SCOPE = "structure_mapping_and_relations"
RELATION_RULES = {
    "entity_duplicate": "实体key重复，不能用名称区分",
    "variant_duplicate": "同一实体的变体key重复",
    "reference_name_duplicate": "不同主图/变体的素材引用名不能相同",
    "entity_unknown": "引用的实体key未声明，不能按名称推断",
    "entity_ambiguous": "引用的实体key有重复声明",
    "entity_kind_mismatch": "引用实体类型须匹配场景/可见人物/道具/发言人关系",
    "episode_duplicate": "集序号重复",
    "shot_duplicate": "同一集的镜序号重复",
    "participant_duplicate": "同镜出场关系不能重复",
    "use_duplicate": "同镜素材使用key重复",
    "variant_unknown": "变体未声明或属于另一实体，不回退主图",
    "variant_ambiguous": "引用的变体key有重复声明",
    "shot_interval_invalid": "集内结束时间须大于开始时间",
    "shot_duration_mismatch": "镜头时长与明确的集内起止时间不一致",
    "speaker_binding_invalid": "非空对白须明确发言人key；无对白不指定发言人，不按名称或可见人物猜测",
    "dialogue_interval_invalid": "对白结束不能早于开始；非空对白须结束晚于开始",
    "dialogue_outside_shot": "对白时间不能超出镜头时长",
}


def script_relation_rules():
    """The request exposes the same finite integrity rules used by diagnostics.

    These are consumer invariants, not configurable creative-field requirements
    or provider property names. Optional fields are checked only when supplied.
    """
    return {"key": "script_relations.v1", "invalid_conditions": dict(RELATION_RULES),
        "optional_fields": "Only validate supplied relationships/times. Defaults and required fields come from RESPONSE_SCHEMA.",
        "participation": "Speaker and media-reference use do not imply visible participation. Preserve authored order, text and times."}


def script_relation_errors(value, *, provenance, consume):
    """Return bounded diagnostics; caller must first validate the semantic schema.

    Mapping evidence identifies the actual source, including whole-object paths
    and lookup results. Literal configuration is never described as source text.
    """
    origins = {row["path"]: row for row in provenance}
    errors = []

    def issue(path, code):
        ancestor = path
        while ancestor not in origins and ancestor != "value":
            ancestor = (ancestor[:ancestor.rfind("[")] if ancestor.endswith("]")
                else ancestor.rsplit(".", 1)[0])
        origin = origins.get(ancestor)
        record = {"path": path, "code": code, "message": RELATION_RULES[code]}
        if origin:
            record["origin_kind"] = origin["kind"]
            if origin.get("source_path") is not None:
                record["source_path"] = origin["source_path"] + path[len(ancestor):]
        consume(record)
        errors.append(record)

    def unique(seen, key, path, code):
        if key in seen:
            issue(path, code)
            return False
        seen.add(key)
        return True

    entities, duplicates, names = {}, set(), set()
    for index, entity in enumerate(value["entities"]):
        path = f"value.entities[{index}]"
        key = entity["key"]
        if key in entities:
            duplicates.add(key)
            issue(path + ".key", "entity_duplicate")
        variants, duplicate_variants = set(), set()
        appearances = [(entity.get("primary", {}), path + ".primary")]
        for offset, variant in enumerate(entity.get("variants", [])):
            location = f"{path}.variants[{offset}]"
            if not unique(variants, variant["key"], location + ".key", "variant_duplicate"):
                duplicate_variants.add(variant["key"])
            appearances.append((variant, location))
        for appearance, location in appearances:
            if "reference_name" in appearance:
                # Same identity comparison as the declaration chain; retain the
                # authored spelling in the value and never translate it.
                name = bible_reference_name_key(appearance["reference_name"])
                unique(names, name, location + ".reference_name", "reference_name_duplicate")
        entities[key] = (entity["kind"], variants, duplicate_variants)

    def entity_reference(key, path, kind=None):
        if key not in entities:
            issue(path, "entity_unknown")
        elif key in duplicates:
            issue(path, "entity_ambiguous")
        elif kind is not None and entities[key][0] != kind:
            issue(path, "entity_kind_mismatch")
        else:
            return entities[key]
        return None

    episodes = set()
    for episode_index, episode in enumerate(value["episodes"]):
        episode_path = f"value.episodes[{episode_index}]"
        unique(episodes, episode["episode"], episode_path + ".episode", "episode_duplicate")
        shots = set()
        for shot_index, shot in enumerate(episode["shots"]):
            path = f"{episode_path}.shots[{shot_index}]"
            unique(shots, shot["shot"], path + ".shot", "shot_duplicate")
            if shot.get("scene_key") is not None:
                entity_reference(shot["scene_key"], path + ".scene_key", "scene")
            for field, kind in (("character_keys", "character"), ("prop_keys", "prop")):
                seen = set()
                for offset, key in enumerate(shot.get(field, [])):
                    location = f"{path}.{field}[{offset}]"
                    unique(seen, key, location, "participant_duplicate")
                    entity_reference(key, location, kind)
            uses = set()
            for offset, reference in enumerate(shot.get("references", [])):
                location = f"{path}.references[{offset}]"
                unique(uses, reference["use_key"], location + ".use_key", "use_duplicate")
                entity = entity_reference(reference["entity_key"], location + ".entity_key")
                variant = reference["variant_key"]
                if entity is not None and variant is not None:
                    if variant not in entity[1]:
                        issue(location + ".variant_key", "variant_unknown")
                    elif variant in entity[2]:
                        issue(location + ".variant_key", "variant_ambiguous")
            start, end, duration = (shot.get(key) for key in ("episode_start_ms", "episode_end_ms", "duration_ms"))
            if start is not None and end is not None:
                if end <= start:
                    issue(path + ".episode_end_ms", "shot_interval_invalid")
                elif duration is not None and end - start != duration:
                    issue(path + ".duration_ms", "shot_duration_mismatch")
            if "dialogue" not in shot:
                continue
            dialogue = shot["dialogue"]
            location = path + ".dialogue"
            speaker = dialogue.get("speaker_entity_key")
            if speaker is not None:
                entity_reference(speaker, location + ".speaker_entity_key", "character")
            if bool(dialogue["text"]) != (speaker is not None):
                issue(location + ".speaker_entity_key", "speaker_binding_invalid")
            # Presence/absence of is_voiceover is resolved solely by the frozen
            # field/default mapping. Neither false nor an absent key is changed.
            start, end = (dialogue.get(key) for key in ("start_ms", "end_ms"))
            if start is not None and end is not None and (end < start or (dialogue["text"] and end == start)):
                issue(location + ".end_ms", "dialogue_interval_invalid")
            if duration is not None:
                for field in ("start_ms", "end_ms"):
                    if field in dialogue and dialogue[field] > duration:
                        issue(location + "." + field, "dialogue_outside_shot")
    return errors
