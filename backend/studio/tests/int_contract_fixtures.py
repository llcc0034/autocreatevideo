"""New INT contracts, authored in an isolated database before any dispatch.

Published model artifacts and Workflow validation flags are explicit dependency
fixtures, NOT live validation/Smoke. Script import, Profile/instance/recipe
publication and Run creation use the original APIs. No old Run is seeded.
"""
from copy import deepcopy
from dataclasses import dataclass
import json
from pathlib import Path
from tempfile import TemporaryDirectory
from unittest.mock import patch

from django.db import connection
from django.test import override_settings
from rest_framework.test import APIClient

from studio.models import (BusinessOutputVersion, DataStructureVersion, ModelDeployment,
    ModelExecutionInstanceVersion, ProductionRecipeVersion, ProductionRun,
    PromptProfileVersion, ScriptRevision, SeriesProject)
from studio.services.business_outputs import write_output
from studio.services.data_structures import data_structure_snapshot
from studio.services.execution_instances import model_family_for_profile
from studio.services.json_contracts import assign_json_contract
from studio.services.production_stage_catalog import UNATTENDED_DECISION_POLICY_REGISTRY
from studio.services.workflow_versions import import_workflow_version, workflow_version_execution_snapshot
from studio.tests.field_definition_fixtures import create_field_definition
from studio.tests.output_structure_fixtures import response_structure
from studio.tests.reference_rendering_fixtures import generation_structure_fixture, rendering_config
from studio.tests.script_semantics_fixtures import script_example, path
from studio.tests.test_dynamic_ref2va_workflow import DynamicRef2VAWorkflowTests
from studio.tests.test_execution_definitions import WorkflowExecutionDefinitionTests
from studio.tests.test_execution_context_inputs import PROJECT_CONFIG
from studio.tests.test_execution_media_source_policy import choices
from studio.tests.test_production_reference_mapping import mapping


@dataclass
class IntegrationSample:
    layout: str
    mode: str
    project: SeriesProject
    output: BusinessOutputVersion
    structure: DataStructureVersion
    revision: ScriptRevision
    raw: dict
    text: str
    recipe: ProductionRecipeVersion
    run: ProductionRun
    create_body: dict


def literal(value):
    return {"kind": "literal", "value": value}


def obj(**fields):
    return {"kind": "object", "fields": fields}


def input_spec(key, kind, target, *, scope="request", maximum=1, subjects=()):
    return {"key": key, "position": 0, "kind": kind, "required": True,
        "min_items": 1, "max_items": maximum, "semantic_contract": {
            "schema_version": 1, "allowed_purposes": [], "allowed_subject_kinds": list(subjects),
            "allowed_scope_kinds": [scope], "paired_with_input_key": None, "allow_repeated_asset": True},
        "targets": [target], **({"media_source_policy": choices()} if kind == "image" else {})}


