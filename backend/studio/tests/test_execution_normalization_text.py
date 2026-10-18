from studio.tests.field_definition_fixtures import create_field_definition
"""Independent translation via original instance Smoke/asset queues, no peers."""
from copy import deepcopy
import json
from types import SimpleNamespace
from unittest.mock import Mock, patch

from django.core.exceptions import ValidationError
from django.db import connection
from django.test import TestCase, override_settings
from django.test.utils import CaptureQueriesContext

from studio.tests.field_definition_fixtures import create_profile_field_rule
from studio.models import ModelProfileVersion, ModelDeployment, FieldDefinition, PromptProfileFieldRule, PromptProfileVersion
from studio.services.asset_generation import create_asset_generation_job, claim_next_text_asset_job, process_text_asset_job
from studio.services.deployment_smoke_queue import claim_next_deployment_smoke, process_deployment_smoke
from studio.services.execution_advancement import advance_execution_preparation
from studio.services.execution_definitions import build_execution_definition
from studio.services.execution_instances import create_execution_instance, create_execution_draft, update_execution_draft, publish_execution_version
from studio.services.execution_normalization_text import normalization_request
from studio.services.execution_smoke import create_instance_smoke
from studio.services.execution_text_contracts import parse_execution_text_response
from studio.services.json_contracts import assign_json_contract
from studio.services.prompt_profiles import refresh_prompt_profile_contract, publish_prompt_profile
from studio.tests import test_execution_text_contracts as fixtures
from studio.tests.test_execution_asset_requests import freeze, isolated_observations


