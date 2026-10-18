"""Native instance API publication through preparation/compiler, in isolation.

Outputs, instance authoring, protocol admission and publication use real
services/APIs and the compact model policy. No definition snapshot is patched to select
the output or transport policy. Corruption cases explicitly mutate local copies.
No Smoke, worker, provider call or current business data is used by these tests.
"""
from copy import deepcopy
from dataclasses import replace
import json
from unittest.mock import patch
from uuid import uuid4

from django.core.exceptions import ValidationError
from django.test import SimpleTestCase, TestCase, override_settings
from rest_framework.test import APIClient

from studio.models import ExecutionPreparation, ModelDeploymentSmokeAttempt, ModelExecutionInstanceVersion
from studio.services.business_outputs import output_reference, write_output
from studio.services.execution_definitions import build_execution_definition
from studio.services.chat_body import assemble_chat_body
from studio.services.execution_native_text import build_native_text_plan, native_text_source_reference, parse_native_text_response
from studio.services.execution_request_compilation import compile_execution_transport_request
from studio.services.execution_request_preparation import resolve_execution_request_facts
from studio.services.json_contracts import json_contract
from studio.services.prompt_profile_compiler import MediaPromptProfileCompilation
from studio.services.script_semantic_requests import verify_script_semantic_execution
from studio.tests import test_execution_native_text as native
from studio.tests import test_script_semantic_request_freeze as frozen
from studio.tests import test_script_revision_structures as structures
from studio.task_center_native_inputs import native_input_trace
from studio.task_center_prompt_trace import _channel
from studio.task_center_system_message import system_message_trace
from studio.tests.test_script_semantic_requests import LIMIT


