from copy import deepcopy
from unittest.mock import patch

from django.contrib.auth import get_user_model
from django.core.exceptions import PermissionDenied, ValidationError
from django.test import TestCase, override_settings

from studio.services.execution_definitions import build_execution_definition
from studio.services.execution_instances import update_execution_draft
from studio.services.execution_parameter_preparation import initialize_execution_definition_parameters
from studio.services.execution_request_compilation import compile_execution_transport_request
from studio.services.execution_preparation_requests import compile_prepared_execution_transport
from studio.services.json_contracts import json_contract
from studio.tests import test_execution_profile_preparation as profile_fixtures
from studio.tests import test_execution_text_contracts as text_fixtures
from studio.tests import test_execution_preparation_creation as manual_fixtures


@override_settings(AI_PROVIDER_ALLOWED_HOSTS=("api.example.com", "cdn.example.com"))
class ExecutionProfileRequestTests(TestCase):
    setUpTestData = classmethod(profile_fixtures.ExecutionProfilePreparationTests.setUpTestData.__func__)
    write_contract = staticmethod(profile_fixtures.ExecutionProfilePreparationTests.write_contract)
    input = staticmethod(profile_fixtures.ExecutionProfilePreparationTests.input)
    bind = profile_fixtures.ExecutionProfilePreparationTests.bind
    resolve = profile_fixtures.ExecutionProfilePreparationTests.resolve
    prepare = profile_fixtures.ExecutionProfilePreparationTests.prepare
    preview = profile_fixtures.ExecutionProfilePreparationTests.preview
    selected_text = profile_fixtures.ExecutionProfilePreparationTests.selected_text

    def setUp(self):
        profile_fixtures.ExecutionProfilePreparationTests.setUp(self)
        self.prepare()
        self.value = self.selected_text("A courier opens a blue parcel in soft daylight.")
        self.compilation = self.preview().compilation

    def compile(self, **changes):
        return compile_execution_transport_request(self.definition, **{
            "prepared_parameters": self.prep.contract.resolved_inputs["parameters"],
            "manual_channels": {}, "idempotency_key": str(self.prep.pk), "profile_compilation": self.compilation, **changes})

    def test_real_field_selection_compiles_the_existing_async_request_without_network(self):
        result = self.compile()
        payload = result.value["payload"]
        self.assertEqual(payload["model"], self.model.model_identifier)
        self.assertIn(self.value.text, payload["prompt"])
        self.assertEqual((payload["width"], payload["height"], payload["fps"]), (768, 1344, 24))
        self.assertEqual(payload["idempotency_key"], str(self.prep.pk))
        from studio.task_center_prompt_trace import prompt_trace, _channel
        trace = prompt_trace(result.value, self.definition.value, self.prep.contract.resolved_inputs)
        with self.assertNumQueries(0):
            checked = [_channel(result.value, k, v, self.definition.value) for k, v in result.value["prompt_channels"]["values"].items()]
        self.assertEqual(trace["channels"], checked)
        self.assertEqual(trace["state"], "recorded")
        self.assertTrue(trace["channels"])
        self.assertTrue(all(row["state"] == "matched" for row in trace["channels"]))
        self.assertEqual(trace["channels"][0]["destination"], 'payload["prompt"]')
        self.assertEqual(result.sha256, self.compile().sha256)
        self.assertEqual(result.value["profile_compilation"]["reviewed_field_evidence"]["field_selections"][0]["field_value_id"], str(self.value.pk))
        self.prep.refresh_from_db()
        self.assertEqual(self.prep.status, "preparing")
        self.assertIsNone(self.prep.request_sha256)
        linked = compile_prepared_execution_transport(preparation_id=self.prep.pk,
            expected_preparation_revision=self.prep.revision, actor=self.actor)
        self.assertEqual(linked.transport, result)
        self.assertEqual(linked.intent_sha256, self.prep.intent_sha256)
        with self.assertRaises(ValidationError):
            compile_prepared_execution_transport(preparation_id=self.prep.pk,
                expected_preparation_revision=self.prep.revision - 1, actor=self.actor)

    def test_profile_cannot_be_replaced_by_manual_text_or_a_missing_compilation(self):
        for changes in ({"manual_channels": {"positive_prompt": "Bypass"}}, {"profile_compilation": None},
                {"idempotency_key": ""}, {"idempotency_key": " caller "}):
            with self.subTest(changes=changes), self.assertRaises(ValidationError):
                self.compile(**changes)

    def test_frozen_request_schema_is_required_and_parameters_cannot_change(self):
        original = self.definition
        payload = deepcopy(original.value)
        del payload["interface"]["request_schema"]
        self.definition = json_contract(payload)
        with self.assertRaisesMessage(ValidationError, "Schema"):
            self.compile()
        self.definition = original
        params = deepcopy(self.prep.contract.resolved_inputs["parameters"])
        params["values"]["width"] = 100000
        with self.assertRaises(ValidationError):
            self.compile(prepared_parameters=params)


