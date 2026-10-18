"""Explicit renderer values shared by preparation and pure compilation.

The renderer consumes these values, not ORM objects. Preparation derives text
slots from exact Profile structure paths and authorized logical inputs; public
requests cannot inject these internal slots or construct fake business rows.
"""
from copy import deepcopy
from dataclasses import asdict, dataclass, fields
import json

from studio.services.prompt_compiler_contract import PromptBuildError
from studio.services.json_contracts import JsonContract, canonical_json_bytes, json_contract, verify_json_contract
from studio.services.reference_rendering_inputs import UNIT_RENDER_SLOTS
from studio.services.source_projection_primitives import SOURCE_PROJECTION_PRIMITIVES
from studio.services.dialogue import DialogueContractError, validate_dialogue_language


@dataclass(frozen=True)
class ReferenceSubjectFacts:
    category: str
    identity: str
    description_parts: tuple[str, ...]


@dataclass(frozen=True)
class ReferenceUnitFacts:
    unit_key: str
    duration_ms: int
    render_values: dict
    subject_keys: tuple[tuple[str, str], ...]
    dialogue: tuple[dict, ...]
    voice_profiles: tuple[str, ...]
    continuity_notes: str
    requires_first_frame_continuity: bool
    requires_last_frame_continuity: bool
    ambient_sound: str


@dataclass(frozen=True)
class ReferenceContextFacts:
    prompt_type: str
    mode: str
    include_audio: bool
    language: str
    ordered_units: tuple[ReferenceUnitFacts, ...]
    subject_facts: tuple[ReferenceSubjectFacts, ...]
    reference_bindings: tuple[dict, ...]
    conditioning_mode_contract: dict | None
    frame_policy: dict | None
    color_script: str
    extra_direction: str
    overall_soundscape: str
    non_diegetic_music: str
    generation_contributions: dict[tuple[str, str], str]


REFERENCE_CONTRIBUTION_TARGETS = frozenset(
    target for primitive in SOURCE_PROJECTION_PRIMITIVES.values()
    for target in primitive.contribution_source_selectors
)

DIALOGUE_REQUIRED_FACTS = frozenset({
    "speaker_id", "text", "delivery", "subject", "voice_profile", "voiceover", "start_ms", "end_ms",
})


def speaker_subject_key_schema():
    """Optional exact identity; omission means no declared reference association."""
    return {"type": "array", "title": "发言人对应主体（类型、确切标识）", "minItems": 2, "maxItems": 2,
            "items": {"type": "string", "minLength": 1}}


