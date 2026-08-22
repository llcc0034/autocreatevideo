"""Strict SCRIPT_BIBLE_V1 parsing, rendering, and deterministic evidence.

The complete master script remains the authoritative source.  This module
derives review-only candidates from its closed preamble block and never writes
database rows.
"""
from __future__ import annotations

import hashlib
import hmac
import json
import re
import unicodedata
from dataclasses import dataclass
from typing import Any, Iterable


SCRIPT_BIBLE_SCHEMA_VERSION = 1
SCRIPT_BIBLE_BEGIN_MARKER = "【SCRIPT_BIBLE_V1】"
SCRIPT_BIBLE_END_MARKER = "【/SCRIPT_BIBLE_V1】"
SCRIPT_BIBLE_DELIMITER = "｜"
SCRIPT_BIBLE_METADATA_KEY = "script_bible_v1"
SCRIPT_BIBLE_ENTITY_LABELS = {
    "人物": "character",
    "场景": "scene",
    "道具": "prop",
}
SCRIPT_BIBLE_ENTITY_COLLECTIONS = {
    "character": "characters",
    "scene": "scenes",
    "prop": "props",
}
SCRIPT_BIBLE_FIELD_LIMITS = {
    "name": 160,
    "description": 2_000,
    "visual_identity_en": 3_000,
}

_ENTITY_LINE_RE = re.compile(r"^【(?P<label>人物|场景|道具)】(?P<body>.*)$")
_DIALOGUE_LINE_RE = re.compile(
    r"^\s*(?:\[[^\]]+\]\s*)?(?P<name>[^：:（）()\n]{1,160}?)\s*"
    r"(?:[（(][^）)\n]*[）)])?\s*[：:]"
)


class ScriptBibleParseError(ValueError):
    """A closed SCRIPT_BIBLE_V1 block could not be safely interpreted."""

    def __init__(self, message: str, *, code: str, line: int | None = None):
        super().__init__(message)
        self.code = code
        self.line = line
        self.diagnostics = ({
            "line": line,
            "severity": "error",
            "code": code,
            "detail": message,
        },)


class ScriptBibleProvenanceProtectedError(ValueError):
    """An untrusted writer attempted to change reserved provenance."""


@dataclass(frozen=True)
class ScriptBibleParseResult:
    source_fingerprint: str
    body: str
    candidates: tuple[dict[str, Any], ...]
    diagnostics: tuple[dict[str, Any], ...]
    has_block: bool


def normalize_script_bible_name(value: object) -> str:
    return unicodedata.normalize("NFKC", str(value or "")).strip().casefold()


def _normalized_script(script: object) -> str:
    value = str(script or "")
    if value.startswith("\ufeff"):
        value = value[1:]
    return value.replace("\r\n", "\n").replace("\r", "\n")


def script_bible_source_fingerprint(script: object) -> str:
    return hashlib.sha256(_normalized_script(script).encode("utf-8")).hexdigest()


def _safe_field(
    value: object,
    *,
    field: str,
    line: int | None = None,
    required: bool = False,
) -> str:
    if not isinstance(value, str):
        raise ScriptBibleParseError(
            f"{field} 必须是字符串。",
            code="script_bible_field_invalid",
            line=line,
        )
    if SCRIPT_BIBLE_DELIMITER in value or any(
        character in value for character in "\r\n"
    ):
        raise ScriptBibleParseError(
            f"{field} 不能包含换行或保留分隔符“{SCRIPT_BIBLE_DELIMITER}”。",
            code="script_bible_field_reserved_character",
            line=line,
        )
    if any(unicodedata.category(character) in {"Cc", "Cf"} for character in value):
        raise ScriptBibleParseError(
            f"{field} 不能包含控制字符。",
            code="script_bible_field_control_character",
            line=line,
        )
    result = value.strip()
    if required and not result:
        raise ScriptBibleParseError(
            f"{field} 不能为空。",
            code="script_bible_field_required",
            line=line,
        )
    if len(result) > SCRIPT_BIBLE_FIELD_LIMITS[field]:
        raise ScriptBibleParseError(
            f"{field} 超过 {SCRIPT_BIBLE_FIELD_LIMITS[field]} 字符。",
            code="script_bible_field_too_long",
            line=line,
        )
    return result


