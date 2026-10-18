from __future__ import annotations

import ast
from pathlib import Path
from types import SimpleNamespace
from unittest.mock import patch
from uuid import uuid4

from django.core.exceptions import ValidationError
from django.test import SimpleTestCase, TestCase

from studio.services.json_contracts import canonical_json_bytes, json_contract
from studio.services.production_runs import _save_stage_snapshot, _verify_stage_snapshot
from studio.services.workflow_compiler import workflow_execution_contract
from studio.services.workflow_semantics import canonical_binding_rows


class _InMemoryStage:
    """Small persistence double for exercising the production snapshot helpers."""

    def __init__(self) -> None:
        self.saved_update_fields: tuple[str, ...] = ()

    def save(self, *, update_fields) -> None:
        self.saved_update_fields = tuple(update_fields)


class ConfiguredJsonContractLimitTests(SimpleTestCase):
    def setUp(self) -> None:
        self.payload = {
            "schema_version": 1,
            # Deliberately above the legacy implicit 64 KiB default.
            "content": "x" * 70_000,
        }
        self.payload_size = len(canonical_json_bytes(self.payload))

    def test_raised_stage_boundary_allows_write_and_read_above_64_kib(self):
        stage = _InMemoryStage()
        configured_limit = self.payload_size + 1

        with patch(
            "studio.services.production_runs._stage_snapshot_max_bytes",
            return_value=configured_limit,
        ):
            digest = _save_stage_snapshot(stage, "output_snapshot", self.payload)
            self.assertEqual(
                _verify_stage_snapshot(stage, "output_snapshot"),
                self.payload,
            )

        self.assertEqual(stage.output_snapshot_size_bytes, self.payload_size)
        self.assertEqual(stage.output_snapshot_sha256, digest)
        self.assertIn("output_snapshot_sha256", stage.saved_update_fields)

    def test_lowered_stage_boundary_blocks_write_and_existing_read_immediately(self):
        stage = _InMemoryStage()
        with patch(
            "studio.services.production_runs._stage_snapshot_max_bytes",
            return_value=self.payload_size + 1,
        ):
            _save_stage_snapshot(stage, "output_snapshot", self.payload)

        configured_limit = self.payload_size - 1
        with patch(
            "studio.services.production_runs._stage_snapshot_max_bytes",
            return_value=configured_limit,
        ):
            with self.assertRaisesMessage(
                ValidationError,
                f"超过 {configured_limit} 字节上限",
            ):
                _save_stage_snapshot(_InMemoryStage(), "output_snapshot", self.payload)
            with self.assertRaisesMessage(
                ValidationError,
                f"超过 {configured_limit} 字节上限",
            ):
                _verify_stage_snapshot(stage, "output_snapshot")

    def test_workflow_contract_follows_parameterized_graph_boundary(self):
        bindings = [
            {
                "semantic_key": f"parameter.long-contract-field-{index:04d}",
                "node_id": f"node-{index:04d}",
                "input_name": "value",
                "value_type": "string",
                "is_required": True,
            }
            for index in range(800)
        ]
        binding_size = len(canonical_json_bytes(canonical_binding_rows(bindings)))
        self.assertGreater(binding_size, 64 * 1024)

        for configured_limit, should_pass in (
            (binding_size + 1, True),
            (binding_size - 1, False),
        ):
            with self.subTest(
                configured_limit=configured_limit,
                should_pass=should_pass,
            ), patch(
                "studio.services.workflow_semantics._workflow_graph_max_bytes",
                return_value=configured_limit,
            ), patch(
                "studio.services.workflow_compiler._workflow_graph_max_bytes",
                return_value=configured_limit,
            ):
                if should_pass:
                    _payload, digest = workflow_execution_contract(
                        compiler_id="generic.comfy",
                        compiler_version=1,
                        graph_sha256="a" * 64,
                        input_contract_sha256="b" * 64,
                        bindings=bindings,
                        requirements=[],
                    )
                    self.assertEqual(len(digest), 64)
                else:
                    with self.assertRaisesMessage(
                        ValidationError,
                        f"超过 {configured_limit} 字节上限",
                    ):
                        workflow_execution_contract(
                            compiler_id="generic.comfy",
                            compiler_version=1,
                            graph_sha256="a" * 64,
                            input_contract_sha256="b" * 64,
                            bindings=bindings,
                            requirements=[],
                        )


