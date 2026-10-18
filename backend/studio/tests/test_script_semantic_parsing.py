"""One JSON parse/default path for script preview, import and frozen response."""
from copy import deepcopy
import json
from unittest.mock import patch
from uuid import uuid4

from django.core.exceptions import ValidationError
from django.test import SimpleTestCase

from studio.services.json_contracts import JsonExpansionBudget
from studio.services.script_semantics import (
    ScriptSemanticValidationError, freeze_script_semantics, parse_script_semantic_output, preview_script_semantics,
)
from studio.services.script_semantic_requests import build_script_semantic_request, parse_script_semantic_response
from studio.services.structured_output_parsing import parse_structure_text, parse_structured_output
from studio.services.script_semantic_relations import VALIDATION_SCOPE
from studio.tests.output_structure_fixtures import frozen_response_structure
from studio.tests.script_semantics_fixtures import path, script_example
from studio.tests.test_script_semantic_requests import LIMIT, reference


class ScriptSemanticParsingTests(SimpleTestCase):
    def setUp(self):
        self.raw, self.source, self.mapping = script_example()
        self.reference = reference(self.source, self.mapping)

    def parse(self, raw=None, **changes):
        return parse_script_semantic_output(**{"reference": self.reference,
            "response_text": json.dumps(self.raw if raw is None else raw, ensure_ascii=False),
            "max_bytes": LIMIT, **changes})

    def request(self, **changes):
        with patch("studio.services.model_profile_contracts._chat_request_option_limits", return_value={
                "max_bytes": LIMIT, "max_depth": 12, "max_fields": 100, "max_array_items": 100, "max_string_characters": 10000}):
            return build_script_semantic_request(**{"operation": "script.generate", "output_reference": self.reference,
                "inputs": {"idea": "隔离解析样例"}, "parameters": {"max_tokens": 2048, "seed": 0},
                "transport_policy": {"timeout_seconds": 30, "chat_request_options": {}},
                "prompt_profile_assembly": {"channels": {"positive_prompt": "本次要求"}, "structured_fields": {}},
                "plan_max_bytes": LIMIT, "request_max_bytes": LIMIT, **changes})

    def diagnostic(self, error, response):
        from studio.services.execution_text_response_review import response_validation_diagnostic

        # Only the unrelated transport-usage bound needs a pure fixture. The
        # semantic parser/report and diagnostic conversion remain unpatched.
        with patch("studio.services.provider_registry.boundary_int", return_value=1_000_000):
            return response_validation_diagnostic(error, response)

    def test_two_layouts_share_preview_import_and_frozen_response_projection(self):
        values = []
        for layout in ("a", "b"):
            raw, source, mapping = script_example(layout)
            output = reference(source, mapping)
            text = json.dumps(raw, ensure_ascii=False, indent=2)
            direct = self.parse(reference=output, response_text=text)
            preview = preview_script_semantics(configuration=freeze_script_semantics(
                source_contract=source, mapping=mapping, max_bytes=LIMIT), response_text=text, max_bytes=LIMIT)
            payload, plan = self.request(output_reference=output)
            response = parse_script_semantic_response(response_text=text, operation_plan=plan, request_payload=payload,
                request_max_bytes=LIMIT, response_max_bytes=LIMIT, expected_output=output)
            self.assertTrue(direct["valid"], direct["errors"])
            self.assertEqual(direct, response)
            self.assertEqual({k: v for k, v in preview.items() if k != "preview_only"},
                {**{k: v for k, v in direct.items() if k not in ("output_version_id", "output_contract_sha256", "relation_errors")},
                    "validation_scope": "structure_and_mapping"})
            self.assertEqual(direct["raw_value"], raw)
            self.assertEqual(direct["source_value"], raw)
            self.assertEqual(direct["validation_scope"], VALIDATION_SCOPE)
            for future in ("candidate", "approved", "materialized", "script_revision_id"):
                self.assertNotIn(future, direct)
            values.append(direct["value"])
        self.assertEqual(*values)

    def test_script_and_generic_output_use_the_same_json_default_and_warning_pass(self):
        source = deepcopy(self.source)
        for name, value in (("is_voiceover", False), ("start_ms", 0)):
            next(n for n in source["nodes"] if n["property_name"] == name).update(has_default=True, default_value=value)
        raw = deepcopy(self.raw)
        dialogue = raw["episodes"][0]["shots"][0]["dialogue"]
        del dialogue["is_voiceover"]
        del dialogue["start_ms"]
        raw["extra.中文"] = {"notes": [False, 0, None]}
        script = self.parse(raw, reference=reference(source, self.mapping))
        generic_ref = reference(source, self.mapping, parser={"key": "structured_json.v1", "format": "json"})
        generic = parse_structured_output(reference=generic_ref, response_text=json.dumps(raw), max_bytes=LIMIT)
        self.assertTrue(script["valid"], script["errors"])
        self.assertEqual(script["source_value"], generic["value"])
        self.assertEqual(script["raw_value"], generic["raw_value"])
        # Only the caller's path label differs; there is one parser/default owner.
        self.assertEqual(json.dumps(script["defaults"]).replace("source.", "response."), json.dumps(generic["defaults"]))
        self.assertEqual(json.dumps(script["warnings"]).replace("source", "response"), json.dumps(generic["warnings"]))
        self.assertNotIn("is_voiceover", script["raw_value"]["episodes"][0]["shots"][0]["dialogue"])
        mapped = script["value"]["episodes"][0]["shots"][0]["dialogue"]
        self.assertIs(mapped["is_voiceover"], False)
        self.assertEqual(mapped["start_ms"], 0)

    def test_both_entrypoints_call_shared_parser_once_not_a_second_decoder(self):
        with patch("studio.services.script_semantics.parse_structure_text", wraps=parse_structure_text) as parser:
            self.assertTrue(self.parse()["valid"])
            self.assertEqual(parser.call_count, 1)
        with patch("studio.services.structured_output_parsing.parse_structure_text", wraps=parse_structure_text) as parser:
            output = reference(self.source, self.mapping, parser={"key": "structured_json.v1", "format": "json"})
            self.assertTrue(parse_structured_output(reference=output, response_text=json.dumps(self.raw), max_bytes=LIMIT)["valid"])
            self.assertEqual(parser.call_count, 1)

    def test_root_list_and_literal_dotted_names_do_not_require_fixed_script_root(self):
        raw = [{"cast.items": self.raw["entities"], "part.一": self.raw["episodes"]}]
        source = frozen_response_structure(raw)
        mapping = deepcopy(self.mapping)
        mapping["node"]["fields"]["entities"]["source"] = path(0, "cast.items", scope="root")
        mapping["node"]["fields"]["episodes"]["source"] = path(0, "part.一", scope="root")
        report = self.parse(raw, reference=reference(source, mapping))
        self.assertTrue(report["valid"], report["errors"])
        self.assertEqual(report["source_value"], raw)
        # Custom content stays in the source; only mapped semantics are projected.
        self.assertIn("mood_note", report["source_value"][0]["part.一"][0]["shots"][0])
        self.assertNotIn("mood_note", report["value"]["episodes"][0]["shots"][0])
        self.assertEqual(report["value"]["entities"], self.raw["entities"])

    def test_invalid_json_is_not_repaired_or_sent_to_a_model(self):
        for text in ("原文自由文本", '[SCRIPT_BIBLE_V2]\n{}', '```json\n{}\n```', '{"a":1,"a":2}',
                     '{"a":NaN}', '{"a":1} trailing', '{"a":}'):
            report = self.parse(response_text=text)
            with self.subTest(text=text):
                self.assertFalse(report["parsed"])
                self.assertFalse(report["valid"])
                self.assertIn("response_text", report["errors"])
                self.assertIsNone(report["value"])

    def test_invalid_report_diagnostic_never_copies_response_values_or_full_report(self):
        bad = deepcopy(self.raw)
        bad["episodes"][0]["shots"][0]["references"][0]["variant_key"] = "private-missing-variant"
        report = self.parse(bad)
        original = deepcopy(report)
        error = ScriptSemanticValidationError(report)
        diagnostic = self.diagnostic(error, {"text": json.dumps(bad),
            "provider_secret": "do-not-retain", "usage": {"prompt_tokens": 8, "completion_tokens": 5}})
        self.assertEqual(report, original)
        self.assertEqual(diagnostic["validation_code"], "script_semantic_result_invalid")
        self.assertEqual(diagnostic["validation_scope"], VALIDATION_SCOPE)
        self.assertEqual(diagnostic["usage"], {"prompt_tokens": 8, "completion_tokens": 5})
        self.assertEqual(diagnostic["relation_errors"], report["relation_errors"])
        serialized = json.dumps(diagnostic)
        for forbidden in ("private-missing-variant", "do-not-retain", "raw_value", "source_value", "provenance"):
            self.assertNotIn(forbidden, serialized)
        report["errors"].clear()
        report["relation_errors"][0]["message"] = "changed after exception"
        self.assertEqual(error.relation_errors, original["relation_errors"])
        self.assertTrue(error.structure_errors)

    def test_source_locations_follow_both_layouts_and_preserve_semantic_locations(self):
        for layout in ("a", "b"):
            raw, source, mapping = script_example(layout)
            node = (raw["episodes"][0]["shots"][0]["references"][0] if layout == "a"
                else raw["chapters"][0]["frames"][0]["uses"][0])
            node["variant_key"] = "unknown"
            report = self.parse(raw, reference=reference(source, mapping))
            error = ScriptSemanticValidationError(report)
            for relation in report["relation_errors"]:
                self.assertIn(relation["path"], error.message_dict)
                self.assertIn(relation["source_path"], error.message_dict)
                self.assertEqual(error.message_dict[relation["source_path"]], [relation["message"]])

    def test_syntax_and_type_errors_keep_their_scope_without_inventing_relations(self):
        bad = deepcopy(self.raw)
        bad["entities"] = "private-invalid-type"
        for report in (self.parse(response_text='{"unfinished":'), self.parse(bad)):
            error = ScriptSemanticValidationError(report)
            diagnostic = self.diagnostic(error, {"text": "not copied"})
            self.assertEqual(diagnostic["validation_scope"], "structure_and_mapping")
            self.assertEqual(diagnostic["relation_errors"], [])
            self.assertEqual(diagnostic["structure_errors"], report["errors"])
            self.assertNotIn("private-invalid-type", json.dumps(diagnostic))

    def test_literal_relation_error_does_not_claim_a_source_text_location(self):
        mapping = deepcopy(self.mapping)
        # A literal wrong scene reference is a configuration fact, not a source
        # field. Use the existing literal operator, never invent a source path.
        mapping["node"]["fields"]["episodes"]["item"]["fields"]["shots"]["item"]["fields"]["scene_key"] = {
            "kind": "literal", "value": "missing-scene"}
        report = self.parse(reference=reference(self.source, mapping))
        error = ScriptSemanticValidationError(report)
        self.assertTrue(error.relation_errors)
        for relation in error.relation_errors:
            self.assertEqual(relation["origin_kind"], "literal")
            self.assertNotIn("source_path", relation)
        self.assertEqual(error.structure_errors, report["errors"])

    def test_valid_report_is_not_a_failure_and_generic_errors_are_not_reclassified(self):
        from studio.services.execution_structured_text import StructuredTextValidationError

        with self.assertRaises(ValueError):
            ScriptSemanticValidationError(self.parse())
        generic = self.diagnostic(StructuredTextValidationError({"response.count": ["wrong type"]}), {})
        self.assertEqual(generic["validation_code"], "structured_text_result_invalid")
        self.assertNotIn("relation_errors", generic)
        plain = self.diagnostic(ValidationError("private-validation-detail"), {})
        self.assertEqual(plain["validation_code"], "response_contract_invalid")
        self.assertNotIn("private-validation-detail", json.dumps(plain))

    def test_invalid_source_preserves_raw_and_error_path_without_projection(self):
        raw = deepcopy(self.raw)
        raw["episodes"][0]["shots"][0]["duration_ms"] = "not an integer"
        report = self.parse(raw)
        self.assertTrue(report["parsed"])
        self.assertFalse(report["valid"])
        self.assertEqual(report["raw_value"], raw)
        self.assertEqual(report["source_value"], raw)
        self.assertIn("source.episodes[0].shots[0].duration_ms", report["errors"])
        self.assertIsNone(report["value"])

    def test_successor_constraint_affects_both_request_rule_and_response_validation(self):
        payload, original = self.request()
        source = deepcopy(self.source)
        source.update(id=str(uuid4()), version=2)
        next(n for n in source["nodes"] if n["property_name"] == "action")["constraints"] = {"max_length": 1}
        newer = reference(source, self.mapping, output_key=self.reference["output_key"], version=2)
        new_payload, _ = self.request(output_reference=newer)
        self.assertIn('"maxLength":1', new_payload["messages"][0]["content"])
        self.assertFalse(self.parse(reference=newer)["valid"])
        self.assertTrue(parse_script_semantic_response(response_text=json.dumps(self.raw), operation_plan=original,
            request_payload=payload, request_max_bytes=LIMIT, response_max_bytes=LIMIT, expected_output=self.reference)["valid"])
        with self.assertRaises(ValidationError):
            parse_script_semantic_response(response_text=json.dumps(self.raw), operation_plan=original,
                request_payload=payload, request_max_bytes=LIMIT, response_max_bytes=LIMIT, expected_output=newer)

    def test_frozen_response_read_does_not_render_rules_or_load_current_policy(self):
        payload, plan = self.request(operation="script.structure", inputs={"source_text": "完整原稿\r\n"})
        with patch("studio.services.script_semantic_requests.script_semantic_messages", side_effect=AssertionError("rerender")), \
             patch("studio.services.model_profile_contracts.validate_chat_request_options", side_effect=AssertionError("policy")):
            report = parse_script_semantic_response(response_text=json.dumps(self.raw), operation_plan=plan,
                request_payload=payload, request_max_bytes=LIMIT, response_max_bytes=LIMIT, expected_output=self.reference)
        self.assertTrue(report["valid"])
        self.assertEqual(report["output_contract_sha256"], self.reference["sha256"])

    def test_invalid_or_wrong_contract_is_not_used_as_a_generic_fallback(self):
        generic = reference(self.source, self.mapping, parser={"key": "structured_json.v1", "format": "json"})
        for ref in (None, generic, {**self.reference, "sha256": "0" * 64}):
            with self.subTest(reference=ref), self.assertRaises(ValidationError):
                self.parse(reference=ref)

    def test_response_adapter_does_not_parse_before_frozen_request_matches(self):
        payload, plan = self.request()
        changed_payload = deepcopy(payload)
        changed_payload["seed"] += 1
        changed_plan = deepcopy(plan)
        changed_plan["plan"]["inputs"]["idea"] = "不是冻结的输入"
        with patch("studio.services.script_semantics.parse_script_semantic_output",
                   side_effect=AssertionError("unverified request reached parser")) as parser:
            for changes in ({"expected_output": None}, {"request_payload": changed_payload},
                            {"operation_plan": changed_plan}):
                with self.subTest(changes=changes), self.assertRaises(ValidationError):
                    parse_script_semantic_response(**{"response_text": json.dumps(self.raw),
                        "operation_plan": plan, "request_payload": payload, "request_max_bytes": LIMIT,
                        "response_max_bytes": LIMIT, "expected_output": self.reference, **changes})
            parser.assert_not_called()

    def test_failed_shared_work_budget_never_reports_valid(self):
        report = parse_structure_text(structure=self.source, response_text=json.dumps(self.raw), max_bytes=LIMIT,
            budget=JsonExpansionBudget(1, path="test.shared_budget"))
        self.assertTrue(report["parsed"])
        self.assertFalse(report["valid"])
        self.assertEqual(report["raw_value"], self.raw)
        self.assertIn("test.shared_budget", report["errors"])

    def test_projection_failure_keeps_successful_source_parse_and_custom_values(self):
        source = deepcopy(self.source)
        mapping = deepcopy(self.mapping)
        mapping["node"]["fields"]["episodes"]["item"]["fields"]["episode"] = {"kind": "literal", "value": 0}
        report = self.parse(reference=reference(source, mapping))
        self.assertFalse(report["valid"])
        self.assertEqual(report["source_value"], self.raw)
        self.assertIn("value.episodes[0].episode", report["errors"])

    def test_bad_relation_is_rejected_after_successful_structure_parse(self):
        raw = deepcopy(self.raw)
        raw["episodes"][0]["shots"][0]["references"][0]["entity_key"] = "missing-entity"
        report = self.parse(raw)
        self.assertFalse(report["valid"])
        self.assertEqual(report["validation_scope"], VALIDATION_SCOPE)
        self.assertEqual(report["relation_errors"][0]["code"], "entity_unknown")
        self.assertEqual(report["source_value"], raw)
        self.assertTrue(report["parsed"])
        self.assertNotIn("candidate", report)
        payload, plan = self.request()
        response = parse_script_semantic_response(response_text=json.dumps(raw, ensure_ascii=False),
            operation_plan=plan, request_payload=payload, expected_output=self.reference,
            request_max_bytes=LIMIT, response_max_bytes=LIMIT)
        self.assertEqual(report, response)
