from copy import deepcopy
from hashlib import sha256
from pathlib import Path
from tempfile import TemporaryDirectory
from types import SimpleNamespace
from unittest.mock import patch

from django.contrib.auth import get_user_model
from django.core.exceptions import ValidationError
from django.test import TestCase
from PIL import Image

from studio.models import Asset
from studio.services.json_contracts import json_contract
from studio.services.generated_artifacts import verified_local_media_facts
from studio.services.provenance import ProvenanceError
from studio.services.workflow_media_tokens import workflow_media_use_token
from studio.services.workflow_compiler import (
    compile_frozen_workflow_values, workflow_execution_contract, WorkflowCompilerError,
)
from studio.services.workflow_resolution import (
    infer_source_image_resolution, validate_source_image_resolution,
    kontext_dimensions, KONTEXT_RESOLUTIONS, WorkflowResolutionError,
    validate_resolved_image_output,
    configure_source_image_resolution, CONFIGURED_RESOLUTION_ADAPTER,
    image_resolution_options, validate_image_resolution_policy, materialize_resolution_graph,
)
from studio.services.comfyui_workflow_imports import _candidate_failure_summary, _candidate_failure_actions
from studio.tests.workflow_projector_fixtures import workflow_projector_reference


class WorkflowResolutionTests(TestCase):
    def setUp(self):
        self.directory = TemporaryDirectory()
        self.addCleanup(self.directory.cleanup)
        self.root = Path(self.directory.name)
        settings = self.settings(ASSET_LIBRARY_ROOT=str(self.root))
        settings.enable()
        self.addCleanup(settings.disable)
        self.graph = {
            "1": {"class_type": "LoadImage", "inputs": {"image": "__AUTOPROMPT_ASSET__:managed"}},
            "2": {"class_type": "FluxKontextImageScale", "inputs": {"image": ["1", 0]}},
            "3": {"class_type": "VAELoader", "inputs": {"vae_name": "fixture.safetensors"}},
            "4": {"class_type": "VAEEncode", "inputs": {"pixels": ["2", 0], "vae": ["3", 0]}},
            "5": {"class_type": "KSampler", "inputs": {"latent_image": ["4", 0], "seed": 1}},
            "6": {"class_type": "VAEDecode", "inputs": {"samples": ["5", 0], "vae": ["3", 0]}},
            "7": {"class_type": "SaveImageAdvanced", "inputs": {"images": ["6", 0], "filename_prefix": "test"}},
        }
        self.bindings = [
            {"semantic_key": "asset.image.source", "node_id": "1", "input_name": "image", "value_type": "asset_image", "is_required": True},
            {"semantic_key": "sampling.seed", "node_id": "5", "input_name": "seed", "value_type": "integer"},
            {"semantic_key": "output.prefix", "node_id": "7", "input_name": "filename_prefix", "value_type": "string"},
        ]
        self.contract = {
            **workflow_projector_reference(),
            "schema_version": 3, "compiler": {"id": "generic.comfy", "version": 1},
            "parameters": {
                "asset.image.source": {"type": "asset_image", "required": True, "input_slot": 1, "asset_category": "image"},
                "sampling.seed": {"type": "integer", "required": True},
                "output.prefix": {"type": "string", "required": True},
            },
            "outputs": {"primary": {"node_id": "7", "kind": "image", "required": True}},
            "resolution_strategy": infer_source_image_resolution(self.graph, self.bindings, "7"),
            "required_dimension_multiple": 16,
        }

    def snapshot(self):
        graph_sha = json_contract(self.graph, max_bytes=100000).sha256
        contract_sha = json_contract(self.contract, max_bytes=100000).sha256
        payload, digest = workflow_execution_contract(
            compiler_id="generic.comfy", compiler_version=1, graph_sha256=graph_sha,
            input_contract_sha256=contract_sha, bindings=self.bindings, requirements=[],
        )
        return {**payload, "graph": self.graph, "input_contract": self.contract,
                "compiler_id": "generic.comfy", "compiler_version": 1, "model_identifier": "unrelated-model-name",
                "semantic_bindings": self.bindings, "model_requirements": [], "execution_contract_sha256": digest}

    def asset(self, size=(1600, 900), *, orientation=None):
        path = self.root / "source.png"
        exif = Image.Exif()
        if orientation is not None:
            exif[274] = orientation
        Image.new("RGB", size).save(path, exif=exif)
        return Asset.objects.create(name="resolution fixture", local_path=str(path),
                                    mime_type="image/png", checksum=sha256(path.read_bytes()).hexdigest())

    def compile(self, asset=None, overrides=None):
        # Compiler protocol fixture. Business ownership and source selection are
        # exercised by ExecutionInputResolution, not a fabricated Project/Prompt.
        binding = None
        if asset is not None:
            media = verified_local_media_facts(Path(asset.local_path), expected_kind="image",
                max_bytes=1000000, include_observed_media=True)
            self.assertEqual(media["sha256"], asset.checksum)
            binding = {"semantic_key": "asset.image.source", "item_key": "resolution-source",
                "asset_id": str(asset.pk), "artifact_id": None, "kind": "image", "media_facts": media}
            binding["source_sha256"] = json_contract(binding).sha256
            binding["token"] = workflow_media_use_token(item_key=binding["item_key"], source_sha256=binding["source_sha256"])
        return compile_frozen_workflow_values(workflow_snapshot=self.snapshot(),
            scalar_values={"sampling.seed": 12, "output.prefix": "test/resolution", **(overrides or {})},
            resolve_asset=lambda key, rule: (None, None) if binding is None else (binding["token"], binding),
            classified_groups=None, classified_bindings=[], output_kind="image", total_duration_ms=None)

    def test_resolution_table_and_orientation(self):
        for size in KONTEXT_RESOLUTIONS:
            self.assertEqual(kontext_dimensions(*size), size)
        compiled = self.compile(self.asset((1600, 900), orientation=6))
        self.assertEqual((compiled.resolved_semantics["image.width"], compiled.resolved_semantics["image.height"]), (752, 1392))
        self.assertEqual(compiled.output_contract["resolution"]["source_width"], 900)

    def configure(self, sizes):
        self.contract.update(workflow_projector_reference(image_resolution={"adapter": CONFIGURED_RESOLUTION_ADAPTER, "sizes": sizes}))
        self.contract["resolution_strategy"] = configure_source_image_resolution(
            infer_source_image_resolution(self.graph, self.bindings, "7"),
            {"adapter": CONFIGURED_RESOLUTION_ADAPTER, "sizes": sizes},
        )

    def test_configured_size_rewrites_real_graph_and_verifies_output(self):
        original = deepcopy(self.graph)
        self.configure([{"width": 512, "height": 512}, {"width": 1024, "height": 576}])
        compiled = self.compile(self.asset())
        self.assertEqual(self.graph, original)
        self.assertEqual(compiled.graph["2"], {"class_type": "ImageScale", "inputs": {
            "image": ["1", 0], "width": 1024, "height": 576, "crop": "center", "upscale_method": "lanczos",
        }})
        self.assertEqual(compiled.resolved_semantics["image.width"], 1024)
        self.assertEqual(compiled.output_contract["resolution"]["adapter"], CONFIGURED_RESOLUTION_ADAPTER)
        path = self.root / "generated.png"
        Image.new("RGB", (1024, 576)).save(path)
        validate_resolved_image_output(path, compiled.output_contract)
        Image.new("RGB", (1392, 752)).save(path)
        with self.assertRaises(ValidationError):
            validate_resolved_image_output(path, compiled.output_contract)

    def test_single_size_is_fixed_and_contract_hash_changes(self):
        before = self.snapshot()["execution_contract_sha256"]
        self.configure([{"width": 768, "height": 512}])
        self.assertNotEqual(self.snapshot()["execution_contract_sha256"], before)
        compiled = self.compile(self.asset((512, 1024)))
        self.assertEqual((compiled.resolved_semantics["image.width"], compiled.resolved_semantics["image.height"]), (768, 512))
        self.assertEqual(compiled.graph, materialize_resolution_graph(compiled.graph | {"2": self.graph["2"]},
            self.contract["resolution_strategy"], 768, 512))

    def test_configured_policy_limits_and_fail_closed(self):
        for sizes in ([], [{"width":True,"height":1024}], [{"width":513,"height":512}],
                      [{"width":16,"height":16}], [{"width":8192,"height":8192}],
                      [{"width":512,"height":512}]*2, [{"width":512,"height":512,"unexpected":1}]):
            with self.subTest(sizes=sizes), self.assertRaises(WorkflowResolutionError):
                validate_image_resolution_policy({"adapter":CONFIGURED_RESOLUTION_ADAPTER,"sizes":sizes})
        with self.assertRaises(WorkflowResolutionError):
            configure_source_image_resolution({}, {"adapter":CONFIGURED_RESOLUTION_ADAPTER,"sizes":[{"width":512,"height":512}]})
        options = image_resolution_options()
        self.assertEqual(options["native_sizes"], [{"width":w,"height":h} for w,h in KONTEXT_RESOLUTIONS])
        validate_image_resolution_policy({"adapter":options["configured_adapter"],"sizes":[{"width":p["width"],"height":p["height"]} for p in options["presets"]]})

    def test_size_limits_follow_published_security_configuration(self):
        from studio.services.configuration import create_config_revision, publish_config_revision
        admin = get_user_model().objects.create_superuser(username="resolution-policy-admin", password="test-password")
        def publish(**limits):
            revision = create_config_revision(scope_type="system", created_by=admin,
                values={f"security.workflow.image_resolution_{key}": value for key, value in limits.items()})
            publish_config_revision(revision, published_by=admin)
        publish(min_dimension=32, max_dimension=16384, max_pixels=40000000, max_candidates=40)
        options = image_resolution_options()
        self.assertEqual(options["limits"], {"min_dimension":32, "max_dimension":16384,
            "max_pixels":40000000, "max_candidates":40, "multiple":16})
        validate_image_resolution_policy({"adapter":CONFIGURED_RESOLUTION_ADAPTER, "sizes":[{"width":9008,"height":32}]})
        publish(max_dimension=2048)
        with self.assertRaises(ValidationError):
            publish(min_dimension=4096)
        with self.assertRaises(WorkflowResolutionError):
            validate_image_resolution_policy({"adapter":CONFIGURED_RESOLUTION_ADAPTER, "sizes":[{"width":9008,"height":32}]})
        # Changing operating limits cannot change the registered Comfy protocol.
        with self.assertRaises(WorkflowResolutionError):
            validate_image_resolution_policy({"adapter":CONFIGURED_RESOLUTION_ADAPTER, "sizes":[{"width":513,"height":512}]})

    def test_projector_configuration_projects_sizes_or_blocks_unsupported_source(self):
        from studio.services.comfyui_workflow_imports import _apply_generic_projector_configuration
        from studio.services.workflow_projectors import current_projector_catalog
        projector = deepcopy(next(row for row in current_projector_catalog() if row.key == "generic.comfy.fixed"))
        policy = {"adapter": CONFIGURED_RESOLUTION_ADAPTER, "sizes": [{"width":512,"height":512}]}
        projector.definition["projection"]["config"]["image_resolution"] = policy
        result = _apply_generic_projector_configuration(projector=projector, graph=self.graph,
            contract=self.contract, bindings=self.bindings, requirements=[], blockers=[], configurable_parameters=[])
        self.assertFalse(result[3])
        self.assertEqual(result[0]["resolution_strategy"]["sizes"], policy["sizes"])
        bad_contract = deepcopy(self.contract)
        del bad_contract["resolution_strategy"]
        rejected = _apply_generic_projector_configuration(projector=projector, graph=self.graph,
            contract=bad_contract, bindings=self.bindings, requirements=[], blockers=[], configurable_parameters=[])
        self.assertTrue(any(row["code"] == "resolution_execution_adapter_unavailable" for row in rejected[3]))

    def test_compiler_freezes_source_evidence_without_rewriting_size_nodes(self):
        asset = self.asset()
        compiled = self.compile(asset)
        self.assertEqual(compiled.graph["2"], self.graph["2"])
        self.assertEqual(compiled.resolved_semantics["image.width"], 1392)
        self.assertEqual(compiled.resolved_semantics["image.height"], 752)
        self.assertEqual(compiled.output_contract["schema_version"], 2)
        self.assertEqual(compiled.output_contract["resolution"]["source_checksum"], asset.checksum)
        self.assertEqual(self.contract["resolution_strategy"]["size_path"], ["7", "6", "5", "4", "2", "1"])

    def test_unrelated_reference_does_not_drive_output_dimensions(self):
        self.graph["8"] = {"class_type": "LoadImage", "inputs": {"image": "other.png"}}
        self.bindings.append({"semantic_key": "asset.image.other", "node_id": "8", "input_name": "image", "value_type": "asset_image"})
        self.assertEqual(infer_source_image_resolution(self.graph, self.bindings, "7")["asset_semantic_keys"], ["asset.image.source"])

    def test_missing_asset_and_manual_width_are_rejected(self):
        with self.assertRaises(WorkflowCompilerError):
            self.compile()
        with self.assertRaises(WorkflowCompilerError):
            self.compile(self.asset(), overrides={"image.width": 128})

    def test_corrupt_bytes_rejected(self):
        asset = self.asset()
        Path(asset.local_path).write_bytes(b"corrupt")
        with self.assertRaises(ProvenanceError):
            self.compile(asset)

    def test_missing_or_drifted_frozen_policy_rejected(self):
        for field in ("adapter", "source_node_id", "size_path"):
            with self.subTest(field=field):
                contract = deepcopy(self.contract)
                contract["resolution_strategy"].pop(field)
                with self.assertRaises(WorkflowResolutionError):
                    validate_source_image_resolution(self.graph, contract, self.bindings)

    def test_unknown_resize_mask_output_and_other_vae_rejected(self):
        for node_id, field, value in (("4", "pixels", ["1", 1]), ("6", "vae", ["9", 0]), ("2", "image", ["1", 1])):
            with self.subTest(node=node_id, field=field):
                graph = deepcopy(self.graph)
                graph[node_id]["inputs"][field] = value
                with self.assertRaises(WorkflowResolutionError):
                    infer_source_image_resolution(graph, self.bindings, "7")
        self.graph["2"]["class_type"] = "UnknownUpscale"
        with self.assertRaises(WorkflowResolutionError):
            infer_source_image_resolution(self.graph, self.bindings, "7")

    def test_source_slot_cannot_be_optional_or_dynamic_or_static_override(self):
        bindings = deepcopy(self.bindings)
        bindings[0]["is_required"] = False
        with self.assertRaises(WorkflowResolutionError):
            infer_source_image_resolution(self.graph, bindings, "7")
        for extra in ({"asset_groups": [{}]}, {"static_semantics": {"image.width": 64}}):
            with self.assertRaises(WorkflowResolutionError):
                validate_source_image_resolution(self.graph, {**self.contract, **extra}, self.bindings)

    def test_real_output_must_match_frozen_dimensions(self):
        compiled = self.compile(self.asset())
        output = self.root / "output.png"
        Image.new("RGB", (1392, 752)).save(output)
        validate_resolved_image_output(output, compiled.output_contract)
        Image.new("RGB", (1024, 1024)).save(output)
        with self.assertRaisesMessage(ValidationError, "实际图片尺寸"):
            validate_resolved_image_output(output, compiled.output_contract)
        validate_resolved_image_output(output, {"schema_version": 1})

    def test_production_archive_rejects_wrong_derived_size_before_recording(self):
        from studio.services.generated_artifacts import _archive_task_output
        from studio.services.provenance import ProvenanceError
        compiled = self.compile(self.asset())
        output = self.root / "wrong.png"
        Image.new("RGB", (64, 64)).save(output)
        task = SimpleNamespace()
        with patch("studio.services.generated_artifacts.resolve_task_output_path", return_value=output):
            with self.assertRaisesMessage(ProvenanceError, "实际图片尺寸"):
                _archive_task_output(task, 0, {"filename": "wrong.png"}, expected_kind="image",
                                     expected_sha256=sha256(output.read_bytes()).hexdigest(), max_bytes=1000000,
                                     output_contract=compiled.output_contract)


    def test_scan_summary_uses_non_dominated_candidates_without_changing_evidence(self):
        rows = [
            {"status": "published", "rejection_codes": ["resolution_execution_adapter_unavailable"]},
            {"status": "published", "rejection_codes": ["resolution_execution_adapter_unavailable", "model_artifact_contract_required"]},
            {"status": "published", "rejection_codes": ["model_profile_modality_mismatch"]},
        ]
        original = deepcopy(rows)
        codes = _candidate_failure_summary(rows)
        self.assertEqual(codes, ["resolution_execution_adapter_unavailable"])
        self.assertEqual(_candidate_failure_actions(codes, rows), [{"code": "review_workflow_contract"}])
        self.assertEqual(rows, original)
        self.assertEqual(_candidate_failure_actions(["model_artifact_contract_required"], rows), [{"code": "register_model_profile"}])
