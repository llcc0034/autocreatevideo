from studio.tests.field_definition_fixtures import create_field_definition
"""Exact resource selection uses no Binding, version allocation or AI call."""
from dataclasses import replace
from uuid import uuid4
from unittest.mock import Mock, patch

from django.core.exceptions import ValidationError
from django.db import connection
from django.test import TestCase, override_settings
from django.test.utils import CaptureQueriesContext

from studio.tests.field_definition_fixtures import create_profile_field_rule
from studio.models import ModelDeploymentSmokeAttempt, ModelExecutionInstanceVersion, PromptProfileVersion, FieldDefinition, PromptProfileFieldRule
from studio.services.execution_instances import ExecutionResourceSelection, resolve_execution_resources
from studio.services.execution_definitions import execution_resource_interface
from studio.services.execution_prompts import validate_execution_prompt_channels
from studio.services.execution_resource_smoke_definition import build_resource_smoke_definition, resource_smoke_input_specs
from studio.services.execution_request_preparation import resolve_execution_request_facts
from studio.services.execution_request_compilation import compile_execution_transport_request
from studio.services.json_contracts import json_contract
from studio.services.execution_preparations import create_resource_smoke_preparation, create_execution_instance_preparation, preparation_execution_definition
from studio.services.execution_preparation_evaluation import evaluate_preparation_fields
from studio.services.execution_preparation_requests import compile_prepared_execution_transport
from studio.services.prompt_profiles import refresh_prompt_profile_contract, publish_prompt_profile
from studio.tests import test_execution_native_text as native_fixtures
from studio.tests import test_execution_auxiliary_definitions as profile_fixtures


