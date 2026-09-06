from copy import deepcopy

from studio.tests.reference_rendering_fixtures import LEGACY_H3_SELECTOR_TO_PRIMITIVE
from uuid import uuid4

from django.contrib.auth import get_user_model
from django.utils import timezone

from studio.models import (
    Asset,
    CharacterBible,
    Episode,
    GenerationTask,
    GeneratedWorkflow,
    ModelDeployment,
    ModelDeploymentCapabilitySnapshot,
    ModelProfileVersion,
    ProviderProfile,
    RuntimeHost,
    SceneBible,
    SeriesProject,
    Shot,
    Style,
    WorkflowTemplateVersion,
    PromptFieldDefinition,
    PromptProfileBinding,
    PromptProfileFieldRule,
    PromptProfileVersion,
)
from studio.services.deployment_smoke import record_deployment_capability_snapshot
from studio.services.json_contracts import assign_json_contract, json_contract
from studio.services.prompt_normalization_contract import PROMPT_NORMALIZE_OPERATION
from studio.services.prompt_profiles import (
    DECLARATIVE_SOURCE_PROJECTION_KEY,
    DIRECT_SOURCE_PROJECTION_KEY,
    PROMPT_PROFILE_CONTRACT_SCHEMA_VERSION,
    model_family_for_profile,
    publish_prompt_profile,
    refresh_prompt_profile_contract,
)
from studio.services.source_projection_primitives import (
    REFERENCE_CONTEXT_ENGINE_KEY,
)


# Canonical v15 example data.  This belongs to tests/seed fixtures rather than
# the projection adapter: production field names and rendering choices come
# from each versioned PromptProfileFieldRule row.
H3_REF2VA_OFFICIAL_RULE_SPECS = (
    (
        PromptProfileFieldRule.SourceSelector.H3_REF2VA_SUBJECT_DEFINITIONS,
        "subject_definitions",
        PromptProfileFieldRule.LabelStyle.LABEL_BLOCK,
    ),
    (
        PromptProfileFieldRule.SourceSelector.H3_REF2VA_SUMMARY,
        "summary",
        PromptProfileFieldRule.LabelStyle.LABEL_COLON,
    ),
    (
        PromptProfileFieldRule.SourceSelector.H3_REF2VA_RETENTION_ANALYSIS,
        "retention_analysis",
        PromptProfileFieldRule.LabelStyle.LABEL_BLOCK,
    ),
    (
        PromptProfileFieldRule.SourceSelector.H3_REF2VA_DETAILED_DESCRIPTION,
        "detailed_description",
        PromptProfileFieldRule.LabelStyle.LABEL_BLOCK,
    ),
    (
        PromptProfileFieldRule.SourceSelector.H3_REF2VA_OVERALL_SOUNDSCAPE,
        "overall_soundscape",
        PromptProfileFieldRule.LabelStyle.LABEL_COLON,
    ),
    (
        PromptProfileFieldRule.SourceSelector.H3_REF2VA_NON_DIEGETIC_MUSIC,
        "non_diegetic_music",
        PromptProfileFieldRule.LabelStyle.LABEL_COLON,
    ),
)


def series_concept_field_contract() -> dict:
    return deepcopy({
        "schema_version": 1,
        "idea": {"max_chars": 6_000, "required": True, "single_line": False},
        "style_hint": {"max_chars": 2_000, "required": False, "single_line": False},
        "title": {"max_chars": 60, "required": True, "single_line": True},
        "logline": {"max_chars": 500, "required": True, "single_line": True},
        "bible.entity.name": {"max_chars": 160, "required": True, "single_line": True},
        "bible.entity.description": {"max_chars": 2_000, "required": False, "single_line": True},
        "bible.entity.visual_identity_en": {"max_chars": 3_000, "required": False, "single_line": True},
        "episode.title": {"max_chars": 80, "required": True, "single_line": True},
        "shot.setting": {"max_chars": 200, "required": True, "single_line": True},
        "shot.action": {"max_chars": 500, "required": True, "single_line": True},
        "shot.dialogue.speaker": {"max_chars": 80, "required": False, "single_line": True},
        "shot.dialogue.delivery": {"max_chars": 120, "required": False, "single_line": True},
        "shot.dialogue.text": {"max_chars": 240, "required": False, "single_line": True},
    })


def series_concept_resource_advisory() -> dict:
    return {
        "schema_version": 1,
        "input_protocol_overhead_chars": 2_048,
        "output_base_units": 512,
        "output_units_per_shot": 200,
        "minimum_output_units": 1_024,
        "output_hint_warning_percent": 80,
        "script_breakdown_reference_chars": 1_000_000,
    }


