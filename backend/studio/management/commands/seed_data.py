from __future__ import annotations

import secrets

from django.contrib.auth import get_user_model
from django.core.management.base import BaseCommand
from django.db import transaction

from studio.models import CharacterBible, Episode, SceneBible, SeriesProject, Shot, Style, WorkflowTemplate
from studio.services.h3_prompts import sync_consistency_locks
from studio.services.resolution import (
    H3_RESOLUTION_MULTIPLE_DEFAULT,
    H3_RESOLUTION_MULTIPLE_MAX,
    H3_RESOLUTION_MULTIPLE_MIN,
    H3_RESOLUTION_MULTIPLE_STEP,
    H3_RESOLUTION_PROFILES,
)


STYLES = [
    ("wuxia", "武侠", "wuxia", "cinematic neo-wuxia, expressive ink-wash atmosphere, realistic faces, flowing fabric, restrained jade and vermilion palette, dynamic wire-action composition"),
    ("xianxia", "玄幻仙侠", "xuanhuan", "epic Chinese xianxia fantasy, celestial mountains, luminous spiritual energy, ornate ancient robes, grand volumetric clouds, premium cinematic VFX"),
    ("anime", "国创动漫", "anime", "premium Chinese animation, precise linework, expressive eyes, layered parallax backgrounds, controlled highlights, coherent character sheets, cinematic framing"),
    ("thriller", "心理惊悚", "thriller", "psychological thriller, oppressive practical lighting, weathered textures, restrained visual clues, escalating dread, no graphic gore"),
    ("detective", "民国侦探", "detective", "Republic-era Chinese detective noir, rain-streaked glass, hard chiaroscuro, tobacco amber and midnight blue, methodical visual clues, elegant slow camera"),
    ("urban-drama", "都市现实剧情", "drama", "grounded contemporary urban drama, natural window light, authentic lived-in spaces, nuanced micro-expressions, handheld intimacy, realistic color science"),
    ("cyberpunk", "未来科幻", "sci_fi", "future cyberpunk China, plausible near-future infrastructure, functional industrial design, neon reflected on rain, precise technology details, large-format cinematography"),
    ("palace-intrigue", "古装权谋", "historical", "historically grounded Chinese palace intrigue, rigorous court etiquette, restrained luxury, carved timber halls, candlelit political tension, sober cinematic realism"),
    ("post-apocalypse", "末日废土", "sci_fi", "post-apocalyptic wasteland, weathered survival gear, reclaimed industrial ruins, windblown dust, harsh natural light, grounded cinematic realism"),
    ("healing-fantasy", "治愈奇幻", "xuanhuan", "healing gentle fantasy, warm pastoral light, tactile handcrafted environments, benevolent magical creatures, soft natural palette, emotionally comforting cinema"),
    ("glamour-wuxia", "魅影风华", "wuxia", "original cinematic elegant wuxia fantasy noir, mature adult characters, refined wardrobe, dramatic night lighting, premium live-action concept art"),
]

H3_TURBO_LORA = (
    "MiniMax-H3-Turbo-Lora-Pruned-ComfyUI\\"
    "minimax_h3_turbo_4step_ckpt600_ema_V4.safetensors"
)


