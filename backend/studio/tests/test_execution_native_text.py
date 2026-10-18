from studio.tests.field_definition_fixtures import create_field_definition
"""Native script wire contract and original queues; all peer output is isolated."""
from copy import deepcopy
import json
from pathlib import Path
import tempfile
from unittest.mock import Mock, patch

from django.core.exceptions import ValidationError
from django.test import TestCase, override_settings
from rest_framework.test import APIClient

from studio.tests.field_definition_fixtures import create_profile_field_rule
from studio.models import Asset, ModelProfileVersion, ModelDeployment, ModelDeploymentCapabilitySnapshot, PromptProfileVersion, PromptProfileFieldRule, FieldDefinition
from studio.services.asset_generation import create_asset_generation_job, claim_next_text_asset_job, process_text_asset_job
from studio.services.deployment_smoke_queue import claim_next_deployment_smoke, process_deployment_smoke
from studio.services.execution_advancement import advance_execution_preparation
from studio.services.execution_definitions import build_execution_definition
from studio.services.execution_instances import create_execution_instance, create_execution_draft, update_execution_draft, publish_execution_version
from studio.services.execution_smoke import create_instance_smoke
from studio.services.execution_text_contracts import parse_execution_text_response
from studio.services.execution_preparation_requests import compile_prepared_execution_transport
from studio.services.prompt_profiles import refresh_prompt_profile_contract, publish_prompt_profile
from studio.services.json_contracts import assign_json_contract
from studio.tests import test_execution_text_contracts as fixtures
from studio.tests.test_execution_asset_requests import freeze, isolated_observations
from studio.tests import test_script_revision_structures as semantic_fixtures


