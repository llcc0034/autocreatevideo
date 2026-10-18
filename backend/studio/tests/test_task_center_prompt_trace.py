from copy import deepcopy
import json
from hashlib import sha256
from unittest.mock import patch

from django.test import SimpleTestCase, TestCase

from studio.task_center_details import bound_content
from studio.task_center_prompt_trace import prompt_trace, _channel, protocol_notes


class ProtocolNotesTests(SimpleTestCase):
    def sample(self, operation="visual_rules.generate", adapter="text.visual_rules/v2"):
        output = {"adapter": adapter, "schema_version": 2, "kind": "text"}
        transport = {"operation": operation, "output": output, "payload": {"messages": [
            {"role": "system", "content": "original protocol, not rebuilt"},
            {"role": "user", "content": "original user data, not interpreted"}]}}
        definition = {"interface": {"output": deepcopy(output)}, "configuration": {"operation": operation}}
        return transport, definition

    def test_visual_notes_distinguish_protocol_policy_output_and_historical_body(self):
        transport, definition = self.sample()
        before = deepcopy((transport, definition))
        notes = " ".join(protocol_notes(transport, definition))
        for text in ("text.visual_rules/v2", "不是本次正文校验", "CONFIGURED_SYSTEM_INSTRUCTION",
                     "结构化字段", "业务输出版本", "不能据当前配置断言旧任务未发送全文", "后继版本"):
            self.assertIn(text, notes)
        self.assertEqual((transport, definition), before)

    def test_each_registered_chat_protocol_explains_its_own_role_rules(self):
        from studio.services.execution_field_contracts import FIELD_OPERATION_MODES
        rows = [("prompt.draft", "text.prompt_draft/v1", "没有额外拼接"),
                ("script.generate", "text.script_generate/v2", "user.inputs"),
                ("script.structure", "text.script_structure/v3", "原稿全文"),
                ("prompt.normalize", "text.prompt_normalize/v1", "system_constraints_as_data")]
        rows += [(op, "text.prompt_fields/v1", "父提示词策略") for op in FIELD_OPERATION_MODES]
        for operation, adapter, explanation in rows:
            with self.subTest(operation=operation):
                transport, definition = self.sample(operation, adapter)
                notes = " ".join(protocol_notes(transport, definition))
                self.assertIn(explanation, notes)
                self.assertNotIn("说明不可用", notes)

    def test_missing_cross_version_unknown_and_malformed_identity_fail_closed(self):
        for change in ("operation", "adapter", "definition", "version", "missing"):
            transport, definition = self.sample()
            if change == "operation": transport["operation"] = {}
            elif change == "adapter": transport["output"]["adapter"] = {}
            elif change == "definition": definition["configuration"]["operation"] = "script.generate"
            elif change == "version":
                transport["output"]["adapter"] = definition["interface"]["output"]["adapter"] = "text.visual_rules/v3"
            else: definition.clear()
            notes = protocol_notes(transport, definition)
            self.assertEqual(len(notes), 1, change)
            self.assertIn("说明不可用", notes[0], change)

    def test_malformed_duplicate_nontext_or_reordered_messages_do_not_get_guide(self):
        for messages in (None, [], [None], [{"role": "system", "content": {}}],
                         [{"role": role, "content": "text"} for role in ("user", "system")],
                         [{"role": role, "content": "text"} for role in ("system", "user", "user")]):
            transport, definition = self.sample()
            transport["payload"]["messages"] = messages
            self.assertIn("说明不可用", protocol_notes(transport, definition)[0])

    def test_draft_may_have_only_user_but_native_protocol_cannot(self):
        for op, adapter in (("prompt.draft", "text.prompt_draft/v1"), ("visual_rules.generate", "text.visual_rules/v2")):
            transport, definition = self.sample(op, adapter)
            transport["payload"]["messages"].pop(0)
            notes = " ".join(protocol_notes(transport, definition))
            self.assertEqual("说明不可用" in notes, op != "prompt.draft")

    def test_media_without_messages_has_no_invented_chat_roles(self):
        self.assertEqual(protocol_notes({"payload": {"prompt": "media"}}, {}), [])
