"""Prepared input observations and the existing configured-size graph rules."""
from copy import deepcopy
from uuid import uuid4
from unittest.mock import patch

from django.test import TestCase, override_settings
from django.core.exceptions import ValidationError
from django.core.files.base import ContentFile
from io import BytesIO
from hashlib import sha256
from tempfile import TemporaryDirectory
from PIL import Image

from studio.services.generated_artifacts import verified_local_media_facts
from studio.services.json_contracts import json_contract
from studio.services.provenance import ProvenanceError
from studio.services.workflow_compiler import compile_frozen_workflow_values, WorkflowCompilerError
from studio.services.workflow_media_tokens import workflow_media_use_token
from studio.tests import test_workflow_resolution as fixtures
from studio.tests import test_execution_definitions as definition_fixtures
from studio.models import Asset, WorkflowTemplate
from studio.services.execution_definitions import build_execution_definition
from studio.services.execution_instances import update_execution_draft
from studio.services.execution_input_resolution import resolve_execution_input_sources
from studio.services.execution_parameter_preparation import initialize_execution_definition_parameters
from studio.services.execution_request_compilation import compile_execution_transport_request
from studio.services.json_contracts import assign_json_contract
from studio.services.workflow_versions import import_workflow_version, workflow_version_execution_snapshot
from studio.services.workflow_resolution import infer_source_image_resolution, configure_source_image_resolution, CONFIGURED_RESOLUTION_ADAPTER
from studio.tests.workflow_projector_fixtures import workflow_projector_reference


class ExecutionPreparedImageResolutionTests(TestCase):
    setUp = fixtures.WorkflowResolutionTests.setUp
    snapshot = fixtures.WorkflowResolutionTests.snapshot
    asset = fixtures.WorkflowResolutionTests.asset
    configure = fixtures.WorkflowResolutionTests.configure

    def source(self, **changes):
        asset = self.asset((1600, 900), orientation=6)
        media = verified_local_media_facts(self.root / "source.png", expected_kind="image",
            max_bytes=65536, include_observed_media=True)
        binding = {"semantic_key": "asset.image.source", "item_key": "source-use", "asset_id": str(asset.pk),
            "artifact_id": None, "kind": "image", "media_facts": media, **changes}
        binding["source_sha256"] = json_contract(binding).sha256
        binding["token"] = workflow_media_use_token(item_key=binding["item_key"], source_sha256=binding["source_sha256"])
        return binding

    def compile(self, binding):
        return compile_frozen_workflow_values(workflow_snapshot=self.snapshot(),
            scalar_values={"sampling.seed": 7, "output.prefix": "execution/source-dimensions"},
            resolve_asset=lambda key, rule: (binding["token"], binding), classified_groups=None, classified_bindings=[],
            output_kind="image", total_duration_ms=None)

    def test_oriented_real_dimensions_use_frozen_candidates_and_materialize_the_shared_graph(self):
        self.configure([{"width": 512, "height": 512}, {"width": 576, "height": 1024}])
        source = self.source()
        self.assertEqual(source["media_facts"]["observed_media"], {"schema_version": 1,
            "observation_source": "image_decoder", "width": 900, "height": 1600, "frame_count": 1})
        original = deepcopy(self.graph)
        compiled = self.compile(source)
        self.assertEqual(self.graph, original)
        evidence = compiled.output_contract["resolution"]
        self.assertEqual((evidence["source_width"], evidence["source_height"]), (900, 1600))
        self.assertEqual((evidence["width"], evidence["height"]), (576, 1024))
        self.assertEqual(evidence["source_item_key"], "source-use")
        self.assertEqual(evidence["source_checksum"], source["media_facts"]["sha256"])
        self.assertEqual(compiled.graph["2"]["class_type"], "ImageScale")
        self.assertEqual(compiled.graph["2"]["inputs"]["width"], 576)
        self.assertEqual(compiled.graph["1"]["inputs"]["image"], source["token"])

    def test_selected_artifact_identity_is_not_replaced_by_an_asset_id(self):
        # Compiler protocol fixture: this does not certify a real selection.
        artifact_id = str(uuid4())
        compiled = self.compile(self.source(asset_id=None, artifact_id=artifact_id))
        evidence = compiled.output_contract["resolution"]
        self.assertIsNone(evidence["source_asset_id"])
        self.assertEqual(evidence["source_artifact_id"], artifact_id)

    def test_missing_multiframe_or_invalid_observations_cannot_read_example_sizes(self):
        source = self.source()
        observations = [None, {}, {"observation_source": "image_decoder", "frame_count": 2, "width": 900, "height": 1600},
            {"observation_source": "image_decoder", "frame_count": 1, "width": True, "height": 1600}]
        for observed in observations:
            with self.subTest(observed=observed), self.assertRaises(WorkflowCompilerError):
                self.compile({**source, "media_facts": {**source["media_facts"], "observed_media": observed}})

    def test_multiframe_image_is_observed_but_not_used_as_a_static_resolution_source(self):
        path = self.root / "animated.gif"
        Image.new("RGB", (12, 18), "red").save(path, save_all=True,
            append_images=[Image.new("RGB", (12, 18), "blue")], format="GIF", duration=100)
        facts = verified_local_media_facts(path, expected_kind="image", max_bytes=65536, include_observed_media=True)
        self.assertEqual(facts["observed_media"]["frame_count"], 2)
        with self.assertRaisesMessage(WorkflowCompilerError, "单帧"):
            self.compile(self.source(media_facts=facts))

    def test_decoder_size_warning_is_a_controlled_source_error_not_unbounded_loading(self):
        path = self.root / "large-relative-to-test-limit.png"
        Image.new("RGB", (15, 10), "blue").save(path)
        with patch.object(Image, "MAX_IMAGE_PIXELS", 100), self.assertRaises(ProvenanceError):
            verified_local_media_facts(path, expected_kind="image", max_bytes=65536, include_observed_media=True)


