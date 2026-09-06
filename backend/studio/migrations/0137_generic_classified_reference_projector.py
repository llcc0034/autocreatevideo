"""Seed a declarative example; never migrate execution acceptance.

Existing workflow evidence is NOT converted. Unsupported legacy projector
versions leave the automatic catalog and need explicit dependency-reviewed
cleanup. Fresh installations have no references and remove that unused seed.
"""
import json
import hashlib

from django.db import migrations
from django.utils import timezone

# Business example data, interpreted by the same generic engine as other models.
DEFINITION = json.loads(r'''{
  "schema_version": 1,
  "priority": 300,
  "detector": {
    "fallback": false,
    "allow_other_nodes": false,
    "node_counts": {
      "UNETLoader": 1,
      "LoraLoaderBypassModelOnly": 1,
      "CLIPLoader": 1,
      "VAELoader": 2,
      "MiniMaxH3AudioConditioningT8": 1,
      "MiniMaxH3DualClockSamplerT8": 1,
      "BasicGuider": 1,
      "RandomNoise": 1,
      "SamplerCustomAdvanced": 1,
      "MiniMaxH3AVDecodeT8": 1,
      "CreateVideo": 1,
      "SaveVideo": 1
    },
    "optional_node_counts": {
      "MiniMaxLowVRAMAttention": {
        "min": 0,
        "max": 1
      }
    },
    "input_equals": [
      {
        "node_type": "MiniMaxH3AudioConditioningT8",
        "input_name": "task_type",
        "value": "Ref2VA"
      }
    ]
  },
  "output_nodes": {
    "SaveVideo": "video"
  },
  "model_selectors": [
    {
      "node_type": "UNETLoader",
      "input_name": "unet_name",
      "role": "diffusion_model",
      "inventory_folder": "",
      "inventory_node_type": "",
      "inventory_input_name": ""
    },
    {
      "node_type": "CLIPLoader",
      "input_name": "clip_name",
      "role": "text_encoder",
      "inventory_folder": "",
      "inventory_node_type": "",
      "inventory_input_name": ""
    },
    {
      "node_type": "VAELoader",
      "input_name": "vae_name",
      "role": "vae",
      "inventory_folder": "",
      "inventory_node_type": "",
      "inventory_input_name": ""
    },
    {
      "node_type": "LoraLoaderBypassModelOnly",
      "input_name": "lora_name",
      "role": "lora",
      "inventory_folder": "",
      "inventory_node_type": "",
      "inventory_input_name": ""
    }
  ],
  "node_roles": {
    "unet": {
      "node_type": "UNETLoader",
      "cardinality": "one"
    },
    "lora": {
      "node_type": "LoraLoaderBypassModelOnly",
      "cardinality": "one"
    },
    "clip": {
      "node_type": "CLIPLoader",
      "cardinality": "one"
    },
    "conditioner": {
      "node_type": "MiniMaxH3AudioConditioningT8",
      "cardinality": "one"
    },
    "sampler": {
      "node_type": "MiniMaxH3DualClockSamplerT8",
      "cardinality": "one"
    },
    "guider": {
      "node_type": "BasicGuider",
      "cardinality": "one"
    },
    "noise": {
      "node_type": "RandomNoise",
      "cardinality": "one"
    },
    "advanced": {
      "node_type": "SamplerCustomAdvanced",
      "cardinality": "one"
    },
    "decode": {
      "node_type": "MiniMaxH3AVDecodeT8",
      "cardinality": "one"
    },
    "create": {
      "node_type": "CreateVideo",
      "cardinality": "one"
    },
    "output": {
      "node_type": "SaveVideo",
      "cardinality": "one"
    }
  },
  "required_edges": [
    {
      "from_role": "unet",
      "output_index": 0,
      "to_role": "lora",
      "input_name": "model"
    },
    {
      "from_role": "clip",
      "output_index": 0,
      "to_role": "conditioner",
      "input_name": "clip"
    },
    {
      "from_role": "sampler",
      "output_index": 0,
      "to_role": "guider",
      "input_name": "model"
    },
    {
      "from_role": "conditioner",
      "output_index": 0,
      "to_role": "guider",
      "input_name": "conditioning"
    },
    {
      "from_role": "noise",
      "output_index": 0,
      "to_role": "advanced",
      "input_name": "noise"
    },
    {
      "from_role": "guider",
      "output_index": 0,
      "to_role": "advanced",
      "input_name": "guider"
    },
    {
      "from_role": "sampler",
      "output_index": 1,
      "to_role": "advanced",
      "input_name": "sampler"
    },
    {
      "from_role": "sampler",
      "output_index": 2,
      "to_role": "advanced",
      "input_name": "sigmas"
    },
    {
      "from_role": "conditioner",
      "output_index": 1,
      "to_role": "advanced",
      "input_name": "latent_image"
    },
    {
      "from_role": "advanced",
      "output_index": 0,
      "to_role": "decode",
      "input_name": "av_latent"
    },
    {
      "from_role": "decode",
      "output_index": 0,
      "to_role": "create",
      "input_name": "images"
    },
    {
      "from_role": "decode",
      "output_index": 1,
      "to_role": "create",
      "input_name": "audio"
    },
    {
      "from_role": "create",
      "output_index": 0,
      "to_role": "output",
      "input_name": "video"
    }
  ],
  "parameter_policies": [
    {
      "semantic_key": "prompt.positive",
      "node_role": "conditioner",
      "input_name": "prompt",
      "policy": "runtime",
      "value": null
    },
    {
      "semantic_key": "image.width",
      "node_role": "conditioner",
      "input_name": "width",
      "policy": "object_info",
      "value": null
    },
    {
      "semantic_key": "image.height",
      "node_role": "conditioner",
      "input_name": "height",
      "policy": "object_info",
      "value": null
    },
    {
      "semantic_key": "video.frames",
      "node_role": "conditioner",
      "input_name": "length",
      "policy": "object_info",
      "value": null,
      "constraints": {
        "min": 5,
        "max": 3600,
        "alignment_modulus": 17,
        "alignment_remainder": 5,
        "default": 22
      }
    },
    {
      "semantic_key": "video.fps",
      "node_role": "create",
      "input_name": "fps",
      "policy": "fixed",
      "value": 24
    },
    {
      "semantic_key": "sampling.seed",
      "node_role": "noise",
      "input_name": "noise_seed",
      "policy": "runtime",
      "value": null
    },
    {
      "semantic_key": "sampling.steps",
      "node_role": "sampler",
      "input_name": "steps",
      "policy": "fixed",
      "value": 4
    },
    {
      "semantic_key": "parameter.lora.strength",
      "node_role": "lora",
      "input_name": "strength_model",
      "policy": "fixed",
      "value": 1
    },
    {
      "semantic_key": "output.prefix",
      "node_role": "output",
      "input_name": "filename_prefix",
      "policy": "runtime",
      "value": null
    }
  ],
  "asset_slots": [
    {
      "key": "images",
      "media_kind": "image",
      "dynamic": true,
      "contract_source": "model_profile"
    },
    {
      "key": "videos",
      "media_kind": "video",
      "dynamic": true,
      "contract_source": "model_profile"
    },
    {
      "key": "video_audios",
      "media_kind": "audio",
      "dynamic": true,
      "contract_source": "model_profile"
    },
    {
      "key": "audios",
      "media_kind": "audio",
      "dynamic": true,
      "contract_source": "model_profile"
    }
  ],
  "adapters": {
    "compiler": "generic.comfy/v3",
    "output": "comfy.primary/v1",
    "dynamic": "generic.classified_references/v1"
  },
  "projection": {
    "kind": "generic_classified",
    "config": {
      "capability": "video.reference_to_video",
      "conditioning_mode": "Ref2VA",
      "target_role": "conditioner",
      "prompt_template_keys": [],
      "static_semantics": {
        "audio.enabled": true
      },
      "dynamic_groups": {
        "images": {
          "input_names": [],
          "prefix": "ref_images.ref_image_",
          "mode_input": "entity_images",
          "minimum_items": null,
          "maximum_items": null
        },
        "videos": {
          "input_names": [],
          "prefix": "ref_videos.ref_video_",
          "mode_input": "reference_videos",
          "minimum_items": null,
          "maximum_items": null
        },
        "video_audios": {
          "input_names": [],
          "prefix": "ref_video_audios.ref_video_audio_",
          "mode_input": "paired_video_audios",
          "minimum_items": null,
          "maximum_items": null
        },
        "audios": {
          "input_names": [],
          "prefix": "ref_audios.ref_audio_",
          "mode_input": "reference_audios",
          "minimum_items": null,
          "maximum_items": null
        }
      }
    }
  }
}''')


