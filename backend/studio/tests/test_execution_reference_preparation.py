from studio.tests.field_definition_fixtures import create_field_definition
"""Published composition -> persisted multi-unit fields -> shared preview.

All dependencies and source inputs are isolated test data. No provider call,
Smoke certification or additional project/shot is created for the request.
"""
from copy import deepcopy
from dataclasses import asdict
import json

from django.core.exceptions import ValidationError
from django.test import TestCase, override_settings

from studio.tests.field_definition_fixtures import create_profile_field_rule
from studio.models import AssetGenerationJob, ModelDeployment, ModelProfileVersion, ProviderProfile, FieldDefinition, PromptProfileFieldRule, PromptProfileVersion, SeriesProject, Shot, WorkflowTemplate
from studio.services.execution_definitions import build_execution_definition
from studio.services.execution_instances import create_execution_draft, create_execution_instance, publish_execution_version, update_execution_draft, model_family_for_profile
from studio.services.execution_input_resolution import resolve_execution_input_sources
from studio.services.execution_parameter_preparation import initialize_execution_definition_parameters
from studio.services.execution_preparation_fields import create_manual_execution_field, confirm_execution_field
from studio.services.execution_preparations import ResolvedExecutionPreparation, create_execution_instance_preparation
from studio.services.execution_profile_inputs import profile_input_targets
from studio.services.execution_profile_preparation import prepare_execution_profile, compile_preparation_profile
from studio.services.execution_preparation_requests import compile_prepared_execution_transport
from studio.services.execution_reference_inputs import REFERENCE_CONTEXT_INPUT_KEY
from studio.services.json_contracts import assign_json_contract, json_contract
from studio.services.prompt_profiles import publish_prompt_profile, refresh_prompt_profile_contract
from studio.services.workflow_versions import import_workflow_version, workflow_version_execution_snapshot
from studio.services.workflow_compiler import compile_frozen_workflow_values
from studio.tests import test_execution_definitions as workflow_fixtures
from studio.tests import test_execution_auxiliary_definitions as leaf_fixtures
from studio.tests import test_reference_context_facts as unit_fixtures
from studio.tests.test_execution_text_contracts import text_specification
from studio.tests.test_media_capabilities import _contract, _mode
from studio.tests.reference_rendering_fixtures import rendering_config, generation_structure_fixture