@override_settings(AI_PROVIDER_ALLOWED_HOSTS=("api.example.com",))
class ExecutionResourceSelectionTests(TestCase):
    setUpTestData = classmethod(native_fixtures.ExecutionNativeTextTests.setUpTestData.__func__)

    def setUp(self):
        native_fixtures.ExecutionNativeTextTests.setUp(self)
        self.selection = ExecutionResourceSelection.from_version(self.version)

    def make_resource_profile(self):
        key = "resource-profile-" + uuid4().hex
        profile = PromptProfileVersion.objects.create(profile_key=key, version=1, name="Exact resource profile",
            model_family="text-protocol", operation="script.generate", mode="", review_state="ready", contract_schema_version=8)
        field = create_field_definition(key=key, label="Instruction", value_type="long_text")
        create_profile_field_rule(fixed_value="Use the explicitly supplied resource fixture.", profile_version=profile, field_definition=field,
            source_selector="fixed.value", generation_source_selector="fixed.value",
            generation_mode="server_locked",
            language_policy="en", channel="positive_prompt", position=1, required=True)
        refresh_prompt_profile_contract(profile)
        return publish_prompt_profile(profile, actor=self.actor)

    def test_manual_and_exact_profile_project_same_native_interface_without_bindings(self):
        profile = self.make_resource_profile()
        count = ModelExecutionInstanceVersion.objects.count()
        for selection in (self.selection, replace(self.selection, prompt_mode="profile", prompt_profile_version=profile)):
            with self.subTest(mode=selection.prompt_mode), CaptureQueriesContext(connection) as queries:
                resources = resolve_execution_resources(selection)
                interface, source, label = execution_resource_interface(selection, resources)
                prompt = validate_execution_prompt_channels(selection, interface["prompt_channels"], source=source, target_label=label)
            self.assertEqual(prompt["mode"], selection.prompt_mode)
            self.assertEqual(interface["parameters"]["max_tokens"]["layers"][0]["source"], "system.execution.resources.review_policy")
            self.assertEqual(interface["operation"], "script.generate")
            self.assertFalse(any("promptprofilebinding" in row["sql"].lower() for row in queries))
            if selection.prompt_mode == "profile":
                self.assertEqual(resources["prompt_profile"]["id"], str(profile.pk))
                self.assertEqual(prompt["profile_sha256"], profile.contract_sha256)
        self.assertEqual(ModelExecutionInstanceVersion.objects.count(), count)

    def test_exact_selection_does_not_accept_manual_plus_profile_or_missing_profile(self):
        for selection in (replace(self.selection, prompt_profile_version=self.make_resource_profile()),
                replace(self.selection, prompt_mode="profile")):
            with self.subTest(mode=selection.prompt_mode), self.assertRaises(ValidationError):
                resolve_execution_resources(selection)

    def test_resource_contract_api_uses_exact_selection_without_tasks_or_binding_reads(self):
        from rest_framework.test import APIClient
        from studio.models import ExecutionPreparation
        client = APIClient()
        client.force_authenticate(self.actor)
        base = f"/api/v1/model-deployments/{self.deployment.pk}/"
        original_count = ModelExecutionInstanceVersion.objects.count()
        options = client.get(base + "smoke-options/")
        self.assertEqual(options.status_code, 200, options.data)
        self.assertIn("script.generate", [row["value"] for row in options.data["target"]["operations"]])
        profile = self.make_resource_profile()
        for mode, profile_id in (("manual", None), ("profile", str(profile.pk))):
            with CaptureQueriesContext(connection) as queries:
                response = client.post(base + "smoke-contract/", {"operation": "script.generate", "mode": "",
                    "prompt_mode": mode, "prompt_profile_version_id": profile_id,
                    "output_version_id": str(self.output_version.pk)}, format="json")
            self.assertEqual(response.status_code, 200, response.data)
            self.assertIsNone(response.data["version_id"])
            self.assertEqual(response.data["scope"], "resource_smoke")
            self.assertEqual(response.data["subject_categories"], [
                {"value": "character", "label": "人物"}, {"value": "scene", "label": "场景"},
                {"value": "prop", "label": "道具"},
            ])
            self.assertNotIn("frozen", response.data)  # Not a published instance contract.
            self.assertNotIn("revision", response.data)
            self.assertEqual(response.data["definition"]["configuration"]["prompt_profile_version_id"], profile_id)
            self.assertEqual(response.data["definition"]["configuration"]["output_version_id"], str(self.output_version.pk))
            self.assertFalse(any("promptprofilebinding" in row["sql"].lower() for row in queries))
        self.assertEqual(ModelExecutionInstanceVersion.objects.count(), original_count)
        self.assertFalse(ExecutionPreparation.objects.exists())
        self.assertFalse(ModelDeploymentSmokeAttempt.objects.exists())

    def test_resource_contract_api_rejects_scope_injection_implicit_profile_and_non_admin(self):
        from rest_framework.test import APIClient
        client = APIClient()
        client.force_authenticate(self.actor)
        base = f"/api/v1/model-deployments/{self.deployment.pk}/"
        body = {"operation": "script.generate", "mode": "", "prompt_mode": "manual", "prompt_profile_version_id": None,
            "output_version_id": str(self.output_version.pk)}
        for extra in ({"scope": "resource"}, {"execution_instance_version_id": str(self.version.pk)},
                {"prompt_profile_binding_id": str(uuid4())}, {"prompt_mode": "profile"}, {"operation": "not.declared"},
                {"output_version_id": None}):
            response = client.post(base + "smoke-contract/", {**body, **extra}, format="json")
            self.assertEqual(response.status_code, 400, response.data)
        self.assertEqual(client.get(base + "smoke-options/", {"scope": "instance"}).status_code, 400)
        self.actor.is_staff = False
        self.actor.save(update_fields=["is_staff"])
        self.assertEqual(client.post(base + "smoke-contract/", body, format="json").status_code, 403)
        self.assertEqual(client.get(base + "smoke-options/").status_code, 403)

    def test_resource_native_fixture_uses_shared_resolution_and_request_compiler_with_null_instance(self):
        from copy import deepcopy
        count = ModelExecutionInstanceVersion.objects.count()
        definition = build_resource_smoke_definition(self.selection)
        self.assertIsNone(definition.value["version_id"])
        self.assertEqual(definition.value["configuration"]["parameters"], [])
        self.assertEqual(definition.value["interface"]["auxiliaries"], {})
        request = deepcopy(self.fixture)
        request["inputs"] = {row["key"]: self.fixture["inputs"][row["targets"][0]["target_key"]]
            for row in definition.value["configuration"]["inputs"]}
        facts = resolve_execution_request_facts(definition, request, owner=self.actor, unit_key="resource-smoke",
            max_media_bytes=1024, review_contract=json_contract({"schema_version": 1, "mode": "manual"}),
            parameter_policies=[], resolve_parameter_source=lambda source: self.fail("No implicit source"))
        resolved = facts.resolved_inputs.value
        transport = compile_execution_transport_request(definition, prepared_parameters=resolved["parameters"],
            manual_channels=request["manual_channels"], idempotency_key="isolated-resource-smoke",
            operation_inputs=resolved["inputs"]["operation_inputs"])
        self.assertIsNone(transport.value["execution_instance_version_id"])
        self.assertEqual(transport.value["payload"]["seed"], 42)
        self.assertEqual(transport.value["prompt_channels"]["values"], request["manual_channels"])
        self.assertEqual(transport.value["operation_plan"]["plan"]["inputs"]["idea"], "旅人放下一个盒子。")
        self.assertEqual(ModelExecutionInstanceVersion.objects.count(), count)
        empty = {**request, "manual_channels": {}}
        with self.assertRaises(ValidationError):
            resolve_execution_request_facts(definition, empty, owner=self.actor, unit_key="resource-smoke",
                max_media_bytes=1024, review_contract=json_contract({"schema_version": 1, "mode": "manual"}),
                parameter_policies=[], resolve_parameter_source=lambda source: self.fail("No implicit source"))

    def test_resource_profile_sources_use_exact_frozen_field_rules_without_binding_or_auxiliary_calls(self):
        from copy import deepcopy
        profile = self.make_resource_profile()
        selection = replace(self.selection, prompt_mode="profile", prompt_profile_version=profile)
        definition = build_resource_smoke_definition(selection)
        request = deepcopy(self.fixture)
        request["manual_channels"] = {}
        request["inputs"] = {row["key"]: self.fixture["inputs"][row["targets"][0]["target_key"]]
            for row in definition.value["configuration"]["inputs"]}
        facts = resolve_execution_request_facts(definition, request, owner=self.actor, unit_key="resource-smoke",
            max_media_bytes=1024, review_contract=json_contract({"schema_version": 1, "mode": "manual"}),
            parameter_policies=[], resolve_parameter_source=lambda source: self.fail("No implicit source"))
        self.assertEqual(definition.value["resources"]["prompt_profile"]["id"], str(profile.pk))
        self.assertEqual(facts.resolved_inputs.value["profile_sources"]["profile"], {
            "version_id": str(profile.pk), "contract_sha256": profile.contract_sha256})
        self.assertEqual(definition.value["configuration"]["auxiliaries"], [])

    def prepare_resource(self, mode):
        from copy import deepcopy
        from dataclasses import asdict
        profile = self.make_resource_profile() if mode == "profile" else None
        selection = replace(self.selection, prompt_mode=mode, prompt_profile_version=profile)
        definition = build_resource_smoke_definition(selection)
        request = deepcopy(self.fixture)
        if profile is not None:
            request["manual_channels"] = {}
        request["inputs"] = {row["key"]: self.fixture["inputs"][row["targets"][0]["target_key"]]
            for row in definition.value["configuration"]["inputs"]}
        attempt = ModelDeploymentSmokeAttempt.objects.create(scope="resource", deployment=self.deployment,
            operation=selection.operation, status="preparing", target_sha256=definition.value["target_fingerprint"],
            request_sha256="", request_snapshot={"schema_version": 2, "resource_definition": asdict(definition)},
            prompt_profile_version=profile, prompt_profile_contract_sha256=profile.contract_sha256 if profile else "")
        resolver = Mock(side_effect=lambda resolved, root: resolve_execution_request_facts(resolved, request,
            owner=self.actor, unit_key="resource-smoke", max_media_bytes=1024,
            review_contract=json_contract({"schema_version": 1, "mode": "manual"}), parameter_policies=[],
            resolve_parameter_source=lambda source: self.fail("No implicit parameter source")))
        args = {"owner_ref": ("deployment_smoke_id", attempt.pk), "expected_definition_sha256": definition.sha256,
            "unit_key": "resource-smoke", "request_key": "candidate-1", "intent": json_contract(request),
            "resolve_facts": resolver, "actor": self.actor}
        return create_resource_smoke_preparation(**args), attempt, args

    def test_resource_manual_preparation_replays_and_compiles_without_fabricated_instance(self):
        prep, attempt, args = self.prepare_resource("manual")
        self.assertIsNone(prep.execution_instance_version_id)
        self.assertEqual(prep.purpose, "resource_smoke")
        replay = create_resource_smoke_preparation(**args)
        self.assertEqual(replay.pk, prep.pk)
        args["resolve_facts"].assert_called_once()
        evaluated = evaluate_preparation_fields(preparation_id=prep.pk, expected_preparation_revision=prep.revision, actor=self.actor)
        preview = compile_prepared_execution_transport(preparation_id=prep.pk,
            expected_preparation_revision=evaluated.revision, actor=self.actor)
        self.assertIsNone(preview.transport.value["execution_instance_version_id"])
        self.assertEqual(preview.transport.value["payload"]["seed"], 42)
        prep.refresh_from_db(); attempt.refresh_from_db()
        self.assertIsNone(prep.request_frozen_at)
        self.assertEqual(attempt.status, "preparing")
        self.assertIsNone(attempt.deadline_started_at)
        with self.assertRaises(ValidationError):
            create_execution_instance_preparation(**args, version_id=self.version.pk)

    def test_resource_profile_preparation_uses_original_field_selection_and_profile_compiler(self):
        prep, attempt, args = self.prepare_resource("profile")
        evaluated = evaluate_preparation_fields(preparation_id=prep.pk, expected_preparation_revision=prep.revision, actor=self.actor)
        preview = compile_prepared_execution_transport(preparation_id=prep.pk,
            expected_preparation_revision=evaluated.revision, actor=self.actor)
        self.assertEqual(preview.transport.value["prompt_channels"]["values"]["positive_prompt"],
            f"{attempt.prompt_profile_version.field_rules.get().field_definition.key}: Use the explicitly supplied resource fixture.")
        self.assertEqual(preview.transport.value["profile_compilation"]["assembly"]["prompt_profile_version_id"], str(attempt.prompt_profile_version_id))
        self.assertNotIn("prompt_profile_binding", {field.name for field in attempt._meta.fields})
        prep.refresh_from_db()
        with patch("studio.services.execution_resource_smoke_definition.build_resource_smoke_definition",
                side_effect=AssertionError("Past preparation must use its frozen resource definition")):
            self.assertEqual(create_resource_smoke_preparation(**args).pk, prep.pk)
            self.assertEqual(preparation_execution_definition(prep).sha256, args["expected_definition_sha256"])
        changed = {**args, "intent": json_contract({**args["intent"].value, "parameters": {"seed": 99}})}
        with self.assertRaisesMessage(ValidationError, "不能覆盖"):
            create_resource_smoke_preparation(**changed)

    def test_legacy_resource_snapshot_is_not_repaired_or_used_as_current_definition(self):
        from studio.services.execution_resource_smoke_definition import read_resource_smoke_definition
        attempt = ModelDeploymentSmokeAttempt.objects.create(scope="resource", deployment=self.deployment,
            operation=self.selection.operation, status="preparing", target_sha256="a" * 64,
            request_sha256="", request_snapshot={"schema_version": 1, "prompt_profile": {"prompt_profile_binding_id": str(uuid4())}})
        with self.assertRaisesMessage(ValidationError, "不读取旧绑定"):
            read_resource_smoke_definition(attempt)

    def test_resource_root_cannot_borrow_instance_identity_or_partial_profile(self):
        from copy import deepcopy
        from dataclasses import asdict
        from studio.services.execution_resource_smoke_definition import read_resource_smoke_definition
        from studio.services.execution_task_links import validate_smoke_preparation_link
        prep, attempt, args = self.prepare_resource("profile")
        validate_smoke_preparation_link(attempt, None)
        original = deepcopy(attempt.request_snapshot)
        for group, key, value in (("configuration", "prompt_profile_version_id", None),
                ("resources", "prompt_profile", {}), ("configuration", "parameters", [{"key": "seed"}])):
            attempt.request_snapshot = deepcopy(original)
            body = attempt.request_snapshot["resource_definition"]["value"]
            body[group][key] = value
            attempt.request_snapshot["resource_definition"] = asdict(json_contract(body))
            with self.subTest(key=key), self.assertRaises(ValidationError):
                read_resource_smoke_definition(attempt)
        attempt.request_snapshot = original
        for scope, version_id in (("instance", None), ("resource", self.version.pk)):
            attempt.scope, attempt.execution_instance_version_id = scope, version_id
            with self.subTest(scope=scope), self.assertRaises(ValidationError):
                read_resource_smoke_definition(attempt)
        attempt.scope, attempt.execution_instance_version_id = "resource", None
        with self.assertRaises(ValidationError):
            validate_smoke_preparation_link(attempt, self.version.pk)
        prep.purpose = "production"
        with self.assertRaises(ValidationError):
            preparation_execution_definition(prep)

    def test_inactive_resource_blocks_before_freeze_and_never_queues_old_path(self):
        from studio.services.execution_finalization import finalize_execution
        from studio.services.execution_smoke_admission import collect_smoke_execution_admission
        prep, attempt, args = self.prepare_resource("manual")
        self.deployment.status = "inactive"
        self.deployment.save(update_fields=("status", "updated_at"))
        original = prep.contract.resolved_inputs
        evaluated = evaluate_preparation_fields(preparation_id=prep.pk, expected_preparation_revision=prep.revision, actor=self.actor)
        result = finalize_execution(preparation_id=prep.pk, expected_preparation_revision=evaluated.revision,
            actor=self.actor, collect_admission_facts=collect_smoke_execution_admission)
        self.assertEqual(result.status, "blocked")
        blockers = result.contract.evaluation["admission"]["blockers"]
        self.assertIn("execution_resource_inactive", [row["code"] for row in blockers])
        self.assertEqual(result.contract.resolved_inputs, original)
        self.assertIsNone(result.request_frozen_at)
        attempt.refresh_from_db()
        self.assertEqual(attempt.status, "preparing")
        self.assertIsNone(attempt.submission_started_at)
        self.assertIsNone(attempt.deadline_started_at)

    def test_resource_create_api_resumes_original_preparation_replays_and_cancels_both_records(self):
        from copy import deepcopy
        from rest_framework.test import APIClient
        from studio.models import ExecutionPreparation
        definition = build_resource_smoke_definition(self.selection)
        fixture = deepcopy(self.fixture)
        fixture["inputs"] = {row["key"]: fixture["inputs"][row["targets"][0]["target_key"]]
            for row in definition.value["configuration"]["inputs"]}
        body = {"selection": {"operation": "script.generate", "mode": "", "prompt_mode": "manual", "prompt_profile_version_id": None,
                "output_version_id": str(self.output_version.pk)},
            "expected_definition_sha256": definition.sha256, "fixture": fixture, "field_values": [],
            "allow_external_calls": False}
        client = APIClient()
        client.force_authenticate(self.actor)
        endpoint = f"/api/v1/model-deployments/{self.deployment.pk}/smoke-test/"
        with patch("studio.services.provider_registry._request_json", side_effect=AssertionError("Creation must not submit")):
            self.assertEqual(client.post(endpoint, body, format="json").status_code, 400)
            created = client.post(endpoint, body, format="json", HTTP_IDEMPOTENCY_KEY="resource-api")
            self.assertEqual(created.status_code, 202, created.data)
            attempt_id, prep_id = str(created.data["attempt_id"]), str(created.data["preparation"]["id"])
            prep_url = f"/api/v1/execution-preparations/{prep_id}/"
            self.assertTrue(created["Location"].endswith(prep_url))
            self.assertEqual(created.data["preparation"]["purpose"], "resource_smoke")
            self.assertIsNone(created.data["preparation"]["execution_instance_version"])
            task_url = f"/api/v1/model-deployment-smoke-attempts/{attempt_id}/"
            detail = client.get(task_url)
            self.assertEqual(detail.status_code, 200, detail.data)
            self.assertEqual(detail.data["preparation_id"], prep_id)
            self.assertIsNone(detail.data["prompt_profile"])
            resumed = client.get(prep_url)
            self.assertEqual(resumed.status_code, 200, resumed.data)
            continued = client.post(prep_url + "continue/", {"expected_preparation_revision": resumed.data["revision"]}, format="json")
            self.assertEqual(continued.status_code, 200, continued.data)
            self.assertEqual(str(continued.data["id"]), prep_id)
            repeated = client.post(endpoint, body, format="json", HTTP_IDEMPOTENCY_KEY="resource-api")
            self.assertEqual(repeated.status_code, 200, repeated.data)
            self.assertEqual(str(repeated.data["attempt_id"]), attempt_id)
            altered = {**body, "fixture": {**fixture, "parameters": {**fixture["parameters"], "seed": 7}}}
            conflict = client.post(endpoint, altered, format="json", HTTP_IDEMPOTENCY_KEY="resource-api")
            self.assertEqual(conflict.status_code, 409, conflict.data)
            cancelled = client.post(task_url + "cancel/", {}, format="json")
            self.assertEqual(cancelled.status_code, 202, cancelled.data)
            self.assertEqual(cancelled.data["status"], "cancelled")
            self.assertEqual(client.post(task_url + "cancel/", {}, format="json").status_code, 202)
        prep = ExecutionPreparation.objects.get(pk=prep_id)
        attempt = ModelDeploymentSmokeAttempt.objects.get(pk=attempt_id)
        self.assertEqual(prep.status, "cancelled")
        self.assertIsNone(attempt.submission_started_at)
        self.assertIsNone(attempt.deadline_started_at)
        self.assertEqual(ExecutionPreparation.objects.count(), 1)
        self.assertEqual(ModelDeploymentSmokeAttempt.objects.count(), 1)

    def test_shared_management_creation_allocates_one_resource_root_and_preserves_exact_replay(self):
        from copy import deepcopy
        from studio.models import AssetGenerationJob, ExecutionPreparation
        from studio.services.execution_smoke import create_resource_smoke
        definition = build_resource_smoke_definition(self.selection)
        fixture = deepcopy(self.fixture)
        fixture["inputs"] = {row["key"]: fixture["inputs"][row["targets"][0]["target_key"]]
            for row in definition.value["configuration"]["inputs"]}
        baseline = ModelExecutionInstanceVersion.objects.count()
        args = {"selection": self.selection, "expected_definition_sha256": definition.sha256, "fixture": fixture,
            "field_values": [], "client_idempotency_key": "resource-create", "allow_external_calls": False, "budget_limit": None, "actor": self.actor}
        attempt, prep, created = create_resource_smoke(**args)
        self.assertTrue(created)
        self.assertEqual((attempt.scope, prep.purpose, attempt.status), ("resource", "resource_smoke", "preparing"))
        self.assertIsNone(prep.execution_instance_version_id)
        self.assertNotIn("prompt_profile_binding", {field.name for field in attempt._meta.fields})
        self.assertIsNone(attempt.deadline_started_at)
        repeated, saved, again = create_resource_smoke(**args)
        self.assertFalse(again)
        self.assertEqual((repeated.pk, saved.pk), (attempt.pk, prep.pk))
        with self.assertRaises(ValidationError):
            create_resource_smoke(**{**args, "allow_external_calls": True})
        with self.assertRaises(ValidationError):
            create_resource_smoke(**{**args, "reuse_preparation_id": prep.pk})
        self.assertEqual(ExecutionPreparation.objects.count(), 1)
        self.assertEqual(ModelExecutionInstanceVersion.objects.count(), baseline)
        self.assertFalse(AssetGenerationJob.objects.exists())
        from studio.services.execution_smoke_dispatch import cancel_prepared_smoke
        cancelled = cancel_prepared_smoke(prep, self.actor)
        self.assertEqual(cancelled.status, "cancelled")
        prep.refresh_from_db()
        self.assertEqual(prep.status, "cancelled")
        self.assertIsNone(prep.request_frozen_at)
        self.assertIsNone(cancelled.submission_started_at)
        self.assertEqual(cancel_prepared_smoke(prep, self.actor).pk, cancelled.pk)
        self.actor.is_staff = False
        self.actor.save(update_fields=["is_staff"])
        from django.core.exceptions import PermissionDenied
        with self.assertRaises(PermissionDenied):
            create_resource_smoke(**args)

