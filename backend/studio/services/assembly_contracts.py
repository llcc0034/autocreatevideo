"""Typed inputs to the existing assembler; not another field dictionary/queue.

Creative JSON layouts use M1's exact structure and mapper for output geometry
and authored timing; codec/disclosure settings remain the original configuration.
The caller still owns source selection, authorization and durable plan freezing.
"""
from copy import deepcopy
from uuid import UUID

from django.core.exceptions import ValidationError

from studio.services.data_structures import _value_errors
from studio.services.json_contracts import JsonExpansionBudget, json_contract
from studio.services.media_processing_profiles import (
    ASSEMBLY_PROFILE_KEY, MediaProcessingProfileError, verify_media_processing_profile,
)
from studio.services.security_boundaries import boundary_int
from studio.services.structure_mapping import evaluate_mapping
from studio.services.structured_output_parsing import parse_structure_text
from studio.services.shot_timing import validate_sequential_episode_timing


OUTPUT_FIELDS = ("width", "height", "fps")
CONFIGURATION_FIELDS = frozenset({"schema_version", *OUTPUT_FIELDS, "media_profile", "media_profile_key",
    "media_profile_sha256", "max_attempts", "label_enabled", "label_text", "fontfile"})


def validate_assembly_sources(value, *, max_bytes):
    """One finite executor contract for both reviewed and recipe timelines.

    These are resolved identities/timing, not creative field names or a second
    adoption record. Current database facts are checked by the queue consumer.
    """
    json_contract(value, max_bytes=max_bytes)

    def fail(path, message):
        raise ValidationError({path: message})

    def keys(row, expected, path):
        if not isinstance(row, dict) or set(row) != set(expected):
            fail(path, "装配来源字段不完整或含未声明字段，不补当前值")

    def identity(value, path):
        try:
            if type(value) is not str or str(UUID(value)) != value:
                raise ValueError()
        except (ValueError, AttributeError, TypeError):
            fail(path, "必须为确切UUID身份")

    def positive(value, path):
        if type(value) is not int or value < 1:
            fail(path, "必须为明确正整数")

    keys(value, ("schema_version", "episodes"), "assembly_sources")
    if type(value["schema_version"]) is not int or value["schema_version"] != 1:
        fail("assembly_sources.schema_version", "装配来源版本必须为1")
    if not isinstance(value["episodes"], list) or not value["episodes"]:
        fail("assembly_sources.episodes", "必须明确提供非空逐集来源")
    seen_episodes, seen_shots, last_episode = set(), set(), 0
    for i, episode in enumerate(value["episodes"]):
        path = f"assembly_sources.episodes[{i}]"
        keys(episode, ("episode_id", "number", "shots"), path)
        identity(episode["episode_id"], path + ".episode_id")
        positive(episode["number"], path + ".number")
        if episode["episode_id"] in seen_episodes or episode["number"] <= last_episode:
            fail(path, "剧集身份不能重复，顺序须与明确集号一致")
        seen_episodes.add(episode["episode_id"])
        last_episode = episode["number"]
        if not isinstance(episode["shots"], list) or not episode["shots"]:
            fail(path + ".shots", "必须明确提供非空逐镜来源")
        last_shot = 0
        for j, shot in enumerate(episode["shots"]):
            shot_path = f"{path}.shots[{j}]"
            keys(shot, ("shot_id", "number", "duration_ms", "media_kind", "episode_start_ms", "episode_end_ms", "source"), shot_path)
            identity(shot["shot_id"], shot_path + ".shot_id")
            positive(shot["number"], shot_path + ".number")
            positive(shot["duration_ms"], shot_path + ".duration_ms")
            if shot["shot_id"] in seen_shots or shot["number"] <= last_shot:
                fail(shot_path, "镜头身份不能重复，顺序须与明确镜号一致")
            seen_shots.add(shot["shot_id"])
            last_shot = shot["number"]
            if shot["media_kind"] not in ("image", "video"):
                fail(shot_path + ".media_kind", "装配只接受明确图片或视频")
            source = shot["source"]
            keys(source, ("generated_artifact_id", "sha256", "manifest_sha256", "decision_id", "decision_revision"), shot_path + ".source")
            for key in ("generated_artifact_id", "decision_id"):
                identity(source[key], shot_path + ".source." + key)
            positive(source["decision_revision"], shot_path + ".source.decision_revision")
            for key in ("sha256", "manifest_sha256"):
                digest = source[key]
                if type(digest) is not str or len(digest) != 64 or any(c not in "0123456789abcdef" for c in digest):
                    fail(shot_path + ".source." + key, "必须提供确切SHA256")
        try:
            validate_sequential_episode_timing(episode["shots"])
        except ValueError as exc:
            fail(path + ".shots", str(exc))
    return deepcopy(value)