from studio.task_center_serializers import TaskCenterDetailQuerySerializer


class PromptTraceTests(TestCase):
    def sample(self):
        channels = {"system_instruction": "  obey policy  ", "positive_prompt": "Theme: 中文😀", "negative_prompt": ""}
        rows = [{"field_key": "theme", "source_selector": "project.logline", "channel": "positive_prompt", "defaulted": False}]
        assembly = {"schema_version": 1, "prompt_profile_version_id": "profile", "contract_sha256": "a"*64,
                    "channels": channels, "trace": rows, "field_rules": deepcopy(rows), "structured_fields": {"theme": "中文😀"}}
        definition = {"interface": {"prompt": {"mode": "profile", "profile_id": "profile", "profile_sha256": "a"*64}},
                      "configuration": {"inputs": [{"key": "different-input-name", "targets": [
                          {"target_kind": "profile_input", "target_key": "project.logline"}]}]}}
        transport = {"operation": "visual_rules.generate", "output": {"adapter": "text.visual_rules/v2"},
                     "profile_compilation": {"assembly": assembly}, "prompt_channels": {"values": channels},
                     "payload": {"messages": [{"role": "system", "content": "Protocol\nCONFIGURED_SYSTEM_INSTRUCTION=obey policy"},
                                               {"role": "user", "content": json.dumps({"prompt_profile": {"positive_prompt": channels["positive_prompt"], "negative_prompt": ""}})}]}}
        source = {"input_key": "different-input-name", "kind": "project_snapshot", "source_reference": {"source_id": "project", "location": "项目设定 → 一句话梗概"},
                  "value_sha256": "b"*64, "edited": False}
        resolved = {"inputs": {"input_provenance": [source]}}
        return transport, definition, resolved

    def test_frozen_fields_channels_and_explicit_source_mapping(self):
        t, d, r = self.sample()
        before = deepcopy((t, d, r))
        result = prompt_trace(t, d, r)
        self.assertEqual(result["state"], "recorded")
        self.assertEqual([row["state"] for row in result["channels"]], ["matched"]*3)
        self.assertEqual(result["channels"][1]["characters"], len("Theme: 中文😀"))
        self.assertIn("JSON.prompt_profile.positive_prompt", result["channels"][1]["destination"])
        self.assertEqual(result["fields"][0]["sources"][0]["input_key"], "different-input-name")
        self.assertEqual(result["fields"][0]["sources"][0]["location"], "项目设定 → 一句话梗概")
        self.assertEqual((t, d, r), before)

    def test_no_same_name_guess_and_default_or_review_overrides_source(self):
        for flag in ("defaulted", "reviewed", "no_target"):
            t, d, r = self.sample()
            if flag == "no_target":
                d["configuration"]["inputs"][0]["targets"] = []
                r["inputs"]["input_provenance"][0]["input_key"] = "project.logline"
            else:
                t["profile_compilation"]["assembly"]["trace"][0][flag] = True
            self.assertEqual(prompt_trace(t, d, r)["fields"][0]["sources"], [])

    def test_incomplete_or_cross_profile_audit_is_unavailable(self):
        for change in ("profile", "sha", "channel", "trace", "boolean_version", "missing", "duplicate", "type"):
            t, d, r = self.sample()
            a = t["profile_compilation"]["assembly"]
            if change == "profile": a["prompt_profile_version_id"] = "other"
            elif change == "sha": a["contract_sha256"] = "c"*64
            elif change == "channel": t["prompt_channels"] = {"values": {"positive_prompt": "different"}}
            elif change == "trace": a["trace"][0]["source_selector"] = "other"
            elif change == "boolean_version": a["schema_version"] = True
            elif change == "missing": t["profile_compilation"] = None
            elif change == "duplicate": a["trace"] *= 2
            else: a["trace"][0]["defaulted"] = 0
            result = prompt_trace(t, d, r)
            self.assertEqual(result["state"], "unavailable", change)
            self.assertEqual(result["fields"], [])

    def test_channel_disagreement_does_not_guess_or_rewrite_actual_messages(self):
        for content in ('{"prompt_profile":{"positive_prompt":"wrong","negative_prompt":""}}',
                        '{"prompt_profile":{"positive_prompt":"wrong","positive_prompt":"Theme: 中文😀"}}',
                        'Theme: 中文😀', '[]'):
            t, d, r = self.sample()
            t["payload"]["messages"][1]["content"] = content
            result = prompt_trace(t, d, r)
            self.assertEqual(result["state"], "recorded")
            self.assertEqual(result["channels"][1]["state"], "mismatch")
            self.assertEqual(t["payload"]["messages"][1]["content"], content)
        t, d, r = self.sample()
        t["payload"]["messages"].append(deepcopy(t["payload"]["messages"][0]))
        self.assertEqual(prompt_trace(t, d, r)["channels"][0]["state"], "mismatch")

    def test_unimplemented_protocol_and_direct_chat_have_distinct_evidence(self):
        t, d, r = self.sample()
        t["output"]["adapter"] = "text.visual_rules/v999"
        self.assertTrue(all(row["state"] == "unavailable" for row in prompt_trace(t, d, r)["channels"]))
        t["operation"], t["output"]["adapter"] = "prompt.draft", "text.prompt_draft/v1"
        values = t["prompt_channels"]["values"]
        t["payload"]["messages"] = [{"role": "system", "content": values["system_instruction"]}, {"role": "user", "content": values["positive_prompt"]}]
        self.assertEqual([row["state"] for row in prompt_trace(t, d, r)["channels"]], ["matched", "matched", "unavailable"])

    def test_redaction_and_empty_system_no_network_or_current_lookup(self):
        t, d, r = self.sample()
        r["inputs"]["input_provenance"][0]["source_reference"]["location"] = "https://private.example/secret password=TOP_SECRET"
        result = prompt_trace(t, d, r)
        self.assertNotIn("private.example", json.dumps(result))
        self.assertNotIn("TOP_SECRET", json.dumps(result))
        t["prompt_channels"]["values"]["system_instruction"] = ""
        self.assertEqual(prompt_trace(t, d, r)["channels"][0]["state"], "omitted")

    def test_large_trace_uses_existing_bounded_field_reader(self):
        t, d, r = self.sample()
        trace = prompt_trace(t, d, r)
        trace["fields"] *= 500
        detail = {"input": {"state": "frozen", "submitted": {}, "resolved": {}, "parameters": [], "prompts": [], "prompt_trace": trace, "field_batch_trace": None, "media_trace": None, "notes": []},
                  "output": {"state": "available", "result": None, "text": "", "media": [], "notes": []}}
        query = TaskCenterDetailQuerySerializer(data={"content_field": "input.prompt_trace", "content_limit": 1024})
        self.assertTrue(query.is_valid())
        result = bound_content(detail, query.validated_data)
        self.assertIsNone(result["input"]["prompt_trace"])
        self.assertEqual(result["deferred_fields"][0]["field"], "input.prompt_trace")
        self.assertLessEqual(len(result["content"]["text"].encode()), 1024)
        self.assertGreater(result["content"]["next_offset"], 0)

    def structure_sample(self, planning, operation="script.structure"):
        from studio.services.script_semantic_requests import build_script_semantic_request
        from studio.tests.script_semantics_fixtures import script_example
        from studio.tests.test_script_semantic_requests import reference, LIMIT
        t, d, r = self.sample()
        _, schema, mapping = script_example()
        output = reference(schema, mapping)
        inputs = {"source_text": "旅人进门。"} if operation == "script.structure" else {"idea": "旅人进门。"}
        if planning is not None:
            inputs["planning"] = planning
        payload, envelope = build_script_semantic_request(operation=operation, output_reference=output,
            inputs=inputs, parameters={"max_tokens": 4096, "seed": 42},
            transport_policy={"timeout_seconds": 30, "chat_request_options": {"temperature": 0.2}},
            prompt_profile_assembly=deepcopy(t["profile_compilation"]["assembly"]),
            plan_max_bytes=LIMIT, request_max_bytes=LIMIT)
        adapter = "text.script_structure/v3" if operation == "script.structure" else "text.script_generate/v2"
        t.update(operation=operation, output={"adapter": adapter}, operation_plan=envelope, payload=payload)
        d["configuration"]["operation"] = operation
        d["interface"].update(output=deepcopy(t["output"]), business_output=output)
        return t, d, r

    def test_actual_structure_builder_with_and_without_optional_planning(self):
        for planning in (None, {"episode_count": 1, "shots_per_episode": 1, "episode_duration_seconds": 2}):
            t, d, r = self.structure_sample(planning)
            before = deepcopy(t)
            trace = prompt_trace(t, d, r)
            self.assertEqual(trace["state"], "recorded")
            self.assertEqual([row["state"] for row in trace["channels"]], ["matched"]*3)
            self.assertIn("system_message_layout.strategy", trace["channels"][0]["destination"])
            self.assertEqual(t, before)
            t["payload"]["messages"][0]["content"] += " changed"
            self.assertEqual(prompt_trace(t, d, r)["channels"][0]["state"], "unavailable")

    def test_structure_missing_or_conflicting_plan_is_not_guessed(self):
        t, d, r = self.structure_sample({"episode_count": 1, "shots_per_episode": 1, "episode_duration_seconds": 2})
        plan = t.pop("operation_plan")
        self.assertEqual(prompt_trace(t, d, r)["channels"][0]["state"], "unavailable")
        t["operation_plan"] = plan
        del plan["plan"]["business_output"]
        self.assertEqual(prompt_trace(t, d, r)["channels"][0]["state"], "unavailable")

    def normalization_sample(self):
        from studio.services.execution_normalization_text import normalization_payload
        t, d, r = self.sample()
        t["prompt_channels"]["values"].update(system_instruction="  保留 @旅人  ", positive_prompt="@旅人 turns. @旅人 waits.")
        item = {"request_id": "one", "text": "@旅人 回家。", "protected_tokens": ["@旅人"],
                "source_lang": "Chinese", "target_lang": "English", "field_key": "action", "label": "动作", "context": ""}
        plan = {"schema_version": 1, "request": {"schema_version": 1, "items": [item]}, "seed": 42,
                "output_length": 4096, "policy": {"max_input_bytes": 29000, "max_items_per_batch": 2,
                    "timeout_seconds": 30, "chat_request_options": {"temperature": 0.2}},
                "prompt_profile_assembly": deepcopy(t["profile_compilation"]["assembly"])}
        t.update(operation="prompt.normalize", output={"adapter": "text.prompt_normalize/v1"},
                 operation_plan={"operation": "prompt.normalize", "plan": plan}, payload=normalization_payload(plan))
        return t, d, r

    def test_actual_normalization_builder_preserves_whitespace_and_protected_projection(self):
        t, d, r = self.normalization_sample()
        before = deepcopy(t)
        trace = prompt_trace(t, d, r)
        self.assertEqual([row["state"] for row in trace["channels"]], ["matched"]*3)
        self.assertIn("messages.1.content → JSON.prompt_profile.system_constraints_as_data", trace["channels"][0]["destination"])
        self.assertIn("保护词转换", trace["channels"][0]["message"])
        self.assertEqual(t, before)
        wire = json.loads(t["payload"]["messages"][1]["content"])
        self.assertIn("__AUTOPROMPT_PROTECTED_", wire["prompt_profile"]["positive_prompt"])
        wire["prompt_profile"]["system_constraints_as_data"] = wire["prompt_profile"]["system_constraints_as_data"].strip()
        t["payload"]["messages"][1]["content"] = json.dumps(wire)
        self.assertEqual(prompt_trace(t, d, r)["channels"][0]["state"], "mismatch")

    def test_normalization_bad_frozen_plan_and_unsupported_context_are_explicit(self):
        for mutation in ("missing", "operation", "version", "extra", "channels", "empty_token", "bad_token"):
            t, d, r = self.normalization_sample()
            plan = t["operation_plan"]["plan"]
            if mutation == "missing": del t["operation_plan"]
            elif mutation == "operation": t["operation_plan"]["operation"] = "script.generate"
            elif mutation == "version": plan["request"]["schema_version"] = True
            elif mutation == "extra": plan["request"]["target_prompt_contract"] = {}
            elif mutation == "channels": plan["prompt_profile_assembly"]["channels"] = {}
            else: plan["request"]["items"][0]["protected_tokens"] = ["" if mutation == "empty_token" else 1]
            self.assertTrue(all(row["state"] == "unavailable" for row in prompt_trace(t, d, r)["channels"]), mutation)

    def test_channel_inspection_never_rebuilds_requests_or_reads_current_config(self):
        samples = [self.structure_sample(None), self.normalization_sample()]
        with patch("studio.services.execution_native_text.build_native_text_request", side_effect=AssertionError("no rebuild")), \
                patch("studio.services.provider_registry._request_json", side_effect=AssertionError("no network")), self.assertNumQueries(0):
            for t, d, _ in samples:
                for channel, value in t["prompt_channels"]["values"].items():
                    self.assertEqual(_channel(t, channel, value, d)["state"], "matched")

    def test_script_generation_and_wrong_adapter_are_distinguished(self):
        t, d, r = self.structure_sample(None, operation="script.generate")
        self.assertEqual([row["state"] for row in prompt_trace(t, d, r)["channels"]], ["matched"]*3)
        t["output"]["adapter"] = "text.script_structure/v3"
        self.assertTrue(all(row["state"] == "unavailable" for row in prompt_trace(t, d, r)["channels"]))

    def media_sample(self, driver):
        t, d, r = self.sample()
        values = t["prompt_channels"]["values"]
        del values["system_instruction"]
        keys = {"positive_prompt": "prompt.positive", "negative_prompt": "prompt.negative"} if driver == "comfyui" else {
            "positive_prompt": "prompt", "negative_prompt": "negative_prompt"}
        d["interface"]["prompt"]["channels"] = {name: {"semantic_key": key, "rule": {"type": "string"}} for name, key in keys.items()}
        d["configuration"]["operation"] = "video.generate"
        d["target"] = {"driver_type": driver, "workflow": None}
        t.update(driver_type=driver, operation="video.generate")
        if driver == "comfyui":
            d["target"]["workflow"] = {"id": "frozen-workflow", "compiler_id": "generic.comfy", "compiler_version": 3,
                "execution_contract_sha256": "e" * 64}
            t["output"] = {"adapter": {"key": "fixture.projector", "version": 1}, "contract": {"primary": {"kind": "video"}}}
            t["payload"] = {"compiled_workflow": {"compiler_id": "generic.comfy", "compiler_version": 3,
                "workflow_contract_sha256": "e" * 64, "resolved_semantics": {keys[k]: v for k, v in values.items()}}}
        else:
            t["output"] = {"adapter": "async_rest", "kind": "video"}
            t["payload"] = {keys[k]: v for k, v in values.items()}
        d["interface"]["output"] = deepcopy(t["output"])
        return t, d, r

    def test_media_channels_use_explicit_frozen_mapping_without_rebuilding(self):
        with patch("studio.services.execution_request_compilation.compile_execution_transport_request", side_effect=AssertionError("no compile")), \
                patch("studio.services.execution_identity.execution_target_contract", side_effect=AssertionError("no live target")):
            for driver, destination in (("comfyui", 'payload.compiled_workflow.resolved_semantics["prompt.positive"]'),
                                        ("async_rest", 'payload["prompt"]')):
                t, d, r = self.media_sample(driver)
                before = deepcopy((t, d, r))
                result = prompt_trace(t, d, r)
                # Public redaction intentionally reads current security caps;
                # only channel comparison must be a zero-query projection.
                with self.assertNumQueries(0):
                    checked = [_channel(t, k, v, d) for k, v in t["prompt_channels"]["values"].items()]
                self.assertEqual(result["channels"], checked)
                self.assertEqual(result["state"], "recorded")
                self.assertEqual([row["state"] for row in result["channels"]], ["matched", "matched"])
                self.assertEqual(result["channels"][0]["destination"], destination)
                self.assertEqual(result["channels"][1]["characters"], 0)
                self.assertIn("不证明", result["channels"][0]["message"])
                self.assertEqual((t, d, r), before)

    def test_media_missing_or_different_values_are_not_inferred_from_similar_text(self):
        for driver in ("comfyui", "async_rest"):
            for value in (None, "Theme: 中文😀 ", ["Theme: 中文😀"], False):
                t, d, r = self.media_sample(driver)
                container = t["payload"]["compiled_workflow"]["resolved_semantics"] if driver == "comfyui" else t["payload"]
                key = d["interface"]["prompt"]["channels"]["positive_prompt"]["semantic_key"]
                if value is None: del container[key]
                else: container[key] = value
                container["positive_prompt"] = "Theme: 中文😀"  # Same-name/equal text cannot substitute the declared slot.
                result = prompt_trace(t, d, r)
                self.assertEqual([row["state"] for row in result["channels"]], ["mismatch", "matched"])
                self.assertEqual(result["fields"][0]["source_selector"], "project.logline")

    def test_media_incomplete_or_cross_target_identity_is_unavailable_not_guessed(self):
        for driver in ("comfyui", "async_rest"):
            for change in ("missing", "key", "duplicate", "rule", "operation", "driver", "output"):
                t, d, r = self.media_sample(driver)
                channels = d["interface"]["prompt"]["channels"]
                if change == "missing": del channels["positive_prompt"]
                elif change == "key": channels["positive_prompt"]["semantic_key"] = "invented"
                elif change == "duplicate": channels["negative_prompt"]["semantic_key"] = channels["positive_prompt"]["semantic_key"]
                elif change == "rule": channels["positive_prompt"]["rule"]["type"] = "integer"
                elif change == "operation": d["configuration"]["operation"] = "another"
                elif change == "driver": d["target"]["driver_type"] = "another"
                else: d["interface"]["output"] = {}
                result = prompt_trace(t, d, r)
                self.assertEqual(result["state"], "recorded")
                self.assertEqual(result["channels"][0]["state"], "unavailable", (driver, change))
        for key, value in (("compiler_version", True), ("compiler_version", 99), ("compiler_id", "unknown"),
                           ("workflow_contract_sha256", "different")):
            t, d, r = self.media_sample("comfyui")
            t["payload"]["compiled_workflow"][key] = value
            self.assertTrue(all(c["state"] == "unavailable" for c in prompt_trace(t, d, r)["channels"]))

    def test_unknown_dictionary_adapter_does_not_hide_valid_field_history(self):
        t, d, r = self.sample()
        t["output"]["adapter"] = {"unsupported": "adapter"}
        result = prompt_trace(t, d, r)
        self.assertEqual(result["state"], "recorded")
        self.assertTrue(all(c["state"] == "unavailable" for c in result["channels"]))
        self.assertEqual(len(result["fields"]), 1)
