"""Restricted REST explanation, isolated databases and mocked transport only."""
from copy import deepcopy
from types import SimpleNamespace
import json
from unittest.mock import patch

from django.core.exceptions import ValidationError
from django.db import connection
from django.test import TestCase, override_settings
from django.test.utils import CaptureQueriesContext

from studio.models import ModelDeployment
from studio.services.async_submit_mapping import async_submit_body
from studio.services.execution_request_compilation import compile_execution_transport_request
from studio.services.execution_parameter_preparation import initialize_execution_definition_parameters
from studio.services.json_contracts import json_contract
from studio.services.provider_registry import ProviderDriverError, RestrictedAsyncRestDriver
from studio.task_center_api_mapping import api_mapping
from studio.task_center_details import inspect_input
from studio.tests import test_execution_definitions as fixtures


@override_settings(AI_PROVIDER_ALLOWED_HOSTS=("api.example.com", "cdn.example.com"))
class TaskApiMappingTests(TestCase):
    setUpTestData = classmethod(fixtures.ExecutionDefinitionTests.setUpTestData.__func__)
    write_contract = staticmethod(fixtures.ExecutionDefinitionTests.write_contract)
    definition = fixtures.ExecutionDefinitionTests.definition

    def setUp(self):
        fixtures.ExecutionDefinitionTests.setUp(self)
        self.config["request_field_map"].update(prompt="input_text", negative_prompt="avoid_text", seed="random_seed", audio="with_audio")
        self.write_contract(self.provider, "driver_config", self.config)
        self.frozen = self.definition()
        parameters = initialize_execution_definition_parameters(self.frozen,
            {"width": 512, "height": 512, "fps": 24, "duration_seconds": 1}, policies=[], resolve_source=lambda _: None)
        self.transport = compile_execution_transport_request(self.frozen, prepared_parameters=parameters,
            manual_channels={"positive_prompt": "Actual private prompt"}, idempotency_key="fixture-preparation")

    def test_real_compiled_input_uses_one_exact_indexed_deployment_read_and_no_writes(self):
        with CaptureQueriesContext(connection) as queries:
            result = api_mapping(self.frozen, self.transport)
        self.assertEqual(result["state"], "verified")
        rows = {r["key"]: r for r in result["rows"]}
        self.assertEqual(rows["prompt"], {"key": "prompt", "field": "input_text", "state": "included"})
        self.assertEqual(rows["duration_seconds"]["field"], "duration")
        self.assertEqual(rows["negative_prompt"]["state"], "omitted")
        encoded = json.dumps(result)
        for private in ("Actual private prompt", "api.example.com", "Authorization", "runtime_config", "idempotency_header"):
            self.assertNotIn(private, encoded)
        self.assertLess(len(encoded.encode()), 4096)
        self.assertFalse(any(q["sql"].lstrip().startswith(("INSERT", "UPDATE", "DELETE")) for q in queries))
        lookups = [q["sql"] for q in queries if 'FROM "studio_modeldeployment"' in q["sql"]]
        self.assertEqual(len(lookups), 1)
        self.assertLess(len(queries), 30)
        for excluded in ('"credential_id"', '"specification"', '"description"', '"name"'):
            self.assertNotIn(excluded, lookups[0])
        if connection.vendor == "sqlite":
            with connection.cursor() as cursor:
                cursor.execute("EXPLAIN QUERY PLAN " + lookups[0])
                plan = cursor.fetchall()
            self.assertTrue(all("SEARCH" in row[-1] and "INDEX" in row[-1] for row in plan), plan)

    def test_absent_null_false_zero_empty_and_unmapped_are_distinct_without_mutating_payload(self):
        value = deepcopy(self.transport.value)
        value["payload"].update(seed=0, audio=False, negative_prompt="")
        result = api_mapping(self.frozen, json_contract(value))
        rows = {r["key"]: r for r in result["rows"]}
        for key in ("seed", "audio", "negative_prompt"):
            self.assertEqual(rows[key]["state"], "included")
        original = deepcopy(value["payload"])
        self.assertEqual(async_submit_body(original, self.config["request_field_map"])["with_audio"], False)
        self.assertEqual(original, value["payload"])
        value["payload"]["negative_prompt"] = None
        rows = {r["key"]: r for r in api_mapping(self.frozen, json_contract(value))["rows"]}
        self.assertEqual(rows["negative_prompt"]["state"], "omitted")
        # A standard field without a declared destination must be visible as such.
        mapping = {k: v for k, v in self.config["request_field_map"].items() if k != "audio"}
        with patch("studio.task_center_api_mapping.read_async_field_map", return_value=mapping):
            rows = {r["key"]: r for r in api_mapping(self.frozen, json_contract(value))["rows"]}
        self.assertEqual(rows["audio"], {"key": "audio", "field": None, "state": "unmapped"})

    def test_adapter_post_uses_the_same_projection_and_keeps_identity_out_of_body(self):
        payload = {**self.transport.value["payload"], "seed": 0, "audio": False, "negative_prompt": ""}
        deployment = SimpleNamespace(credential=None, provider=SimpleNamespace(credential=None))
        with patch("studio.services.provider_registry._combined_config", return_value=self.config), patch(
                "studio.services.provider_registry._request_json", return_value={"id": "fixture-job"}) as send:
            result = RestrictedAsyncRestDriver().invoke(deployment, "submit", payload)
        self.assertEqual(result["status"], "submitted")
        send.assert_called_once()
        self.assertEqual(send.call_args.kwargs["payload"], async_submit_body(payload, self.config["request_field_map"]))
        self.assertEqual(send.call_args.kwargs["headers"]["Idempotency-Key"], "fixture-preparation")
        self.assertNotIn("idempotency_key", send.call_args.kwargs["payload"])
        self.assertNotIn("schema_version", send.call_args.kwargs["payload"])

    def test_drift_is_unavailable_not_a_new_mapping_or_rewritten_result(self):
        before = deepcopy(self.transport)
        self.config["request_field_map"]["prompt"] = "new_text"
        self.write_contract(self.provider, "driver_config", self.config)
        result = api_mapping(self.frozen, self.transport)
        self.assertEqual((result["state"], result["code"], result["rows"]), ("unavailable", "deployment_mapping_changed", []))
        self.assertNotIn("new_text", json.dumps(result))
        self.assertEqual(self.transport, before)

    def test_non_execution_pricing_change_and_disabled_deployment_do_not_destroy_historical_mapping(self):
        self.write_contract(self.provider, "driver_config", {**self.config, "pricing": {"currency": "USD", "fixed_per_request": "0.02"}})
        ModelDeployment.objects.filter(pk=self.deployment.pk).update(status="disabled")
        self.assertEqual(api_mapping(self.frozen, self.transport)["state"], "verified")

    def test_missing_invalid_and_sensitive_errors_are_safe_and_never_replaced(self):
        with patch("studio.services.execution_parameter_mappings.ModelDeployment.objects.select_related") as query:
            query.return_value.only.return_value.get.side_effect = ModelDeployment.DoesNotExist()
            result = api_mapping(self.frozen, self.transport)
        self.assertEqual(result["code"], "mapping_resource_missing")
        for error in (ValidationError("private endpoint secret"), ProviderDriverError("private endpoint secret", code="invalid")):
            with patch("studio.services.execution_parameter_mappings.deployment_execution_config_contract", side_effect=error):
                result = api_mapping(self.frozen, self.transport)
            self.assertEqual((result["code"], result["rows"]), ("mapping_evidence_invalid", []))
            self.assertNotIn("private endpoint", json.dumps(result))

    def test_non_rest_and_identity_mismatch_never_read_current_mapping(self):
        with patch("studio.task_center_api_mapping.read_async_field_map", side_effect=AssertionError("No lookup")):
            for field, value in (("driver_type", "comfyui"), ("definition_sha256", "0" * 64), ("operation", "audio.text_to_audio")):
                transport = json_contract({**self.transport.value, field: value})
                result = api_mapping(self.frozen, transport)
                if field == "driver_type":
                    self.assertIsNone(result)
                else:
                    self.assertEqual(result["code"], "api_mapping_identity_invalid")

    def test_inspect_input_keeps_original_prompts_when_mapping_is_unavailable(self):
        prepared = SimpleNamespace(intent={"request": {"requested": {}}}, resolved_inputs={})
        preparation = SimpleNamespace(request_frozen_at=True)
        self.config["timeout_seconds"] = 31
        self.write_contract(self.provider, "driver_config", self.config)
        with patch("studio.task_center_details.read_frozen_execution_request", return_value=(prepared, self.frozen, self.transport)):
            result = inspect_input(preparation)
        self.assertEqual(result["state"], "frozen")
        self.assertEqual(result["api_mapping"]["state"], "unavailable")
        self.assertEqual(result["prompts"][0]["text"], "Actual private prompt")