def assembly_output_schema():
    """Finite executor inputs, not configurable creative-field identities."""
    dimension = boundary_int("security.media.max_dimension")
    return {"type": "object", "additionalProperties": False, "required": list(OUTPUT_FIELDS),
        "properties": {key: {"type": "integer", "minimum": 1, "maximum": maximum}
            for key, maximum in (("width", dimension), ("height", dimension),
                ("fps", boundary_int("security.media.max_fps")))}}


def validate_assembly_output(value):
    schema = assembly_output_schema()
    errors = _value_errors(schema, value, path="configuration.output")
    if isinstance(value, dict):
        for key in OUTPUT_FIELDS:
            if key in value and type(value[key]) is not int:
                errors[f"configuration.output.{key}"] = ["必须为明确整数，不转换字符串、布尔或小数"]
    if errors:
        raise ValidationError(errors)
    return deepcopy(value)


def validate_assembly_configuration(value, *, max_bytes):
    """Check a current or frozen value without reading current business settings."""
    if not isinstance(value, dict) or set(value) != CONFIGURATION_FIELDS:
        raise ValidationError({"configuration": "装配配置字段不完整或包含未声明字段，不补当前默认值"})
    json_contract(value, max_bytes=max_bytes)
    if type(value["schema_version"]) is not int or value["schema_version"] != 1:
        raise ValidationError({"configuration.schema_version": "装配配置版本必须为1"})
    validate_assembly_output({key: value[key] for key in OUTPUT_FIELDS})
    if type(value["max_attempts"]) is not int or value["max_attempts"] < 1:
        raise ValidationError({"configuration.max_attempts": "装配重试上限必须为明确正整数"})
    if type(value["label_enabled"]) is not bool:
        raise ValidationError({"configuration.label_enabled": "披露开关必须为布尔值"})
    for key in ("label_text", "fontfile"):
        if type(value[key]) is not str or "\x00" in value[key]:
            raise ValidationError({f"configuration.{key}": "必须为不含NUL的文本"})
    if value["media_profile_key"] != ASSEMBLY_PROFILE_KEY:
        raise ValidationError({"configuration.media_profile_key": "必须引用正式装配编码配置"})
    digest = value["media_profile_sha256"]
    if type(digest) is not str or len(digest) != 64 or any(char not in "0123456789abcdef" for char in digest):
        raise ValidationError({"configuration.media_profile_sha256": "必须提供确切编码配置SHA256"})
    try:
        verify_media_processing_profile(ASSEMBLY_PROFILE_KEY, value["media_profile"], sha256=digest)
    except MediaProcessingProfileError as exc:
        raise ValidationError({"configuration.media_profile": str(exc)}) from exc
    return deepcopy(value)


