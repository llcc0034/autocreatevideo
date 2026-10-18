from copy import deepcopy
from hashlib import sha256
import json
from unittest.mock import patch

from django.test import TestCase

from studio.services.json_contracts import canonical_json_bytes
from studio.task_center_field_trace import field_batch_trace
from studio.task_center_public import public_value
from studio.task_center_details import bound_content
from studio.task_center_serializers import TaskCenterDetailQuerySerializer


class FieldBatchTraceTests(TestCase):
    def sample(self, operation="prompt.fields.generate"):
        # Projection fixtures; full protocol builder and child API are also
        # exercised in the execution text / production auxiliary tests.
        mode = "model_generated" if operation.endswith("generate") else "translate_only"
        rule = {"field_key": "visual", "source_selector": "request.text", "generation_source_selector": "request.action",
                "channel": "positive_prompt", "generation_mode": mode, "generation_instruction": "Use only the facts.",
                "language_policy": "en", "generation_output_semantics": "value",
                "generation_context_selectors": ["project.logline"]}
        item = {"request_id": "request-A", "unit_key": "shot:1", "field_contract": rule, "source_text": " 原文😀 ",
                "source_language": "Chinese", "target_language": "English", "context_facts": {"project.logline": "A journey."},
                "reference_facts": {"protected_tokens": [], "logical_inputs": [], "subject_facts": []}}
        batch = {"schema_version": 1, "operation": operation, "profile": {"version_id": "parent-profile", "contract_sha256": "a"*64},
                 "profile_field_keys": ["visual"], "items": [item]}
        t = {"operation": operation, "output": {"adapter": "text.prompt_fields/v1"}, "field_request_sha256": sha256(canonical_json_bytes(batch)).hexdigest(),
             "payload": {"messages": [{"role": "system", "content": "Output protocol"}, {"role": "user", "content": json.dumps(batch, ensure_ascii=False)}]}}
        return t, {"inputs": {"operation_inputs": {"field_request": batch}}}

    def test_generation_and_translation_use_original_batch_no_queries_or_writes(self):
        for operation in ("prompt.fields.generate", "prompt.fields.translate"):
            t, r = self.sample(operation)
            before = deepcopy((t, r))
            with self.assertNumQueries(0), patch("studio.services.execution_text_contracts.build_execution_text_payload", side_effect=AssertionError("no build")), patch("studio.services.provider_registry._request_json", side_effect=AssertionError("no network")):
                result = field_batch_trace(t, r)
            self.assertEqual(result["state"], "matched")
            self.assertEqual(result["profile_id"], "parent-profile")
            self.assertEqual(result["items"][0]["source_characters"], 5)
            self.assertEqual(result["items"][0]["source_selector"], "request.text")
            self.assertEqual(result["items"][0]["generation_source_selector"], "request.action")
            self.assertEqual(result["items"][0]["context_selectors"], ["project.logline"])
            self.assertEqual((t, r), before)

    def test_missing_unsupported_changed_digest_and_bad_batch_are_unavailable(self):
        for change in ("missing", "adapter", "hash", "operation", "schema", "profile", "mode", "context", "duplicate", "duplicate-field", "type"):
            t, r = self.sample()
            b = r["inputs"]["operation_inputs"]["field_request"]
            if change == "missing": r = {}
            elif change == "adapter": t["output"]["adapter"] = "text.prompt_fields/v9"
            elif change == "hash": t["field_request_sha256"] = "b"*64
            else:
                if change == "operation": b["operation"] = "prompt.fields.translate"
                elif change == "schema": b["schema_version"] = True
                elif change == "profile": b["profile"]["contract_sha256"] = "bad"
                elif change == "mode": b["items"][0]["field_contract"]["generation_mode"] = "passthrough"
                elif change == "context": b["items"][0]["context_facts"] = {}
                elif change == "duplicate": b["items"] *= 2
                elif change == "duplicate-field":
                    b["items"].append(deepcopy(b["items"][0])); b["items"][1]["request_id"] = "new-id"
                else: b["items"][0]["source_text"] = 123
                t["field_request_sha256"] = sha256(canonical_json_bytes(b)).hexdigest()
            result = field_batch_trace(t, r)
            self.assertEqual(result["state"], "unavailable", change)
            self.assertEqual(result["items"], [])
        t, r = self.sample(); t["operation"] = "prompt.draft"
        self.assertIsNone(field_batch_trace(t, r))

    def test_wrong_duplicate_or_non_json_wire_does_not_claim_matching(self):
        for change in ("changed", "duplicate_json", "duplicate_message", "missing", "non_json", "bool_number", "extra"):
            t, r = self.sample()
            if change == "duplicate_message": t["payload"]["messages"] *= 2
            elif change == "missing": t["payload"]["messages"] = []
            elif change == "non_json": t["payload"]["messages"][1]["content"] = "not JSON"
            elif change == "duplicate_json": t["payload"]["messages"][1]["content"] = '{"items":[],"items":[]}'
            else:
                b = deepcopy(r["inputs"]["operation_inputs"]["field_request"])
                if change == "changed": b["items"][0]["source_text"] = "different"
                elif change == "bool_number": b["schema_version"] = True
                else: b["extra"] = "hidden"
                t["payload"]["messages"][1]["content"] = json.dumps(b)
            original = deepcopy(t)
            result = field_batch_trace(t, r)
            self.assertEqual(result["state"], "mismatch", change)
            self.assertEqual(len(result["items"]), 1)
            self.assertEqual(t, original)

    def test_redaction_and_bounded_reader_use_existing_contract(self):
        t, r = self.sample()
        b = r["inputs"]["operation_inputs"]["field_request"]
        b["items"][0]["field_contract"]["generation_instruction"] = "https://private.example/path password=TOP_SECRET"
        t["field_request_sha256"] = sha256(canonical_json_bytes(b)).hexdigest()
        t["payload"]["messages"][1]["content"] = json.dumps(b)
        result = public_value(field_batch_trace(t, r))
        self.assertEqual(result["state"], "matched")
        self.assertNotIn("TOP_SECRET", json.dumps(result))
        self.assertNotIn("private.example", json.dumps(result))
        result["items"] *= 400
        detail = {"input": {"state": "frozen", "submitted": {}, "resolved": {}, "parameters": [], "prompts": [], "prompt_trace": None, "field_batch_trace": result, "media_trace": None, "notes": []},
                  "output": {"state": "available", "result": None, "text": "", "media": [], "notes": []}}
        query = TaskCenterDetailQuerySerializer(data={"content_field": "input.field_batch_trace", "content_limit": 1024})
        self.assertTrue(query.is_valid(), query.errors)
        bounded = bound_content(detail, query.validated_data)
        self.assertIsNone(bounded["input"]["field_batch_trace"])
        self.assertEqual(bounded["deferred_fields"][0]["field"], "input.field_batch_trace")
        self.assertLessEqual(len(bounded["content"]["text"].encode()), 1024)
