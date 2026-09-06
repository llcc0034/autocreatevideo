import hashlib
from copy import deepcopy
from pathlib import Path
import tempfile
from types import SimpleNamespace
from unittest.mock import ANY, Mock, patch

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
from studio.services.execution_monitoring import execution_monitoring_contract_sha256
from studio.services.workflow_compiler import (
    WorkflowCompilerError,
    _compile_generic,
    build_frozen_workflow_runtime_overrides,
    compile_frozen_workflow,
    workflow_execution_contract,
)
from studio.services.workflow_versions import import_workflow_version, workflow_version_execution_snapshot
from studio.services.production_pipeline import (
    materialize_compiled_workflow_version,
    materialize_generation_task,
)
from studio.services.comfyui import ComfyUIError
from studio.services.workflow import WorkflowBuildError
from studio.services.workflow_semantics import (
    WorkflowSemanticError,
    resolve_workflow_semantics,
)
from studio.services.provenance import persist_task_provenance, verified_task_manifest
from studio.services.task_queue import prepare_workflow
from studio.tests.factories import conditioning_contract
from studio.tests.workflow_projector_fixtures import workflow_projector_reference


class GenericWorkflowSemanticCompilerTests(TestCase):
    def setUp(self):
        self.user = get_user_model().objects.create_user(
            username="semantic-compiler-owner",
            password="StrongPass123!",
        )
        self.project = SeriesProject.objects.create(language="Chinese", aspect_ratio="9:16",
            owner=self.user,
            title="Semantic compiler",
            width=768,
            height=432,
            fps=24,
            default_shot_duration_ms=5000,
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

    def test_runtime_identity_overrides_dispatch_by_frozen_compiler_contract(self):
        snapshot = {
            "input_contract": {"compiler": {"id": "generic.comfy", "version": 1}},
            "compiler_id": "generic.comfy",
            "compiler_version": 1,
            "semantic_bindings": [
                {"semantic_key": key}
                for key in (
                    "image.width", "image.height", "sampling.seed", "output.prefix",
                )
            ],
        }
        prompt = SimpleNamespace(total_duration_ms=1_000)

        overrides = build_frozen_workflow_runtime_overrides(
            workflow_snapshot=snapshot,
            prompt=prompt,
            width=352,
            height=608,
            seed=17,
            output_prefix="production/run/shot",
        )

        self.assertEqual(overrides, {
            "image.width": 352,
            "image.height": 608,
            "sampling.seed": 17,
            "output.prefix": "production/run/shot",
        })
        snapshot["semantic_bindings"] = snapshot["semantic_bindings"][:-1]
        with self.assertRaisesRegex(WorkflowCompilerError, "output.prefix"):
            build_frozen_workflow_runtime_overrides(
                workflow_snapshot=snapshot,
                prompt=prompt,
                width=352,
                height=608,
                seed=17,
                output_prefix="production/run/shot",
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
            conditioning_mode="T2VA",
            model=self.profile.model_identifier,
            structured_payload={"reference_bindings": references or []},
        )

    def _version(
        self,
        *,
        output_kind="image",
        graph,
        parameters,
        bindings,
        outputs,
        conditioning=None,
    ):
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
        width_nodes = {
            str(row["node_id"]) for row in bindings
            if row.get("semantic_key") == "image.width"
        }
        height_nodes = {
            str(row["node_id"]) for row in bindings
            if row.get("semantic_key") == "image.height"
        }
        resolution_nodes = sorted(width_nodes & height_nodes)
        dimension_multiples = {
            definition.get("multiple_of")
            for key, definition in parameters.items()
            if key in {"image.width", "image.height"}
        } - {None}
        if resolution_nodes and len(dimension_multiples) != 1:
            raise AssertionError(
                "semantic workflow fixture must declare one exact dimension multiple"
            )
        return import_workflow_version(
            template=template,
            graph=graph,
            input_contract={
                **workflow_projector_reference(),
                "schema_version": 2,
                "workflow_mode": "semantic",
                "capability": (
                    "image.text_to_image" if output_kind == "image" else "video.image_to_video"
                ),
                "compiler": {"id": "generic.comfy", "version": 1},
                **({
                    "conditioning_contract": conditioning or conditioning_contract(
                        "T2VA", input_kind="classified_references", first_frame=(0, 1),
                    ),
                    "static_semantics": {"audio.enabled": False},
                } if output_kind == "video" else {}),
                **({
                    "resolution_strategy": {
                        "mode": "explicit_width_height",
                        "node_ids": resolution_nodes,
                    },
                    "required_dimension_multiple": next(iter(dimension_multiples)),
                } if resolution_nodes else {}),
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

    @staticmethod
    def _frozen_video_prompt_payload(version, *, mode, **extra):
        contract = version.input_contract["conditioning_contract"]
        mode_contract = next(
            item for item in contract["modes"] if item["key"] == mode
        )
        return {
            "effective_conditioning_mode": mode,
            "conditioning_contract": contract,
            "conditioning_contract_sha256": json_contract(contract).sha256,
            "conditioning_mode_contract": mode_contract,
            "conditioning_mode_contract_sha256": json_contract(
                mode_contract,
            ).sha256,
            **extra,
        }

    def test_batch_size_requires_a_frozen_contract_value_or_override(self):
        with self.assertRaisesRegex(
            WorkflowSemanticError,
            "缺少必填 semantic: generation.batch_size",
        ):
            resolve_workflow_semantics(
                graph={"1": {"class_type": "Mock", "inputs": {"batch_size": 1}}},
                contract={
                    "parameters": {
                        "generation.batch_size": {"type": "integer", "required": True},
                    },
                },
                bindings=[{
                    "semantic_key": "generation.batch_size",
                    "node_id": "1",
                    "input_name": "batch_size",
                    "value_type": "integer",
                    "is_required": True,
                }],
                requirements=[],
                prompt=self._prompt(),
                project=self.project,
            )

    def test_t2i_fanout_model_alias_types_and_hash_are_deterministic(self):
        self.artifact.filename = "vendor/configured-model.safetensors"
        self.artifact.save(update_fields=("filename", "updated_at"))
        exact_model_option = "vendor\\configured-model.safetensors"
        graph = {
            "1": {"class_type": "CheckpointLoaderSimple", "inputs": {"ckpt_name": exact_model_option}},
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
        self.assertEqual(first.graph["1"]["inputs"]["ckpt_name"], exact_model_option)
        self.assertEqual(
            first.resolved_semantics["model.base.filename"],
            exact_model_option,
        )
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

    def test_compile_rejects_frozen_dimension_default_outside_lattice(self):
        version = self._version(
            graph={
                "4": {
                    "class_type": "EmptyLatentImage",
                    "inputs": {"width": 512, "height": 512, "batch_size": 1},
                },
                "6": {
                    "class_type": "SaveImage",
                    "inputs": {"images": "fixture", "filename_prefix": "output"},
                },
            },
            parameters={
                "image.width": {
                    "type": "integer", "required": True, "multiple_of": 8,
                    "default": 512, "use_source_default": True,
                },
                "image.height": {
                    "type": "integer", "required": True, "multiple_of": 8,
                    "default": 512, "use_source_default": True,
                },
            },
            bindings=[
                {
                    "semantic_key": "image.width", "node_id": "4",
                    "input_name": "width", "value_type": "integer",
                },
                {
                    "semantic_key": "image.height", "node_id": "4",
                    "input_name": "height", "value_type": "integer",
                },
            ],
            outputs={"node_id": "6", "kind": "image", "required": True},
        )
        snapshot = workflow_version_execution_snapshot(version)
        snapshot["input_contract"]["parameters"]["image.width"]["default"] = 513
        snapshot["graph"]["4"]["inputs"]["width"] = 513
        snapshot["graph_sha256"] = json_contract(snapshot["graph"]).sha256
        snapshot["input_contract_sha256"] = json_contract(
            snapshot["input_contract"],
        ).sha256
        execution_payload, execution_sha256 = workflow_execution_contract(
            compiler_id=snapshot["compiler_id"],
            compiler_version=snapshot["compiler_version"],
            graph_sha256=snapshot["graph_sha256"],
            input_contract_sha256=snapshot["input_contract_sha256"],
            bindings=snapshot["semantic_bindings"],
            requirements=snapshot["model_requirements"],
        )
        snapshot["semantic_bindings_sha256"] = execution_payload[
            "semantic_bindings_sha256"
        ]
        snapshot["model_requirements_sha256"] = execution_payload[
            "model_requirements_sha256"
        ]
        snapshot["execution_contract_sha256"] = execution_sha256

        with self.assertRaisesRegex(WorkflowCompilerError, "尺寸倍数合同"):
            compile_frozen_workflow(
                workflow_snapshot=snapshot,
                prompt=self._prompt(),
                project=self.project,
            )

    def test_model_widget_identity_requires_canonical_filename_and_sha256(self):
        parameters = {
            "model.base.filename": {
                "type": "model_artifact", "requirement_alias": "base",
            },
            "image.width": {"type": "integer", "required": True, "multiple_of": 8},
            "image.height": {"type": "integer", "required": True, "multiple_of": 8},
        }
        bindings = [
            {
                "semantic_key": "model.base.filename",
                "node_id": "1",
                "input_name": "ckpt_name",
                "value_type": "model_artifact",
            },
            {
                "semantic_key": "image.width", "node_id": "3",
                "input_name": "width", "value_type": "integer",
            },
            {
                "semantic_key": "image.height", "node_id": "3",
                "input_name": "height", "value_type": "integer",
            },
        ]
        version = self._version(
            graph={
                "1": {"class_type": "CheckpointLoaderSimple", "inputs": {
                    "ckpt_name": "different-model.safetensors",
                }},
                "2": {"class_type": "SaveImage", "inputs": {
                    "images": "fixture", "filename_prefix": "output",
                }},
                "3": {"class_type": "EmptyLatentImage", "inputs": {
                    "width": 512, "height": 512, "batch_size": 1,
                }},
            },
            parameters=parameters,
            bindings=bindings,
            outputs={"node_id": "2", "kind": "image", "required": True},
        )
        with self.assertRaisesRegex(WorkflowCompilerError, "expected_filename"):
            compile_frozen_workflow(
                workflow_snapshot=workflow_version_execution_snapshot(version),
                prompt=self._prompt(),
                project=self.project,
            )

        self.artifact.sha256 = ""
        self.artifact.save(update_fields=("sha256", "updated_at"))
        version = self._version(
            graph={
                "1": {"class_type": "CheckpointLoaderSimple", "inputs": {
                    "ckpt_name": self.artifact.filename,
                }},
                "2": {"class_type": "SaveImage", "inputs": {
                    "images": "fixture", "filename_prefix": "output",
                }},
                "3": {"class_type": "EmptyLatentImage", "inputs": {
                    "width": 512, "height": 512, "batch_size": 1,
                }},
            },
            parameters=parameters,
            bindings=bindings,
            outputs={"node_id": "2", "kind": "image", "required": True},
        )
        with self.assertRaisesRegex(WorkflowCompilerError, "expected_sha256"):
            compile_frozen_workflow(
                workflow_snapshot=workflow_version_execution_snapshot(version),
                prompt=self._prompt(),
                project=self.project,
            )

    def test_snapshot_graph_tampering_is_rejected_before_model_resolution(self):
        version = self._version(
            graph={
                "1": {"class_type": "CheckpointLoaderSimple", "inputs": {
                    "ckpt_name": self.artifact.filename,
                }},
                "2": {"class_type": "SaveImage", "inputs": {
                    "images": "fixture", "filename_prefix": "output",
                }},
                "3": {"class_type": "EmptyLatentImage", "inputs": {
                    "width": 512, "height": 512, "batch_size": 1,
                }},
            },
            parameters={
                "model.base.filename": {
                    "type": "model_artifact", "requirement_alias": "base",
                },
                "image.width": {"type": "integer", "required": True, "multiple_of": 8},
                "image.height": {"type": "integer", "required": True, "multiple_of": 8},
            },
            bindings=[
                {
                    "semantic_key": "model.base.filename",
                    "node_id": "1",
                    "input_name": "ckpt_name",
                    "value_type": "model_artifact",
                },
                {
                    "semantic_key": "image.width", "node_id": "3",
                    "input_name": "width", "value_type": "integer",
                },
                {
                    "semantic_key": "image.height", "node_id": "3",
                    "input_name": "height", "value_type": "integer",
                },
            ],
            outputs={"node_id": "2", "kind": "image", "required": True},
        )
        snapshot = workflow_version_execution_snapshot(version)
        snapshot["graph"]["1"]["inputs"]["ckpt_name"] = "tampered.safetensors"
        with self.assertRaisesRegex(WorkflowCompilerError, "graph hash"):
            compile_frozen_workflow(
                workflow_snapshot=snapshot,
                prompt=self._prompt(),
                project=self.project,
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
                "asset.image.source": {
                    "type": "asset_image", "required": True,
                    "input_slot": 1, "asset_category": "image",
                },
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
                "input_slot": 1,
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
                    "input_slot": 1,
                    "checksum": "b" * 64,
                }]),
                project=self.project,
            )

    def test_three_image_slots_bind_by_explicit_input_slot_not_category_or_list_order(self):
        assets = [
            Asset.objects.create(
                owner=self.user,
                kind=Asset.Kind.REFERENCE,
                name=f"slot {index}",
                mime_type="image/png",
                checksum=character * 64,
            )
            for index, character in enumerate(("b", "c", "d"), start=1)
        ]
        parameters = {
            **{
                f"asset.image.source{'' if index == 1 else f'_{index}'}": {
                    "type": "asset_image",
                    "required": True,
                    "input_slot": index,
                    "asset_category": "image",
                }
                for index in range(1, 4)
            },
            "image.width": {"type": "integer", "required": True, "multiple_of": 8},
            "image.height": {"type": "integer", "required": True, "multiple_of": 8},
        }
        bindings = [
            {
                "semantic_key": f"asset.image.source{'' if index == 1 else f'_{index}'}",
                "node_id": str(index),
                "input_name": "image",
                "value_type": "asset_image",
            }
            for index in range(1, 4)
        ] + [
            {
                "semantic_key": "image.width", "node_id": "4",
                "input_name": "width", "value_type": "integer",
            },
            {
                "semantic_key": "image.height", "node_id": "4",
                "input_name": "height", "value_type": "integer",
            },
        ]
        version = self._version(
            graph={
                "1": {"class_type": "LoadImage", "inputs": {"image": "one.png"}},
                "2": {"class_type": "LoadImage", "inputs": {"image": "two.png"}},
                "3": {"class_type": "LoadImage", "inputs": {"image": "three.png"}},
                "4": {"class_type": "EmptyLatentImage", "inputs": {
                    "width": 512, "height": 512, "batch_size": 1,
                }},
                "5": {"class_type": "SaveImage", "inputs": {
                    "images": "fixture", "filename_prefix": "output",
                }},
            },
            parameters=parameters,
            bindings=bindings,
            outputs={"node_id": "5", "kind": "image", "required": True},
        )
        references = [
            {
                "asset_id": str(assets[index - 1].id),
                "category": category,
                "input_slot": index,
                "checksum": assets[index - 1].checksum,
            }
            for index, category in ((3, "prop"), (1, "scene"), (2, "character"))
        ]
        compiled = compile_frozen_workflow(
            workflow_snapshot=workflow_version_execution_snapshot(version),
            prompt=self._prompt(references=references),
            project=self.project,
        )

        self.assertEqual(
            [compiled.graph[str(index)]["inputs"]["image"] for index in range(1, 4)],
            [f"__AUTOPROMPT_ASSET__:{asset.id}" for asset in assets],
        )
        self.assertEqual(
            [row["input_slot"] for row in compiled.asset_bindings],
            [1, 2, 3],
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
                    "asset.image.source": {
                        "type": "asset_image", "required": True,
                        "input_slot": 1, "asset_category": "image",
                    },
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
            prompt = PromptArtifact.objects.create(is_multi_shot=False,
                project=self.project,
                created_by=self.user,
                prompt_type=PromptArtifact.PromptType.VIDEO,
                model=self.profile.model_identifier,
                conditioning_mode="T2VA",
                prompt_text="animate the source",
                include_audio=False,
                total_duration_ms=5000,
                structured_payload=self._frozen_video_prompt_payload(
                    version,
                    mode="T2VA",
                    reference_bindings=[{
                    "asset_id": str(asset.id),
                    "category": "first_frame",
                    "input_slot": 1,
                    "checksum": checksum,
                    }],
                ),
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
        prompt = PromptArtifact.objects.create(is_multi_shot=False,
            project=self.project,
            created_by=self.user,
            prompt_type=PromptArtifact.PromptType.VIDEO,
            model=self.profile.model_identifier,
            conditioning_mode="T2VA",
            prompt_text="five second station shot",
            include_audio=True,
            total_duration_ms=5000,
            structured_payload=self._frozen_video_prompt_payload(
                version,
                mode="T2VA",
            ),
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

        self.project.fps = 0
        with self.assertRaisesRegex(WorkflowCompilerError, "video.fps"):
            compile_frozen_workflow(
                workflow_snapshot=snapshot,
                prompt=prompt,
                project=self.project,
            )
        self.project.fps = 24

        frozen_prompt_payload = deepcopy(prompt.structured_payload)
        prompt.structured_payload = {
            **frozen_prompt_payload,
            "conditioning_contract_sha256": "f" * 64,
        }
        with transaction.atomic(), self.assertRaisesRegex(
            WorkflowBuildError,
            "冻结 conditioning contract 不一致",
        ):
            materialize_compiled_workflow_version(
                user=self.user,
                workflow_version=version,
                prompt=prompt,
                compiled=compiled,
                route_snapshot_sha256="e" * 64,
                workflow_snapshot=snapshot,
            )
        prompt.structured_payload = frozen_prompt_payload

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
        self.assertEqual(
            task.capability_snapshot["execution_monitoring_sha256"],
            execution_monitoring_contract_sha256(
                task.capability_snapshot["execution_monitoring"],
            ),
        )

    def test_fictional_video_route_uses_frozen_preprocessing_adapter_not_model_name(self):
        with tempfile.TemporaryDirectory() as root:
            source = Path(root) / "orbit-reference.mp4"
            source.write_bytes(b"fictional-orbit-reference")
            checksum = hashlib.sha256(source.read_bytes()).hexdigest()
            asset = Asset.objects.create(
                owner=None,
                kind=Asset.Kind.REFERENCE_VIDEO,
                name="Orbit reference",
                local_path=str(source),
                mime_type="video/mp4",
                checksum=checksum,
            )
            orbit_contract = conditioning_contract(
                "OrbitRefs",
                input_kind="classified_references",
                reference_videos=(0, 1),
                video_preprocessing_adapter="cfr_video_v1",
                video_target_fps=25,
            )
            version = self._version(
                output_kind="video",
                graph={
                    "1": {"class_type": "LoadVideo", "inputs": {"video": "fixture.mp4"}},
                    "2": {"class_type": "SaveVideo", "inputs": {
                        "video": ["1", 0], "filename_prefix": "output",
                    }},
                    "3": {"class_type": "EmptyLatentImage", "inputs": {
                        "width": 512, "height": 512, "batch_size": 1,
                    }},
                },
                parameters={
                    "asset.video.source": {
                        "type": "asset_video", "required": True,
                        "input_slot": 1, "asset_category": "video",
                    },
                    "image.width": {
                        "type": "integer", "required": True, "multiple_of": 8,
                    },
                    "image.height": {
                        "type": "integer", "required": True, "multiple_of": 8,
                    },
                },
                bindings=[
                    {
                        "semantic_key": "asset.video.source",
                        "node_id": "1",
                        "input_name": "video",
                        "value_type": "asset_video",
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
                conditioning=orbit_contract,
            )
            prompt = PromptArtifact.objects.create(is_multi_shot=False,
                project=self.project,
                created_by=self.user,
                prompt_type=PromptArtifact.PromptType.VIDEO,
                model="Fictional Orbit Video Engine",
                conditioning_mode="OrbitRefs",
                prompt_text="orbiting camera reference",
                include_audio=False,
                total_duration_ms=1200,
                structured_payload=self._frozen_video_prompt_payload(
                    version,
                    mode="OrbitRefs",
                    reference_bindings=[{
                    "asset_id": str(asset.id),
                    "category": "video",
                    "input_slot": 1,
                    "checksum": checksum,
                    "use_embedded_audio": False,
                    }],
                ),
                content_hash="7" * 64,
            )
            snapshot = workflow_version_execution_snapshot(version)
            compiled = compile_frozen_workflow(
                workflow_snapshot=snapshot,
                prompt=prompt,
                project=self.project,
            )
            with transaction.atomic():
                workflow = materialize_compiled_workflow_version(
                    user=self.user,
                    workflow_version=version,
                    prompt=prompt,
                    compiled=compiled,
                    route_snapshot_sha256="8" * 64,
                    workflow_snapshot=snapshot,
                )
                task = materialize_generation_task(
                    user=self.user,
                    workflow=workflow,
                    priority=0,
                    max_attempts=1,
                )

            class UploadClient:
                def upload_input_file(self, path, *, subfolder, filename=None):
                    return {"name": filename, "subfolder": subfolder, "type": "input"}

            plan = SimpleNamespace(
                target_fps=25,
                effective_duration_seconds=1.2,
            )

            def stage_video(_source, output, **_kwargs):
                Path(output).write_bytes(b"contract-normalized-video")
                return {
                    "staging_version": 2,
                    "media_role": "reference_video",
                    "preprocessing_adapter_key": "cfr_video_v1",
                    "effective_fps": 25,
                    "effective_duration_ms": 1200,
                    "cfr_guaranteed": True,
                    "original_asset_preserved": True,
                }

            with (
                override_settings(ASSET_LIBRARY_ROOT=root),
                patch(
                    "studio.services.task_queue.inspect_reference_video",
                    return_value=plan,
                ) as inspect_video,
                patch(
                    "studio.services.task_queue.normalize_reference_video",
                    side_effect=stage_video,
                ),
            ):
                prepared = prepare_workflow(task, UploadClient())

            inspect_video.assert_called_once_with(
                source,
                target_fps=25,
                minimum_duration_ms=2000,
                maximum_duration_ms=15000,
                security_max_fps=480,
                security_max_duration_ms=86_400_000,
                media_profile=ANY,
            )
            self.assertNotIn("__AUTOPROMPT_ASSET__", str(prepared))
            task.refresh_from_db()
            self.assertEqual(task.staged_inputs[0]["effective_fps"], 25)
            self.assertEqual(
                task.staged_inputs[0]["conditioning_mode_contract_sha256"],
                task.capability_snapshot["conditioning_mode_contract_sha256"],
            )
            self.assertEqual(
                task.staged_inputs[0]["media_processing_profile_sha256"],
                task.capability_snapshot["reference_media_profile_sha256"],
            )
            self.assertNotIn("minimax", str(task.capability_snapshot).casefold())

            task.capability_snapshot["conditioning_mode_contract_sha256"] = "0" * 64
            task.save(update_fields=("capability_snapshot", "updated_at"))
            with override_settings(ASSET_LIBRARY_ROOT=root), self.assertRaisesRegex(
                ComfyUIError, "conditioning mode contract",
            ):
                prepare_workflow(task, UploadClient())



class GenericClassifiedWorkflowCompilerTests(TestCase):
    def setUp(self):
        self.user = get_user_model().objects.create_user(
            username="h3-v2-owner", password="StrongPass123!",
        )
        self.project = SeriesProject.objects.create(language="Chinese", aspect_ratio="9:16",
            owner=self.user,
            title="H3 dynamic Ref2VA",
            width=416,
            height=256,
            fps=24,
            default_shot_duration_ms=5000,
            continuity_seed=7,
        )

    @staticmethod
    def _snapshot():
        graph = {
            "1": {"class_type": "UNETLoader", "inputs": {"unet_name": "MiniMax_H3_Ref2VA_pruned_int8_convrot.safetensors", "weight_dtype": "default"}},
            "2": {"class_type": "LoraLoaderBypassModelOnly", "inputs": {"model": ["1", 0], "lora_name": "minimax_h3_turbo_4step_ckpt600_ema_V4.safetensors", "strength_model": 1.0}},
            "3": {"class_type": "CLIPLoader", "inputs": {"clip_name": "qwen3vl_32b_minimax_h3_int8_convrot.safetensors", "type": "minimax", "device": "default"}},
            "4": {"class_type": "VAELoader", "inputs": {"vae_name": "minimax_h3_video_vae_fp16.safetensors"}},
            "5": {"class_type": "VAELoader", "inputs": {"vae_name": "minimax_h3_audio_vae_fp32.safetensors"}},
            "6": {"class_type": "MiniMaxH3AudioConditioningT8", "inputs": {"clip": ["3", 0], "video_vae": ["4", 0], "audio_vae": ["5", 0], "prompt": "managed prompt", "width": 416, "height": 256, "length": 124, "task_type": "Ref2VA", "audio_mode": "native", "audio_denoise_strength": 1.0, "add_source_as_reference": False, "prompt_primary_audio_ordinal": 0, "strict_prompt_tags": True, "ref_image_size": "match", "reference_video_policy": "official_2_to_15s"}},
            "7": {"class_type": "MiniMaxH3DualClockSamplerT8", "inputs": {"model": ["2", 0], "av_latent": ["6", 1], "steps": 4, "shift_video": 12.0, "shift_audio": 3.0}},
            "8": {"class_type": "BasicGuider", "inputs": {"model": ["7", 0], "conditioning": ["6", 0]}},
            "9": {"class_type": "RandomNoise", "inputs": {"noise_seed": 1}},
            "10": {"class_type": "SamplerCustomAdvanced", "inputs": {"noise": ["9", 0], "guider": ["8", 0], "sampler": ["7", 1], "sigmas": ["7", 2], "latent_image": ["6", 1]}},
            "11": {"class_type": "MiniMaxH3AVDecodeT8", "inputs": {"av_latent": ["10", 0], "video_vae": ["4", 0], "audio_vae": ["5", 0]}},
            "12": {"class_type": "CreateVideo", "inputs": {"images": ["11", 0], "audio": ["11", 1], "fps": 24.0, "bit_depth": 8}},
            "13": {"class_type": "SaveVideo", "inputs": {"video": ["12", 0], "filename_prefix": "output", "format": "auto", "codec": "auto"}},
        }
        parameters = {
            "prompt.positive": {"type": "string", "required": True},
            "image.width": {"type": "integer", "required": True, "min": 32, "max": 2048, "multiple_of": 32},
            "image.height": {"type": "integer", "required": True, "min": 32, "max": 2048, "multiple_of": 32},
            "video.frames": {"type": "integer", "required": True, "min": 5, "max": 3600},
            "video.fps": {"type": "integer", "required": True, "min": 24, "max": 24, "default": 24},
            "sampling.seed": {"type": "integer", "required": True, "min": 0},
            "parameter.h3.steps": {"type": "integer", "required": True, "default": 4, "min": 4, "max": 4},
            "output.prefix": {"type": "string", "required": True},
        }
        bindings = [
            {"semantic_key": "prompt.positive", "node_id": "6", "input_name": "prompt", "value_type": "string", "is_required": True},
            {"semantic_key": "image.width", "node_id": "6", "input_name": "width", "value_type": "integer", "is_required": True},
            {"semantic_key": "image.height", "node_id": "6", "input_name": "height", "value_type": "integer", "is_required": True},
            {"semantic_key": "video.frames", "node_id": "6", "input_name": "length", "value_type": "integer", "is_required": True},
            {"semantic_key": "video.fps", "node_id": "12", "input_name": "fps", "value_type": "integer", "is_required": True},
            {"semantic_key": "sampling.seed", "node_id": "9", "input_name": "noise_seed", "value_type": "integer", "is_required": True},
            {"semantic_key": "parameter.h3.steps", "node_id": "7", "input_name": "steps", "value_type": "integer", "is_required": True},
            {"semantic_key": "output.prefix", "node_id": "13", "input_name": "filename_prefix", "value_type": "string", "is_required": True},
        ]
        graph_sha = json_contract(graph).sha256
        contract = {
            "schema_version": 3,
            "compiler": {"id": "generic.comfy", "version": 3},
            "capability": "video.reference_to_video",
            "workflow_mode": "reference_to_video",
            "conditioning_contract": conditioning_contract(
                "Ref2VA",
                input_kind="classified_references",
                requires_bible=True,
                first_frame=(0, 1),
                entity_images=(0, 9),
                reference_videos=(0, 3),
                paired_video_audios=(0, 3),
                reference_audios=(0, 3),
            ),
            "prompt_template_keys": ["configured.empty", "configured.references"],
            "resolution_strategy": {"mode": "explicit_width_height", "node_ids": ["6"]},
            "required_dimension_multiple": 32,
            "static_semantics": {"audio.enabled": True},
            "parameters": parameters,
            "outputs": {"primary": {"node_id": "13", "kind": "video", "required": True}},
        }
        return {
            "graph": graph,
            "graph_sha256": graph_sha,
            "input_contract": contract,
            "input_contract_sha256": json_contract(contract).sha256,
            "compiler_id": "generic.comfy",
            "compiler_version": 3,
            "semantic_bindings": bindings,
            "semantic_bindings_sha256": json_contract(sorted(bindings, key=lambda row: (row["semantic_key"], row["node_id"], row["input_name"]))).sha256,
            "model_requirements": [],
            "model_requirements_sha256": json_contract([]).sha256,
            "execution_contract_sha256": "e" * 64,
            "model_identifier": "minimax-h3-test",
            "template": {"slug": "h3-dynamic-v2"},
        }

    def _prompt(self, references, *, key, duration_ms=5000):
        labels = [
            label
            for item in references
            for label in (item.get("input_label"), item.get("audio_input_label"))
            if label
        ]
        return SimpleNamespace(
            id="00000000-0000-0000-0000-000000000101",
            project=self.project,
            prompt_text="dynamic smoke " + " ".join(labels),
            negative_prompt="",
            total_duration_ms=duration_ms,
            include_audio=True,
            prompt_type="video",
            conditioning_mode="Ref2VA",
            model="minimax-h3",
            structured_payload={
                "reference_bindings": references,
                "prompt_template_key": key,
                "effective_conditioning_mode": "Ref2VA",
            },
        )

    def _compile(
        self,
        references,
        *,
        key="configured.references",
        duration_ms=5000,
        frames=124,
        minimum_duration_ms=2000,
    ):
        from studio.services.workflow_classified_references import attach_plan
        from studio.services.workflow_projectors import current_projector_catalog
        snapshot = self._snapshot()
        projector = next(row for row in current_projector_catalog() if row.key == "minimax_h3.classified_ref2va")
        definition = deepcopy(projector.definition)
        definition["projection"]["config"]["prompt_template_keys"] = ["configured.empty", "configured.references"]
        snapshot["input_contract"]["parameters"]["video.frames"].update(alignment_modulus=17, alignment_remainder=5)
        attach_plan(snapshot["input_contract"], definition, snapshot["graph"], {"conditioning_contract": snapshot["input_contract"]["conditioning_contract"]})
        return _compile_generic(
            _compiler_version=3,
            workflow_snapshot=snapshot,
            prompt=self._prompt(references, key=key, duration_ms=duration_ms),
            project=self.project,
            overrides={
                "image.width": 416,
                "image.height": 256,
                "video.frames": frames,
                "video.fps": 24,
                "sampling.seed": 1,
                "parameter.h3.steps": 4,
                "output.prefix": "autoprompt/smoke/h3-v2",
            },
            video_duration_policy={
                "minimum_duration_ms": minimum_duration_ms,
                "maximum_duration_ms": 15000,
            },
        )

    def test_one_second_duration_accepts_22_frames_and_rejects_old_39_frame_ceiling(self):
        compiled = self._compile(
            [],
            key="configured.empty",
            duration_ms=1000,
            frames=22,
            minimum_duration_ms=1000,
        )
        self.assertEqual(compiled.resolved_semantics["video.frames"], 22)
        with self.assertRaisesRegex(WorkflowCompilerError, "冻结分镜时长不一致"):
            self._compile(
                [],
                key="configured.empty",
                duration_ms=1000,
                frames=39,
                minimum_duration_ms=1000,
            )

    def test_zero_reference_uses_zero_key_and_creates_no_loader(self):
        compiled = self._compile([], key="configured.empty")
        self.assertFalse(compiled.asset_bindings)
        self.assertFalse(any(
            node["class_type"] in {"LoadImage", "LoadVideo", "LoadAudio"}
            for node in compiled.graph.values()
        ))

    def test_compiles_sparse_paired_audio_and_standalone_audio(self):
        refs = [
            {"asset_id": "00000000-0000-0000-0000-000000000111", "category": "character", "entity_id": "00000000-0000-0000-0000-000000000211", "entity_name": "Hero", "input_slot": 1, "input_label": "<Picture 1>", "checksum": "1" * 64, "mime_type": "image/png"},
            {"asset_id": "00000000-0000-0000-0000-000000000112", "category": "video", "input_slot": 1, "input_label": "<Video 1>", "checksum": "2" * 64, "mime_type": "video/mp4", "purpose": "motion_camera_reference", "use_embedded_audio": False, "audio_input_label": None},
            {"asset_id": "00000000-0000-0000-0000-000000000113", "category": "video", "input_slot": 2, "input_label": "<Video 2>", "checksum": "3" * 64, "mime_type": "video/mp4", "purpose": "video_editing", "use_embedded_audio": False, "audio_asset_id": "00000000-0000-0000-0000-000000000114", "audio_checksum": "4" * 64, "audio_input_label": "<Audio 1>", "audio_purpose": "audio_reference"},
            {"asset_id": "00000000-0000-0000-0000-000000000115", "category": "audio", "input_slot": 1, "input_label": "<Audio 2>", "checksum": "5" * 64, "mime_type": "audio/wav", "purpose": "audio_reuse"},
        ]
        compiled = self._compile(refs)
        conditioner = compiled.graph["6"]["inputs"]
        self.assertIn("ref_images.ref_image_0", conditioner)
        self.assertIn("ref_videos.ref_video_0", conditioner)
        self.assertIn("ref_videos.ref_video_1", conditioner)
        self.assertNotIn("ref_video_audios.ref_video_audio_0", conditioner)
        self.assertIn("ref_video_audios.ref_video_audio_1", conditioner)
        self.assertIn("ref_audios.ref_audio_0", conditioner)
        self.assertEqual(compiled.resolved_semantics["video.frames"], 124)
        self.assertEqual(compiled.compiler_version, 3)

    def test_rejects_prompt_key_outside_configured_allowlist(self):
        with self.assertRaises(WorkflowCompilerError):
            self._compile([], key="not-configured")
