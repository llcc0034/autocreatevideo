"""Explicit request facts reuse frozen Comfy graph rules; no remote execution."""
from copy import deepcopy

from django.test import SimpleTestCase, TestCase, override_settings

from studio.models import SeriesProject, Shot
from studio.services.workflow_compiler import compile_frozen_workflow_values, materialize_generic_workflow_graph, WorkflowCompilerError
from studio.services.workflow_versions import workflow_version_execution_snapshot
from studio.services.execution_definitions import build_execution_definition
from studio.services.execution_parameter_preparation import initialize_execution_definition_parameters
from studio.services.execution_request_compilation import compile_execution_transport_request
from django.core.exceptions import ValidationError
from studio.tests import test_execution_definitions as fixtures
from studio.tests import test_dynamic_ref2va_workflow as classified_fixtures


class ExplicitFrameClockTests(SimpleTestCase):
    def test_manual_clock_checks_frames_and_fps_without_a_hidden_duration(self):
        from studio.services.workflow_compiler import _validate_runtime_frame_policy
        contract = {"frame_policy": {"fps": 24, "minimum_frames": 5, "maximum_frames": 260,
            "alignment_modulus": 17, "alignment_remainder": 5}}
        values = {"video.fps": 24, "video.frames": 124}
        _validate_runtime_frame_policy(contract, values, None, None)
        for change in ({"video.frames": 120}, {"video.frames": True}, {"video.frames": 277}, {"video.fps": 0}, {"video.fps": True}):
            with self.assertRaises(WorkflowCompilerError):
                _validate_runtime_frame_policy(contract, {**values, **change}, None, None)

    def test_routed_clock_still_requires_duration_range_and_exact_alignment(self):
        from studio.services.workflow_compiler import _validate_runtime_frame_policy
        contract = {"frame_policy": {"fps": 24, "minimum_frames": 5, "maximum_frames": 260,
            "alignment_modulus": 17, "alignment_remainder": 5}}
        values = {"video.fps": 24, "video.frames": 124}
        policy = {"minimum_duration_ms": 1000, "maximum_duration_ms": 9000}
        _validate_runtime_frame_policy(contract, values, 5000, policy)
        for duration in (None, True, "5000", -1, 9500):
            with self.assertRaises(WorkflowCompilerError):
                _validate_runtime_frame_policy(contract, values, duration, policy)
        with self.assertRaisesMessage(WorkflowCompilerError, "分镜时长不一致"):
            _validate_runtime_frame_policy(contract, {**values, "video.frames": 107}, 5000, policy)