@override_settings(AI_PROVIDER_ALLOWED_HOSTS=("api.example.com",))
class ScriptSemanticNativeCompilationTests(TestCase):
    setUpTestData = classmethod(native.ExecutionNativeTextTests.setUpTestData.__func__)
    structure_instance = native.ExecutionNativeTextTests.structure_instance
    make_output = structures.ScriptRevisionStructureTests.make_output

    def setUp(self):
        native.ExecutionNativeTextTests.setUp(self)
        self.client = APIClient()
        self.client.force_authenticate(self.actor)
        self.versions_url = "/api/v1/model-execution-instance-versions/"
        self.base_definition = self.definition
        self.select_output()

    def select_output(self, layout="a"):
        self.output_version, self.raw = self.make_output(layout)
        self.output = output_reference(self.output_version)
        config = self.base_definition.value["configuration"]
        original = ModelExecutionInstanceVersion.objects.select_related("deployment__profile_version").get(
            pk=self.base_definition.value["version_id"])
        policy_key = {"script.generate": "series_concept", "script.structure": "script_structure"}[config["operation"]]
        selected_policy = original.deployment.profile_version.specification[policy_key]
        self.policy = {key: deepcopy(selected_policy[key]) for key in ("timeout_seconds", "chat_request_options")}
        instance = self.client.post("/api/v1/model-execution-instances/", {
            "key": f"semantic-{uuid4().hex}", "name": "Isolated exact-output script"}, format="json")
        self.assertEqual(instance.status_code, 201, instance.data)
        response = self.client.post(self.versions_url, {
            "instance_id": instance.data["id"], "expected_revision": instance.data["revision"],
            "deployment_id": str(original.deployment_id), "operation": config["operation"], "mode": "",
            "prompt_mode": "manual", "prompt_profile_version_id": None, "workflow_version_id": None,
            "output_version_id": str(self.output_version.pk), "inputs": deepcopy(config["inputs"]),
        }, format="json")
        self.assertEqual(response.status_code, 201, response.data)
        url = f"{self.versions_url}{response.data['id']}/"
        reviewed = self.client.get(url + "input-contract/")
        self.assertEqual(reviewed.status_code, 200, reviewed.data)
        self.assertEqual(reviewed.data["definition"]["interface"]["business_output"], self.output)
        self.assertEqual(reviewed.data["definition"]["interface"]["native_policy"], self.policy)
        published = self.client.post(url + "publish/", {"expected_revision": reviewed.data["revision"],
            "expected_definition_sha256": reviewed.data["sha256"]}, format="json")
        self.assertEqual(published.status_code, 200, published.data)
        self.version = ModelExecutionInstanceVersion.objects.get(pk=response.data["id"])
        self.assertEqual(self.version.status, "published")
        self.definition = build_execution_definition(self.version.pk)
        self.assertEqual(self.definition.value, reviewed.data["definition"])
        self.assertEqual(self.version.contract.definition, self.definition.value)
        self.assertEqual(self.version.output_version_id, self.output_version.pk)

    def clone(self):
        response = self.client.post(f"{self.versions_url}{self.version.pk}/clone-successor/", {
            "expected_revision": self.version.revision, "expected_definition_sha256": self.definition.sha256}, format="json")
        self.assertEqual(response.status_code, 201, response.data)
        return response.data

    def test_successor_explicit_selection_changes_rules_without_rewriting_original(self):
        original = self.definition
        original_version = self.version
        facts = self.resolve()
        request = self.compile(facts)
        newer = write_output(actor=self.actor, output_id=self.output_version.pk,
            expected_revision=self.output_version.revision, action="successor")
        newer = write_output(actor=self.actor, output_id=newer.pk, expected_revision=newer.revision, action="publish")
        self.assertEqual(newer.version, self.output_version.version + 1)
        self.assertEqual(build_execution_definition(original_version.pk), original)
        successor = self.clone()
        self.assertEqual(successor["output_version_id"], str(self.output_version.pk))
        other_output, _ = self.make_output("b")
        url = f"{self.versions_url}{successor['id']}/"
        response = self.client.patch(url, {"expected_revision": successor["revision"],
            "output_version_id": str(other_output.pk)}, format="json")
        self.assertEqual(response.status_code, 200, response.data)
        reviewed = self.client.get(url + "input-contract/")
        self.assertEqual(reviewed.status_code, 200, reviewed.data)
        response = self.client.post(url + "publish/", {"expected_revision": reviewed.data["revision"],
            "expected_definition_sha256": reviewed.data["sha256"]}, format="json")
        self.assertEqual(response.status_code, 200, response.data)
        self.definition = build_execution_definition(successor["id"])
        changed = self.compile(self.resolve())
        self.assertNotEqual(self.definition.sha256, original.sha256)
        self.assertEqual(changed.value["operation_plan"]["plan"]["business_output"], output_reference(other_output))
        self.assertIn('"motion"', changed.value["payload"]["messages"][0]["content"])
        self.definition = original
        self.assertEqual(self.compile(facts), request)
        self.assertEqual(build_execution_definition(original_version.pk), original)
        original_version.refresh_from_db()
        self.assertEqual(original_version.contract.definition, original.value)

    def test_api_rejects_wrong_or_unpublished_output_without_changing_draft(self):
        successor = self.clone()
        url = f"{self.versions_url}{successor['id']}/"
        draft, _ = self.make_output(publish=False)
        wrong, _ = self.make_output(parser="structured_json.v1")
        retired, _ = self.make_output()
        retired = write_output(actor=self.actor, output_id=retired.pk,
            expected_revision=retired.revision, action="retire")
        for output in (draft, wrong, retired):
            with self.subTest(status=output.status, parser=output.parser_key):
                response = self.client.patch(url, {"expected_revision": successor["revision"],
                    "output_version_id": str(output.pk)}, format="json")
                self.assertEqual(response.status_code, 400, response.data)
                saved = ModelExecutionInstanceVersion.objects.get(pk=successor["id"])
                self.assertEqual(saved.revision, successor["revision"])
                self.assertEqual(saved.output_version_id, self.output_version.pk)
        cleared = self.client.patch(url, {"expected_revision": successor["revision"],
            "output_version_id": None}, format="json")
        self.assertEqual(cleared.status_code, 200, cleared.data)
        self.assertEqual(self.client.get(url + "input-contract/").status_code, 400)
        response = self.client.post(url + "publish/", {"expected_revision": cleared.data["revision"],
            "expected_definition_sha256": self.definition.sha256}, format="json")
        self.assertEqual(response.status_code, 400, response.data)
        self.assertEqual(ModelExecutionInstanceVersion.objects.get(pk=successor["id"]).status, "draft")

    def test_registry_still_controls_admission_and_catalog_without_silent_fallback(self):
        query = {"deployment_id": str(self.version.deployment_id)}
        response = self.client.get(self.versions_url + "selection-options/", query)
        self.assertEqual(response.status_code, 200, response.data)
        operation = next(row for row in response.data["target"]["operations"] if row["value"] == "script.generate")
        self.assertEqual(operation["output_parser_keys"], ["script_semantics.v1"])
        with patch.dict("studio.services.execution_output_contracts._OUTPUT_PROTOCOLS", {"script.generate": ()}):
            response = self.client.get(self.versions_url + "selection-options/", query)
            self.assertEqual(response.status_code, 200, response.data)
            operation = next(row for row in response.data["target"]["operations"] if row["value"] == "script.generate")
            self.assertEqual(operation["output_parser_keys"], [])
            with self.assertRaises(ValidationError):
                build_execution_definition(self.version.pk)

    def test_resource_contract_reuses_output_and_transport_without_allocating_execution(self):
        from studio.services.execution_instances import ExecutionResourceSelection
        from studio.services.execution_resource_smoke_definition import build_resource_smoke_definition
        before = (ModelExecutionInstanceVersion.objects.count(), ExecutionPreparation.objects.count(),
            ModelDeploymentSmokeAttempt.objects.count())
        selection = ExecutionResourceSelection(deployment=self.version.deployment,
            operation="script.generate", mode="", prompt_mode="manual", prompt_profile_version=None,
            workflow_version=None, output_version=self.output_version)
        resource = build_resource_smoke_definition(selection)
        self.assertIsNone(resource.value["version_id"])
        self.assertEqual(resource.value["interface"]["business_output"], self.output)
        self.assertEqual(resource.value["interface"]["native_policy"], self.policy)
        self.assertNotIn("resource_advisory", resource.value["interface"])
        self.assertEqual(before, (ModelExecutionInstanceVersion.objects.count(), ExecutionPreparation.objects.count(),
            ModelDeploymentSmokeAttempt.objects.count()))

    def resolve(self, **changes):
        return resolve_execution_request_facts(self.definition, deepcopy(self.fixture), owner=self.actor,
            unit_key="script", max_media_bytes=LIMIT,
            review_contract=json_contract({"schema_version": 1, "mode": "manual"}),
            parameter_policies=[], resolve_parameter_source=lambda _: self.fail("unexpected source"), **changes).resolved_inputs.value

    def compile(self, facts, **changes):
        return compile_execution_transport_request(self.definition, **{
            "prepared_parameters": facts["parameters"], "manual_channels": self.fixture["manual_channels"],
            "idempotency_key": "isolated-native-compile", "operation_inputs": facts["inputs"]["operation_inputs"],
            "operation_source_reference": native_text_source_reference(self.definition.value, facts["inputs"]), **changes})

    def verify(self, facts, transport):
        return verify_script_semantic_execution(self.definition.value, facts, transport.value, max_bytes=LIMIT)

    def test_original_preparation_and_compiler_follow_both_exact_output_layouts(self):
        systems = []
        for layout in ("a", "b"):
            self.select_output(layout)
            facts = self.resolve()
            request = self.compile(facts)
            value = request.value
            self.assertEqual(self.verify(facts, request), self.output)
            self.assertTrue(value["parameters"]["sources"]["max_tokens"].startswith("system.execution.resources.review_policy@"))
            self.assertEqual(value["payload"]["seed"], 42)
            self.assertEqual(value["operation_plan"]["plan"]["transport_policy"], self.policy)
            self.assertEqual(value["definition_sha256"], self.definition.sha256)
            self.assertEqual(value["operation_plan"]["plan"]["inputs"], facts["inputs"]["operation_inputs"])
            wire, layout_receipt = assemble_chat_body(self.definition.value["interface"]["model_identifier"], value["payload"])
            self.assertEqual(value["chat_body_layout"], layout_receipt)
            self.assertEqual(wire["temperature"], self.policy["chat_request_options"]["temperature"])
            self.assertNotIn("timeout_seconds", wire)
            result = parse_native_text_response("script.generate", json.dumps(self.raw, ensure_ascii=False),
                value["operation_plan"], request_payload=value["payload"], expected_output=self.output)
            self.assertTrue(result["result"]["valid"])
            self.assertEqual(result["output_contract"], self.output)
            self.assertEqual(system_message_trace(value, self.definition.value)["state"], "matched")
            self.assertEqual(native_input_trace(value, self.definition.value)["state"], "matched")
            systems.append(value["payload"]["messages"][0]["content"])
            for old in ("FIELD_CONTRACT", "REFERENCE_SCHEMA=", "SCRIPT_BIBLE", "printable ASCII"):
                self.assertNotIn(old, str(value["payload"]))
        self.assertNotEqual(*systems)
        self.assertIn('"motion"', systems[1])

    def test_early_preparation_never_creates_placeholder_profile_or_messages(self):
        with patch("studio.services.script_semantic_requests.script_semantic_messages", side_effect=AssertionError("too early")):
            facts = self.resolve()
            plan = build_native_text_plan(self.definition.value["interface"],
                inputs=facts["inputs"]["operation_inputs"], parameters=facts["parameters"]["values"], reference_max_bytes=LIMIT)
        self.assertNotIn("prompt_profile_assembly", plan)
        self.assertNotIn("system_message_layout", plan)
        self.assertNotIn("messages", plan)
        compiled = self.compile(facts).value["operation_plan"]["plan"]
        self.assertEqual(plan, {key: value for key, value in compiled.items()
            if key not in {"prompt_profile_assembly", "system_message_layout"}})

    def test_structure_uses_actual_resolver_source_and_preserves_text_and_optional_planning(self):
        self.definition = self.base_definition
        self.structure_instance(planning=True)
        self.base_definition = self.definition
        text = "  台词原文\r\n下一行 😀\r结尾  "
        self.fixture["inputs"]["script"]["value"] = text
        planning = {"episode_count": 2, "shots_per_episode": 1, "episode_duration_seconds": 2}
        self.fixture["inputs"]["plan"] = {"kind": "literal", "value": planning}
        for layout in ("a", "b"):
            self.select_output(layout)
            facts = self.resolve()
            result = self.compile(facts)
            plan = result.value["operation_plan"]["plan"]
            self.assertEqual(self.verify(facts, result), self.output)
            self.assertEqual(plan["source_kind"], "literal")
            self.assertIsNone(plan["source_revision"]["id"])
            self.assertEqual(plan["inputs"], {"source_text": text, "planning": planning})
            self.assertEqual(json.loads(result.value["payload"]["messages"][1]["content"])["inputs"], plan["inputs"])
            self.assertEqual(native_input_trace(result.value, self.definition.value)["state"], "matched")

    def test_invalid_explicit_output_or_old_model_policy_never_falls_back(self):
        original = self.definition
        variants = [{"business_output": None}, {"business_output": {}},
            {"native_policy": {**self.policy, "field_contract": {}}},
            {"native_policy": {**self.policy, "reference_policy": {}}},
            {"native_policy": {"timeout_seconds": 30}},
            {"native_policy": {**self.policy, "chat_request_options": {"seed": 100}}}]
        for changes in variants:
            value = deepcopy(original.value)
            value["interface"].update(changes)
            self.definition = json_contract(value)
            with self.subTest(changes=changes), self.assertRaises(ValidationError):
                self.resolve()

    def test_frozen_transport_cannot_change_selected_policy_even_with_consistent_wire(self):
        facts = self.resolve()
        original = self.definition
        value = deepcopy(original.value)
        value["interface"]["native_policy"]["timeout_seconds"] += 1
        self.definition = json_contract(value)
        changed = self.compile(facts)
        self.definition = original
        with self.assertRaisesMessage(ValidationError, "不一致"):
            self.verify(facts, changed)
        result = self.compile(facts)
        with patch("studio.services.script_semantic_requests.script_semantic_messages", side_effect=AssertionError("rebuild")), \
                patch("studio.services.model_profile_contracts.validate_chat_request_options", side_effect=AssertionError("current options")):
            self.assertEqual(self.verify(facts, result), self.output)

    def test_original_parameter_receipt_stays_required_and_channels_cannot_override_it(self):
        facts = self.resolve()
        params = deepcopy(facts["parameters"])
        params["values"]["max_tokens"] += 1
        with self.assertRaises(ValidationError):
            self.compile(facts, prepared_parameters=params)
        with self.assertRaises(ValidationError):
            self.compile(facts, manual_channels={"positive_prompt": "x", "max_tokens": "2"})
        with patch("studio.services.execution_parameter_preparation.randbelow", side_effect=AssertionError("redraw")):
            self.assertEqual(self.compile(facts).sha256, self.compile(facts).sha256)

    def test_exact_profile_compilation_is_consumed_without_repeating_structured_audit_values(self):
        facts = self.resolve()
        value = deepcopy(self.definition.value)
        profile_id, digest = str(uuid4()), "b" * 64
        value["configuration"].update(prompt_mode="profile", prompt_profile_version_id=profile_id)
        value["interface"]["prompt"].update(mode="profile", profile_id=profile_id, profile_sha256=digest)
        for key, channel in value["interface"]["prompt"]["channels"].items():
            channel["layers"] = [{"source": f"fixture.{key}", "rule": deepcopy(channel["rule"])}]
        self.definition = json_contract(value)
        # Explicit already-compiled Profile fixture: the upstream field engine
        # is not under test; its exact-identity/channel checks are real here.
        channels = {"system_instruction": "所选策略", "positive_prompt": "本次策略字段", "negative_prompt": "不改对白"}
        assembly = {"prompt_profile_version_id": profile_id, "contract_sha256": digest,
            "channels": channels, "structured_fields": {"audit_only": "不要重复进入正文"},
            "length_diagnostics": [{"channel": key, "character_count": len(text), "fragments": [
                {"field_key": key, "field_definition_version_id": str(uuid4()), "source_selector": "request.fixture",
                    "text_chars": len(text), "separator_chars": 0, "max_chars": 1000}]} for key, text in channels.items()]}
        compilation = MediaPromptProfileCompilation(assembly=assembly, structured_fields={})
        result = self.compile(facts, manual_channels={}, profile_compilation=compilation)
        self.assertEqual(self.verify(facts, result), self.output)
        self.assertEqual(result.value["operation_plan"]["plan"]["prompt_profile_assembly"],
            {"channels": channels, "structured_fields": assembly["structured_fields"]})
        self.assertIn("本次策略字段", str(result.value["payload"]))
        self.assertNotIn("不要重复进入正文", str(result.value["payload"]))
        with self.assertRaises(ValidationError):
            self.compile(facts, manual_channels={}, profile_compilation=replace(compilation,
                assembly={**assembly, "contract_sha256": "c" * 64}))
        with self.assertRaises(ValidationError):
            self.compile(facts, profile_compilation=compilation)