class ExplicitRegistryContractLimitTests(TestCase):
    def test_registry_metadata_write_read_and_frozen_reference_share_size_boundaries(self):
        from studio.services import business_outputs, content_sources

        source = SimpleNamespace(pk=uuid4(), source_key=uuid4(), version=1, name="梗概",
            description="来源定义", reader_selector="project.logline", status="published",
            reader_contract=content_sources.reader_contract("project.logline"))
        output = SimpleNamespace(pk=uuid4(), output_key=uuid4(), version=1, name="规则",
            description="输出定义", status="published", parser_key=business_outputs.PARSER_KEY,
            parser_contract=business_outputs.parser_contract(business_outputs.PARSER_KEY), quality_advisories={})
        cases = (
            (content_sources, "CONTENT_SOURCE_CONTRACT_MAX_BYTES", source,
             content_sources.source_contract, content_sources.source_reference),
            (business_outputs, "DATABASE_JSON_FIELD_MAX_BYTES", output,
             business_outputs.output_contract, business_outputs.output_reference),
        )
        for module, boundary, obj, write, read in cases:
            with self.subTest(registry=module.__name__):
                original = write(obj)
                obj.contract_size_bytes, obj.contract_sha256 = original.size_bytes, original.sha256
                with patch.object(module, boundary, original.size_bytes):
                    self.assertEqual(write(obj), original)
                    reference = read(obj)
                    self.assertEqual(reference["sha256"], original.sha256)
                    if obj is output:
                        self.assertEqual(business_outputs.verify_output_reference(reference), reference)
                with patch.object(module, boundary, original.size_bytes - 1):
                    with self.assertRaises(ValidationError):
                        write(obj)
                    with self.assertRaises(ValidationError):
                        read(obj)
                    if obj is output:
                        with self.assertRaises(ValidationError):
                            business_outputs.verify_output_reference(reference)

    def test_parser_cannot_bypass_physical_ceiling_with_large_field_label(self):
        from studio.services import business_outputs
        parser = business_outputs.parser_contract(business_outputs.PARSER_KEY)
        parser["fields"][0]["label"] = "界" * business_outputs.DATABASE_JSON_FIELD_MAX_BYTES
        with self.assertRaisesMessage(ValidationError, "65536"):
            business_outputs.validate_parser_contract(parser)

    def test_reconciliation_uses_one_resolved_limit_for_parent_and_both_row_digests(self):
        from studio.services import execution_profile_inputs as inputs
        # This helper checks reconciliation only; the full instance schema is
        # checked separately by the original authoring service.
        row = {"key": "request.theme", "position": 0,
            "targets": [{"target_kind": "profile_input", "target_key": "request.theme"}]}
        version = SimpleNamespace(prompt_mode="profile", prompt_profile_version=object())
        size = len(canonical_json_bytes([row]))
        with patch.object(inputs, "execution_configuration_max_bytes", return_value=size) as limit, \
                patch.object(inputs, "derived_profile_inputs", return_value=[row]), \
                patch.object(inputs, "json_contract", wraps=json_contract) as contract:
            self.assertEqual(inputs.reconcile_profile_inputs(version, [row]), [row])
        limit.assert_called_once_with()
        self.assertEqual(len(contract.call_args_list), 3)
        self.assertEqual([item.kwargs for item in contract.call_args_list], [{"max_bytes": size}] * 3)
        with patch.object(inputs, "execution_configuration_max_bytes", return_value=size - 1), \
                patch.object(inputs, "derived_profile_inputs") as derive:
            with self.assertRaises(ValidationError):
                inputs.reconcile_profile_inputs(version, [row])
        derive.assert_not_called()


class JsonContractCallsiteGovernanceTests(SimpleTestCase):
    """Prevent reintroducing an implicit 64 KiB gate on runtime contracts."""

    _CALLS = {"json_contract", "verify_json_contract", "assign_json_contract"}

    @staticmethod
    def _backend_root() -> Path:
        return Path(__file__).resolve().parents[1]

    def test_runtime_callsites_declare_max_bytes(self):
        missing: list[str] = []
        root = self._backend_root()
        for path in sorted(root.rglob("*.py")):
            if "migrations" in path.parts or "tests" in path.parts:
                continue
            tree = ast.parse(path.read_text(encoding="utf-8"), filename=str(path))
            for node in ast.walk(tree):
                if not isinstance(node, ast.Call):
                    continue
                name = (
                    node.func.id
                    if isinstance(node.func, ast.Name)
                    else node.func.attr
                    if isinstance(node.func, ast.Attribute)
                    else ""
                )
                if name not in self._CALLS:
                    continue
                if not any(keyword.arg == "max_bytes" for keyword in node.keywords):
                    missing.append(f"{path.relative_to(root)}:{node.lineno}")
        self.assertEqual(missing, [], "隐式 JSON 上限调用: " + ", ".join(missing))

    def test_runtime_callsites_do_not_embed_literal_max_bytes(self):
        embedded: list[str] = []
        root = self._backend_root()
        for path in sorted(root.rglob("*.py")):
            if "migrations" in path.parts or "tests" in path.parts:
                continue
            tree = ast.parse(path.read_text(encoding="utf-8"), filename=str(path))
            for node in ast.walk(tree):
                if not isinstance(node, ast.Call):
                    continue
                name = (
                    node.func.id
                    if isinstance(node.func, ast.Name)
                    else node.func.attr
                    if isinstance(node.func, ast.Attribute)
                    else ""
                )
                if name not in self._CALLS:
                    continue
                maximum = next(
                    (keyword.value for keyword in node.keywords if keyword.arg == "max_bytes"),
                    None,
                )
                if isinstance(maximum, (ast.Constant, ast.BinOp)):
                    embedded.append(f"{path.relative_to(root)}:{node.lineno}")
        self.assertEqual(
            embedded,
            [],
            "运行期 JSON 上限必须读取命名安全边界: " + ", ".join(embedded),
        )

    def test_default_limit_is_only_used_by_configuration_bootstrap(self):
        root = self._backend_root()
        users: list[str] = []
        for path in sorted(root.rglob("*.py")):
            if "migrations" in path.parts or "tests" in path.parts:
                continue
            if path.name == "json_contracts.py":
                continue
            tree = ast.parse(path.read_text(encoding="utf-8"), filename=str(path))
            if any(
                isinstance(node, ast.Name)
                and node.id == "DEFAULT_JSON_LIMIT_BYTES"
                for node in ast.walk(tree)
            ):
                users.append(path.relative_to(root).as_posix())
        self.assertEqual(users, ["services/configuration.py"])