def script_structure_field_contract() -> dict:
    return deepcopy({
        "schema_version": 1,
        "title": {"max_chars": 200, "required": True, "single_line": True},
        "logline": {"max_chars": 2_000, "required": True, "single_line": True},
        "bible.entity.name": {"max_chars": 160, "required": True, "single_line": True},
        "bible.entity.description": {"max_chars": 2_000, "required": False, "single_line": True},
        "bible.entity.visual_identity_en": {"max_chars": 3_000, "required": False, "single_line": True},
        "episode.title": {"max_chars": 200, "required": True, "single_line": True},
        "shot.setting": {"max_chars": 500, "required": True, "single_line": True},
        "shot.action": {"max_chars": 4_000, "required": True, "single_line": True},
        "shot.dialogue.speaker": {"max_chars": 160, "required": False, "single_line": True},
        "shot.dialogue.delivery": {"max_chars": 240, "required": False, "single_line": True},
        "shot.dialogue.text": {"max_chars": 2_000, "required": False, "single_line": True},
    })


def conditioning_contract(
    mode: str,
    *,
    input_kind: str = "text_only",
    requires_bible: bool = False,
    first_frame: tuple[int, int] = (0, 0),
    last_frame: tuple[int, int] = (0, 0),
    entity_images: tuple[int, int] = (0, 0),
    reference_videos: tuple[int, int] = (0, 0),
    paired_video_audios: tuple[int, int] = (0, 0),
    reference_audios: tuple[int, int] = (0, 0),
    video_purposes: tuple[str, ...] = (
        "motion_camera_reference", "video_editing", "video_continuation",
    ),
    audio_purposes: tuple[str, ...] = ("audio_reference", "audio_reuse"),
    video_preprocessing_adapter: str = "cfr_video_v1",
    video_target_fps: int | None = 24,
    video_requires_source_fps: bool = True,
    paired_audio_preprocessing_adapter: str = "aligned_audio_v1",
    reference_audio_preprocessing_adapter: str = "passthrough_audio_v1",
) -> dict:
    slot = lambda bounds: {
        "minimum_items": bounds[0],
        "maximum_items": bounds[1],
    }
    total_max = first_frame[1] + last_frame[1] + entity_images[1]
    total_min = min(total_max, first_frame[0] + last_frame[0] + entity_images[0])
    mode_contract = {
        "key": mode,
        "label": mode,
        "description": "test conditioning mode",
        "input_kind": input_kind,
        "requires_entity_reference_bible": requires_bible,
        "supports_native_audio": True,
        "inputs": {
            "total_images": slot((total_min, total_max)),
            "first_frame": slot(first_frame),
            "last_frame": slot(last_frame),
            "entity_images": slot(entity_images),
            "reference_videos": slot(reference_videos),
            "paired_video_audios": slot(paired_video_audios),
            "reference_audios": slot(reference_audios),
        },
    }
    if max(reference_videos[1], paired_video_audios[1], reference_audios[1]) > 0:
        audio_purpose_maximum = max(paired_video_audios[1], reference_audios[1])
        mode_contract["dynamic_reference_topology"] = {
            "schema_version": 1,
            "videos": {
                **slot(reference_videos),
                "default_purpose_key": video_purposes[0] if reference_videos[1] else None,
                "purposes": [{
                    "key": key,
                    "label": key.replace("_", " ").title(),
                    "description": f"Test video purpose {key}",
                    **slot((0, reference_videos[1])),
                } for key in video_purposes] if reference_videos[1] else [],
            },
            "audios": {
                **slot(reference_audios),
                "default_purpose_key": audio_purposes[0] if audio_purpose_maximum else None,
                "purposes": [{
                    "key": key,
                    "label": key.replace("_", " ").title(),
                    "description": f"Test audio purpose {key}",
                    **slot((0, audio_purpose_maximum)),
                } for key in audio_purposes] if audio_purpose_maximum else [],
            },
            "preprocessing": {
                "videos": ({
                    "adapter_key": video_preprocessing_adapter,
                    "minimum_duration_ms": 2_000,
                    "maximum_duration_ms": 15_000,
                    "target_fps": video_target_fps,
                    "requires_source_fps": video_requires_source_fps,
                } if reference_videos[1] else None),
                "paired_video_audios": ({
                    "adapter_key": paired_audio_preprocessing_adapter,
                    "minimum_duration_ms": 2_000,
                    "maximum_duration_ms": 15_000,
                } if paired_video_audios[1] else None),
                "reference_audios": ({
                    "adapter_key": reference_audio_preprocessing_adapter,
                    "minimum_duration_ms": 2_000,
                    "maximum_duration_ms": 15_000,
                } if reference_audios[1] else None),
            },
            "pairing_rules": ([{
                "key": "video_audio_pairing",
                "label": "Video audio pairing",
                "description": "Test configurable video to audio pairing",
                "source_media_kind": "video",
                "target_media_kind": "audio",
                "source_purpose_keys": list(video_purposes),
                "target_purpose_keys": list(audio_purposes),
                "default_target_purpose_key": audio_purposes[0],
                "minimum_targets_per_source": 0,
                "maximum_targets_per_source": 1,
                "allow_embedded_source": True,
                "allow_external_target": True,
                "source_and_external_mutually_exclusive": True,
            }] if paired_video_audios[1] else []),
            "mutual_exclusion_rules": ([{
                "key": "editing_or_continuation",
                "label": "Editing or continuation",
                "description": "Do not mix editing and continuation in one binding set",
                "media_kind": "video",
                "purpose_keys": ["video_editing", "video_continuation"],
                "maximum_distinct_purposes": 1,
            }] if {"video_editing", "video_continuation"}.issubset(video_purposes) else []),
        }
    return {
        "schema_version": 1,
        "adapter_key": "media_conditioning_v1",
        "modes": [mode_contract],
    }


