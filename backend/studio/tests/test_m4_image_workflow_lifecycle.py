"""M4-08: exact Z v4 / Qwen v2 contracts, isolated lifecycle evidence only.

See fixtures/m4_image_workflows.json::_comment for the read-only export source.
No operational acceptance results are imported. Workflow, validation, resource
Smoke, combination Smoke and production preparation run through real services.
Only the external driver/upload peer and its PNG output are test fixtures.
Helper TestCases are imported as modules, never as discoverable class aliases.
"""
from copy import deepcopy
from hashlib import sha256
from io import BytesIO
import json
from pathlib import Path
from tempfile import TemporaryDirectory
from unittest.mock import Mock, patch
from uuid import uuid4

from PIL import Image
from django.contrib.auth import get_user_model
from django.core.exceptions import ValidationError
from django.core.files.base import ContentFile
from django.test import TestCase, override_settings
from rest_framework.test import APIClient

from studio.models import (
    Asset, ExecutionPreparation, ExecutionPreparationContract, GenerationAttempt,
    GenerationTask, ModelArtifact, ModelDeployment, ModelProfileVersion,
    ProductionRun, ProviderProfile, SeriesProject, StageRun, WorkflowProjector,
    WorkflowProjectorVersion, WorkflowSmokeAttempt, WorkflowTemplate,
    WorkflowTemplateVersion,
)
from studio.services.configuration import resolve_configuration
from studio.services.execution_advancement import advance_execution_preparation
from studio.services.execution_definitions import build_execution_definition
from studio.services.execution_instances import (
    ExecutionResourceSelection, create_execution_draft, create_execution_instance,
    publish_execution_version, update_execution_draft,
)
from studio.services.execution_preparations import (
    create_execution_instance_preparation, read_frozen_execution_request,
)
from studio.services.execution_resource_smoke_definition import build_resource_smoke_definition
from studio.services.execution_smoke import create_instance_smoke, create_resource_smoke
from studio.services.json_contracts import assign_json_contract, json_contract
from studio.services.production_run_plans import freeze_run_execution_plan
from studio.services.production_stage_catalog import production_stage_catalog
from studio.services.security_boundaries import boundary_int
from studio.services.task_queue import claim_next_task
from studio.services.workflow_versions import (
    import_workflow_version, publish_workflow_version, validate_workflow_version,
    verify_published_workflow_capability, verify_workflow_live_validation,
)
from studio.tests import test_execution_resource_smoke_media as resource_helpers
from studio.tests import test_execution_workflow_admission_integration as admission_helpers
from studio.tests import test_workflow_lifecycle_contract as lifecycle_helpers
from studio.tests.execution_runtime_fixtures import record_workflow_runtime
from studio.tests.test_execution_context_inputs import PROJECT_CONFIG
from studio.tests.test_execution_workflow_staging import RecordingUploadClient


_CONTRACTS = json.loads((Path(__file__).parent / "fixtures" / "m4_image_workflows.json").read_text(encoding="utf-8"))

