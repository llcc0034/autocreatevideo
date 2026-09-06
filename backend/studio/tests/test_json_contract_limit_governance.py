from __future__ import annotations

import ast
from pathlib import Path
from unittest.mock import patch

from django.core.exceptions import ValidationError
from django.test import SimpleTestCase

from studio.services.json_contracts import canonical_json_bytes
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


class JsonContractCallsiteGovernanceTests(SimpleTestCase):
    """Prevent reintroducing an implicit 64 KiB gate on runtime contracts."""

    _CALLS = {"json_contract", "verify_json_contract", "assign_json_contract"}
    _CONCURRENTLY_OWNED_EXCLUSIONS = {
        "assembly.py",
        "dialogue.py",
        "reference_media.py",
        "series_concept_drafts.py",
    }

    @staticmethod
    def _backend_root() -> Path:
        return Path(__file__).resolve().parents[1]

    def test_runtime_callsites_declare_max_bytes(self):
        missing: list[str] = []
        root = self._backend_root()
        for path in sorted(root.rglob("*.py")):
            if "migrations" in path.parts or "tests" in path.parts:
                continue
            if path.name in self._CONCURRENTLY_OWNED_EXCLUSIONS:
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
