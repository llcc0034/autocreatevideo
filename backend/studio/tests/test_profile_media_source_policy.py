"""Profile consumption of one frozen media policy and actual local PNG files.

The Workflow's published/validated state is an isolated fixture, not real Smoke.
Instance publication, preparation, rendering and graph compilation use original
services. Remote clients are guarded; this suite never submits generation.
"""
from copy import deepcopy
from dataclasses import asdict, replace
import json

from django.core.exceptions import ValidationError
from django.db import connection
from django.test import TestCase, override_settings
from django.test.utils import CaptureQueriesContext

from studio.models import AssetGenerationJob, ExecutionPreparation, ModelExecutionInstanceVersion, PromptProfileVersion
from studio.services.asset_provenance import ASSET_UPLOAD_SOURCE_INFO
from studio.services.execution_definitions import build_execution_definition
from studio.services.execution_input_resolution import resolve_execution_input_sources
from studio.services.execution_instances import model_family_for_profile, publish_execution_version, update_execution_draft
from studio.services.execution_parameter_preparation import initialize_execution_definition_parameters
from studio.services.execution_preparation_requests import compile_prepared_execution_transport
from studio.services.execution_preparations import ResolvedExecutionPreparation, create_execution_instance_preparation
from studio.services.execution_profile_preparation import compile_preparation_profile, prepare_execution_profile
from studio.services.execution_reference_inputs import REFERENCE_CONTEXT_INPUT_KEY
from studio.services.execution_reference_materials import project_execution_reference_materials
from studio.services.json_contracts import assign_json_contract, json_contract
from studio.services.prompt_profiles import publish_prompt_profile, refresh_prompt_profile_contract
from studio.services.production_recipe_media_selection import bind_recipe_media_selection
from studio.tests.field_definition_fixtures import create_field_definition, create_profile_field_rule
from studio.tests.reference_rendering_fixtures import generation_structure_fixture, rendering_config
from studio.tests.test_execution_media_source_policy import choices
from studio.tests import test_execution_workflow_request_media as workflow_fixture
from studio.tests import test_reference_context_facts as context_fixture
from studio.tests import test_execution_selected_outputs as selected_fixture