def _candidate_id(entity_type: str, name: str) -> str:
    canonical = json.dumps(
        {
            "schema_version": SCRIPT_BIBLE_SCHEMA_VERSION,
            "entity_type": entity_type,
            "name": normalize_script_bible_name(name),
        },
        ensure_ascii=False,
        sort_keys=True,
        separators=(",", ":"),
    )
    return hashlib.sha256(canonical.encode("utf-8")).hexdigest()


def _declaration_fingerprint(
    *,
    entity_type: str,
    name: str,
    description: str,
    visual_identity_en: str,
) -> str:
    """Identify one normalized declaration without depending on script body."""

    canonical = json.dumps(
        {
            "schema_version": SCRIPT_BIBLE_SCHEMA_VERSION,
            "entity_type": entity_type,
            "name": normalize_script_bible_name(name),
            "description": unicodedata.normalize("NFKC", description),
            "visual_identity_en": unicodedata.normalize("NFKC", visual_identity_en),
        },
        ensure_ascii=False,
        sort_keys=True,
        separators=(",", ":"),
    )
    return hashlib.sha256(canonical.encode("utf-8")).hexdigest()


def _validated_entity(
    value: object,
    *,
    entity_type: str,
    line: int | None = None,
) -> dict[str, str]:
    if not isinstance(value, dict) or set(value) != {
        "name", "description", "visual_identity_en",
    }:
        raise ScriptBibleParseError(
            "Bible 实体必须且只能包含 name、description、visual_identity_en。",
            code="script_bible_entity_keys_invalid",
            line=line,
        )
    validated = {
        "entity_type": entity_type,
        "name": _safe_field(
            value["name"], field="name", line=line, required=True,
        ),
        "description": _safe_field(
            value["description"], field="description", line=line,
        ),
        "visual_identity_en": _safe_field(
            value["visual_identity_en"], field="visual_identity_en", line=line,
        ),
    }
    return {
        "candidate_id": _candidate_id(entity_type, validated["name"]),
        "declaration_fingerprint": _declaration_fingerprint(**validated),
        **validated,
    }


def validate_script_bible_ir(value: object) -> dict[str, list[dict[str, str]]]:
    """Validate the only structured IR accepted by the deterministic renderer."""

    if not isinstance(value, dict) or set(value) != {"characters", "scenes", "props"}:
        raise ScriptBibleParseError(
            "bible 必须且只能包含 characters、scenes、props。",
            code="script_bible_ir_keys_invalid",
        )
    result: dict[str, list[dict[str, str]]] = {
        "characters": [], "scenes": [], "props": [],
    }
    normalized_names: dict[str, str] = {}
    for collection, entity_type in (
        ("characters", "character"),
        ("scenes", "scene"),
        ("props", "prop"),
    ):
        rows = value[collection]
        if not isinstance(rows, list):
            raise ScriptBibleParseError(
                f"bible.{collection} 必须是数组。",
                code="script_bible_ir_collection_invalid",
            )
        for row in rows:
            candidate = _validated_entity(row, entity_type=entity_type)
            normalized = normalize_script_bible_name(candidate["name"])
            if normalized in normalized_names:
                raise ScriptBibleParseError(
                    f"Bible 名称“{candidate['name']}”与 {normalized_names[normalized]} 重复。",
                    code="script_bible_name_conflict",
                )
            normalized_names[normalized] = f"{entity_type}/{candidate['name']}"
            result[collection].append({
                key: candidate[key]
                for key in ("name", "description", "visual_identity_en")
            })
    return result