class ScriptSemanticRequestTraceTests(SimpleTestCase):
    build = frozen.ScriptSemanticRequestFreezeTests.build

    def setUp(self):
        frozen.ScriptSemanticRequestFreezeTests.setUp(self)

    def transport(self, operation="script.generate", **kwargs):
        from studio.services.execution_native_text import NATIVE_TEXT_ADAPTERS
        payload, plan = self.build(operation=operation, **kwargs)
        output = {"adapter": NATIVE_TEXT_ADAPTERS[operation], "kind": "script", "schema_version": 1}
        definition = {"configuration": {"operation": operation}, "interface": {
            "output": output, "business_output": self.reference}}
        transport = {"operation": operation, "driver_type": "openai_compatible", "output": output,
            "operation_plan": plan, "payload": payload}
        return transport, definition

    def test_both_operations_trace_configured_inputs_and_real_channel_sections_without_queries(self):
        for operation in ("script.generate", "script.structure"):
            inputs = {"带点.字段": {"flag": False, "value": 0}, "source_text": " 夹具原稿内容唯一标记\r\n "}
            transport, definition = self.transport(operation, inputs=inputs)
            with patch("studio.services.script_semantic_requests.script_semantic_messages", side_effect=AssertionError("rebuild")):
                system = system_message_trace(transport, definition)
                user = native_input_trace(transport, definition)
                channels = [_channel(transport, key, value, definition) for key, value in self.assembly["channels"].items()]
            self.assertEqual(system["state"], "matched")
            self.assertEqual(user["state"], "matched")
            self.assertEqual(user["unmapped_count"], 0)
            self.assertIn('JSON["inputs"]["带点.字段"]', [row["destination"].split(" → ")[1] for row in user["rows"]])
            self.assertTrue(all(row["state"] == "matched" for row in channels))
            self.assertIn("不附加旧协议标签", system["rows"][-1]["source"])
            self.assertNotIn("夹具原稿内容唯一标记", str(user))

    def test_changed_inputs_unknown_members_and_broken_identity_are_not_reported_as_matched(self):
        for case in ("type", "unknown", "missing", "output", "system"):
            transport, definition = self.transport()
            user = json.loads(transport["payload"]["messages"][1]["content"])
            if case == "type": user["inputs"]["episode_count"] = "2"
            elif case == "unknown": user["inputs"]["extra"] = 1
            elif case == "missing": del user["inputs"]["idea"]
            elif case == "output": definition["interface"]["business_output"] = {}
            else: transport["payload"]["messages"][0]["content"] += "changed"
            transport["payload"]["messages"][1]["content"] = json.dumps(user, ensure_ascii=False)
            trace = system_message_trace(transport, definition) if case == "system" else native_input_trace(transport, definition)
            with self.subTest(case=case):
                self.assertNotEqual(trace["state"], "matched")
        transport, definition = self.transport()
        self.assertEqual(_channel(transport, "system_instruction", "different", definition)["state"], "mismatch")

    def test_empty_system_channel_omits_strategy_and_keeps_exact_output_sections(self):
        self.assembly["channels"]["system_instruction"] = " \r\n "
        transport, definition = self.transport()
        self.assertEqual(system_message_trace(transport, definition)["state"], "matched")
        self.assertEqual(_channel(transport, "system_instruction", " \r\n ", definition)["state"], "omitted")
        self.assertEqual(len(transport["operation_plan"]["plan"]["system_message_layout"]["sections"]), 3)

    def test_missing_semantic_receipt_or_old_section_never_uses_legacy_contract(self):
        for mode in ("missing", "old"):
            transport, definition = self.transport()
            plan = transport["operation_plan"]["plan"]
            if mode == "missing": del plan["system_message_layout"]
            else: plan["system_message_layout"]["sections"][1]["key"] = "field_contract"
            with self.subTest(mode=mode):
                self.assertEqual(system_message_trace(transport, definition)["state"], "unavailable")
                self.assertEqual(_channel(transport, "system_instruction", "actual", definition)["state"], "unavailable")