def h3_text_to_video_workflow_graph() -> dict:
    """Build a test-only H3 graph through the current workflow fact factory."""

    return {
        "1": {"class_type": "UNETLoader", "inputs": {
            "unet_name": "MiniMax_H3_FL2VA_pruned_int8_convrot.safetensors",
            "weight_dtype": "default",
        }},
        "2": {"class_type": "CLIPLoader", "inputs": {
            "clip_name": "qwen3vl_32b_minimax_h3_int8_convrot.safetensors",
            "type": "minimax", "device": "default",
        }},
        "3": {"class_type": "VAELoader", "inputs": {
            "vae_name": "minimax_h3_video_vae_fp16.safetensors",
        }},
        "4": {"class_type": "VAELoader", "inputs": {
            "vae_name": "minimax_h3_audio_vae_fp32.safetensors",
        }},
        "5": {"class_type": "MiniMaxH3ImageToVideo", "inputs": {
            "clip": ["2", 0], "vae": ["3", 0],
            "prompt": "${PROMPT}", "width": "${WIDTH}",
            "height": "${HEIGHT}", "length": "${FRAMES}",
        }},
        "6": {"class_type": "RandomNoise", "inputs": {"noise_seed": "${SEED}"}},
        "7": {"class_type": "BasicGuider", "inputs": {
            "model": ["1", 0], "conditioning": ["5", 0],
        }},
        "8": {"class_type": "KSamplerSelect", "inputs": {
            "sampler_name": "res_multistep",
        }},
        "9": {"class_type": "BasicScheduler", "inputs": {
            "model": ["1", 0], "scheduler": "simple", "steps": 20, "denoise": 1.0,
        }},
        "10": {"class_type": "SamplerCustomAdvanced", "inputs": {
            "noise": ["6", 0], "guider": ["7", 0], "sampler": ["8", 0],
            "sigmas": ["9", 0], "latent_image": ["5", 1],
        }},
        "11": {"class_type": "VAEDecode", "inputs": {
            "samples": ["10", 0], "vae": ["3", 0],
        }},
        "12": {"class_type": "VAEDecodeAudio", "inputs": {
            "samples": ["10", 0], "vae": ["4", 0],
        }},
        "13": {"class_type": "CreateVideo", "inputs": {
            "images": ["11", 0], "fps": 24.0, "audio": ["12", 0], "bit_depth": 8,
        }},
        "14": {"class_type": "SaveVideo", "inputs": {
            "video": ["13", 0], "filename_prefix": "${OUTPUT_PREFIX}",
            "format": "auto", "codec": "auto",
        }},
    }


def h3_dual_clock_ref2va_workflow_graph(*, low_vram: bool = True) -> dict:
    """Build the v2/v3 Ref2VA topology as mutable test input."""

    graph = {
        "1": {"class_type": "UNETLoader", "inputs": {
            "unet_name": "MiniMax_H3_Ref2VA_pruned_int8_convrot.safetensors",
            "weight_dtype": "default",
        }},
        "2": {"class_type": "LoraLoaderBypassModelOnly", "inputs": {
            "model": ["1", 0],
            "lora_name": (
                "MiniMax-H3-Turbo-Lora-Pruned-ComfyUI/"
                "minimax_h3_turbo_4step_ckpt600_ema_V4.safetensors"
            ),
            "strength_model": "${LORA_STRENGTH}",
        }},
        "3": {"class_type": "CLIPLoader", "inputs": {
            "clip_name": "qwen3vl_32b_minimax_h3_int8_convrot.safetensors",
            "type": "minimax", "device": "default",
        }},
        "4": {"class_type": "VAELoader", "inputs": {
            "vae_name": "minimax_h3_video_vae_fp16.safetensors",
        }},
        "5": {"class_type": "VAELoader", "inputs": {
            "vae_name": "minimax_h3_audio_vae_fp32.safetensors",
        }},
        "6": {"class_type": "MiniMaxH3AudioConditioningT8", "inputs": {
            "clip": ["3", 0], "video_vae": ["4", 0], "audio_vae": ["5", 0],
            "prompt": "${PROMPT}", "width": "${WIDTH}", "height": "${HEIGHT}",
            "length": "${FRAMES}", "task_type": "Ref2VA", "audio_mode": "native",
            "audio_denoise_strength": 1.0, "add_source_as_reference": False,
            "prompt_primary_audio_ordinal": 0, "strict_prompt_tags": True,
            "ref_image_size": "match", "reference_video_policy": "official_2_to_15s",
        }},
        "7": {"class_type": "MiniMaxH3DualClockSamplerT8", "inputs": {
            "model": ["14" if low_vram else "2", 0],
            "av_latent": ["6", 1], "steps": "${STEPS}",
            "shift_video": 12.0, "shift_audio": 3.0,
        }},
        "8": {"class_type": "BasicGuider", "inputs": {
            "model": ["7", 0], "conditioning": ["6", 0],
        }},
        "9": {"class_type": "RandomNoise", "inputs": {"noise_seed": "${SEED}"}},
        "10": {"class_type": "SamplerCustomAdvanced", "inputs": {
            "noise": ["9", 0], "guider": ["8", 0], "sampler": ["7", 1],
            "sigmas": ["7", 2], "latent_image": ["6", 1],
        }},
        "11": {"class_type": "MiniMaxH3AVDecodeT8", "inputs": {
            "av_latent": ["10", 0], "video_vae": ["4", 0], "audio_vae": ["5", 0],
        }},
        "12": {"class_type": "CreateVideo", "inputs": {
            "images": ["11", 0], "fps": 24.0, "audio": ["11", 1], "bit_depth": 8,
        }},
        "13": {"class_type": "SaveVideo", "inputs": {
            "video": ["12", 0], "filename_prefix": "${OUTPUT_PREFIX}",
            "format": "auto", "codec": "auto",
        }},
    }
    if low_vram:
        graph["14"] = {
            "class_type": "MiniMaxLowVRAMAttention",
            "inputs": {"model": ["2", 0], "head_chunks": 4},
        }
    return graph