def render_script_bible(value: object) -> str:
    """Render validated structured IR into one closed SCRIPT_BIBLE_V1 block."""

    bible = validate_script_bible_ir(value)
    lines = [SCRIPT_BIBLE_BEGIN_MARKER]
    labels = {"characters": "人物", "scenes": "场景", "props": "道具"}
    for collection in ("characters", "scenes", "props"):
        for row in bible[collection]:
            lines.append(
                f"【{labels[collection]}】{row['name']}"
                f"{SCRIPT_BIBLE_DELIMITER}{row['description']}"
                f"{SCRIPT_BIBLE_DELIMITER}{row['visual_identity_en']}"
            )
    lines.append(SCRIPT_BIBLE_END_MARKER)
    return "\n".join(lines)


def _marker_error(lines: list[str]) -> ScriptBibleParseError | None:
    for index, line in enumerate(lines, start=1):
        if "SCRIPT_BIBLE_V1" in line and line not in {
            SCRIPT_BIBLE_BEGIN_MARKER, SCRIPT_BIBLE_END_MARKER,
        }:
            return ScriptBibleParseError(
                "SCRIPT_BIBLE_V1 标记必须独占一行且不得出现在正文中。",
                code="script_bible_marker_not_standalone",
                line=index,
            )
    return None


def _longest_non_overlapping_matches(
    text: str,
    candidates: Iterable[dict[str, Any]],
) -> list[tuple[int, int, dict[str, Any]]]:
    normalized_text = unicodedata.normalize("NFKC", text).casefold()
    matches: list[tuple[int, int, dict[str, Any]]] = []
    for candidate in candidates:
        name = normalize_script_bible_name(candidate.get("name"))
        if not name:
            continue
        start = 0
        while True:
            index = normalized_text.find(name, start)
            if index < 0:
                break
            matches.append((index, index + len(name), candidate))
            start = index + 1
    matches.sort(key=lambda item: (item[0], -(item[1] - item[0]), item[2]["candidate_id"]))
    selected: list[tuple[int, int, dict[str, Any]]] = []
    occupied_until = -1
    for start, end, candidate in matches:
        if start < occupied_until:
            continue
        selected.append((start, end, candidate))
        occupied_until = end
    return selected


def longest_non_overlapping_entities(
    text: str,
    named_entities: Iterable[tuple[str, Any]],
) -> list[Any]:
    """Resolve overlapping entity names once, preferring the longest match."""

    rows: list[dict[str, Any]] = []
    for index, (name, entity) in enumerate(named_entities):
        normalized = normalize_script_bible_name(name)
        if normalized:
            rows.append({
                "candidate_id": f"{index:012d}",
                "name": normalized,
                "entity": entity,
            })
    selected = _longest_non_overlapping_matches(text, rows)
    result: list[Any] = []
    seen: set[object] = set()
    for _start, _end, row in selected:
        entity = row["entity"]
        identity = getattr(entity, "pk", None) or id(entity)
        if identity not in seen:
            seen.add(identity)
            result.append(entity)
    return result


def _evidence_kind(line: str, candidate: dict[str, Any]) -> str:
    dialogue = _DIALOGUE_LINE_RE.match(line)
    if (
        candidate["entity_type"] == "character"
        and dialogue
        and normalize_script_bible_name(dialogue.group("name"))
        == normalize_script_bible_name(candidate["name"])
    ):
        return "dialogue_speaker"
    if candidate["entity_type"] == "scene" and re.match(r"^\s*(?:镜头|分镜|SHOT)", line, re.I):
        return "shot_setting"
    return "body_mention"


