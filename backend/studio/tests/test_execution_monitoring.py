from django.core.exceptions import ValidationError
from django.test import SimpleTestCase
from unittest.mock import patch

from studio.services.execution_monitoring import (
    execution_monitoring_contract_sha256,
    idle_timeout_for_node,
    resolve_execution_monitoring_contract,
    verify_frozen_execution_monitoring_contract,
)
from studio.services.media_capabilities import (
    validate_model_profile_media_capabilities,
)


class ExecutionMonitoringContractTests(SimpleTestCase):
    graph = {
        "9": {"class_type": "LoadImage", "inputs": {"image": "x.png"}},
        "10": {"class_type": "SamplerCustomAdvanced", "inputs": {}},
        "11": {"class_type": "CreateVideo", "inputs": {"images": ["10", 0]}},
    }

    def setUp(self):
        super().setUp()
        configured = {
            "security.execution.max_idle_timeout_seconds": 86_400,
            "security.execution.max_type_overrides": 128,
            "security.execution.max_node_overrides": 256,
            "security.snapshot.stage_max_bytes": 8 * 1024 * 1024,
        }
        execution_limit_patch = patch(
            "studio.services.execution_monitoring.boundary_int",
            side_effect=lambda key: configured[key],
        )
        execution_limit_patch.start()
        self.addCleanup(execution_limit_patch.stop)

    def test_boolean_or_float_schema_version_is_not_an_integer_contract(self):
        from studio.services.execution_monitoring import normalize_frozen_execution_monitoring_contract
        for schema in (True, 1.0, "1"):
            with self.subTest(schema=schema), self.assertRaises(ValidationError):
                resolve_execution_monitoring_contract(model_specification={"execution_monitoring": {"schema_version": schema}})
            with self.subTest(frozen=schema), self.assertRaises(ValidationError):
                normalize_frozen_execution_monitoring_contract({"schema_version": schema,
                    "default_idle_timeout_seconds": None, "node_type_overrides": {}, "node_overrides": {}})

    def test_model_and_workflow_sources_merge_without_model_names(self):
        contract = resolve_execution_monitoring_contract(
            model_specification={
                "execution_monitoring": {
                    "schema_version": 1,
                    "default_idle_timeout_seconds": 900,
                    "node_type_overrides": {
                        "SamplerCustomAdvanced": 3_600,
                        "CreateVideo": 600,
                    },
                },
            },
            workflow_input_contract={
                "execution_monitoring": {
                    "schema_version": 1,
                    "default_idle_timeout_seconds": 1_200,
                    "node_type_overrides": {"CreateVideo": 750},
                    "node_overrides": {"10": 5_400},
                },
            },
            workflow_graph=self.graph,
        )

        self.assertEqual(contract["default_idle_timeout_seconds"], 1_200)
        self.assertEqual(contract["node_type_overrides"]["CreateVideo"], 750)
        self.assertEqual(
            contract["node_type_overrides"]["SamplerCustomAdvanced"],
            3_600,
        )
        self.assertEqual(contract["node_overrides"], {"10": 5_400})

    def test_node_id_then_node_type_then_default_then_legacy_precedence(self):
        contract = resolve_execution_monitoring_contract(
            model_specification={
                "execution_monitoring": {
                    "schema_version": 1,
                    "default_idle_timeout_seconds": 900,
                    "node_type_overrides": {"SamplerCustomAdvanced": 3_600},
                },
            },
            workflow_input_contract={
                "execution_monitoring": {
                    "schema_version": 1,
                    "node_type_overrides": {},
                    "node_overrides": {"10": 5_400},
                },
            },
            workflow_graph=self.graph,
        )

        self.assertEqual(idle_timeout_for_node(
            contract,
            node_id="10",
            node_type="SamplerCustomAdvanced",
            frozen_runtime_idle_timeout_seconds=60,
        ), 5_400)
        self.assertEqual(idle_timeout_for_node(
            contract,
            node_id="12",
            node_type="SamplerCustomAdvanced",
            frozen_runtime_idle_timeout_seconds=60,
        ), 3_600)
        self.assertEqual(idle_timeout_for_node(
            contract,
            node_id="11",
            node_type="CreateVideo",
            frozen_runtime_idle_timeout_seconds=60,
        ), 900)

        legacy = resolve_execution_monitoring_contract()
        self.assertEqual(idle_timeout_for_node(
            legacy,
            node_id="10",
            node_type="SamplerCustomAdvanced",
            frozen_runtime_idle_timeout_seconds=60,
        ), 60)

    def test_workflow_node_override_must_name_a_real_graph_node(self):
        with self.assertRaises(ValidationError):
            resolve_execution_monitoring_contract(
                workflow_input_contract={
                    "execution_monitoring": {
                        "schema_version": 1,
                        "node_type_overrides": {},
                        "node_overrides": {"404": 3_600},
                    },
                },
                workflow_graph=self.graph,
            )

    def test_model_contract_cannot_contain_workflow_node_ids(self):
        with self.assertRaises(ValidationError):
            resolve_execution_monitoring_contract(
                model_specification={
                    "execution_monitoring": {
                        "schema_version": 1,
                        "node_type_overrides": {},
                        "node_overrides": {"10": 3_600},
                    },
                },
            )

    def test_model_profile_validation_accepts_media_contract_and_rejects_text(self):
        specification = {
            "execution_monitoring": {
                "schema_version": 1,
                "default_idle_timeout_seconds": 1_200,
                "node_type_overrides": {"SamplerCustomAdvanced": 3_600},
            },
        }
        validate_model_profile_media_capabilities(
            specification,
            modality="video",
        )
        with self.assertRaises(ValidationError):
            validate_model_profile_media_capabilities(
                specification,
                modality="text",
            )

    def test_node_type_override_accepts_comfy_extension_display_names(self):
        contract = resolve_execution_monitoring_contract(
            model_specification={
                "execution_monitoring": {
                    "schema_version": 1,
                    "node_type_overrides": {
                        "KSampler (Efficient)": 3_600,
                        "中文节点 β": 900,
                    },
                },
            },
        )
        self.assertEqual(
            contract["node_type_overrides"]["KSampler (Efficient)"],
            3_600,
        )
        with self.assertRaises(ValidationError):
            resolve_execution_monitoring_contract(
                model_specification={
                    "execution_monitoring": {
                        "schema_version": 1,
                        "node_type_overrides": {"bad\nnode": 900},
                    },
                },
            )

    def test_frozen_contract_hash_detects_mutation(self):
        contract = resolve_execution_monitoring_contract(
            workflow_input_contract={
                "execution_monitoring": {
                    "schema_version": 1,
                    "default_idle_timeout_seconds": 1_200,
                    "node_type_overrides": {},
                    "node_overrides": {"10": 5_400},
                },
            },
            workflow_graph=self.graph,
        )
        digest = execution_monitoring_contract_sha256(contract)
        self.assertEqual(
            verify_frozen_execution_monitoring_contract(
                contract,
                sha256=digest,
            ),
            contract,
        )
        changed = {
            **contract,
            "node_overrides": {"10": 5_399},
        }
        with self.assertRaises(ValidationError):
            verify_frozen_execution_monitoring_contract(changed, sha256=digest)

    def test_effective_contract_contains_no_executable_selector(self):
        contract = resolve_execution_monitoring_contract(
            workflow_input_contract={
                "execution_monitoring": {
                    "schema_version": 1,
                    "node_type_overrides": {"SamplerCustomAdvanced": 3_600},
                    "node_overrides": {},
                },
            },
            workflow_graph=self.graph,
        )
        self.assertEqual(
            set(contract),
            {
                "schema_version",
                "default_idle_timeout_seconds",
                "node_type_overrides",
                "node_overrides",
            },
        )