@override_settings(AI_PROVIDER_ALLOWED_HOSTS=("127.0.0.1",))
class ProfileMediaSourcePolicyTests(TestCase):
    def _create_version(self):
        assign_json_contract(self.profile, "specification", {**self.profile.specification, "model_family": "configured-media-family"})
        self.profile.save()
        return workflow_fixture.ExecutionWorkflowMediaRequestTests._create_version(self)

    def setUp(self, *, configure_profile=None):
        workflow_fixture.ExecutionWorkflowMediaRequestTests.setUp(self)
        context_fixture.ReferenceContextFactTests.setUp(self)
        config = rendering_config()
        config.pop("quality_advisories")
        config["primitive_options"]["reference_context_ir_v1"]["rendering"]["unit_text_bindings"] = {"description": ["direction"]}
        config["primitive_options"]["reference_context_ir_v1"]["rendering"]["reference_rules"].append({
            "category": "image", "purpose": "", "task": "reference generation", "relationship": "fully_preserved",
            "usage": "Use the selected picture."})
        self.prompt_profile = PromptProfileVersion.objects.create(profile_key="media-source-consumer", name="Media consumer",
            version=1, model_family=model_family_for_profile(self.profile), operation="video.generate", mode="Ref2VA",
            review_state="ready", contract_schema_version=8, source_projection_key="declarative_v1",
            source_projection_config=config)
        generation_structure_fixture(self.prompt_profile)
        field = create_field_definition(key="configured_action", label="Action", value_type="long_text")
        create_profile_field_rule(profile_version=self.prompt_profile, field_definition=field,
            source_selector="projection.output", projection_primitive="reference_context.detailed_description",
            generation_mode="passthrough", generation_source_selector="projection.output",
            generation_output_semantics="final", language_policy="preserve_source", generation_required=False,
            channel="positive_prompt", position=1, required=True, max_chars=7000)
        auxiliaries = configure_profile(self.prompt_profile) if configure_profile is not None else []
        refresh_prompt_profile_contract(self.prompt_profile)
        self.prompt_profile = publish_prompt_profile(self.prompt_profile, actor=self.user)
        version = ModelExecutionInstanceVersion.objects.get(pk=self.definition.value["version_id"])
        self.spec = deepcopy(self.definition.value["configuration"]["inputs"][0])
        self.spec["media_source_policy"] = choices()
        unit_spec = {"key": "units", "position": 1, "kind": "json", "required": True, "min_items": 1, "max_items": 1,
            "semantic_contract": {"schema_version": 1, "allowed_purposes": [], "allowed_subject_kinds": [],
                "allowed_scope_kinds": ["request"], "paired_with_input_key": None, "allow_repeated_asset": False},
            "targets": [{"target_kind": "profile_input", "target_key": REFERENCE_CONTEXT_INPUT_KEY, "is_primary": False}]}
        self.version = update_execution_draft(version_id=version.pk, expected_revision=version.revision, actor=self.user,
            changes={"prompt_mode": "profile", "prompt_profile_version": self.prompt_profile,
                "inputs": [self.spec, unit_spec], "auxiliaries": auxiliaries})
        self.definition = build_execution_definition(self.version.pk)
        self.version = publish_execution_version(version_id=self.version.pk, expected_revision=self.version.revision,
            expected_definition_sha256=self.definition.sha256, actor=self.user)
        self.parameter_values = {key: self.parameters["values"][key] for key in (
            "image.width", "image.height", "video.frames", "video.fps", "sampling.seed", "sampling.steps", "parameter.lora.strength")}
        unit = json.loads(json.dumps(asdict(replace(self.unit, unit_key="composition", duration_ms=5000))))
        unit.pop("render_values")
        unit["structured_input"] = {"direction": "A red object turns slowly."}
        self.sources = {"references": {"selection": self.selection()}, "units": {"kind": "literal", "value": {
            "schema_version": 1, "ordered_units": [unit],
            "color_script": "", "extra_direction": "", "overall_soundscape": "", "non_diegetic_music": ""}}}
        self.job = AssetGenerationJob.objects.create(owner=self.user, model_deployment=self.deployment,
            execution_instance_version=self.version, workflow_template_version=self.workflow,
            execution_kind="comfy_workflow", job_type="asset", media_type="video", operation=self.version.operation,
            prompt="", idempotency_key="profile-media-policy", request_snapshot_sha256=json_contract({}).sha256)

    def selection(self, source="asset_library"):
        return {"source": source, "items": [{"kind": "asset", "item_key": key, "asset_id": str(self.asset.pk), "purpose": None}
            for key in ("second", "first")]}

    def resolve(self, definition, owner):
        inputs = resolve_execution_input_sources(definition, self.sources, owner=owner.owner,
            subject_facts=[], max_media_bytes=65536)
        parameters = initialize_execution_definition_parameters(definition, self.parameter_values, policies=[],
            resolve_source=lambda *_: self.fail("No implicit parameter source"))
        profile = prepare_execution_profile(definition, inputs, profile=self.prompt_profile, unit_key="composition",
            source_language="English", target_language="English", prepared_parameters=parameters, scope_units={})
        return ResolvedExecutionPreparation(json_contract({**profile.value, "inputs": inputs.value, "parameters": parameters}),
            json_contract({"schema_version": 1, "mode": "manual"}))

    def prepare(self):
        self.prep = create_execution_instance_preparation(owner_ref=("asset_job_id", self.job.pk), version_id=self.version.pk,
            expected_definition_sha256=self.definition.sha256, unit_key="composition", request_key="candidate-1",
            intent=json_contract({"inputs": self.sources, "parameters": self.parameter_values}), resolve_facts=self.resolve, actor=self.user)
        return self.prep

    def preview(self):
        self.prep.refresh_from_db()
        return compile_preparation_profile(preparation_id=self.prep.pk, expected_preparation_revision=self.prep.revision, actor=self.user)

    def test_fixed_recipe_choice_reaches_saved_profile_and_original_graph_slots(self):
        selection = self.selection()
        selection["items"] = [{k: v for k, v in row.items() if k != "kind"} for row in selection["items"]]
        self.sources["references"] = bind_recipe_media_selection({"kind": "media_selection", "selection": selection}, self.spec)
        self.prepare()
        result = self.preview()
        text = result.resolved_channels["values"]["positive_prompt"]
        self.assertIn("A red object turns slowly.", text)
        self.assertIn("<Picture 1>", text)
        self.assertIn("<Picture 2>", text)
        self.assertNotIn("asset_library", text)
        self.assertNotIn("overridden", text)
        self.assertEqual(self.prep.field_values.count(), 0)
        self.prep.refresh_from_db()
        transport = compile_prepared_execution_transport(preparation_id=self.prep.pk,
            expected_preparation_revision=self.prep.revision, actor=self.user).transport.value
        self.assertEqual(transport["prompt_channels"]["values"]["positive_prompt"], text)
        media = transport["payload"]["media_preparation"]["bindings"]
        self.assertEqual([row["item_key"] for row in media], ["second", "first"])
        graph = transport["payload"]["compiled_workflow"]["graph"]
        self.assertEqual(graph["cr_images_0"]["inputs"]["image"], media[0]["token"])
        self.assertEqual(graph["cr_images_1"]["inputs"]["image"], media[1]["token"])
        self.assertIsNone(self.prep.request_frozen_at)
        self.assertEqual(self.prep.generation_attempts.count(), 0)

    def test_source_choice_is_frozen_in_profile_reference_context_without_copying_policy(self):
        self.asset.description = "VIDEO_MUST_NOT_AUTO_INJECT_ASSET_DESCRIPTION"
        self.asset.prompt_hint = "VIDEO_MUST_NOT_AUTO_INJECT_IMAGE_PROMPT"
        self.asset.save(update_fields=("description", "prompt_hint"))
        facts = self.resolve(self.definition, self.job).resolved_inputs.value
        references = facts["profile_sources"]["reference_context"]["value"]["context"]["reference_bindings"]
        self.assertEqual([row["item_key"] for row in references], ["second", "first"])
        for row in references:
            self.assertEqual(row["source_selection"], {"source": "asset_library", "overridden": False})
            self.assertNotIn("media_source_policy", row)
            self.assertNotIn("description", row)
            self.assertNotIn("prompt_hint", row)
        self.assertIn(self.asset.prompt_hint, str(facts["inputs"]))
        self.assertNotIn(self.asset.prompt_hint, str(facts["profile_sources"]))
        self.assertEqual(facts["field_requests"], [])

    def test_explicit_upload_override_replaces_unread_base_and_keeps_original_order(self):
        self.asset.source_info = ASSET_UPLOAD_SOURCE_INFO
        self.asset.save(update_fields=["source_info"])
        self.sources["references"] = {"selection": {"source": "asset_library", "items": [{"unused": True}]},
            "override": self.selection("asset_upload")}
        facts = self.resolve(self.definition, self.job).resolved_inputs.value
        for row in facts["profile_sources"]["reference_context"]["value"]["context"]["reference_bindings"]:
            self.assertEqual(row["source_selection"], {"source": "asset_upload", "overridden": True})
        self.prepare()
        before = self.preview().resolved_channels
        self.sources["references"] = {"selection": {"source": "asset_library", "items": []}}
        self.asset.name = "New live label must not replace frozen facts"
        self.asset.save(update_fields=["name"])
        self.assertEqual(self.preview().resolved_channels, before)

    def test_invalid_upload_or_missing_reference_does_not_create_preparation_or_fallback(self):
        for source in ({"selection": self.selection(), "override": self.selection("asset_upload")},
                {"selection": {"source": "asset_library", "items": []}},
                {"selection": {"source": "selected_output", "items": self.selection()["items"]}}):
            with self.subTest(source=source), self.assertRaises(ValidationError):
                self.sources["references"] = source
                self.prepare()
            self.assertFalse(ExecutionPreparation.objects.filter(asset_job=self.job).exists())