def assembly_timing_schema():
    """Authored coordinates/timing only; adopted identities remain server-owned."""
    integer = {"type": "integer", "minimum": 1}
    time = {"type": ["integer", "null"], "minimum": 0}
    shot = {"type": "object", "additionalProperties": False,
        "required": ["number", "duration_ms", "episode_start_ms", "episode_end_ms"],
        "properties": {"number": integer, "duration_ms": integer,
            "episode_start_ms": time, "episode_end_ms": time}}
    episode = {"type": "object", "additionalProperties": False, "required": ["number", "shots"],
        "properties": {"number": integer, "shots": {"type": "array", "minItems": 1, "items": shot}}}
    return {"type": "object", "additionalProperties": False, "required": ["episodes"],
        "properties": {"episodes": {"type": "array", "minItems": 1, "items": episode}}}


def validate_assembly_timing(value):
    errors = _value_errors(assembly_timing_schema(), value, path="assembly_timing")
    if errors:
        raise ValidationError(errors)
    previous_episode = 0
    for episode in value["episodes"]:
        if type(episode["number"]) is not int or episode["number"] <= previous_episode:
            raise ValidationError({"assembly_timing": "集号必须为不重复、递增的正整数，不自动排序"})
        previous_episode, previous_shot = episode["number"], 0
        for shot in episode["shots"]:
            if type(shot["number"]) is not int or shot["number"] <= previous_shot:
                raise ValidationError({"assembly_timing": "镜号必须为不重复、递增的正整数，不自动排序"})
            previous_shot = shot["number"]
        try:
            validate_sequential_episode_timing(episode["shots"])
        except ValueError as exc:
            raise ValidationError({"assembly_timing": str(exc)}) from exc
    return deepcopy(value)


def assert_assembly_timing_matches(timing, sources):
    """No source selection or timeline mutation: compare the whole explicit set."""
    actual = {"episodes": [{"number": episode["number"], "shots": [
        {key: shot[key] for key in ("number", "duration_ms", "episode_start_ms", "episode_end_ms")}
        for shot in episode["shots"]]} for episode in sources["episodes"]]}
    if validate_assembly_timing(timing) != actual:
        raise ValidationError({"assembly_timing": "映射后的集镜范围、顺序或时长与本轮分镜不一致；不改选片、不覆盖分镜或补缺项"})


def assembly_media_schema():
    """Exact adopted artifacts, including existing Asset adoptions; no new selection."""
    number = {"type": "integer", "minimum": 1}
    shot = {"type": "object", "additionalProperties": False, "required": ["number", "artifact_id"],
        "properties": {"number": number, "artifact_id": {"type": "string", "format": "uuid"}}}
    episode = {"type": "object", "additionalProperties": False, "required": ["number", "shots"],
        "properties": {"number": number, "shots": {"type": "array", "minItems": 1, "items": shot}}}
    return {"type": "object", "additionalProperties": False, "required": ["episodes"],
        "properties": {"episodes": {"type": "array", "minItems": 1, "items": episode}}}


def validate_assembly_media(value):
    errors = _value_errors(assembly_media_schema(), value, path="assembly_media")
    if errors:
        raise ValidationError(errors)
    previous_episode = 0
    for episode in value["episodes"]:
        if type(episode["number"]) is not int or episode["number"] <= previous_episode:
            raise ValidationError({"assembly_media": "集号必须为不重复、递增的正整数，不自动排序"})
        previous_episode, previous_shot = episode["number"], 0
        for shot in episode["shots"]:
            if type(shot["number"]) is not int or shot["number"] <= previous_shot:
                raise ValidationError({"assembly_media": "镜号必须为不重复、递增的正整数，不自动排序"})
            previous_shot = shot["number"]
            try:
                if type(shot["artifact_id"]) is not str or str(UUID(shot["artifact_id"])) != shot["artifact_id"]:
                    raise ValueError()
            except (ValueError, TypeError, AttributeError):
                raise ValidationError({"assembly_media": "片段必须引用原已采用产物的确切UUID，不以素材名或文件路径替代"}) from None
    return deepcopy(value)


