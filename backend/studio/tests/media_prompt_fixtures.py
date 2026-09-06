"""Build test artifacts using the current compiler, never a retired model builder.

Fixture modes/ranges are explicit test data. Production never imports this file.
"""
from dataclasses import dataclass, field
from types import SimpleNamespace
from uuid import uuid4
import hashlib

from studio.models import PromptFieldDefinition, PromptProfileFieldRule, PromptProfileVersion
from studio.services.consistency import capture_consistency_snapshot, sync_consistency_locks
from studio.services.prompt_compiler_contract import PROMPT_COMPILER_VERSION, PromptBuildError
from studio.services.prompt_profile_compiler import MediaPromptCompilationContext, compile_media_prompt_profile
from studio.services.prompt_profiles import media_profile_sources, refresh_prompt_profile_contract
from studio.services.production_pipeline import _profiled_media_quality
from studio.services.reference_bindings import resolve_group_reference_bindings
from studio.tests.factories import conditioning_contract
from studio.tests.reference_rendering_fixtures import FRAME_POLICY, rendering_config


@dataclass
class MediaPromptOptions:
    model_identifier: str = "fixture-video-model"
    mode: str = "T2VA"
    include_audio: bool = True
    language: str = "Chinese"
    duration_overrides: dict = field(default_factory=dict)
    overall_soundscape: str = ""
    non_diegetic_music: str = ""
    extra_direction: str = ""
    single_shot_reference_bindings: dict | None = None
    single_shot_reference_assets: list | None = None
    consistency_snapshot: dict | None = None
    language_gate_mode: str = "warn"
    conditioning_mode_contract: dict | None = None
    frame_policy: dict | None = None


def fixture_mode_contract(mode):
    if mode == "Ref2VA":
        return conditioning_contract(mode, input_kind="classified_references", requires_bible=True,
            entity_images=(0, 9), reference_videos=(0, 3), paired_video_audios=(0, 3), reference_audios=(0, 3))["modes"][0]
    return conditioning_contract(mode, input_kind="text_only" if mode == "T2VA" else "frame_slots",
        first_frame=(0, int(mode in {"I2VA", "FL2VA"})), last_frame=(0, int(mode in {"L2VA", "FL2VA"})))["modes"][0]


def build_test_video_prompt(project, shots, options=None, *, resolved_reference_bindings=None):
    options = options or MediaPromptOptions()
    snapshot = options.consistency_snapshot
    if snapshot is None:
        sync_consistency_locks(project)
        snapshot = capture_consistency_snapshot(project)
    mode_contract = options.conditioning_mode_contract or fixture_mode_contract(options.mode)
    references = resolved_reference_bindings
    if references is None:
        references = resolve_group_reference_bindings(project=project, shots=shots, mode_contract=mode_contract,
            snapshot=snapshot, single_shot_bindings=options.single_shot_reference_bindings,
            single_shot_reference_assets=options.single_shot_reference_assets)
    return _compile(project, shots, options, snapshot, references, mode_contract, "video")


def build_test_image_prompt(project, shot, *, extra_direction="", consistency_snapshot=None, language_gate_mode="warn", **kwargs):
    options = MediaPromptOptions(model_identifier="fixture-image-model", include_audio=False,
        extra_direction=extra_direction, language_gate_mode=language_gate_mode)
    return _compile(project, [shot], options, consistency_snapshot or capture_consistency_snapshot(project), [], fixture_mode_contract("T2VA"), "image")


def build_test_negative_prompt(project, shots):
    return media_profile_sources(project=project, shots=shots)["request.negative_constraints"]


def _compile(project, shots, options, snapshot, references, mode_contract, prompt_type):
    config = rendering_config()
    profile = PromptProfileVersion.objects.create(profile_key="fixture." + uuid4().hex, version=1,
        name="Fixture reference renderer", model_family="fixture_model", operation=f"{prompt_type}.generate", mode=options.mode,
        source_projection_key="declarative_v1", source_projection_config={key: value for key, value in config.items() if key != "quality_advisories"},
        contract_schema_version=6)
    fields = ("subject_definitions", "summary", "retention_analysis", "detailed_description", "overall_soundscape", "non_diegetic_music")
    for index, key in enumerate(fields):
        definition, _ = PromptFieldDefinition.objects.get_or_create(key="fixture_" + key, defaults={"label": key,
            "category": "fixture", "description": "test field", "impact": "test prompt", "value_type": "long_text"})
        PromptProfileFieldRule.objects.create(profile_version=profile, field_definition=definition,
            source_selector="projection.output", projection_primitive="reference_context." + key,
            generation_mode="server_locked", generation_source_selector="projection.output",
            channel="positive_prompt", position=index + 1, label_style="label_colon", separator="newline", omit_if_empty=True,
            language_policy="any")
    refresh_prompt_profile_contract(profile)
    profile.status = "published"
    profile.save(update_fields=("status", "updated_at"))
    context = MediaPromptCompilationContext(project=project, shots=tuple(shots), prompt_type=prompt_type,
        mode=options.mode, include_audio=options.include_audio, duration_overrides=options.duration_overrides,
        overall_soundscape=options.overall_soundscape, non_diegetic_music=options.non_diegetic_music,
        extra_direction=options.extra_direction, reference_bindings=tuple(references), consistency_snapshot=snapshot,
        conditioning_mode_contract=mode_contract, frame_policy=options.frame_policy or FRAME_POLICY,
        model_identifier=options.model_identifier, language_gate_mode=options.language_gate_mode)
    compiled = compile_media_prompt_profile(profile, context=context)
    text = compiled.assembly["channels"].get("positive_prompt", "")
    negative = build_test_negative_prompt(project, shots)
    quality = _profiled_media_quality(project=project, prompt_type=prompt_type, prompt_text=text,
        negative_prompt=negative, assembly=compiled.assembly, language_gate_mode=options.language_gate_mode)
    if options.language_gate_mode == "block" and quality["production_language_gate"]["would_block_enqueue"]:
        raise PromptBuildError("Director prose requires reviewed prompt.normalize translation")
    duration = sum(options.duration_overrides.get(str(shot.id), shot.duration_ms) for shot in shots)
    payload = {"model": options.model_identifier, "prompt_compiler_version": PROMPT_COMPILER_VERSION,
        "mode": options.mode, **compiled.execution_facts, "duration_ms": duration,
        "shot_durations_ms": [options.duration_overrides.get(str(shot.id), shot.duration_ms) for shot in shots],
        "fields": compiled.structured_fields, "quality": quality, "reference_bindings": references, "reference_assets": references}
    return SimpleNamespace(text=text, payload=payload, consistency_snapshot=snapshot,
        total_duration_ms=duration, content_hash=hashlib.sha256(text.encode()).hexdigest(), source_snapshot={})