@override_settings(AI_PROVIDER_ALLOWED_HOSTS=("127.0.0.1",))
class ExecutionImageResolutionRequestTests(TestCase):
    """Full registry-derived interface, real fixed media slot and model limits."""

    def setUp(self):
        definition_fixtures.WorkflowExecutionDefinitionTests.setUp(self)
        root = TemporaryDirectory()
        self.addCleanup(root.cleanup)
        setting = override_settings(MEDIA_ROOT=root.name)
        setting.enable()
        self.addCleanup(setting.disable)
        specification = deepcopy(self.profile.specification)
        specification["media_constraints"] = {"schema_version": 1,
            "resolution": {"maximum_width": 768, "maximum_height": 768}, "fps": {"allowed": []}, "duration_ms": {}}
        assign_json_contract(self.profile, "specification", specification)
        self.profile.save()
        graph, contract = deepcopy(self.workflow.graph), deepcopy(self.workflow.input_contract)
        graph["4"] = {"class_type": "VAEEncode", "inputs": {"pixels": ["10", 0], "vae": ["1", 2]}}
        graph["9"] = {"class_type": "LoadImage", "inputs": {"image": "__AUTOPROMPT_ASSET__:managed"}}
        graph["10"] = {"class_type": "FluxKontextImageScale", "inputs": {"image": ["9", 0]}}
        for key in ("image.width", "image.height"):
            contract["parameters"].pop(key)
        contract["parameters"]["asset.image.source"] = {"type": "asset_image", "required": True,
            "min_items": 1, "max_items": 1, "input_slot": 1, "asset_category": "image"}
        bindings = list(self.workflow.semantic_bindings.exclude(semantic_key__in=["image.width", "image.height"]).values(
            "semantic_key", "node_id", "input_name", "value_type", "is_required"))
        bindings.append({"semantic_key": "asset.image.source", "node_id": "9", "input_name": "image", "value_type": "asset_image", "is_required": True})
        policy = {"adapter": CONFIGURED_RESOLUTION_ADAPTER, "sizes": [{"width": 512, "height": 768}, {"width": 1024, "height": 576}]}
        contract.update(workflow_projector_reference(image_resolution=policy))
        contract["resolution_strategy"] = configure_source_image_resolution(infer_source_image_resolution(graph, bindings, "7"), policy)
        contract["required_dimension_multiple"] = 16
        template = WorkflowTemplate.objects.create(name="Source size", slug="source-size-execution", output_kind="image",
            provider="comfyui", model=self.profile.model_identifier, definition={}, input_schema={}, is_system=True, is_active=False)
        self.workflow = import_workflow_version(template=template, graph=graph, input_contract=contract,
            semantic_bindings=bindings, model_requirements=list(self.workflow.model_requirements.values(
                "role", "alias", "artifact_id", "expected_filename", "expected_sha256", "is_required")),
            provider=self.provider, model_profile_version=self.profile)
        self.workflow.status, self.workflow.validated_deployment = "published", self.deployment
        self.workflow.save()
        self.version = update_execution_draft(version_id=self.version.pk, expected_revision=self.version.revision, actor=self.user,
            changes={"workflow_version": self.workflow, "inputs": [{"key": "picture", "position": 0, "kind": "image",
                "required": True, "min_items": 1, "max_items": 1, "semantic_contract": {"schema_version": 1,
                    "allowed_purposes": [], "allowed_subject_kinds": [], "allowed_scope_kinds": ["request"],
                    "paired_with_input_key": None, "allow_repeated_asset": False},
                "targets": [{"target_kind": "workflow_slot", "target_key": "asset.image.source", "is_primary": True}]}]})
        self.definition = build_execution_definition(self.version.pk)
        self.parameters = initialize_execution_definition_parameters(self.definition, {"sampling.seed": 7},
            policies=[], resolve_source=lambda *_: self.fail("No scalar width default"))

    def compile(self, size):
        content = BytesIO()
        Image.new("RGB", size, "blue").save(content, format="PNG")
        asset = Asset(owner=self.user, name="Source", kind="reference", checksum=sha256(content.getvalue()).hexdigest())
        asset.file.save("source.png", ContentFile(content.getvalue()))
        inputs = resolve_execution_input_sources(self.definition, {"picture": {"items": [
            {"kind": "asset", "item_key": "picture-use", "asset_id": str(asset.pk), "purpose": None}]}},
            owner=self.user, subject_facts=[], max_media_bytes=65536)
        return compile_execution_transport_request(self.definition, prepared_parameters=self.parameters,
            manual_channels={"positive_prompt": "Describe <Picture 1>.", "negative_prompt": "blur"},
            workflow_inputs=inputs, workflow_snapshot=workflow_version_execution_snapshot(self.workflow), idempotency_key="source-size")

    def test_frozen_model_limits_are_checked_after_real_source_dimension_resolution(self):
        result = self.compile((12, 18))
        compiled = result.value["payload"]["compiled_workflow"]
        self.assertEqual(compiled["output_contract"]["schema_version"], 2)
        self.assertEqual(compiled["graph"]["10"]["inputs"]["width"], 512)
        self.assertEqual(compiled["graph"]["10"]["inputs"]["height"], 768)
        self.assertNotIn("image.width", self.parameters["values"])
        self.assertEqual(compiled["asset_bindings"][0]["item_key"], "picture-use")
        with self.assertRaisesMessage(ValidationError, "width 超过模型上限"):
            self.compile((16, 9))

    def test_request_reader_checks_saved_resolution_evidence_not_other_candidate_sizes(self):
        from studio.services.execution_workflow_requests import verify_execution_workflow_transport
        result = self.compile((12, 18))
        self.assertEqual(verify_execution_workflow_transport(self.definition, result), result.value["payload"]["compiled_workflow"])
        for section in ("output_contract", "resolved_semantics"):
            bad = deepcopy(result.value)
            compiled = bad["payload"]["compiled_workflow"]
            if section == "output_contract":
                compiled[section]["resolution"]["width"] = 1024
            else:
                compiled[section]["image.width"] = 1024
                compiled["resolved_semantics_sha256"] = json_contract(compiled[section]).sha256
            with self.subTest(section=section), self.assertRaises(ValidationError):
                verify_execution_workflow_transport(self.definition, json_contract(bad))