def workflow_primary_output(graph: dict, *, kind: str) -> dict:
    """Derive the current ``input_contract.outputs`` test fact from a graph."""

    node_types = {
        "image": {"SaveImage", "SaveImageAdvanced", "PreviewImage"},
        "video": {"SaveVideo", "VHS_VideoCombine"},
        "audio": {"SaveAudio", "SaveAudioAdvanced", "SaveAudioMP3", "SaveAudioOpus"},
    }
    candidates = [
        str(node_id)
        for node_id, node in graph.items()
        if isinstance(node, dict) and node.get("class_type") in node_types.get(kind, set())
    ]
    if len(candidates) != 1:
        raise ValueError(f"test graph must declare exactly one terminal {kind} output")
    return {
        "primary": {"node_id": candidates[0], "kind": kind, "required": True},
    }


def create_test_prompt_profile_binding(
    *,
    actor,
    model_profile_version,
    operation: str,
    mode: str = "",
) -> PromptProfileBinding:
    """Create one explicit reviewed PromptProfile for a production test route."""

    family = model_family_for_profile(model_profile_version)
    suffix = f"{model_profile_version.id.hex[:12]}-{operation.replace('.', '-')}-{mode or 'default'}"
    from studio.tests.reference_rendering_fixtures import rendering_config

    profile = PromptProfileVersion.objects.create(
        profile_key=f"test.{suffix}"[:191],
        version=1,
        name=f"Test {operation} {mode or 'default'}",
        model_family=family,
        operation=operation,
        mode=mode,
        source_projection_key=(
            DECLARATIVE_SOURCE_PROJECTION_KEY
            if operation == "video.generate" and mode == "Ref2VA"
            else DIRECT_SOURCE_PROJECTION_KEY
        ),
        source_projection_config=(
            {
                "schema_version": 5,
                "primitive_options": {REFERENCE_CONTEXT_ENGINE_KEY: {
                    "rendering": rendering_config()["primitive_options"]["reference_context_ir_v1"]["rendering"],
                }},
            }
            if operation == "video.generate" and mode == "Ref2VA"
            else {}
        ),
        review_state=PromptProfileVersion.ReviewState.READY,
        contract_schema_version=PROMPT_PROFILE_CONTRACT_SCHEMA_VERSION,
    )
    if operation == "video.generate" and mode == "Ref2VA":
        generated_sources = {
            "detailed_description": (
                "shot.detailed_description", 140, 220,
                "Expand only the frozen visible shot facts in chronological order.",
            ),
            "overall_soundscape": (
                "shot.sound_effects", 1, 80,
                "Describe only supported diegetic ambience and synchronized sounds.",
            ),
            "non_diegetic_music": (
                "shot.music", 1, 60,
                "Describe only supported non-diegetic music and silence.",
            ),
        }
        for position, (selector, field_key, label_style) in enumerate(
            H3_REF2VA_OFFICIAL_RULE_SPECS,
            start=1,
        ):
            definition, _ = PromptFieldDefinition.objects.get_or_create(
                key=field_key,
                defaults={
                    "label": field_key,
                    "category": "test_h3_ref2va",
                    "description": "Official H3 Ref2VA Context-IR test field.",
                    "impact": "Affects the frozen H3 Ref2VA model prompt.",
                    "value_type": PromptFieldDefinition.ValueType.LONG_TEXT,
                },
            )
            PromptProfileFieldRule.objects.create(
                profile_version=profile,
                field_definition=definition,
                source_selector=PromptProfileFieldRule.SourceSelector.PROJECTION_OUTPUT,
                projection_primitive=LEGACY_H3_SELECTOR_TO_PRIMITIVE[selector],
                channel=PromptProfileFieldRule.Channel.POSITIVE_PROMPT,
                position=position,
                label_style=label_style,
                separator=PromptProfileFieldRule.Separator.NEWLINE,
                required=True,
                omit_if_empty=False,
                max_chars=7000,
                generation_mode=(
                    PromptProfileFieldRule.GenerationMode.MODEL_GENERATED
                    if field_key in generated_sources
                    else PromptProfileFieldRule.GenerationMode.SERVER_LOCKED
                ),
                generation_source_selector=(
                    generated_sources[field_key][0]
                    if field_key in generated_sources
                    else PromptProfileFieldRule.SourceSelector.PROJECTION_OUTPUT
                ),
                generation_instruction=(
                    generated_sources[field_key][3]
                    if field_key in generated_sources else ""
                ),
                generation_output_semantics=(
                    PromptProfileFieldRule.GenerationOutputSemantics.CONTRIBUTION
                    if field_key in generated_sources
                    else PromptProfileFieldRule.GenerationOutputSemantics.FINAL
                ),
                generation_required=True,
                language_policy="en" if field_key in generated_sources else "any",
                length_unit=(
                    PromptProfileFieldRule.LengthUnit.ENGLISH_WORDS
                    if field_key in generated_sources
                    else PromptProfileFieldRule.LengthUnit.NONE
                ),
                generation_min=(
                    generated_sources[field_key][1]
                    if field_key in generated_sources else None
                ),
                generation_max=(
                    generated_sources[field_key][2]
                    if field_key in generated_sources else None
                ),
                generation_validator_keys=(
                    [
                        PromptProfileFieldRule.GenerationValidator.NON_EMPTY,
                        PromptProfileFieldRule.GenerationValidator.NO_CONTROL_CHARACTERS,
                        PromptProfileFieldRule.GenerationValidator.NO_UNRESOLVED_PLACEHOLDERS,
                    ]
                    if field_key in generated_sources else []
                ),
                generation_context_selectors=(
                    [
                        PromptProfileFieldRule.GenerationContextSelector.PROJECT_STYLE,
                        PromptProfileFieldRule.GenerationContextSelector.TARGET_SHOT_CORE,
                        PromptProfileFieldRule.GenerationContextSelector.TARGET_SHOT_DIALOGUE_METADATA,
                        PromptProfileFieldRule.GenerationContextSelector.TARGET_SHOT_LINKED_BIBLE,
                    ]
                    if field_key in generated_sources else []
                ),
            )
    else:
        definition = PromptFieldDefinition.objects.create(
            key=f"test_policy_{suffix}"[:191],
            label="Reviewed test policy",
            category="test",
            description="Explicit test-only policy required by the production PromptProfile gate.",
            impact="Affects the frozen model request in this test only.",
            value_type=PromptFieldDefinition.ValueType.LONG_TEXT,
            default_value="Use the strict fixture contract without adding unsupported facts.",
        )
        PromptProfileFieldRule.objects.create(
            profile_version=profile,
            field_definition=definition,
            source_selector=PromptProfileFieldRule.SourceSelector.DEFINITION_DEFAULT,
            channel=(
                PromptProfileFieldRule.Channel.POSITIVE_PROMPT
                if operation in {"image.generate", "video.generate"}
                else PromptProfileFieldRule.Channel.SYSTEM_INSTRUCTION
            ),
            position=1,
            max_chars=4000,
            generation_mode=PromptProfileFieldRule.GenerationMode.PASSTHROUGH,
            generation_source_selector=(
                PromptProfileFieldRule.SourceSelector.DEFINITION_DEFAULT
            ),
        )
    refresh_prompt_profile_contract(
        profile,
        schema_version=PROMPT_PROFILE_CONTRACT_SCHEMA_VERSION,
    )
    profile = publish_prompt_profile(profile, actor=actor)
    return PromptProfileBinding.objects.create(
        key=f"test-binding-{suffix}"[:191],
        name=f"Test binding {operation} {mode or 'default'}",
        operation=operation,
        model_family=family,
        mode=mode,
        model_profile_version=model_profile_version,
        prompt_profile_version=profile,
        is_active=True,
    )