@override_settings(AI_PROVIDER_ALLOWED_HOSTS=("api.example.com",))
class ExecutionNormalizationTextTests(TestCase):
    setUpTestData = classmethod(fixtures.ExecutionTextContractTests.setUpTestData.__func__)

    def setUp(self):
        fixtures.ExecutionTextContractTests.setUp(self)
        self.enterContext(patch("studio.services.provider_registry.OpenAICompatibleDriver.validate_execution_credentials"))
        provider = self.deployment.provider
        assign_json_contract(provider, "driver_config", {**provider.driver_config,
            "pricing": {"fixed_per_request": "0.1", "currency": "USD"}})
        provider.save()
        model = ModelProfileVersion(profile_key="independent-translation", version=1, model_identifier="declared-translator",
            provider=provider, modality="text", status="published")
        assign_json_contract(model, "specification", {"schema_version": 1, "model_family": "text-protocol",
            "capabilities": ["prompt.normalize"], "prompt_normalization": {"max_input_bytes": 29000,
                "max_items_per_batch": 2, "timeout_seconds": 45,
                "chat_request_options": {"temperature": 0, "response_format": {"type": "json_object"}}}})
        model.save()
        self.deployment = ModelDeployment(key="independent-translation", name="Translation", profile_version=model)
        assign_json_contract(self.deployment, "runtime_config", {})
        self.deployment.save()
        self.source = {"request_id": "term-1", "text": "@人物 进入房间。", "source_lang": "Chinese", "target_lang": "English",
            "field_key": "identity", "label": "人物身份", "context": "保留人物身份标识。", "protected_tokens": ["@人物"]}
        self.fixture = {"inputs": {"translation": {"kind": "literal", "value": [deepcopy(self.source)]}},
            "parameters": {"seed": 17}, "manual_channels": {"positive_prompt": "保留事实，不补写外貌。"},
            "subject_facts": [], "source_language": "", "target_language": ""}
        self.version = self._version("independent-translation")
        self.definition = build_execution_definition(self.version.pk)

    def _version(self, key, profile=None):
        instance = create_execution_instance(key=key, name=key, description="", actor=self.actor)
        version = create_execution_draft(instance_id=instance.pk, expected_revision=instance.revision, actor=self.actor,
            selection={"deployment": self.deployment, "operation": "prompt.normalize", "mode": "",
                "prompt_mode": "profile" if profile else "manual", "prompt_profile_version": profile, "workflow_version": None})
        version = update_execution_draft(version_id=version.pk, expected_revision=version.revision, actor=self.actor, changes={"inputs": [{
            "key": "translation", "position": 0, "kind": "json", "required": True, "min_items": 1, "max_items": 1,
            "semantic_contract": {"schema_version": 1, "allowed_purposes": [], "allowed_subject_kinds": [],
                "allowed_scope_kinds": ["request"], "paired_with_input_key": None, "allow_repeated_asset": False},
            "targets": [{"target_kind": "operation_input", "target_key": "items", "is_primary": True}],
        }]})
        definition = build_execution_definition(version.pk)
        publish_execution_version(version_id=version.pk, expected_revision=version.revision,
            expected_definition_sha256=definition.sha256, actor=self.actor)
        return version

    def smoke(self):
        return create_instance_smoke(version_id=self.version.pk, expected_definition_sha256=self.definition.sha256,
            fixture=self.fixture, field_values=[], client_idempotency_key="independent-smoke", allow_external_calls=True,
            budget_limit=None, actor=self.actor)

    def _prepared(self):
        attempt, prep, _ = self.smoke()
        advanced = advance_execution_preparation(preparation_id=prep.pk, expected_preparation_revision=prep.revision, actor=self.actor)
        self.assertEqual(advanced.status, "dispatched", advanced.reason)
        prep.refresh_from_db()
        return attempt, prep, prep.contract.prepared_request["transport"]

    def _response(self, transport):
        payload = json.loads(transport["payload"]["messages"][-1]["content"])
        token = payload["items"][0]["protected_tokens"][0]
        return {"schema_version": 1, "items": [{"request_id": "term-1", "translation": f"{token} enters the room."}]}

    def test_original_smoke_uses_exact_input_and_restores_protected_tokens(self):
        with CaptureQueriesContext(connection) as captured:
            attempt, prep, transport = self._prepared()
        self.assertFalse(any("studio_promptprofilebinding" in row["sql"].lower() for row in captured))
        self.assertEqual(transport["output"], {"adapter": "text.prompt_normalize/v1", "kind": "text", "schema_version": 1})
        self.assertEqual(transport["operation_plan"]["plan"]["request"]["items"], [self.source])
        self.assertEqual(transport["payload"]["seed"], 17)
        from studio.services.execution_resource_policy import read_resource_policy
        self.assertEqual(transport["payload"]["max_tokens"], read_resource_policy().value["values"]["output_length"])
        self.assertEqual(transport["payload"]["timeout_seconds"], 45)
        wire = json.loads(transport["payload"]["messages"][-1]["content"])
        self.assertEqual(wire["task"], "translate_prompt_fields")
        self.assertEqual(wire["prompt_profile"]["positive_prompt"], "保留事实，不补写外貌。")
        self.assertNotIn("target_prompt_contract", wire)
        from studio.task_center_system_message import system_message_trace
        self.assertEqual(system_message_trace(transport, self.definition.value)["state"], "matched")
        self.assertNotIn("system_message_layout", transport["payload"])
        self.assertNotIn("strategy", [row["key"] for row in transport["operation_plan"]["plan"]["system_message_layout"]["sections"]])
        from studio.task_center_native_inputs import native_input_trace
        trace = native_input_trace(transport, self.definition.value)
        self.assertEqual(trace["state"], "partial")
        self.assertEqual(trace["unmapped_count"], 0)
        self.assertEqual([row["state"] for row in trace["rows"][:4]], ["matched"] * 4)
        self.assertEqual(trace["rows"][7]["state"], "matched")
        peer = Mock()
        peer.invoke.return_value = {"text": json.dumps(self._response(transport)), "request_id": "isolated-translation"}
        with patch("studio.services.execution_smoke_worker.driver_for", return_value=peer):
            result = process_deployment_smoke(claim_next_deployment_smoke("translation-worker"))
        self.assertEqual(result.status, "succeeded", result.error_code)
        self.assertEqual(result.result["evidence"]["result"]["items"][0]["translation"], "@人物 enters the room.")
        self.assertEqual(peer.invoke.call_count, 1)
        self.assertEqual(peer.invoke.call_args.args[2], transport["payload"])
        attempt.refresh_from_db()
        self.assertTrue(attempt.execution_preparations.filter(pk=prep.pk).exists())

    def test_missing_language_wrong_type_and_undeclared_authority_fail_before_task_creation(self):
        for changes in ({"target_lang": ""}, {"source_lang": None}, {"request_id": 123}, {"label": "x" * 241},
                {"protected_tokens": None}, {"target_prompt_contract": {}}, {"source_lang": " Chinese"}, {"text": True}):
            with self.subTest(changes=changes), self.assertRaises(ValidationError):
                normalization_request([{**self.source, **changes}])
        items = [{key: value for key, value in self.source.items() if key != "source_lang"}]
        self.fixture["inputs"]["translation"]["value"] = items
        with self.assertRaises(ValidationError):
            self.smoke()
        self.fixture["inputs"]["translation"]["value"] = [{**self.source, "request_id": f"term-{n}"} for n in range(3)]
        with self.assertRaises(ValidationError):
            self.smoke()
        self.assertFalse(self.deployment.smoke_attempts.exists())

    def test_response_cannot_drop_protection_change_item_identity_or_duplicate_json(self):
        _, _, transport = self._prepared()
        valid = self._response(transport)
        invalid = ["prose", '{"schema_version":1,"schema_version":1,"items":[]}',
            *(json.dumps({**valid, "schema_version": value}) for value in (True, 1.0, "1", None)),
            json.dumps({"schema_version": 1, "items": []}),
            json.dumps({"schema_version": 1, "items": [{"request_id": "term-1", "translation": "a person"}]}),
            json.dumps({"schema_version": 1, "items": [{**valid["items"][0], "request_id": "other"}]})]
        for text in invalid:
            with self.subTest(text=text), self.assertRaises(ValidationError):
                parse_execution_text_response("prompt.normalize", {"text": text}, operation_plan=transport["operation_plan"])
        with self.assertRaises(ValidationError):
            parse_execution_text_response("prompt.normalize", {"text": json.dumps(valid)}, operation_plan=None)

    def test_independent_translation_is_not_a_production_stage_or_candidate(self):
        from studio.services.production_stage_catalog import validate_stage_execution_definition
        from studio.services.execution_production_text_worker import process_prepared_production_text
        from studio.services.execution_production_text_contracts import PRODUCTION_TEXT_OPERATIONS
        self.assertNotIn("prompt.normalize", PRODUCTION_TEXT_OPERATIONS)
        with self.assertRaises(ValidationError):
            validate_stage_execution_definition("script_generate", self.definition)
        with self.assertRaises(ValidationError):
            process_prepared_production_text(SimpleNamespace(operation="prompt.normalize"))

    def test_exact_profile_in_original_preparation_is_not_a_binding_lookup(self):
        profile = PromptProfileVersion.objects.create(profile_key="translation-policy", version=1, name="Translation policy",
            model_family="text-protocol", operation="prompt.normalize", mode="", review_state="ready", contract_schema_version=8)
        field = create_field_definition(key="translation_instruction", label="Translation", value_type="long_text")
        create_profile_field_rule(fixed_value="Preserve declared names exactly.", profile_version=profile, field_definition=field, source_selector="fixed.value",
            generation_source_selector="fixed.value", generation_mode="server_locked", channel="positive_prompt", position=1, required=True,
            language_policy="en")
        refresh_prompt_profile_contract(profile)
        profile = publish_prompt_profile(profile, actor=self.actor)
        self.version = self._version("translation-with-profile", profile)
        self.definition = build_execution_definition(self.version.pk)
        self.fixture["manual_channels"] = {}
        _, _, transport = self._prepared()
        self.assertEqual(transport["profile_compilation"]["assembly"]["prompt_profile_version_id"], str(profile.pk))
        wire = json.loads(transport["payload"]["messages"][-1]["content"])
        self.assertIn("Preserve declared names exactly.", wire["prompt_profile"]["positive_prompt"])
        from studio.task_center_prompt_trace import prompt_trace
        trace = prompt_trace(transport, self.definition.value, {})
        self.assertEqual(trace["state"], "recorded")
        self.assertTrue(trace["channels"])
        self.assertTrue(all(row["state"] == "matched" for row in trace["channels"]))
        from studio.task_center_native_inputs import native_input_trace
        native = native_input_trace(transport, self.definition.value)
        self.assertEqual(native["state"], "partial")
        self.assertEqual(native["unmapped_count"], 0)
        self.assertEqual([row["state"] for row in native["rows"][:4]], ["matched"] * 4)
        assembly = transport["operation_plan"]["plan"]["prompt_profile_assembly"]
        self.assertNotIn("profile_key", assembly)
        self.assertNotIn("version", assembly)
        self.assertEqual([row["state"] for row in native["rows"][4:6]], ["unavailable"] * 2)
        self.assertEqual(native["rows"][7]["state"], "matched")
        self.assertEqual(native["rows"][-1]["state"], "matched")

    def test_original_asset_queue_saves_verified_translation_as_text_candidate(self):
        self.enterContext(patch("studio.services.execution_asset_dispatch.collect_asset_execution_admission", side_effect=isolated_observations))
        job, _ = create_asset_generation_job(owner=self.actor, client_idempotency_key="translation-asset", job_type="prompt", media_type="video",
            execution_instance_version_id=self.version.pk, expected_definition_sha256=self.definition.sha256,
            name="Translation result", field_values=[], **self.fixture)
        prep = freeze(job, self.actor)
        job.status = "queued"
        job.save(update_fields=["status"])
        transport = prep.contract.prepared_request["transport"]
        peer = Mock()
        peer.invoke.return_value = {"text": json.dumps(self._response(transport)), "request_id": "isolated-translation-asset"}
        with patch("studio.services.asset_generation.driver_for", return_value=peer):
            result = process_text_asset_job(claim_next_text_asset_job("translation-asset-worker"))
        self.assertEqual(result.status, "succeeded", result.error_message)
        self.assertIsNone(result.output_asset_id)
        artifact = json.loads(result.result_text)
        self.assertEqual(artifact["items"], [{"request_id": "term-1", "translation": "@人物 enters the room."}])
        self.assertEqual(peer.invoke.call_args.args[2], transport["payload"])
