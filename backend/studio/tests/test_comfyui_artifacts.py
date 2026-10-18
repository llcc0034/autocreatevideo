import os
from pathlib import Path
from tempfile import TemporaryDirectory
from types import SimpleNamespace
from uuid import uuid4

from django.core.exceptions import ValidationError
from django.test import SimpleTestCase, override_settings

from studio.models import ModelArtifact
from studio.services.comfyui_artifacts import (
    artifact_inventory_spec,
    controlled_local_model_path,
    exact_object_info_model_option,
    object_info_inventory_names,
    safe_model_relative_filename,
)
from studio.services.workflow_versions import _validate_model_inventory


class ComfyUIArtifactInventoryTests(SimpleTestCase):
    def test_import_freezes_live_model_spelling_not_normalized_inventory_name(self):
        from studio.services.comfyui_workflow_imports import _neutral_binding_value, SavedWorkflowUnsafeLiteral

        filename = "nested/model.safetensors"
        widget = "nested\\model.safetensors"
        item = {"input_contract": {"parameters": {
            "model.lora.filename": {"type": "model_artifact", "requirement_alias": "lora"},
        }}, "model_requirements": [{"alias": "lora", "expected_filename": filename}]}
        binding = {"value_type": "model_artifact", "semantic_key": "model.lora.filename"}
        for definition in ([[widget], {}], ["COMBO", {"options": [widget]}]):
            with self.subTest(definition=definition):
                self.assertEqual(_neutral_binding_value(item=item, binding=binding, definition=definition), widget)
        for choices in (["other.safetensors"], [filename, widget]):
            with self.subTest(choices=choices), self.assertRaises(SavedWorkflowUnsafeLiteral):
                _neutral_binding_value(item=item, binding=binding, definition=[choices, {}])

    def test_live_inventory_requires_exact_graph_option_for_portable_file_identity(self):
        profile_id = uuid4()
        artifact = ModelArtifact(
            profile_version_id=profile_id,
            role=ModelArtifact.Role.LORA,
            filename="HunyuanVideo\\lightning\\h3_lora.safetensors",
        )
        requirement = SimpleNamespace(
            id=uuid4(),
            alias="h3_lora",
            artifact=artifact,
            is_required=False,
            expected_filename="HunyuanVideo/lightning/h3_lora.safetensors",
        )

        class Requirements:
            def select_related(self, *_args):
                return self

            def all(self):
                return [requirement]

        binding = SimpleNamespace(
            semantic_key="model.h3_lora.filename",
            node_id="7",
            input_name="lora_name",
            value_type="model_artifact",
        )

        class Bindings:
            def all(self):
                return [binding]

        class Driver:
            def invoke(self, _deployment, operation, payload):
                self.assertions = (operation, payload)
                return {
                    "items": ["HunyuanVideo/lightning/h3_lora.safetensors"],
                }

        version = SimpleNamespace(
            model_profile_version_id=profile_id,
            model_requirements=Requirements(),
            semantic_bindings=Bindings(),
            graph={
                "7": {
                    "class_type": "LoraLoader",
                    "inputs": {"lora_name": artifact.filename},
                },
            },
        )
        deployment = SimpleNamespace(
            profile_version_id=profile_id,
            runtime_host_id=None,
        )
        object_info = {
            "LoraLoader": {
                "input": {"required": {
                    "lora_name": [[artifact.filename], {}],
                }},
            },
        }
        report = _validate_model_inventory(
            version,
            deployment,
            Driver(),
            object_info,
        )

        version.graph["7"]["inputs"]["lora_name"] = requirement.expected_filename
        with self.assertRaisesRegex(ValidationError, "精确枚举不一致"):
            _validate_model_inventory(version, deployment, Driver(), object_info)

        version.graph["7"]["inputs"]["lora_name"] = (
            "HunyuanVideo/lightning/another_lora.safetensors"
        )
        with self.assertRaisesRegex(ValidationError, "受管模型身份不一致"):
            _validate_model_inventory(
                version,
                deployment,
                Driver(),
                object_info,
            )

        self.assertEqual(
            report["checked_requirements"][0]["filename"],
            requirement.expected_filename,
        )
        self.assertEqual(
            report["checked_requirements"][0]["submission_filename"],
            artifact.filename,
        )
        self.assertEqual(report["schema_version"], 2)

    def test_exact_object_info_option_preserves_windows_spelling(self):
        object_info = {
            "LoraLoader": {"input": {"required": {
                "lora_name": [[
                    "MiniMax-H3-Turbo-Lora-Pruned-ComfyUI\\minimax_h3.safetensors",
                ], {}],
            }}},
        }

        resolved = exact_object_info_model_option(
            object_info,
            node_type="LoraLoader",
            input_name="lora_name",
            expected_filename=(
                "MiniMax-H3-Turbo-Lora-Pruned-ComfyUI/minimax_h3.safetensors"
            ),
        )

        self.assertEqual(
            resolved,
            "MiniMax-H3-Turbo-Lora-Pruned-ComfyUI\\minimax_h3.safetensors",
        )

    def test_exact_object_info_option_missing_or_ambiguous_fails_closed(self):
        missing = {
            "LoraLoader": {"input": {"required": {
                "lora_name": [["other.safetensors"], {}],
            }}},
        }
        ambiguous = {
            "LoraLoader": {"input": {"required": {
                "lora_name": [[
                    "nested/model.safetensors",
                    "nested\\model.safetensors",
                ], {}],
            }}},
        }
        for object_info, message in (
            (missing, "缺少"),
            (ambiguous, "多个等价枚举值"),
        ):
            with self.subTest(message=message), self.assertRaisesRegex(
                ValueError,
                message,
            ):
                exact_object_info_model_option(
                    object_info,
                    node_type="LoraLoader",
                    input_name="lora_name",
                    expected_filename="nested/model.safetensors",
                )

    def test_standard_role_uses_one_authoritative_default_folder(self):
        artifact = ModelArtifact(role=ModelArtifact.Role.DIFFUSION_MODEL, filename="model.safetensors")
        spec = artifact_inventory_spec(artifact)

        self.assertEqual(spec.folder, "diffusion_models")
        self.assertEqual(spec.source, "models_api")
        self.assertEqual(spec.node_type, "")

    def test_custom_seed_folder_uses_exact_object_info_selector(self):
        artifact = ModelArtifact(
            role=ModelArtifact.Role.OTHER,
            filename="seedvr2_ema_3b_fp16.safetensors",
            inventory_folder="SEEDVR2",
            inventory_node_type="SeedVR2LoadDiTModel",
            inventory_input_name="model",
        )
        spec = artifact_inventory_spec(artifact)
        names = object_info_inventory_names({
            "SeedVR2LoadDiTModel": {
                "input": {"required": {"model": [[artifact.filename], {}]}},
            },
        }, spec)

        self.assertEqual(spec.source, "object_info")
        self.assertEqual(names, {artifact.filename})

    def test_custom_seed_combo_config_options_supports_string_and_key_rows(self):
        artifact = ModelArtifact(
            role=ModelArtifact.Role.OTHER,
            filename="seedvr2_ema_3b_fp8_e4m3fn.safetensors",
            inventory_folder="SEEDVR2",
            inventory_node_type="SeedVR2LoadDiTModel",
            inventory_input_name="model",
        )
        spec = artifact_inventory_spec(artifact)
        names = object_info_inventory_names({
            "SeedVR2LoadDiTModel": {
                "input": {
                    "required": {
                        "model": ["COMBO", {"options": [
                            artifact.filename,
                            {"key": "second.safetensors", "label": "Second"},
                            {"label": "ignored-without-key"},
                        ]}],
                    },
                },
            },
        }, spec)

        self.assertEqual(names, {artifact.filename, "second.safetensors"})

    def test_custom_inventory_normalizes_windows_separators_and_drops_unsafe_names(self):
        artifact = ModelArtifact(
            role=ModelArtifact.Role.OTHER,
            filename="nested/model.safetensors",
            inventory_folder="SEEDVR2",
            inventory_node_type="SeedVR2LoadDiTModel",
            inventory_input_name="model",
        )
        names = object_info_inventory_names({
            "SeedVR2LoadDiTModel": {"input": {"required": {
                "model": ["COMBO", {"options": [
                    "nested\\model.safetensors",
                    "nested/model.safetensors",
                    "../escape.safetensors",
                    "file.safetensors:stream",
                ]}],
            }}},
        }, artifact_inventory_spec(artifact))

        self.assertEqual(names, {"nested/model.safetensors"})

    def test_custom_selector_missing_or_malformed_fails_closed(self):
        artifact = ModelArtifact(
            role=ModelArtifact.Role.OTHER,
            filename="seed.safetensors",
            inventory_folder="SEEDVR2",
            inventory_node_type="SeedVR2LoadDiTModel",
            inventory_input_name="model",
        )
        spec = artifact_inventory_spec(artifact)
        for object_info in ({}, {"SeedVR2LoadDiTModel": {"input": {"required": {"model": ["STRING", {}]}}}}):
            with self.subTest(object_info=object_info), self.assertRaises(ValueError):
                object_info_inventory_names(object_info, spec)

    def test_controlled_local_path_never_leaves_declared_model_folder(self):
        with TemporaryDirectory() as root:
            comfy_root = Path(root) / "ComfyUI"
            output = comfy_root / "output"
            model = comfy_root / "models" / "SEEDVR2" / "seed.safetensors"
            output.mkdir(parents=True)
            model.parent.mkdir(parents=True)
            model.write_bytes(b"seed")
            with override_settings(COMFYUI_OUTPUT_DIR=str(output)):
                self.assertEqual(
                    controlled_local_model_path("SEEDVR2", "seed.safetensors"),
                    model.resolve(),
                )
                self.assertEqual(
                    controlled_local_model_path("SEEDVR2", "SEEDVR2\\seed.safetensors"),
                    model.resolve(),
                )
                self.assertIsNone(controlled_local_model_path("SEEDVR2", "../seed.safetensors"))
                self.assertIsNone(controlled_local_model_path("../outside", "seed.safetensors"))

    def test_windows_ads_controls_devices_and_ambiguous_segments_fail_closed(self):
        unsafe_names = (
            "file.safetensors:secret",
            "sub/file.safetensors:secret",
            "C:/models/file.safetensors",
            "//server/share/file.safetensors",
            "bad\x00name.safetensors",
            "bad\nname.safetensors",
            "bad\rname.safetensors",
            "bad\x7fname.safetensors",
            "trailing-dot.",
            "trailing-space ",
            "CON",
            "sub/Lpt1.txt",
            "bad*name.safetensors",
            "bad?name.safetensors",
        )
        with TemporaryDirectory() as root:
            comfy_root = Path(root) / "ComfyUI"
            output = comfy_root / "output"
            (comfy_root / "models" / "SEEDVR2").mkdir(parents=True)
            output.mkdir(parents=True)
            with override_settings(COMFYUI_OUTPUT_DIR=str(output)):
                for value in unsafe_names:
                    with self.subTest(value=repr(value)):
                        self.assertIsNone(safe_model_relative_filename(value))
                        self.assertIsNone(
                            controlled_local_model_path("SEEDVR2", value),
                        )

    def test_symlink_or_reparse_components_never_resolve_into_model_inventory(self):
        with TemporaryDirectory() as root:
            comfy_root = Path(root) / "ComfyUI"
            output = comfy_root / "output"
            model_root = comfy_root / "models" / "SEEDVR2"
            outside = Path(root) / "outside"
            output.mkdir(parents=True)
            model_root.mkdir(parents=True)
            outside.mkdir()
            (outside / "target.safetensors").write_bytes(b"outside")
            final_link = model_root / "final.safetensors"
            directory_link = model_root / "linked-directory"
            try:
                os.symlink(outside / "target.safetensors", final_link)
                os.symlink(outside, directory_link, target_is_directory=True)
            except OSError as exc:
                self.skipTest(f"host cannot create symlink/reparse test fixture: {exc}")
            with override_settings(COMFYUI_OUTPUT_DIR=str(output)):
                self.assertIsNone(
                    controlled_local_model_path("SEEDVR2", "final.safetensors"),
                )
                self.assertIsNone(controlled_local_model_path(
                    "SEEDVR2",
                    "linked-directory/target.safetensors",
                ))
