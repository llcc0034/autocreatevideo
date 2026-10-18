"""Saved auxiliary prompt display only: isolated DB/media, no model or dispatch."""
from copy import deepcopy
from io import BytesIO
from unittest.mock import patch

from django.contrib.auth import get_user_model
from django.core.exceptions import ValidationError
from django.core.files.storage import default_storage
from django.db import connection
from django.test import SimpleTestCase, TestCase, override_settings
from drf_spectacular.generators import SchemaGenerator
from rest_framework.test import APIClient

from studio.models import ExecutionPreparation, GenerationAttempt
from studio.services.execution_finalization import finalize_execution
from studio.services.execution_preparations import read_frozen_execution_request
from studio.services.execution_prompt_display import _frozen_content
from studio.tests import test_execution_production_auxiliaries as fixtures


def readonly(execute, sql, params, many, context):
    if sql.lstrip().upper().startswith(("INSERT", "UPDATE", "DELETE", "REPLACE")):
        raise AssertionError("Prompt display cannot write: " + sql.split()[0])
    return execute(sql, params, many, context)


@override_settings(AI_PROVIDER_ALLOWED_HOSTS=("api.example.com", "cdn.example.com"))
class FrozenAuxiliaryPromptDisplayTests(TestCase):
    setUpTestData = fixtures.ProductionAuxiliaryTests.__dict__["setUpTestData"]
    write_contract = staticmethod(fixtures.ProductionAuxiliaryTests.write_contract)
    input = staticmethod(fixtures.ProductionAuxiliaryTests.input)
    bind = fixtures.ProductionAuxiliaryTests.bind
    stage = fixtures.ProductionAuxiliaryTests.stage
    prepare = fixtures.ProductionAuxiliaryTests.prepare
    child = fixtures.ProductionAuxiliaryTests.child

    def setUp(self):
        fixtures.ProductionAuxiliaryTests.setUp(self)
        child = self.child()
        self.frozen = finalize_execution(preparation_id=child.pk,
            expected_preparation_revision=child.revision, actor=self.actor,
            collect_admission_facts=fixtures.production_observations)
        self.assertEqual(self.frozen.status, "ready", self.frozen.reason)
        self.client = APIClient()
        self.client.force_authenticate(self.actor)
        self.url = f"/api/v1/execution-preparations/{child.pk}/prompt-display/"
        self.query = {"expected_preparation_revision": self.frozen.revision}
        for target in (
            "studio.services.execution_prompt_display._compile_preparation_profile",
            "studio.services.execution_request_compilation.compile_execution_transport_request",
            "studio.services.execution_text_contracts.build_execution_text_payload",
        ):
            self.enterContext(patch(target, side_effect=AssertionError("Saved display must never reassemble")))
        self.enterContext(patch.object(default_storage, "save", side_effect=AssertionError("No display file writes")))

    def test_frozen_field_protocol_get_preserves_saved_messages_and_is_readonly(self):
        detail, definition, transport = read_frozen_execution_request(self.frozen)
        self.assertEqual(definition.value["interface"]["prompt"]["mode"], "none")
        self.assertEqual(transport.value["operation"], "prompt.fields.generate")
        self.assertEqual(transport.value["prompt_channels"]["values"], {})
        before = list(ExecutionPreparation.objects.values())
        with connection.execute_wrapper(readonly):
            response = self.client.get(self.url, self.query)
        self.assertEqual(response.status_code, 200, response.data)
        result = response.data
        self.assertEqual((result["mode"], result["prompt_mode"], result["content_kind"]),
            ("frozen", "none", "messages"))
        self.assertEqual(result["messages"], [{**row, "character_count": len(row["content"])}
            for row in transport.value["payload"]["messages"]])
        self.assertEqual([row["role"] for row in result["messages"]], ["system", "user"])
        self.assertIn('"request_id"', result["messages"][1]["content"])
        self.assertEqual(result["request_sha256"], detail.prepared_request_sha256)
        for key in ("channels", "field_basis", "length_diagnostics", "issues"):
            self.assertEqual(result[key], [])
        self.assertIsNone(result["profile_basis"])
        self.assertEqual(list(ExecutionPreparation.objects.values()), before)
        self.assertFalse(GenerationAttempt.objects.exists())
        self.assertFalse(self.parent.field_values.exists())
        self.assertFalse(self.parent.field_selections.exists())

    def test_frozen_auxiliary_rejects_wrong_owner_revision_and_query_without_writes(self):
        outsider = get_user_model().objects.create_user(username="prompt-display-outsider")
        with connection.execute_wrapper(readonly):
            self.assertEqual(self.client.get(self.url, {"expected_preparation_revision": self.frozen.revision + 1}).status_code, 409)
            self.assertEqual(self.client.get(self.url, {**self.query, "unexpected": "1"}).status_code, 400)
            self.client.force_authenticate(outsider)
            self.assertEqual(self.client.get(self.url, self.query).status_code, 404)
        self.assertFalse(GenerationAttempt.objects.exists())

    def test_damaged_saved_file_returns_error_instead_of_rebuilding_or_not_applicable(self):
        key = self.frozen.contract.prepared_request_key
        original_open = default_storage.open

        def corrupt(name, *args, **kwargs):
            return BytesIO(b"{}") if name == key else original_open(name, *args, **kwargs)

        with connection.execute_wrapper(readonly), patch.object(default_storage, "open", side_effect=corrupt):
            response = self.client.get(self.url, self.query)
        self.assertEqual(response.status_code, 400)
        self.assertFalse(GenerationAttempt.objects.exists())