@override_settings(AI_PROVIDER_ALLOWED_HOSTS=("127.0.0.1",))
class ExecutionWorkflowValueTests(TestCase):
    def setUp(self):
        fixtures.WorkflowExecutionDefinitionTests.setUp(self)
        self.snapshot = workflow_version_execution_snapshot(self.workflow)
        self.values = {"prompt.positive": "A blue parcel on a table.", "prompt.negative": "blur",
            "image.width": 320, "image.height": 512, "sampling.seed": 37, "output.prefix": "execution/explicit-request"}

    def compile(self, **changes):
        return compile_frozen_workflow_values(**{"workflow_snapshot": self.snapshot, "scalar_values": self.values,
            "resolve_asset": lambda *_: self.fail("No asset input was declared"), "classified_groups": None, "classified_bindings": [],
            "output_kind": "image", "total_duration_ms": None, **changes})

    def test_real_frozen_graph_is_compiled_without_project_or_prompt_stand_ins(self):
        counts = (SeriesProject.objects.count(), Shot.objects.count())
        original = deepcopy(self.snapshot)
        result = self.compile()
        self.assertEqual(result.graph["2"]["inputs"]["text"], self.values["prompt.positive"])
        self.assertEqual(result.graph["4"]["inputs"]["width"], 320)
        self.assertEqual(result.graph["5"]["inputs"]["seed"], 37)
        self.assertEqual(result.graph["1"]["inputs"]["ckpt_name"], self.artifact.filename)
        self.assertEqual(result.graph["5"]["inputs"]["positive"], ["2", 0])
        self.assertEqual(result.graph_sha256, self.compile().graph_sha256)
        self.assertEqual(self.snapshot, original)
        self.assertEqual((SeriesProject.objects.count(), Shot.objects.count()), counts)

    def test_missing_request_value_does_not_read_imported_example_or_project_default(self):
        for key in ("prompt.positive", "image.width", "sampling.seed", "output.prefix"):
            with self.subTest(key=key), self.assertRaises(WorkflowCompilerError):
                self.compile(scalar_values={name: value for name, value in self.values.items() if name != key})

    def test_model_filename_and_unknown_value_cannot_be_scalar_overrides(self):
        for key in ("model.base.filename", "undeclared.parameter"):
            with self.subTest(key=key), self.assertRaisesMessage(WorkflowCompilerError, "不能覆盖模型文件或素材来源"):
                self.compile(scalar_values={**self.values, key: "invented"})

    def test_frozen_graph_hash_is_rechecked_before_applying_values(self):
        self.snapshot["graph"]["1"]["inputs"]["ckpt_name"] = "changed.safetensors"
        with self.assertRaisesMessage(WorkflowCompilerError, "graph hash 不一致"):
            self.compile()

    def test_shared_typed_rules_reject_out_of_contract_dimensions(self):
        with self.assertRaises(WorkflowCompilerError):
            self.compile(scalar_values={**self.values, "image.width": 321})

    def test_shared_graph_materializer_rejects_wiring_overwrite_and_preserves_input(self):
        graph = {"1": {"class_type": "Example", "inputs": {"connection": ["2", 0]}}}
        with self.assertRaisesMessage(WorkflowCompilerError, "禁止覆盖 ComfyUI 节点连接"):
            materialize_generic_workflow_graph(graph=graph, contract={}, values={"example.value": "text"},
                bindings=[{"semantic_key": "example.value", "node_id": "1", "input_name": "connection", "is_required": True}],
                compiler_version=1, classified_groups=None)
        self.assertEqual(graph["1"]["inputs"]["connection"], ["2", 0])

    def test_workflow_transport_contains_the_same_compiled_graph_and_original_target(self):
        definition = build_execution_definition(self.version.pk)
        parameters = initialize_execution_definition_parameters(definition,
            {"image.width": 320, "image.height": 512, "sampling.seed": 37}, policies=[],
            resolve_source=lambda *_: self.fail("No implicit parameter source"))
        request = compile_execution_transport_request(definition, prepared_parameters=parameters,
            manual_channels={"positive_prompt": self.values["prompt.positive"], "negative_prompt": "blur"},
            idempotency_key="same-preparation", workflow_snapshot=self.snapshot)
        graph = request.value["payload"]["compiled_workflow"]["graph"]
        self.assertEqual(graph["2"]["inputs"]["text"], self.values["prompt.positive"])
        self.assertEqual(graph["7"]["inputs"]["filename_prefix"], "autoprompt/execution/same-preparation")
        self.assertEqual(request.value["target_fingerprint"], definition.value["target_fingerprint"])
        wrong_snapshot = {**self.snapshot, "workflow_version_id": "different-version"}
        with self.assertRaisesMessage(ValidationError, "原精确工作流合同"):
            compile_execution_transport_request(definition, prepared_parameters=parameters,
                manual_channels={"positive_prompt": self.values["prompt.positive"], "negative_prompt": "blur"},
                idempotency_key="same-preparation", workflow_snapshot=wrong_snapshot)