def create_ready_prompt_normalization_test_deployment() -> ModelDeployment:
    """Create one exact, selectable prompt.normalize route for API tests.

    Tests that exercise translation orchestration mock the actual model call, but
    their configuration still has to reference a real active deployment with a
    published capability contract and fresh smoke evidence.  Keeping that
    control-plane state realistic prevents a random UUID from bypassing the
    production resource-reference gate.
    """

    suffix = uuid4().hex[:12]
    host = RuntimeHost.objects.create(
        key=f"test-normalizer-host-{suffix}",
        name="Prompt normalization test host",
    )
    provider = ProviderProfile(
        key=f"test-normalizer-provider-{suffix}",
        name="Prompt normalization test provider",
        driver_type=ProviderProfile.DriverType.LOCAL_QWEN,
        modalities=ModelProfileVersion.Modality.TEXT,
        operations=PROMPT_NORMALIZE_OPERATION,
        is_active=True,
    )
    assign_json_contract(provider, "driver_config", {
        "runtime_source": "openai_compatible_endpoint",
        "base_url": "http://127.0.0.1:9123",
        "chat_path": "/v1/chat/completions",
        "timeout_seconds": 10,
        "capability_ttl_seconds": 3600,
    })
    provider.save()
    profile = ModelProfileVersion(
        profile_key=f"test-normalizer-profile-{suffix}",
        version=1,
        provider=provider,
        model_identifier=f"test-normalizer-{suffix}",
        modality=ModelProfileVersion.Modality.TEXT,
        status=ModelProfileVersion.Status.PUBLISHED,
        published_at=timezone.now(),
    )
    assign_json_contract(profile, "specification", {
        "schema_version": 1,
        "model_family": "test_prompt_normalizer",
        "capabilities": [PROMPT_NORMALIZE_OPERATION],
        "prompt_normalization": {
            "max_input_bytes": 29_000,
            "max_output_tokens": 8_192,
            "max_items_per_batch": 4,
            "timeout_seconds": 180.0,
            "chat_request_options": {
                "temperature": 0,
                "response_format": {"type": "json_object"},
            },
        },
    })
    profile.save()
    deployment = ModelDeployment(
        key=f"test-normalizer-{suffix}",
        name="Prompt normalization test deployment",
        profile_version=profile,
        runtime_host=host,
        status=ModelDeployment.Status.ACTIVE,
    )
    assign_json_contract(deployment, "runtime_config", {})
    deployment.save()
    from studio.services.deployment_smoke_profiles import select_smoke_profile, smoke_profile_reference
    actor = get_user_model().objects.filter(is_superuser=True).first()
    if actor is None:
        actor = get_user_model().objects.create(username=f"fixture-reviewer-{suffix}", is_superuser=True, is_staff=True)
    binding = create_test_prompt_profile_binding(actor=actor, model_profile_version=profile, operation=PROMPT_NORMALIZE_OPERATION)
    selected = select_smoke_profile(deployment, PROMPT_NORMALIZE_OPERATION, binding.id)
    record_deployment_capability_snapshot(
        deployment,
        probe_kind=ModelDeploymentCapabilitySnapshot.ProbeKind.SMOKE,
        status=ModelDeploymentCapabilitySnapshot.Status.PASSED,
        operations=[PROMPT_NORMALIZE_OPERATION],
        evidence={
            "schema_version": 1,
            "execution_submitted": True,
            "test_fixture": True,
            "prompt_profile": smoke_profile_reference(deployment, PROMPT_NORMALIZE_OPERATION, selected),
        },
    )
    return deployment