def seed_generic_projector(apps, schema_editor):
    Projector = apps.get_model("studio", "WorkflowProjector")
    Version = apps.get_model("studio", "WorkflowProjectorVersion")
    Workflow = apps.get_model("studio", "WorkflowTemplateVersion")
    Source = apps.get_model("studio", "ComfyWorkflowSource")
    Draft = apps.get_model("studio", "ComfyWorkflowContractDraft")
    alias = schema_editor.connection.alias
    for legacy in Version.objects.using(alias).filter(engine_key="minimax_h3.ref2va"):
        identity = str(legacy.pk)
        referenced = Workflow.objects.using(alias).filter(input_contract__contract_projector__version_id=identity).exists()
        # Candidate/draft provenance is a reference too, even before promotion.
        referenced = referenced or Draft.objects.using(alias).filter(projector_version_id=legacy.pk).exists()
        referenced = referenced or Source.objects.using(alias).filter(conversion_report__contract_projector__version_id=identity).exists()
        if referenced:
            Version.objects.using(alias).filter(pk=legacy.pk).update(is_current=False)
            Projector.objects.using(alias).filter(pk=legacy.projector_id).update(is_active=False)
        else:
            Version.objects.using(alias).filter(pk=legacy.pk).delete()
            if not Version.objects.using(alias).filter(projector_id=legacy.projector_id).exists():
                Projector.objects.using(alias).filter(pk=legacy.projector_id).delete()
    family, _ = Projector.objects.using(alias).get_or_create(
        key="minimax_h3.classified_ref2va",
        defaults={"name": "MiniMax H3 分类参考 · 通用接线", "description": "节点、端口和参数均由通用分类参考 Projector 配置解释。", "is_active": True},
    )
    encoded = json.dumps(DEFINITION, ensure_ascii=False, sort_keys=True, separators=(",", ":"), allow_nan=False).encode()
    Version.objects.using(alias).get_or_create(projector_id=family.pk, version=1, defaults={
        "provider_driver_type": "comfyui", "operation": "workflow.import", "output_kind": "video",
        "engine_key": "generic.classified", "engine_version": 1,
        "definition": DEFINITION, "definition_schema_version": 1,
        "definition_size_bytes": len(encoded), "definition_sha256": hashlib.sha256(encoded).hexdigest(),
        "status": "published", "is_current": True, "published_at": timezone.now(),
    })


class Migration(migrations.Migration):
    dependencies = [("studio", "0136_allow_unresolved_comfy_contract_hashes")]
    operations = [migrations.RunPython(seed_generic_projector, migrations.RunPython.noop)]