@override_settings(AI_PROVIDER_ALLOWED_HOSTS=("api.example.com", "cdn.example.com"))
class ResourceProfileFieldReviewTests(TestCase):
    setUpTestData = classmethod(profile_fixtures.ExecutionAuxiliaryDefinitionTests.setUpTestData.__func__)
    write_contract = staticmethod(profile_fixtures.ExecutionAuxiliaryDefinitionTests.write_contract)
    input = staticmethod(profile_fixtures.ExecutionAuxiliaryDefinitionTests.input)

    def setUp(self):
        profile_fixtures.ExecutionAuxiliaryDefinitionTests.setUp(self)
        for target in ("studio.services.provider_registry._request_json", "studio.services.provider_registry.resolve_credential"):
            self.enterContext(patch(target, side_effect=AssertionError("Resource field review cannot call a model")))

    def test_resource_profile_required_fields_use_explicit_fixture_review_not_automatic_ai(self):
        from rest_framework.test import APIClient
        from studio.models import AssetGenerationJob
        from studio.services.execution_smoke import create_resource_smoke
        from studio.services.execution_asset_auxiliaries import create_asset_field_child
        selection = ExecutionResourceSelection.from_version(self.version)
        definition = build_resource_smoke_definition(selection)
        sources = {"request.action": {"kind": "literal", "value": "A courier opens a blue parcel."},
            "context.project.style": {"kind": "literal", "value": {"lighting": "soft daylight"}}}
        fixture = {"inputs": {row["key"]: sources[row["targets"][0]["target_key"]]
                for row in definition.value["configuration"]["inputs"]},
            "parameters": {"width": 768, "height": 1344, "fps": 24, "duration_seconds": 3},
            "manual_channels": {}, "subject_facts": [], "source_language": "English", "target_language": "English"}
        attempt, prep, _ = create_resource_smoke(selection=selection, expected_definition_sha256=definition.sha256,
            fixture=fixture, field_values=[], client_idempotency_key="resource-fields", allow_external_calls=True,
            budget_limit=None, actor=self.actor)
        self.assertEqual(prep.status, "awaiting_input")
        self.assertIsNone(prep.next_check_at)
        client = APIClient()
        client.force_authenticate(self.actor)
        url = f"/api/v1/execution-preparations/{prep.pk}/"
        detail = client.get(url)
        self.assertEqual(detail.status_code, 200, detail.data)
        self.assertEqual(detail.data["auxiliary_batches"], [])
        self.assertEqual(len(detail.data["field_slots"]), 1)
        continued = client.post(url + "continue/", {"expected_preparation_revision": prep.revision}, format="json")
        self.assertEqual(continued.status_code, 200, continued.data)
        self.assertEqual(continued.data["status"], "awaiting_input")
        prep.refresh_from_db()
        self.assertFalse(AssetGenerationJob.objects.exists())
        with self.assertRaisesMessage(ValidationError, "不自动分配"):
            create_asset_field_child(preparation_id=prep.pk, expected_preparation_revision=prep.revision,
                field_batch_key=prep.contract.resolved_inputs["field_requests"][0]["sha256"], attempt_number=1, actor=self.actor)
        field_key = detail.data["field_slots"][0]["field_key"]
        created = client.post("/api/v1/execution-field-values/", {"preparation_id": str(prep.pk), "unit_key": "smoke",
            "field_key": field_key, "text": "An explicitly reviewed resource fixture.", "revision": 1,
            "expected_preparation_revision": prep.revision}, format="json")
        self.assertEqual(created.status_code, 201, created.data)
        self.assertEqual(created.data["field"]["origin_kind"], "manual")
        self.assertEqual(created.data["field"]["status"], "pending")
        field = created.data["field"]
        confirmed = client.post(f"/api/v1/execution-field-values/{field['id']}/confirm/", {
            "expected_text_sha256": field["text_sha256"], "expected_source_sha256": field["source_sha256"],
            "expected_preparation_revision": created.data["preparation"]["revision"], "select_for_execution": True,
            "expected_selection_revision": None, "replace": False}, format="json")
        self.assertEqual(confirmed.status_code, 200, confirmed.data)
        self.assertEqual(confirmed.data["field"]["approval_kind"], "human")
        prep.refresh_from_db()
        evaluated = evaluate_preparation_fields(preparation_id=prep.pk, expected_preparation_revision=prep.revision, actor=self.actor)
        preview = compile_prepared_execution_transport(preparation_id=prep.pk,
            expected_preparation_revision=evaluated.revision, actor=self.actor)
        self.assertEqual(preview.transport.value["prompt_channels"]["values"]["positive_prompt"],
            f"{field_key}: An explicitly reviewed resource fixture.")
        self.assertFalse(AssetGenerationJob.objects.exists())
        attempt.refresh_from_db()
        self.assertEqual(attempt.status, "preparing")
        self.assertIsNone(attempt.deadline_started_at)