class IntegrationContracts:
    """Test helper only: no test discovery, runtime imports or business queue."""
    _create_version = DynamicRef2VAWorkflowTests._create_version

    def int_setup(self):
        self.assertEqual(connection.vendor, "sqlite")
        self.assertIn("memory", str(connection.settings_dict["NAME"]))
        temporary = TemporaryDirectory(prefix="autoprompt-int-")
        self.addCleanup(temporary.cleanup)
        self.int_root = Path(temporary.name).resolve()
        self.enterContext(override_settings(MEDIA_ROOT=str(self.int_root / "media"),
            COMFYUI_OUTPUT_DIR=str(self.int_root / "comfy")))
        self.network_guards = [self.enterContext(patch(target,
            side_effect=AssertionError("INT isolated test must not use a live peer")))
            for target in ("socket.socket.connect", "socket.socket.connect_ex", "requests.sessions.Session.request")]
        WorkflowExecutionDefinitionTests.setUp(self)
        self.actor = self.user
        self.actor.is_staff = True
        self.actor.save(update_fields=("is_staff",))
        self.client = APIClient()
        self.client.force_authenticate(self.actor)
        self.api_trace = []
        # Capture all image dependencies before the independent video fixture.
        self.image_model, self.image_deployment = self.profile, self.deployment
        self.primary_workflow = self.workflow
        assign_json_contract(self.image_model, "specification", {**self.image_model.specification,
            "model_family": "int-image", "capabilities": ["image.text_to_image", "image.image_to_image"]})
        self.image_model.save()
        self.variant_workflow = self.int_variant_workflow()
        self.image_parameters = {"image.width": 512, "image.height": 320, "sampling.seed": 37}
        self.image_profile = self.int_profile("int-image-profile", self.image_model, "image.generate", "")
        self.primary = self.int_instance("int-primary", self.image_deployment, self.primary_workflow,
            self.image_profile, "image.text_to_image", "", [])
        self.variant_profile = self.int_profile("int-variant-profile", self.image_model, "image.generate", "", reference=True)
        basis = input_spec("basis", "image", {"target_kind": "workflow_slot", "target_key": "asset.image.basis", "is_primary": True})
        basis["position"] = 1
        image_units = input_spec("units", "json", {"target_kind": "profile_input", "target_key": "reference_context.units", "is_primary": False})
        image_units["position"] = 2
        self.variant = self.int_instance("int-variant", self.image_deployment, self.variant_workflow,
            self.variant_profile, "image.image_to_image", "", [basis, image_units])

        DynamicRef2VAWorkflowTests.setUp(self)
        self.video_model, self.video_workflow = self.profile, self.version
        assign_json_contract(self.video_model, "specification", {**self.video_model.specification,
            "model_family": "int-reference-video"})
        self.video_model.save()
        assign_json_contract(self.provider, "driver_config", {"base_url": "http://127.0.0.1:8188",
            "capability_ttl_seconds": 3600, "timeout_seconds": 30, "poll_interval_seconds": 1, "max_poll_seconds": 60})
        self.provider.save()
        self.video_deployment = ModelDeployment(key="int-reference-video", name="Isolated reference video",
            profile_version=self.video_model)
        assign_json_contract(self.video_deployment, "runtime_config", {})
        self.video_deployment.save()
        self.video_workflow.status, self.video_workflow.validated_deployment = "published", self.video_deployment
        self.video_workflow.save()
        self.video_profile = self.int_profile("int-video-profile", self.video_model, "video.generate", "Ref2VA", reference=True)
        pictures = input_spec("references", "image", {"target_kind": "workflow_slot", "target_key": "images", "is_primary": True},
            scope="shot", maximum=9, subjects=("character", "scene", "prop"))
        units = input_spec("units", "json", {"target_kind": "profile_input", "target_key": "reference_context.units", "is_primary": False}, scope="shot")
        units["position"] = 1
        self.video = self.int_instance("int-reference-video", self.video_deployment, self.video_workflow,
            self.video_profile, "video.reference_to_video", "Ref2VA", [pictures, units])
        self.video_parameters = {"image.width": 640, "image.height": 384, "video.frames": 124,
            "video.fps": 24, "sampling.seed": 37, "sampling.steps": 4, "parameter.lora.strength": 1}

    def api(self, method, url, body=None, *, status=200, **headers):
        response = getattr(self.client, method)(url, body, format="json", **headers)
        self.assertEqual(response.status_code, status, response.data)
        self.api_trace.append((method, url, status))
        return response.json()

    def int_variant_workflow(self):
        snapshot = workflow_version_execution_snapshot(self.primary_workflow)
        graph = deepcopy(snapshot["graph"])
        del graph["4"]
        graph.update({"10": {"class_type": "LoadImage", "inputs": {"image": "never-used-import-example.png"}},
            "11": {"class_type": "ImageScale", "inputs": {"image": ["10", 0], "upscale_method": "nearest-exact",
                "width": 64, "height": 64, "crop": "disabled"}},
            "12": {"class_type": "VAEEncode", "inputs": {"pixels": ["11", 0], "vae": ["1", 2]}}})
        graph["5"]["inputs"]["latent_image"] = ["12", 0]
        bindings = deepcopy(snapshot["semantic_bindings"])
        for binding in bindings:
            if binding["semantic_key"] in {"image.width", "image.height"}:
                binding["node_id"] = "11"
        bindings.append({"semantic_key": "asset.image.basis", "node_id": "10", "input_name": "image", "value_type": "asset_image"})
        contract = deepcopy(snapshot["input_contract"])
        contract.update(capability="image.image_to_image", resolution_strategy={"mode": "explicit_width_height", "node_ids": ["11"]})
        contract["parameters"]["asset.image.basis"] = {"type": "asset_image", "required": True,
            "input_slot": 1, "asset_category": "image", "min_items": 1, "max_items": 1}
        workflow = import_workflow_version(template=self.primary_workflow.template, graph=graph,
            input_contract=contract, semantic_bindings=bindings, model_requirements=snapshot["model_requirements"],
            provider=self.provider, model_profile_version=self.image_model)
        workflow.status, workflow.validated_deployment = "published", self.image_deployment
        workflow.save()
        return workflow

    def int_profile(self, key, model, operation, mode, *, reference=False):
        config = rendering_config() if reference else {}
        if reference:
            config.pop("quality_advisories")
            config["primitive_options"]["reference_context_ir_v1"]["rendering"]["unit_text_bindings"] = {"description": ["direction"]}
            if operation == "image.generate":
                config["primitive_options"]["reference_context_ir_v1"]["rendering"]["reference_rules"].append({
                    "category": "image", "purpose": "", "task": "reference generation",
                    "relationship": "fully_preserved", "usage": "Preserve the supplied visual reference."})
        authored = self.api("post", "/api/v1/prompt-profile-versions/", {"profile_key": key, "name": key,
            "model_family": model_family_for_profile(model), "operation": operation, "mode": mode,
            "source_projection_key": "declarative_v1" if reference else "direct_v1",
            "source_projection_config": config}, status=201)
        profile = PromptProfileVersion.objects.get(pk=authored["id"])
        if reference:
            generation_structure_fixture(profile)
        primitives = (("", "reference_context.subject_definitions") if operation == "image.generate" else
            ("reference_context.subject_definitions", "reference_context.detailed_description")) if reference else ("",)
        for position, primitive in enumerate(primitives):
            field = create_field_definition(key=f"{key}-{position}", label="Independent prompt input", value_type="long_text")
            selector = "projection.output" if primitive else "request.subject"
            self.api("post", "/api/v1/prompt-profile-field-rules/", {"profile_version": str(profile.pk),
                "field_definition": str(field.pk), "field_definition_version": str(field._fixture_version.pk),
                "source_selector": selector, "generation_source_selector": selector, "projection_primitive": primitive,
                "channel": "positive_prompt", "position": position + 1, "required": True,
                "generation_mode": "passthrough", "generation_output_semantics": "final",
                "generation_required": False, "language_policy": "en" if primitive else "preserve_source", "max_chars": 12000,
                "generation_instruction": "", "length_unit": "none", "generation_validator_keys": [],
                "generation_context_selectors": []}, status=201)
        if operation == "image.generate":
            negative = create_field_definition(key=f"{key}-negative", label="Explicit negative", value_type="long_text")
            self.api("post", "/api/v1/prompt-profile-field-rules/", {
                "profile_version": str(profile.pk), "field_definition": str(negative.pk),
                "field_definition_version": str(negative._fixture_version.pk),
                "source_selector": "fixed.value", "generation_source_selector": "fixed.value",
                "fixed_value": "Blurred edges.", "channel": "negative_prompt", "position": len(primitives) + 1,
                "required": True, "generation_mode": "passthrough", "generation_output_semantics": "final",
                "generation_required": False, "language_policy": "en", "max_chars": 1000,
                "generation_instruction": "", "length_unit": "none", "generation_validator_keys": [],
                "generation_context_selectors": []}, status=201)
        url = f"/api/v1/prompt-profile-versions/{profile.pk}/"
        self.api("patch", url, {"review_state": "ready"})
        self.api("post", url + "publish/", {})
        profile.refresh_from_db()
        return profile

    def int_instance(self, key, deployment, workflow, profile, operation, mode, inputs):
        parent = self.api("post", "/api/v1/model-execution-instances/", {"key": key, "name": key}, status=201)
        row = self.api("post", "/api/v1/model-execution-instance-versions/", {"instance_id": parent["id"],
            "expected_revision": parent["revision"], "deployment_id": str(deployment.pk),
            "workflow_version_id": str(workflow.pk), "prompt_profile_version_id": str(profile.pk),
            "operation": operation, "mode": mode, "prompt_mode": "profile", "inputs": inputs, "auxiliaries": []}, status=201)
        url = f"/api/v1/model-execution-instance-versions/{row['id']}/"
        before = self.api("get", url + "input-contract/")
        self.api("post", url + "publish/", {"expected_revision": before["revision"], "expected_definition_sha256": before["sha256"]})
        after = self.api("get", url + "input-contract/")
        self.assertTrue(after["frozen"])
        self.assertEqual(after["definition"], before["definition"])
        return ModelExecutionInstanceVersion.objects.get(pk=row["id"]), after

    @staticmethod
    def route(target, inputs, parameters):
        version, contract = target
        return {"mode": "fixed", "variant_count": 1, "evaluator": "first_succeeded", "routes": [{
            "execution_instance_version_id": str(version.pk), "expected_definition_sha256": contract["sha256"],
            "input_sources": inputs, "parameter_policies": [
                {"key": key, "policy": "locked_value", "value": value} for key, value in parameters.items()]}]}

    def int_script(self, layout, mode):
        raw, _, semantics = script_example(layout)
        entity_node = semantics["node"]["fields"]["entities"]["item"]
        entity_node["fields"]["description"] = path("description")
        entities = raw["entities"] if layout == "a" else raw["world"]["cast"]
        entities[0]["description"] = "A traveler wearing a plain coat."
        for kind, name in (("scene", "旧车站"), ("prop", "行李箱")):
            original = deepcopy(entities[0])
            if layout == "a":
                original.update(key=kind, kind=kind, name=name, identity_anchor=name,
                    primary={"reference_name": name + "·主图", "anchor": name}, variants=[])
            else:
                original.update(id=kind, type=kind, label=name, identity=name,
                    looks={"base": {"reference_name": name + "·主图", "anchor": name}, "alternatives": []})
            original["description"] = "An old train station." if kind == "scene" else "A leather suitcase."
            entities.append(original)
        chapters = raw["episodes"] if layout == "a" else raw["chapters"]
        del chapters[1:]
        for index, shot in enumerate(chapters[0]["shots" if layout == "a" else "frames"]):
            shot["action" if layout == "a" else "motion"] = "The traveler turns." if index == 0 else "The traveler walks to the door."
        raw["创作补充"] = {"count": 0, "enabled": False, "optional": "", "note": "原文 🙂\r\nNo translation"}
        structure = response_structure(example=raw, nullable_fields=("optional",))
        raw["创作补充"]["optional"] = None
        output = write_output(actor=self.actor, data={"name": f"INT {layout} exact semantic output", "parser_key": "script_semantics.v1",
            "data_structure_version": str(structure.pk), "semantic_mapping": semantics})
        output = write_output(actor=self.actor, output_id=output.pk, expected_revision=output.revision, action="publish")
        project = SeriesProject.objects.create(owner=self.actor, title=f"INT {layout} {mode}", **{**PROJECT_CONFIG, "language": "Chinese"})
        text = " \r\n" + json.dumps(raw, ensure_ascii=False, indent=2).replace("\n", "\r\n") + "\r\n "
        imported = self.api("post", "/api/v1/script-sources/", {"project_id": str(project.pk), "content": text}, status=201)
        revision_id = imported["revision"]["id"]
        self.api("post", f"/api/v1/script-revisions/{revision_id}/structure/", {"output_version_id": str(output.pk)})
        self.api("post", f"/api/v1/script-revisions/{revision_id}/confirm/", {})
        return project, output, structure, ScriptRevision.objects.get(pk=revision_id), raw, text

    def int_sample(self, *, layout="a", mode="automatic"):
        self.assertIn(mode, ("automatic", "mixed"))
        project, output, structure, revision, raw, text = self.int_script(layout, mode)
        # Address the exact source node, not a removed fixed Shot.action column.
        snapshot = data_structure_snapshot(structure)
        node = next(row for row in snapshot["nodes"] if row["property_name"] == ("action" if layout == "a" else "motion"))
        direction = {"kind": "shot_field", "scope": {"kind": "shot"}, "field": {
            "structure_version_id": str(structure.pk), "node_key": node["node_key"],
            "field_definition_version_id": node["field_definition"]["field_definition_version_id"]}}
        def shot_source(selector):
            return {"kind": "context_field", "selector_key": selector, "scope": {"kind": "shot"}}
        dialogue_fields = {key: {"kind": "item_field", "path": [key]} for key in (
            "speaker_id", "text", "subject", "voice_profile", "voiceover", "start_ms", "end_ms", "speaker_subject_key")}
        dialogue_fields["delivery"] = {"kind": "item_field", "path": ["delivery"], "if_missing": ""}
        unit = obj(unit_key={"kind": "unit_key"}, duration_ms=shot_source("shot.duration_ms"),
            subject_keys={"kind": "reference_subjects"}, structured_input=obj(direction=direction),
            dialogue={"kind": "each", "source": shot_source("shot.dialogue_with_speakers"), "item": obj(**dialogue_fields)},
            voice_profiles=literal([]), continuity_notes=literal(""),
            requires_first_frame_continuity=shot_source("shot.requires_first_frame_continuity"),
            requires_last_frame_continuity=shot_source("shot.requires_last_frame_continuity"), ambient_sound=literal(""))
        unit_source = {"kind": "structured", "template": obj(schema_version=literal(1),
            ordered_units={"kind": "array", "items": [unit]}, color_script=literal(""), extra_direction=literal(""),
            overall_soundscape=literal(""), non_diegetic_music=literal(""))}
        image_units = {"kind": "structured", "template": obj(schema_version=literal(1),
            ordered_units={"kind": "array", "items": [obj(unit_key={"kind": "unit_key"}, duration_ms=literal(1000),
                subject_keys={"kind": "reference_subjects"}, structured_input=obj(direction=literal("")),
                dialogue=literal([]), voice_profiles=literal([]), continuity_notes=literal(""),
                requires_first_frame_continuity=literal(False), requires_last_frame_continuity=literal(False), ambient_sound=literal(""))]},
            color_script=literal(""), extra_direction=literal(""), overall_soundscape=literal(""), non_diegetic_music=literal(""))}
        materials = {"key": "bible_image_generation", "mode": "existing_pipeline", "approval": "auto",
            "material_sources": {"primary": "generate" if mode == "automatic" else "existing", "variant": "generate"},
            "variant_routing": self.route(self.variant, {"request.subject": {"kind": "material_difference"},
                "basis": {"kind": "material_primary", "item_key": "basis", "purpose": None}, "units": image_units}, self.image_parameters)}
        if mode == "automatic":
            materials["routing"] = self.route(self.primary, {"request.subject": {"kind": "context_field",
                "selector_key": "production.entity.description", "scope": {"kind": "request"}}}, self.image_parameters)
        else:
            materials["material_selection"] = {"primary": "manual"}
        stages = [
            {"key": "script_input", "mode": "confirmed_revision", "approval": "auto"},
            {"key": "bible_materialize", "mode": "deterministic", "approval": "auto"},
            {"key": "bible_review", "mode": "review", "approval": "required"},
            {"key": "storyboard", "mode": "deterministic", "approval": "auto"}, materials,
            {"key": "bible_image_review", "mode": "review", "approval": "required"},
            {"key": "storyboard_review", "mode": "review", "approval": "required", "shot_reference_selection": {
                "source": "upstream", "source_stage_key": "bible_image_generation", "selection": "unique_setting", "allow_override": True}},
            {"key": "media_generation", "mode": "existing_pipeline", "approval": "auto",
                "routing": self.route(self.video, {"references": mapping("images"), "units": unit_source}, self.video_parameters)},
            {"key": "artifact_selection", "mode": "review", "approval": "required"},
            {"key": "assembly", "mode": "existing_pipeline", "approval": "auto"},
            {"key": "delivery", "mode": "existing_pipeline", "approval": "auto"}]
        if mode == "automatic":
            for stage in stages:
                if stage["mode"] == "review":
                    stage.update(approval="policy", decision_policy=UNATTENDED_DECISION_POLICY_REGISTRY[stage["key"]])
        definition = {"schema_version": 3, "interaction_mode": "unattended" if mode == "automatic" else "assisted", "stages": stages}
        draft = self.api("post", "/api/v1/production-recipes/", {"recipe_key": f"int-{layout}-{mode}",
            "name": f"INT {layout} {mode}", "definition": definition}, status=201)
        self.api("post", f"/api/v1/production-recipes/{draft['id']}/publish/", {})
        recipe = ProductionRecipeVersion.objects.get(pk=draft["id"])
        body = {"project_id": str(project.pk), "recipe_version_id": str(recipe.pk), "plan_source": "recipe",
            "script_revision_id": str(revision.pk), "idempotency_key": f"int-{layout}-{mode}",
            "allow_external_calls": True, "unattended_confirmed": mode == "automatic"}
        created = self.api("post", "/api/v1/production-runs/", body, status=201)
        run = ProductionRun.objects.get(pk=created["id"])
        return IntegrationSample(layout, mode, project, output, structure, revision, raw, text, recipe, run, body)
