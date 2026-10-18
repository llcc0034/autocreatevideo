from copy import deepcopy
import json
from types import SimpleNamespace
from unittest.mock import patch

from django.test import SimpleTestCase, TestCase

from studio.task_center_native_inputs import native_input_trace


class NativeInputTraceTests(SimpleTestCase):
    def sample(self, operation="script.generate"):
        adapters = {"script.generate": "text.script_generate/v2", "script.structure": "text.script_structure/v3",
                    "visual_rules.generate": "text.visual_rules/v2"}
        channels = {"positive_prompt": "主题 中文😀", "negative_prompt": ""}
        inputs = ({"idea": "故事", "style_hint": "", "episode_count": 1} if operation == "script.generate"
                  else {"source_text": "很长的剧本\n"})
        plan = {"prompt_profile_assembly": {"channels": channels, "structured_fields": {"theme": "主题 中文😀"}}}
        body = {"prompt_profile": deepcopy(channels)}
        output = {"adapter": adapters[operation]}
        definition = {"configuration": {"operation": operation}, "interface": {"output": deepcopy(output)}}
        if operation in {"script.generate", "script.structure"}:
            # This pure reader's parent verifies full output identity; here only
            # its exact equality is under test, not publication or execution.
            reference = {"id": "isolated-reader-output"}
            plan.update(inputs=inputs, business_output=reference)
            body.update(operation=operation, inputs=deepcopy(inputs))
            definition["interface"]["business_output"] = deepcopy(reference)
        else:
            plan["request"] = {}
        transport = {"operation": operation, "output": output,
                     "operation_plan": {"operation": operation, "plan": plan},
                     "payload": {"messages": [{"role": "system", "content": "Original private protocol"},
                                              {"role": "user", "content": json.dumps(body, ensure_ascii=False)}]}}
        return transport, definition, body

    def test_three_protocols_do_not_query_rebuild_mutate_or_repeat_text(self):
        for operation, count in (("visual_rules.generate", 2), ("script.generate", 6), ("script.structure", 4)):
            t, d, body = self.sample(operation)
            before = deepcopy((t, d))
            result = native_input_trace(t, d)
            self.assertEqual(result["state"], "matched")
            self.assertEqual(len(result["rows"]), count)
            self.assertEqual(result["unmapped_count"], 0)
            self.assertNotIn("主题 中文", json.dumps(result, ensure_ascii=False))
            self.assertLess(len(json.dumps(result, ensure_ascii=False).encode()), 4096)
            self.assertEqual((t, d), before)

    def test_missing_null_false_zero_and_numeric_types_are_not_equal(self):
        for value in (None, False, 0, "1", 1.0):
            t, d, body = self.sample()
            body["inputs"]["episode_count"] = value
            t["payload"]["messages"][1]["content"] = json.dumps(body)
            result = native_input_trace(t, d)
            self.assertEqual(result["state"], "mismatch", value)
        t, d, body = self.sample()
        del body["inputs"]["style_hint"]
        t["payload"]["messages"][1]["content"] = json.dumps(body)
        self.assertEqual(native_input_trace(t, d)["state"], "mismatch")

    def test_extra_fields_are_counted_without_echoing_sensitive_names_or_values(self):
        t, d, body = self.sample("visual_rules.generate")
        body["private_key"] = "secret"
        body["prompt_profile"]["unexplained"] = "script content"
        t["payload"]["messages"][1]["content"] = json.dumps(body)
        result = native_input_trace(t, d)
        self.assertEqual(result["state"], "partial")
        self.assertEqual(result["unmapped_count"], 2)
        self.assertNotIn("private_key", json.dumps(result))

    def test_historical_audit_copy_is_only_compared_when_actually_sent(self):
        t, d, body = self.sample("visual_rules.generate")
        body["prompt_profile"]["structured_fields"] = {"theme": "主题 中文😀"}
        t["payload"]["messages"][1]["content"] = json.dumps(body)
        result = native_input_trace(t, d)
        self.assertEqual(result["state"], "matched")
        self.assertEqual(len(result["rows"]), 3)
        del t["operation_plan"]["plan"]["prompt_profile_assembly"]["structured_fields"]
        self.assertEqual(native_input_trace(t, d)["state"], "partial")

    def test_optional_planning_is_exact_and_never_read_from_current_configuration(self):
        t, d, body = self.sample("script.structure")
        planning = {"episodes": [{"number": 1, "enabled": False}]}
        t["operation_plan"]["plan"]["inputs"]["planning"] = deepcopy(planning)
        body["inputs"]["planning"] = planning
        t["payload"]["messages"][1]["content"] = json.dumps(body)
        self.assertEqual(native_input_trace(t, d)["state"], "matched")
        t["operation_plan"]["plan"]["inputs"]["planning"]["episodes"][0]["enabled"] = 0
        self.assertEqual(native_input_trace(t, d)["state"], "mismatch")
        del t["operation_plan"]["plan"]["inputs"]["planning"]
        self.assertEqual(native_input_trace(t, d)["state"], "partial")

    def test_invalid_identity_messages_duplicate_json_and_non_finite_are_unavailable(self):
        for change in ("operation", "adapter", "plan", "roles", "duplicate", "nan", "array", "profile"):
            t, d, body = self.sample()
            if change == "operation": d["configuration"]["operation"] = "visual_rules.generate"
            elif change == "adapter": d["interface"]["output"]["adapter"] = "unknown"
            elif change == "plan": t["operation_plan"] = None
            elif change == "roles": t["payload"]["messages"].append(t["payload"]["messages"][1])
            else:
                t["payload"]["messages"][1]["content"] = {
                    "duplicate": '{"prompt_profile":{"x":1,"x":2}}', "nan": '{"x":NaN}',
                    "array": '[]', "profile": '{"prompt_profile":null}'}[change]
            self.assertEqual(native_input_trace(t, d)["state"], "unavailable", change)
        self.assertIsNone(native_input_trace({"operation": "prompt.draft"}, {}))

    def test_missing_channel_stays_partial_instead_of_defaulting_to_empty(self):
        t, d, _ = self.sample("visual_rules.generate")
        del t["operation_plan"]["plan"]["prompt_profile_assembly"]["channels"]["negative_prompt"]
        self.assertEqual(native_input_trace(t, d)["rows"][-1]["state"], "unavailable")

