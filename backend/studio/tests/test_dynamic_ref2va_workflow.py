import hashlib
from copy import deepcopy
from pathlib import Path
import tempfile
from types import SimpleNamespace
from unittest.mock import Mock, patch

from django.contrib.auth import get_user_model
from django.core.exceptions import ValidationError
from django.test import SimpleTestCase, TestCase, override_settings

from studio.models import (
    Asset,
    ModelDeployment,
    ModelArtifact,
    ModelProfileVersion,
    ProviderProfile,
    WorkflowTemplate,
    WorkflowTemplateVersion,
)
from studio.services.comfyui_artifacts import exact_object_info_model_option
from studio.services.json_contracts import assign_json_contract, json_contract
from studio.services.media_capabilities import (
    conditioning_mode_contract,
    resolve_conditioning_contract,
)
from studio.services.workflow_compiler import (
    WorkflowCompilerError,
    compile_frozen_workflow,
)
from studio.services.workflow_smoke_fixtures import (
    _default_smoke_value,
    finalize_workflow_smoke_fixture,
    normalize_workflow_execution_inputs,
    normalize_workflow_smoke_fixture,
    workflow_execution_input_schema,
    workflow_smoke_fixture_schema,
)
from studio.services.workflow_versions import (
    _materialize_smoke_graph,
    import_workflow_version,
    validate_workflow_version,
    workflow_version_execution_snapshot,
)
from studio.tests.factories import (
    conditioning_contract,
    h3_dual_clock_ref2va_workflow_graph,
)
from studio.tests.workflow_projector_fixtures import workflow_projector_reference


class WorkflowSmokeDefaultTests(SimpleTestCase):
    def test_normalizes_custom_multiple_constraint(self):
        self.assertEqual(
            _default_smoke_value("parameter.h3.length", {
                "type": "integer", "default": 124, "min": 5,
                "max": 3600, "multiple_of": 17,
            }),
            119,
        )


