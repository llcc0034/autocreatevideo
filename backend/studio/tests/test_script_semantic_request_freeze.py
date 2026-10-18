"""Isolated unsent transport contracts, not native registry/worker acceptance."""
from copy import deepcopy
from hashlib import sha256
import json
from unittest.mock import patch
from uuid import uuid4

from django.core.exceptions import ValidationError
from django.test import SimpleTestCase

from studio.services.chat_body import assemble_chat_body, validate_chat_body_layout
from studio.services.execution_output_contracts import verify_operation_output
from studio.services.json_contracts import canonical_json_bytes
from studio.services.script_semantic_requests import build_script_semantic_request, verify_script_semantic_request
from studio.services.script_sources import verify_script_text_source
from studio.tests.script_semantics_fixtures import script_example
from studio.tests.test_script_semantic_requests import LIMIT, reference


class ScriptSemanticRequestFreezeTests(SimpleTestCase):
    def setUp(self):
        _, source, mapping = script_example()
        self.reference = reference(source, mapping)
        self.assembly = {"channels": {"system_instruction": "服从已选字段定义", "positive_prompt": "本次要求",
            "negative_prompt": "不改对白"}, "structured_fields": {"audit_only": "不再重复组装"}}
        self.policy = {"timeout_seconds": 31.5, "chat_request_options": {
            "temperature": 0, "response_format": {"type": "json_object"}, "extra_body": {"enable_thinking": False}}}
        # Only original configurable size/depth limits are isolated; the real
        # Chat validator/assembler run. No driver/provider call is made.
        self.limits = patch("studio.services.model_profile_contracts._chat_request_option_limits", return_value={
            "max_bytes": LIMIT, "max_depth": 12, "max_fields": 100, "max_array_items": 100, "max_string_characters": 10000})
        self.limits.start()
        self.addCleanup(self.limits.stop)

    def build(self, **changes):
        return build_script_semantic_request(**{"operation": "script.generate", "output_reference": self.reference,
            "inputs": {"idea": "本次创意", "episode_count": 2, "shots_per_episode": 2, "episode_duration_seconds": 10,
                "style_hint": "原始创作要求"}, "parameters": {"max_tokens": 2048, "seed": 0},
            "transport_policy": self.policy, "prompt_profile_assembly": self.assembly,
            "plan_max_bytes": LIMIT, "request_max_bytes": LIMIT, **changes})

    def verify(self, payload, envelope, **changes):
        return verify_script_semantic_request(envelope, payload,
            **{"max_bytes": LIMIT, "expected_output": self.reference, **changes})

    def test_exact_output_plan_survives_json_reload_and_projects_real_chat_body(self):
        payload, envelope = self.build()
        payload, envelope = json.loads(json.dumps([payload, envelope]))
        self.assertEqual(self.verify(payload, envelope), self.reference)
        body, layout = assemble_chat_body("isolated-fixture-model", payload)
        validate_chat_body_layout(layout, payload)
        self.assertEqual(body["model"], "isolated-fixture-model")
        self.assertEqual(body["messages"], payload["messages"])
        self.assertEqual(body["max_tokens"], 2048)
        self.assertEqual(body["seed"], 0)
        self.assertIs(body["extra_body"]["enable_thinking"], False)
        self.assertEqual(body["temperature"], 0)
        self.assertNotIn("timeout_seconds", body)
        self.assertEqual(layout["transport_only"], ["timeout_seconds"])
        for private in ("business_output", "system_message_layout", "structured_fields"):
            self.assertNotIn(private, body)
        self.assertNotIn("不再重复组装", str(body))

    def test_both_response_layouts_build_different_requests_from_saved_output(self):
        requests = []
        for variant in ("a", "b"):
            _, source, mapping = script_example(variant)
            output = reference(source, mapping)
            payload, plan = self.build(output_reference=output)
            self.assertEqual(self.verify(payload, plan, expected_output=output), output)
            self.assertEqual(plan["plan"]["business_output"]["parser"]["mapping"], mapping)
            requests.append(payload["messages"][0]["content"])
        self.assertNotEqual(*requests)
        self.assertIn('"motion"', requests[1])
        for old in ("FIELD_CONTRACT", "REFERENCE_SCHEMA=", "SCRIPT_BIBLE", "printable ASCII"):
            self.assertNotIn(old, "".join(requests))

    def test_json_schema_uses_output_structure_and_frozen_reader_checks_without_current_options(self):
        from studio.services.data_structures import validate_data_structure_snapshot
        policy = deepcopy(self.policy)
        policy["chat_request_options"]["response_format"] = {"type": "json_schema", "json_schema": {"name": "script_response", "strict": False}}
        original = deepcopy(policy)
        with patch("studio.services.execution_request_storage.execution_request_max_bytes", return_value=LIMIT):
            payload, envelope = self.build(transport_policy=policy)
            body, _ = assemble_chat_body("isolated-model", payload)
        self.assertEqual(policy, original)
        self.assertEqual(envelope["plan"]["transport_policy"], original)
        self.assertEqual(body["response_format"]["json_schema"]["schema"], validate_data_structure_snapshot(self.reference["data_structure"]))
        self.assertIs(body["response_format"]["json_schema"]["strict"], False)
        with patch("studio.services.model_profile_contracts._chat_request_option_limits", side_effect=AssertionError("No current limits")):
            self.assertEqual(self.verify(payload, envelope), self.reference)
            tampered = deepcopy(payload)
            tampered["chat_request_options"]["response_format"]["json_schema"]["schema"] = {"type": "string"}
            with self.assertRaises(ValidationError):
                self.verify(tampered, envelope)

    def test_literal_structure_preserves_whitespace_unicode_and_planning_without_revision(self):
        text = "  原稿\r\n第二行 😀\r最后一行  "
        inputs = {"source_text": text, "planning": {"episode_count": 2}}
        payload, envelope = self.build(operation="script.structure", inputs=inputs)
        plan = envelope["plan"]
        self.assertEqual(plan["source_kind"], "literal")
        self.assertEqual(plan["source_revision"], {"id": None, "source_id": None, "revision": None,
            "content_sha256": sha256(text.encode()).hexdigest(), "content_size_bytes": len(text.encode())})
        self.assertEqual(json.loads(payload["messages"][1]["content"])["inputs"], inputs)
        self.assertEqual(self.verify(payload, envelope), self.reference)

    def test_real_revision_identity_is_verified_not_inferred_or_sent_as_approval(self):
        text = "保持来源原文\r\n"
        source = {"id": str(uuid4()), "source_id": str(uuid4()), "revision": 3,
            "content_sha256": sha256(text.encode()).hexdigest(), "content_size_bytes": len(text.encode())}
        payload, envelope = self.build(operation="script.structure", inputs={"source_text": text}, source_reference=source)
        self.assertEqual(envelope["plan"]["source_kind"], "script_revision")
        self.assertEqual(envelope["plan"]["source_revision"], source)
        self.assertEqual(self.verify(payload, envelope), self.reference)
        self.assertNotIn(source["id"], str(payload))
        for changes in ({"content_sha256": "0" * 64}, {"content_size_bytes": 1}, {"revision": True},
                        {"id": "not-a-revision"}, {"source_id": None}):
            with self.subTest(changes=changes), self.assertRaises(ValidationError):
                self.build(operation="script.structure", inputs={"source_text": text}, source_reference={**source, **changes})
        with self.assertRaises(ValidationError):
            self.build(source_reference=source)
        with self.assertRaises(ValidationError):
            verify_script_text_source(text, source_reference=source, source_kind="literal")

    def test_invalid_source_never_normalizes_replaces_or_allocates_a_revision(self):
        for value in (None, "", " \r\n", 3, "bad\ud800"):
            with self.subTest(value=repr(value)), self.assertRaises(ValidationError):
                self.build(operation="script.structure", inputs={"source_text": value})

    def test_frozen_read_does_not_recompile_or_read_current_configuration(self):
        payload, plan = self.build()
        with patch("studio.services.script_semantic_requests.script_semantic_messages", side_effect=AssertionError("recompile")), \
             patch("studio.services.model_profile_contracts.validate_chat_request_options", side_effect=AssertionError("current policy")):
            self.assertEqual(self.verify(payload, plan), self.reference)

    def test_successor_cannot_replace_frozen_output_and_mutable_arguments_are_copied(self):
        payload, plan = self.build()
        saved = deepcopy((payload, plan))
        successor = reference(self.reference["data_structure"], self.reference["parser"]["mapping"],
            output_key=self.reference["output_key"], version=2)
        with self.assertRaises(ValidationError):
            self.verify(payload, plan, expected_output=successor)
        self.policy["chat_request_options"]["temperature"] = 9
        self.assembly["channels"]["positive_prompt"] = "later"
        self.assertEqual((payload, plan), saved)
        self.assertEqual(self.verify(payload, plan), self.reference)
        payload["chat_request_options"]["temperature"] = 3
        self.assertEqual(plan, saved[1])
        with self.assertRaises(ValidationError):
            self.verify(payload, plan)

    def test_old_policy_missing_fields_and_wrong_protocol_are_not_silently_projected(self):
        policies = ({}, {**self.policy, "field_contract": {}}, {**self.policy, "reference_policy": {}},
            {**self.policy, "max_output_tokens": 500}, {**self.policy, "timeout_seconds": 0},
            {**self.policy, "timeout_seconds": True}, {**self.policy, "timeout_seconds": float("nan")})
        for policy in policies:
            with self.subTest(policy=policy), self.assertRaises(ValidationError):
                self.build(transport_policy=policy)
        for changes in ({"output_reference": None}, {"operation": "text.structured"}, {"inputs": None}):
            with self.subTest(changes=changes), self.assertRaises(ValidationError):
                self.build(**changes)

    def test_original_chat_validator_rejects_transport_field_collisions(self):
        for options in ({"seed": 9}, {"max_tokens": 9}, {"messages": []}, {"api_key": "fixture-secret"}):
            with self.subTest(options=options), self.assertRaises(ValidationError):
                self.build(transport_policy={**self.policy, "chat_request_options": options})

    def test_request_and_plan_limits_check_complete_envelopes_not_just_messages(self):
        payload, envelope = self.build()
        request_size, plan_size = map(lambda v: len(canonical_json_bytes(v)), (payload, envelope))
        self.assertEqual(self.build(request_max_bytes=request_size, plan_max_bytes=plan_size), (payload, envelope))
        for changes in ({"request_max_bytes": request_size - 1}, {"plan_max_bytes": plan_size - 1},
                        {"request_max_bytes": True}, {"plan_max_bytes": 0}):
            with self.subTest(changes=changes), self.assertRaises(ValidationError):
                self.build(**changes)

    def test_frozen_reader_rejects_output_source_channel_parameter_and_receipt_drift(self):
        payload, envelope = self.build(operation="script.structure", inputs={"source_text": "原稿"})
        for change in ("seed", "budget", "timeout", "options", "source", "source_kind", "profile", "strategy", "user", "system",
                       "layout_gap", "layout_hash", "layout_section", "layout_missing", "unknown", "operation"):
            p, e = deepcopy((payload, envelope))
            plan = e["plan"]
            if change == "seed": p["seed"] = False
            elif change == "budget": p["max_tokens"] += 1
            elif change == "timeout": p["timeout_seconds"] += 1
            elif change == "options": p["chat_request_options"]["temperature"] = 1
            elif change == "source": plan["inputs"]["source_text"] += "变化"
            elif change == "source_kind": plan["source_kind"] = "guessed"
            elif change == "profile": plan["prompt_profile_assembly"]["channels"]["positive_prompt"] = "变化"
            elif change == "strategy": plan["prompt_profile_assembly"]["channels"]["system_instruction"] = "变化"
            elif change == "user": p["messages"][1]["content"] += "变化"
            elif change == "system": p["messages"][0]["content"] += "变化"
            elif change == "layout_gap": plan["system_message_layout"]["sections"][1]["start"] += 1
            elif change == "layout_hash": plan["system_message_layout"]["sha256"] = "0" * 64
            elif change == "layout_section": plan["system_message_layout"]["sections"][1]["key"] = "field_contract"
            elif change == "layout_missing": del plan["system_message_layout"]
            elif change == "unknown": plan["old_fallback"] = True
            else: e["operation"] = "script.generate"
            with self.subTest(change=change), self.assertRaises(ValidationError):
                self.verify(p, e)

    def test_script_operations_require_their_explicit_output_protocol(self):
        for operation in ("script.generate", "script.structure"):
            with self.subTest(operation=operation):
                self.assertEqual(verify_operation_output(operation, self.reference), self.reference)
                with self.assertRaises(ValidationError):
                    verify_operation_output(operation, None)
        for operation in ("text.structured", "visual_rules.generate"):
            with self.subTest(operation=operation), self.assertRaises(ValidationError):
                verify_operation_output(operation, self.reference)

    def test_structure_channels_and_source_remain_literal_without_legacy_fields(self):
        source = "\ufeff第一集\r\n保留 {{literal}} 与原稿时间 [45~50秒]。\r\n"
        assembly = {"channels": {"system_instruction": "STRUCTURE_REVIEWED_RULE",
            "positive_prompt": "retain {{profile.literal}}", "negative_prompt": "NO_EXTRA_PLOT"},
            "structured_fields": {"audit_only": "AUDIT_VALUE_MUST_NOT_BE_SENT"}}
        before = deepcopy(assembly)
        payload, envelope = self.build(operation="script.structure",
            inputs={"source_text": source, "planning": {"episode_count": 1,
                "shots_per_episode": 1, "episode_duration_seconds": 1}},
            prompt_profile_assembly=assembly)
        self.assertEqual(self.verify(payload, envelope), self.reference)
        user = json.loads(payload["messages"][1]["content"])
        self.assertEqual(user["inputs"]["source_text"], source)
        self.assertEqual(user["prompt_profile"], {"positive_prompt": "retain {{profile.literal}}",
            "negative_prompt": "NO_EXTRA_PLOT"})
        self.assertEqual(payload["messages"][0]["content"].count("STRUCTURE_REVIEWED_RULE"), 1)
        self.assertNotIn("AUDIT_VALUE_MUST_NOT_BE_SENT", str(payload))
        self.assertNotIn("STRUCTURE_REVIEWED_RULE", payload["messages"][1]["content"])
        self.assertEqual(assembly, before)
        for key in ("reference_request", "field_contract", "request", "authored_shot_timings"):
            self.assertNotIn(key, envelope["plan"])
        for source in ("invalid\ud800", "invalid\udfff"):
            with self.subTest(source=ascii(source)), self.assertRaises(ValidationError):
                self.build(operation="script.structure", inputs={"source_text": source})