def _native_h3_workflow(*, reference: bool = False):
    return {
        "1": {"class_type": "UNETLoader", "inputs": {
            "unet_name": (
                "MiniMax_H3_Ref2VA_pruned_int8_convrot.safetensors" if reference
                else "MiniMax_H3_FL2VA_pruned_int8_convrot.safetensors"
            ),
            "weight_dtype": "default",
        }},
        "2": {"class_type": "CLIPLoader", "inputs": {
            "clip_name": "qwen3vl_32b_minimax_h3_int8_convrot.safetensors",
            "type": "minimax", "device": "default",
        }},
        "3": {"class_type": "VAELoader", "inputs": {"vae_name": "minimax_h3_video_vae_fp16.safetensors"}},
        "4": {"class_type": "VAELoader", "inputs": {"vae_name": "minimax_h3_audio_vae_fp32.safetensors"}},
        "5": {"class_type": "MiniMaxH3ReferenceToVideo" if reference else "MiniMaxH3ImageToVideo", "inputs": {
            "clip": ["2", 0], "vae": ["3", 0],
            **({"audio_vae": ["4", 0]} if reference else {}),
            "prompt": "${PROMPT}", "width": "${WIDTH}", "height": "${HEIGHT}", "length": "${FRAMES}",
            **({"ref_image_size": "match"} if reference else {}),
        }},
        "6": {"class_type": "RandomNoise", "inputs": {"noise_seed": "${SEED}"}},
        "7": {"class_type": "BasicGuider", "inputs": {"model": ["1", 0], "conditioning": ["5", 0]}},
        "8": {"class_type": "KSamplerSelect", "inputs": {"sampler_name": "res_multistep"}},
        "9": {"class_type": "BasicScheduler", "inputs": {
            "model": ["1", 0], "scheduler": "simple", "steps": 20, "denoise": 1.0,
        }},
        "10": {"class_type": "SamplerCustomAdvanced", "inputs": {
            "noise": ["6", 0], "guider": ["7", 0], "sampler": ["8", 0],
            "sigmas": ["9", 0], "latent_image": ["5", 1],
        }},
        "11": {"class_type": "VAEDecode", "inputs": {"samples": ["10", 0], "vae": ["3", 0]}},
        "12": {"class_type": "VAEDecodeAudio", "inputs": {"samples": ["10", 0], "vae": ["4", 0]}},
        "13": {"class_type": "CreateVideo", "inputs": {
            "images": ["11", 0], "fps": 24.0, "audio": ["12", 0], "bit_depth": 8,
        }},
        "14": {"class_type": "SaveVideo", "inputs": {
            "video": ["13", 0], "filename_prefix": "${OUTPUT_PREFIX}", "format": "auto", "codec": "auto",
        }},
    }


VIDEO_WORKFLOW = _native_h3_workflow(reference=False)
REF_VIDEO_WORKFLOW = _native_h3_workflow(reference=True)


def _native_h3_turbo_lora_workflow():
    """Reviewed API equivalent of video_minimax_h3_r2v_turbo_lora.json."""
    workflow = _native_h3_workflow(reference=True)
    workflow["15"] = {
        "class_type": "LoraLoaderModelOnly",
        "inputs": {
            "model": ["1", 0],
            "lora_name": H3_TURBO_LORA,
            "strength_model": "${LORA_STRENGTH}",
        },
    }
    workflow["7"]["inputs"]["model"] = ["15", 0]
    workflow["9"]["inputs"]["model"] = ["15", 0]
    workflow["9"]["inputs"]["steps"] = "${STEPS}"
    return workflow


