from copy import deepcopy

from uuid import uuid4

from django.contrib.auth import get_user_model
from django.utils import timezone

from studio.models import (
    Asset,
    CharacterBible,
    Episode,
    ModelDeployment,
    ModelProfileVersion,
    ProviderProfile,
    RuntimeHost,
    SceneBible,
    SeriesProject,
    Shot,
    Style,
    StyleCategory,
    PromptProfileFieldRule,
    PromptProfileVersion,
)
from studio.services.json_contracts import assign_json_contract
from studio.services.prompt_normalization_contract import PROMPT_NORMALIZE_OPERATION


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




def create_prompt_normalization_test_deployment() -> ModelDeployment:
    """Create a native protocol fixture, not route or Smoke evidence."""

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
    return deployment


def build_story(username="alice"):
    user = get_user_model().objects.create_user(username=username, password="StrongPass123!")
    category, _ = StyleCategory.objects.get_or_create(key="detective", defaults={"name": "侦探"})
    style = Style.objects.create(
        slug=f"noir-{username}", name="黑色侦探", category=category,
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
        wardrobe="炭灰风衣",
        voice_profile="冷静低声", face_identity="高颧骨，深棕眼睛",
        anchor_asset=portrait,
    )
    scene = SceneBible.objects.create(
        project=project, slug="archive", name="旧档案室",
        lighting="绿色台灯和窗外闪电",
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
        episode=episode, number=1, title="证物",
        dialogue=[{"speaker_id": "S1", "text": "这封信不是昨晚烧的。"}],
        scene=scene, duration_ms=3500,
        media_type=Shot.MediaType.VIDEO, include_audio=True,
    )
    shot2 = Shot.objects.create(
        episode=episode, number=2, title="闪电",
        dialogue=[{"speaker_id": "S1", "text": "别动。"}],
        scene=scene, duration_ms=2100,
        media_type=Shot.MediaType.VIDEO, include_audio=True,
    )
    return user, project, character, scene, episode, shot1, shot2
