import hashlib
from pathlib import Path
import tempfile
from types import SimpleNamespace
from unittest.mock import Mock, patch

from django.contrib.auth import get_user_model
from django.core.exceptions import ValidationError
from django.db import transaction
from django.test import TestCase, override_settings

from studio.models import (
    Asset,
    ModelArtifact,
    ModelProfileVersion,
    PromptArtifact,
    ProviderProfile,
    SeriesProject,
    WorkflowTemplate,
)
from studio.services.json_contracts import assign_json_contract, json_contract
from studio.services.workflow_compiler import (
    WorkflowCompilerError,
    _compile_minimax_h3,
    compile_frozen_workflow,
)
from studio.services.workflow_versions import import_workflow_version, workflow_version_execution_snapshot
from studio.services.production_pipeline import (
    materialize_compiled_workflow_version,
    materialize_generation_task,
)
from studio.services.comfyui import ComfyUIError
from studio.services.provenance import persist_task_provenance, verified_task_manifest
from studio.services.task_queue import prepare_workflow


class GenericWorkflowSemanticCompilerTests(TestCase):
    def setUp(self):
        self.user = get_user_model().objects.create_user(
            username="semantic-compiler-owner",
            password="StrongPass123!",
        )
        self.project = SeriesProject.objects.create(
            owner=self.user,
            title="Semantic compiler",
            width=768,
            height=432,
            fps=24,
            continuity_seed=123,
        )
        self.provider = ProviderProfile(
            key="semantic-compiler-comfy",
            name="Semantic compiler Comfy",
            driver_type=ProviderProfile.DriverType.COMFYUI,
            modalities="image,video",
            operations="health,object_info,models,submit,poll,cancel,collect",
        )
        assign_json_contract(self.provider, "driver_config", {"base_url": "http://127.0.0.1:8188"})
        self.provider.save()
        self.profile = ModelProfileVersion(
            profile_key="semantic-compiler-model",
            version=1,
            provider=self.provider,
            model_identifier="configured-model.safetensors",
            modality=ModelProfileVersion.Modality.MULTIMODAL,
            status=ModelProfileVersion.Status.PUBLISHED,
        )
        assign_json_contract(self.profile, "specification", {
            "schema_version": 1,
            "capabilities": ["image.text_to_image", "video.image_to_video", "video.text_to_video"],
        })
        self.profile.save()
        self.artifact = ModelArtifact.objects.create(
            profile_version=self.profile,
            role=ModelArtifact.Role.CHECKPOINT,
            logical_name="base",
            filename="configured-model.safetensors",
            sha256="a" * 64,
            status=ModelArtifact.Status.AVAILABLE,
        )

    def _prompt(self, *, prompt_type="image", references=None):
        return SimpleNamespace(
            id="00000000-0000-0000-0000-000000000001",
            project=self.project,
            prompt_text="a quiet rain-soaked street",
            negative_prompt="blur",
            total_duration_ms=5000,
            include_audio=False,
            prompt_type=prompt_type,
            h3_mode="T2VA",
            model=self.profile.model_identifier,
            structured_payload={"reference_bindings": references or []},
        )

    def _version(self, *, output_kind="image", graph, parameters, bindings, outputs):
        template = WorkflowTemplate.objects.create(
            name=f"Semantic {output_kind}",
            slug=f"semantic-{output_kind}-{WorkflowTemplate.objects.count()}",
            output_kind=output_kind,
            provider="comfyui",
            model=self.profile.model_identifier,
            definition={},
            input_schema={},
            is_system=True,
            is_active=False,
        )
        output_node_id = str(outputs["node_id"])
        if "filename_prefix" in (graph.get(output_node_id, {}).get("inputs") or {}):
            bindings = [*bindings, {
                "semantic_key": "output.prefix",
                "node_id": output_node_id,
                "input_name": "filename_prefix",
                "value_type": "string",
            }]
        return import_workflow_version(
            template=template,
            graph=graph,
            input_contract={
                "schema_version": 2,
                "capability": (
                    "image.text_to_image" if output_kind == "image" else "video.image_to_video"
                ),
                "compiler": {"id": "generic.comfy", "version": 1},
                "parameters": {
                    **parameters,
                    "output.prefix": {"type": "string", "required": True},
                },
                "outputs": {"primary": outputs},
                "smoke_inputs": {
                    key: definition.get("default")
                    for key, definition in parameters.items()
                    if "default" in definition
                } | {"prompt.positive": "smoke"},
            },
            semantic_bindings=bindings,
            model_requirements=[{
                "role": ModelArtifact.Role.CHECKPOINT,
                "alias": "base",
                "artifact_id": str(self.artifact.id),
                "expected_filename": self.artifact.filename,
                "expected_sha256": self.artifact.sha256,
                "is_required": True,
            }] if any(
                definition.get("type") == "model_artifact"
                for definition in parameters.values()
            ) else [],
            provider=self.provider,
            model_profile_version=self.profile,
        )

    def test_t2i_fanout_model_alias_types_and_hash_are_deterministic(self):
        graph = {
            "1": {"class_type": "CheckpointLoaderSimple", "inputs": {"ckpt_name": "default.safetensors"}},
            "2": {"class_type": "CLIPTextEncode", "inputs": {"text": "", "clip": ["1", 1]}},
            "3": {"class_type": "CLIPTextEncode", "inputs": {"text": "", "clip": ["1", 1]}},
            "4": {"class_type": "EmptyLatentImage", "inputs": {"width": 512, "height": 512, "batch_size": 1}},
            "5": {"class_type": "KSampler", "inputs": {
                "model": ["1", 0], "positive": ["2", 0], "negative": ["3", 0],
                "latent_image": ["4", 0], "seed": 0, "steps": 10, "cfg": 5.0,
                "sampler_name": "euler", "scheduler": "normal", "denoise": 1.0,
            }},
            "6": {"class_type": "SaveImage", "inputs": {
                "images": "fixture", "filename_prefix": "output",
            }},
        }
        parameters = {
            "prompt.positive": {"type": "string", "required": True},
            "image.width": {"type": "integer", "required": True, "multiple_of": 8},
            "image.height": {"type": "integer", "required": True, "multiple_of": 8},
            "sampling.seed": {"type": "integer", "required": True, "min": 0},
            "sampling.steps": {"type": "integer", "required": True, "default": 20, "min": 1, "max": 50},
            "model.base.filename": {"type": "model_artifact", "requirement_alias": "base"},
        }
        bindings = [
            {"semantic_key": "model.base.filename", "node_id": "1", "input_name": "ckpt_name", "value_type": "model_artifact"},
            {"semantic_key": "prompt.positive", "node_id": "2", "input_name": "text", "value_type": "string"},
            {"semantic_key": "prompt.positive", "node_id": "3", "input_name": "text", "value_type": "string"},
            {"semantic_key": "image.width", "node_id": "4", "input_name": "width", "value_type": "integer"},
            {"semantic_key": "image.height", "node_id": "4", "input_name": "height", "value_type": "integer"},
            {"semantic_key": "sampling.seed", "node_id": "5", "input_name": "seed", "value_type": "integer"},
            {"semantic_key": "sampling.steps", "node_id": "5", "input_name": "steps", "value_type": "integer"},
        ]
        version = self._version(
            graph=graph,
            parameters=parameters,
            bindings=bindings,
            outputs={"node_id": "6", "kind": "image", "required": True},
        )
        snapshot = workflow_version_execution_snapshot(version)
        first = compile_frozen_workflow(
            workflow_snapshot=snapshot,
            prompt=self._prompt(),
            project=self.project,
        )
        second = compile_frozen_workflow(
            workflow_snapshot=snapshot,
            prompt=self._prompt(),
            project=self.project,
        )
        self.assertEqual(first.graph["1"]["inputs"]["ckpt_name"], self.artifact.filename)
        self.assertEqual(first.graph["2"]["inputs"]["text"], "a quiet rain-soaked street")
        self.assertEqual(first.graph["3"]["inputs"]["text"], "a quiet rain-soaked street")
        self.assertEqual(first.graph["4"]["inputs"]["width"], 768)
        self.assertEqual(first.graph["5"]["inputs"]["steps"], 20)
        self.assertEqual(first.graph_sha256, second.graph_sha256)
        self.assertEqual(first.resolved_semantics_sha256, second.resolved_semantics_sha256)

        with self.assertRaises(WorkflowCompilerError):
            compile_frozen_workflow(
                workflow_snapshot=snapshot,
                prompt=self._prompt(),
                project=self.project,
                overrides={"sampling.steps": 100},
            )
        with self.assertRaises(WorkflowCompilerError):
            compile_frozen_workflow(
                workflow_snapshot=snapshot,
                prompt=self._prompt(),
                project=self.project,
                overrides={"model.base.filename": "other.safetensors"},
            )

    def test_import_rejects_binding_that_overwrites_connection(self):
        with self.assertRaises(ValidationError):
            self._version(
                graph={
                    "1": {"class_type": "CheckpointLoaderSimple", "inputs": {"ckpt_name": "model"}},
                    "2": {"class_type": "CLIPTextEncode", "inputs": {"text": "", "clip": ["1", 1]}},
                    "3": {"class_type": "PreviewImage", "inputs": {"images": "fixture"}},
                },
                parameters={"prompt.positive": {"type": "string", "required": True}},
                bindings=[{
                    "semantic_key": "prompt.positive",
                    "node_id": "2",
                    "input_name": "clip",
                    "value_type": "string",
                }],
                outputs={"node_id": "3", "kind": "image", "required": True},
            )

    def test_i2v_asset_is_frozen_as_typed_token(self):
        asset = Asset.objects.create(
            owner=self.user,
            kind=Asset.Kind.REFERENCE,
            name="source image",
            mime_type="image/png",
            checksum="b" * 64,
        )
        version = self._version(
            output_kind="video",
            graph={
                "1": {"class_type": "LoadImage", "inputs": {"image": "fixture.png"}},
                "2": {"class_type": "SaveVideo", "inputs": {"video": "fixture", "filename_prefix": "output"}},
                "3": {"class_type": "EmptyLatentImage", "inputs": {
                    "width": 512, "height": 512, "batch_size": 1,
                }},
            },
            parameters={
                "asset.image.source": {"type": "asset_image", "required": True},
                "image.width": {"type": "integer", "required": True, "multiple_of": 8},
                "image.height": {"type": "integer", "required": True, "multiple_of": 8},
            },
            bindings=[
                {
                    "semantic_key": "asset.image.source",
                    "node_id": "1",
                    "input_name": "image",
                    "value_type": "asset_image",
                },
                {
                    "semantic_key": "image.width",
                    "node_id": "3",
                    "input_name": "width",
                    "value_type": "integer",
                },
                {
                    "semantic_key": "image.height",
                    "node_id": "3",
                    "input_name": "height",
                    "value_type": "integer",
                },
            ],
            outputs={"node_id": "2", "kind": "video", "required": True},
        )
        compiled = compile_frozen_workflow(
            workflow_snapshot=workflow_version_execution_snapshot(version),
            prompt=self._prompt(prompt_type="video", references=[{
                "asset_id": str(asset.id),
                "category": "first_frame",
                "checksum": asset.checksum,
            }]),
            project=self.project,
        )
        self.assertEqual(compiled.graph["1"]["inputs"]["image"], f"__AUTOPROMPT_ASSET__:{asset.id}")
        self.assertEqual(compiled.graph["3"]["inputs"]["width"], self.project.width)
        self.assertEqual(compiled.graph["3"]["inputs"]["height"], self.project.height)
        self.assertEqual(compiled.asset_bindings[0]["checksum"], asset.checksum)

        asset.checksum = "c" * 64
        asset.save(update_fields=("checksum", "updated_at"))
        with self.assertRaises(WorkflowCompilerError):
            compile_frozen_workflow(
                workflow_snapshot=workflow_version_execution_snapshot(version),
                prompt=self._prompt(prompt_type="video", references=[{
                    "asset_id": str(asset.id),
                    "category": "first_frame",
                    "checksum": "b" * 64,
                }]),
                project=self.project,
            )

    def test_worker_uses_frozen_asset_manifest_after_prompt_json_changes(self):
        with tempfile.TemporaryDirectory() as root:
            source = Path(root) / "source.png"
            source.write_bytes(b"semantic-asset-bytes")
            checksum = hashlib.sha256(source.read_bytes()).hexdigest()
            asset = Asset.objects.create(
                owner=None,
                kind=Asset.Kind.REFERENCE,
                name="global source",
                local_path=str(source),
                mime_type="image/png",
                checksum=checksum,
            )
            version = self._version(
                output_kind="video",
                graph={
                    "1": {"class_type": "LoadImage", "inputs": {"image": "fixture.png"}},
                    "2": {"class_type": "SaveVideo", "inputs": {"video": "fixture", "filename_prefix": "output"}},
                    "3": {"class_type": "CLIPTextEncode", "inputs": {"text": ""}},
                    "4": {"class_type": "EmptyLatentImage", "inputs": {
                        "width": 512, "height": 512, "batch_size": 1,
                    }},
                },
                parameters={
                    "asset.image.source": {"type": "asset_image", "required": True},
                    "prompt.positive": {"type": "string", "required": True},
                    "image.width": {"type": "integer", "required": True, "multiple_of": 8},
                    "image.height": {"type": "integer", "required": True, "multiple_of": 8},
                },
                bindings=[
                    {
                        "semantic_key": "asset.image.source",
                        "node_id": "1",
                        "input_name": "image",
                        "value_type": "asset_image",
                    },
                    {
                        "semantic_key": "prompt.positive",
                        "node_id": "3",
                        "input_name": "text",
                        "value_type": "string",
                    },
                    {
                        "semantic_key": "image.width",
                        "node_id": "4",
                        "input_name": "width",
                        "value_type": "integer",
                    },
                    {
                        "semantic_key": "image.height",
                        "node_id": "4",
                        "input_name": "height",
                        "value_type": "integer",
                    },
                ],
                outputs={"node_id": "2", "kind": "video", "required": True},
            )
            prompt = PromptArtifact.objects.create(
                project=self.project,
                created_by=self.user,
                prompt_type=PromptArtifact.PromptType.VIDEO,
                model=self.profile.model_identifier,
                prompt_text="animate the source",
                include_audio=False,
                total_duration_ms=5000,
                structured_payload={"reference_bindings": [{
                    "asset_id": str(asset.id),
                    "category": "first_frame",
                    "checksum": checksum,
                }]},
                content_hash="d" * 64,
            )
            snapshot = workflow_version_execution_snapshot(version)
            compiled = compile_frozen_workflow(
                workflow_snapshot=snapshot,
                prompt=prompt,
                project=self.project,
            )
            self.assertEqual(compiled.graph["4"]["inputs"]["width"], self.project.width)
            self.assertEqual(compiled.graph["4"]["inputs"]["height"], self.project.height)
            with transaction.atomic():
                workflow = materialize_compiled_workflow_version(
                    user=self.user,
                    workflow_version=version,
                    prompt=prompt,
                    compiled=compiled,
                    route_snapshot_sha256="e" * 64,
                    workflow_snapshot=snapshot,
                )
                task = materialize_generation_task(
                    user=self.user,
                    workflow=workflow,
                    priority=0,
                    max_attempts=1,
                )
            prompt.structured_payload = {"reference_bindings": []}
            prompt.save(update_fields=("structured_payload", "updated_at"))
            client = Mock()
            client.upload_input_file.return_value = {
                "name": f"{asset.id}.png",
                "subfolder": f"autoprompt/{self.user.id}/{task.id}",
            }
            with override_settings(ASSET_LIBRARY_ROOT=root):
                prepared = prepare_workflow(task, client)
            self.assertEqual(
                prepared["1"]["inputs"]["image"],
                f"autoprompt/{self.user.id}/{task.id}/{asset.id}.png",
            )
            self.assertEqual(prepared["4"]["inputs"]["width"], self.project.width)
            self.assertEqual(prepared["4"]["inputs"]["height"], self.project.height)

            original_template_slug = version.template.slug
            original_prompt_sha = hashlib.sha256(b"animate the source").hexdigest()
            prompt.prompt_text = "mutated live prompt must not enter provenance"
            prompt.content_hash = "9" * 64
            prompt.save(update_fields=("prompt_text", "content_hash", "updated_at"))
            template = version.template
            template.slug = f"mutated-{template.slug}"
            template.model = "mutated-live-model"
            template.save(update_fields=("slug", "model", "updated_at"))
            output = Path(root) / "semantic-result.mp4"
            output.write_bytes(b"frozen-semantic-output")
            task.outputs = [{
                "node_id": "2", "filename": output.name, "subfolder": "", "type": "output",
            }]
            with override_settings(COMFYUI_OUTPUT_DIR=root, MEDIA_ROOT=root):
                persist_task_provenance(task, task.outputs)
                generation = verified_task_manifest(task, 0)["generation"]
            self.assertEqual(generation["prompt_sha256"], original_prompt_sha)
            self.assertEqual(generation["template_slug"], original_template_slug)
            self.assertEqual(generation["model"], self.profile.model_identifier)

            asset.checksum = "f" * 64
            asset.save(update_fields=("checksum", "updated_at"))
            task.refresh_from_db()
            with override_settings(ASSET_LIBRARY_ROOT=root), self.assertRaises(ComfyUIError):
                prepare_workflow(task, client)

    def test_materialized_video_task_derives_planned_duration_from_bound_frames_and_fps(self):
        version = self._version(
            output_kind="video",
            graph={
                "1": {"class_type": "EmptyLatentImage", "inputs": {
                    "width": 512, "height": 512, "frames": 1, "fps": 1,
                }},
                "2": {"class_type": "SaveVideo", "inputs": {
                    "video": ["1", 0], "filename_prefix": "output",
                }},
            },
            parameters={
                "video.frames": {"type": "integer", "required": True, "min": 1},
                "video.fps": {"type": "integer", "required": True, "min": 1},
                "image.width": {"type": "integer", "required": True, "multiple_of": 8},
                "image.height": {"type": "integer", "required": True, "multiple_of": 8},
            },
            bindings=[
                {
                    "semantic_key": "video.frames", "node_id": "1",
                    "input_name": "frames", "value_type": "integer",
                },
                {
                    "semantic_key": "video.fps", "node_id": "1",
                    "input_name": "fps", "value_type": "integer",
                },
                {
                    "semantic_key": "image.width", "node_id": "1",
                    "input_name": "width", "value_type": "integer",
                },
                {
                    "semantic_key": "image.height", "node_id": "1",
                    "input_name": "height", "value_type": "integer",
                },
            ],
            outputs={"node_id": "2", "kind": "video", "required": True},
        )
        prompt = PromptArtifact.objects.create(
            project=self.project,
            created_by=self.user,
            prompt_type=PromptArtifact.PromptType.VIDEO,
            model=self.profile.model_identifier,
            prompt_text="five second station shot",
            include_audio=True,
            total_duration_ms=5000,
            content_hash="5" * 64,
        )
        snapshot = workflow_version_execution_snapshot(version)
        compiled = compile_frozen_workflow(
            workflow_snapshot=snapshot,
            prompt=prompt,
            project=self.project,
        )
        self.assertNotIn("video.duration_ms", compiled.resolved_semantics)
        self.assertEqual(compiled.resolved_semantics["video.frames"], 120)
        self.assertEqual(compiled.resolved_semantics["video.fps"], 24)

        with transaction.atomic():
            workflow = materialize_compiled_workflow_version(
                user=self.user,
                workflow_version=version,
                prompt=prompt,
                compiled=compiled,
                route_snapshot_sha256="e" * 64,
                workflow_snapshot=snapshot,
            )
            task = materialize_generation_task(
                user=self.user,
                workflow=workflow,
                priority=0,
                max_attempts=1,
            )

        self.assertEqual(task.effective_frames, 120)
        self.assertEqual(task.effective_duration_ms, 5000)
        self.assertEqual(task.capability_snapshot["requested_duration_ms"], 5000)

    def test_h3_paired_audio_manifest_hashes_the_exact_canonical_payload(self):
        video_checksum = hashlib.sha256(b"different-video-bytes").hexdigest()
        audio_checksum = hashlib.sha256(b"different-audio-bytes").hexdigest()
        self.assertNotEqual(video_checksum, audio_checksum)
        graph = {
            "2": {"class_type": "SaveVideo", "inputs": {
                "video": "fixture", "filename_prefix": "output",
            }},
        }
        prompt = self._prompt(prompt_type="video", references=[{
            "asset_id": "00000000-0000-0000-0000-000000000010",
            "category": "video",
            "mime_type": "video/mp4",
            "checksum": video_checksum,
            "audio_asset_id": "00000000-0000-0000-0000-000000000011",
            "audio_checksum": audio_checksum,
            "use_embedded_audio": False,
        }])
        snapshot = {
            "graph": graph,
            "input_contract": {"outputs": {"primary": {
                "node_id": "2", "kind": "video", "required": True,
            }}},
            "model_requirements": [],
            "execution_contract_sha256": "f" * 64,
            "model_identifier": "minimax-h3",
            "template": {"slug": "frozen-h3"},
        }
        with patch(
            "studio.services.workflow_compiler.build_workflow",
            return_value=(graph, {"PROMPT": prompt.prompt_text}, "unused"),
        ):
            compiled = _compile_minimax_h3(
                workflow_snapshot=snapshot,
                prompt=prompt,
                project=self.project,
            )
        self.assertEqual(compiled.asset_bindings[0]["checksum"], video_checksum)
        self.assertEqual(compiled.asset_bindings[0]["audio_checksum"], audio_checksum)
        self.assertEqual(
            compiled.asset_bindings_sha256,
            json_contract(compiled.asset_bindings).sha256,
        )