def validate_reference_context_facts(context):
    if not isinstance(context, ReferenceContextFacts):
        raise PromptBuildError("参考组装器只接收明确的事实值对象，不接收项目或镜头对象")
    if type(context.include_audio) is not bool or any(not isinstance(getattr(context, key), str) for key in (
        "prompt_type", "mode", "language", "color_script", "extra_direction", "overall_soundscape", "non_diegetic_music",
    )):
        raise PromptBuildError("参考上下文的文本和音频开关类型不正确")
    if any(not isinstance(getattr(context, key), (list, tuple)) for key in ("ordered_units", "subject_facts", "reference_bindings")):
        raise PromptBuildError("参考上下文的单元、主体和素材必须是明确的有序集合")
    if any(not isinstance(row, dict) for row in context.reference_bindings):
        raise PromptBuildError("参考素材必须是已解析事实对象")
    if any(value is not None and not isinstance(value, dict) for value in (context.conditioning_mode_contract, context.frame_policy)):
        raise PromptBuildError("数量和时钟合同必须为空或明确对象")
    units, subjects = set(), set()
    speaker_subjects, subject_speakers = {}, {}
    for subject in context.subject_facts:
        if not isinstance(subject, ReferenceSubjectFacts) or any(not isinstance(value, str) or not value for value in (subject.category, subject.identity)):
            raise PromptBuildError("参考主体必须有明确类型和身份")
        key = (subject.category, subject.identity)
        if key in subjects or not isinstance(subject.description_parts, (list, tuple)) or any(not isinstance(value, str) for value in subject.description_parts):
            raise PromptBuildError("参考主体重复或描述类型不正确")
        subjects.add(key)
    for unit in context.ordered_units:
        if not isinstance(unit, ReferenceUnitFacts) or not isinstance(unit.unit_key, str) or not unit.unit_key or unit.unit_key in units:
            raise PromptBuildError("请求单元必须有唯一的明确 key")
        units.add(unit.unit_key)
        if type(unit.duration_ms) is not int or unit.duration_ms <= 0:
            raise PromptBuildError("请求单元必须有明确的正整数时长")
        if (not isinstance(unit.render_values, dict) or set(unit.render_values) != set(UNIT_RENDER_SLOTS)
                or any(not isinstance(value, str) for value in unit.render_values.values())):
            raise PromptBuildError("请求单元缺少确切映射后的渲染值，不接受额外字段或补齐缺失事实")
        if not isinstance(unit.subject_keys, (list, tuple)) or any(not isinstance(key, tuple) or len(key) != 2 or any(not isinstance(value, str) for value in key) for key in unit.subject_keys):
            raise PromptBuildError("请求单元的主体引用格式无效")
        if len(unit.subject_keys) != len(set(unit.subject_keys)) or any(key not in subjects for key in unit.subject_keys):
            raise PromptBuildError("请求单元引用了未声明的主体")
        if any(type(value) is not bool for value in (unit.requires_first_frame_continuity, unit.requires_last_frame_continuity)):
            raise PromptBuildError("首尾连续性必须是明确布尔值")
        if not isinstance(unit.voice_profiles, (list, tuple)) or any(not isinstance(value, str) for value in (*unit.voice_profiles, unit.continuity_notes, unit.ambient_sound)):
            raise PromptBuildError("声音与连续性事实必须是文本")
        if not isinstance(unit.dialogue, (list, tuple)):
            raise PromptBuildError("对白必须是明确的有序集合")
        if not context.include_audio and unit.dialogue:
            raise PromptBuildError("当前请求不输出音频，不能静默丢弃传入的对白")
        for entry in unit.dialogue:
            if (not isinstance(entry, dict) or not DIALOGUE_REQUIRED_FACTS <= set(entry)
                    or set(entry) - DIALOGUE_REQUIRED_FACTS - {"speaker_subject_key", "language"}
                    or any(not isinstance(entry.get(key), str) for key in ("speaker_id", "text", "delivery", "subject", "voice_profile"))):
                raise PromptBuildError("对白必须明确提供说话人、内容和来源说明")
            if "language" in entry:
                try:
                    validate_dialogue_language(entry["language"])
                except DialogueContractError as exc:
                    raise PromptBuildError(str(exc)) from exc
            if type(entry.get("voiceover")) is not bool or any(type(entry.get(key)) is not int for key in ("start_ms", "end_ms")):
                raise PromptBuildError("对白必须明确声明时间和画外音类型")
            if not 0 <= entry["start_ms"] < entry["end_ms"] <= unit.duration_ms:
                raise PromptBuildError("对白时间超出请求单元范围")
            if "speaker_subject_key" in entry:
                key = entry["speaker_subject_key"]
                if (not isinstance(key, (list, tuple)) or len(key) != 2
                        or any(not isinstance(part, str) or not part.strip() for part in key)
                        or not entry["speaker_id"].strip()):
                    raise PromptBuildError("发言人主体关联必须包含类型和确切标识，不能按姓名或声音编号猜测")
                key = tuple(key)
                speaker_id = entry["speaker_id"]
                if (speaker_id in speaker_subjects and speaker_subjects[speaker_id] != key
                        or key in subject_speakers and subject_speakers[key] != speaker_id):
                    raise PromptBuildError("同一次请求的说话人标识与主体身份关联冲突")
                speaker_subjects[speaker_id] = key
                subject_speakers[key] = speaker_id
    if not units and context.prompt_type != "image":
        raise PromptBuildError("参考上下文至少需要一个请求单元")
    if not units and (context.include_audio or context.color_script or context.extra_direction
            or context.overall_soundscape or context.non_diegetic_music):
        raise PromptBuildError("无单元图片参考不能携带时间线或声音事实")
    for binding in context.reference_bindings:
        if "unit_keys" in binding:
            members = binding["unit_keys"]
            if (not isinstance(members, (list, tuple)) or not members
                    or any(not isinstance(key, str) or key not in units for key in members)
                    or len(set(members)) != len(members)):
                raise PromptBuildError("参考素材范围必须指向本次明确且唯一的单元")
        if binding.get("entity_id") and "entity_category" in binding:
            if (binding["entity_category"], str(binding["entity_id"])) not in subjects:
                raise PromptBuildError("参考素材的主体类型与实际主体事实不一致")
    if not isinstance(context.generation_contributions, dict) or any(
        not isinstance(key, tuple) or len(key) != 2 or key[0] not in units
        or key[1] not in REFERENCE_CONTRIBUTION_TARGETS or not isinstance(value, str)
        for key, value in context.generation_contributions.items()
    ):
        raise PromptBuildError("审核贡献必须引用当前请求单元的受控来源字段")