@override_settings(AI_PROVIDER_ALLOWED_HOSTS=("api.example.com",))
class ExecutionTextRequestTests(TestCase):
    setUpTestData = classmethod(text_fixtures.ExecutionTextContractTests.setUpTestData.__func__)
    version = text_fixtures.ExecutionTextContractTests.version
    field_request = text_fixtures.ExecutionTextContractTests.field_request

    def setUp(self):
        text_fixtures.ExecutionTextContractTests.setUp(self)

    def compile(self, version, **kwargs):
        definition = build_execution_definition(version.pk)
        params = initialize_execution_definition_parameters(definition, {}, policies=[], resolve_source=lambda _: self.fail("No current source reads"))
        with patch("studio.services.execution_parameter_preparation.randbelow", side_effect=AssertionError("No seed redraw")):
            return compile_execution_transport_request(definition, prepared_parameters=params,
                idempotency_key="actual-owned-request", **kwargs)

    def test_manual_text_uses_exact_text_and_saved_generated_seed(self):
        version = self.version()
        version = update_execution_draft(version_id=version.pk, expected_revision=version.revision, actor=self.actor,
            changes={"parameters": [{"key": "seed", "value_type": "integer", "policy": "overridable",
                "seed_policy": "generate_once", "minimum": 1, "maximum": 99,
                "enum_values": [], "step": None, "remainder": None, "default_value": None, "locked_value": None}]})
        with patch("studio.services.execution_parameter_preparation.randbelow", return_value=6) as draw:
            result = self.compile(version, manual_channels={"positive_prompt": "  Exact user text  "})
        draw.assert_called_once()
        self.assertEqual(result.value["payload"]["seed"], 7)
        self.assertEqual(result.value["payload"]["messages"], [{"role": "user", "content": "  Exact user text  "}])
        from studio.services.chat_body import assemble_chat_body
        definition = build_execution_definition(version.pk)
        body, layout = assemble_chat_body(definition.value["interface"]["model_identifier"], result.value["payload"])
        self.assertEqual(result.value["chat_body_layout"], layout)
        from studio.task_center_prompt_trace import protocol_notes
        notes = " ".join(protocol_notes(result.value, definition.value))
        self.assertIn('payload.seed → JSON["seed"]', notes)
        self.assertIn("不是逐字节HTTP正文", notes)
        self.assertEqual(body["seed"], 7)
        self.assertNotIn("chat_body_layout", result.value["payload"])
        self.assertNotIn("system_message_layout", result.value)

    def test_field_leaf_uses_exact_frozen_field_protocol(self):
        from studio.task_center_system_message import system_message_trace
        request = self.field_request()
        version = self.version("prompt.fields.generate")
        result = self.compile(version, manual_channels={}, field_request=request)
        self.assertEqual(result.value["field_request_sha256"], request.sha256)
        self.assertIn('"request_id":"field-1"', result.value["payload"]["messages"][1]["content"])
        self.assertEqual(result.value["prompt_channels"]["values"], {})
        self.assertNotIn("system_message_layout", result.value["payload"])
        self.assertNotIn("operation_plan", result.value)
        self.assertEqual(result.value["chat_body_layout"]["adapter"], "chat.completions/v1")
        self.assertEqual([row["key"] for row in result.value["system_message_layout"]["sections"]], ["protocol", "field_rules"])
        report = system_message_trace(result.value, build_execution_definition(version.pk).value)
        self.assertEqual(report["state"], "matched")
        self.assertIn("user批次", report["rows"][1]["source"])

    def test_field_leaf_rejects_absent_request_or_stray_manual_channel(self):
        version = self.version("prompt.fields.generate")
        with self.assertRaises(ValidationError):
            self.compile(version, manual_channels={})
        with self.assertRaises(ValidationError):
            self.compile(version, manual_channels={"positive_prompt": "Bypass"}, field_request=self.field_request())