class DynamicRef2VAWorkflowTests(TestCase):
    def setUp(self):
        self.user = get_user_model().objects.create_user(
            username="dynamic-ref2va-owner", password="StrongPass123!",
        )
        self.provider = ProviderProfile(
            key="dynamic-ref2va-comfy",
            name="Dynamic Ref2VA Comfy",
            driver_type=ProviderProfile.DriverType.COMFYUI,
            modalities="video",
            operations="health,object_info,models,upload,submit,poll,cancel,collect",
        )
        assign_json_contract(self.provider, "driver_config", {"base_url": "http://127.0.0.1:8188"})
        self.provider.save()
        self.profile = ModelProfileVersion(
            profile_key="fictional-dynamic-reference-model",
            version=1,
            provider=self.provider,
            model_identifier="fictional-cinema-video-v9",
            modality=ModelProfileVersion.Modality.VIDEO,
            status=ModelProfileVersion.Status.PUBLISHED,
        )
        assign_json_contract(self.profile, "specification", {
            "schema_version": 1,
            "capabilities": ["video.reference_to_video"],
            "conditioning_contract": conditioning_contract("Ref2VA", input_kind="classified_references", requires_bible=True, first_frame=(0, 1), entity_images=(0, 9), reference_videos=(0, 3), paired_video_audios=(0, 3), reference_audios=(0, 3)),
        })
        self.profile.save()
        self.artifacts = {}
        for alias, role, filename, marker in (
            ("h3-ref2va", ModelArtifact.Role.DIFFUSION_MODEL, "MiniMax_H3_Ref2VA_pruned_int8_convrot.safetensors", "1"),
            (
                "h3-turbo-lora",
                ModelArtifact.Role.LORA,
                (
                    "MiniMax-H3-Turbo-Lora-Pruned-ComfyUI/"
                    "minimax_h3_turbo_4step_ckpt600_ema_V4.safetensors"
                ),
                "2",
            ),
            ("h3-qwen3vl", ModelArtifact.Role.TEXT_ENCODER, "qwen3vl_32b_minimax_h3_int8_convrot.safetensors", "3"),
            ("h3-video-vae", ModelArtifact.Role.VAE, "minimax_h3_video_vae_fp16.safetensors", "4"),
            ("h3-audio-vae", ModelArtifact.Role.VAE, "minimax_h3_audio_vae_fp32.safetensors", "5"),
        ):
            self.artifacts[alias] = ModelArtifact.objects.create(
                profile_version=self.profile,
                role=role,
                logical_name=alias,
                filename=filename,
                sha256=marker * 64,
                status=ModelArtifact.Status.AVAILABLE,
            )
        self.version = self._create_version()

    def _create_version(self):
        template = WorkflowTemplate.objects.create(
            name="Dynamic Ref2VA",
            slug="dynamic-ref2va-v3",
            output_kind=WorkflowTemplate.OutputKind.VIDEO,
            provider="comfyui",
            model=self.profile.model_identifier,
            definition={},
            input_schema={},
            is_system=True,
            is_active=False,
        )
        graph = h3_dual_clock_ref2va_workflow_graph(low_vram=True)
        graph["2"]["inputs"]["strength_model"] = 1.0
        graph["6"]["inputs"].update({"prompt": "managed prompt", "width": 352, "height": 608, "length": 56})
        graph["7"]["inputs"]["steps"] = 4
        graph["9"]["inputs"]["noise_seed"] = 1
        graph["13"]["inputs"]["filename_prefix"] = "output"
        parameters = {
            "prompt.positive": {"type": "string", "required": True, "default": "smoke"},
            "image.width": {"type": "integer", "required": True, "default": 352, "min": 32, "max": 2048, "multiple_of": 32},
            "image.height": {"type": "integer", "required": True, "default": 608, "min": 32, "max": 2048, "multiple_of": 32},
            "video.frames": {"type": "integer", "required": True, "default": 56, "min": 5, "max": 3600, "alignment_modulus": 17, "alignment_remainder": 5},
            "video.fps": {"type": "number", "required": True, "default": 24, "min": 24, "max": 24},
            "sampling.seed": {"type": "integer", "required": True, "default": 1, "min": 0},
            "sampling.steps": {"type": "integer", "required": True, "default": 4, "min": 4, "max": 4},
            "parameter.lora.strength": {"type": "number", "required": True, "default": 1, "min": 1, "max": 1},
            "output.prefix": {"type": "string", "required": True},
            **{
                f"model.{alias}.filename": {
                    "type": "model_artifact", "required": True, "requirement_alias": alias,
                }
                for alias in self.artifacts
            },
        }
        contract = {
            **workflow_projector_reference("minimax_h3.classified_ref2va"),
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
            "prompt_template_keys": ["h3_r2v_zero_reference_v1", "h3_r2v_classified_v1"],
            "resolution_strategy": {"mode": "explicit_width_height", "node_ids": ["6"]},
            "required_dimension_multiple": 32,
            "static_semantics": {"audio.enabled": True},
            "parameters": parameters,
            "outputs": {"primary": {"node_id": "13", "kind": "video", "required": True}},
        }
        target_by_alias = {
            "h3-ref2va": ("1", "unet_name"),
            "h3-turbo-lora": ("2", "lora_name"),
            "h3-qwen3vl": ("3", "clip_name"),
            "h3-video-vae": ("4", "vae_name"),
            "h3-audio-vae": ("5", "vae_name"),
        }
        bindings = [
            {"semantic_key": "prompt.positive", "node_id": "6", "input_name": "prompt", "value_type": "string"},
            {"semantic_key": "image.width", "node_id": "6", "input_name": "width", "value_type": "integer"},
            {"semantic_key": "image.height", "node_id": "6", "input_name": "height", "value_type": "integer"},
            {"semantic_key": "video.frames", "node_id": "6", "input_name": "length", "value_type": "integer"},
            {"semantic_key": "video.fps", "node_id": "12", "input_name": "fps", "value_type": "number"},
            {"semantic_key": "sampling.seed", "node_id": "9", "input_name": "noise_seed", "value_type": "integer"},
            {"semantic_key": "sampling.steps", "node_id": "7", "input_name": "steps", "value_type": "integer"},
            {"semantic_key": "parameter.lora.strength", "node_id": "2", "input_name": "strength_model", "value_type": "number"},
            {"semantic_key": "output.prefix", "node_id": "13", "input_name": "filename_prefix", "value_type": "string"},
            *[
                {"semantic_key": f"model.{alias}.filename", "node_id": target_by_alias[alias][0], "input_name": target_by_alias[alias][1], "value_type": "model_artifact"}
                for alias in self.artifacts
            ],
        ]
        requirements = [
            {
                "role": artifact.role,
                "alias": alias,
                "artifact_id": str(artifact.id),
                "expected_filename": artifact.filename,
                "expected_sha256": artifact.sha256,
                "is_required": True,
            }
            for alias, artifact in self.artifacts.items()
        ]
        from studio.services.workflow_classified_references import attach_plan
        from studio.services.workflow_projectors import current_projector_catalog
        projector = next(row for row in current_projector_catalog() if row.key == "minimax_h3.classified_ref2va")
        contract["parameters"]["video.frames"]["default"] = 22
        for policy in projector.definition["parameter_policies"]:
            if policy["policy"] == "fixed":
                contract["parameters"][policy["semantic_key"]]["const"] = policy["value"]
        attach_plan(contract, projector.definition, graph, self.profile.specification)
        return import_workflow_version(
            template=template,
            graph=graph,
            input_contract=contract,
            semantic_bindings=bindings,
            model_requirements=requirements,
            provider=self.provider,
            model_profile_version=self.profile,
        )

    def test_execution_schema_uses_frozen_configured_mode(self):
        schema = workflow_execution_input_schema(self.version)
        self.assertEqual(set(schema["dynamic_reference_modes"]), {"Ref2VA"})
        self.assertEqual(schema["dynamic_reference_modes"]["Ref2VA"]["pictures"]["max_items"], 9)

    def test_live_validation_h3_probe_passes_frozen_conditioning_contract(self):
        """The read-only H3 probe must receive the version's frozen mode contract."""

        deployment = ModelDeployment(
            key="dynamic-ref2va-validation-deployment",
            name="Dynamic Ref2VA validation deployment",
            profile_version=self.profile,
            status=ModelDeployment.Status.ACTIVE,
        )
        assign_json_contract(
            deployment,
            "runtime_config",
            {"base_url": "http://127.0.0.1:8188"},
        )
        deployment.save()

        driver = Mock()
        driver.invoke.return_value = {}
        contract_report = Mock()
        contract_report.as_dict.return_value = {"schema_version": 1}

        from studio.services.workflow_classified_references import materialize as real_adapter
        with (
            patch("studio.services.workflow_versions.driver_for", return_value=driver),
            patch("studio.services.workflow_versions.validate_comfyui_api_contract", return_value=contract_report),
            patch("studio.services.workflow_versions._validate_model_inventory", return_value={"schema_version": 2, "checked_requirements": []}),
            patch("studio.services.workflow_classified_references.materialize", wraps=real_adapter) as adapter,
        ):
            validated, _report = validate_workflow_version(self.version, deployment=deployment)
        self.assertEqual(validated.validation_state, WorkflowTemplateVersion.ValidationState.PASSED)
        self.assertEqual(adapter.call_count, 3)
        self.assertEqual([row["kind"] for row in validated.validation_evidence["compiler_probes"]],
                         ["classified_references_minimum", "classified_references_maximum", "classified_references_last_video_audio"])

    @staticmethod
    def _write_asset(root: Path, *, name: str, kind: str, mime: str, metadata=None):
        source = root / name
        source.write_bytes(("fixture:" + name).encode("utf-8"))
        return Asset.objects.create(
            owner=None,
            kind=kind,
            name=name,
            local_path=str(source),
            mime_type=mime,
            checksum=hashlib.sha256(source.read_bytes()).hexdigest(),
            metadata=metadata or {},
        )

    def test_dynamic_fixture_materializes_image_video_sparse_pair_and_audio(self):
        with tempfile.TemporaryDirectory() as temp_root:
            root = Path(temp_root)
            picture = self._write_asset(root, name="person.png", kind=Asset.Kind.REFERENCE, mime="image/png")
            video_one = self._write_asset(root, name="motion.mp4", kind=Asset.Kind.REFERENCE_VIDEO, mime="video/mp4", metadata={"duration_ms": 4000, "has_audio": False, "fps": 24})
            video_two = self._write_asset(root, name="edit.mp4", kind=Asset.Kind.REFERENCE_VIDEO, mime="video/mp4", metadata={"duration_ms": 5000, "has_audio": False, "fps": 24})
            paired = self._write_asset(root, name="paired.wav", kind=Asset.Kind.REFERENCE_AUDIO, mime="audio/wav", metadata={"duration_ms": 5000})
            standalone = self._write_asset(root, name="voice.wav", kind=Asset.Kind.REFERENCE_AUDIO, mime="audio/wav", metadata={"duration_ms": 3000})
            fixture_request = {
                "schema_version": 2,
                "values": {"image.width": 352, "image.height": 608, "video.frames": 56, "sampling.steps": 4},
                "assets": [],
                "references": {
                    "pictures": [{"asset_id": str(picture.id), "category": "character"}],
                    "videos": [
                        {"asset_id": str(video_one.id), "purpose": "motion_camera_reference", "use_embedded_audio": False},
                        {"asset_id": str(video_two.id), "purpose": "video_editing", "use_embedded_audio": False, "audio_asset_id": str(paired.id), "audio_purpose": "audio_reference"},
                    ],
                    "audios": [{"asset_id": str(standalone.id), "purpose": "audio_reuse"}],
                },
            }
            with override_settings(ASSET_LIBRARY_ROOT=root):
                frozen = normalize_workflow_smoke_fixture(self.version, fixture_request)
                compiled = _materialize_smoke_graph(
                    self.version,
                    fixture_values=frozen.values,
                    fixture_reference_bindings=frozen.reference_bindings,
                    fixture_asset_facts=frozen.asset_facts,
                )
                frozen = finalize_workflow_smoke_fixture(
                    frozen, materialized_graph_sha256=compiled.graph_sha256,
                )
            inputs = compiled.graph["6"]["inputs"]
            self.assertIn("ref_images.ref_image_0", inputs)
            self.assertIn("ref_videos.ref_video_0", inputs)
            self.assertIn("ref_videos.ref_video_1", inputs)
            self.assertNotIn("ref_video_audios.ref_video_audio_0", inputs)
            self.assertIn("ref_video_audios.ref_video_audio_1", inputs)
            self.assertIn("ref_audios.ref_audio_0", inputs)
            self.assertEqual(len(frozen.asset_facts), 5)
            self.assertEqual(frozen.persisted_values["parameters"]["sampling.steps"], 4)
            self.assertEqual(frozen.materialized_graph_sha256, compiled.graph_sha256)
            self.assertEqual(len(frozen.fixture_sha256), 64)
            schema = workflow_smoke_fixture_schema(self.version)
            self.assertEqual(schema["schema_version"], 2)
            self.assertEqual(schema["dynamic_references"]["pictures"]["max_items"], 9)
            self.assertEqual(schema["dynamic_references"]["maximum_physical_assets"], 18)
            self.assertEqual(
                compiled.graph["14"],
                {
                    "class_type": "MiniMaxLowVRAMAttention",
                    "inputs": {"model": ["2", 0], "head_chunks": 4},
                },
            )
            self.assertEqual(compiled.graph["7"]["inputs"]["model"], ["14", 0])
            self.assertEqual(compiled.graph["6"]["inputs"]["width"], 352)
            self.assertEqual(compiled.graph["6"]["inputs"]["height"], 608)
            self.assertEqual(compiled.graph["6"]["inputs"]["length"], 56)
            self.assertEqual(compiled.graph["7"]["inputs"]["steps"], 4)
            self.assertEqual(compiled.compiler_version, 3)
            self.assertTrue(compiled.resolved_semantics["audio.enabled"])

    def test_validated_materialization_uses_exact_frozen_object_info_model_option(self):
        target_definitions = {}
        requirements = list(
            self.version.model_requirements.select_related("artifact").all()
        )
        bindings = {
            row.semantic_key: row
            for row in self.version.semantic_bindings.all()
        }
        for requirement in requirements:
            binding = bindings[f"model.{requirement.alias}.filename"]
            node_type = self.version.graph[str(binding.node_id)]["class_type"]
            key = (node_type, binding.input_name)
            target_definitions.setdefault(key, []).append(
                requirement.expected_filename.replace(
                    "/",
                    "\\",
                ) if requirement.alias == "h3-turbo-lora" else requirement.expected_filename,
            )
        object_info = {}
        for (node_type, input_name), options in target_definitions.items():
            object_info.setdefault(node_type, {"input": {"required": {}}})[
                "input"
            ]["required"][input_name] = [options, {}]

        checked = []
        for requirement in requirements:
            semantic_key = f"model.{requirement.alias}.filename"
            binding = bindings[semantic_key]
            node_type = self.version.graph[str(binding.node_id)]["class_type"]
            submission_filename = exact_object_info_model_option(
                object_info,
                node_type=node_type,
                input_name=binding.input_name,
                expected_filename=requirement.expected_filename,
            )
            checked.append({
                "requirement_id": str(requirement.id),
                "artifact_id": str(requirement.artifact_id),
                "filename": requirement.expected_filename,
                "submission_filename": submission_filename,
                "sha256": requirement.artifact.sha256,
                "folder": requirement.artifact.inventory_folder,
                "inventory_source": "models_api",
                "semantic_key": semantic_key,
                "node_id": str(binding.node_id),
                "node_type": node_type,
                "input_name": binding.input_name,
            })
        evidence = {
            "schema_version": 1,
            "inventory": {
                "schema_version": 2,
                "checked_requirements": checked,
            },
        }
        self.version.validation_state = WorkflowTemplateVersion.ValidationState.PASSED
        self.version.validation_evidence = evidence
        self.version.validation_evidence_sha256 = json_contract(evidence).sha256

        frozen = normalize_workflow_smoke_fixture(self.version, {
            "schema_version": 2,
            "values": {
                "image.width": 352,
                "image.height": 608,
                "video.frames": 56,
                "video.fps": 24,
                "sampling.steps": 4,
            },
            "assets": [],
            "references": {"pictures": [], "videos": [], "audios": []},
        })
        compiled = _materialize_smoke_graph(
            self.version,
            fixture_values=frozen.values,
            fixture_reference_bindings=frozen.reference_bindings,
            fixture_asset_facts=frozen.asset_facts,
        )

        expected_submission = (
            "MiniMax-H3-Turbo-Lora-Pruned-ComfyUI\\"
            "minimax_h3_turbo_4step_ckpt600_ema_V4.safetensors"
        )
        self.assertEqual(
            compiled.graph["2"]["inputs"]["lora_name"],
            expected_submission,
        )
        lora_requirement = next(
            row for row in requirements if row.alias == "h3-turbo-lora"
        )
        self.assertIn("/", lora_requirement.expected_filename)
        self.assertEqual(
            lora_requirement.artifact.filename,
            lora_requirement.expected_filename,
        )

        evidence["inventory"]["checked_requirements"] = [
            row for row in checked
            if row["semantic_key"] != "model.h3-turbo-lora.filename"
        ]
        self.version.validation_evidence_sha256 = json_contract(evidence).sha256
        with self.assertRaisesRegex(Exception, "精确模型提交枚举证据"):
            _materialize_smoke_graph(
                self.version,
                fixture_values=frozen.values,
                fixture_reference_bindings=frozen.reference_bindings,
                fixture_asset_facts=frozen.asset_facts,
            )

        legacy_evidence = {
            "schema_version": 1,
            "inventory": {
                "schema_version": 1,
                "checked_requirements": checked,
            },
        }
        self.version.status = WorkflowTemplateVersion.Status.PUBLISHED
        self.version.validation_evidence = legacy_evidence
        self.version.validation_evidence_sha256 = json_contract(
            legacy_evidence,
        ).sha256
        with self.assertRaisesRegex(Exception, "已发布版本需新建版本"):
            _materialize_smoke_graph(
                self.version,
                fixture_values=frozen.values,
                fixture_reference_bindings=frozen.reference_bindings,
                fixture_asset_facts=frozen.asset_facts,
            )

    def test_runtime_compile_preserves_exact_frozen_model_widget_option(self):
        project = SimpleNamespace(
            id="00000000-0000-0000-0000-000000000201",
            owner_id=self.user.id,
            width=352,
            height=608,
            fps=24,
            continuity_seed=23,
            aspect_ratio="9:16",
        )
        prompt = SimpleNamespace(
            id="00000000-0000-0000-0000-000000000202",
            project=project,
            prompt_text="A locked elevator descends in silence.",
            negative_prompt="",
            total_duration_ms=5000,
            include_audio=True,
            prompt_type="video",
            conditioning_mode="Ref2VA",
            model=self.profile.model_identifier,
            structured_payload={
                "reference_bindings": [],
                "prompt_template_key": "h3_r2v_zero_reference_v1",
                "effective_conditioning_mode": "Ref2VA",
            },
        )
        compiled = compile_frozen_workflow(
            workflow_snapshot=workflow_version_execution_snapshot(self.version),
            prompt=prompt,
            project=project,
            overrides={
                "video.frames": 124,
                "output.prefix": "autoprompt/runtime/exact-model-option",
            },
            video_duration_policy={
                "minimum_duration_ms": 5000,
                "maximum_duration_ms": 15000,
            },
        )
        exact_lora_option = self.version.graph["2"]["inputs"]["lora_name"]
        semantic_key = "model.h3-turbo-lora.filename"
        self.assertEqual(compiled.graph["2"]["inputs"]["lora_name"], exact_lora_option)
        self.assertEqual(compiled.resolved_semantics[semantic_key], exact_lora_option)
        self.assertEqual(
            compiled.resolved_semantics_sha256,
            json_contract(compiled.resolved_semantics).sha256,
        )
        requirement = next(
            row for row in compiled.model_requirements
            if row["alias"] == "h3-turbo-lora"
        )
        self.assertIn("/", requirement["expected_filename"])
        self.assertNotIn("\\", requirement["expected_filename"])
        self.assertEqual(exact_lora_option, requirement["expected_filename"])

        tampered = workflow_version_execution_snapshot(self.version)
        tampered["graph"]["2"]["inputs"]["lora_name"] = exact_lora_option.replace(
            "/", "\\",
        )
        with self.assertRaisesRegex(WorkflowCompilerError, "graph hash"):
            compile_frozen_workflow(
                workflow_snapshot=tampered,
                prompt=prompt,
                project=project,
                overrides={
                    "video.frames": 124,
                    "output.prefix": "autoprompt/runtime/tampered-model-option",
                },
                video_duration_policy={
                    "minimum_duration_ms": 5000,
                    "maximum_duration_ms": 15000,
                },
            )

    def test_generic_validator_rejects_configured_edge_drift(self):
        from studio.services.workflow_versions import _validate_classified_definition
        graph = deepcopy(self.version.graph)
        graph["8"]["inputs"]["model"] = ["1", 0]
        with self.assertRaises(ValidationError):
            _validate_classified_definition(graph, self.version.input_contract, self.profile.specification, list(self.version.semantic_bindings.values()), list(self.version.model_requirements.values()))

    def test_generic_validator_rejects_frozen_plan_and_parameter_drift(self):
        from studio.services.workflow_versions import _validate_classified_definition
        for mutate in (
            lambda contract: contract["classified_references"]["groups"]["images"].update(max_items=20),
            lambda contract: contract["parameters"]["video.frames"].update(alignment_modulus=1),
            lambda contract: contract.pop("static_semantics"),
        ):
            contract = deepcopy(self.version.input_contract)
            mutate(contract)
            with self.assertRaises(ValidationError):
                _validate_classified_definition(self.version.graph, contract, self.profile.specification, list(self.version.semantic_bindings.values()), list(self.version.model_requirements.values()))

    def test_configured_optional_attention_node_is_not_a_separate_compiler(self):
        from studio.services.workflow_versions import _validate_classified_definition
        graph = deepcopy(self.version.graph)
        attention = next(key for key,node in graph.items() if node["class_type"] == "MiniMaxLowVRAMAttention")
        del graph[attention]
        graph["7"]["inputs"]["model"] = ["2", 0]
        _validate_classified_definition(graph, self.version.input_contract, self.profile.specification, list(self.version.semantic_bindings.values()), list(self.version.model_requirements.values()))

    def test_dynamic_fixture_rejects_embedded_and_external_audio_together(self):
        with tempfile.TemporaryDirectory() as temp_root:
            root = Path(temp_root)
            video = self._write_asset(root, name="motion.mp4", kind=Asset.Kind.REFERENCE_VIDEO, mime="video/mp4", metadata={"duration_ms": 4000, "has_audio": True, "fps": 24})
            audio = self._write_asset(root, name="voice.wav", kind=Asset.Kind.REFERENCE_AUDIO, mime="audio/wav", metadata={"duration_ms": 4000})
            with override_settings(ASSET_LIBRARY_ROOT=root), self.assertRaises(Exception):
                normalize_workflow_smoke_fixture(self.version, {
                    "schema_version": 2,
                    "values": {},
                    "assets": [],
                    "references": {"videos": [{
                        "asset_id": str(video.id),
                        "use_embedded_audio": True,
                        "audio_asset_id": str(audio.id),
                    }]},
                })
