"""Isolated generic capability/contract tests: never a real model or live DB."""
from copy import deepcopy
from dataclasses import asdict, replace
import json
from unittest.mock import Mock, patch

from django.core.exceptions import ValidationError
from django.db import connection
from django.test import TestCase, override_settings
from django.test.utils import CaptureQueriesContext
from rest_framework.test import APIClient

from studio.models import ModelDeploymentSmokeAttempt
from studio.services.business_outputs import output_reference, write_output
from studio.services.execution_definitions import build_execution_definition, verify_execution_definition_payload
from studio.services.execution_instances import (
    ExecutionResourceSelection, clone_execution_draft, execution_selection_options, publish_execution_version, update_execution_draft,
)
from studio.services.execution_parameter_preparation import initialize_execution_definition_parameters
from studio.services.execution_request_compilation import compile_execution_transport_request
from studio.services.execution_resource_smoke_definition import build_resource_smoke_definition, read_resource_smoke_definition
from studio.services.execution_structured_text import structured_text_plan, verify_structured_text_plan
from studio.services.execution_text_contracts import parse_execution_text_response
from studio.services.json_contracts import assign_json_contract, json_contract
from studio.tests import test_execution_text_contracts as text_fixture
from studio.tests.test_data_structures import StructureFixture


@override_settings(AI_PROVIDER_ALLOWED_HOSTS=("api.example.com",))
class StructuredTextExecutionTests(TestCase):
    node = StructureFixture.node
    create = StructureFixture.create
    publish = StructureFixture.publish
    version = text_fixture.ExecutionTextContractTests.version
    setUp = text_fixture.ExecutionTextContractTests.setUp

    @classmethod
    def setUpTestData(cls):
        text_fixture.ExecutionTextContractTests.setUpTestData.__func__(cls)
        StructureFixture.setUpTestData.__func__(cls)

    def output(self, nodes):
        structure = self.publish(self.create(nodes))
        row = write_output(actor=self.actor, data={"name": "Isolated configurable output", "parser_key": "structured_json.v1",
            "data_structure_version": structure.pk})
        return write_output(actor=self.actor, output_id=row.pk, expected_revision=1, action="publish")

    def compile(self, definition):
        params = initialize_execution_definition_parameters(definition, {}, policies=[], resolve_source=lambda _: self.fail("No implicit sources"))
        return compile_execution_transport_request(definition, prepared_parameters=params,
            manual_channels={"positive_prompt": "Explicit generation facts", "system_instruction": "Configured writing policy"}, idempotency_key="isolated-request")

    def test_catalog_and_profile_registry_share_new_capability_and_parser(self):
        from studio.execution_instance_serializers import ExecutionSelectionOptionsSerializer
        from studio.services.prompt_profiles import KNOWN_PROMPT_OPERATIONS, CONTROLLED_PROMPT_OPERATION_REGISTRY
        rows = ExecutionSelectionOptionsSerializer(execution_selection_options(deployment=self.deployment)).data["target"]["operations"]
        self.assertEqual(next(row for row in rows if row["value"] == "text.structured")["output_parser_keys"], ["structured_json.v1"])
        self.assertEqual(next(row for row in rows if row["value"] == "prompt.draft")["output_parser_keys"], [])
        self.assertIn("text.structured", KNOWN_PROMPT_OPERATIONS)
        self.assertIn("text.structured", CONTROLLED_PROMPT_OPERATION_REGISTRY)

    def test_response_schema_comes_from_exact_output_and_not_model_options(self):
        from studio.services.data_structures import validate_data_structure_snapshot
        from studio.services.chat_body import assemble_chat_body
        specification = text_fixture.text_specification()
        options = {"temperature": 0, "response_format": {"type": "json_schema", "json_schema": {"strict": False}}}
        specification["text_execution"]["operations"]["text.structured"]["chat_request_options"] = options
        assign_json_contract(self.model, "specification", specification); self.model.save()
        version = self.version("text.structured")
        # Transport-reserved words and Unicode are valid business property names.
        output = self.output([self.node("root", "object"), self.node("custom", "text", "root", property_name="model", required=True),
            self.node("unicode", "text", "root", property_name="原文😀", position=1)])
        version = update_execution_draft(version_id=version.pk, expected_revision=version.revision,
            actor=self.actor, changes={"output_version": output})
        definition = build_execution_definition(version.pk)
        frozen = deepcopy(definition.value)
        transport = self.compile(definition).value
        body, _ = assemble_chat_body("declared-model", transport["payload"])
        metadata = body["response_format"]["json_schema"]
        self.assertIs(metadata["strict"], False)
        self.assertEqual(metadata["name"], "output_" + str(output.pk).replace("-", ""))
        self.assertEqual(metadata["schema"], validate_data_structure_snapshot(output_reference(output)["data_structure"]))
        self.assertEqual(definition.value, frozen)
        self.assertEqual(definition.value["interface"]["chat_request_options"], options)
        # The model remains format-only; a changed output builds a new schema.
        successor = self.output([self.node("root", "text")])
        version = update_execution_draft(version_id=version.pk, expected_revision=version.revision,
            actor=self.actor, changes={"output_version": successor})
        next_body = self.compile(build_execution_definition(version.pk)).value["payload"]
        self.assertNotEqual(next_body["chat_request_options"], transport["payload"]["chat_request_options"])
        self.assertEqual(self.compile(definition).value, transport)

    def test_schema_format_without_a_business_output_is_rejected_at_definition(self):
        specification = text_fixture.text_specification()
        specification["text_execution"]["operations"]["prompt.draft"]["chat_request_options"] = {"response_format": {"type": "json_schema"}}
        assign_json_contract(self.model, "specification", specification); self.model.save()
        with self.assertRaisesMessage(ValidationError, "确切业务输出"):
            build_execution_definition(self.version("prompt.draft").pk)

    def test_two_layouts_and_successor_do_not_change_original_definition_or_request(self):
        version = self.version("text.structured")
        first = build_execution_definition(version.pk)
        transport = self.compile(first)
        published = publish_execution_version(version_id=version.pk, expected_revision=version.revision,
            expected_definition_sha256=first.sha256, actor=self.actor)
        successor = clone_execution_draft(source_id=published.pk, expected_revision=published.revision,
            expected_definition_sha256=first.sha256, actor=self.actor)
        self.assertEqual(successor.output_version_id, version.output_version_id)
        second_output = self.output([self.node("root", "list"), self.node("item", "object", "root"),
            self.node("custom", "integer", "item", property_name="custom.count", required=True)])
        changed = update_execution_draft(version_id=successor.pk, expected_revision=successor.revision,
            actor=self.actor, changes={"output_version": second_output})
        second = build_execution_definition(changed.pk)
        self.assertNotEqual(first.sha256, second.sha256)
        self.assertIn('"custom.count"', self.compile(second).value["payload"]["messages"][0]["content"])
        self.assertEqual(self.compile(first), transport)
        self.assertEqual(published.contract.definition["interface"]["business_output"], first.value["interface"]["business_output"])

    def test_definition_requires_exact_published_matching_protocol(self):
        version = self.version("text.structured")
        definition = build_execution_definition(version.pk)
        for mutate in (lambda p: p["configuration"].pop("output_version_id"),
                lambda p: p["interface"].pop("business_output"),
                lambda p: p["interface"]["output"].update(adapter="text.prompt_draft/v1")):
            value = deepcopy(definition.value); mutate(value)
            with self.assertRaises(ValidationError):
                verify_execution_definition_payload(json_contract(value))
        version = update_execution_draft(version_id=version.pk, expected_revision=version.revision, actor=self.actor, changes={"output_version": None})
        with self.assertRaisesMessage(ValidationError, "确切已发布"):
            build_execution_definition(version.pk)
        from studio.tests.output_structure_fixtures import response_structure
        visual = write_output(actor=self.actor, data={"name": "Visual only", "parser_key": "visual_rules.v2", "data_structure_version": response_structure().pk})
        visual = write_output(actor=self.actor, output_id=visual.pk, expected_revision=1, action="publish")
        with self.assertRaisesMessage(ValidationError, "协议与执行能力不匹配"):
            update_execution_draft(version_id=version.pk, expected_revision=version.revision, actor=self.actor, changes={"output_version": visual})

    def test_undeclared_model_does_not_offer_or_accept_new_operation(self):
        specification = text_fixture.text_specification()
        specification["capabilities"].remove("text.structured")
        del specification["text_execution"]["operations"]["text.structured"]
        assign_json_contract(self.model, "specification", specification); self.model.save()
        self.deployment.refresh_from_db()
        self.assertNotIn("text.structured", [row["value"] for row in execution_selection_options(deployment=self.deployment)["target"]["operations"]])
        with self.assertRaises(ValidationError):
            self.version("text.structured")

    def test_output_plan_cannot_be_swapped_for_another_self_consistent_snapshot(self):
        definition = build_execution_definition(self.version("text.structured").pk)
        transport = self.compile(definition)
        expected = definition.value["interface"]["business_output"]
        self.assertEqual(verify_structured_text_plan(transport.value["operation_plan"], expected_output=expected), expected)
        other = structured_text_plan(output_reference(self.output([self.node("root", "text")])))
        for plan in (None, {}, other, {**other, "extra": 1}, {**other, "operation": "prompt.draft"},
                {**other, "plan": {**other["plan"], "schema_version": True}}):
            with self.subTest(plan=plan), self.assertRaises(ValidationError):
                verify_structured_text_plan(plan, expected_output=expected)
        from studio.task_center_system_message import system_message_trace
        self.assertEqual(system_message_trace(transport.value, definition.value)["state"], "matched")
        replaced = {**transport.value, "operation_plan": other}
        self.assertEqual(system_message_trace(replaced, definition.value)["state"], "unavailable")
        from studio.task_center_prompt_trace import _channel, protocol_notes
        for channel, text in transport.value["prompt_channels"]["values"].items():
            self.assertEqual(_channel(transport.value, channel, text, definition.value)["state"], "matched")
        notes = " ".join(protocol_notes(transport.value, definition.value))
        self.assertIn("JSON Schema", notes)
        self.assertNotIn("system_constraints_as_data", notes)
        altered = deepcopy(transport.value)
        altered["payload"]["messages"][0]["content"] += "wrong"
        self.assertEqual(_channel(altered, "system_instruction", "Configured writing policy", definition.value)["state"], "unavailable")

    def test_frozen_request_reader_requires_plan_and_matches_its_original_definition(self):
        from studio.services.execution_preparations import read_frozen_execution_request
        version = self.version("text.structured")
        definition = build_execution_definition(version.pk)
        transport = self.compile(definition).value
        prep = Mock(pk="isolated-request", execution_instance_version_id=version.pk, intent_sha256="test-intent", revision=2)
        frozen = {"schema_version": 1, "preparation_id": str(prep.pk), "basis_preparation_revision": 1,
            "intent_sha256": prep.intent_sha256, "field_selections": {}, "transport": transport}
        detail = Mock(prepared_request=frozen, intent={"request": {"definition_sha256": definition.sha256}},
            resolved_inputs={"parameters": transport["parameters"]})
        other = structured_text_plan(output_reference(self.output([self.node("root", "text")])))
        with patch("studio.services.execution_preparations.read_execution_preparation_detail", return_value=detail), \
                patch("studio.services.execution_preparations.preparation_execution_definition", return_value=definition):
            self.assertEqual(read_frozen_execution_request(prep)[2].value, transport)
            for change in ({"operation_plan": other}, {"operation_plan": None}, {"operation_plan": {}}, {"extra": True}):
                detail.prepared_request = {**frozen, "transport": {**transport, **change}}
                with self.subTest(change=change), self.assertRaises(ValidationError):
                    read_frozen_execution_request(prep)
            missing = dict(transport); missing.pop("operation_plan")
            detail.prepared_request = {**frozen, "transport": missing}
            with self.assertRaises(ValidationError):
                read_frozen_execution_request(prep)
            changed = deepcopy(transport)
            changed["payload"]["chat_request_options"]["response_format"] = {"type": "json_object"}
            detail.prepared_request = {**frozen, "transport": changed}
            with self.assertRaisesMessage(ValidationError, "Chat"):
                read_frozen_execution_request(prep)
            # Without optional wire-layout evidence, the exact output contract
            # still independently rejects a changed response format.
            changed.pop("chat_body_layout")
            with self.assertRaisesMessage(ValidationError, "响应格式与冻结业务输出不一致"):
                read_frozen_execution_request(prep)

    def test_parser_accepts_configured_root_values_without_business_field_branches(self):
        cases = [("text", "", False), ("integer", 0, False), ("boolean", False, False),
            ("object", {}, False), ("text", None, True)]
        for kind, value, nullable in cases:
            with self.subTest(kind=kind, value=value):
                output = self.output([self.node("root", kind, allow_null=nullable, allow_blank=True, allow_empty=True)])
                plan = structured_text_plan(output_reference(output))
                with CaptureQueriesContext(connection) as queries:
                    result = parse_execution_text_response("text.structured", {"text": json.dumps(value)}, operation_plan=plan)
                self.assertFalse(any(table in row["sql"].lower() for row in queries for table in (
                    "studio_businessoutputversion", "studio_datastructure", "studio_fielddefinition")))
                self.assertEqual(result["result"]["value"], value)
                self.assertEqual(result["result"]["output_version_id"], str(output.pk))

    def test_parser_reuses_default_provenance_and_rejects_invalid_response(self):
        output = self.output([self.node("root", "object"), self.node("flag", "boolean", "root", property_name="enabled",
            has_default=True, default_value=False)])
        plan = structured_text_plan(output_reference(output))
        result = parse_execution_text_response("text.structured", {"text": '{"extra":0}'}, operation_plan=plan)
        self.assertIs(result["result"]["value"]["enabled"], False)
        self.assertEqual(result["result"]["defaults"][0]["source"], "configured_default")
        self.assertTrue(result["warnings"])
        for raw in ('{"enabled":"false"}', '{"enabled":true,"enabled":false}', 'bad', '```json\n{}\n```'):
            with self.subTest(raw=raw), self.assertRaises(ValidationError):
                parse_execution_text_response("text.structured", {"text": raw}, operation_plan=plan)
        with self.assertRaises(ValidationError):
            parse_execution_text_response("prompt.draft", {"text": "{}"}, operation_plan=plan)

    def test_resource_api_freezes_output_and_reloads_exact_reference_without_invocation(self):
        version = self.version("text.structured")
        selection = ExecutionResourceSelection.from_version(version)
        definition = build_resource_smoke_definition(selection)
        client = APIClient(); client.force_authenticate(self.actor)
        request = {"operation": "text.structured", "mode": "", "prompt_mode": "manual", "prompt_profile_version_id": None,
            "output_version_id": str(version.output_version_id)}
        base = f"/api/v1/model-deployments/{self.deployment.pk}/"
        response = client.post(base + "smoke-contract/", request, format="json")
        self.assertEqual(response.status_code, 200, response.data)
        self.assertEqual(response.data["sha256"], definition.sha256)
        self.assertFalse(ModelDeploymentSmokeAttempt.objects.exists())
        response = client.post(base + "smoke-contract/", {**request, "output_version_id": None}, format="json")
        self.assertEqual(response.status_code, 400, response.data)
        attempt = ModelDeploymentSmokeAttempt.objects.create(scope="resource", deployment=self.deployment,
            operation=selection.operation, status="preparing", target_sha256=definition.value["target_fingerprint"],
            request_sha256="", request_snapshot={"schema_version": 2, "resource_definition": asdict(definition)})
        self.assertEqual(read_resource_smoke_definition(attempt, current=True), definition)
        self.assertEqual(self.compile(definition).value["operation_plan"]["plan"]["business_output"]["id"], str(version.output_version_id))

    def test_resource_replay_rejects_changed_or_missing_output_without_duplicate_task(self):
        from studio.services.execution_smoke import create_resource_smoke
        version = self.version("text.structured")
        selection = ExecutionResourceSelection.from_version(version)
        definition = build_resource_smoke_definition(selection)
        args = {"selection": selection, "expected_definition_sha256": definition.sha256, "fixture": {
            "inputs": {}, "parameters": {}, "manual_channels": {"positive_prompt": "Explicit fixture"},
            "subject_facts": [], "source_language": "", "target_language": ""}, "field_values": [],
            "client_idempotency_key": "isolated-output-replay", "allow_external_calls": False, "actor": self.actor}
        attempt, prep, created = create_resource_smoke(**args)
        self.assertTrue(created)
        same, same_prep, created = create_resource_smoke(**args)
        self.assertFalse(created); self.assertEqual((same.pk, same_prep.pk), (attempt.pk, prep.pk))
        other = self.output([self.node("root", "text")])
        for output in (None, other):
            with self.subTest(output=output), self.assertRaises(ValidationError) as error:
                create_resource_smoke(**{**args, "selection": replace(selection, output_version=output)})
            self.assertEqual(error.exception.code, "execution_preparation_conflict")
        self.assertEqual(ModelDeploymentSmokeAttempt.objects.count(), 1)