@override_settings(AI_PROVIDER_ALLOWED_HOSTS=("api.example.com",))
class ExecutionNativeTextTests(TestCase):
    setUpTestData = classmethod(fixtures.ExecutionTextContractTests.setUpTestData.__func__)
    make_output = semantic_fixtures.ScriptRevisionStructureTests.make_output

    def setUp(self):
        self.media_root = self.enterContext(tempfile.TemporaryDirectory())
        self.enterContext(override_settings(MEDIA_ROOT=self.media_root))
        self.enterContext(patch("studio.services.provider_registry._request_json", side_effect=AssertionError("no network")))
        from studio.tests.secret_fixtures import enable_test_keyring, create_test_secret
        enable_test_keyring(self)
        provider = self.deployment.provider
        assign_json_contract(provider, "driver_config", {**provider.driver_config,
            "pricing": {"fixed_per_request": "0.1", "currency": "USD"}})
        provider.save()
        model = ModelProfileVersion(profile_key="native-script", version=1, model_identifier="native-model",
            provider=provider, modality="text", status="published")
        assign_json_contract(model, "specification", {"schema_version": 1, "model_family": "text-protocol", "capabilities": ["script.generate"],
            "series_concept": {"timeout_seconds": 30, "chat_request_options": {"temperature": 0.2}}})
        model.save()
        self.deployment = ModelDeployment(key="native-script", name="Native script", profile_version=model,
            credential=create_test_secret(key="isolated-native-credential", actor=self.actor))
        assign_json_contract(self.deployment, "runtime_config", {})
        self.deployment.save()
        instance = create_execution_instance(key="native-script", name="Native script", description="", actor=self.actor)
        self.output_version, self.candidate = semantic_fixtures.ScriptRevisionStructureTests.make_output(self)
        self.version = create_execution_draft(instance_id=instance.pk, expected_revision=instance.revision, actor=self.actor,
            selection={"deployment": self.deployment, "operation": "script.generate", "mode": "", "prompt_mode": "manual",
                "prompt_profile_version": None, "workflow_version": None, "output_version": self.output_version})
        kinds = {"idea": "text", "style_hint": "text", "episode_count": "json", "shots_per_episode": "json", "episode_duration_seconds": "json"}
        self.version = update_execution_draft(version_id=self.version.pk, expected_revision=self.version.revision, actor=self.actor,
            changes={"inputs": [{"key": key, "position": index, "kind": kind, "required": True, "min_items": 1, "max_items": 1,
                "semantic_contract": {"schema_version": 1, "allowed_purposes": [], "allowed_subject_kinds": [],
                    "allowed_scope_kinds": ["request"], "paired_with_input_key": None, "allow_repeated_asset": False},
                "targets": [{"target_kind": "operation_input", "target_key": key, "is_primary": True}]}
                for index, (key, kind) in enumerate(kinds.items())]})
        self.definition = build_execution_definition(self.version.pk)
        publish_execution_version(version_id=self.version.pk, expected_revision=self.version.revision,
            expected_definition_sha256=self.definition.sha256, actor=self.actor)
        self.fixture = {"inputs": {key: {"kind": "literal", "value": value} for key, value in {
            "idea": "旅人放下一个盒子。", "style_hint": "安静", "episode_count": 1,
            "shots_per_episode": 1, "episode_duration_seconds": 2}.items()},
            "parameters": {"seed": 42}, "manual_channels": {"positive_prompt": "只使用给出的故事事实。"},
            "subject_facts": [], "source_language": "", "target_language": ""}

    def smoke(self):
        return create_instance_smoke(version_id=self.version.pk, expected_definition_sha256=self.definition.sha256,
            fixture=self.fixture, field_values=[], client_idempotency_key="native-smoke", allow_external_calls=True,
            budget_limit=None, actor=self.actor)

    def test_input_form_metadata_matches_native_types_without_changing_frozen_definition(self):
        from studio.services.execution_definitions import read_execution_input_contract
        from studio.services.execution_input_catalog import execution_input_form_options
        from studio.services.execution_native_text import validate_native_text_inputs
        from studio.execution_instance_serializers import ExecutionInputContractSerializer
        contract = read_execution_input_contract(self.version.pk)
        self.assertEqual(contract["sha256"], self.definition.sha256)
        self.assertEqual(contract["definition"], self.definition.value)
        forms = ExecutionInputContractSerializer(contract).data["input_forms"]
        self.assertEqual(forms["episode_count"]["label"], "本轮计划集数")
        self.assertEqual(forms["episode_count"]["schemas"], [{"type": "integer", "minimum": 1}])
        changed = deepcopy(self.definition.value)
        changed["configuration"]["inputs"][2]["key"] = "custom_count"
        self.assertEqual(execution_input_form_options(changed)["custom_count"], forms["episode_count"])
        changed["interface"]["output"]["adapter"] = "unregistered"
        self.assertEqual(execution_input_form_options(changed)["custom_count"]["schemas"], [])
        values = {key: row["value"] for key, row in self.fixture["inputs"].items()}
        for invalid in (True, 0, 1.2, {}, "2"):
            with self.subTest(invalid=invalid), self.assertRaises(ValidationError):
                validate_native_text_inputs("script.generate", {**values, "episode_count": invalid})

    def test_input_form_keeps_all_exact_target_constraints(self):
        from studio.services.execution_input_catalog import execution_input_form_options
        definition = deepcopy(self.definition.value)
        definition["interface"]["input_targets"]["episode_count"]["value_schema"] = {
            "type": "integer", "maximum": 8, "title": "精确配置名称"}
        schema = execution_input_form_options(definition)["episode_count"]
        self.assertEqual(schema["label"], "精确配置名称")
        self.assertEqual(schema["schemas"], [{"type": "integer", "maximum": 8, "title": "精确配置名称"},
            {"type": "integer", "minimum": 1}])

    def test_suggestions_follow_target_mapping_not_input_names_and_preserve_definition(self):
        from studio.services.execution_input_catalog import execution_input_form_options
        definition = deepcopy(self.definition.value)
        definition["configuration"]["inputs"][0]["key"] = "custom_story"
        forms = execution_input_form_options(definition)
        self.assertEqual(forms["custom_story"]["suggestions"][0]["key"], "project.logline")
        self.assertIn("example:0", [row["key"] for row in forms["custom_story"]["suggestions"]])
        definition["interface"]["input_targets"]["idea"]["source_kinds"] = ["context_field"]
        self.assertEqual(execution_input_form_options(definition)["custom_story"]["suggestions"], [])
        self.assertEqual(build_execution_definition(self.version.pk).sha256, self.definition.sha256)

    def test_preview_api_uses_saved_project_content_and_reports_missing_or_foreign_sources(self):
        from django.contrib.auth import get_user_model
        from studio.models import SeriesProject, Style
        style = Style.objects.create(name="Campus", slug="campus", visual_prompt="Natural light.", description="NOT PROMPT",
            negative_prompt="NOT VISUAL STYLE")
        project = SeriesProject.objects.create(owner=self.actor, title="Project", logline="A missing letter.", style=style,
            language="zh", aspect_ratio="9:16", width=720, height=1280, fps=24, default_shot_duration_ms=2000, continuity_seed=1)
        api = APIClient(); api.force_authenticate(self.actor)
        url = f"/api/v1/model-execution-instance-versions/{self.version.pk}/input-suggestion/"
        params = {"project_id": str(project.pk), "input_key": "idea", "suggestion_key": "project.logline"}
        response = api.get(url, params)
        self.assertEqual(response.status_code, 200, response.data)
        self.assertEqual(response.data["content"], project.logline)
        self.assertEqual(response.data["definition_sha256"], self.definition.sha256)
        response = api.get(url, {**params, "input_key": "style_hint", "suggestion_key": "project.visual_style"})
        self.assertEqual(response.status_code, 200, response.data)
        self.assertEqual(response.data["content"], style.visual_prompt)
        self.assertNotIn("NOT", response.data["content"])
        project.logline = ""; project.save()
        self.assertEqual(api.get(url, params).status_code, 400)
        other = get_user_model().objects.create_user(username="suggestion-other")
        project.owner = other; project.save()
        self.assertEqual(api.get(url, params).status_code, 400)
        self.assertEqual(api.get(url, {"input_key": "idea", "suggestion_key": "project.logline"}).status_code, 400)
        self.assertEqual(api.get(url, {"input_key": "episode_count", "suggestion_key": "example:0"}).status_code, 400)
        api.force_authenticate(user=None)
        self.assertIn(api.get(url, params).status_code, (401, 403))

    def test_project_reference_freezes_preview_and_actual_edited_text_and_rejects_mismatch(self):
        from studio.models import SeriesProject
        from studio.services.execution_definitions import read_execution_input_contract
        from studio.services.execution_input_suggestions import preview_execution_input_suggestion
        from studio.services.execution_input_resolution import resolve_execution_input_sources
        project = SeriesProject.objects.create(owner=self.actor, title="Project", logline="Saved original story.",
            language="zh", aspect_ratio="9:16", width=720, height=1280, fps=24, default_shot_duration_ms=2000, continuity_seed=1)
        preview = preview_execution_input_suggestion(read_execution_input_contract(self.version.pk), owner=self.actor,
            project_id=project.pk, input_key="idea", suggestion_key="project.logline")
        sources = deepcopy(self.fixture["inputs"])
        sources["idea"] = {"kind": "literal", "value": preview["content"] + " End with a mystery.", "reference": preview["reference"]}
        project.logline = "Changed after preview."; project.save()
        def resolve(values, **kwargs):
            return resolve_execution_input_sources(self.definition, values, owner=self.actor, subject_facts=[],
                max_media_bytes=65536, project_id=kwargs.get("project_id", project.pk)).value
        result = resolve(sources)
        self.assertEqual(result["operation_inputs"]["idea"], sources["idea"]["value"])
        reference = next(row for row in result["input_provenance"] if row["input_key"] == "idea")["reference"]
        self.assertEqual(reference["content"], "Saved original story.")
        self.assertTrue(reference["edited"])
        for bad in ({**sources, "idea": {**sources["idea"], "reference": preview["reference"] + "tampered"}},
                    {**sources, "style_hint": sources["idea"]}):
            with self.assertRaises(ValidationError): resolve(bad)
        with self.assertRaises(ValidationError): resolve(sources, project_id=None)
        self.assertNotIn("reference", resolve(self.fixture["inputs"])["input_provenance"][0])

    def test_example_is_only_used_after_explicit_submission_and_saved_in_preparation(self):
        from studio.services.execution_definitions import read_execution_input_contract
        from studio.services.execution_input_suggestions import preview_execution_input_suggestion
        from studio.services.execution_preparations import read_execution_preparation_detail
        preview = preview_execution_input_suggestion(read_execution_input_contract(self.version.pk), owner=self.actor,
            project_id=None, input_key="idea", suggestion_key="example:0")
        self.fixture["inputs"]["idea"] = {"kind": "literal", "value": preview["content"], "reference": preview["reference"]}
        attempt, prep, _ = self.smoke()
        detail = read_execution_preparation_detail(prep)
        origin = next(row for row in detail.resolved_inputs["inputs"]["input_provenance"] if row["input_key"] == "idea")
        self.assertEqual(origin["reference"]["content"], preview["content"])
        self.assertEqual(origin["reference"]["selector"], "example:0")
        self.assertFalse(origin["reference"]["edited"])

    def test_resource_native_smoke_uses_real_admission_and_original_worker(self):
        from studio.services.execution_instances import ExecutionResourceSelection
        from studio.services.execution_resource_smoke_definition import build_resource_smoke_definition
        from studio.services.execution_smoke import create_resource_smoke
        selection = ExecutionResourceSelection.from_version(self.version)
        definition = build_resource_smoke_definition(selection)
        fixture = deepcopy(self.fixture)
        fixture["inputs"] = {row["key"]: self.fixture["inputs"][row["targets"][0]["target_key"]]
            for row in definition.value["configuration"]["inputs"]}
        attempt, prep, _ = create_resource_smoke(selection=selection,
            expected_definition_sha256=definition.sha256, fixture=fixture, field_values=[],
            client_idempotency_key="native-resource-worker", allow_external_calls=True, budget_limit=None, actor=self.actor)
        ready = advance_execution_preparation(preparation_id=prep.pk,
            expected_preparation_revision=prep.revision, actor=self.actor)
        self.assertEqual(ready.status, "dispatched", ready.reason)
        peer = Mock()
        peer.invoke.return_value = {"text": json.dumps(self.candidate, ensure_ascii=False), "request_id": "isolated-resource-native"}
        claim = claim_next_deployment_smoke("native-resource-worker")
        self.assertEqual(claim.pk, attempt.pk)
        with patch("studio.services.execution_smoke_worker.driver_for", return_value=peer):
            result = process_deployment_smoke(claim)
        self.assertEqual(result.status, "succeeded", result.error_code)
        self.assertEqual(result.result["scope"], "resource_smoke")
        self.assertIsNone(result.execution_instance_version_id)
        self.assertEqual(ModelDeploymentCapabilitySnapshot.objects.count(), 1)
        self.assertFalse(Asset.objects.exists())

    def test_original_smoke_validates_native_schema_instead_of_plain_text(self):
        self.assertEqual(self.definition.value["interface"]["output"]["kind"], "script")
        self.assertEqual(self.definition.value["interface"]["business_output"]["parser"]["key"], "script_semantics.v1")
        self.assertEqual(self.definition.value["interface"]["parameters"]["max_tokens"]["layers"][0]["source"], "system.execution.resources.review_policy")
        attempt, prep, _ = self.smoke()
        advanced = advance_execution_preparation(preparation_id=prep.pk, expected_preparation_revision=prep.revision, actor=self.actor)
        self.assertEqual(advanced.status, "dispatched", advanced.reason)
        prep.refresh_from_db()
        transport = prep.contract.prepared_request["transport"]
        self.assertEqual(transport["operation_plan"]["plan"]["parameters"]["seed"], 42)
        self.assertEqual(transport["payload"]["seed"], 42)
        from studio.task_center_system_message import system_message_trace
        self.assertEqual(system_message_trace(transport, self.definition.value)["state"], "matched")
        self.assertNotIn("system_message_layout", transport["payload"])
        from studio.task_center_native_inputs import native_input_trace
        trace = native_input_trace(transport, self.definition.value)
        self.assertEqual(trace["unmapped_count"], 0)
        self.assertEqual(trace["state"], "matched")
        self.assertTrue(all(row["state"] == "matched" for row in trace["rows"]))
        peer = Mock()
        peer.invoke.return_value = {"text": json.dumps(self.candidate, ensure_ascii=False), "request_id": "isolated-native-smoke"}
        claim = claim_next_deployment_smoke("native-smoke-worker")
        with patch("studio.services.execution_smoke_worker.driver_for", return_value=peer):
            result = process_deployment_smoke(claim)
        self.assertEqual(result.status, "succeeded", result.error_code)
        self.assertTrue(result.result["evidence"]["result"]["valid"])
        self.assertFalse(ModelDeploymentCapabilitySnapshot.objects.exists())
        self.assertFalse(Asset.objects.exists())
        for text in ("just some prose", '{"schema_version":2,"schema_version":2}', json.dumps({**self.candidate, "episodes": "not an array"})):
            with self.subTest(text=text), self.assertRaises(ValidationError):
                parse_execution_text_response("script.generate", {"text": text}, operation_plan=transport["operation_plan"],
                    request_payload=transport["payload"], expected_output=self.definition.value["interface"]["business_output"])

    def test_large_script_smoke_keeps_bounded_evidence_and_exact_original_response(self):
        from hashlib import sha256
        from studio.services.execution_smoke_responses import read_smoke_response
        from studio.services.json_contracts import canonical_json_bytes, DATABASE_JSON_FIELD_MAX_BYTES
        from studio.task_center_details import inspect_output
        candidate = deepcopy(self.candidate)
        shot = candidate["episodes"][0]["shots"][0]
        candidate["episodes"][0]["shots"] = [{**deepcopy(shot), "shot": index + 1,
            "action": "旅人凝望窗外，雨水沿着玻璃流淌。" * 12} for index in range(24)]
        text = json.dumps(candidate, ensure_ascii=False)
        self.fixture["inputs"]["shots_per_episode"]["value"] = 24
        _, prep, _ = self.smoke()
        advanced = advance_execution_preparation(preparation_id=prep.pk,
            expected_preparation_revision=prep.revision, actor=self.actor)
        self.assertEqual(advanced.status, "dispatched", advanced.reason)
        prep.refresh_from_db()
        transport = prep.contract.prepared_request["transport"]
        parsed = parse_execution_text_response("script.generate", {"text": text},
            operation_plan=transport["operation_plan"], request_payload=transport["payload"],
            expected_output=self.definition.value["interface"]["business_output"])
        full_report = canonical_json_bytes(parsed["result"])
        self.assertGreater(len(full_report), DATABASE_JSON_FIELD_MAX_BYTES)
        peer = Mock()
        peer.invoke.return_value = {"text": text, "request_id": "isolated-large-script"}
        with patch("studio.services.execution_smoke_worker.driver_for", return_value=peer):
            done = process_deployment_smoke(claim_next_deployment_smoke("large-script-worker"))
        peer.invoke.assert_called_once()
        self.assertEqual(done.status, "succeeded", done.error_code)
        self.assertLess(done.result_size_bytes, DATABASE_JSON_FIELD_MAX_BYTES)
        summary = done.result["evidence"]["result"]
        self.assertEqual(summary["representation"], "script_validation_summary")
        self.assertEqual(summary["report_sha256"], sha256(full_report).hexdigest())
        self.assertEqual(summary["report_size_bytes"], len(full_report))
        self.assertEqual(summary["warning_count"], len(parsed["result"]["warnings"]))
        self.assertEqual(sum(row["count"] for row in summary["warnings"]), summary["warning_count"])
        self.assertTrue(summary["valid"])
        for key in ("source_fields", "raw_value", "source_value", "value", "provenance"):
            self.assertNotIn(key, summary)
        self.assertEqual(read_smoke_response(done, done.result["evidence"]["response_body"]), text)
        output, _ = inspect_output(done, "deployment_smoke", {}, None)
        self.assertEqual(output["text"], text)
        self.assertFalse(Asset.objects.exists())
        self.assertFalse(ModelDeploymentCapabilitySnapshot.objects.exists())

    def test_original_asset_worker_preserves_validated_structured_json(self):
        self.enterContext(patch("studio.services.execution_asset_dispatch.collect_asset_execution_admission", side_effect=isolated_observations))
        job, _ = create_asset_generation_job(owner=self.actor, client_idempotency_key="native-asset", job_type="asset", media_type="script",
            execution_instance_version_id=self.version.pk, expected_definition_sha256=self.definition.sha256,
            name="Script candidate", field_values=[], **self.fixture)
        prep = freeze(job, self.actor)
        job.status = "queued"
        job.save(update_fields=["status"])
        peer = Mock()
        peer.invoke.return_value = {"text": json.dumps(self.candidate, ensure_ascii=False), "request_id": "isolated-native-asset"}
        claim = claim_next_text_asset_job("native-asset-worker")
        self.assertIsNotNone(claim)
        with patch("studio.services.asset_generation.driver_for", return_value=peer):
            result = process_text_asset_job(claim)
        self.assertEqual(result.status, "succeeded", result.error_message)
        self.assertEqual(result.output_asset.kind, "script")
        with result.output_asset.file.open("rb") as artifact:
            text = artifact.read().decode("utf-8")
        self.assertEqual(json.loads(text), self.candidate)
        self.assertEqual(result.output_asset.prompt_hint, self.fixture["manual_channels"]["positive_prompt"])
        self.assertEqual(peer.invoke.call_args.args[2], prep.contract.prepared_request["transport"]["payload"])
        asset = result.output_asset
        self.assertEqual(asset.metadata["generation_job_id"], str(result.pk))
        self.assertEqual(asset.metadata["preparation_id"], str(prep.pk))
        client = APIClient()
        client.force_authenticate(self.actor)
        detail = client.get(f"/api/v1/assets/{asset.pk}/")
        self.assertEqual(detail.status_code, 200, detail.data)
        download = client.get(detail.data["download_url"])
        self.assertEqual(download.status_code, 200)
        try:
            self.assertEqual(b"".join(download.streaming_content).decode("utf-8"), text)
        finally:
            download.close()
        for change in ({"source_info": "伪造来源"}, {"prompt_hint": "覆盖冻结提示词"}):
            with self.subTest(change=change):
                response = client.patch(f"/api/v1/assets/{asset.pk}/", change, format="json")
                self.assertEqual(response.status_code, 400, response.data)
        asset_path = Path(asset.file.path).resolve()
        self.assertTrue(asset_path.is_relative_to(Path(self.media_root).resolve()))
        asset_path.write_bytes(b"tampered isolated generated script")
        self.assertEqual(client.get(detail.data["preview_url"]).status_code, 404)
        from studio.models import SeriesProject
        from studio.tests.test_execution_context_inputs import PROJECT_CONFIG
        project = SeriesProject.objects.create(owner=self.actor, title="Script integrity", **PROJECT_CONFIG)
        imported = client.post("/api/v1/script-sources/", {"project_id": str(project.pk), "asset_id": str(asset.pk)}, format="json")
        self.assertEqual(imported.status_code, 422, imported.data)
        self.assertEqual(imported.data["error"]["code"], "script_asset_integrity_mismatch")
        deleted = client.delete(f"/api/v1/assets/{asset.pk}/")
        self.assertEqual(deleted.status_code, 400, deleted.data)
        self.assertTrue(Asset.objects.filter(pk=asset.pk).exists())

    def test_bad_input_is_rejected_without_creating_smoke(self):
        for key, value in (("episode_count", True), ("shots_per_episode", -1), ("idea", 2), ("idea", None)):
            original = deepcopy(self.fixture)
            self.fixture["inputs"][key]["value"] = value
            with self.subTest(key=key), self.assertRaises(ValidationError):
                self.smoke()
            self.fixture = original
        self.assertFalse(self.deployment.smoke_attempts.exists())

    def test_profile_channels_use_exact_published_strategy_not_binding(self):
        profile = PromptProfileVersion.objects.create(profile_key="native-script-policy", version=1, name="Native policy",
            model_family="text-protocol", operation="script.generate", mode="", review_state="ready", contract_schema_version=8)
        field = create_field_definition(key="native_script_instruction", label="Instructions", value_type="long_text")
        create_profile_field_rule(fixed_value="Only describe the supplied facts.", profile_version=profile, field_definition=field, source_selector="fixed.value",
            generation_source_selector="fixed.value", generation_mode="server_locked", channel="positive_prompt", position=1, required=True)
        refresh_prompt_profile_contract(profile)
        profile = publish_prompt_profile(profile, actor=self.actor)
        instance = create_execution_instance(key="native-script-profile", name="Profile script", description="", actor=self.actor)
        version = create_execution_draft(instance_id=instance.pk, expected_revision=instance.revision, actor=self.actor,
            selection={"deployment": self.deployment, "operation": "script.generate", "mode": "", "prompt_mode": "profile",
                "prompt_profile_version": profile, "workflow_version": None, "output_version": self.output_version})
        version = update_execution_draft(version_id=version.pk, expected_revision=version.revision, actor=self.actor,
            changes={"inputs": deepcopy(self.definition.value["configuration"]["inputs"])})
        self.version = version
        self.definition = build_execution_definition(version.pk)
        publish_execution_version(version_id=version.pk, expected_revision=version.revision,
            expected_definition_sha256=self.definition.sha256, actor=self.actor)
        self.fixture["manual_channels"] = {}
        self.fixture["source_language"] = self.fixture["target_language"] = "English"
        _, prep, _ = self.smoke()
        preview = compile_prepared_execution_transport(preparation_id=prep.pk, expected_preparation_revision=prep.revision, actor=self.actor)
        transport = preview.transport.value
        self.assertIn("Only describe the supplied facts.", transport["payload"]["messages"][1]["content"])
        self.assertEqual(transport["profile_compilation"]["assembly"]["prompt_profile_version_id"], str(profile.pk))
        self.assertEqual(transport["operation_plan"]["plan"]["inputs"]["idea"], self.fixture["inputs"]["idea"]["value"])
        from studio.task_center_prompt_trace import prompt_trace
        trace = prompt_trace(transport, self.definition.value, {})
        self.assertEqual(trace["state"], "recorded")
        self.assertTrue(trace["channels"])
        self.assertTrue(all(row["state"] == "matched" for row in trace["channels"]))

    def test_english_idea_and_style_preserve_text_with_exact_language_form(self):
        from studio.services.execution_definitions import read_execution_input_contract
        from studio.services.execution_input_catalog import execution_language_form_options
        from studio.execution_instance_serializers import ExecutionInputContractSerializer

        profile = PromptProfileVersion.objects.create(profile_key="english-input-policy", version=1, name="Input language",
            model_family="text-protocol", operation="script.generate", mode="", review_state="ready", contract_schema_version=8)
        field = create_field_definition(key="english_idea", label="创意", value_type="long_text")
        create_profile_field_rule(profile_version=profile, field_definition=field, source_selector="request.theme",
            generation_source_selector="request.theme", generation_mode="passthrough", language_policy="preserve_source",
            channel="positive_prompt", position=1, required=True)
        refresh_prompt_profile_contract(profile)
        profile = publish_prompt_profile(profile, actor=self.actor)
        instance = create_execution_instance(key="english-inputs", name="English inputs", description="", actor=self.actor)
        version = create_execution_draft(instance_id=instance.pk, expected_revision=instance.revision, actor=self.actor,
            selection={"deployment": self.deployment, "operation": "script.generate", "mode": "", "prompt_mode": "profile",
                "prompt_profile_version": profile, "workflow_version": None, "output_version": self.output_version})
        inputs = deepcopy(self.definition.value["configuration"]["inputs"])
        inputs[0]["key"] = "custom_creative"
        inputs[0]["targets"].append({"target_kind": "profile_input", "target_key": "request.theme", "is_primary": False})
        version = update_execution_draft(version_id=version.pk, expected_revision=version.revision, actor=self.actor, changes={"inputs": inputs})
        self.version = version
        self.definition = build_execution_definition(version.pk)
        publish_execution_version(version_id=version.pk, expected_revision=version.revision,
            expected_definition_sha256=self.definition.sha256, actor=self.actor)
        before = deepcopy(self.definition.value)
        contract = ExecutionInputContractSerializer(read_execution_input_contract(version.pk)).data
        language = contract["language_form"]
        self.assertEqual(language["source"], {"required": True, "fields": ["本轮创意"]})
        self.assertEqual(language["target"], {"required": False, "fields": []})
        self.assertIn({"value": "en", "label": "英文 / English"}, language["choices"])
        self.assertEqual(contract["sha256"], self.definition.sha256)
        self.fixture["inputs"].pop("idea")
        self.fixture["inputs"]["custom_creative"] = {"kind": "literal", "value": "A student uncovers a secret."}
        self.fixture["inputs"]["style_hint"]["value"] = "Quiet cinematic suspense."
        self.fixture.update(manual_channels={}, source_language="en", target_language="")
        _, prep, _ = self.smoke()
        transport = compile_prepared_execution_transport(preparation_id=prep.pk,
            expected_preparation_revision=prep.revision, actor=self.actor).transport.value
        request = transport["operation_plan"]["plan"]["inputs"]
        self.assertEqual(request["idea"], self.fixture["inputs"]["custom_creative"]["value"])
        self.assertEqual(request["style_hint"], self.fixture["inputs"]["style_hint"]["value"])
        self.assertEqual(before, self.definition.value)
        changed = deepcopy(before)
        changed["interface"]["prompt"]["profile_sha256"] = "0" * 64
        with self.assertRaisesMessage(ValidationError, "语言说明与实例的精确策略不一致"):
            execution_language_form_options(changed)

    def test_language_form_separates_optional_inherited_fixed_and_unused_policies(self):
        from studio.services.execution_input_catalog import execution_language_form_options
        from studio.services.prompt_profiles import prompt_language_options
        from studio.execution_smoke_serializers import ResourceSmokeContractSerializer

        none = execution_language_form_options(self.definition.value)
        self.assertEqual(none["source"], {"required": False, "fields": []})
        self.assertEqual(none["target"], {"required": False, "fields": []})
        self.assertEqual(none["choices"], prompt_language_options())
        profile = PromptProfileVersion.objects.create(profile_key="language-metadata", version=1, name="Language metadata",
            model_family="text-protocol", operation="script.generate", mode="", review_state="ready", contract_schema_version=8)
        for index, policy in enumerate(("preserve_source", "inherit", "en", "any")):
            field = create_field_definition(key=f"language_meta_{index}", label=f"语言字段{index}",
                value_type="long_text")
            create_profile_field_rule(fixed_value="A source sentence.", profile_version=profile, field_definition=field, source_selector="fixed.value",
                generation_source_selector="fixed.value", generation_mode="server_locked", language_policy=policy,
                generation_required=index == 1, channel="positive_prompt", position=index, required=index == 1)
        refresh_prompt_profile_contract(profile)
        profile = publish_prompt_profile(profile, actor=self.actor)
        definition = deepcopy(self.definition.value)
        definition["interface"]["prompt"] = {"mode": "profile", "profile_id": str(profile.pk), "profile_sha256": profile.contract_sha256}
        result = execution_language_form_options(definition)
        self.assertEqual(result["source"], {"required": False, "fields": ["语言字段0"]})
        self.assertEqual(result["target"], {"required": True, "fields": ["语言字段1"]})
        self.assertEqual(result["fixed_fields"], [{"label": "语言字段2", "language": "英文 / English"}])
        envelope = {"language_form": result, "scope": "resource_smoke", "version_id": None,
            "schema_version": self.definition.schema_version, "size_bytes": self.definition.size_bytes,
            "sha256": self.definition.sha256, "target_fingerprint": definition["target_fingerprint"],
            "definition": definition, "context_fields": [], "input_forms": {}}
        self.assertEqual(ResourceSmokeContractSerializer(envelope).data["language_form"], result)

    def structure_instance(self, scopes=("request",), *, planning=False):
        model = ModelProfileVersion(profile_key="native-structure", version=1, model_identifier="structure-model",
            provider=self.deployment.provider, modality="text", status="published")
        assign_json_contract(model, "specification", {"schema_version": 1, "capabilities": ["script.structure"],
            "script_structure": {"timeout_seconds": 30, "chat_request_options": {}}})
        model.save()
        deployment = ModelDeployment(key="native-structure", name="Structure", profile_version=model,
            credential=self.deployment.credential)
        assign_json_contract(deployment, "runtime_config", {})
        deployment.save()
        instance = create_execution_instance(key="native-structure", name="Structure", description="", actor=self.actor)
        version = create_execution_draft(instance_id=instance.pk, expected_revision=instance.revision, actor=self.actor,
            selection={"deployment": deployment, "operation": "script.structure", "mode": "", "prompt_mode": "manual",
                "prompt_profile_version": None, "workflow_version": None, "output_version": self.output_version})
        source = deepcopy(self.definition.value["configuration"]["inputs"][0])
        source.update(key="script", position=0, targets=[{"target_kind": "operation_input", "target_key": "source_text", "is_primary": True}])
        source["semantic_contract"]["allowed_scope_kinds"] = list(scopes)
        inputs = [source]
        if planning:
            inputs.append({**deepcopy(source), "key": "plan", "position": 1, "kind": "json",
                "targets": [{"target_kind": "operation_input", "target_key": "planning", "is_primary": True}]})
        self.version = update_execution_draft(version_id=version.pk, expected_revision=version.revision, actor=self.actor, changes={"inputs": inputs})
        self.definition = build_execution_definition(version.pk)
        publish_execution_version(version_id=version.pk, expected_revision=self.version.revision,
            expected_definition_sha256=self.definition.sha256, actor=self.actor)
        self.fixture["inputs"] = {"script": {"kind": "literal", "value": "旅人来到房间。\n旅人放下盒子。"}}
        self.fixture["manual_channels"] = {"positive_prompt": "完整保留所有源行。"}

    def test_structure_planning_is_optional_but_an_explicit_mapping_is_closed_and_frozen(self):
        from studio.services.execution_native_text import validate_native_text_inputs
        self.structure_instance(planning=True)
        target = self.definition.value["interface"]["input_targets"]["planning"]
        self.assertFalse(target["required"])
        self.assertEqual(target["value_schema"]["additionalProperties"], False)
        planning = {"episode_count": 1, "shots_per_episode": 1, "episode_duration_seconds": 2}
        self.fixture["inputs"]["plan"] = {"kind": "literal", "value": planning}
        _, prep, _ = self.smoke()
        transport = compile_prepared_execution_transport(preparation_id=prep.pk,
            expected_preparation_revision=prep.revision, actor=self.actor).transport.value
        self.assertEqual(transport["operation_plan"]["plan"]["inputs"]["planning"], planning)
        self.assertEqual(json.loads(transport["payload"]["messages"][1]["content"])["inputs"]["planning"], planning)
        from studio.task_center_system_message import system_message_trace
        self.assertEqual(system_message_trace(transport, self.definition.value)["state"], "matched")
        self.assertIn("semantic_mapping", [row["key"] for row in transport["operation_plan"]["plan"]["system_message_layout"]["sections"]])
        from studio.task_center_native_inputs import native_input_trace
        trace = native_input_trace(transport, self.definition.value)
        self.assertEqual(trace["unmapped_count"], 0)
        self.assertEqual(trace["state"], "matched")
        self.assertTrue(all(row["state"] == "matched" for row in trace["rows"]))
        for value in (None, {}, {**planning, "shots_per_episode": True}, {**planning, "other": 1}):
            with self.subTest(value=value), self.assertRaises(ValidationError):
                validate_native_text_inputs("script.structure", {"source_text": "原文", "planning": value})
        validate_native_text_inputs("script.structure", {"source_text": "原文"})

    def test_structure_smoke_keeps_literal_source_and_requires_exact_output(self):
        self.structure_instance()
        attempt, prep, _ = self.smoke()
        advanced = advance_execution_preparation(preparation_id=prep.pk, expected_preparation_revision=prep.revision, actor=self.actor)
        self.assertEqual(advanced.status, "dispatched", advanced.reason)
        prep.refresh_from_db()
        transport = prep.contract.prepared_request["transport"]
        source = transport["operation_plan"]["plan"]["source_revision"]
        self.assertIsNone(source["id"])
        self.assertIsNone(source["revision"])
        self.assertEqual(transport["operation_plan"]["plan"]["source_kind"], "literal")
        response = deepcopy(self.candidate)
        peer = Mock()
        peer.invoke.return_value = {"text": json.dumps(response, ensure_ascii=False), "request_id": "isolated-structure"}
        claim = claim_next_deployment_smoke("native-structure-worker")
        with patch("studio.services.execution_smoke_worker.driver_for", return_value=peer):
            result = process_deployment_smoke(claim)
        self.assertEqual(result.status, "succeeded", result.error_code)
        self.assertTrue(result.result["evidence"]["result"]["valid"])
        for expected in (None, {}):
            with self.subTest(expected=expected), self.assertRaises(ValidationError):
                parse_execution_text_response("script.structure", {"text": json.dumps(response)},
                    operation_plan=transport["operation_plan"], request_payload=transport["payload"], expected_output=expected)

    def test_confirmed_script_flows_through_preparation_and_original_worker_without_source_requery(self):
        from studio.models import SeriesProject
        from studio.services.script_sources import create_pasted_script_source, confirm_script_revision, create_script_revision
        from studio.tests.test_execution_context_inputs import PROJECT_CONFIG
        self.structure_instance(scopes=("request", "project"))
        project = SeriesProject.objects.create(owner=self.actor, title="Real test project", **PROJECT_CONFIG)
        source, revision = create_pasted_script_source(project=project, user=self.actor, content="旅人来到房间。\n旅人放下盒子。")
        revision = confirm_script_revision(revision, user=self.actor)
        self.fixture["inputs"] = {"script": {"kind": "context_field", "selector_key": "script.content",
            "scope": {"kind": "project", "id": str(project.pk)}, "content_revision_id": str(revision.pk)}}
        _, prep, _ = self.smoke()
        later = create_script_revision(source=source, user=self.actor, content="Later reviewed content")
        confirm_script_revision(later, user=self.actor)
        result = advance_execution_preparation(preparation_id=prep.pk, expected_preparation_revision=prep.revision, actor=self.actor)
        self.assertEqual(result.status, "dispatched", result.reason)
        prep.refresh_from_db()
        transport = prep.contract.prepared_request["transport"]
        self.assertEqual(transport["operation_plan"]["plan"]["source_revision"]["id"], str(revision.pk))
        self.assertEqual(transport["operation_plan"]["plan"]["inputs"]["source_text"], revision.content)
        payload = deepcopy(self.candidate)
        peer = Mock()
        peer.invoke.return_value = {"text": json.dumps(payload, ensure_ascii=False), "request_id": "isolated-context-structure"}
        with patch("studio.services.execution_smoke_worker.driver_for", return_value=peer):
            result = process_deployment_smoke(claim_next_deployment_smoke("context-structure-worker"))
        self.assertEqual(result.status, "succeeded", result.error_code)
        self.assertTrue(result.result["evidence"]["result"]["valid"])
        prep.refresh_from_db()
        self.assertEqual(prep.contract.prepared_request["transport"]["operation_plan"]["plan"]["source_revision"]["id"], str(revision.pk))

    def test_visual_rules_project_previews_freeze_through_saved_preparation_and_transport(self):
        from studio.models import SeriesProject, Style
        from studio.services.execution_definitions import read_execution_input_contract
        from studio.services.execution_input_suggestions import preview_execution_input_suggestion
        from studio.tests.visual_rules_fixtures import configure_visual_instance
        configure_visual_instance(self, project_sources=True)
        style = Style.objects.create(name="Project style", slug="project-style", visual_prompt="Natural light.")
        project = SeriesProject.objects.create(owner=self.actor, title="Project", logline="A letter is missing.", style=style,
            master_script="NEVER APPEND WHOLE SCRIPT " * 10000, language="zh", aspect_ratio="9:16", width=720, height=1280,
            fps=24, default_shot_duration_ms=2000, continuity_seed=1)
        contract = read_execution_input_contract(self.version.pk)
        self.fixture["inputs"] = {key: {"kind": "project_snapshot", "reference": preview_execution_input_suggestion(contract,
            owner=self.actor, project_id=project.pk, input_key=key, suggestion_key=key)["reference"]}
            for key in ("project.logline", "style.visual_prompt")}
        _, prep, _ = self.smoke()
        project.logline = "CHANGED AFTER SAVE"; project.save()
        style.visual_prompt = "CHANGED STYLE"; style.save()
        advanced = advance_execution_preparation(preparation_id=prep.pk, expected_preparation_revision=prep.revision, actor=self.actor)
        self.assertEqual(advanced.status, "dispatched", advanced.reason)
        prep.refresh_from_db()
        user = prep.contract.prepared_request["transport"]["payload"]["messages"][1]["content"]
        self.assertIn("A letter is missing.", user)
        self.assertIn("Natural light.", user)
        self.assertNotIn("CHANGED", user)
        self.assertNotIn("NEVER APPEND", user)
        origins = prep.contract.resolved_inputs["inputs"]["input_provenance"]
        self.assertEqual({row["kind"] for row in origins}, {"project_snapshot"})
        self.assertEqual({row["scope"]["id"] for row in origins}, {str(project.pk)})

    def test_visual_rules_use_only_configured_profile_inputs_without_script_or_bible(self):
        from studio.tests.visual_rules_fixtures import configure_visual_instance
        from studio.services.execution_input_catalog import execution_input_form_options
        from studio.tests.test_production_visual_rules import ProductionVisualRulesContractTests
        configure_visual_instance(self)
        self.assertEqual(self.definition.value["interface"]["input_targets"], {})
        forms = execution_input_form_options(self.definition.value)
        self.assertEqual(forms["request.theme"]["suggestions"][0]["key"], "project.logline")
        self.assertEqual(forms["request.style"]["suggestions"][0]["key"], "project.visual_style")
        _, prep, _ = self.smoke()
        advanced = advance_execution_preparation(preparation_id=prep.pk, expected_preparation_revision=prep.revision, actor=self.actor)
        self.assertEqual(advanced.status, "dispatched", advanced.reason)
        prep.refresh_from_db()
        transport = prep.contract.prepared_request["transport"]
        self.plan = transport["operation_plan"]["plan"]
        self.assertIsNone(self.plan["input_baseline"]["project_id"])
        user = json.loads(transport["payload"]["messages"][1]["content"])
        self.assertEqual(set(user), {"prompt_profile"})
        self.assertIn("Restrained cool realism.", user["prompt_profile"]["positive_prompt"])
        self.assertNotIn("structured_fields", user["prompt_profile"])
        self.assertTrue(self.plan["prompt_profile_assembly"]["structured_fields"])
        frozen_request = json.dumps(prep.contract.prepared_request, sort_keys=True)
        self.assertNotIn("source_text", user)
        candidate = ProductionVisualRulesContractTests._payload(self)
        peer = Mock()
        peer.invoke.return_value = {"text": json.dumps(candidate), "request_id": "isolated-configured-visual"}
        with patch("studio.services.execution_smoke_worker.driver_for", return_value=peer):
            result = process_deployment_smoke(claim_next_deployment_smoke("configured-visual-worker"))
        self.assertEqual(result.status, "succeeded", result.error_code)
        prep.contract.refresh_from_db()
        self.assertEqual(json.dumps(prep.contract.prepared_request, sort_keys=True), frozen_request)
        candidate["source_revision_id"] = "invented-source"
        with self.assertRaises(ValidationError):
            parse_execution_text_response("visual_rules.generate", {"text": json.dumps(candidate)}, operation_plan=transport["operation_plan"])

    def test_large_visual_plan_uses_document_limit_and_resumes_without_losing_input(self):
        from studio.tests.visual_rules_fixtures import configure_visual_instance
        from studio.services.execution_preparations import read_frozen_execution_request
        from studio.services.json_contracts import canonical_json_bytes
        configure_visual_instance(self, max_input_bytes=262144, field_max_chars=25000)
        original = "雨夜。" * 6000
        self.fixture["inputs"]["request.theme"]["value"] = original
        self.enterContext(patch("studio.services.prompt_profiles._prompt_profile_preview_max_bytes", return_value=262144))
        self.enterContext(patch("studio.services.prompt_profile_compiler._prompt_profile_preview_max_bytes", return_value=262144))
        _, prep, _ = self.smoke()
        with patch("studio.services.execution_request_storage.boundary_int", return_value=65536):
            blocked = advance_execution_preparation(preparation_id=prep.pk,
                expected_preparation_revision=prep.revision, actor=self.actor)
        self.assertEqual((blocked.status, blocked.reason_code), ("blocked", "execution_request_invalid"))
        diagnostic = blocked.contract.evaluation["diagnostic"]
        self.assertEqual(diagnostic["part"], "native_plan")
        self.assertGreater(diagnostic["actual_bytes"], 65536)
        self.assertIn("65536", blocked.reason)
        self.assertNotIn(original, blocked.reason)
        self.assertIsNone(blocked.request_frozen_at)
        self.assertIsNone(blocked.deployment_smoke.submission_started_at)
        with patch("studio.services.execution_request_storage.boundary_int", return_value=8 * 1024 * 1024):
            compile_prepared_execution_transport(preparation_id=prep.pk,
                expected_preparation_revision=blocked.revision, actor=self.actor)
            resumed = advance_execution_preparation(preparation_id=prep.pk,
                expected_preparation_revision=blocked.revision, actor=self.actor, human_initiated=True)
            self.assertEqual(resumed.status, "dispatched", resumed.reason)
            _, _, frozen = read_frozen_execution_request(resumed)
            document = deepcopy(resumed.contract.prepared_request)
            again = advance_execution_preparation(preparation_id=prep.pk,
                expected_preparation_revision=resumed.revision, actor=self.actor, human_initiated=True)
            self.assertEqual(again.request_sha256, resumed.request_sha256)
            self.assertEqual(again.contract.prepared_request, document)
        transport = frozen.value
        self.assertGreater(len(canonical_json_bytes(transport["operation_plan"]["plan"])), 65536)
        user = json.loads(transport["payload"]["messages"][1]["content"])
        self.assertIn(original, user["prompt_profile"]["positive_prompt"])
        self.assertNotIn("structured_fields", user["prompt_profile"])
        self.assertEqual(self.version.preparations.count(), 1)

    def test_long_confirmed_script_plan_passes_early_and_final_request_boundaries(self):
        from studio.models import SeriesProject
        from studio.services.script_sources import create_pasted_script_source, confirm_script_revision
        from studio.tests.test_execution_context_inputs import PROJECT_CONFIG
        from studio.services.json_contracts import canonical_json_bytes
        self.structure_instance(scopes=("request", "project"))
        project = SeriesProject.objects.create(owner=self.actor, title="Long script", **PROJECT_CONFIG)
        original = "雨夜。" * 7000
        _, revision = create_pasted_script_source(project=project, user=self.actor, content=original)
        revision = confirm_script_revision(revision, user=self.actor)
        self.fixture["inputs"] = {"script": {"kind": "context_field", "selector_key": "script.content",
            "scope": {"kind": "project", "id": str(project.pk)}, "content_revision_id": str(revision.pk)}}
        _, prep, _ = self.smoke()
        preview = compile_prepared_execution_transport(preparation_id=prep.pk,
            expected_preparation_revision=prep.revision, actor=self.actor)
        plan = preview.transport.value["operation_plan"]["plan"]
        self.assertGreater(len(canonical_json_bytes(plan)), 65536)
        self.assertEqual(plan["inputs"]["source_text"], original)
        self.assertEqual(plan["source_revision"]["id"], str(revision.pk))

    def test_output_version_freezes_quality_and_remains_consumable_after_retirement(self):
        from studio.services.business_outputs import write_output
        from studio.services.visual_rules_output_contract import visual_rule_response_example
        from studio.tests.visual_rules_fixtures import configure_visual_instance
        quality = {"schema_version": 1, "continuity_summary_max_chars": 1}
        configure_visual_instance(self, quality=quality)
        frozen = self.definition.value["interface"]["business_output"]
        self.assertEqual(frozen["quality_advisories"], quality)
        self.assertEqual(self.definition.value["configuration"]["output_version_id"], frozen["id"])
        _, prep, _ = self.smoke()
        transport = compile_prepared_execution_transport(preparation_id=prep.pk,
            expected_preparation_revision=prep.revision, actor=self.actor).transport.value
        plan = transport["operation_plan"]["plan"]
        self.assertEqual(plan["business_output"], frozen)
        from studio.task_center_system_message import system_message_trace
        self.assertEqual(system_message_trace(transport, self.definition.value)["state"], "matched")
        self.assertIn("quality_advisories", [row["key"] for row in plan["system_message_layout"]["sections"]])
        from studio.task_center_native_inputs import native_input_trace
        trace = native_input_trace(transport, self.definition.value)
        # This profile declares only positive fields. The wire's empty negative
        # was protocol-supplied, not a value recorded in the frozen plan.
        self.assertNotIn("negative_prompt", plan["prompt_profile_assembly"]["channels"])
        self.assertEqual(trace["state"], "partial")
        self.assertEqual([row["state"] for row in trace["rows"]], ["matched", "unavailable"])
        self.assertEqual(len(trace["rows"]), 2)
        self.assertEqual(trace["unmapped_count"], 0)
        output = self.version.output_version
        write_output(actor=self.actor, output_id=output.pk, expected_revision=output.revision, action="retire")
        sample = visual_rule_response_example()
        result = parse_execution_text_response("visual_rules.generate", {"text": json.dumps(sample)}, operation_plan=transport["operation_plan"])
        self.assertEqual(result["result"]["business_output"], frozen)
        self.assertEqual(result["result"]["candidate"]["continuity_summary"], sample["continuity_summary"])
        self.assertTrue(result["result"]["warnings"])
        changed = deepcopy(transport["operation_plan"])
        changed["plan"]["resource_estimate"]["quality_advisories"] = {}
        with self.assertRaises(ValidationError):
            parse_execution_text_response("visual_rules.generate", {"text": json.dumps(sample)}, operation_plan=changed)

    def test_fresh_visual_definition_requires_explicit_output_and_nonvisual_rejects_it(self):
        from studio.services.execution_instances import clone_execution_draft, resolve_execution_resources
        from studio.tests.visual_rules_fixtures import configure_visual_instance
        script_version = self.version
        configure_visual_instance(self)
        script_version.output_version = self.version.output_version
        with self.assertRaises(ValidationError):
            resolve_execution_resources(script_version)
        draft = clone_execution_draft(source_id=self.version.pk, expected_revision=self.version.revision,
            expected_definition_sha256=self.definition.sha256, actor=self.actor)
        draft = update_execution_draft(version_id=draft.pk, expected_revision=draft.revision,
            changes={"output_version": None}, actor=self.actor)
        with self.assertRaises(ValidationError) as missing_output:
            build_execution_definition(draft.pk)
        self.assertEqual(missing_output.exception.message_dict,
            {"output_version": ["请选择此能力支持的确切已发布业务输出版本"]})

    def test_output_allowed_values_reach_request_and_actual_parser_without_repair(self):
        from studio.tests.visual_rules_fixtures import configure_visual_instance
        allowed = {"base_colors": ["navy"], "lighting": ["low_key"], "preserve_natural_skin_exposure": [False]}
        configure_visual_instance(self, allowed_values=allowed)
        frozen = self.definition.value["interface"]["business_output"]
        self.assertEqual(frozen["parser"]["allowed_values"], allowed)
        _, prep, _ = self.smoke()
        transport = compile_prepared_execution_transport(preparation_id=prep.pk,
            expected_preparation_revision=prep.revision, actor=self.actor).transport.value
        system = transport["payload"]["messages"][0]["content"]
        enums = json.loads(system.split("\nOUTPUT_ENUMS=")[1].split("\n")[0])
        self.assertEqual(enums["color_design.base_colors"]["allowed"], ["navy"])
        self.assertEqual(enums["color_design.lighting"], ["low_key"])
        self.assertEqual(enums["color_design.preserve_natural_skin_exposure"], [False])
        sample = deepcopy(frozen["parser"]["response_example"])
        result = parse_execution_text_response("visual_rules.generate", {"text": json.dumps(sample)}, operation_plan=transport["operation_plan"])
        self.assertEqual(result["result"]["business_output"], frozen)
        self.assertIn("navy", result["result"]["candidate"]["color_script"])
        self.assertEqual(result["result"]["candidate"]["continuity_summary"], sample["continuity_summary"])
        for key, invalid in (("base_colors", ["teal"]), ("lighting", "soft_diffuse"), ("preserve_natural_skin_exposure", True)):
            sample["color_design"] = deepcopy(frozen["parser"]["response_example"]["color_design"])
            sample["color_design"][key] = invalid
            before = deepcopy(sample)
            with self.subTest(key=key), self.assertRaisesMessage(ValidationError, "不符合此输出版本"):
                parse_execution_text_response("visual_rules.generate", {"text": json.dumps(sample)}, operation_plan=transport["operation_plan"])
            self.assertEqual(sample, before)

    def test_direct_color_mapping_reaches_actual_transport_and_parser(self):
        from studio.tests.visual_rules_fixtures import configure_visual_instance
        from studio.services.visual_rules_output_contract import normalize_visual_rule_field_sources
        sources = {**normalize_visual_rule_field_sources(), 'color_script': 'color_script'}
        configure_visual_instance(self, field_sources=sources, quality={'schema_version': 1, 'color_script_max_chars': 1})
        _, prep, _ = self.smoke()
        transport = compile_prepared_execution_transport(preparation_id=prep.pk,
            expected_preparation_revision=prep.revision, actor=self.actor).transport.value
        system = transport['payload']['messages'][0]['content']
        self.assertNotIn('OUTPUT_ENUMS', system)
        self.assertNotIn('color_design', system)
        sample = deepcopy(self.definition.value['interface']['business_output']['parser']['response_example'])
        sample['color_script'] = 'Complete English color description, retained despite the length recommendation.'
        result = parse_execution_text_response('visual_rules.generate', {'text': json.dumps(sample)}, operation_plan=transport['operation_plan'])['result']
        self.assertEqual(result['candidate']['color_script'], sample['color_script'])
        self.assertNotIn('color_design', result)
        self.assertTrue(result['warnings'])
        self.assertFalse(any(row['field']=='color_design.base_colors' for row in result['advisory_audit']))
        self.assertEqual(result['business_output'], self.definition.value['interface']['business_output'])

    def test_alternate_output_shapes_are_declared_in_transport_and_parse_without_guessing(self):
        from studio.tests.visual_rules_fixtures import configure_visual_instance
        sources = {'continuity_summary': 'continuity_points', 'color_script': 'color_script', 'negative_prompt': 'negative_prompt'}
        configure_visual_instance(self, field_sources=sources)
        _, prep, _ = self.smoke()
        transport = compile_prepared_execution_transport(preparation_id=prep.pk,
            expected_preparation_revision=prep.revision, actor=self.actor).transport.value
        system = transport['payload']['messages'][0]['content']
        self.assertIn('continuity_points MUST be a non-empty array', system)
        self.assertIn('negative_prompt MUST be a printable ASCII English string', system)
        self.assertIn('"separator":","', system)
        self.assertNotIn('negative_constraints MUST', system)
        self.assertNotIn('OUTPUT_ENUMS', system)
        sample = deepcopy(self.definition.value['interface']['business_output']['parser']['response_example'])
        result = parse_execution_text_response('visual_rules.generate', {'text': json.dumps(sample)}, operation_plan=transport['operation_plan'])['result']
        self.assertEqual(result['candidate']['continuity_summary'], ' '.join(sample['continuity_points']))
        self.assertEqual(result['candidate']['negative_prompt'], sample['negative_prompt'])
        self.assertEqual(result['business_output'], self.definition.value['interface']['business_output'])
        with self.assertRaises(ValidationError):
            parse_execution_text_response('visual_rules.generate', {'text': json.dumps({**sample, 'negative_constraints': []})}, operation_plan=transport['operation_plan'])
