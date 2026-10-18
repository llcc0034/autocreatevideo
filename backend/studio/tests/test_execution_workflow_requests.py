"""Frozen compiled contracts, real dynamic graph fixture, no provider calls."""
from copy import deepcopy
from unittest.mock import patch

from django.core.exceptions import ValidationError
from django.test import TestCase, override_settings

from studio.services.execution_definitions import verify_execution_definition_payload
from studio.services.execution_workflow_requests import verify_execution_workflow_transport
from studio.services.json_contracts import json_contract
from studio.tests import test_execution_workflow_request_media as fixtures


@override_settings(AI_PROVIDER_ALLOWED_HOSTS=("127.0.0.1",))
class ExecutionWorkflowRequestTests(TestCase):
    _create_version = fixtures.ExecutionWorkflowMediaRequestTests._create_version
    compile = fixtures.ExecutionWorkflowMediaRequestTests.compile

    def setUp(self):
        fixtures.ExecutionWorkflowMediaRequestTests.setUp(self)
        self.transport = self.compile()

    def read(self, value=None):
        return verify_execution_workflow_transport(self.definition,
            self.transport if value is None else json_contract(value))

    def test_reads_exact_frozen_graph_without_compiling_or_current_resource_lookup(self):
        with patch("studio.services.workflow_compiler.compile_frozen_workflow_values", side_effect=AssertionError("No recompile")), \
                patch("studio.services.execution_definitions.build_execution_definition", side_effect=AssertionError("No current defaults")), \
                patch("studio.services.execution_identity.execution_target_contract", side_effect=AssertionError("No resource lookup")):
            # Current system safety caps may still be checked; business model,
            # workflow, route and project defaults must not be rebuilt here.
            actual = self.read()
        self.assertEqual(actual, self.transport.value["payload"]["compiled_workflow"])
        actual["resolved_semantics"]["sampling.seed"] = 1234
        self.assertEqual(self.transport.value["payload"]["compiled_workflow"]["resolved_semantics"]["sampling.seed"], 37)

    def test_prompt_mapping_reads_actual_compiler_semantics_not_current_graph_defaults(self):
        from studio.task_center_prompt_trace import _channel
        from studio.services.execution_workflow_inspection import frozen_workflow_prompt_rows
        with self.assertNumQueries(0), patch("studio.services.workflow_compiler.compile_frozen_workflow_values", side_effect=AssertionError("No recompile")):
            for row in frozen_workflow_prompt_rows(self.definition.value, self.transport.value):
                result = _channel(self.transport.value, row["key"], row["text"], self.definition.value)
                self.assertEqual(result["state"], "matched")
                self.assertIn(row["semantic_key"], result["destination"])
                self.assertIn("不按正文反推节点端口", result["message"])

    def test_mismatched_component_hashes_are_rejected_even_with_valid_outer_envelope(self):
        for name in ("graph", "resolved_semantics", "asset_bindings", "model_requirements"):
            with self.subTest(name=name):
                value = deepcopy(self.transport.value)
                value["payload"]["compiled_workflow"][name + "_sha256"] = "0" * 64
                with self.assertRaises(ValidationError):
                    self.read(value)

    def test_other_compiler_workflow_and_output_are_not_accepted(self):
        mutations = {"compiler_id": "another.compiler", "compiler_version": True,
            "workflow_contract_sha256": "0" * 64, "output_contract": {"primary": {"kind": "image", "node_id": "missing"}}}
        for key, replacement in mutations.items():
            with self.subTest(key=key):
                value = deepcopy(self.transport.value)
                value["payload"]["compiled_workflow"][key] = replacement
                with self.assertRaises(ValidationError):
                    self.read(value)
        value = deepcopy(self.transport.value)
        value["payload"]["compiled_workflow"]["legacy_route"] = {}
        with self.assertRaises(ValidationError):
            self.read(value)

    def test_graph_with_rehashed_missing_primary_node_is_rejected(self):
        value = deepcopy(self.transport.value)
        compiled = value["payload"]["compiled_workflow"]
        del compiled["graph"][str(compiled["output_contract"]["primary"]["node_id"])]
        compiled["graph_sha256"] = json_contract(compiled["graph"]).sha256
        with self.assertRaises(ValidationError):
            self.read(value)

    def test_unrelated_preparation_namespace_and_auxiliary_identity_are_rejected(self):
        for key, replacement in (("idempotency_key", "other-request"), ("field_request_sha256", "a" * 64),
                ("operation", "image.generate"), ("execution_instance_version_id", "another-instance")):
            with self.subTest(key=key):
                value = deepcopy(self.transport.value)
                value[key] = replacement
                with self.assertRaises(ValidationError):
                    self.read(value)

    def test_frozen_workflow_definition_requires_canonical_monitoring_not_runtime_fallback(self):
        monitoring = self.definition.value["interface"]["execution_monitoring"]
        self.assertEqual(monitoring["node_overrides"], {})
        value = deepcopy(self.definition.value)
        del value["interface"]["execution_monitoring"]
        with self.assertRaisesMessage(ValidationError, "节点进度监测"):
            verify_execution_definition_payload(json_contract(value))