def freeze_current_task_contract(
    task: GenerationTask,
    *,
    resolved_semantics: dict | None = None,
    asset_bindings: list[dict] | None = None,
    continuity_assets: list[dict] | None = None,
    resolution_multiple: int = 1,
) -> GenerationTask:
    """Give a task the same immutable projection contract as current production."""

    workflow: GeneratedWorkflow = task.generated_workflow
    prompt = workflow.prompt_artifact
    template = workflow.template
    graph_contract = json_contract(workflow.workflow_json)
    input_contract = {
        "schema_version": 2,
        "compiler": {"id": "generic.comfy", "version": 1},
        "workflow_mode": "semantic",
        "required_dimension_multiple": int(resolution_multiple),
    }
    input_contract_json = json_contract(input_contract)
    version = workflow.workflow_template_version
    if version is None:
        execution_contract_sha256 = json_contract({
            "graph_sha256": graph_contract.sha256,
            "input_contract_sha256": input_contract_json.sha256,
        }).sha256
        version = WorkflowTemplateVersion.objects.create(
            template=template,
            version=max(
                template.platform_versions.values_list("version", flat=True),
                default=0,
            ) + 1,
            graph=deepcopy(workflow.workflow_json),
            graph_size_bytes=graph_contract.size_bytes,
            graph_sha256=graph_contract.sha256,
            input_contract=input_contract,
            input_contract_size_bytes=input_contract_json.size_bytes,
            input_contract_sha256=input_contract_json.sha256,
            compiler_id="generic.comfy",
            compiler_version=1,
            semantic_bindings_sha256=json_contract([]).sha256,
            model_requirements_sha256=json_contract([]).sha256,
            execution_contract_sha256=execution_contract_sha256,
        )
    execution_contract_sha256 = str(version.execution_contract_sha256 or "")
    if not execution_contract_sha256:
        execution_contract_sha256 = json_contract({
            "version_id": str(version.id),
            "graph_sha256": graph_contract.sha256,
        }).sha256
        version.execution_contract_sha256 = execution_contract_sha256
        version.save(update_fields=("execution_contract_sha256", "updated_at"))

    is_image = task.kind == GenerationTask.Kind.IMAGE
    width = int(task.project.width)
    height = int(task.project.height)
    duration_ms = int(
        task.effective_duration_ms
        or prompt.total_duration_ms
        or getattr(prompt.shot, "duration_ms", 0)
        or 1000
    )
    fps = int(task.project.fps or 24)
    frames = int(task.effective_frames or max(1, round(duration_ms * fps / 1000)))
    semantics = deepcopy(resolved_semantics) if resolved_semantics is not None else {
        "prompt.positive": str(prompt.prompt_text or "fixture prompt"),
        "image.width": width,
        "image.height": height,
        "sampling.seed": int(task.seed),
    }
    if prompt.negative_prompt and "prompt.negative" not in semantics:
        semantics["prompt.negative"] = str(prompt.negative_prompt)
    if not is_image:
        semantics.update({
            "video.fps": fps,
            "video.duration_ms": duration_ms,
            "video.frames": frames,
            "audio.enabled": bool(prompt.include_audio),
        })

    payload = prompt.structured_payload if isinstance(prompt.structured_payload, dict) else {}
    mode = (
        "TEXT_ONLY" if is_image else str(
            payload.get("effective_conditioning_mode")
            or prompt.conditioning_mode
            or "test_video"
        )
    )
    selected_mode = None if is_image else {"key": mode}
    conditioning = {
        "schema_version": 1,
        "modes": [] if is_image else [selected_mode],
    }
    monitoring = {"schema_version": 1, "stages": []}
    model_identifier = str(
        task.model_profile or prompt.model or template.model or "fixture-model"
    )
    execution_metadata = {
        "schema_version": 1,
        "workflow_version_id": str(version.id),
        "workflow_version": int(version.version),
        "template": {
            "id": str(template.id),
            "slug": template.slug,
            "name": template.name,
            "output_kind": task.kind,
        },
        "model_identifier": model_identifier,
        "workflow_mode": "semantic",
        "source_workflow": "",
        "required_dimension_multiple": int(resolution_multiple),
        "conditioning_contract": conditioning,
        "conditioning_contract_sha256": json_contract(conditioning).sha256,
        "conditioning_mode_contract": selected_mode,
        "conditioning_mode_contract_sha256": (
            json_contract(selected_mode).sha256 if selected_mode else ""
        ),
        "execution_monitoring": monitoring,
        "execution_monitoring_sha256": json_contract(monitoring).sha256,
    }
    references = deepcopy(asset_bindings or [])
    continuity = deepcopy(continuity_assets or [])
    model_requirements: list[dict] = []
    output_contract = {
        "schema_version": 1,
        "primary": {
            "node_id": str(next(iter(workflow.workflow_json))),
            "kind": task.kind,
            "required": True,
        },
    }
    semantics_sha256 = json_contract(semantics).sha256
    bindings_sha256 = json_contract(references).sha256
    requirements_sha256 = json_contract(model_requirements).sha256
    metadata_sha256 = json_contract(execution_metadata).sha256
    route_sha256 = str(workflow.route_snapshot_sha256 or json_contract({
        "fixture_route": str(workflow.id),
    }).sha256)

    workflow.workflow_template_version = version
    workflow.injected_values = {
        "schema_version": 2,
        "resolved_semantics": semantics,
        "asset_bindings": references,
        "model_requirements": model_requirements,
        "output_contract": output_contract,
        "execution_metadata": execution_metadata,
        "execution_metadata_sha256": metadata_sha256,
    }
    workflow.content_hash = graph_contract.sha256
    workflow.compiler_id = "generic.comfy"
    workflow.compiler_version = 1
    workflow.route_snapshot_sha256 = route_sha256
    workflow.workflow_contract_sha256 = execution_contract_sha256
    workflow.resolved_semantics_sha256 = semantics_sha256
    workflow.asset_bindings_sha256 = bindings_sha256
    workflow.model_requirements_sha256 = requirements_sha256
    workflow.save(update_fields=(
        "workflow_template_version", "injected_values", "content_hash",
        "compiler_id", "compiler_version", "route_snapshot_sha256",
        "workflow_contract_sha256", "resolved_semantics_sha256",
        "asset_bindings_sha256", "model_requirements_sha256", "updated_at",
    ))

    request_shot_ids = (prompt.request_options or {}).get("shot_ids")
    shot_ids = (
        [str(value) for value in request_shot_ids]
        if isinstance(request_shot_ids, list)
        else ([str(prompt.shot_id)] if prompt.shot_id else [])
    )
    task.backend = task.backend or "comfyui_local"
    task.model_profile = model_identifier
    task.workflow_version = version.version
    if not is_image:
        task.effective_frames = frames
        task.effective_duration_ms = duration_ms
    task.capability_snapshot = {
        **(task.capability_snapshot or {}),
        "generated_workflow_id": str(workflow.id),
        "workflow_hash": graph_contract.sha256,
        "prompt_artifact_id": str(prompt.id),
        "prompt_content_hash": prompt.content_hash,
        "template_id": str(template.id),
        "template_slug": template.slug,
        "model": model_identifier,
        "conditioning_mode": "" if is_image else str(prompt.conditioning_mode or mode),
        "effective_conditioning_mode": mode,
        "prompt_template_key": str(payload.get("prompt_template_key") or ""),
        "workflow_mode": "semantic",
        "source_workflow": "",
        "workflow_parameters": semantics,
        "resolution_profile": "semantic_contract",
        "resolution_multiple": int(resolution_multiple),
        "width": width,
        "height": height,
        "shot_ids": shot_ids,
        "shot_count": len(shot_ids),
        "whole_episode": bool((prompt.request_options or {}).get("whole_episode", False)),
        "workflow_template_version_id": str(version.id),
        "workflow_contract_sha256": execution_contract_sha256,
        "route_snapshot_sha256": route_sha256,
        "asset_bindings": references,
        "asset_bindings_sha256": bindings_sha256,
        "continuity_assets": continuity,
        "continuity_assets_sha256": json_contract(continuity).sha256,
        "model_requirements": model_requirements,
        "model_requirements_sha256": requirements_sha256,
        "resolved_semantics_sha256": semantics_sha256,
        "output_contract": output_contract,
        "execution_metadata": execution_metadata,
        "execution_metadata_sha256": metadata_sha256,
    }
    if is_image:
        for key in ("fps", "requested_duration_ms", "include_audio"):
            task.capability_snapshot.pop(key, None)
    else:
        task.capability_snapshot.update({
            "fps": fps,
            "requested_duration_ms": duration_ms,
            "include_audio": bool(prompt.include_audio),
        })
    task.save(update_fields=(
        "backend", "model_profile", "workflow_version", "effective_frames",
        "effective_duration_ms", "capability_snapshot", "updated_at",
    ))
    return task