class ResourceSmokePortProjectionTests(TestCase):
    def target(self, key, *, kind="image", parent=None):
        return {"target_kind": "workflow_slot", "target_key": key, "kind": kind, "min_items": 0, "max_items": 3,
            "required": False, "wiring": {"kind": "classified_group", **({"index_source_target_key": parent} if parent else {})}}

    def test_resource_ports_keep_type_count_pairing_and_distinct_profile_identity(self):
        from copy import deepcopy
        interface = {"input_targets": {"references": self.target("references", kind="video"),
            "sounds": self.target("sounds", kind="audio", parent="references")}, "profile_inputs": {
                "references": {"target_kind": "profile_input", "target_key": "references", "kind": "text",
                    "min_items": 1, "max_items": 1, "required": True}}, "input_purposes": {"video": [{"value": "declared-purpose"}]}}
        original = deepcopy(interface)
        inputs = resource_smoke_input_specs(interface)
        parent, child, profile = inputs
        self.assertEqual(child["semantic_contract"]["paired_with_input_key"], parent["key"])
        self.assertNotEqual(profile["key"], parent["key"])
        self.assertEqual(parent["semantic_contract"]["allowed_purposes"], ["declared-purpose"])
        self.assertEqual(child["max_items"], 3)
        self.assertFalse(profile["targets"][0]["is_primary"])
        self.assertEqual(interface, original)
        self.assertEqual(resource_smoke_input_specs(interface), inputs)
        self.assertTrue(all(len(row["key"]) <= 100 for row in inputs))

    def test_required_context_uses_registered_scope_not_synthetic_project_or_literal(self):
        target = {"target_kind": "operation_input", "target_key": "source", "kind": "json",
            "min_items": 1, "max_items": 1, "required": True,
            "source_kinds": ["context_field", "selected_output"], "context_selectors": ["script.visual_context"]}
        interface = {"input_targets": {"source": target}, "input_purposes": {}}
        self.assertEqual(resource_smoke_input_specs(interface)[0]["semantic_contract"]["allowed_scope_kinds"], ["project"])
        target["context_selectors"] = ["unknown.source"]
        with self.assertRaisesMessage(ValidationError, "不以样例代替"):
            resource_smoke_input_specs(interface)