class ProfileSelectedMediaProjectionTests(TestCase):
    setUpTestData = classmethod(selected_fixture.ExecutionSelectedOutputTests.setUpTestData.__func__)
    setUp = selected_fixture.ExecutionSelectedOutputTests.setUp
    definition = selected_fixture.ExecutionSelectedOutputTests.definition
    item = selected_fixture.ExecutionSelectedOutputTests.item

    def test_selected_artifact_projects_without_fake_asset_or_current_policy_read(self):
        self.spec["media_source_policy"] = choices(default_source="selected_output")
        self.target["reference_binding"] = {"category": "first_frame", "input_slot": 1}
        self.target["wiring"] = {"kind": "semantic", "semantic_key": "input.first_frame"}
        self.spec["max_items"] = self.target["max_items"] = 1
        definition = json_contract(deepcopy(self.definition().value))
        sources = {"references": {"selection": {"source": "selected_output", "items": [self.item()]}}}
        inputs = resolve_execution_input_sources(definition, sources, owner=self.owner, subject_facts=[],
            max_media_bytes=65536, project_id=self.project.pk, production_run_id=str(self.run.pk), eligible_stage_ids=[self.stage.pk])
        self.spec["media_source_policy"] = choices(default_source="asset_upload")
        with CaptureQueriesContext(connection) as queries:
            actual = project_execution_reference_materials(definition, inputs, prompt_type="image", unit_keys=["frame"], scope_units={}).value
        self.assertFalse(any(name in query["sql"] for query in queries
            for name in ("studio_executioninputspec", "studio_asset", "studio_generatedartifact")))
        row = actual["references"][0]
        self.assertIsNone(row["asset_id"])
        self.assertEqual(row["artifact_id"], str(self.artifact.pk))
        self.assertEqual(row["source_selection"], {"source": "selected_output", "overridden": False})
        self.assertEqual(row["input_label"], "<Picture 1>")
        self.assertEqual(row["unit_keys"], ["frame"])
        self.assertNotIn(str(self.root), str(actual))
        row["source_selection"]["source"] = "changed display copy"
        self.assertEqual(inputs.value["logical_values"]["references"][0]["source_selection"]["source"], "selected_output")
