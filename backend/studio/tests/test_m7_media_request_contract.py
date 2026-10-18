"""M7 request composition, not GPU/remote Smoke or full module acceptance.

The only substituted execution boundary is HTTP. Runtime observations and a
completed historical Smoke are explicitly synthetic, isolated database fixtures;
real admission readers, formal producers, finalization and drivers are retained.
Run only with dotenv disabled, an in-memory SQLite database and python -B.
"""
from copy import deepcopy
from hashlib import sha256
from io import BytesIO
import json
import os
from pathlib import Path
import socket
import sys
from tempfile import TemporaryDirectory
from types import SimpleNamespace
from unittest.mock import patch
from urllib.parse import urlsplit
from uuid import uuid4

from django.core.exceptions import ValidationError
from django.core.files.base import ContentFile
from django.db import connection
from django.test import TestCase, override_settings
from django.test.utils import CaptureQueriesContext
from PIL import Image
import requests
from rest_framework.test import APIClient

from studio.models import (
    ApprovalDecision, Asset, AssetReferenceSeries, Episode, ExecutionPreparation,
    GeneratedArtifact, GenerationAttempt, GenerationTask, ModelDeployment, ModelDeploymentSmokeAttempt,
    ModelProfileVersion, ProductionRecipeVersion, ProductionRun,
    PromptProfileVersion, ProviderProfile, SeriesProject, Shot, ShotReferenceUse,
    StageRun, WorkflowSmokeAttempt,
)
from studio.services.asset_references import attach_asset
from studio.services.async_media_queue import claim_next_async_media_attempt, process_async_media_attempt
from studio.services.configuration import resolve_configuration
from studio.services.comfyui import ComfyUIError
from studio.services.execution_definitions import build_execution_definition
from studio.services.execution_finalization import finalize_execution
from studio.services.execution_instances import (
    clone_execution_draft, create_execution_draft, create_execution_instance,
    execution_resolved_inputs_max_bytes, publish_execution_version, update_execution_draft,
)
from studio.services.execution_preparation_fields import (
    confirm_execution_field, create_manual_execution_field, select_execution_field,
)
from studio.services.execution_preparations import (
    ResolvedExecutionPreparation, create_execution_instance_preparation,
    read_frozen_execution_request,
)
from studio.services.execution_production_admission import collect_production_execution_admission
from studio.services.execution_production_dispatch import dispatch_production_execution
from studio.services.execution_production_workflow_queue import (
    begin_prepared_generation_submission, record_prepared_generation_submission,
)
from studio.services.execution_reference_inputs import REFERENCE_CONTEXT_INPUT_KEY
from studio.services.execution_resource_policy import confirm_resource_review
from studio.services.execution_smoke_admission import collect_smoke_execution_admission
from studio.services.execution_smoke_evidence import finish_prepared_smoke
from studio.services.json_contracts import assign_json_contract, json_contract
from studio.services.production_recipe_media_preparations import prepare_recipe_media_unit
from studio.services.production_reference_mapping import expand_reviewed_reference_sources, reference_use_records
from studio.services.production_run_plans import freeze_run_execution_plan, get_run_execution_plan
from studio.services.production_stage_catalog import production_stage_catalog
from studio.services.production_runs import _formal_snapshot, _save_stage_snapshot
from studio.services.prompt_profiles import publish_prompt_profile, refresh_prompt_profile_contract
from studio.services.security_boundaries import boundary_int
from studio.services.shot_reference_sources import resolve_shot_reference_sources
from studio.services.task_queue import claim_next_task, comfy_client_for_task, prepare_workflow
from studio.tests.execution_runtime_fixtures import record_workflow_runtime
from studio.tests.field_definition_fixtures import create_field_definition, create_profile_field_rule
from studio.tests.recipe_request_fixtures import freeze_recipe_request_fixture
from studio.tests.reference_rendering_fixtures import generation_structure_fixture, rendering_config
from studio.tests.test_async_media_p0 import _async_spec, _provider_config
from studio.tests import test_dynamic_ref2va_workflow as workflow_fixtures
from studio.tests import test_execution_auxiliary_definitions as input_fixtures
from studio.tests.test_execution_context_inputs import PROJECT_CONFIG
from studio.tests.test_execution_text_contracts import text_specification
from studio.tests.test_production_reference_mapping import mapping