def parse_script_bible(script: object) -> ScriptBibleParseResult:
    """Parse one optional closed preamble block without mutating the source."""

    normalized = _normalized_script(script)
    fingerprint = script_bible_source_fingerprint(normalized)
    lines = normalized.split("\n")
    marker_error = _marker_error(lines)
    if marker_error:
        raise marker_error
    begin_indexes = [index for index, line in enumerate(lines) if line == SCRIPT_BIBLE_BEGIN_MARKER]
    end_indexes = [index for index, line in enumerate(lines) if line == SCRIPT_BIBLE_END_MARKER]
    if not begin_indexes and not end_indexes:
        return ScriptBibleParseResult(
            source_fingerprint=fingerprint,
            body=normalized,
            candidates=(),
            diagnostics=(),
            has_block=False,
        )
    if len(begin_indexes) != 1 or len(end_indexes) != 1:
        raise ScriptBibleParseError(
            "完整剧本只能包含一个闭合的 SCRIPT_BIBLE_V1 块。",
            code="script_bible_marker_count_invalid",
        )
    begin, end = begin_indexes[0], end_indexes[0]
    if end <= begin:
        raise ScriptBibleParseError(
            "SCRIPT_BIBLE_V1 结束标记必须位于开始标记之后。",
            code="script_bible_marker_order_invalid",
            line=end + 1,
        )
    if any(line.strip() for line in lines[:begin]):
        raise ScriptBibleParseError(
            "SCRIPT_BIBLE_V1 必须是完整剧本的前置块。",
            code="script_bible_not_preamble",
            line=begin + 1,
        )

    candidates: list[dict[str, Any]] = []
    diagnostics: list[dict[str, Any]] = []
    normalized_names: dict[str, str] = {}
    for index in range(begin + 1, end):
        raw_line = lines[index]
        if not raw_line.strip():
            continue
        matched = _ENTITY_LINE_RE.fullmatch(raw_line)
        if not matched:
            raise ScriptBibleParseError(
                "SCRIPT_BIBLE_V1 实体行格式无效。",
                code="script_bible_entity_line_invalid",
                line=index + 1,
            )
        parts = matched.group("body").split(SCRIPT_BIBLE_DELIMITER)
        if len(parts) != 3:
            raise ScriptBibleParseError(
                "Bible 实体行必须包含名称、描述和英文视觉锚点三个字段。",
                code="script_bible_entity_field_count_invalid",
                line=index + 1,
            )
        entity_type = SCRIPT_BIBLE_ENTITY_LABELS[matched.group("label")]
        candidate = _validated_entity(
            {
                "name": parts[0],
                "description": parts[1],
                "visual_identity_en": parts[2],
            },
            entity_type=entity_type,
            line=index + 1,
        )
        normalized_name = normalize_script_bible_name(candidate["name"])
        if normalized_name in normalized_names:
            raise ScriptBibleParseError(
                f"Bible 名称“{candidate['name']}”与 {normalized_names[normalized_name]} 重复。",
                code="script_bible_name_conflict",
                line=index + 1,
            )
        normalized_names[normalized_name] = f"{entity_type}/{candidate['name']}"
        candidate["declaration_line"] = index + 1
        candidates.append(candidate)
        diagnostics.append({
            "line": index + 1,
            "severity": "info",
            "code": "script_bible_candidate_parsed",
            "entity_type": entity_type,
            "candidate_id": candidate["candidate_id"],
        })

    body_lines = lines[end + 1:]
    while body_lines and not body_lines[0]:
        body_lines.pop(0)
    body = "\n".join(body_lines)
    evidence_by_id: dict[str, list[dict[str, Any]]] = {
        candidate["candidate_id"]: [] for candidate in candidates
    }
    occurrence_by_id = {candidate["candidate_id"]: 0 for candidate in candidates}
    body_start_line = end + 2 + (len(lines[end + 1:]) - len(body_lines))
    for relative_index, line in enumerate(body_lines):
        for _start, _finish, candidate in _longest_non_overlapping_matches(line, candidates):
            candidate_id = candidate["candidate_id"]
            occurrence_by_id[candidate_id] += 1
            evidence_by_id[candidate_id].append({
                "line": body_start_line + relative_index,
                "kind": _evidence_kind(line, candidate),
                "excerpt": line.strip()[:240],
            })

    hydrated: list[dict[str, Any]] = []
    for candidate in candidates:
        item = dict(candidate)
        candidate_id = item["candidate_id"]
        item["occurrence"] = occurrence_by_id[candidate_id]
        item["evidence"] = evidence_by_id[candidate_id]
        item["recommended"] = item["occurrence"] > 0
        if not item["description"]:
            diagnostics.append({
                "line": item["declaration_line"],
                "severity": "warning",
                "code": "script_bible_description_empty",
                "entity_type": item["entity_type"],
                "candidate_id": candidate_id,
            })
        if not item["visual_identity_en"]:
            diagnostics.append({
                "line": item["declaration_line"],
                "severity": "warning",
                "code": "script_bible_visual_identity_empty",
                "entity_type": item["entity_type"],
                "candidate_id": candidate_id,
            })
        if not item["recommended"]:
            diagnostics.append({
                "line": item["declaration_line"],
                "severity": "warning",
                "code": "script_bible_entity_unused",
                "entity_type": item["entity_type"],
                "candidate_id": candidate_id,
            })
        hydrated.append(item)
    return ScriptBibleParseResult(
        source_fingerprint=fingerprint,
        body=body,
        candidates=tuple(hydrated),
        diagnostics=tuple(diagnostics),
        has_block=True,
    )