def build_story(username="alice"):
    user = get_user_model().objects.create_user(username=username, password="StrongPass123!")
    style = Style.objects.create(
        slug=f"noir-{username}", name="黑色侦探", category=Style.Category.DETECTIVE,
        visual_prompt="rainy neo-noir, amber practical lights",
        negative_prompt="face drift, costume drift",
    )
    project = SeriesProject.objects.create(
        owner=user, title="雨夜证词", style=style, width=768, height=1344,
        fps=24, default_shot_duration_ms=5000, continuity_seed=42,
        language="Chinese", aspect_ratio="9:16",
        logline="侦探在停电前找出说谎的人。",
    )
    portrait = Asset.objects.create(
        owner=user, style=style, kind=Asset.Kind.CHARACTER, name="林默标准像",
        local_path="C:/fixtures/lin.png", checksum="fixture-lin", mime_type="image/png",
    )
    scene_reference = Asset.objects.create(
        owner=user, style=style, kind=Asset.Kind.SCENE, name="档案室标准景",
        local_path="C:/fixtures/archive.png", checksum="fixture-archive", mime_type="image/png",
    )
    character = CharacterBible.objects.create(
        project=project, slug="lin", name="林默", stable_speaker_id="S1",
        visual_identity="三十岁短发女侦探，左眼下小痣", wardrobe="炭灰风衣",
        voice_profile="冷静低声", face_identity="高颧骨，深棕眼睛",
        anchor_asset=portrait,
    )
    scene = SceneBible.objects.create(
        project=project, slug="archive", name="旧档案室",
        visual_identity="狭窄高架档案室，尘埃漂浮", lighting="绿色台灯和窗外闪电",
        ambient_sound="雨打玻璃，老钟滴答",
        anchor_asset=scene_reference,
    )
    episode = Episode.objects.create(
        project=project,
        number=1,
        title="停电前",
        synopsis="线索浮现。",
        prompt_settings={"include_audio": True},
    )
    shot1 = Shot.objects.create(
        episode=episode, number=1, title="证物", narrative="林默展开烧焦的信纸。",
        camera="俯拍信纸后推近面部", dialogue=[{"speaker_id": "S1", "text": "这封信不是昨晚烧的。"}],
        scene=scene, duration_ms=3500, sound_effects="纸张摩擦", music="低音弦乐",
        media_type=Shot.MediaType.VIDEO, include_audio=True,
    )
    shot1.characters.add(character)
    shot2 = Shot.objects.create(
        episode=episode, number=2, title="闪电", narrative="闪电照亮门后的人影。",
        camera="快速摇镜到门口", dialogue=[{"speaker_id": "S1", "text": "别动。"}],
        scene=scene, duration_ms=2100, sound_effects="近雷炸响",
        media_type=Shot.MediaType.VIDEO, include_audio=True,
    )
    shot2.characters.add(character)
    return user, project, character, scene, episode, shot1, shot2