def freeze_reference_context_facts(context, *, max_bytes):
    """Freeze resolved sources for preparation, not its later field approvals.

    Callers resolve ownership, model/Workflow facts and media slots first. This
    codec is not a public endpoint that trusts arbitrary reference bindings.
    Selected generated contributions have their own explicit choice records.
    """
    validate_reference_context_facts(context)
    if context.generation_contributions:
        raise PromptBuildError("参考来源快照不能携带已批准贡献；贡献必须由显式字段选择提供")
    payload = {"schema_version": 1, "context": asdict(context)}
    json_contract(payload, max_bytes=max_bytes)
    # Persisted JSON has arrays rather than tuples. Exercise that exact shape
    # before freezing, rather than depending on in-memory dataclass objects.
    frozen = json_contract(json.loads(canonical_json_bytes(payload)), max_bytes=max_bytes)
    read_reference_context_facts(frozen, max_bytes=max_bytes)
    return frozen


def read_reference_context_facts(contract, *, max_bytes):
    """Reconstruct exact frozen source values, without ORM/current/default reads."""
    if not isinstance(contract, JsonContract) or type(contract.schema_version) is not int or contract.schema_version != 1:
        raise PromptBuildError("参考事实需要当前完整快照及摘要")
    verify_json_contract(contract.value, schema_version=contract.schema_version, size_bytes=contract.size_bytes,
                         sha256=contract.sha256, max_bytes=max_bytes)
    payload = deepcopy(contract.value)
    if not isinstance(payload, dict) or set(payload) != {"schema_version", "context"} or type(payload["schema_version"]) is not int or payload["schema_version"] != 1:
        raise PromptBuildError("参考事实快照结构或版本无效")
    value = payload["context"]
    if not isinstance(value, dict) or set(value) != {item.name for item in fields(ReferenceContextFacts)}:
        raise PromptBuildError("参考事实缺少字段或包含额外内容，不能用空值补齐")
    if any(not isinstance(value[key], list) for key in ("ordered_units", "subject_facts", "reference_bindings")) or value["generation_contributions"] != {}:
        raise PromptBuildError("参考来源必须为明确有序事实，不能夹带字段批准结果")
    units, subjects = [], []
    for row in value["ordered_units"]:
        if not isinstance(row, dict) or set(row) != {item.name for item in fields(ReferenceUnitFacts)}:
            raise PromptBuildError("请求单元快照字段不完整")
        if any(not isinstance(row[key], list) for key in ("subject_keys", "dialogue", "voice_profiles")):
            raise PromptBuildError("请求单元集合字段必须保持有序")
        if any(not isinstance(key, list) or len(key) != 2 or any(not isinstance(part, str) for part in key) for key in row["subject_keys"]):
            raise PromptBuildError("单元主体必须引用明确的类型和身份")
        units.append(ReferenceUnitFacts(**{**row, "subject_keys": tuple(tuple(key) for key in row["subject_keys"]),
                                          "dialogue": tuple(row["dialogue"]), "voice_profiles": tuple(row["voice_profiles"])}))
    for row in value["subject_facts"]:
        if not isinstance(row, dict) or set(row) != {item.name for item in fields(ReferenceSubjectFacts)} or not isinstance(row["description_parts"], list):
            raise PromptBuildError("主体快照必须具有完整身份和有序描述")
        subjects.append(ReferenceSubjectFacts(**{**row, "description_parts": tuple(row["description_parts"])}))
    context = ReferenceContextFacts(**{**value, "ordered_units": tuple(units), "subject_facts": tuple(subjects),
                                      "reference_bindings": tuple(value["reference_bindings"])})
    validate_reference_context_facts(context)
    return context