def candidate_content_matches(candidate: dict[str, Any], bible: Any) -> bool:
    return (
        str(getattr(bible, "description", "") or "").strip()
        == candidate["description"]
        and str(getattr(bible, "visual_identity", "") or "").strip()
        == candidate["visual_identity_en"]
    )


def script_bible_reviewed_content_sha256(candidate: dict[str, Any]) -> str:
    canonical = json.dumps(
        {
            "name": candidate["name"],
            "description": candidate["description"],
            "visual_identity_en": candidate["visual_identity_en"],
        },
        ensure_ascii=False,
        sort_keys=True,
        separators=(",", ":"),
    )
    return hashlib.sha256(canonical.encode("utf-8")).hexdigest()


def validate_untrusted_script_bible_metadata(
    previous_metadata: object,
    incoming_metadata: object,
) -> None:
    """Reject any non-apply mutation of the reserved provenance member."""

    previous = previous_metadata if isinstance(previous_metadata, dict) else {}
    incoming = incoming_metadata if isinstance(incoming_metadata, dict) else {}
    previous_has = SCRIPT_BIBLE_METADATA_KEY in previous
    incoming_has = SCRIPT_BIBLE_METADATA_KEY in incoming
    previous_provenance = previous.get(SCRIPT_BIBLE_METADATA_KEY)
    incoming_provenance = incoming.get(SCRIPT_BIBLE_METADATA_KEY)
    same_provenance = False
    if previous_has and incoming_has:
        try:
            same_provenance = json.dumps(
                previous_provenance,
                ensure_ascii=False,
                sort_keys=True,
                separators=(",", ":"),
            ) == json.dumps(
                incoming_provenance,
                ensure_ascii=False,
                sort_keys=True,
                separators=(",", ":"),
            )
        except (TypeError, ValueError):
            same_provenance = False
    if previous_has != incoming_has or (previous_has and not same_provenance):
        raise ScriptBibleProvenanceProtectedError(
            "metadata.script_bible_v1 只能由完整剧本候选的正式 apply 接口写入，"
            "其他入口不能注入、修改或删除。"
        )


def _valid_reviewed_provenance_sha256(provenance: object) -> str | None:
    """Return a verified reviewed snapshot hash, otherwise fail closed."""

    if not isinstance(provenance, dict) or set(provenance) != {
        "schema_version",
        "candidate_id",
        "declaration_fingerprint",
        "source_fingerprint",
        "reviewed_snapshot",
        "reviewed_content_sha256",
    }:
        return None
    if (
        type(provenance.get("schema_version")) is not int
        or provenance.get("schema_version") != SCRIPT_BIBLE_SCHEMA_VERSION
    ):
        return None
    for field in (
        "candidate_id", "declaration_fingerprint", "source_fingerprint",
        "reviewed_content_sha256",
    ):
        field_value = provenance.get(field)
        if type(field_value) is not str or not re.fullmatch(
            r"[0-9a-f]{64}", field_value,
        ):
            return None
    snapshot = provenance.get("reviewed_snapshot")
    if not isinstance(snapshot, dict) or set(snapshot) != {
        "name", "description", "visual_identity_en",
    }:
        return None
    try:
        validated = _validated_entity(snapshot, entity_type="character")
    except ScriptBibleParseError:
        return None
    expected = script_bible_reviewed_content_sha256(validated)
    provided = provenance["reviewed_content_sha256"]
    return expected if hmac.compare_digest(provided, expected) else None