@override_settings(AI_PROVIDER_ALLOWED_HOSTS=("127.0.0.1",))
class WorkflowResourceDefinitionTests(TestCase):
    from studio.tests import test_execution_workflow_request_media as workflow_fixtures
    _create_version = workflow_fixtures.ExecutionWorkflowMediaRequestTests._create_version

    def setUp(self):
        self.workflow_fixtures.ExecutionWorkflowMediaRequestTests.setUp(self)
        self.user.is_staff = True
        self.user.save(update_fields=["is_staff"])

    def test_workflow_resource_contract_api_uses_its_validated_target_and_declared_dynamic_ports(self):
        from rest_framework.test import APIClient
        from studio.models import ExecutionPreparation, WorkflowSmokeAttempt
        client = APIClient()
        client.force_authenticate(self.user)
        self.workflow.status = "validated"
        self.workflow.save(update_fields=["status", "updated_at"])
        base = f"/api/v1/workflow-template-versions/{self.workflow.pk}/"
        choices = client.get(base + "smoke-options/")
        self.assertEqual(choices.status_code, 200, choices.data)
        self.assertEqual(choices.data["target"]["deployment_id"], str(self.deployment.pk))
        self.assertIsNone(choices.data["target"]["model_family"])  # Manual resource has no Profile-matching requirement.
        self.assertEqual([row["value"] for row in choices.data["target"]["operations"]], ["video.reference_to_video"])
        self.assertNotIn("profile", [row["value"] for row in choices.data["prompt_modes"]])
        selection = {"operation": "video.reference_to_video", "mode": "Ref2VA", "prompt_mode": "manual", "prompt_profile_version_id": None}
        result = client.post(base + "smoke-contract/", selection, format="json")
        self.assertEqual(result.status_code, 200, result.data)
        self.assertEqual(result.data["subject_categories"], [
            {"value": "character", "label": "人物"}, {"value": "scene", "label": "场景"},
            {"value": "prop", "label": "道具"},
        ])
        definition = result.data["definition"]
        self.assertIsNone(definition["version_id"])
        self.assertEqual(definition["configuration"]["workflow_version_id"], str(self.workflow.pk))
        self.assertEqual(definition["interface"]["parameters"]["video.frames"]["rule"],
            build_resource_smoke_definition(ExecutionResourceSelection(self.deployment,
                "video.reference_to_video", "Ref2VA", "manual", None, self.workflow)).value["interface"]["parameters"]["video.frames"]["rule"])
        direct = client.post(f"/api/v1/model-deployments/{self.deployment.pk}/smoke-contract/", selection, format="json")
        self.assertEqual(direct.status_code, 400, direct.data)
        self.assertFalse(ExecutionPreparation.objects.exists())
        self.assertFalse(WorkflowSmokeAttempt.objects.exists())

    def resource_fixture(self, definition):
        port = next(row for row in definition.value["configuration"]["inputs"] if row["targets"][0]["target_key"] == "images")
        return {"inputs": {port["key"]: {"items": [{"kind": "asset", "item_key": key,
            "asset_id": str(self.asset.pk), "purpose": None} for key in ("second", "first")]}},
            "parameters": {key: value for key, value in self.parameters["values"].items() if self.parameters["sources"][key] == "request"},
            "manual_channels": {"positive_prompt": "A parcel opens."}, "subject_facts": [], "source_language": "", "target_language": ""}

    def test_draft_workflow_uses_original_resource_root_and_same_dynamic_media_compiler(self):
        from dataclasses import asdict
        from studio.models import WorkflowSmokeAttempt
        from studio.services.json_contracts import assign_json_contract
        from studio.services.execution_resource_smoke_definition import read_resource_smoke_definition
        self.workflow.status = "draft"
        self.workflow.save(update_fields=["status", "updated_at"])
        selection = ExecutionResourceSelection(self.deployment, "video.reference_to_video", "Ref2VA", "manual", None, self.workflow)
        with self.assertRaises(ValidationError):
            resolve_execution_resources(selection)
        definition = build_resource_smoke_definition(selection)
        fixture = self.resource_fixture(definition)
        from studio.services.execution_smoke import create_resource_smoke
        with self.assertRaisesMessage(ValidationError, "先完成Live Validate"):
            create_resource_smoke(selection=selection, expected_definition_sha256=definition.sha256, fixture=fixture,
                field_values=[], client_idempotency_key="not-validated", allow_external_calls=False, budget_limit=None, actor=self.user)
        self.assertFalse(WorkflowSmokeAttempt.objects.exists())
        attempt = WorkflowSmokeAttempt(scope="resource", deployment=self.deployment, workflow_version=self.workflow,
            created_by=self.user, status="preparing", target_sha256=definition.value["target_fingerprint"],
            graph_sha256=self.workflow.graph_sha256, execution_contract_sha256=self.workflow.execution_contract_sha256,
            validation_evidence_sha256=self.workflow.validation_evidence_sha256)
        assign_json_contract(attempt, "fixture_values", {"schema_version": 3, "resource_definition": asdict(definition)}, schema_version=3)
        attempt.fixture_sha256 = attempt.fixture_values_sha256
        attempt.save()
        self.assertEqual(read_resource_smoke_definition(attempt, current=True).sha256, definition.sha256)
        def resolve(frozen, root):
            return resolve_execution_request_facts(frozen, fixture, owner=self.user, unit_key="smoke", max_media_bytes=65536,
                review_contract=json_contract({"schema_version": 1, "mode": "manual"}), parameter_policies=[],
                resolve_parameter_source=lambda source: self.fail("No implicit parameter source"))
        prep = create_resource_smoke_preparation(owner_ref=("workflow_smoke_id", attempt.pk),
            expected_definition_sha256=definition.sha256, unit_key="smoke", request_key="resource-dynamic", intent=json_contract(fixture),
            resolve_facts=resolve, actor=self.user)
        evaluated = evaluate_preparation_fields(preparation_id=prep.pk, expected_preparation_revision=prep.revision, actor=self.user)
        preview = compile_prepared_execution_transport(preparation_id=prep.pk, expected_preparation_revision=evaluated.revision, actor=self.user)
        media = preview.transport.value["payload"]["media_preparation"]
        self.assertEqual([row["item_key"] for row in media["bindings"]], ["second", "first"])
        self.assertEqual(len({row["token"] for row in media["bindings"]}), 2)
        self.assertIsNone(preview.transport.value["execution_instance_version_id"])
        self.assertEqual(self.workflow.status, "draft")
        attempt.fixture_values_schema_version = 2
        with self.assertRaisesMessage(ValidationError, "快照版本"):
            read_resource_smoke_definition(attempt)
        attempt.fixture_values_schema_version = 3
        attempt.execution_contract_sha256 = "0" * 64
        with self.assertRaisesMessage(ValidationError, "执行合同不一致"):
            read_resource_smoke_definition(attempt)

    def validate_resource_workflow(self):
        from studio.services.workflow_versions import validate_workflow_version, verify_workflow_live_validation
        self.workflow.status = "draft"
        self.workflow.save(update_fields=["status", "updated_at"])
        peer = Mock()
        peer.invoke.return_value = {}
        report = Mock()
        report.as_dict.return_value = {"schema_version": 1}
        with patch("studio.services.workflow_versions.driver_for", return_value=peer), patch(
                "studio.services.workflow_versions.validate_comfyui_api_contract", return_value=report), patch(
                "studio.services.workflow_versions._validate_model_inventory", return_value={"schema_version": 2, "checked_requirements": []}):
            validated, _ = validate_workflow_version(self.workflow, deployment=self.deployment)
        self.assertEqual(validated.status, "validated")
        verify_workflow_live_validation(validated, self.deployment)
        return validated

    def test_workflow_resource_create_api_resumes_and_cancels_the_same_preparation(self):
        from rest_framework.test import APIClient
        from studio.models import ExecutionPreparation, WorkflowSmokeAttempt
        validated = self.validate_resource_workflow()
        selection = ExecutionResourceSelection(self.deployment, "video.reference_to_video", "Ref2VA", "manual", None, validated)
        definition = build_resource_smoke_definition(selection)
        client = APIClient()
        client.force_authenticate(self.user)
        body = {"selection": {"operation": selection.operation, "mode": selection.mode, "prompt_mode": "manual", "prompt_profile_version_id": None},
            "expected_definition_sha256": definition.sha256, "fixture": self.resource_fixture(definition),
            "field_values": [], "allow_external_calls": False}
        endpoint = f"/api/v1/workflow-template-versions/{validated.pk}/smoke-test/"
        created = client.post(endpoint, body, format="json", HTTP_IDEMPOTENCY_KEY="workflow-resource-api")
        self.assertEqual(created.status_code, 202, created.data)
        self.assertEqual(created.data["attempt_kind"], "workflow_smoke")
        prep_id, attempt_id = str(created.data["preparation"]["id"]), str(created.data["attempt_id"])
        task_url = f"/api/v1/workflow-smoke-attempts/{attempt_id}/"
        detail = client.get(task_url)
        self.assertEqual(detail.status_code, 200, detail.data)
        self.assertEqual(detail.data["preparation_id"], prep_id)
        self.assertEqual(detail.data["scope"], "resource")
        repeated = client.post(endpoint, body, format="json", HTTP_IDEMPOTENCY_KEY="workflow-resource-api")
        self.assertEqual(repeated.status_code, 200, repeated.data)
        self.assertEqual(str(repeated.data["preparation"]["id"]), prep_id)
        cancelled = client.post(task_url + "cancel/", {}, format="json")
        self.assertEqual(cancelled.status_code, 202, cancelled.data)
        self.assertEqual(cancelled.data["status"], "cancelled")
        self.assertEqual(ExecutionPreparation.objects.get(pk=prep_id).status, "cancelled")
        self.assertIsNone(WorkflowSmokeAttempt.objects.get(pk=attempt_id).submission_started_at)
        self.assertEqual(WorkflowSmokeAttempt.objects.count(), 1)

    def test_validated_workflow_creates_resource_preparation_without_its_own_smoke_or_published_instance(self):
        from studio.models import WorkflowSmokeAttempt
        from studio.services.workflow_versions import verify_workflow_live_validation
        from studio.services.execution_smoke import create_resource_smoke
        from studio.services.execution_resource_admission import read_execution_resource_state
        validated = self.validate_resource_workflow()
        selection = ExecutionResourceSelection(self.deployment, "video.reference_to_video", "Ref2VA", "manual", None, validated)
        definition = build_resource_smoke_definition(selection)
        attempt, prep, created = create_resource_smoke(selection=selection, expected_definition_sha256=definition.sha256,
            fixture=self.resource_fixture(definition), field_values=[], client_idempotency_key="validated-resource",
            allow_external_calls=False, budget_limit=None, actor=self.user)
        self.assertTrue(created)
        self.assertEqual((attempt.scope, prep.purpose), ("resource", "resource_smoke"))
        self.assertIsNone(prep.execution_instance_version_id)
        self.assertIsNone(attempt.deadline_started_at)
        self.assertEqual(WorkflowSmokeAttempt.objects.count(), 1)
        validated.validation_evidence = {}
        with self.assertRaises(ValidationError):
            verify_workflow_live_validation(validated, self.deployment)
        state = read_execution_resource_state(selection).value
        self.assertIn("execution_live_validation_invalid", [row["code"] for row in state["blockers"]])
