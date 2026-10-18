from copy import deepcopy
import json
from types import SimpleNamespace
from unittest.mock import patch

from django.test import TestCase

from studio.services.system_message_layout import assemble_system_message, text_sha
from studio.task_center_system_message import system_message_trace


class SystemMessageTraceTests(TestCase):
    def sample(self):
        layout = {}
        body = assemble_system_message([
            ("protocol", "平台原协议😀\n"), ("output_contract", 'CONTRACT={"original":true}'),
            ("strategy", "\nCONFIGURED_SYSTEM_INSTRUCTION=伪标签 CONTRACT={} 仍是策略正文")], layout=layout)
        output = {"adapter": "text.visual_rules/v2", "kind": "text", "schema_version": 2}
        transport = {"operation": "visual_rules.generate", "output": output,
            "operation_plan": {"operation": "visual_rules.generate", "plan": {"system_message_layout": layout}},
            "payload": {"messages": [{"role": "system", "content": body}, {"role": "user", "content": "{}"}]}}
        definition = {"configuration": {"operation": "visual_rules.generate"}, "interface": {"output": deepcopy(output)}}
        return transport, definition

    def test_saved_segments_cover_unicode_without_text_copy_or_rebuild(self):
        t, d = self.sample()
        before = deepcopy((t, d))
        with self.assertNumQueries(0), patch("studio.services.production_visual_rules.visual_rules_provider_payload", side_effect=AssertionError("no rebuild")):
            report = system_message_trace(t, d)
        self.assertEqual(report["state"], "matched")
        self.assertEqual(len(report["rows"]), 3)
        self.assertEqual(report["rows"][0]["end"], len("平台原协议😀\n"))
        self.assertEqual(report["rows"][-1]["end"], len(t["payload"]["messages"][0]["content"]))
        self.assertEqual((t, d), before)
        self.assertNotIn("伪标签", json.dumps(report, ensure_ascii=False))
        self.assertNotIn("平台原协议", json.dumps(t["operation_plan"], ensure_ascii=False))
        self.assertLess(len(json.dumps(report, ensure_ascii=False).encode()), 4096)

    def test_missing_old_layout_never_uses_current_builder(self):
        t, d = self.sample()
        del t["operation_plan"]["plan"]["system_message_layout"]
        self.assertEqual(system_message_trace(t, d)["state"], "unavailable")
        self.assertIsNone(system_message_trace({"operation": "image.generate"}, {}))

    def test_body_changes_suffix_and_truncation_are_mismatch(self):
        for change in ("中", "x", ""):
            t, d = self.sample()
            t["payload"]["messages"][0]["content"] += change or " "
            self.assertEqual(system_message_trace(t, d)["state"], "mismatch")
        t, d = self.sample()
        t["payload"]["messages"][0]["content"] = t["payload"]["messages"][0]["content"][1:]
        self.assertEqual(system_message_trace(t, d)["state"], "mismatch")

    def test_field_layout_has_explicit_location_and_never_rebuilds_old_requests(self):
        for operation in ("prompt.fields.generate", "prompt.fields.translate"):
            layout = {}
            content = assemble_system_message([("protocol", "JSON protocol. "), ("field_rules", "Read user field rules.")], layout=layout)
            output = {"adapter": "text.prompt_fields/v1", "kind": "text", "schema_version": 1}
            transport = {"operation": operation, "output": output, "system_message_layout": layout,
                "payload": {"messages": [{"role": "system", "content": content}, {"role": "user", "content": "{}"}]}}
            definition = {"configuration": {"operation": operation}, "interface": {"output": deepcopy(output)}}
            before = deepcopy((transport, definition))
            with self.assertNumQueries(0), patch("studio.services.execution_text_contracts.build_execution_text_payload", side_effect=AssertionError("no rebuild")):
                result = system_message_trace(transport, definition)
            self.assertEqual(result["state"], "matched")
            self.assertIn("不是策略system通道", result["rows"][1]["source"])
            self.assertEqual((transport, definition), before)
            for change in ("missing", "location", "adapter", "operation", "section", "null", "roles", "gap", "omitted_rules"):
                with self.subTest(operation=operation, change=change):
                    t, d = deepcopy(before)
                    if change == "missing": del t["system_message_layout"]
                    elif change == "location": t["operation_plan"] = {"operation": operation, "plan": {"system_message_layout": t.pop("system_message_layout")}}
                    elif change == "adapter": t["output"]["adapter"] = "text.prompt_draft/v1"
                    elif change == "operation": d["configuration"]["operation"] = "prompt.draft"
                    elif change == "section": t["system_message_layout"]["sections"][1]["key"] = "strategy"
                    elif change == "null": t["system_message_layout"] = None
                    elif change == "roles": t["payload"]["messages"][0]["role"] = "user"
                    elif change == "gap": t["system_message_layout"]["sections"][1]["start"] += 1
                    else:
                        replacement = {}
                        t["payload"]["messages"][0]["content"] = assemble_system_message([("protocol", content)], layout=replacement)
                        t["system_message_layout"] = replacement
                    self.assertEqual(system_message_trace(t, d)["state"], "unavailable")
            transport["payload"]["messages"][0]["content"] += "Changed"
            self.assertEqual(system_message_trace(transport, definition)["state"], "mismatch")
        t, d = self.sample()
        t["system_message_layout"] = t["operation_plan"]["plan"]["system_message_layout"]
        self.assertEqual(system_message_trace(t, d)["state"], "unavailable")

    def test_invalid_identity_segments_bounds_and_hashes_fail_closed(self):
        for change in ("missing", "adapter", "roles", "version", "index", "count", "gap", "overlap", "negative", "bool", "overflow", "duplicate", "unknown", "hash", "empty", "extra", "no_protocol"):
            with self.subTest(change=change):
                t, d = self.sample()
                layout = t["operation_plan"]["plan"]["system_message_layout"]
                row = layout["sections"][1]
                if change == "missing": del layout["role"]
                elif change == "adapter": d["interface"]["output"]["adapter"] = "another"
                elif change == "roles": t["payload"]["messages"][0]["role"] = "user"
                elif change == "version": layout["schema_version"] = True
                elif change == "index": layout["message_index"] = True
                elif change == "count": layout["characters"] = False
                elif change == "gap": row["start"] += 1
                elif change == "overlap": row["start"] -= 1
                elif change == "negative": row["start"] = -1
                elif change == "bool": row["end"] = True
                elif change == "overflow": row["end"] = layout["characters"] + 1
                elif change == "duplicate": row["key"] = "protocol"
                elif change == "unknown": row["key"] = "private_secret"
                elif change == "hash": row["sha256"] = "not-a-digest"
                elif change == "empty": layout["sections"] = []
                elif change == "extra": row["text"] = "private secret"
                else: layout["sections"][0]["key"] = "quality_advisories"
                report = system_message_trace(t, d)
                self.assertEqual(report["state"], "unavailable")
                self.assertEqual(report["rows"], [])
                self.assertNotIn("private", json.dumps(report))

    def test_whole_message_and_section_digests_are_independently_checked(self):
        for part in ("whole", "section"):
            t, d = self.sample()
            layout = t["operation_plan"]["plan"]["system_message_layout"]
            target = layout if part == "whole" else layout["sections"][1]
            target["sha256"] = text_sha("different")
            self.assertEqual(system_message_trace(t, d)["state"], "mismatch")

    def test_task_reader_exposes_layout_without_adding_queries(self):
        from studio.task_center_details import inspect_input
        from django.db import connection
        from django.test.utils import CaptureQueriesContext
        t, d = self.sample()
        # The parent frozen reader normally verifies this required declaration.
        d["configuration"]["inputs"] = []
        t.update(parameters={"values": {}}, prompt_channels={"values": {}}, driver_type="deepseek_api")
        detail = SimpleNamespace(intent={"request": {"requested": {}}}, resolved_inputs={})
        with patch("studio.task_center_details.read_frozen_execution_request", return_value=(detail, SimpleNamespace(value=d), SimpleNamespace(value=t))):
            with CaptureQueriesContext(connection) as baseline, patch("studio.task_center_details.system_message_trace", return_value=None):
                inspect_input(SimpleNamespace(request_frozen_at=True))
            with CaptureQueriesContext(connection) as actual:
                result = inspect_input(SimpleNamespace(request_frozen_at=True))
        # The existing reader resolves redaction/security policy. This feature
        # must not add any query, rather than mislabelling the whole reader pure.
        self.assertEqual([row["sql"] for row in actual], [row["sql"] for row in baseline])
        self.assertEqual(result["system_message_trace"]["state"], "matched")
        self.assertEqual(result["prompts"][0]["text"], t["payload"]["messages"][0]["content"])

    def test_writer_does_not_overwrite_receipt_and_empty_segments_add_no_offsets(self):
        layout = {}
        self.assertEqual(assemble_system_message([("protocol", "abc"), ("strategy", "")], layout=layout), "abc")
        self.assertEqual(len(layout["sections"]), 1)
        for parts in ([("protocol", "again")], [("unknown", "text")], [("protocol", 5)]):
            with self.assertRaises(ValueError):
                assemble_system_message(parts, layout=layout)

    def test_frozen_reader_keeps_old_field_receipts_missing_and_rejects_foreign_protocol_metadata(self):
        from django.core.exceptions import ValidationError
        from studio.services.execution_preparations import read_frozen_execution_request
        operation = "prompt.fields.generate"
        definition = SimpleNamespace(sha256="definition", value={"configuration": {"operation": operation},
            "target": {"workflow": None, "driver_type": "deepseek_api"}, "target_fingerprint": "target",
            "interface": {"output": {"adapter": "text.prompt_fields/v1"}}})
        transport = {"schema_version": 1, "execution_instance_version_id": "version", "definition_sha256": "definition",
            "target_fingerprint": "target", "operation": operation, "driver_type": "deepseek_api", "idempotency_key": "prep",
            "parameters": {}, "prompt_channels": {}, "profile_compilation": None, "field_request_sha256": "batch",
            "payload": {}, "output": definition.value["interface"]["output"]}
        detail = SimpleNamespace(intent={"request": {"definition_sha256": "definition"}}, resolved_inputs={"parameters": {}},
            prepared_request={"schema_version": 1, "preparation_id": "prep", "basis_preparation_revision": 1,
                "intent_sha256": "intent", "field_selections": {}, "transport": transport})
        preparation = SimpleNamespace(pk="prep", revision=2, intent_sha256="intent", execution_instance_version_id="version")
        with patch("studio.services.execution_preparations.read_execution_preparation_detail", return_value=detail), patch(
                "studio.services.execution_preparations.preparation_execution_definition", return_value=definition):
            before = deepcopy(transport)
            saved = read_frozen_execution_request(preparation)[2].value
            self.assertEqual(saved, before)
            self.assertEqual(system_message_trace(saved, definition.value)["state"], "unavailable")
            layout = {}
            system = assemble_system_message([("protocol", "JSON"), ("field_rules", " field rules")], layout=layout)
            transport.update(system_message_layout=layout, payload={"messages": [
                {"role": "system", "content": system}, {"role": "user", "content": "{}"}]})
            saved = read_frozen_execution_request(preparation)[2].value
            self.assertEqual(system_message_trace(saved, definition.value)["state"], "matched")
            transport["operation"] = definition.value["configuration"]["operation"] = "prompt.draft"
            with self.assertRaises(ValidationError):
                read_frozen_execution_request(preparation)
