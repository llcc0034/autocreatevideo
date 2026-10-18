from copy import deepcopy

from django.contrib.auth import get_user_model
from django.core.exceptions import ValidationError
from django.test import TestCase

from studio.models import (
    Asset,
    ModelArtifact,
    ModelProfileVersion,
    ProviderProfile,
    SeriesProject,
    WorkflowTemplate,
)
from studio.services.json_contracts import assign_json_contract, json_contract
from studio.services.workflow_compiler import (
    WorkflowCompilerError,
    compile_frozen_workflow_values,
    workflow_execution_contract,
)
from studio.services.workflow_versions import import_workflow_version, workflow_version_execution_snapshot
from studio.services.workflow_semantics import (
    WorkflowSemanticError,
    resolve_workflow_semantic_values,
)
from studio.tests.factories import conditioning_contract
from studio.tests.workflow_projector_fixtures import workflow_projector_reference


class GenericWorkflowSemanticCompilerTests(TestCase):
    def compile(self, snapshot, *, values, media=None, output_kind="image"):
        media = {} if media is None else media
        def resolve(key, _rule):
            binding = media.get(key)
            return (binding["token"], binding) if binding is not None else (None, None)
        return compile_frozen_workflow_values(workflow_snapshot=snapshot, scalar_values=values,
            resolve_asset=resolve, classified_groups=None, classified_bindings=[],
            output_kind=output_kind, total_duration_ms=None)

    def media_binding(self, asset, *, item_key, semantic_key):
        # Synthetic compiler protocol facts; business sources use the real
        # ExecutionInputResolution tests and are not certified by this fixture.
        from studio.services.workflow_media_tokens import workflow_media_use_token
        return {"semantic_key": semantic_key, "item_key": item_key, "asset_id": str(asset.pk),
            "artifact_id": None, "kind": "image", "source_sha256": asset.checksum,
            "token": workflow_media_use_token(item_key=item_key, source_sha256=asset.checksum)}

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


    def test_batch_size_requires_a_frozen_contract_value_or_override(self):
        with self.assertRaisesRegex(
            WorkflowSemanticError,
            "缺少必填 semantic: generation.batch_size",
        ):
            resolve_workflow_semantic_values(
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
                resolve_scalar=lambda *_: None,
                resolve_asset=lambda *_: self.fail("No declared media"),
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
        values = {"prompt.positive": "a quiet rain-soaked street", "image.width": 768,
            "image.height": 432, "sampling.seed": 123, "sampling.steps": 20, "output.prefix": "test/fanout"}
        first = self.compile(snapshot, values=values)
        second = self.compile(snapshot, values=values)
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
            self.compile(snapshot, values={**values, "sampling.steps": 100})
        with self.assertRaises(WorkflowCompilerError):
            self.compile(snapshot, values={**values, "model.base.filename": "other.safetensors"})

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
            self.compile(snapshot, values={"image.width": 768, "image.height": 432})

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
            self.compile(workflow_version_execution_snapshot(version), values={"image.width": 768, "image.height": 432})

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
            self.compile(workflow_version_execution_snapshot(version), values={"image.width": 768, "image.height": 432})

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
            self.compile(snapshot, values={"image.width": 768, "image.height": 432})

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
        binding = self.media_binding(asset, item_key="first-frame-use", semantic_key="asset.image.source")
        compiled = self.compile(workflow_version_execution_snapshot(version),
            values={"image.width": 768, "image.height": 432, "output.prefix": "test/first-frame"},
            media={"asset.image.source": binding}, output_kind="video")
        self.assertEqual(compiled.graph["1"]["inputs"]["image"], binding["token"])
        self.assertEqual(compiled.graph["3"]["inputs"]["width"], 768)
        self.assertEqual(compiled.graph["3"]["inputs"]["height"], 432)
        self.assertEqual(compiled.asset_bindings, [binding])
        # Current-source checksum drift belongs to the original source resolver
        # and staging checks, not a second Asset query inside the graph compiler.

    def test_three_image_slots_consume_explicit_semantic_uses_not_dictionary_order(self):
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
        media = {
            key: self.media_binding(assets[index - 1], item_key=f"use-{index}", semantic_key=key)
            for index, key in ((3, "asset.image.source_3"), (1, "asset.image.source"), (2, "asset.image.source_2"))
        }
        compiled = self.compile(workflow_version_execution_snapshot(version),
            values={"image.width": 768, "image.height": 432, "output.prefix": "test/three-uses"}, media=media)

        self.assertEqual(
            [compiled.graph[str(index)]["inputs"]["image"] for index in range(1, 4)],
            [media[key]["token"] for key in ("asset.image.source", "asset.image.source_2", "asset.image.source_3")],
        )
        self.assertEqual(
            [row["item_key"] for row in compiled.asset_bindings],
            ["use-1", "use-2", "use-3"],
        )