class FrozenPromptRepresentationTests(SimpleTestCase):
    def setUp(self):
        self.configuration = {"mode": "none", "channels": {}}
        self.transport = {"driver_type": "openai_compatible", "operation": "prompt.fields.generate",
            "prompt_channels": {"values": {}}, "payload": {"messages": [
                {"role": "system", "content": "  Return JSON.\n"},
                {"role": "user", "content": ' {"items":[{"source":"<script>中😀</script>"}]}\n'},
            ]}}

    def test_saved_roles_whitespace_structured_protocol_and_codepoints_are_unchanged(self):
        before = deepcopy(self.transport)
        result = _frozen_content(self.configuration, self.transport)
        self.assertEqual(result["content_kind"], "messages")
        self.assertEqual(result["channels"], [])
        self.assertEqual(result["messages"], [{**row, "character_count": len(row["content"])}
            for row in self.transport["payload"]["messages"]])
        self.assertEqual(self.transport, before)

    def test_none_non_chat_is_explicitly_not_applicable(self):
        for driver in ("comfyui", "async_rest"):
            with self.subTest(driver=driver):
                self.transport.update(driver_type=driver, operation="video.frame_interpolation", payload={})
                self.assertEqual(_frozen_content(self.configuration, self.transport),
                    {"content_kind": "not_applicable", "channels": [], "messages": []})

    def test_missing_or_malformed_messages_are_errors_not_empty_success(self):
        system, user = self.transport["payload"]["messages"]
        for messages in (None, {}, [], [system], [user], [user, system],
                [{**system, "content": " "}, user], [system, {**user, "content": " "}],
                [system, {**user, "role": "unknown"}], [system, {**user, "content": {}}],
                [system, {**user, "extra": "would be lost"}]):
            with self.subTest(messages=messages), self.assertRaises(ValidationError):
                _frozen_content(self.configuration, {**self.transport, "payload": {"messages": messages}})
        with self.assertRaises(ValidationError):
            _frozen_content(self.configuration, {**self.transport, "payload": {}})

    def test_only_none_accepts_empty_channels_and_rejects_fabricated_channels(self):
        for prompt_mode in ("profile", "manual"):
            for values in (None, {}, {"positive_prompt": 1}):
                with self.subTest(mode=prompt_mode, values=values), self.assertRaises(ValidationError):
                    _frozen_content({"mode": prompt_mode}, {**self.transport, "prompt_channels": {"values": values}})
            result = _frozen_content({"mode": prompt_mode}, {**self.transport,
                "prompt_channels": {"values": {"positive_prompt": "  saved 😀\n", "negative_prompt": ""}}})
            self.assertEqual(result["content_kind"], "channels")
            self.assertEqual(result["messages"], [])
            self.assertEqual(result["channels"][0]["text"], "  saved 😀\n")
        for channels in (None, {}, {"values": None}, {"values": []}, {"values": {"positive_prompt": "fabricated"}}):
            with self.subTest(channels=channels), self.assertRaises(ValidationError):
                _frozen_content(self.configuration, {**self.transport, "prompt_channels": channels})
        with self.assertRaises(ValidationError):
            _frozen_content({"mode": "none", "channels": {"positive_prompt": {}}}, self.transport)

    def test_openapi_declares_explicit_content_and_saved_message_fields(self):
        schemas = SchemaGenerator().get_schema(request=None, public=True)["components"]["schemas"]
        display = schemas["ExecutionPromptDisplay"]
        self.assertTrue({"prompt_mode", "content_kind", "messages"} <= set(display["required"]))
        self.assertEqual(display["properties"]["messages"]["items"]["$ref"], "#/components/schemas/PromptMessage")
        message = schemas["PromptMessage"]
        self.assertEqual(set(message["required"]), {"role", "content", "character_count"})
        self.assertEqual(message["properties"]["character_count"]["minimum"], 0)