# Isolated /object_info signatures, not observations of installed Comfy nodes.
# Named connection types are explicit so real port/link validation still runs.
_NODE_PORTS = {
    "CLIPLoader": ({"clip_name": "STRING", "type": "STRING", "device": "STRING"}, {}, ["CLIP"]),
    "CLIPTextEncode": ({"text": "STRING", "clip": "CLIP"}, {}, ["CONDITIONING"]),
    "UNETLoader": ({"unet_name": "STRING", "weight_dtype": "STRING"}, {}, ["MODEL"]),
    "DiffusionModelLoaderKJ": ({"model_name": "STRING", "weight_dtype": "STRING", "compute_dtype": "STRING",
        "patch_cublaslinear": "BOOLEAN", "sage_attention": "STRING", "enable_fp16_accumulation": "BOOLEAN"}, {}, ["MODEL"]),
    "VAELoader": ({"vae_name": "STRING"}, {}, ["VAE"]),
    "ModelSamplingAuraFlow": ({"model": "MODEL", "shift": "FLOAT"}, {}, ["MODEL"]),
    "LoraLoaderModelOnly": ({"model": "MODEL", "lora_name": "STRING", "strength_model": "FLOAT"}, {}, ["MODEL"]),
    "CFGNorm": ({"model": "MODEL", "strength": "FLOAT", "pre_cfg": "BOOLEAN"}, {}, ["MODEL"]),
    "EmptySD3LatentImage": ({"width": "INT", "height": "INT", "batch_size": "INT"}, {}, ["LATENT"]),
    "KSampler": ({"model": "MODEL", "seed": "INT", "steps": "INT", "cfg": "FLOAT", "sampler_name": "STRING",
        "scheduler": "STRING", "positive": "CONDITIONING", "negative": "CONDITIONING", "latent_image": "LATENT",
        "denoise": "FLOAT"}, {}, ["LATENT"]),
    "VAEDecode": ({"samples": "LATENT", "vae": "VAE"}, {}, ["IMAGE"]),
    "LoadImage": ({"image": "STRING"}, {}, ["IMAGE", "MASK"]),
    "TextEncodeQwenImageEditPlus": ({"clip": "CLIP", "prompt": "STRING", "vae": "VAE"},
        {"image1": "IMAGE", "image2": "IMAGE", "image3": "IMAGE"}, ["CONDITIONING"]),
    "SaveImage": ({"images": "IMAGE", "filename_prefix": "STRING"}, {}, []),
}


def _object_info(source):
    """Build an external schema fixture without replacing its validator."""
    result = {}
    for node in source["graph"].values():
        name = node["class_type"]
        required, optional, outputs = _NODE_PORTS[name]
        result[name] = {"input": {"required": {key: [value] for key, value in required.items()},
            "optional": {key: [value] for key, value in optional.items()}}, "output": outputs}
        if name == "LoadImage":
            result[name]["input"]["required"]["image"] = [[], {"image_upload": True}]
    for binding in source["semantic_bindings"]:
        node = source["graph"][binding["node_id"]]
        rule = source["input_contract"]["parameters"][binding["semantic_key"]]
        inputs = result[node["class_type"]]["input"]["required"]
        if binding["value_type"] == "model_artifact":
            inputs[binding["input_name"]] = [[node["inputs"][binding["input_name"]]]]
        elif rule["type"] == "enum":
            inputs[binding["input_name"]] = [deepcopy(rule["options"])]
        elif rule["type"] in {"integer", "number"}:
            inputs[binding["input_name"]] = ["INT" if rule["type"] == "integer" else "FLOAT",
                {key: rule[key] for key in ("min", "max") if key in rule}]
    return result