@override_settings(AI_PROVIDER_ALLOWED_HOSTS=("127.0.0.1", "api.example.com", "cdn.example.com"))
class M7MediaRequestContractTests(TestCase):
    _create_version = workflow_fixtures.DynamicRef2VAWorkflowTests._create_version
    requires_bible = False
    selected_text = "SELECTED_A: the parcel turns clockwise."
    unselected_text = "UNSELECTED_B: this must never be transmitted."

    def setUp(self):
        self.assertTrue(sys.dont_write_bytecode, "Use python -B")
        self.assertEqual(os.environ.get("PYTHON_DOTENV_DISABLED"), "1")
        self.assertEqual(os.environ.get("DATABASE_URL"), "sqlite:///:memory:")
        self.assertEqual(connection.vendor, "sqlite")
        self.assertIn("memory", connection.settings_dict["NAME"])
        self.root = Path(self.enterContext(TemporaryDirectory(prefix="m7-media-contract-")))
        self.enterContext(override_settings(MEDIA_ROOT=str(self.root), ASSET_LIBRARY_ROOT=self.root / "library"))
        self.calls = []
        self.missing_rest_id = False
        self.enterContext(patch("requests.sessions.Session.request", side_effect=self.http))
        for target in ("socket.socket.connect", "socket.socket.connect_ex", "socket.create_connection",
                       "studio.services.provider_registry.resolve_credential"):
            guard = self.enterContext(patch(target, side_effect=AssertionError("Forbidden network/credential access")))
            self.addCleanup(guard.assert_not_called)
        # Even address resolution is limited to this suite's loopback fixture.
        self.enterContext(patch("socket.getaddrinfo", side_effect=self.addresses))

    def addresses(self, host, port, *args, **kwargs):
        self.assertEqual(host, "127.0.0.1")
        return [(socket.AF_INET, socket.SOCK_STREAM, socket.IPPROTO_TCP, "", (host, port))]

    def http(self, method, url, **kwargs):
        parsed = urlsplit(url)
        self.assertEqual(parsed.hostname, "127.0.0.1")
        self.assertEqual(method.upper(), "POST")
        row = {"path": parsed.path, "json": deepcopy(kwargs.get("json")), "headers": deepcopy(kwargs.get("headers", {}))}
        if parsed.path == "/upload/image":
            name, stream, _mime = kwargs["files"]["image"]
            row.update(data=deepcopy(kwargs["data"]), name=name, bytes=stream.read())
            body = {"name": name, "subfolder": row["data"]["subfolder"], "type": "input"}
        elif parsed.path == "/prompt":
            body = {"prompt_id": row["json"]["prompt_id"], "number": 1, "node_errors": {}}
        elif parsed.path == "/jobs":
            body = {} if self.missing_rest_id else {"id": "m7-isolated-response"}
        else:
            self.fail(f"Unexpected remote route: {parsed.path}")
        self.calls.append(row)
        response = requests.Response()
        response.status_code, response.url, response.encoding = 200, url, "utf-8"
        response._content = json.dumps(body).encode()
        response._content_consumed = True
        response.headers.update({"Content-Type": "application/json", "Content-Length": str(len(response.content))})
        response.raw = SimpleNamespace(_connection=SimpleNamespace(sock=SimpleNamespace(
            getpeername=lambda: ("127.0.0.1", parsed.port or 80))), close=lambda: None)
        return response

    @staticmethod
    def write(obj, field, value):
        assign_json_contract(obj, field, value)
        obj.save()
        return obj

    def published(self, version):
        definition = build_execution_definition(version.pk)
        return publish_execution_version(version_id=version.pk, expected_revision=version.revision,
            expected_definition_sha256=definition.sha256, actor=self.actor)

    def leaf(self):
        provider = self.write(ProviderProfile(key="m7-field", name="Isolated field", driver_type="openai_compatible", modalities="text"),
            "driver_config", {"base_url": "http://127.0.0.1:8199", "chat_path": "/v1/chat/completions",
                "timeout_seconds": 40, "capability_ttl_seconds": 3600})
        model = self.write(ModelProfileVersion(profile_key="m7-field", version=1, provider=provider,
            model_identifier="fixture-only", modality="text", status="published"), "specification", text_specification())
        deployment = self.write(ModelDeployment(key="m7-field", name="Isolated field", profile_version=model), "runtime_config", {})
        instance = create_execution_instance(key="m7-field", name="Isolated field", description="", actor=self.actor)
        version = create_execution_draft(instance_id=instance.pk, expected_revision=instance.revision, actor=self.actor,
            selection={"deployment": deployment, "operation": "prompt.fields.generate", "mode": "", "workflow_version": None,
                "prompt_mode": "none", "prompt_profile_version": None})
        version = update_execution_draft(version_id=version.pk, expected_revision=version.revision, actor=self.actor,
            changes={"inputs": [input_fixtures.ExecutionAuxiliaryDefinitionTests.input("field_request", "field_request", kind="json", target_kind="operation_input")]})
        return self.published(version)

    def setup_case(self, driver, entry):
        self.driver, self.entry = driver, entry
        if driver == "comfy":
            workflow_fixtures.DynamicRef2VAWorkflowTests.setUp(self)  # Only rows and graph import; no patched driver or real models.
            self.actor = self.user
            self.actor.is_staff = True
            self.actor.save(update_fields=("is_staff",))
            self.workflow = self.version
            self.model = self.profile
            self.write(self.model, "specification", {**self.model.specification, "model_family": "configured-media-family"})
            self.write(self.provider, "driver_config", {"base_url": "http://127.0.0.1:8188", "timeout_seconds": 30,
                "poll_interval_seconds": 1, "max_poll_seconds": 60, "capability_ttl_seconds": 3600})
        else:
            from django.contrib.auth import get_user_model
            self.actor = get_user_model().objects.create_user(username="m7-owner", is_staff=True)
            config = _provider_config()
            config.update(base_url="http://127.0.0.1:8190", request_field_map={"model": "engine", "prompt": "instruction",
                "width": "w", "height": "h", "fps": "rate", "duration_seconds": "seconds"})
            self.provider = self.write(ProviderProfile(key="m7-rest", name="Isolated REST", driver_type="async_rest", modalities="video"),
                "driver_config", config)
            self.model = self.write(ModelProfileVersion(profile_key="m7-rest", version=1, provider=self.provider,
                model_identifier="cloud-v1", modality="video", status="published"), "specification", _async_spec())
            self.workflow = None
        self.deployment = self.write(ModelDeployment(key="m7-media", name="Isolated media", profile_version=self.model), "runtime_config", {})
        if self.workflow:
            self.workflow.status, self.workflow.validated_deployment = "published", self.deployment
            self.workflow.save()
            self.workflow.template.is_active = True
            self.workflow.template.save()
            record_workflow_runtime(self.deployment)
        profile = PromptProfileVersion.objects.create(profile_key="m7-profile", version=1, name="M7 exact profile",
            model_family="configured-media-family" if self.workflow else "async_cloud", operation="video.generate",
            mode="Ref2VA" if self.workflow else "T2VA", review_state="ready", contract_schema_version=8)
        if self.workflow:
            config = rendering_config()
            config.pop("quality_advisories")
            rendering = config["primitive_options"]["reference_context_ir_v1"]["rendering"]
            rendering["unit_text_bindings"] = {"description": ["direction"]}
            rendering["reference_rules"].append({"category": "image", "purpose": "", "task": "reference generation",
                "relationship": "fully_preserved", "usage": "Use the selected picture."})
            profile.source_projection_key, profile.source_projection_config = "declarative_v1", config
            profile.save()
            generation_structure_fixture(profile)
            names = create_field_definition(key="m7_names", label="Names", value_type="long_text")
            create_profile_field_rule(profile_version=profile, field_definition=names, source_selector="projection.output",
                projection_primitive="reference_context.subject_definitions", generation_mode="passthrough",
                generation_source_selector="projection.output", generation_output_semantics="final", language_policy="preserve_source",
                channel="positive_prompt", position=1, required=True, max_chars=7000)
        field = create_field_definition(key="custom_motion", label="Motion", value_type="long_text")
        create_profile_field_rule(profile_version=profile, field_definition=field,
            source_selector="projection.output" if self.workflow else "request.text",
            generation_source_selector="request.structured_input" if self.workflow else "request.action",
            **({"projection_primitive": "reference_context.detailed_description", "source_path": ["direction"]} if self.workflow else {}),
            generation_mode="model_generated", generation_output_semantics="contribution" if self.workflow else "final",
            generation_instruction="Describe the motion.", generation_required=True,
            language_policy="inherit" if self.workflow else "en", channel="positive_prompt", position=2,
            label_style="none", required=True, max_chars=7000)
        refresh_prompt_profile_contract(profile)
        self.prompt_profile = publish_prompt_profile(profile, actor=self.actor)
        leaf = self.leaf()
        instance = create_execution_instance(key="m7-media", name="M7 media", description="", actor=self.actor)
        version = create_execution_draft(instance_id=instance.pk, expected_revision=instance.revision, actor=self.actor,
            selection={"deployment": self.deployment, "operation": "video.reference_to_video" if self.workflow else "video.text_to_video",
                "mode": "Ref2VA" if self.workflow else "T2VA", "workflow_version": self.workflow,
                "prompt_mode": "profile", "prompt_profile_version": self.prompt_profile})
        inputs = []
        if self.workflow:
            inputs = [{"key": "references", "position": 0, "kind": "image", "required": True, "min_items": 1, "max_items": 2,
                "semantic_contract": {"schema_version": 1, "allowed_purposes": [], "allowed_subject_kinds": [],
                    "allowed_scope_kinds": ["request", "shot"], "paired_with_input_key": None, "allow_repeated_asset": False},
                "targets": [{"target_kind": "workflow_slot", "target_key": "images", "is_primary": True}]},
                input_fixtures.ExecutionAuxiliaryDefinitionTests.input("units", REFERENCE_CONTEXT_INPUT_KEY, kind="json", position=1)]
        version = update_execution_draft(version_id=version.pk, expected_revision=version.revision, actor=self.actor,
            changes={"inputs": inputs, "auxiliaries": [{"purpose": "generate_fields", "target_version_id": str(leaf.pk)}]})
        self.version = self.published(version)
        self.definition = build_execution_definition(self.version.pk)
        self.project = SeriesProject.objects.create(owner=self.actor, title="Isolated M7", **PROJECT_CONFIG)
        episode = Episode.objects.create(project=self.project, number=1, title="M7")
        self.shot = Shot.objects.create(episode=episode, number=1, title="M7", media_type="video",
            duration_ms=5000 if self.workflow else 3000, include_audio=bool(self.workflow))
        self.parameters = ({"image.width": 320, "image.height": 512, "video.frames": 124, "video.fps": 24,
            "sampling.seed": 37, "sampling.steps": 4, "parameter.lora.strength": 1} if self.workflow
            else {"width": 768, "height": 1344, "fps": 24, "duration_seconds": 3})
        self.sources = {}
        if self.workflow:
            self.reference_fixture()
            self.sources["units"] = {"kind": "literal", "value": {"schema_version": 1,
                "ordered_units": [{"unit_key": f"shot:{self.shot.pk}", "duration_ms": 5000, "subject_keys": [],
                    "dialogue": [], "voice_profiles": [], "continuity_notes": "", "requires_first_frame_continuity": False,
                    "requires_last_frame_continuity": False, "ambient_sound": "", "structured_input": {"direction": "A parcel turns."}}],
                "color_script": "", "extra_direction": "", "overall_soundscape": "", "non_diegetic_music": ""}}
        if not self.workflow:
            self.sources["request.action"] = {"kind": "literal", "value": "Isolated motion source"}
        self.root_fixture()
        self.client = APIClient()
        self.client.force_authenticate(self.actor)

    def reference_fixture(self):
        versions = []
        self.assets = []
        for index, color in enumerate(("red", "blue", "green")):
            stream = BytesIO()
            Image.new("RGB", (12, 18), color).save(stream, format="PNG")
            asset = Asset(owner=self.actor, kind="reference", name="Same parcel", mime_type="image/png",
                checksum=sha256(stream.getvalue()).hexdigest())
            asset.file.save(f"m7-{index}.png", ContentFile(stream.getvalue()))
            self.assets.append(asset)
        for asset in self.assets[:2]:
            series = AssetReferenceSeries.objects.create(owner=self.actor, name="Same parcel", role="primary")
            versions.append(attach_asset(actor=self.actor, series_id=series.pk, asset_id=asset.pk, source="existing")[0])
        self.newer = attach_asset(actor=self.actor, series_id=versions[0].series_id, asset_id=self.assets[2].pk, source="existing")[0]
        for index, version in enumerate(versions):
            ShotReferenceUse.objects.create(shot=self.shot, use_key=f"use-{index}", position=1-index,
                reference_version=version, media_kind="image", purpose="", source_mode="existing", asset_sha256=version.asset.checksum)
        self.reference_ids = [str(versions[1].pk), str(versions[0].pk)]
        uses = resolve_shot_reference_sources(actor=self.actor, project_id=self.project.pk, shot_ids=[self.shot.pk]).value["shots"][0]["uses"]
        specs = self.definition.value["configuration"]["inputs"]
        self.sources, excluded = expand_reviewed_reference_sources(specs=specs, sources={"references": mapping("images")},
            records=reference_use_records(uses), shot_id=str(self.shot.pk))
        self.assertEqual(excluded, [])

    def root_fixture(self):
        recipe = None
        if self.entry == "recipe":
            route = {"execution_instance_version_id": str(self.version.pk), "expected_definition_sha256": self.definition.sha256,
                "input_sources": {**self.sources, **({"references": mapping("images")} if self.workflow else {})},
                "parameter_policies": [{"key": key, "policy": "locked_value", "value": value} for key, value in self.parameters.items()]}
            recipe = ProductionRecipeVersion(recipe_key=str(uuid4()), version=1, name="Isolated M7 recipe", status="published")
            assign_json_contract(recipe, "definition", {"schema_version": 3, "interaction_mode": "assisted", "stages":
                ([{"key": "storyboard_review", "mode": "review", "approval": "required"}] if self.workflow else []) +
                [{"key": "media_generation", "mode": "existing_pipeline", "approval": "required",
                    "routing": {"mode": "fixed", "variant_count": 1, "routes": [route], "evaluator": None}}]}, schema_version=3)
            recipe.save()
        self.run = ProductionRun.objects.create(owner=self.actor, project=self.project, recipe_version=recipe, plan_source=self.entry,
            status="running", allow_external_calls=True, idempotency_key=str(uuid4()), request_sha256="a"*64)
        assign_json_contract(self.run, "config_snapshot", resolve_configuration(run=self.run).snapshot(),
            max_bytes=boundary_int("security.snapshot.stage_max_bytes"))
        assign_json_contract(self.run, "input_snapshot", {"schema_version": 1, "scope": "isolated M7 fixture"})
        self.run.save()
        catalog = production_stage_catalog()
        freeze_run_execution_plan(run=self.run, scope={"kind": "project"}, delivery={"kind": "episode_masters"}, initial_content=None,
            stage_catalog_key=catalog.value["key"], expected_stage_catalog_sha256=catalog.sha256, actor=self.actor)
        self.plan = get_run_execution_plan(self.run)
        if recipe:
            freeze_recipe_request_fixture(self.run, authorized=True)
        ordinal = (2 if self.workflow else 1) if recipe else next(row["ordinal"] for row in catalog.value["stages"] if row["key"] == "media_generation")
        self.stage = StageRun.objects.create(production_run=self.run, stage_key="media_generation", ordinal=ordinal,
            mode="existing_pipeline" if recipe else "manual", status="running", idempotency_key=str(uuid4()))
        if recipe and self.workflow:
            review = StageRun.objects.create(production_run=self.run, stage_key="storyboard_review", ordinal=1,
                mode="review", status="succeeded", idempotency_key=str(uuid4()))
            snapshot = _formal_snapshot(self.run, review, f"storyboard:{self.shot.episode_id}")
            digest = _save_stage_snapshot(review, "output_snapshot", snapshot)
            ApprovalDecision.objects.create(stage_run=review, revision=1, decision="approve", actor="human",
                decided_by=self.actor, output_snapshot_sha256=digest)

    def prepare(self):
        if self.entry == "recipe":
            self.prep = prepare_recipe_media_unit(stage_run_id=self.stage.pk, expected_run_plan_sha256=self.plan.sha256,
                scope={"kind": "shot", "id": str(self.shot.pk)}, candidate_number=1, actor=self.actor)
        else:
            response = self.client.post(f"/api/v1/production-runs/{self.run.pk}/manual-actions/", {
                "action": "generate", "stage_key": "media_generation", "unit_key": f"shot:{self.shot.pk}",
                "scope": {"kind": "shot", "id": str(self.shot.pk)}, "expected_run_plan_sha256": self.plan.sha256,
                "expected_selection_revision": None, "idempotency_key": "m7-manual", "payload": {
                    "execution_instance_version_id": str(self.version.pk), "expected_definition_sha256": self.definition.sha256,
                    "inputs": self.sources, "parameters": self.parameters, "manual_channels": {}, "subject_facts": [],
                    "source_language": "English", "target_language": "English"}}, format="json")
            self.assertEqual(response.status_code, 202, response.data)
            self.prep = ExecutionPreparation.objects.get(pk=response.data["target_id"])
        self.chosen = create_manual_execution_field(preparation_id=self.prep.pk, unit_key=self.prep.unit_key,
            field_key="custom_motion", text=self.selected_text, revision=1, expected_preparation_revision=self.prep.revision, actor=self.actor)
        self.prep.refresh_from_db()
        confirm_execution_field(preparation_id=self.prep.pk, field_value_id=self.chosen.pk,
            expected_preparation_revision=self.prep.revision, expected_text_sha256=self.chosen.text_sha256,
            expected_source_sha256=self.chosen.source_sha256, select_for_execution=True, expected_selection_revision=None,
            replace=False, actor=self.actor)
        self.prep.refresh_from_db()
        self.pending = create_manual_execution_field(preparation_id=self.prep.pk, unit_key=self.prep.unit_key,
            field_key="custom_motion", text=self.unselected_text, revision=2, expected_preparation_revision=self.prep.revision, actor=self.actor)
        self.prep.refresh_from_db()
        return self.prep

    def finalize(self, prep, collector):
        result = finalize_execution(preparation_id=prep.pk, expected_preparation_revision=prep.revision,
            actor=self.actor, collect_admission_facts=collector)
        if result.status == "awaiting_review":
            result = confirm_resource_review(preparation_id=result.pk, expected_preparation_revision=result.revision,
                fingerprint=result.contract.evaluation["root_checks"]["resource_review"]["fingerprint"], actor=self.actor)
            result = finalize_execution(preparation_id=result.pk, expected_preparation_revision=result.revision,
                actor=self.actor, collect_admission_facts=collector)
        self.assertEqual(result.status, "ready", result.contract.evaluation)
        return result

    def seed_historical_admission_fixture(self):
        """Synthetic completed history; never claim this as a model Smoke run."""
        values = dict(deployment=self.deployment, scope="instance", execution_instance_version=self.version,
            status="preparing", target_sha256=self.definition.value["target_fingerprint"], idempotency_key=str(uuid4()), allow_external_calls=True)
        if self.workflow:
            attempt = WorkflowSmokeAttempt(**values, workflow_version=self.workflow, created_by=self.actor,
                graph_sha256=self.workflow.graph_sha256, execution_contract_sha256=self.workflow.execution_contract_sha256,
                validation_evidence_sha256=self.workflow.validation_evidence_sha256)
            assign_json_contract(attempt, "fixture_values", {"schema_version": 1, "scope": "isolated M7 synthetic admission"})
            attempt.fixture_sha256 = attempt.fixture_values_sha256
            owner = "workflow_smoke_id"
        else:
            attempt = ModelDeploymentSmokeAttempt(**values, operation=self.version.operation, request_sha256="")
            owner = "deployment_smoke_id"
        attempt.save()
        facts = deepcopy(self.prep.contract.resolved_inputs)
        smoke = create_execution_instance_preparation(owner_ref=(owner, attempt.pk), version_id=self.version.pk,
            expected_definition_sha256=self.definition.sha256, unit_key=self.prep.unit_key, request_key="m7-synthetic-history",
            intent=json_contract({"test_scope": "isolated historical admission fixture",
                "parameters": {key: value for key, value in facts["parameters"]["values"].items()
                    if facts["parameters"]["sources"][key] == "request"}, "manual_channels": {}}), actor=self.actor,
            resolve_facts=lambda *_: ResolvedExecutionPreparation(json_contract(facts, max_bytes=execution_resolved_inputs_max_bytes()),
                json_contract({"schema_version": 1, "mode": "manual"})))
        select_execution_field(preparation_id=smoke.pk, source_preparation_id=self.prep.pk, field_value_id=self.chosen.pk,
            expected_preparation_revision=smoke.revision, expected_selection_revision=None, replace=False, actor=self.actor)
        smoke.refresh_from_db()
        smoke = self.finalize(smoke, collect_smoke_execution_admission)
        if not self.workflow:
            attempt.request_sha256 = smoke.request_sha256
        attempt.status, attempt.claim_token = "submitting", uuid4()
        attempt.save()
        finish_prepared_smoke(attempt, attempt.claim_token, status="succeeded",
            evidence=json_contract({"test_scope": "SYNTHETIC historical admission; no remote Smoke or generated output"}))

    def display(self):
        self.prep.refresh_from_db()
        with CaptureQueriesContext(connection) as queries:
            response = self.client.get(f"/api/v1/execution-preparations/{self.prep.pk}/prompt-display/",
                {"expected_preparation_revision": self.prep.revision})
        self.assertEqual(response.status_code, 200, response.data)
        self.assertFalse(any(q["sql"].lstrip().upper().startswith(("INSERT", "UPDATE", "DELETE")) for q in queries))
        return response.data

    def freeze(self):
        self.prepare()
        before = self.display()
        self.seed_historical_admission_fixture()
        self.prep.refresh_from_db()
        self.prep = self.finalize(self.prep, collect_production_execution_admission)
        self.detail, _definition, self.transport = read_frozen_execution_request(self.prep)
        self.request_path = self.root / self.detail.prepared_request_key
        self.assertTrue(self.request_path.resolve().is_relative_to(self.root.resolve()))
        self.original_bytes = self.request_path.read_bytes()
        self.assertEqual(sha256(self.original_bytes).hexdigest(), self.prep.request_sha256)
        self.assertEqual(len(self.original_bytes), self.detail.prepared_request_size_bytes)
        after = self.display()
        self.frozen_display = after
        self.assertEqual(before["mode"], "preview")
        self.assertEqual(after["mode"], "frozen")
        for key in ("channels", "field_basis", "profile_basis", "length_diagnostics"):
            self.assertEqual(after[key], before[key], key)
        document = json.loads(self.original_bytes)
        selected = document["field_selections"]["fields"]
        self.assertEqual([row["field_value_id"] for row in selected], [str(self.chosen.pk)])
        self.assertEqual(selected[0]["text_sha256"], self.chosen.text_sha256)
        self.assertEqual(selected[0]["field_contract_sha256"], self.chosen.field_contract_sha256)
        self.assertEqual(selected[0]["source_preparation_id"], str(self.prep.pk))
        self.assertEqual(selected[0]["field_revision"], 1)
        self.assertEqual(after["profile_basis"], {"version_id": str(self.prompt_profile.pk), "version": 1,
            "contract_sha256": self.prompt_profile.contract_sha256})
        self.pending.refresh_from_db()
        self.assertEqual(self.pending.status, "pending")
        self.assertNotIn(str(self.pending.pk), self.original_bytes.decode())
        self.assertNotIn(self.unselected_text, self.original_bytes.decode())
        self.assertIn(self.selected_text, self.transport.value["prompt_channels"]["values"]["positive_prompt"])
        for key, value in self.parameters.items():
            self.assertEqual(self.transport.value["parameters"]["values"][key], value)
        self.assertEqual(self.calls, [])

    def change_configuration(self):
        successor = clone_execution_draft(source_id=self.version.pk, expected_revision=self.version.revision,
            expected_definition_sha256=self.definition.sha256, actor=self.actor)
        parameters = deepcopy(self.definition.value["configuration"]["parameters"])
        width = "image.width" if self.workflow else "width"
        for row in parameters:
            if row["key"] == width:
                row["policy"], row["locked_value"], row["default_value"] = "locked", 640, None
        if not any(row["key"] == width for row in parameters):
            parameters.append({"key": width, "value_type": "integer", "minimum": None, "maximum": None,
                "enum_values": [], "step": None, "remainder": None, "policy": "locked",
                "default_value": None, "locked_value": 640, "seed_policy": None})
        successor = update_execution_draft(version_id=successor.pk, expected_revision=successor.revision,
            actor=self.actor, changes={"parameters": parameters})
        successor = self.published(successor)
        self.assertNotEqual(successor.pk, self.version.pk)
        self.assertNotEqual(build_execution_definition(successor.pk).sha256, self.definition.sha256)
        self.project.title = "Changed after freeze"
        self.project.save(update_fields=("title",))
        if self.workflow:
            self.assets[0].name = "Changed live name"
            self.assets[0].save(update_fields=("name",))
        self.assertEqual(self.request_path.read_bytes(), self.original_bytes)
        replay = self.finalize(self.prep, collect_production_execution_admission)
        self.assertEqual(replay.request_sha256, sha256(self.original_bytes).hexdigest())
        self.assertEqual(self.display(), self.frozen_display)

    def dispatch(self):
        self.prep.refresh_from_db()
        self.attempt = dispatch_production_execution(preparation_id=self.prep.pk,
            expected_preparation_revision=self.prep.revision, actor=self.actor)
        self.assertEqual(self.attempt.request_snapshot_sha256, self.prep.request_sha256)
        self.assertEqual(self.attempt.preparation_id, self.prep.pk)

    def stage_comfy(self):
        self.task = claim_next_task()
        self.assertEqual(self.task.pk, self.attempt.generation_task_id)
        self.remote = comfy_client_for_task(self.task)
        self.graph = prepare_workflow(self.task, self.remote)
        return self.graph

    def assert_transport(self):
        self.dispatch()
        if self.workflow:
            self.stage_comfy()
            bindings = self.transport.value["payload"]["media_preparation"]["bindings"]
            self.assertEqual([row["asset_id"] for row in bindings], [str(self.assets[1].pk), str(self.assets[0].pk)])
            original_graph = deepcopy(self.transport.value["payload"]["compiled_workflow"]["graph"])
            uploads = [row for row in self.calls if row["path"] == "/upload/image"]
            self.assertEqual(len(uploads), 2)
            for index, upload in enumerate(uploads):
                self.assertEqual(original_graph[f"cr_images_{index}"]["inputs"]["image"], bindings[index]["token"])
                original_graph[f"cr_images_{index}"]["inputs"]["image"] = upload["data"]["subfolder"] + "/" + upload["name"]
                self.assertTrue(upload["bytes"].startswith(b"\x89PNG"))
                self.assertEqual(sha256(upload["bytes"]).hexdigest(), self.assets[1-index].checksum)
            self.assertEqual(self.graph, original_graph)
            self.task.refresh_from_db()
            for receipt, upload in zip(self.task.staged_inputs, uploads):
                self.assertEqual(receipt["submitted_sha256"], sha256(upload["bytes"]).hexdigest())
            self.assertTrue(begin_prepared_generation_submission(self.task))
            receipt = self.remote.queue_prompt(self.graph, self.task.client_id, prompt_id=self.task.submission_token)
            self.assertEqual(receipt.prompt_id, str(self.attempt.pk))
            record_prepared_generation_submission(self.task)
            self.assertEqual(self.calls[-1]["json"]["prompt"], original_graph)
            self.assertEqual(self.calls[-1]["json"]["prompt_id"], str(self.attempt.pk))
            with CaptureQueriesContext(connection) as queries:
                response = self.client.get(f"/api/v1/tasks/{self.task.pk}/")
                graph_response = self.client.get(f"/api/v1/tasks/{self.task.pk}/comfyui-workflow/")
            self.assertEqual(response.status_code, 200, response.data)
            self.assertEqual(graph_response.status_code, 200, graph_response.data)
            self.assertEqual(graph_response.data["workflow"], original_graph)
            self.assertEqual(graph_response.data["request_sha256"], self.prep.request_sha256)
            self.assertFalse(any(q["sql"].lstrip().upper().startswith(("INSERT", "UPDATE", "DELETE")) for q in queries))
            media = response.data["input_parameters"]["media_uses"]
            references = [row["reference_binding"] for row in media]
            self.assertEqual([row["shot_reference"]["source"]["reference_version_id"] for row in references], self.reference_ids)
            self.assertEqual([row["input_slot"] for row in references], [1, 2])
            self.assertEqual([row["original_name"] for row in references], ["Same parcel"] * 2)
            self.assertTrue(all(row["name_disambiguated"] for row in references))
            self.assertEqual(len({row["reference_name"] for row in references}), 2)
        else:
            claim = claim_next_async_media_attempt("m7-isolated")
            self.assertEqual(claim.pk, self.attempt.pk)
            process_async_media_attempt(claim)
            self.attempt.refresh_from_db()
            self.assertEqual(self.attempt.status, "submitted", self.attempt.error_message)
            payload = self.transport.value["payload"]
            expected = {self.provider.driver_config["request_field_map"][key]: value for key, value in payload.items()
                if key not in {"schema_version", "idempotency_key"}}
            self.assertEqual(self.calls, [{"path": "/jobs", "json": expected,
                "headers": {"Content-Type": "application/json", "Idempotency-Key": f"production-attempt:{self.attempt.pk}"}}])
            self.assertEqual(set(expected), {"engine", "instruction", "w", "h", "rate", "seconds"})
        self.assertEqual(self.request_path.read_bytes(), self.original_bytes)
        self.assertEqual(GenerationAttempt.objects.filter(preparation=self.prep).count(), 1)
        self.assertFalse(GeneratedArtifact.objects.exists())

    def positive(self, driver, entry):
        self.setup_case(driver, entry)
        self.freeze()
        self.change_configuration()
        self.assert_transport()

    def test_manual_comfy_frozen_fields_versions_order_and_transport(self):
        self.positive("comfy", "manual")

    def test_recipe_comfy_frozen_fields_versions_order_and_transport(self):
        self.positive("comfy", "recipe")

    def test_manual_rest_frozen_fields_mapping_and_transport(self):
        self.positive("rest", "manual")

    def test_recipe_rest_frozen_fields_mapping_and_transport(self):
        self.positive("rest", "recipe")

    def test_corrupt_manifest_refuses_before_network_without_rebuilding(self):
        self.setup_case("rest", "manual")
        self.freeze()
        self.dispatch()
        self.request_path.write_bytes(self.original_bytes + b" ")
        with self.assertRaises(ValidationError):
            read_frozen_execution_request(self.prep)
        claim = claim_next_async_media_attempt("m7-corrupt")
        if claim is not None:
            process_async_media_attempt(claim)
        self.attempt.refresh_from_db()
        self.assertEqual(self.attempt.status, "failed")
        self.assertEqual(self.calls, [])
        self.assertEqual(self.request_path.read_bytes(), self.original_bytes + b" ")

    def test_rest_missing_response_identity_is_uncertain_and_never_reposted(self):
        self.setup_case("rest", "recipe")
        self.freeze()
        self.dispatch()
        self.missing_rest_id = True
        process_async_media_attempt(claim_next_async_media_attempt("m7-missing-id"))
        self.attempt.refresh_from_db()
        self.assertEqual(self.attempt.status, "failed")
        self.assertEqual(self.attempt.error_code, "uncertain_remote_response")
        self.assertEqual(self.attempt.provider_request_id, "")
        self.assertGreater(self.attempt.committed_cost, 0)
        self.assertIsNone(claim_next_async_media_attempt("m7-no-repost"))
        self.assertEqual([row["path"] for row in self.calls], ["/jobs"])
        self.assertEqual(self.request_path.read_bytes(), self.original_bytes)

    def test_changed_reference_bytes_refuse_upload_and_prompt(self):
        self.setup_case("comfy", "recipe")
        self.freeze()
        self.dispatch()
        Path(self.assets[1].file.path).write_bytes(b"not the frozen PNG")
        with self.assertRaisesMessage(ComfyUIError, "参考素材内容哈希与已确认记录不一致") as error:
            self.stage_comfy()
        self.assertIsInstance(error.exception.__cause__, ValidationError)
        self.assertEqual(self.calls, [])
        self.assertEqual(self.request_path.read_bytes(), self.original_bytes)

    def test_wrong_or_duplicate_upload_receipts_refuse_reuse_before_prompt(self):
        self.setup_case("comfy", "manual")
        self.freeze()
        self.dispatch()
        self.stage_comfy()
        self.task.refresh_from_db()
        original = deepcopy(self.task.staged_inputs)
        wrong = deepcopy(original)
        wrong[0]["root_id"] = str(uuid4())
        for name, invalid, message in (("wrong_root", wrong, "暂存回执的来源、目标或任务已漂移"),
                ("duplicate", [*original, original[0]], "暂存回执包含重复或其他请求的素材使用")):
            with self.subTest(case=name):
                GenerationTask.objects.filter(pk=self.task.pk).update(staged_inputs=invalid)
                self.task.refresh_from_db()
                with self.assertRaisesMessage(ComfyUIError, message) as error:
                    prepare_workflow(self.task, self.remote)
                self.assertIsInstance(error.exception.__cause__, ValidationError)
        self.assertEqual([row["path"] for row in self.calls], ["/upload/image", "/upload/image"])
        self.assertEqual(self.request_path.read_bytes(), self.original_bytes)