def classify_script_bible_candidates(
    candidates: Iterable[dict[str, Any]],
    *,
    characters: Iterable[Any],
    scenes: Iterable[Any],
    props: Iterable[Any],
) -> list[dict[str, Any]]:
    """Attach current project state without changing either source."""

    typed = {
        "character": list(characters),
        "scene": list(scenes),
        "prop": list(props),
    }
    all_by_name: dict[str, list[tuple[str, Any]]] = {}
    for entity_type, rows in typed.items():
        for row in rows:
            all_by_name.setdefault(normalize_script_bible_name(row.name), []).append(
                (entity_type, row)
            )
    result: list[dict[str, Any]] = []
    for source in candidates:
        item = dict(source)
        provenance_matches: list[tuple[str, Any]] = []
        candidate_id_matches: list[tuple[str, Any]] = []
        for provenance_type, rows in typed.items():
            for row in rows:
                metadata = row.metadata if isinstance(row.metadata, dict) else {}
                provenance = metadata.get(SCRIPT_BIBLE_METADATA_KEY)
                if (
                    isinstance(provenance, dict)
                    and type(provenance.get("schema_version")) is int
                    and provenance.get("schema_version") == SCRIPT_BIBLE_SCHEMA_VERSION
                    and provenance.get("candidate_id") == item["candidate_id"]
                ):
                    candidate_id_matches.append((provenance_type, row))
                    if (
                        provenance.get("declaration_fingerprint")
                        == item["declaration_fingerprint"]
                    ):
                        provenance_matches.append((provenance_type, row))
        if provenance_matches:
            matched_type, matched = provenance_matches[0]
            provenance = (
                matched.metadata.get(SCRIPT_BIBLE_METADATA_KEY)
                if isinstance(matched.metadata, dict) else None
            )
            reviewed_sha256 = _valid_reviewed_provenance_sha256(provenance)
            if (
                len(provenance_matches) != 1
                or len(candidate_id_matches) != 1
                or matched_type != item["entity_type"]
                or reviewed_sha256 is None
            ):
                item["matched_bible_id"] = str(matched.pk)
                item["existing"] = False
                item["confirmed"] = False
                item["conflict_code"] = "script_bible_provenance_conflict"
                result.append(item)
                continue
            item["matched_bible_id"] = str(matched.pk)
            item["existing"] = True
            item["confirmed"] = True
            item["conflict_code"] = None
            item["_reviewed_content_sha256"] = reviewed_sha256
            result.append(item)
            continue
        if candidate_id_matches:
            _matched_type, matched = candidate_id_matches[0]
            item["matched_bible_id"] = str(matched.pk)
            item["existing"] = False
            item["confirmed"] = False
            item["conflict_code"] = "script_bible_provenance_conflict"
            result.append(item)
            continue
        matches = all_by_name.get(normalize_script_bible_name(item["name"]), [])
        same_type = next(
            (row for entity_type, row in matches if entity_type == item["entity_type"]),
            None,
        )
        cross_type = next(
            (row for entity_type, row in matches if entity_type != item["entity_type"]),
            None,
        )
        matched = same_type or cross_type
        item["matched_bible_id"] = str(matched.pk) if matched is not None else None
        item["existing"] = bool(
            same_type is not None and candidate_content_matches(item, same_type)
        )
        item["confirmed"] = False
        same_type_metadata = (
            same_type.metadata if same_type is not None
            and isinstance(same_type.metadata, dict) else {}
        )
        other_provenance = same_type_metadata.get(SCRIPT_BIBLE_METADATA_KEY)
        if cross_type is not None:
            item["existing"] = False
            item["conflict_code"] = "script_bible_cross_type_name_conflict"
        elif isinstance(other_provenance, dict):
            item["existing"] = False
            item["conflict_code"] = "script_bible_provenance_conflict"
        elif same_type is not None and not item["existing"]:
            item["conflict_code"] = "script_bible_content_conflict"
        else:
            item["conflict_code"] = None
        result.append(item)
    return result
