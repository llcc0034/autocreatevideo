from copy import deepcopy
import json
from types import SimpleNamespace
from unittest.mock import patch

from django.core.exceptions import ValidationError
from django.db import connection
from django.test import SimpleTestCase, TestCase, override_settings
from django.test.utils import CaptureQueriesContext

from studio.services.json_contracts import json_contract
from studio.task_center_media_trace import media_trace
from studio.task_center_details import bound_content
from studio.task_center_serializers import TaskCenterDetailQuerySerializer
from studio.tests import test_execution_workflow_staging as fixtures


class NoMediaGraphTraceTests(SimpleTestCase):
    def test_no_media_still_explains_complete_graph_without_inventing_receipts(self):
        # Isolate verified input/receipt boundaries; this is projection coverage,
        # not a Smoke result or admission proof. No database/network is permitted.
        from studio.services.execution_workflow_staging import ExecutionWorkflowReceipts
        graph = {"text": {"class_type": "TextEncoder", "inputs": {"text": "Original prompt"}},
            "output": {"class_type": "Output", "inputs": {"conditioning": ["text", 0]}},
            "unused": {"class_type": "Unconnected", "inputs": {"seed": 0}}}
        definition = json_contract({"target": {"workflow": {"id": "pinned-version", "execution_contract_sha256": "a" * 64}}})
        transport = json_contract({"driver_type": "comfyui", "payload": {"media_preparation": {
            "bindings": [], "reference_media_profile": None}}})
        before = deepcopy((definition, transport, graph))
        targets = {
            "execution_configuration_max_bytes": 1048576,
            "read_execution_workflow_uses": SimpleNamespace(items={}, slots={}),
            "read_execution_workflow_receipts": ExecutionWorkflowReceipts(graph, {}, frozenset(), ()),
            "execution_media_processing_uses": {}, "native_video_audio_uses": set(), "read_pinned_workflow": None,
        }
        from contextlib import ExitStack
        with ExitStack() as stack:
            for name, result in targets.items():
                stack.enter_context(patch("studio.task_center_media_trace." + name, return_value=result))
            stack.enter_context(patch("studio.services.security_boundaries.boundary_resolution",
                return_value=SimpleNamespace(values={"security.prompt.max_usage_count": 1000000})))
            trace = media_trace(definition, transport, {"inputs": {}}, owner_id="owner", root_id="root", receipts=[])
        self.assertEqual(trace["items"], [])
        self.assertEqual([row["node_id"] for row in trace["nodes"]], list(graph))
        self.assertEqual(trace["edges"], [{"from_node": "text", "output_index": 0, "to_node": "output", "input_name": "conditioning"}])
        self.assertTrue(all(row["origin"] == "unavailable" for row in trace["nodes"]))
        self.assertEqual((definition, transport, graph), before)