def _native_h3_t8_dual_clock_workflow():
    """Reviewed API equivalent of video_minimax_h3_r2v_t8_dual_clock_4step.json."""
    return {
        "1": {"class_type": "UNETLoader", "inputs": {
            "unet_name": "MiniMax_H3_Ref2VA_pruned_int8_convrot.safetensors",
            "weight_dtype": "default",
        }},
        "2": {"class_type": "LoraLoaderBypassModelOnly", "inputs": {
            "model": ["1", 0],
            "lora_name": H3_TURBO_LORA,
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
            "model": ["2", 0], "av_latent": ["6", 1], "steps": "${STEPS}",
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


TURBO_LORA_REF_VIDEO_WORKFLOW = _native_h3_turbo_lora_workflow()
T8_DUAL_CLOCK_REF_VIDEO_WORKFLOW = _native_h3_t8_dual_clock_workflow()

SDXL_IMAGE_CHECKPOINT = "sd_xl_base_1.0.safetensors"
SDXL_IMAGE_CHECKPOINT_SHA256 = "31e35c80fc4829d14f90153f4c74cd59c90b779f6afe05a74cd6120b893f7e5b"


def _native_sdxl_image_workflow():
    """Reviewed, local-only SDXL text-to-image API graph.

    Reference images selected in a shot are continuity-library material only in
    this v1 workflow. Their Bible descriptions are compiled into ``PROMPT``;
    no LoadImage/IP-Adapter/ControlNet pixel conditioning is created here.
    """

    return {
        "1": {"class_type": "CheckpointLoaderSimple", "inputs": {
            "ckpt_name": "${MODEL}",
        }},
        "2": {"class_type": "CLIPTextEncode", "inputs": {
            "text": "${PROMPT}", "clip": ["1", 1],
        }},
        "3": {"class_type": "CLIPTextEncode", "inputs": {
            "text": "${NEGATIVE_PROMPT}", "clip": ["1", 1],
        }},
        "4": {"class_type": "EmptyLatentImage", "inputs": {
            "width": "${WIDTH}", "height": "${HEIGHT}", "batch_size": 1,
        }},
        "5": {"class_type": "KSampler", "inputs": {
            "model": ["1", 0], "positive": ["2", 0], "negative": ["3", 0],
            "latent_image": ["4", 0], "seed": "${SEED}",
            "steps": 30, "cfg": 6.5, "sampler_name": "dpmpp_2m",
            "scheduler": "karras", "denoise": 1.0,
        }},
        "6": {"class_type": "VAEDecode", "inputs": {
            "samples": ["5", 0], "vae": ["1", 2],
        }},
        "7": {"class_type": "SaveImage", "inputs": {
            "images": ["6", 0], "filename_prefix": "${OUTPUT_PREFIX}",
        }},
    }


SDXL_IMAGE_WORKFLOW = _native_sdxl_image_workflow()

REVIEWED_WORKFLOW_VERSIONS = {
    "minimax-h3-base-video": 1,
    "minimax-h3-first-last-video": 1,
    "minimax-h3-reference-video": 1,
    "minimax-h3-r2v-t8-dual-clock-4step": 1,
    "minimax-h3-r2v-turbo-lora": 1,
    "minimax-reference-image": 1,
}


class Command(BaseCommand):
    help = "Seed eleven commercial style presets and dynamically injectable ComfyUI workflow templates."

    def add_arguments(self, parser):
        parser.add_argument(
            "--with-demo-project",
            action="store_true",
            help="Also create a local demo user with a one-time random password and a sample series",
        )

    @transaction.atomic
    def handle(self, *args, **options):
        active_slugs = {item[0] for item in STYLES}
        Style.objects.filter(owner=None, is_system=True).exclude(slug__in=active_slugs).update(is_active=False)
        for slug, name, category, visual_prompt in STYLES:
            Style.objects.update_or_create(
                owner=None, slug=slug,
                defaults={
                    "name": name, "category": category, "visual_prompt": visual_prompt,
                    "negative_prompt": (
                        "identity drift, face drift, costume drift, duplicate people, duplicate objects, "
                        "duplicate weapons, extra weapons, extra limbs, extra fingers, malformed hands, "
                        "text, watermark, logo, low detail, flicker"
                    ),
                    "defaults": {"aspect_ratio": "9:16", "fps": 24, "model": "minimax-h3"},
                    "description": f"{name}商业短剧预设，可绑定本地人物、场景与道具参考图。",
                    "is_system": True, "is_active": True,
                },
            )
        schema = {
            "allowed_overrides": [
                "SEED", "WIDTH", "HEIGHT", "FPS", "DURATION_MS", "MODEL",
                "AUDIO_ENABLED", "OUTPUT_PREFIX", "RESOLUTION_PROFILE",
                "RESOLUTION_MULTIPLE",
            ],
            "resolution_profiles": list(H3_RESOLUTION_PROFILES),
            "resolution_selector": {
                "algorithm": "comfyui-official",
                "multiple": H3_RESOLUTION_MULTIPLE_DEFAULT,
                "multiple_min": H3_RESOLUTION_MULTIPLE_MIN,
                "multiple_max": H3_RESOLUTION_MULTIPLE_MAX,
                "multiple_step": H3_RESOLUTION_MULTIPLE_STEP,
                "supported_aspect_ratios": ["9:16", "16:9", "1:1"],
            },
        }
        turbo_h3_parameters = {
            "STEPS": {
                "label": "采样步数", "type": "integer", "default": 10,
                "min": 1, "max": 20, "step": 1,
                "help": "Turbo LoRA 工作流默认 10 步。",
            },
            "LORA_STRENGTH": {
                "label": "LoRA 模型强度", "type": "number", "default": 1.0,
                "min": 0.0, "max": 2.2, "step": 0.05,
                "help": "0 表示不施加 LoRA，推荐从默认 1.0 开始调整。",
            },
        }
        t8_h3_parameters = {
            **turbo_h3_parameters,
            "STEPS": {
                **turbo_h3_parameters["STEPS"], "default": 4, "max": 20,
                "help": "T8 Dual-Clock 工作流默认 4 步。",
            },
        }
        i2v_source = {
            "source_workflow": "video_minimax_h3_i2v.json",
            "source_format": "comfyui_ui_workflow",
            "execution_graph": "security-reviewed equivalent ComfyUI API JSON",
            "official_node_type": "MiniMaxH3ImageToVideo",
            "required_models": {
                "diffusion_models": ["MiniMax_H3_FL2VA_pruned_int8_convrot.safetensors"],
                "text_encoders": ["qwen3vl_32b_minimax_h3_int8_convrot.safetensors"],
                "vae": [
                    "minimax_h3_video_vae_fp16.safetensors",
                    "minimax_h3_audio_vae_fp32.safetensors",
                ],
            },
        }
        r2v_source = {
            "source_workflow": "video_minimax_h3_r2v.json",
            "source_format": "comfyui_ui_workflow",
            "execution_graph": "security-reviewed equivalent ComfyUI API JSON",
            "official_node_type": "MiniMaxH3ReferenceToVideo",
            "reference_loader_nodes": {
                "image": "LoadImage -> IMAGE",
                "video": "LoadVideo -> GetVideoComponents(images=0,audio=1)",
                "audio": "LoadAudio -> AUDIO",
            },
            "required_models": {
                "diffusion_models": ["MiniMax_H3_Ref2VA_pruned_int8_convrot.safetensors"],
                "text_encoders": ["qwen3vl_32b_minimax_h3_int8_convrot.safetensors"],
                "vae": [
                    "minimax_h3_video_vae_fp16.safetensors",
                    "minimax_h3_audio_vae_fp32.safetensors",
                ],
            },
        }
        binding_schema = {
            "reference_bindings_field": "Shot.reference_bindings",
            "reference_bindings_contract": {
                "first_frame_asset_id": "uuid?",
                "last_frame_asset_id": "uuid?",
                "characters": [{"entity_id": "uuid", "asset_id": "uuid"}],
                "scene": {"entity_id": "uuid", "asset_id": "uuid"},
                "props": [{"entity_id": "uuid", "asset_id": "uuid"}],
                "videos": [{
                    "asset_id": "uuid", "use_embedded_audio": False,
                    "audio_asset_id": "uuid?",
                }],
                "audios": [{"asset_id": "uuid"}],
            },
        }
        active_workflow_slugs = set(REVIEWED_WORKFLOW_VERSIONS)
        system_templates = WorkflowTemplate.objects.filter(owner=None, is_system=True)
        system_templates.exclude(slug__in=active_workflow_slugs).update(is_active=False)
        for slug, active_version in REVIEWED_WORKFLOW_VERSIONS.items():
            system_templates.filter(slug=slug).exclude(version=active_version).update(
                is_active=False,
            )
        WorkflowTemplate.objects.update_or_create(
            owner=None, slug="minimax-h3-base-video",
            version=REVIEWED_WORKFLOW_VERSIONS["minimax-h3-base-video"],
            defaults={
                "name": "MiniMax H3 文生视频 T2VA", "provider": "comfyui", "output_kind": "video",
                "model": "minimax-h3", "definition": VIDEO_WORKFLOW,
                "input_schema": {
                    **schema, **i2v_source, **binding_schema,
                    "workflow_mode": "text_to_video",
                    "h3_modes": ["T2VA"],
                    "is_default": True,
                    "prompt_template_keys": ["h3_t2v_text_v1"],
                    "reference_policy": "forbidden",
                },
                "description": (
                    "文生视频独立入口，仅允许 T2VA 且禁止参考图。来源为本机官方 "
                    "video_minimax_h3_i2v.json；平台执行经安全审核的等价 API JSON。"
                ),
                "is_system": True, "is_active": True,
            },
        )
        WorkflowTemplate.objects.update_or_create(
            owner=None, slug="minimax-h3-first-last-video",
            version=REVIEWED_WORKFLOW_VERSIONS["minimax-h3-first-last-video"],
            defaults={
                "name": "MiniMax H3 首尾帧视频 FL2VA", "provider": "comfyui", "output_kind": "video",
                "model": "minimax-h3", "definition": VIDEO_WORKFLOW,
                "input_schema": {
                    **schema, **i2v_source, **binding_schema,
                    "workflow_mode": "first_last_to_video",
                    "is_default": True,
                    # I2VA/L2VA remain executable for historical records. New
                    # UI writes FL2VA and actual conditioning is persisted as
                    # T2VA/I2VA/L2VA/FL2VA according to its 0/1/2 frame slots.
                    "h3_modes": ["I2VA", "FL2VA", "L2VA"],
                    "prompt_template_keys": [
                        "h3_i2v_zero_frame_v1", "h3_i2v_first_frame_v1",
                        "h3_i2v_last_frame_v1", "h3_i2v_first_last_v1",
                    ],
                    "reference_policy": "optional first_frame and/or last_frame; distinct when both",
                },
                "description": (
                    "首尾帧入口支持 0/1/2 张图；根据已绑定槽位实际执行 T2VA/I2VA/L2VA/FL2VA。"
                    "来源为本机官方 video_minimax_h3_i2v.json；平台执行经安全审核的等价 API JSON。"
                ),
                "is_system": True, "is_active": True,
            },
        )
        WorkflowTemplate.objects.update_or_create(
            owner=None, slug="minimax-h3-reference-video",
            version=REVIEWED_WORKFLOW_VERSIONS["minimax-h3-reference-video"],
            defaults={
                "name": "MiniMax H3 本地 Ref2VA 视频", "provider": "comfyui", "output_kind": "video",
                "model": "minimax-h3", "definition": REF_VIDEO_WORKFLOW,
                "input_schema": {
                    **schema, **r2v_source, **binding_schema,
                    "workflow_mode": "reference_to_video",
                    "h3_modes": ["Ref2VA"],
                    "is_default": True,
                    "prompt_template_keys": ["h3_r2v_zero_reference_v1", "h3_r2v_classified_v1"],
                    "reference_policy": (
                        "0-9 images / 0-3 videos / 0-3 standalone audios / "
                        "0-3 video-paired audio tracks (embedded or external, one per video); "
                        "unique assets"
                    ),
                    "reference_media_limits": {
                        "images": 9,
                        "videos": 3,
                        "standalone_audios": 3,
                        "video_paired_audios": 3,
                    },
                },
                "description": (
                    "参考生视频独立入口；接受 0–9 张分类图片、0–3 个参考视频和 0–3 个独立音频；"
                    "还支持最多 3 条按视频索引配对的音轨，每个视频只能在内嵌音轨与外部音频中二选一。"
                    "来源为本机官方 video_minimax_h3_r2v.json；平台执行经安全审核的等价 API JSON。"
                ),
                "is_system": True, "is_active": True,
            },
        )
        WorkflowTemplate.objects.update_or_create(
            owner=None, slug="minimax-h3-r2v-t8-dual-clock-4step",
            version=REVIEWED_WORKFLOW_VERSIONS["minimax-h3-r2v-t8-dual-clock-4step"],
            defaults={
                "name": "MiniMax H3 Ref2VA T8 Dual-Clock 4-Step",
                "provider": "comfyui", "output_kind": "video", "model": "minimax-h3",
                "definition": T8_DUAL_CLOCK_REF_VIDEO_WORKFLOW,
                "input_schema": {
                    **schema, **r2v_source, **binding_schema,
                    "source_workflow": "video_minimax_h3_r2v_t8_dual_clock_4step.json",
                    "official_node_type": "MiniMaxH3AudioConditioningT8",
                    "workflow_mode": "reference_to_video",
                    "model_profile": "local_h3_ref2va_t8_dual_clock_4step",
                    "h3_modes": ["Ref2VA"],
                    "is_default": False,
                    "maturity": "experimental",
                    "user_parameters": t8_h3_parameters,
                    "lora_model": H3_TURBO_LORA,
                    "required_models": {
                        **r2v_source["required_models"],
                        "loras": [H3_TURBO_LORA],
                    },
                    "required_custom_nodes": ["comfyui-minimax-h3-audio-T8"],
                    "prompt_template_keys": ["h3_r2v_zero_reference_v1", "h3_r2v_classified_v1"],
                    "reference_policy": (
                        "0-9 images / 0-3 videos / 0-3 standalone audios / "
                        "0-3 video-paired audio tracks; unique assets"
                    ),
                    "reference_media_limits": {
                        "images": 9, "videos": 3, "standalone_audios": 3,
                        "video_paired_audios": 3,
                    },
                },
                "description": (
                    "Ref2VA 的 T8 双时钟四步变体，可调整采样步数和 LoRA 模型强度。"
                    "来源为本机 video_minimax_h3_r2v_t8_dual_clock_4step.json；"
                    "平台执行经安全审核的等价 API JSON。"
                ),
                "is_system": True, "is_active": True,
            },
        )
        WorkflowTemplate.objects.update_or_create(
            owner=None, slug="minimax-h3-r2v-turbo-lora",
            version=REVIEWED_WORKFLOW_VERSIONS["minimax-h3-r2v-turbo-lora"],
            defaults={
                "name": "MiniMax H3 Ref2VA Turbo LoRA",
                "provider": "comfyui", "output_kind": "video", "model": "minimax-h3",
                "definition": TURBO_LORA_REF_VIDEO_WORKFLOW,
                "input_schema": {
                    **schema, **r2v_source, **binding_schema,
                    "source_workflow": "video_minimax_h3_r2v_turbo_lora.json",
                    "workflow_mode": "reference_to_video",
                    "model_profile": "local_h3_ref2va_turbo_lora",
                    "h3_modes": ["Ref2VA"],
                    "is_default": False,
                    "maturity": "experimental",
                    "user_parameters": turbo_h3_parameters,
                    "lora_model": H3_TURBO_LORA,
                    "required_models": {
                        **r2v_source["required_models"],
                        "loras": [H3_TURBO_LORA],
                    },
                    "prompt_template_keys": ["h3_r2v_zero_reference_v1", "h3_r2v_classified_v1"],
                    "reference_policy": (
                        "0-9 images / 0-3 videos / 0-3 standalone audios / "
                        "0-3 video-paired audio tracks; unique assets"
                    ),
                    "reference_media_limits": {
                        "images": 9, "videos": 3, "standalone_audios": 3,
                        "video_paired_audios": 3,
                    },
                },
                "description": (
                    "Ref2VA 的 Turbo LoRA 变体，可调整采样步数和 LoRA 模型强度。"
                    "来源为本机 video_minimax_h3_r2v_turbo_lora.json；"
                    "平台执行经安全审核的等价 API JSON。"
                ),
                "is_system": True, "is_active": True,
            },
        )
        WorkflowTemplate.objects.update_or_create(
            owner=None, slug="minimax-reference-image",
            version=REVIEWED_WORKFLOW_VERSIONS["minimax-reference-image"],
            defaults={
                "name": "本地 SDXL 一致性定帧（文生图）", "provider": "comfyui", "output_kind": "image",
                "model": SDXL_IMAGE_CHECKPOINT, "definition": SDXL_IMAGE_WORKFLOW,
                "input_schema": {
                    **schema,
                    "allowed_overrides": [
                        "SEED", "WIDTH", "HEIGHT", "OUTPUT_PREFIX", "RESOLUTION_PROFILE",
                        "RESOLUTION_MULTIPLE",
                    ],
                    "workflow_mode": "text_to_image",
                    "conditioning_mode": "text_and_bible_anchors_only",
                    "pixel_reference_inputs": False,
                    "prompt_template_keys": ["sdxl_txt2img_bible_v1"],
                    "model_id": "stabilityai/stable-diffusion-xl-base-1.0",
                    "fixed_model": True,
                    "required_dimension_multiple": 8,
                    "recommended_resolution_profile": "0.98",
                    "recommended_megapixels_min": 0.8,
                    "low_resolution_smoke_profiles": ["0.1", "0.2", "0.3"],
                    "required_models": {"checkpoints": [SDXL_IMAGE_CHECKPOINT]},
                    "model_source": (
                        "https://huggingface.co/stabilityai/"
                        "stable-diffusion-xl-base-1.0"
                    ),
                    "model_file": {
                        "name": SDXL_IMAGE_CHECKPOINT,
                        "size_bytes": 6_938_078_334,
                        "sha256": SDXL_IMAGE_CHECKPOINT_SHA256,
                    },
                },
                "description": (
                    "本机标准 SDXL Base 1.0 文生图片工作流。固定使用已校验 checkpoint；"
                    "人物、场景、道具 Bible 会写入文本提示词，但所选参考图片不会作为像素条件连接。"
                    "每次任务仍会在 POST /prompt 前用实时 /object_info 校验 ckpt_name、节点、端口和枚举。"
                ),
                "is_system": True, "is_active": True,
            },
        )
        if options["with_demo_project"]:
            self._seed_demo_project()
        self.stdout.write(self.style.SUCCESS(
            "Seeded 11 styles, 3 official-default and 2 experimental active H3 video templates, "
            "and 1 active local SDXL image template."
        ))

    def _seed_demo_project(self):
        User = get_user_model()
        user, created = User.objects.get_or_create(username="demo", defaults={"email": "demo@example.test"})
        if created:
            # Generate enough entropy for a local demonstration account and
            # reveal it only in this first command invocation.  The plaintext
            # is never stored by the application; Django persists only its
            # salted password hash.  Re-seeding an existing demo must not reset
            # its credentials.
            one_time_password = secrets.token_urlsafe(32)
            user.set_password(one_time_password)
            user.save(update_fields=("password",))
            self.stdout.write(self.style.WARNING(
                "Created local demo user 'demo'. One-time password: "
                f"{one_time_password}\n"
                "This password will not be displayed again. Store it through an approved secret channel."
            ))
        style = Style.objects.get(owner=None, slug="wuxia")
        project, _ = SeriesProject.objects.get_or_create(
            owner=user, title="无名剑影",
            defaults={
                "logline": "失忆剑客在雨夜客栈追查一枚残缺玉佩的主人。", "style": style,
                "status": SeriesProject.Status.ACTIVE, "continuity_seed": 8675309,
                "continuity_summary": "玉佩始终由沈砚贴身携带；雨势逐集增强。",
            },
        )
        if project.width % 32 or project.height % 32 or project.fps != 24:
            project.width, project.height, project.fps = 768, 1344, 24
            project.save(update_fields=("width", "height", "fps", "updated_at"))
        character, _ = CharacterBible.objects.get_or_create(
            project=project, slug="shen-yan",
            defaults={
                "name": "沈砚", "stable_speaker_id": "S1", "role": "主角",
                "visual_identity": "清瘦青年剑客，冷峻轮廓，右眉尾浅疤，长发高束",
                "face_identity": "狭长深色眼睛，直鼻，薄唇", "wardrobe": "深靛窄袖劲装，旧黑披风",
                "voice_profile": "低沉克制，语速偏慢", "is_locked": True,
            },
        )
        scene, _ = SceneBible.objects.get_or_create(
            project=project, slug="rain-inn",
            defaults={
                "name": "雨夜客栈", "location": "山道旁两层木客栈", "time_of_day": "深夜",
                "visual_identity": "潮湿乌木梁柱，破旧纸窗，门外连绵冷雨",
                "lighting": "暖黄油灯对比门外青蓝雨光", "ambient_sound": "屋檐雨滴，远雷，木板轻响",
            },
        )
        episode, _ = Episode.objects.get_or_create(
            project=project, number=1,
            defaults={"title": "残玉", "synopsis": "沈砚在客栈听见陌生人叫出自己的旧名。"},
        )
        shot, _ = Shot.objects.get_or_create(
            episode=episode, number=1,
            defaults={
                "title": "推门", "narrative": "沈砚推开客栈木门，雨水沿披风滴落，他停在门槛扫视众人。",
                "camera": "低机位中景缓慢推近，最后停在右眉浅疤特写", "duration_ms": 5000,
                "dialogue": [{"speaker_id": "S1", "text": "有人在等我？"}], "scene": scene,
                "sound_effects": "木门吱呀，雨声突然清晰", "music": "低沉古琴单音",
            },
        )
        shot.characters.add(character)
        sync_consistency_locks(project)