def assert_assembly_media_matches(media, sources, *, manual=False):
    # The two original readers have explicitly different source projections.
    # Missing identities are errors, not a fallback to another reader or Asset.
    source_key = "artifact_id" if manual else "generated_artifact_id"
    actual = {"episodes": []}
    for episode in sources["episodes"]:
        shots = []
        for shot in episode["shots"]:
            source = shot.get("source")
            if not isinstance(source, dict) or not source.get(source_key):
                raise ValidationError({"assembly_media": "本轮镜头尚无明确采用来源，请先通过原选片入口完成采用"})
            shots.append({"number": shot["number"], "artifact_id": source[source_key]})
        actual["episodes"].append({"number": episode["number"], "shots": shots})
    if validate_assembly_media(media) != actual:
        raise ValidationError({"assembly_media": "映射后的完整集镜范围、顺序或产物与本轮采用不一致；不自动采用、换片或补缺项"})


def project_assembly_media_fields(**kwargs):
    return _project_assembly_fields(**kwargs, schema=assembly_media_schema(), validator=validate_assembly_media)


def _project_assembly_fields(*, source_structure, mapping, max_bytes, schema, validator,
        source_text=None, selected_value=None, from_selected=False):
    """One M1 projection for author validation and frozen runtime consumption."""
    budget = JsonExpansionBudget(max_bytes, path="assembly.output.mapping_budget")
    if from_selected:
        # The selected-output reader already verified the original parsed result.
        # Do not serialize it as authored text or apply structure defaults again.
        from studio.services.data_structures import validate_data_structure_snapshot, validate_structure_value
        source_schema = validate_data_structure_snapshot(source_structure)
        validate_structure_value(source_structure, selected_value, path="source")
        budget.consume(selected_value)
        parsed = {"parsed": True, "valid": True, "raw_value": deepcopy(selected_value),
            "value": deepcopy(selected_value), "structure_version_id": source_structure["id"],
            "json_schema": source_schema, "defaults": [], "warnings": [], "errors": {}}
    else:
        parsed = parse_structure_text(structure=source_structure, response_text=source_text,
            max_bytes=max_bytes, path="source", budget=budget)
    result = {"parsed": parsed["parsed"], "valid": False, "raw_value": parsed["raw_value"],
        "source_value": parsed["value"], "source_structure_version_id": parsed["structure_version_id"],
        "defaults": parsed["defaults"], "warnings": parsed["warnings"], "errors": parsed["errors"],
        "provenance": [], "output": None}
    if not parsed["valid"]:
        return result
    try:
        value, origins = evaluate_mapping(mapping, parsed["value"], source_schema=parsed["json_schema"],
            target_schema=schema, consume=budget.consume)
        output = validator(value)
        for origin in origins:
            record = {**origin, "source_structure_version_id": parsed["structure_version_id"]}
            budget.consume(record)
            result["provenance"].append(record)
        result.update(valid=True, output=output)
    except ValidationError as exc:
        result["errors"] = getattr(exc, "message_dict", None) or {"mapping": exc.messages}
    except RecursionError:
        result["errors"] = {"mapping": ["映射嵌套过深，无法完成装配参数解析"]}
    return result


def project_assembly_output_fields(**kwargs):
    return _project_assembly_fields(**kwargs, schema=assembly_output_schema(), validator=validate_assembly_output)


def project_assembly_timing_fields(**kwargs):
    return _project_assembly_fields(**kwargs, schema=assembly_timing_schema(), validator=validate_assembly_timing)


def project_assembly_output(*, source_structure, source_text, mapping, configuration, max_bytes):
    """Project geometry without replacing the original encoding/disclosure policy."""
    base = validate_assembly_configuration(configuration, max_bytes=max_bytes)
    result = project_assembly_output_fields(source_structure=source_structure, source_text=source_text,
        mapping=mapping, max_bytes=max_bytes)
    output = result.pop("output")
    result["configuration"] = (validate_assembly_configuration({**base, **output}, max_bytes=max_bytes)
        if result["valid"] else None)
    return result
