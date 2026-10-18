from copy import deepcopy
import json
from types import SimpleNamespace
from unittest.mock import patch

from django.core.exceptions import ValidationError
from django.test import TestCase

from studio.services.chat_body import CHAT_DRIVERS, assemble_chat_body, validate_chat_body_layout
from studio.services.provider_registry import ProviderDriverError, _invoke_chat_completion
from studio.task_center_prompt_trace import chat_body_notes


class ChatBodyTests(TestCase):
    def payload(self):
        return {"messages": [{"role": "system", "content": "  系统\n"},
                             {"role": "user", "content": "PRIVATE-PROMPT-😀"}],
                "chat_request_options": {"temperature": 0, "logprobs": False, "stop": None,
                    "response_format": {"type": "json_object"}, "thinking": {"type": "disabled"}},
                "max_tokens": 512, "seed": 0, "timeout_seconds": 30}

    def test_exact_assembly_flattens_options_preserves_zero_false_null_and_excludes_transport(self):
        payload = self.payload()
        body, layout = assemble_chat_body("fixture-model", payload)
        self.assertEqual(body, {"model": "fixture-model", "messages": payload["messages"],
                               **payload["chat_request_options"], "max_tokens": 512, "seed": 0})
        self.assertEqual(layout["transport_only"], ["timeout_seconds"])
        self.assertEqual(validate_chat_body_layout(layout, payload), layout)
        for key in ("timeout_seconds", "chat_request_options", "chat_body_layout", "Authorization"):
            self.assertNotIn(key, body)

    def test_assembly_and_reading_do_not_mutate_or_alias_input(self):
        payload = self.payload()
        original = deepcopy(payload)
        body, layout = assemble_chat_body("fixture", payload)
        validate_chat_body_layout(layout, payload)
        body["messages"][0]["content"] = "changed"
        body["thinking"]["type"] = "changed"
        self.assertEqual(payload, original)

    def test_minimal_payload_has_no_invented_seed_budget_or_timeout(self):
        payload = {"messages": [{"role": "user", "content": "hello"}], "chat_request_options": {}}
        body, layout = assemble_chat_body("fixture", payload)
        self.assertEqual(set(body), {"model", "messages"})
        self.assertEqual(layout["transport_only"], [])
        self.assertEqual(len(layout["fields"]), 2)

    def test_original_layout_reader_does_not_rebuild_or_read_current_policy(self):
        payload = self.payload()
        _, layout = assemble_chat_body("fixture", payload)
        with patch("studio.services.chat_body.validate_chat_request_options", side_effect=AssertionError("No current policy")):
            with self.assertNumQueries(0):
                self.assertEqual(validate_chat_body_layout(layout, payload), layout)
                notes = chat_body_notes({"driver_type": "deepseek_api", "payload": payload, "chat_body_layout": layout})
        self.assertIn('payload.chat_request_options["temperature"] → JSON["temperature"]', " ".join(notes))
        self.assertIn("不是逐字节HTTP正文", notes[0])
        for value in ("PRIVATE-PROMPT", "fixture", "disabled", "json_object"):
            self.assertNotIn(value, json.dumps(layout) + " ".join(notes))

    def test_old_history_remains_missing_instead_of_rebuilt(self):
        transport = {"driver_type": "deepseek_api", "payload": self.payload()}
        before = deepcopy(transport)
        with self.assertNumQueries(0):
            notes = chat_body_notes(transport)
        self.assertIn("没有冻结Chat组包位置记录", notes[0])
        self.assertEqual(transport, before)
        self.assertEqual(chat_body_notes({"driver_type": "comfyui"}), [])

    def test_invalid_layout_never_exposes_untrusted_extra_values(self):
        payload = self.payload()
        _, original = assemble_chat_body("fixture", payload)
        for mutation in ("missing", "extra", "version", "adapter", "duplicate", "source", "transport", "unknown", "values", "shape"):
            with self.subTest(mutation=mutation):
                layout = deepcopy(original)
                if mutation == "missing": layout.pop("fields")
                elif mutation == "extra": layout["secret"] = "PRIVATE-EXTRA"
                elif mutation == "version": layout["schema_version"] = True
                elif mutation == "adapter": layout["adapter"] = "unknown"
                elif mutation == "duplicate": layout["fields"].append(layout["fields"][0])
                elif mutation == "source": layout["fields"][0]["source"] = "PRIVATE-EXTRA"
                elif mutation == "transport": layout["transport_only"] = []
                elif mutation == "unknown": layout["fields"].append({"key": "PRIVATE-EXTRA", "source": "user"})
                elif mutation == "values": layout["fields"][0]["value"] = "PRIVATE-EXTRA"
                else: layout["fields"] = None
                with self.assertRaises(ValidationError):
                    validate_chat_body_layout(layout, payload)
                notes = chat_body_notes({"driver_type": "deepseek_api", "payload": payload, "chat_body_layout": layout})
                self.assertIn("记录异常", notes[0])
                self.assertNotIn("PRIVATE-EXTRA", " ".join(notes))

    def test_options_cannot_claim_runtime_fields_or_credentials(self):
        for key in ("model", "messages", "seed", "max_tokens", "stream", "timeout_seconds", "api_key", "authorization"):
            with self.subTest(key=key):
                payload = self.payload()
                payload["chat_request_options"][key] = "not-allowed"
                with self.assertRaises(ValidationError):
                    assemble_chat_body("fixture", payload)

    def test_missing_messages_options_and_retired_fields_fail_closed(self):
        for mutation in ("messages", "options", "temperature", "response_format", "chat_completion_options"):
            payload = self.payload()
            if mutation == "messages": payload["messages"] = []
            elif mutation == "options": payload.pop("chat_request_options")
            else: payload[mutation] = 0
            with self.subTest(mutation=mutation), self.assertRaises(ValidationError):
                assemble_chat_body("fixture", payload)

    def test_adapter_and_compiler_use_same_body_without_sending_layout(self):
        for driver in CHAT_DRIVERS:
            payload = self.payload()
            body, _ = assemble_chat_body("fixture", payload)
            deployment = SimpleNamespace(profile_version=SimpleNamespace(model_identifier="fixture"),
                credential=None, provider=SimpleNamespace(credential=None), driver_type=driver)
            with (self.subTest(driver=driver),
                  patch("studio.services.provider_registry._combined_config", return_value={
                      "base_url": "http://localhost", "chat_path": "/v1/chat/completions", "timeout_seconds": 60}),
                  patch("studio.services.provider_registry.validate_endpoint", return_value="http://localhost"),
                  patch("studio.services.provider_registry._request_json", return_value={
                      "choices": [{"finish_reason": "stop", "message": {"content": "ok"}}]}) as send):
                result = _invoke_chat_completion(deployment, payload, credential_optional=True)
            self.assertEqual(send.call_args.kwargs["payload"], body)
            self.assertEqual(send.call_args.kwargs["timeout"], 30)
            self.assertEqual(send.call_args.kwargs["headers"], {"Content-Type": "application/json"})
            self.assertEqual(result["text"], "ok")

    def test_invalid_body_stops_before_transport(self):
        payload = self.payload()
        payload["chat_request_options"]["model"] = "override"
        deployment = SimpleNamespace(profile_version=SimpleNamespace(model_identifier="fixture"))
        with (patch("studio.services.provider_registry._combined_config", return_value={
                  "base_url": "http://localhost", "chat_path": "/v1/chat/completions", "timeout_seconds": 60}),
              patch("studio.services.provider_registry.validate_endpoint", return_value="http://localhost"),
              patch("studio.services.provider_registry._request_json") as send,
              self.assertRaises(ProviderDriverError) as caught):
            _invoke_chat_completion(deployment, payload, credential_optional=True)
        self.assertEqual(caught.exception.code, "provider_payload_invalid")
        send.assert_not_called()

    def test_frozen_envelope_accepts_only_matching_original_layout(self):
        from studio.services.execution_preparations import read_frozen_execution_request
        definition = SimpleNamespace(sha256="definition", value={"configuration": {"operation": "prompt.draft"},
            "target": {"workflow": None, "driver_type": "deepseek_api"}, "target_fingerprint": "target",
            "interface": {"output": {"adapter": "text.prompt_draft/v1"}}})
        transport = {"schema_version": 1, "execution_instance_version_id": "version", "definition_sha256": "definition",
            "target_fingerprint": "target", "operation": "prompt.draft", "driver_type": "deepseek_api", "idempotency_key": "prep",
            "parameters": {}, "prompt_channels": {}, "profile_compilation": None, "field_request_sha256": None,
            "payload": self.payload(), "output": definition.value["interface"]["output"]}
        detail = SimpleNamespace(intent={"request": {"definition_sha256": "definition"}}, resolved_inputs={"parameters": {}},
            prepared_request={"schema_version": 1, "preparation_id": "prep", "basis_preparation_revision": 1,
                "intent_sha256": "intent", "field_selections": {}, "transport": transport})
        preparation = SimpleNamespace(pk="prep", revision=2, intent_sha256="intent", execution_instance_version_id="version")
        with patch("studio.services.execution_preparations.read_execution_preparation_detail", return_value=detail), patch(
                "studio.services.execution_preparations.preparation_execution_definition", return_value=definition):
            saved = read_frozen_execution_request(preparation)[2].value
            self.assertNotIn("chat_body_layout", saved)
            transport["chat_body_layout"] = assemble_chat_body("fixture", transport["payload"])[1]
            saved = read_frozen_execution_request(preparation)[2].value
            self.assertEqual(saved, transport)
            transport["chat_body_layout"]["fields"][0]["source"] = "wrong"
            with self.assertRaises(ValidationError):
                read_frozen_execution_request(preparation)
            transport["chat_body_layout"] = assemble_chat_body("fixture", transport["payload"])[1]
            transport["driver_type"] = definition.value["target"]["driver_type"] = "async_rest"
            with self.assertRaises(ValidationError):
                read_frozen_execution_request(preparation)
