"""Code-reviewed seed workflow definitions shared by seeding and readiness checks."""
from __future__ import annotations

from studio.services.system_workflow_contracts import approved_minimax_reference_image_definition


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
        "3": {"class_type": "VAELoader", "inputs": {
            "vae_name": "minimax_h3_video_vae_fp16.safetensors",
        }},
        "4": {"class_type": "VAELoader", "inputs": {
            "vae_name": "minimax_h3_audio_vae_fp32.safetensors",
        }},
        "5": {
            "class_type": "MiniMaxH3ReferenceToVideo" if reference else "MiniMaxH3ImageToVideo",
            "inputs": {
                "clip": ["2", 0], "vae": ["3", 0],
                **({"audio_vae": ["4", 0]} if reference else {}),
                "prompt": "${PROMPT}", "width": "${WIDTH}", "height": "${HEIGHT}",
                "length": "${FRAMES}",
                **({"ref_image_size": "match"} if reference else {}),
            },
        },
        "6": {"class_type": "RandomNoise", "inputs": {"noise_seed": "${SEED}"}},
        "7": {"class_type": "BasicGuider", "inputs": {
            "model": ["1", 0], "conditioning": ["5", 0],
        }},
        "8": {"class_type": "KSamplerSelect", "inputs": {"sampler_name": "res_multistep"}},
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


VIDEO_WORKFLOW = _native_h3_workflow(reference=False)
REF_VIDEO_WORKFLOW = _native_h3_workflow(reference=True)


def _native_h3_turbo_lora_workflow():
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
    return {
        "1": {"class_type": "UNETLoader", "inputs": {
            "unet_name": "MiniMax_H3_Ref2VA_pruned_int8_convrot.safetensors",
            "weight_dtype": "default",
        }},
        "2": {"class_type": "LoraLoaderBypassModelOnly", "inputs": {
            "model": ["1", 0], "lora_name": H3_TURBO_LORA,
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
SDXL_IMAGE_WORKFLOW = approved_minimax_reference_image_definition()

REVIEWED_WORKFLOW_VERSIONS = {
    "minimax-h3-base-video": 2,
    "minimax-h3-first-last-video": 2,
    "minimax-h3-reference-video": 2,
    "minimax-h3-r2v-t8-dual-clock-4step": 2,
    "minimax-h3-r2v-turbo-lora": 2,
    "minimax-reference-image": 1,
}