@override_settings(AI_PROVIDER_ALLOWED_HOSTS=("127.0.0.1",))
class M4ImageWorkflowLifecycleTests(TestCase):
    cycle = resource_helpers.ResourceComfyWorkerTests.cycle
    resolve = admission_helpers.WorkflowAdmissionIntegrationTests.resolve
    continue_api = admission_helpers.WorkflowAdmissionIntegrationTests.continue_api
    import_arguments = lifecycle_helpers.WorkflowLifecycleContractTests.import_arguments
    published_facts = lifecycle_helpers.WorkflowLifecycleContractTests.published_facts

    def setUp(self):
        for target in ("requests.sessions.Session.request", "socket.socket.connect", "socket.socket.connect_ex",
                "socket.create_connection", "socket.getaddrinfo"):
            self.enterContext(patch(target, side_effect=AssertionError("M4 image lifecycle forbids real network I/O")))
        self.media_root = self.enterContext(TemporaryDirectory(prefix="m4-image-lifecycle-"))
        self.enterContext(override_settings(MEDIA_ROOT=self.media_root, COMFYUI_OUTPUT_DIR=self.media_root))

    def prepare_target(self, key):
        self.source = deepcopy(_CONTRACTS[key])
        source = self.source
        self.operation = source["input_contract"]["capability"]
        self.assertEqual(json_contract(source["graph"]).sha256, source["graph_sha256"])
        self.assertEqual(json_contract(source["input_contract"]).sha256, source["input_contract_sha256"])
        self.user = get_user_model().objects.create_user(username=f"m4-{key}", is_staff=True)
        self.client = APIClient()
        self.client.force_authenticate(self.user)
        self.provider = ProviderProfile(key=f"m4-{key}", name="Isolated M4 image peer", driver_type="comfyui",
            modalities="image", operations="health,object_info,models,upload,submit,poll,cancel,collect")
        assign_json_contract(self.provider, "driver_config", {"base_url": "http://127.0.0.1:8188",
            "capability_ttl_seconds": 3600, "timeout_seconds": 30, "poll_interval_seconds": 1, "max_poll_seconds": 60})
        self.provider.save()
        self.profile = ModelProfileVersion(profile_key=f"m4-{key}", version=1, provider=self.provider,
            model_identifier=source["model_profile"]["model_identifier"], modality="image", status="published")
        assign_json_contract(self.profile, "specification", source["model_profile"]["specification"])
        self.profile.save()
        self.assertEqual(self.profile.specification_sha256, source["model_profile"]["specification_sha256"])
        requirements = deepcopy(source["model_requirements"])
        for row in requirements:
            artifact = ModelArtifact.objects.create(profile_version=self.profile, role=row["role"], logical_name=row["alias"],
                filename=row["expected_filename"], sha256=row["expected_sha256"], status="available")
            row["artifact_id"] = str(artifact.pk)
        projector_ref = source["input_contract"]["contract_projector"]
        family = WorkflowProjector.objects.create(key=projector_ref["id"], name=f"Isolated {key} projector")
        projector = WorkflowProjectorVersion(id=projector_ref["version_id"], projector=family, status="published", is_current=True,
            **{field: source["projector"][field] for field in
                ("version", "provider_driver_type", "operation", "output_kind", "engine_key", "engine_version")})
        assign_json_contract(projector, "definition", source["projector"]["definition"])
        projector.save()
        self.assertEqual(projector.definition_sha256, source["projector"]["definition_sha256"])
        template = WorkflowTemplate.objects.create(name=f"M4 {key} source v{source['version']}", slug=f"m4-{key}",
            output_kind="image", provider="comfyui", model=self.profile.model_identifier,
            definition={}, input_schema={}, is_system=True, is_active=True)
        # New local workflow/artifact identities: source ordinal/hash is provenance,
        # not an imported success or a fabricated chain of historical versions.
        self.workflow = import_workflow_version(template=template, graph=source["graph"], input_contract=source["input_contract"],
            semantic_bindings=source["semantic_bindings"], model_requirements=requirements,
            provider=self.provider, model_profile_version=self.profile)
        self.assertEqual(self.workflow.graph_sha256, source["graph_sha256"])
        self.assertEqual(self.workflow.input_contract_sha256, source["input_contract_sha256"])
        self.assertEqual((self.workflow.compiler_id, self.workflow.compiler_version),
            (source["compiler_id"], source["compiler_version"]))
        self.deployment = ModelDeployment(key=f"m4-{key}", name="Isolated M4 image target", profile_version=self.profile)
        assign_json_contract(self.deployment, "runtime_config", {})
        self.deployment.save()
        record_workflow_runtime(self.deployment)
        schema = _object_info(source)

        def validation_transport(deployment, operation, payload):
            self.assertEqual(deployment.pk, self.deployment.pk)
            if operation == "object_info":
                return deepcopy(schema)
            if operation == "models":
                return {"items": [row["expected_filename"] for row in requirements]}
            self.fail(f"Unexpected validation transport operation: {operation}")

        with patch("studio.services.workflow_versions.driver_for", return_value=Mock(invoke=Mock(side_effect=validation_transport))):
            self.workflow, _ = validate_workflow_version(self.workflow, deployment=self.deployment)
        self.assertEqual(self.workflow.validation_state, "passed", self.workflow.validation_message)
        verify_workflow_live_validation(self.workflow, self.deployment)
        self.peer, self.upload = Mock(), RecordingUploadClient()
        self.enterContext(patch("studio.services.execution_workflow_smoke_worker.driver_for", return_value=self.peer))
        self.enterContext(patch("studio.services.execution_workflow_smoke_worker.ExecutionWorkflowUploadClient", return_value=self.upload))
        selection = ExecutionResourceSelection(self.deployment, self.operation, "", "manual", None, self.workflow)
        self.definition = build_resource_smoke_definition(selection)
        self.asset_ids = []
        for index in range(3 if key == "qwen_edit" else 0):
            buffer = BytesIO()
            Image.new("RGB", (32, 32), (index * 70, 90, 180)).save(buffer, format="PNG")
            asset = Asset(owner=self.user, kind="reference", name=f"Isolated image reference {index}", checksum=sha256(buffer.getvalue()).hexdigest())
            asset.file.save(f"m4-reference-{index}.png", ContentFile(buffer.getvalue()))
            self.asset_ids.append(str(asset.pk))
        self.fixture = self.resource_fixture(self.definition)
        self.attempt, self.prep, _ = create_resource_smoke(selection=selection,
            expected_definition_sha256=self.definition.sha256, fixture=self.fixture, field_values=[],
            client_idempotency_key=f"m4-resource-{key}", allow_external_calls=False, budget_limit=None, actor=self.user)
        self.prep = advance_execution_preparation(preparation_id=self.prep.pk, expected_preparation_revision=self.prep.revision, actor=self.user)
        self.assertEqual(self.prep.status, "dispatched", self.prep.reason)
        self.resource_attempt = self.finish_smoke()
        self.resource_preparation = self.prep
        self.workflow.refresh_from_db()
        self.workflow = publish_workflow_version(self.workflow, published_by=self.user)
        parent = create_execution_instance(key=f"m4-{key}", name="M4 image lifecycle", description="Isolated fixture", actor=self.user)
        self.version = create_execution_draft(instance_id=parent.pk, expected_revision=parent.revision, actor=self.user,
            selection={"deployment": self.deployment, "operation": self.operation, "mode": "",
                "workflow_version": self.workflow, "prompt_mode": "manual", "prompt_profile_version": None})
        self.version = update_execution_draft(version_id=self.version.pk, expected_revision=self.version.revision, actor=self.user,
            changes={"inputs": deepcopy(self.definition.value["configuration"]["inputs"])})
        self.definition = build_execution_definition(self.version.pk)
        self.version = publish_execution_version(version_id=self.version.pk, expected_revision=self.version.revision,
            expected_definition_sha256=self.definition.sha256, actor=self.user)
        self.definition = build_execution_definition(self.version.pk)
        self.fixture = self.resource_fixture(self.definition)
        self.attempt, self.prep, _ = create_instance_smoke(version_id=self.version.pk,
            expected_definition_sha256=self.definition.sha256, fixture=self.fixture, field_values=[],
            client_idempotency_key=f"m4-combination-{key}", allow_external_calls=False, budget_limit=None, actor=self.user)
        self.prep = advance_execution_preparation(preparation_id=self.prep.pk, expected_preparation_revision=self.prep.revision, actor=self.user)
        self.assertEqual(self.prep.status, "dispatched", self.prep.reason)
        self.combination_attempt = self.finish_smoke()
        self.combination_preparation = self.prep
        self.prepare_production()
        proof, _ = verify_published_workflow_capability(self.workflow, self.deployment, operation=self.operation)
        self.assertEqual(proof.pk, self.resource_attempt.pk)
        self.peer.invoke.reset_mock()

    def resource_fixture(self, definition):
        return {"inputs": {row["key"]: {"items": [{"kind": "asset", "item_key": f"reference-{index}",
                "asset_id": asset_id, "purpose": None} for index, asset_id in enumerate(self.asset_ids)]}
                for row in definition.value["configuration"]["inputs"]},
            "parameters": {key: value["default"] for key, value in self.source["input_contract"]["parameters"].items()
                if "default" in value},
            "manual_channels": {"positive_prompt": "A blue parcel on a table.", "negative_prompt": "Blurred edges."},
            "subject_facts": [], "source_language": "", "target_language": ""}

    def finish_smoke(self):
        self.peer.invoke.side_effect = None
        self.peer.invoke.return_value = {"request_id": str(self.prep.pk)}
        self.upload.uploads.clear()
        submitted = self.cycle()
        self.assertEqual(submitted.status, "running", submitted.error_code)
        self.assertEqual(len(self.upload.uploads), len(self.asset_ids))
        self.assertEqual(len(submitted.staged_inputs), len(self.asset_ids))
        self.prep.refresh_from_db()
        _, _, frozen = read_frozen_execution_request(self.prep)
        compiled = frozen.value["payload"]["compiled_workflow"]
        submitted_graph = self.peer.invoke.call_args.args[2]["workflow"]
        if self.asset_ids:
            for target in ("1", "3"):
                for index in range(3):
                    node_id, output = submitted_graph[target]["inputs"][f"image{index + 1}"]
                    self.assertEqual(output, 0)
                    self.assertEqual(submitted_graph[node_id]["inputs"]["image"], self.upload.uploads[index])
        else:
            self.assertNotIn("LoadImage", {node["class_type"] for node in submitted_graph.values()})
        semantics = compiled["resolved_semantics"]
        primary = compiled["output_contract"]["primary"]
        self.assertEqual(primary["kind"], "image")
        output = Path(self.media_root) / "autoprompt" / "execution" / f"{self.prep.pk}_00001_.png"
        output.parent.mkdir(parents=True, exist_ok=True)
        Image.new("RGB", (semantics["image.width"], semantics["image.height"]), "blue").save(output)
        self.peer.invoke.side_effect = [{"status": "succeeded"}, {"outputs": [{"node_id": primary["node_id"],
            "kind": "images", "filename": output.name, "subfolder": "autoprompt/execution", "type": "output"}]}]
        # Real PNG decoding, dimensions, checksum and original evidence writer.
        done = self.cycle()
        self.assertEqual(done.status, "succeeded", done.error_code)
        fact = done.result["evidence"]["artifact_facts"][0]
        self.assertEqual((fact["kind"], fact["node_id"]), ("image", primary["node_id"]))
        self.assertEqual(fact["sha256"], sha256(output.read_bytes()).hexdigest())
        return done

    def prepare_production(self):
        self.project = SeriesProject.objects.create(owner=self.user, title="M4 isolated image lifecycle", **PROJECT_CONFIG)
        self.run = ProductionRun.objects.create(owner=self.user, project=self.project, plan_source="manual", status="running",
            allow_external_calls=False, idempotency_key=str(uuid4()), request_sha256=json_contract(self.fixture).sha256)
        assign_json_contract(self.run, "config_snapshot", resolve_configuration(run=self.run).snapshot(),
            max_bytes=boundary_int("security.snapshot.stage_max_bytes"))
        self.run.save()
        catalog = production_stage_catalog()
        freeze_run_execution_plan(run=self.run, scope={"kind": "project"}, delivery={"kind": "episode_masters"},
            initial_content=None, stage_catalog_key=catalog.value["key"], expected_stage_catalog_sha256=catalog.sha256, actor=self.user)
        media = next(row for row in catalog.value["stages"] if row["key"] == "media_generation")
        self.stage = StageRun.objects.create(production_run=self.run, stage_key=media["key"], ordinal=media["ordinal"],
            mode="manual", status="running", idempotency_key=str(uuid4()))
        self.preparation = create_execution_instance_preparation(owner_ref=("stage_run_id", self.stage.pk),
            version_id=self.version.pk, expected_definition_sha256=self.definition.sha256,
            unit_key="integration-unit", request_key="m4-image-lifecycle", intent=json_contract(self.fixture),
            resolve_facts=self.resolve, actor=self.user)

    def historical_facts(self):
        prep_ids = (self.resource_preparation.pk, self.combination_preparation.pk)
        return deepcopy({"workflow": self.published_facts(),
            "attempts": list(WorkflowSmokeAttempt.objects.order_by("id").values()),
            "preparations": list(ExecutionPreparation.objects.filter(pk__in=prep_ids).order_by("id").values()),
            "contracts": list(ExecutionPreparationContract.objects.filter(preparation_id__in=prep_ids).order_by("id").values()),
            "requests": [ExecutionPreparationContract.objects.get(preparation_id=pk).prepared_request for pk in prep_ids]})

    def frozen_inputs(self):
        return deepcopy(ExecutionPreparationContract.objects.filter(preparation=self.preparation).values(
            "intent", "intent_sha256", "resolved_inputs", "resolved_inputs_sha256", "review_contract", "review_contract_sha256").get())

    def check_reprojection(self, key, broken):
        self.prepare_target(key)
        original, inputs = self.historical_facts(), self.frozen_inputs()
        preparation = ExecutionPreparation.objects.filter(pk=self.preparation.pk).values().get()
        contract = ExecutionPreparationContract.objects.filter(preparation=self.preparation).values().get()
        versions = list(WorkflowTemplateVersion.objects.order_by("id").values_list("id", flat=True))
        arguments = self.import_arguments()
        if broken == "port":
            binding = next(row for row in arguments["semantic_bindings"] if row["semantic_key"] == "prompt.positive")
            binding["input_name"] = "missing_m4_prompt_port"
        else:
            arguments["input_contract"].pop("contract_projector")
        with self.assertRaises(ValidationError) as rejected:
            import_workflow_version(**arguments)
        if broken == "port":
            self.assertIn("semantic_bindings", rejected.exception.message_dict)
            self.assertIn("missing_m4_prompt_port", str(rejected.exception))
        else:
            self.assertIn("projector", str(rejected.exception).lower())
        self.assertEqual(list(WorkflowTemplateVersion.objects.order_by("id").values_list("id", flat=True)), versions)
        self.assertEqual(self.historical_facts(), original)
        self.assertEqual(self.frozen_inputs(), inputs)
        self.assertEqual(ExecutionPreparation.objects.filter(pk=self.preparation.pk).values().get(), preparation)
        self.assertEqual(ExecutionPreparationContract.objects.filter(preparation=self.preparation).values().get(), contract)
        self.peer.invoke.assert_not_called()
        # Positive control: failed import did not poison the original admission.
        self.continue_api()
        self.assertEqual(self.preparation.status, "dispatched", self.preparation.reason)
        attempt = GenerationAttempt.objects.get(preparation=self.preparation)
        task = claim_next_task()
        self.assertIsNotNone(task)
        self.assertEqual(task.pk, attempt.generation_task_id)
        self.assertEqual(self.frozen_inputs(), inputs)
        self.assertEqual(self.historical_facts(), original)
        self.peer.invoke.assert_not_called()

    def check_dependency_drift(self, key):
        self.prepare_target(key)
        original, inputs = self.historical_facts(), self.frozen_inputs()
        original_versions = list(WorkflowTemplateVersion.objects.order_by("id").values_list("id", flat=True))
        runtime = deepcopy(self.deployment.runtime_config)
        runtime["base_url"] = "http://127.0.0.1:8199"
        assign_json_contract(self.deployment, "runtime_config", runtime)
        self.deployment.save()
        with self.assertRaises(ValidationError) as rejected:
            verify_published_workflow_capability(self.workflow, self.deployment, operation=self.operation)
        self.assertEqual(rejected.exception.code, "workflow_target_drifted")
        self.continue_api()
        self.assertEqual(self.preparation.status, "blocked", self.preparation.reason)
        self.assertTrue(self.preparation.reason_code)
        self.assertTrue(self.preparation.reason)
        self.assertFalse(GenerationAttempt.objects.filter(preparation=self.preparation).exists())
        self.assertFalse(GenerationTask.objects.exists())
        self.assertIsNone(claim_next_task())
        self.peer.invoke.assert_not_called()
        self.assertEqual(self.frozen_inputs(), inputs)
        self.assertEqual(self.historical_facts(), original)
        self.assertEqual(list(WorkflowTemplateVersion.objects.order_by("id").values_list("id", flat=True)), original_versions)
        self.run.refresh_from_db()
        self.assertEqual(self.run.generation_task_count, 0)

    def test_z_v4_bad_port_reprojection_preserves_published_lifecycle(self):
        self.check_reprojection("z_image", "port")

    def test_z_v4_missing_projector_reprojection_preserves_published_lifecycle(self):
        self.check_reprojection("z_image", "projector")

    def test_z_v4_target_drift_blocks_preparation_before_queue(self):
        self.check_dependency_drift("z_image")

    def test_qwen_v2_bad_port_reprojection_preserves_published_lifecycle(self):
        self.check_reprojection("qwen_edit", "port")

    def test_qwen_v2_missing_projector_reprojection_preserves_published_lifecycle(self):
        self.check_reprojection("qwen_edit", "projector")

    def test_qwen_v2_target_drift_blocks_preparation_before_queue(self):
        self.check_dependency_drift("qwen_edit")