@override_settings(AI_PROVIDER_ALLOWED_HOSTS=("127.0.0.1",))
class MediaTraceTests(TestCase):
    _create_version = fixtures.ExecutionWorkflowStagingTests._create_version
    compile = fixtures.ExecutionWorkflowStagingTests.compile
    stage = fixtures.ExecutionWorkflowStagingTests.stage
    persist = fixtures.ExecutionWorkflowStagingTests.persist
    setUp = fixtures.ExecutionWorkflowStagingTests.setUp

    def trace(self, **kwargs):
        return media_trace(**{"definition": self.definition, "transport": self.transport,
            "resolved": {"inputs": self.inputs.value}, "owner_id": self.user.pk, "root_id": self.job.pk,
            "receipts": [], **kwargs})

    def test_exact_duplicate_uses_graph_ports_and_partial_receipts_without_live_lookups(self):
        records = self.stage().receipts.value
        before = deepcopy((self.definition, self.inputs, self.transport, records))
        with CaptureQueriesContext(connection) as queries, patch(
                "studio.services.execution_workflow_staging.preprocess_reference_input", side_effect=AssertionError("No processing")), patch(
                "studio.services.execution_request_compilation.compile_execution_transport_request", side_effect=AssertionError("No compile")):
            trace = self.trace(receipts=records[:1])
        for query in queries:
            self.assertTrue(query["sql"].lstrip().upper().startswith("SELECT"))
            for forbidden in ('"studio_asset"', '"studio_modelexecutioninstanceversion"'):
                self.assertNotIn(forbidden, query["sql"].lower())
        pinned = [q["sql"] for q in queries if 'FROM "studio_workflowtemplateversion"' in q["sql"]]
        self.assertEqual(len(pinned), 1)
        self.assertIn(str(self.workflow.pk).replace("-", ""), pinned[0].replace("-", ""))
        self.assertIn('"studio_workflowtemplateversion"."id" =', pinned[0])
        self.assertEqual(trace["scalar_wiring"]["state"], "matched")
        graph = self.transport.value["payload"]["compiled_workflow"]["graph"]
        self.assertEqual({node["node_id"] for node in trace["nodes"]}, set(graph))
        for node in trace["nodes"]:
            for port in node["inputs"]:
                if port["kind"] == "connection":
                    self.assertIn({"from_node": port["value"][0], "output_index": port["value"][1],
                        "to_node": node["node_id"], "input_name": port["input_name"]}, trace["edges"])
        self.assertTrue(any(r["semantic_key"] == "prompt.positive" for r in trace["scalar_wiring"]["rows"]))
        rows = trace["items"]
        self.assertEqual([r["item_key"] for r in rows], ["second", "first"])
        self.assertEqual(rows[0]["asset_id"], rows[1]["asset_id"])
        self.assertEqual([r["receipt_state"] for r in rows], ["recorded", "missing"])
        self.assertEqual([r["processing_summary"]["state"] for r in rows], ["recorded", "declared"])
        self.assertEqual(rows[0]["processing_summary"]["byte_identity"], "same")
        self.assertEqual(rows[1]["processing_summary"]["observed"], [])
        self.assertEqual(rows[0]["graph_inputs"], [{"node_id": "cr_images_0", "class_type": "LoadImage", "input_path": ["image"]}])
        self.assertEqual(rows[1]["graph_inputs"][0]["node_id"], "cr_images_1")
        self.assertEqual(rows[0]["adapter"], "source_bytes/v1")
        self.assertEqual(rows[0]["receipt"]["submitted_sha256"], self.asset.checksum)
        self.assertTrue(any(e["from_node"] == "cr_images_0" for e in trace["edges"]))
        loader = next(n for n in trace["nodes"] if n["node_id"] == "cr_images_0")
        self.assertEqual(loader["origin"], "added")
        image = next(row for row in loader["inputs"] if row["input_name"] == "image")
        self.assertEqual(image["kind"], "media")
        self.assertIsNone(image["value"])
        for row in rows:
            self.assertTrue(row["port_routes"])
            for route in row["port_routes"]:
                self.assertEqual(route["state"], "connected")
                self.assertEqual(route["entry_node_id"], row["graph_inputs"][0]["node_id"])
                final_edge = trace["edges"][route["edge_indexes"][-1]]
                self.assertEqual(final_edge["to_node"], route["destination"]["node_id"])
                self.assertEqual(final_edge["input_name"], route["destination"]["input_name"])
        self.assertEqual((self.definition, self.inputs, self.transport, records), before)
        public = json.dumps(trace)  # No second redaction can hide a raw-response leak.
        for private in ("comfy_path", "autoprompt/", "__AUTOPROMPT_MEDIA_USE__", self.asset.file.path):
            self.assertNotIn(private, public)

    def test_corrupt_receipt_target_or_actual_graph_never_looks_successful(self):
        receipts = self.stage().receipts.value
        for key in ("root_id", "owner_id", "source_sha256", "target_fingerprint", "metadata_sha256"):
            bad = deepcopy(receipts); bad[0][key] = "bad"
            with self.subTest(key=key), self.assertRaises(ValidationError):
                self.trace(receipts=bad)
        bad = deepcopy(self.transport.value)
        bad["payload"]["compiled_workflow"]["graph"]["cr_images_0"]["inputs"]["image"] = "unrecorded.png"
        bad["payload"]["compiled_workflow"]["graph_sha256"] = json_contract(bad["payload"]["compiled_workflow"]["graph"]).sha256
        with self.assertRaisesMessage(ValidationError, "媒体占位符"):
            self.trace(transport=json_contract(bad))

    def test_isolated_node_retains_frozen_parameters_but_never_private_paths(self):
        request = deepcopy(self.transport.value)
        compiled = request["payload"]["compiled_workflow"]
        compiled["graph"]["isolated"] = {"class_type": "KSampler", "inputs": {
            "seed": 0, "enabled": False, "filename": "private-unused.png", "api_key": "private-key"}}
        compiled["graph_sha256"] = json_contract(compiled["graph"]).sha256
        transport = json_contract(request)
        before = deepcopy(transport)
        trace = self.trace(transport=transport)
        node = next(row for row in trace["nodes"] if row["node_id"] == "isolated")
        ports = {row["input_name"]: row for row in node["inputs"]}
        self.assertEqual(node["origin"], "added")
        self.assertEqual(ports["seed"]["value"], 0)
        self.assertIs(ports["enabled"]["value"], False)
        for key in ("filename", "api_key"):
            self.assertEqual(ports[key]["kind"], "redacted")
            self.assertIsNone(ports[key]["value"])
        self.assertNotIn("private-unused.png", json.dumps(trace))
        self.assertNotIn("private-key", json.dumps(trace))
        self.assertFalse(any("isolated" in (edge["from_node"], edge["to_node"]) for edge in trace["edges"]))
        self.assertIn("节点存在不代表已执行", trace["message"])
        self.assertEqual(transport, before)

    def test_pinned_wiring_is_bounded_readonly_retired_and_never_uses_latest(self):
        from studio.models import WorkflowTemplateVersion
        from studio.task_center_workflow_wiring import scalar_wiring
        self.workflow.status = "retired"
        self.workflow.save(update_fields=["status"])
        with CaptureQueriesContext(connection) as queries:
            trace = scalar_wiring(self.definition, self.transport)
        registry = [q for q in queries if 'FROM "studio_workflow' in q["sql"]]
        self.assertEqual(len(registry), 4)  # Version, bindings, requirements, exact frozen Projector.
        projector = [q["sql"] for q in registry if 'FROM "studio_workflowprojectorversion"' in q["sql"]]
        self.assertEqual(len(projector), 1)
        projector_id = self.workflow.input_contract["contract_projector"]["version_id"]
        self.assertIn(projector_id.replace("-", ""), projector[0].replace("-", ""))
        self.assertIn('"studio_workflowprojectorversion"."id" =', projector[0])
        for query in queries:
            self.assertTrue(query["sql"].lstrip().upper().startswith("SELECT"))
            self.assertTrue('FROM "studio_workflow' in query["sql"] or 'FROM "studio_config' in query["sql"], query["sql"])
        # Shared safety limits may be loaded once each, never for every graph binding.
        with patch("studio.task_center_workflow_wiring.scalar_port_rows", return_value=[]), CaptureQueriesContext(connection) as no_rows:
            scalar_wiring(self.definition, self.transport)
        self.assertEqual(len(queries), len(no_rows))
        self.assertEqual(trace["state"], "matched")
        before = deepcopy((self.definition, self.transport))
        # A deleted exact version cannot be substituted by another version.
        with patch("django.db.models.query.QuerySet.get", side_effect=WorkflowTemplateVersion.DoesNotExist):
            self.assertEqual(scalar_wiring(self.definition, self.transport)["state"], "unavailable")
        self.assertEqual((self.definition, self.transport), before)
        # Corrupt the isolated registry fixture only; original request/media remain readable.
        WorkflowTemplateVersion.objects.filter(pk=self.workflow.pk).update(graph_sha256="0" * 64)
        trace = self.trace()
        self.assertEqual(trace["scalar_wiring"]["state"], "unavailable")
        self.assertEqual(len(trace["items"]), 2)
        self.assertEqual(trace["scalar_wiring"]["rows"], [])
        self.assertTrue(all(n["origin"] == "unavailable" for n in trace["nodes"]))
        self.assertTrue(all(row["change"] == "unavailable" for n in trace["nodes"] for row in n["inputs"]))

    def test_changed_binding_or_pinned_digest_cannot_claim_a_match(self):
        from studio.task_center_workflow_wiring import scalar_wiring
        from studio.services.workflow_versions import workflow_version_execution_snapshot
        snapshot = workflow_version_execution_snapshot(self.workflow)
        for field in ("semantic_bindings", "execution_contract_sha256"):
            bad = deepcopy(snapshot)
            if field == "semantic_bindings":
                bad[field][0]["input_name"] = "invented"
            else:
                bad[field] = "f" * 64
            with patch("studio.task_center_workflow_wiring.workflow_version_execution_snapshot", return_value=bad):
                self.assertEqual(scalar_wiring(self.definition, self.transport)["state"], "unavailable")

    def test_bounded_trace_reader_and_non_workflow_null(self):
        self.assertIsNone(self.trace(transport=json_contract({"driver_type": "deepseek_api"})))
        trace = self.trace(); trace["items"] *= 300
        detail = {"input": {"state": "frozen", "submitted": {}, "resolved": {}, "parameters": [], "prompts": [],
            "prompt_trace": None, "field_batch_trace": None, "media_trace": trace, "notes": []},
            "output": {"state": "unavailable", "result": None, "text": "", "media": [], "notes": []}}
        q = TaskCenterDetailQuerySerializer(data={"content_field": "input.media_trace", "content_limit": 1024})
        self.assertTrue(q.is_valid(), q.errors)
        result = bound_content(detail, q.validated_data)
        self.assertIsNone(result["input"]["media_trace"])
        self.assertEqual(result["content"]["field"], "input.media_trace")
        self.assertLessEqual(len(result["content"]["text"].encode()), 1024)

    def test_context_only_use_keeps_identity_without_upload_or_graph_input(self):
        from studio.models import ModelExecutionInstanceVersion
        from studio.services.execution_instances import update_execution_draft
        from studio.services.execution_input_resolution import resolve_execution_input_sources
        version = ModelExecutionInstanceVersion.objects.get(pk=self.definition.value["version_id"])
        specs = deepcopy(self.definition.value["configuration"]["inputs"])
        context = deepcopy(specs[0]); context.update(key="context_photo", position=1, targets=[])
        with self.assertRaisesMessage(ValidationError, "未配置目标输入"):
            update_execution_draft(version_id=version.pk, expected_revision=version.revision,
                actor=self.user, changes={"inputs": [*specs, context]})
        # Lower-level projection fixture only, like ExecutionWorkflowMediaTests.
        # Authoring still requires an explicit target (e.g. a profile context).
        # No registry version or business Preparation is persisted by this fixture.
        payload = deepcopy(self.definition.value)
        payload["configuration"]["inputs"] = [*specs, context]
        self.definition = json_contract(payload)
        self.inputs = resolve_execution_input_sources(self.definition, {key: {"items": [{"kind": "asset",
            "item_key": key, "asset_id": str(self.asset.pk), "purpose": None}]} for key in ("references", "context_photo")},
            owner=self.user, subject_facts=[], max_media_bytes=65536)
        self.transport = self.compile()
        row = self.trace()["items"][1]
        self.assertEqual(row["mode"], "context_only")
        self.assertEqual(row["receipt_state"], "not_applicable")
        self.assertEqual(row["ports"], [])
        self.assertEqual(row["graph_inputs"], [])
        self.assertIsNone(row["adapter"])
        self.assertEqual(row["port_routes"], [])
        self.assertEqual(row["processing_summary"]["state"], "not_applicable")

    def test_swapped_duplicate_asset_uses_do_not_claim_connected_ports(self):
        bad = deepcopy(self.transport.value)
        graph = bad["payload"]["compiled_workflow"]["graph"]
        trace = self.trace()
        first, second = [r["port_routes"][0]["destination"] for r in trace["items"]]
        a, b = graph[first["node_id"]]["inputs"], graph[second["node_id"]]["inputs"]
        a[first["input_name"]], b[second["input_name"]] = b[second["input_name"]], a[first["input_name"]]
        bad["payload"]["compiled_workflow"]["graph_sha256"] = json_contract(graph).sha256
        result = self.trace(transport=json_contract(bad))
        self.assertEqual([row["port_routes"][0]["state"] for row in result["items"]], ["disconnected", "disconnected"])