class ExecutionClassifiedWorkflowValueTests(TestCase):
    """Full frozen Projector/graph, with explicitly synthetic resolved media."""

    _create_version = classified_fixtures.DynamicRef2VAWorkflowTests._create_version

    def setUp(self):
        classified_fixtures.DynamicRef2VAWorkflowTests.setUp(self)
        self.snapshot = workflow_version_execution_snapshot(self.version)
        self.values = {"prompt.positive": "Declared video request.", "image.width": 320, "image.height": 512,
            "video.frames": 124, "video.fps": 24, "sampling.seed": 37, "sampling.steps": 4,
            "parameter.lora.strength": 1, "output.prefix": "execution/classified-use"}

    def test_same_compiler_preserves_artifact_identity_and_per_use_tokens(self):
        bindings = [{"item_key": "selected-output-use", "token": "use:selected-artifact", "kind": "image",
            "asset_id": None, "artifact_id": "explicit-test-artifact", "source_sha256": "a" * 64}]
        result = compile_frozen_workflow_values(workflow_snapshot=self.snapshot, scalar_values=self.values,
            resolve_asset=lambda *_: self.fail("Dynamic media must not be remapped to a fixed Asset slot"),
            classified_groups={"images": [{"token": "use:selected-artifact"}], "videos": [], "audios": []},
            classified_bindings=bindings, output_kind="video", total_duration_ms=5000)
        self.assertEqual(result.graph["cr_images_0"]["inputs"]["image"], "use:selected-artifact")
        self.assertEqual(result.asset_bindings, bindings)
        self.assertIsNone(result.asset_bindings[0]["asset_id"])
        self.assertEqual(result.graph["6"]["inputs"]["prompt"], self.values["prompt.positive"])

    def test_complete_frozen_graph_accepts_one_second_clock_and_rejects_other_legal_frame_count(self):
        def compile_frames(frames):
            return compile_frozen_workflow_values(workflow_snapshot=self.snapshot,
                scalar_values={**self.values, "video.frames": frames},
                resolve_asset=lambda *_: self.fail("No fixed asset slot"),
                classified_groups={"images": [], "videos": [], "audios": []}, classified_bindings=[],
                output_kind="video", total_duration_ms=1000,
                video_duration_policy={"minimum_duration_ms": 1000, "maximum_duration_ms": 15000})
        result = compile_frames(22)
        self.assertEqual(result.resolved_semantics["video.frames"], 22)
        self.assertFalse(result.asset_bindings)
        self.assertFalse(any(row["class_type"] in {"LoadImage", "LoadVideo", "LoadAudio"} for row in result.graph.values()))
        with self.assertRaisesMessage(WorkflowCompilerError, "分镜时长不一致"):
            compile_frames(39)

    def test_exact_model_widget_value_and_manual_prompt_are_not_rewritten(self):
        original = deepcopy(self.snapshot)
        values = {**self.values, "prompt.positive": "  人工第一行\n第二行：保留间距。  "}
        compiled = compile_frozen_workflow_values(workflow_snapshot=self.snapshot, scalar_values=values,
            resolve_asset=lambda *_: self.fail("No fixed asset slot was declared"),
            classified_groups={"images": [], "videos": [], "audios": []}, classified_bindings=[],
            output_kind="video", total_duration_ms=None)
        exact = self.snapshot["graph"]["2"]["inputs"]["lora_name"]
        self.assertEqual(compiled.graph["2"]["inputs"]["lora_name"], exact)
        self.assertEqual(compiled.resolved_semantics["model.h3-turbo-lora.filename"], exact)
        self.assertEqual(compiled.graph["6"]["inputs"]["prompt"], values["prompt.positive"])
        self.assertEqual(self.snapshot, original)
        self.snapshot["graph"]["2"]["inputs"]["lora_name"] = "different-model.safetensors"
        with self.assertRaisesMessage(WorkflowCompilerError, "graph hash"):
            compile_frozen_workflow_values(workflow_snapshot=self.snapshot, scalar_values=values,
                resolve_asset=lambda *_: self.fail("No fixed asset slot was declared"),
                classified_groups={"images": [], "videos": [], "audios": []}, classified_bindings=[],
                output_kind="video", total_duration_ms=None)