@override_settings(AI_PROVIDER_ALLOWED_HOSTS=("api.example.com", "127.0.0.1"))
class ExecutionReferencePreparationTests(TestCase):
    input = staticmethod(leaf_fixtures.ExecutionAuxiliaryDefinitionTests.input)

    def write(self, obj, field, value):
        assign_json_contract(obj, field, value)
        obj.save()
        return obj

    def setUp(self, *, configure_profile=None):
        workflow_fixtures.WorkflowExecutionDefinitionTests.setUp(self)
        self.model = self.profile
        spec = deepcopy(self.model.specification)
        spec["model_family"] = "configured-reference-model"
        spec["conditioning_contract"] = _contract(_mode("explicit-unit-mode"))
        self.write(self.model, "specification", spec)
        # Import a separate explicit video contract. Do not mutate the published
        # image fixture or bypass its real Workflow/Projector integrity checks.
        graph = deepcopy(self.workflow.graph)
        graph["7"] = {"class_type": "SaveVideo", "inputs": {
            "video": ["6", 0], "filename_prefix": "fixture", "frame_rate": 24}}
        graph["4"]["inputs"]["frames"] = 48
        contract = deepcopy(self.workflow.input_contract)
        contract.update({"capability": "video.text_to_video", "conditioning_contract": spec["conditioning_contract"],
            "static_semantics": {"audio.enabled": getattr(self, "reference_audio_enabled", False)},
            "frame_policy": {"fps": 24, "minimum_frames": 1, "maximum_frames": 240,
                "alignment_modulus": 1, "alignment_remainder": 0},
            "outputs": {"primary": {"node_id": "7", "kind": "video", "required": True}}})
        contract["parameters"].update({"video.fps": {"type": "number", "required": True},
            "video.frames": {"type": "integer", "required": True, "min": 1, "max": 240}})
        contract["parameters"].pop("prompt.negative")
        bindings = list(self.workflow.semantic_bindings.exclude(semantic_key="prompt.negative").values(
            "semantic_key", "node_id", "input_name", "value_type", "is_required"))
        bindings += [{"semantic_key": "video.fps", "node_id": "7", "input_name": "frame_rate", "value_type": "number"},
            {"semantic_key": "video.frames", "node_id": "4", "input_name": "frames", "value_type": "integer"}]
        template = WorkflowTemplate.objects.create(name="Explicit video", slug="reference-unit-video", output_kind="video",
            provider="comfyui", model=self.model.model_identifier, definition={}, input_schema={}, is_system=True, is_active=False)
        self.workflow = import_workflow_version(template=template, graph=graph, input_contract=contract,
            semantic_bindings=bindings, model_requirements=list(self.workflow.model_requirements.values(
                "role", "alias", "artifact_id", "expected_filename", "expected_sha256", "is_required")),
            provider=self.provider, model_profile_version=self.model)
        self.workflow.status = "published"
        self.workflow.validated_deployment = self.deployment
        self.workflow.save()
        leaf_provider = self.write(ProviderProfile(key="reference-field-provider", name="Field", driver_type="openai_compatible", modalities="text"),
            "driver_config", {"base_url": "https://api.example.com", "chat_path": "/v1/chat/completions", "timeout_seconds": 30, "capability_ttl_seconds": 3600})
        leaf_model = self.write(ModelProfileVersion(profile_key="reference-field-model", version=1, provider=leaf_provider,
            model_identifier="declared-text-model", modality="text", status="published"), "specification", text_specification())
        leaf_deployment = self.write(ModelDeployment(key="reference-leaf", name="Field", profile_version=leaf_model), "runtime_config", {})
        parent = create_execution_instance(key="reference-leaf", name="Field", description="", actor=self.user)
        leaf = create_execution_draft(instance_id=parent.pk, expected_revision=parent.revision, actor=self.user,
            selection={"deployment": leaf_deployment, "operation": "prompt.fields.generate", "mode": "", "workflow_version": None,
                "prompt_profile_version": None, "prompt_mode": "none"})
        leaf = update_execution_draft(version_id=leaf.pk, expected_revision=leaf.revision, actor=self.user,
            changes={"inputs": [self.input("field_request", "field_request", kind="json", target_kind="operation_input")]})
        leaf = publish_execution_version(version_id=leaf.pk, expected_revision=leaf.revision,
            expected_definition_sha256=build_execution_definition(leaf.pk).sha256, actor=self.user)
        field = create_field_definition(key="unit_description", label="Unit description", value_type="long_text")
        projection_config = rendering_config()
        projection_config.pop("quality_advisories")
        profile = PromptProfileVersion.objects.create(profile_key="reference-composition", name="Reference composition", version=1,
            model_family=model_family_for_profile(self.model), operation="video.generate", mode="explicit-unit-mode", review_state="ready",
            contract_schema_version=8, source_projection_key="declarative_v1", source_projection_config=projection_config)
        create_profile_field_rule(profile_version=profile, field_definition=field, source_selector="projection.output",
            projection_primitive="reference_context.detailed_description", generation_mode="model_generated",
            generation_output_semantics="contribution", generation_source_selector="request.structured_input", source_path=["direction"],
            generation_context_selectors=["project.style"], generation_instruction="Use the declared facts for this unit only.",
            generation_required=True, language_policy="en", channel="positive_prompt", position=1, required=True, max_chars=7000)
        if configure_profile is not None:
            configure_profile(profile)
        else:
            generation_structure_fixture(profile)
        profile.channel_limits = getattr(self, "channel_limits", {})
        profile.save(update_fields=("channel_limits",))
        refresh_prompt_profile_contract(profile)
        self.prompt_profile = publish_prompt_profile(profile, actor=self.user)
        self.version = update_execution_draft(version_id=self.version.pk, expected_revision=self.version.revision, actor=self.user,
            changes={"operation": "video.text_to_video", "workflow_version": self.workflow,
                "mode": "explicit-unit-mode", "prompt_mode": "profile", "prompt_profile_version": self.prompt_profile,
                "inputs": [self.input("units", REFERENCE_CONTEXT_INPUT_KEY, kind="json")] + (
                    [] if getattr(self, "raw_only", False) else [self.input("styles", "context.project.style", kind="json", position=1)]),
                "auxiliaries": [] if getattr(self, "raw_only", False) else [{"purpose": "generate_fields", "target_version_id": str(leaf.pk)}]})
        self.definition = build_execution_definition(self.version.pk)
        self.version = publish_execution_version(version_id=self.version.pk, expected_revision=self.version.revision,
            expected_definition_sha256=self.definition.sha256, actor=self.user)
        unit_fixtures.ReferenceContextFactTests.setUp(self)
        unit = json.loads(json.dumps(asdict(self.unit)))
        unit.pop("render_values")
        first, second = deepcopy(unit), deepcopy(unit)
        first["unit_key"], second["unit_key"] = "first", "second"
        first["structured_input"] = {"direction": "A courier arrives."}
        second["structured_input"] = {"direction": "A parcel opens."}
        self.sources = {"units": {"kind": "literal", "value": {"schema_version": 1, "ordered_units": [first, second],
            "color_script": "", "extra_direction": "", "overall_soundscape": "", "non_diegetic_music": ""}},
            "styles": {"kind": "literal", "value": {"first": {"palette": "blue"}, "second": {"palette": "red"}}}}
        self.parameters = {"image.width": 512, "image.height": 512, "sampling.seed": 7, "video.fps": 24, "video.frames": 48}
        if getattr(self, "raw_only", False):
            self.sources.pop("styles")
        self.job = AssetGenerationJob.objects.create(owner=self.user, model_deployment=self.deployment, execution_instance_version=self.version,
            workflow_template_version=self.version.workflow_version, execution_kind="comfy_workflow",
            job_type="asset", media_type="video", operation=self.version.operation, prompt="", idempotency_key="reference-preparation",
            request_snapshot_sha256=json_contract({}).sha256)

    def resolve(self, definition, owner):
        inputs = resolve_execution_input_sources(definition, self.sources, owner=owner.owner, subject_facts=[], max_media_bytes=65536)
        parameters = initialize_execution_definition_parameters(definition, self.parameters, policies=[],
            resolve_source=lambda _: self.fail("No hidden parameter source"))
        profile = prepare_execution_profile(definition, inputs, profile=self.prompt_profile, unit_key="composition",
            source_language="English", target_language="English", prepared_parameters=parameters, scope_units={})
        return ResolvedExecutionPreparation(json_contract({**profile.value, "inputs": inputs.value, "parameters": parameters}),
            json_contract({"schema_version": 1, "mode": "manual"}))

    def prepare(self):
        self.prep = create_execution_instance_preparation(owner_ref=("asset_job_id", self.job.pk), version_id=self.version.pk,
            expected_definition_sha256=self.definition.sha256, unit_key="composition", request_key="candidate-1",
            intent=json_contract({"inputs": self.sources, "parameters": self.parameters}), resolve_facts=self.resolve, actor=self.user)
        return self.prep

    def preview(self):
        self.prep.refresh_from_db()
        return compile_preparation_profile(preparation_id=self.prep.pk, expected_preparation_revision=self.prep.revision, actor=self.user)

    def select(self, unit, text):
        self.prep.refresh_from_db()
        row = create_manual_execution_field(preparation_id=self.prep.pk, unit_key=unit, field_key="unit_description", text=text,
            revision=1, expected_preparation_revision=self.prep.revision, actor=self.user)
        self.prep.refresh_from_db()
        confirm_execution_field(preparation_id=self.prep.pk, field_value_id=row.pk, expected_text_sha256=row.text_sha256,
            expected_source_sha256=row.source_sha256, expected_preparation_revision=self.prep.revision,
            select_for_execution=True, expected_selection_revision=None, replace=False, actor=self.user)
        return row

    def test_published_definition_exposes_structured_units_and_per_unit_contexts(self):
        targets = profile_input_targets(self.prompt_profile)
        self.assertEqual(set(targets), {REFERENCE_CONTEXT_INPUT_KEY, "context.project.style"})
        self.assertIn("ordered_units", targets[REFERENCE_CONTEXT_INPUT_KEY]["value_schema"]["properties"])
        self.assertEqual(targets["context.project.style"]["value_domain"], "unit_context")
        self.assertEqual(self.definition.value["interface"]["prompt"]["prompt_operation"], "video.generate")
        self.assertEqual(self.definition.value["resources"]["execution"]["operation"], "video.text_to_video")

    def test_root_uses_frozen_video_output_kind_for_reference_projection(self):
        from unittest.mock import patch
        from studio.services.execution_reference_materials import project_execution_reference_materials
        with patch("studio.services.execution_profile_preparation.project_execution_reference_materials",
                wraps=project_execution_reference_materials) as projector:
            self.prepare()
        self.assertEqual(projector.call_count, 1)
        self.assertEqual(projector.call_args.kwargs["prompt_type"], "video")
        self.assertEqual(projector.call_args.kwargs["unit_keys"], ["first", "second"])

    def test_saved_multi_unit_choices_compile_through_the_same_profile_without_extra_owners(self):
        counts = (SeriesProject.objects.count(), Shot.objects.count())
        self.prepare()
        with self.assertRaisesMessage(ValidationError, "尚未明确采用"):
            self.preview()
        first = self.select("first", "A courier arrives beside a blue doorway.")
        with self.assertRaisesMessage(ValidationError, "尚未明确采用"):
            self.preview()
        second = self.select("second", "A red parcel opens on the table.")
        result = self.preview()
        text = result.resolved_channels["values"]["positive_prompt"]
        self.assertIn(first.text, text)
        self.assertIn(second.text, text)
        self.assertLess(text.index(first.text), text.index(second.text))
        self.assertEqual(result.resolved_channels, self.preview().resolved_channels)
        self.assertEqual(len(result.compilation.reviewed_field_evidence["field_selections"]), 2)
        self.assertEqual((SeriesProject.objects.count(), Shot.objects.count()), counts)
        self.prep.refresh_from_db()
        self.assertIsNone(self.prep.request_frozen_at)
        self.assertEqual(AssetGenerationJob.objects.count(), 1)
        from studio.services.execution_prompt_display import read_preparation_prompt_display
        display = read_preparation_prompt_display(preparation_id=self.prep.pk, expected_preparation_revision=self.prep.revision, actor=self.user)
        self.assertEqual(display["mode"], "preview")
        self.assertEqual(display["channels"][0]["text"], text)
        self.assertEqual(len(display["field_basis"]), 2)

    def test_context_keys_cannot_miss_a_unit_or_add_an_unrelated_one(self):
        for values in ({"first": {}}, {"first": {}, "second": {}, "third": {}}):
            with self.subTest(values=values):
                self.sources["styles"]["value"] = values
                with self.assertRaisesMessage(ValidationError, "有序单元一一对应"):
                    self.prepare()
                self.assertFalse(self.job.execution_preparations.exists())

    def test_duration_and_actual_frame_parameters_must_agree(self):
        self.parameters["video.frames"] = 47
        with self.assertRaisesMessage(ValidationError, "视频帧数不一致"):
            self.prepare()
        self.assertFalse(self.job.execution_preparations.exists())

    def test_preview_keeps_frozen_sources_and_does_not_adopt_a_new_manual_candidate(self):
        self.prepare()
        self.select("first", "A courier arrives beside a blue doorway.")
        self.select("second", "A red parcel opens on the table.")
        original = self.preview()
        self.sources["units"]["value"]["ordered_units"][0]["structured_input"]["direction"] = "Later source edit."
        self.prep.refresh_from_db()
        create_manual_execution_field(preparation_id=self.prep.pk, unit_key="first", field_key="unit_description",
            text="Unreviewed replacement must not leak.", revision=2,
            expected_preparation_revision=self.prep.revision, actor=self.user)
        current = self.preview()
        self.assertEqual(current.resolved_channels, original.resolved_channels)
        self.assertNotIn("Later source edit", str(current.compilation))
        self.assertNotIn("Unreviewed replacement", str(current.compilation))
        self.assertGreater(current.preparation_revision, original.preparation_revision)

    def test_selected_contributions_reach_frozen_workflow_nodes_without_a_fake_prompt(self):
        self.prepare()
        self.select("first", "A courier arrives beside a blue doorway.")
        self.select("second", "A red parcel opens on the table.")
        preview = self.preview()
        compiled = compile_frozen_workflow_values(workflow_snapshot=workflow_version_execution_snapshot(self.workflow),
            scalar_values={**self.parameters, "prompt.positive": preview.resolved_channels["values"]["positive_prompt"],
                "output.prefix": f"execution/{self.prep.pk}"},
            resolve_asset=lambda *_: self.fail("No media port declared"), classified_groups=None, classified_bindings=[],
            output_kind="video", total_duration_ms=2000,
            video_duration_policy={"minimum_duration_ms": 2000, "maximum_duration_ms": 2000})
        self.assertIn("A courier arrives", compiled.graph["2"]["inputs"]["text"])
        self.assertIn("A red parcel opens", compiled.graph["2"]["inputs"]["text"])
        self.assertEqual(compiled.graph["4"]["inputs"]["frames"], 48)
        self.assertEqual(compiled.graph["7"]["inputs"]["frame_rate"], 24)
        self.assertIs(compiled.resolved_semantics["audio.enabled"], False)
        self.prep.refresh_from_db()
        request = compile_prepared_execution_transport(preparation_id=self.prep.pk,
            expected_preparation_revision=self.prep.revision, actor=self.user)
        body = request.transport.value["payload"]["compiled_workflow"]
        self.assertEqual(body["graph"]["2"]["inputs"]["text"], compiled.graph["2"]["inputs"]["text"])
        self.assertEqual(body["graph"]["4"]["inputs"]["frames"], 48)
        self.assertEqual(body["workflow_contract_sha256"], self.workflow.execution_contract_sha256)
        self.prep.refresh_from_db()
        self.assertIsNone(self.prep.request_frozen_at)

    def test_missing_structured_unit_fact_is_not_filled_from_workflow_or_project(self):
        self.sources["units"]["value"]["ordered_units"][0]["structured_input"].pop("direction")
        with self.assertRaises(ValidationError):
            self.prepare()
        self.assertFalse(self.job.execution_preparations.exists())