@override_settings(AI_PROVIDER_ALLOWED_HOSTS=("api.example.com", "cdn.example.com"))
class ExecutionManualPreparationRequestTests(TestCase):
    setUpTestData = classmethod(manual_fixtures.ExecutionPreparationCreationTests.setUpTestData.__func__)
    write_contract = staticmethod(manual_fixtures.ExecutionPreparationCreationTests.write_contract)
    job_row = manual_fixtures.ExecutionPreparationCreationTests.job_row
    resolve = manual_fixtures.ExecutionPreparationCreationTests.resolve
    create = manual_fixtures.ExecutionPreparationCreationTests.create

    def setUp(self):
        manual_fixtures.ExecutionPreparationCreationTests.setUp(self)
        self.prep = self.create()

    def preview(self, **changes):
        return compile_prepared_execution_transport(**{"preparation_id": self.prep.pk,
            "expected_preparation_revision": self.prep.revision, "actor": self.actor, **changes})

    def test_persisted_manual_channels_and_seed_compile_without_resolving_sources(self):
        self.resolver.reset_mock()
        with patch("studio.services.execution_parameter_preparation.randbelow", side_effect=AssertionError("No redraw")):
            before = self.preview()
            self.assertEqual(before, self.preview())
        self.resolver.assert_not_called()
        self.assertEqual(before.transport.value["payload"]["prompt"], self.intent.value["manual_channels"]["positive_prompt"])
        self.assertEqual(before.transport.value["payload"]["seed"], self.prep.contract.resolved_inputs["parameters"]["values"]["seed"])
        self.assertFalse(self.prep.field_values.exists())
        self.prep.refresh_from_db()
        self.assertIsNone(self.prep.request_frozen_at)

    def test_request_preview_requires_actual_owner_and_current_revision(self):
        other = get_user_model().objects.create_user(username="other-request-owner")
        with self.assertRaises(PermissionDenied):
            self.preview(actor=other)
        with self.assertRaises(ValidationError):
            self.preview(expected_preparation_revision=self.prep.revision + 1)

    def prompt_display(self, *, actor=None, revision=None):
        from rest_framework.test import APIRequestFactory, force_authenticate
        from studio.execution_preparation_views import ExecutionPreparationViewSet
        request = APIRequestFactory().get("/prompt-display/", {
            "expected_preparation_revision": self.prep.revision if revision is None else revision})
        force_authenticate(request, actor or self.actor)
        return ExecutionPreparationViewSet.as_view({"get": "prompt_display"})(request, pk=self.prep.pk)

    def test_manual_prompt_display_reports_unfrozen_boundary_without_profile_compilation_or_writes(self):
        from django.db import connection
        self.assertEqual(self.prep.contract.resolved_inputs["profile_sources"], {})
        self.resolver.reset_mock()
        def readonly(execute, sql, params, many, context):
            self.assertTrue(sql.lstrip().upper().startswith(("SELECT", "SAVEPOINT", "RELEASE", "ROLLBACK")), sql)
            return execute(sql, params, many, context)
        with connection.execute_wrapper(readonly), patch(
                "studio.services.execution_prompt_display._compile_preparation_profile",
                side_effect=AssertionError("Manual mode must not compile Profile sources")):
            response = self.prompt_display()
        self.assertEqual(response.status_code, 200)
        self.assertEqual(response.data["mode"], "unavailable")
        self.assertEqual(response.data["channels"], [])
        self.assertEqual(response.data["field_basis"], [])
        self.assertIn("勿为预览而提交模型调用", response.data["issues"][0])
        self.resolver.assert_not_called()
        self.prep.refresh_from_db()
        self.assertEqual((self.prep.revision, self.prep.status), (1, "preparing"))
        self.assertIsNone(self.prep.request_frozen_at)
        self.assertIsNone(self.prep.request_sha256)
        self.assertFalse(self.prep.generation_attempts.exists())
        self.assertFalse(self.prep.field_values.exists())

    def test_manual_prompt_display_still_checks_owner_and_preparation_revision(self):
        other = get_user_model().objects.create_user(username="manual-display-stranger")
        self.assertEqual(self.prompt_display(actor=other).status_code, 404)
        self.assertEqual(self.prompt_display(revision=self.prep.revision + 1).status_code, 409)

    def test_preview_detects_field_revision_change_during_transport_assembly(self):
        from studio.models import ExecutionPreparation
        original = compile_execution_transport_request
        def intervening_write(*args, **kwargs):
            ExecutionPreparation.objects.filter(pk=self.prep.pk).update(revision=self.prep.revision + 1)
            return original(*args, **kwargs)
        with patch("studio.services.execution_preparation_requests.compile_execution_transport_request", side_effect=intervening_write):
            with self.assertRaisesMessage(ValidationError, "准备已变化"):
                self.preview()