class NativeInputReaderTests(TestCase):
    sample = NativeInputTraceTests.sample

    def test_task_reader_retains_original_messages_when_trace_cannot_be_verified(self):
        from studio.task_center_details import inspect_input
        t, d, _ = self.sample()
        # The parent frozen reader normally verifies this required declaration.
        d["configuration"]["inputs"] = []
        t.update(parameters={"values": {}}, prompt_channels={"values": {}}, driver_type="deepseek_api")
        del t["operation_plan"]
        detail = SimpleNamespace(intent={"request": {"requested": {}}}, resolved_inputs={})
        with patch("studio.task_center_details.read_frozen_execution_request", return_value=(detail, SimpleNamespace(value=d), SimpleNamespace(value=t))):
            result = inspect_input(SimpleNamespace(request_frozen_at=True))
        self.assertEqual(result["native_input_trace"]["state"], "unavailable")
        self.assertEqual(result["state"], "frozen")
        self.assertEqual(result["prompts"][1]["text"], t["payload"]["messages"][1]["content"])


class NormalizationInputTraceTests(SimpleTestCase):
    def sample(self):
        from hashlib import sha256
        token = "@旅人"
        placeholder = "__AUTOPROMPT_PROTECTED_" + sha256(("prompt-normalization-protected-token-v1\0" + token).encode()).hexdigest()[:24] + "__"
        item = {"request_id": "one", "text": "  @旅人 waits.  ", "source_lang": "Chinese", "target_lang": "English",
                "field_key": "action", "label": "@旅人 动作", "context": "保留 @旅人", "protected_tokens": [token]}
        other = {**item, "request_id": "two", "text": "@旅人 smiles.", "protected_tokens": []}
        channels = {"system_instruction": "  保留 @旅人  ", "positive_prompt": "@旅人", "negative_prompt": ""}
        structured = {"nested": ["@旅人", {"flag": False, "count": 0, "empty": None}]}
        assembly = {"channels": channels, "structured_fields": structured, "profile_key": "policy", "version": 1}
        request = {"schema_version": 1, "items": [item, other]}
        # Independent expected message: cross-item tokens are projected globally,
        # but each item's protected_tokens list remains its own ordered list.
        wire_items = [{key: [placeholder] if key == "protected_tokens" and value else
                       value.replace(token, placeholder) if isinstance(value, str) else deepcopy(value)
                       for key, value in source.items()} for source in request["items"]]
        profile = {"profile_key": "policy", "version": 1, "system_constraints_as_data": "  保留 " + placeholder + "  ",
                   "positive_prompt": placeholder, "negative_prompt": "",
                   "structured_fields": {"nested": [placeholder, {"flag": False, "count": 0, "empty": None}]}}
        body = {"schema_version": 1, "task": "translate_prompt_fields", "items": wire_items,
                "response_skeleton": {"schema_version": 1, "items": [{"request_id": key, "translation": ""} for key in ("one", "two")]},
                "prompt_profile": profile}
        output = {"adapter": "text.prompt_normalize/v1", "kind": "text", "schema_version": 1}
        transport = {"operation": "prompt.normalize", "output": output, "prompt_channels": {"values": deepcopy(channels)},
                     "operation_plan": {"operation": "prompt.normalize", "plan": {"schema_version": 1, "request": request, "prompt_profile_assembly": assembly}},
                     "payload": {"messages": [{"role": "system", "content": "original platform protocol"},
                                              {"role": "user", "content": json.dumps(body, ensure_ascii=False)}]}}
        definition = {"configuration": {"operation": "prompt.normalize"}, "interface": {"output": deepcopy(output)}}
        return transport, definition, body

    def test_whole_batch_and_nested_profile_are_checked_without_queries_or_body_echo(self):
        t, d, _ = self.sample()
        before = deepcopy((t, d))
        with patch("studio.services.execution_normalization_text.normalization_payload", side_effect=AssertionError("no request rebuild")), \
                patch("studio.services.prompt_normalization_contract._generic_fact_projection_max_depth", side_effect=AssertionError("no current limits")), \
                patch("studio.services.provider_registry._request_json", side_effect=AssertionError("no network")):
            result = native_input_trace(t, d)
        self.assertEqual(result["state"], "matched")
        self.assertEqual(len(result["rows"]), 10)
        self.assertTrue(all(row["state"] == "matched" for row in result["rows"]))
        self.assertEqual(result["unmapped_count"], 0)
        self.assertEqual((t, d), before)
        encoded = json.dumps(result, ensure_ascii=False)
        self.assertNotIn("@旅人", encoded)
        self.assertNotIn("__AUTOPROMPT_PROTECTED_", encoded)
        self.assertLess(len(encoded.encode()), 4096)
        self.assertIn("不是模型输出", encoded)

    def test_changes_anywhere_in_batch_skeleton_and_profile_are_not_hidden(self):
        for mutation in ("order", "late_text", "language", "id", "tokens", "nested_extra", "root_type", "task",
                         "skeleton_order", "skeleton_value", "system", "nested_type", "version_type", "drop_items"):
            t, d, body = self.sample()
            if mutation == "order": body["items"].reverse()
            elif mutation == "late_text": body["items"][-1]["text"] += "changed"
            elif mutation == "language": body["items"][-1]["target_lang"] = None
            elif mutation == "id": body["items"][-1]["request_id"] = "another"
            elif mutation == "tokens": body["items"][0]["protected_tokens"] = ["@旅人"]
            elif mutation == "nested_extra": body["items"][-1]["secret_extra"] = "unknown"
            elif mutation == "root_type": body["schema_version"] = True
            elif mutation == "task": body["task"] = "generate_or_normalize_prompt_fields"
            elif mutation == "skeleton_order": body["response_skeleton"]["items"].reverse()
            elif mutation == "skeleton_value": body["response_skeleton"]["items"][0]["translation"] = "not an output"
            elif mutation == "system": body["prompt_profile"]["system_constraints_as_data"] = body["prompt_profile"]["system_constraints_as_data"].strip()
            elif mutation == "nested_type": body["prompt_profile"]["structured_fields"]["nested"][1]["flag"] = 0
            elif mutation == "version_type": body["prompt_profile"]["version"] = 1.0
            else: del body["items"]
            t["payload"]["messages"][1]["content"] = json.dumps(body)
            self.assertEqual(native_input_trace(t, d)["state"], "mismatch", mutation)

    def test_missing_sources_and_unknown_fields_stay_partial(self):
        t, d, body = self.sample()
        del t["operation_plan"]["plan"]["prompt_profile_assembly"]["channels"]["negative_prompt"]
        del t["prompt_channels"]["values"]["negative_prompt"]
        body["secret_name"] = "secret value"
        body["prompt_profile"]["another_private_key"] = "unknown"
        t["payload"]["messages"][1]["content"] = json.dumps(body)
        result = native_input_trace(t, d)
        self.assertEqual(result["state"], "partial")
        self.assertEqual(result["unmapped_count"], 2)
        self.assertEqual(result["rows"][-2]["state"], "unavailable")
        self.assertNotIn("secret", json.dumps(result))

    def test_malformed_frozen_items_or_unsupported_context_are_unavailable(self):
        for mutation in ("duplicate_id", "empty_id", "duplicate_token", "bad_token", "empty_token", "item_type", "version",
                         "extra_context", "channels", "token_key", "existing_placeholder", "depth"):
            t, d, body = self.sample()
            plan = t["operation_plan"]["plan"]
            item = plan["request"]["items"][0]
            if mutation == "duplicate_id": plan["request"]["items"][-1]["request_id"] = item["request_id"]
            elif mutation == "empty_id": item["request_id"] = ""
            elif mutation == "duplicate_token": item["protected_tokens"] *= 2
            elif mutation == "bad_token": item["protected_tokens"] = [1]
            elif mutation == "empty_token": item["protected_tokens"] = [""]
            elif mutation == "item_type": item["text"] = 1
            elif mutation == "version": plan["request"]["schema_version"] = True
            elif mutation == "extra_context": plan["request"]["target_prompt_contract"] = {}
            elif mutation == "channels": t["prompt_channels"]["values"] = {}
            elif mutation == "token_key": plan["prompt_profile_assembly"]["structured_fields"] = {"@旅人": "value"}
            elif mutation == "existing_placeholder": item["text"] = body["items"][0]["text"]
            else:
                value = "leaf"
                for _ in range(66): value = [value]
                plan["prompt_profile_assembly"]["structured_fields"] = value
            result = native_input_trace(t, d)
            self.assertEqual(result["state"], "unavailable", mutation)
            self.assertEqual(result["rows"], [], mutation)

    def test_output_size_does_not_grow_with_batch_size(self):
        t, d, body = self.sample()
        for n in range(200):
            identifier = f"item-{n}"
            t["operation_plan"]["plan"]["request"]["items"].append({**deepcopy(t["operation_plan"]["plan"]["request"]["items"][0]), "request_id": identifier})
            body["items"].append({**deepcopy(body["items"][0]), "request_id": identifier})
            body["response_skeleton"]["items"].append({"request_id": identifier, "translation": ""})
        t["payload"]["messages"][1]["content"] = json.dumps(body)
        result = native_input_trace(t, d)
        self.assertEqual(result["state"], "matched")
        self.assertEqual(len(result["rows"]), 10)
        self.assertLess(len(json.dumps(result, ensure_ascii=False).encode()), 4096)
