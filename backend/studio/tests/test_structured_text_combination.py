"""G06 configured layouts through formal recipe publication and original queues.

Only provider transport/media download is faked by isolated fixtures. Real
publication, run creation, admission, preparation, selection and downstream
compilation are exercised; no live model or completed media delivery is claimed.
"""
from copy import deepcopy
from unittest.mock import Mock, patch
from uuid import uuid4

from django.test import TestCase, override_settings
from rest_framework.test import APIClient

from studio.models import (ArtifactSelectionDecision, ExecutionPreparation, GeneratedArtifact,
    GenerationAttempt, ModelDeployment, PromptProfileVersion)
from studio.services.business_outputs import write_output
from studio.services.deployment_smoke_queue import claim_next_deployment_smoke, process_deployment_smoke
from studio.services.execution_definitions import build_execution_definition
from studio.services.execution_instances import create_execution_instance, create_execution_draft, publish_execution_version
from studio.services.execution_smoke import create_instance_smoke
from studio.services.execution_text_response_review import describe_text_response_review
from studio.services.json_contracts import canonical_json_bytes
from studio.services.production_run_plans import get_run_execution_plan
from studio.services.production_runs import advance_production_run
from studio.services.provider_registry import ProviderDriverError
from studio.services.prompt_profile_structures import STRUCTURED_INPUT_SELECTOR
from studio.services.prompt_profiles import refresh_prompt_profile_contract, publish_prompt_profile
from studio.tests.field_definition_fixtures import create_field_definition, create_profile_field_rule
from studio.tests.test_data_structures import StructureFixture
from studio.tests import test_production_recipe_delivery_chain as delivery_fixtures


@override_settings(AI_PROVIDER_ALLOWED_HOSTS=("api.example.com", "cdn.example.com"))
class StructuredTextCombinationTests(TestCase):
    fixture = delivery_fixtures.RecipeDeliveryChainTests
    setUp = fixture.setUp
    profile_instance = fixture.profile_instance
    root = fixture.root
    receipt = fixture.receipt
    pass_smoke = fixture.pass_smoke
    advance = fixture.advance
    response = fixture.response
    process = fixture.process
    media_instance = fixture.media_instance
    media_bytes = fixture.media_bytes
    transfer = fixture.transfer
    pass_media_smoke = fixture.pass_media_smoke
    collected = staticmethod(fixture.collected)
    start_complete_run = fixture.start_complete_run
    node = StructureFixture.node
    create = StructureFixture.create
    publish = StructureFixture.publish

    @classmethod
    def setUpTestData(cls):
        cls.fixture.setUpTestData.__func__(cls)
        StructureFixture.setUpTestData.__func__(cls)

    def generic_instance(self, nodes, *, consumer_structure=None):
        key = "generic-" + uuid4().hex
        output = write_output(actor=self.actor, data={"name": key, "parser_key": "structured_json.v1",
            "data_structure_version": self.publish(self.create(nodes)).pk})
        output = write_output(actor=self.actor, output_id=output.pk, expected_revision=1, action="publish")
        profile = PromptProfileVersion.objects.create(profile_key=key, version=1,
            name="Isolated configurable instruction", model_family="text-protocol", operation="text.structured",
            mode="", review_state="ready", input_structure_version=consumer_structure)
        field = create_field_definition(key=key, label="Custom input", value_type="integer" if consumer_structure else "long_text")
        selector = STRUCTURED_INPUT_SELECTOR if consumer_structure else "fixed.value"
        create_profile_field_rule(profile_version=profile, field_definition=field,
            source_selector=selector, generation_source_selector=selector,
            source_path=["layout_a", 0, "quantity"] if consumer_structure else None,
            fixed_value="Use the configured layout." if not consumer_structure else None,
            generation_mode="passthrough" if consumer_structure else "server_locked",
            channel="positive_prompt", position=1, required=True, language_policy="any", label_style="none")
        refresh_prompt_profile_contract(profile)
        profile = publish_prompt_profile(profile, actor=self.actor)
        deployment = ModelDeployment.objects.get(key="text-protocol")
        deployment.credential = self.deployment.credential  # isolated keyring fixture only
        deployment.save(update_fields=("credential",))
        instance = create_execution_instance(key=key, name=key, description="", actor=self.actor)
        version = create_execution_draft(instance_id=instance.pk, expected_revision=instance.revision, actor=self.actor,
            selection={"deployment": deployment, "operation": "text.structured", "mode": "", "prompt_mode": "profile",
                "workflow_version": None, "prompt_profile_version": profile, "output_version": output})
        definition = build_execution_definition(version.pk)
        version = publish_execution_version(version_id=version.pk, expected_revision=version.revision,
            expected_definition_sha256=definition.sha256, actor=self.actor)
        return version, definition

    def generic_smoke(self, target, value, inputs=None):
        version, definition = target
        attempt, prep, _ = create_instance_smoke(version_id=version.pk, expected_definition_sha256=definition.sha256,
            fixture={"inputs": inputs or {}, "parameters": {}, "manual_channels": {}, "subject_facts": [],
                "source_language": "English", "target_language": "English"}, field_values=[],
            client_idempotency_key=f"isolated-combination:{version.pk}", allow_external_calls=True, actor=self.actor)
        ready = self.advance(prep)
        self.assertEqual(ready.status, "dispatched", ready.reason)
        peer = Mock()
        peer.invoke.return_value = {"text": canonical_json_bytes(value).decode("utf-8"), "request_id": "isolated-layout-smoke"}
        with patch("studio.services.execution_smoke_worker.driver_for", return_value=peer):
            claim = claim_next_deployment_smoke("isolated-layout-smoke")
            self.assertEqual(claim.pk, attempt.pk)
            result = process_deployment_smoke(claim)
        self.assertEqual(result.status, "succeeded", result.error_code)
        peer.invoke.assert_called_once()

    def declaration(self, key, target, *, count=1, manual=False, inputs=None):
        version, definition = target
        return {"key": key, "kind": "structured_text", "name": "同名自定义步骤", "mode": "ai", "approval": "auto",
            "routing": {"mode": "fixed", "variant_count": count, "evaluator": None if manual else "first_succeeded",
                "routes": [{"execution_instance_version_id": str(version.pk), "expected_definition_sha256": definition.sha256,
                    "input_sources": inputs or {}, "parameter_policies": []}]}}

    def configured_chain(self):
        layout_fields = lambda parent: [self.node("count", "integer", parent, property_name="custom.count", required=True),
            self.node("flag", "boolean", parent, property_name="flag", required=True, position=1),
            self.node("note", "text", parent, property_name="note", allow_null=True, required=True, position=2)]
        a = self.generic_instance([self.node("root", "object"),
            self.node("rows", "list", "root", property_name="first.layout", required=True),
            self.node("item", "object", "rows"), *layout_fields("item")])
        b = self.generic_instance([self.node("root", "list"), self.node("item", "object", "root"), *layout_fields("item")])
        consumer_nodes = [self.node("root", "object")]
        for index, key in enumerate(("layout_a", "layout_b")):
            consumer_nodes += [self.node(key, "list", "root", property_name=key, required=True, position=index),
                self.node(key + "_item", "object", key),
                self.node(key + "_count", "integer", key + "_item", property_name="quantity", required=True),
                self.node(key + "_flag", "boolean", key + "_item", property_name="active", required=True, position=1),
                self.node(key + "_note", "text", key + "_item", property_name="text", allow_null=True, required=True, position=2)]
        consumer = self.generic_instance([self.node("root", "integer")], consumer_structure=self.publish(self.create(consumer_nodes)))
        prior = [self.declaration("layout-a", a, count=3), self.declaration("layout-b", b, manual=True)]
        self.actor.is_staff = True
        self.actor.save(update_fields=("is_staff",))
        client = APIClient(); client.force_authenticate(self.actor)
        response = client.post("/api/v1/production-recipes/source-options/", {"definition": {
            "schema_version": 3, "interaction_mode": "assisted", "stages": prior}}, format="json")
        self.assertEqual(response.status_code, 200, response.data)
        options = {row["stage_key"]: row for row in response.data["text_output_options"]}
        self.assertEqual(options["layout-a"]["value_schema"]["type"], "object")
        self.assertEqual(options["layout-b"]["value_schema"]["type"], "array")
        mappings = {}
        for key, stage, path in (("layout_a", "layout-a", ["first.layout"]), ("layout_b", "layout-b", [])):
            mappings[key] = {"kind": "each", "source": {"kind": "selected_output", "stage_key": stage,
                "output_key": options[stage]["output_key"], "scope": {"kind": "project"},
                "selection_policy": "reviewed_selected", "path": path},
                "item": {"kind": "object", "fields": {"quantity": {"kind": "item_field", "path": ["custom.count"]},
                    "active": {"kind": "item_field", "path": ["flag"]}, "text": {"kind": "item_field", "path": ["note"]}}}}
        prior.append(self.declaration("consume-layouts", consumer, inputs={STRUCTURED_INPUT_SELECTOR: {
            "kind": "structured", "template": {"kind": "object", "fields": mappings}}}))
        rows = [{"custom.count": 0, "flag": False, "note": None}]
        self.expected = {key: [{"quantity": 0, "active": False, "text": None}] for key in mappings}
        self.generic_smoke(a, {"first.layout": rows})
        self.generic_smoke(b, rows)
        self.generic_smoke(consumer, 0, {STRUCTURED_INPUT_SELECTOR: {"kind": "literal", "value": self.expected}})
        self.start_complete_run(prior=prior)
        return rows, prior

    def test_two_layouts_real_publication_auto_partial_failure_manual_choice_and_downstream(self):
        rows, declarations = self.configured_chain()
        original_plan = deepcopy(get_run_execution_plan(self.run).value)
        # The public clone/edit path must not rewrite an existing frozen run.
        clone = self.client.post(f"/api/v1/production-recipes/{self.recipe.pk}/clone-successor/", {
            "expected_definition_sha256": self.recipe.definition_sha256}, format="json")
        self.assertEqual(clone.status_code, 201, clone.data)
        changed = deepcopy(clone.data["definition"])
        changed["stages"][0]["name"] = "New draft only"
        changed["stages"][0], changed["stages"][1] = changed["stages"][1], changed["stages"][0]
        response = self.client.patch(f"/api/v1/production-recipes/{clone.data['id']}/", {"definition": changed}, format="json")
        self.assertEqual(response.status_code, 200, response.data)
        self.run = advance_production_run(self.run)
        self.assertEqual((self.run.current_stage_key, self.run.blocked_code), ("layout-a", "waiting_external_task"), self.run.blocked_reason)
        failed, _ = self.process(response={}, effect=ProviderDriverError("Isolated provider failure"))
        self.assertEqual(failed.status, "failed", failed.error_message)
        invalid, _ = self.process(response={"text": "invalid JSON", "request_id": "isolated-invalid"})
        self.assertEqual(invalid.status, "awaiting_review", invalid.error_message)
        self.assertFalse(invalid.artifacts.exists())
        self.assertEqual(describe_text_response_review(invalid.preparation)["original_text"], "invalid JSON")
        valid, _ = self.process(response={"text": canonical_json_bytes({"first.layout": rows}).decode("utf-8")})
        self.assertEqual(valid.status, "succeeded", valid.error_message)
        self.run = advance_production_run(self.run)
        self.assertEqual(self.run.current_stage_key, "layout-b", self.run.blocked_reason)
        automatic = ArtifactSelectionDecision.objects.get(stage_run__production_run=self.run, stage_run__stage_key="layout-a")
        self.assertEqual(automatic.artifact.generation_attempt_id, valid.pk)
        self.assertEqual(automatic.selection_mode, "automatic")
        self.assertIsNone(automatic.selected_by_id)
        manual, _ = self.process(response={"text": canonical_json_bytes(rows).decode("utf-8")})
        self.assertEqual(manual.status, "succeeded", manual.error_message)
        self.run = advance_production_run(self.run)
        self.assertEqual((self.run.status, self.run.blocked_code), ("awaiting_input", "execution_candidate_selection_required"), self.run.blocked_reason)
        self.assertFalse(ExecutionPreparation.objects.filter(stage_run__production_run=self.run, stage_run__stage_key="consume-layouts").exists())
        artifact = GeneratedArtifact.objects.get(generation_attempt=manual)
        response = self.client.post(f"/api/v1/production-runs/{self.run.pk}/select-artifact/", {"artifact_id": str(artifact.pk)}, format="json", HTTP_IDEMPOTENCY_KEY="select-layout")
        self.assertEqual(response.status_code, 200, response.data)
        self.run = advance_production_run(self.run)
        self.assertEqual(self.run.current_stage_key, "consume-layouts", self.run.blocked_reason)
        self.assertEqual(self.run.blocked_code, "waiting_external_task", self.run.blocked_reason)
        selected = ArtifactSelectionDecision.objects.get(artifact=artifact)
        self.assertEqual((selected.selection_mode, selected.selected_by_id), ("manual", self.actor.pk))
        prep = ExecutionPreparation.objects.get(stage_run__production_run=self.run, stage_run__stage_key="consume-layouts")
        self.assertEqual(prep.contract.resolved_inputs["inputs"]["logical_values"][STRUCTURED_INPUT_SELECTOR], self.expected)
        attempt = GenerationAttempt.objects.get(preparation=prep)
        request = deepcopy(attempt.request_snapshot)
        self.assertEqual(request["transport"]["prompt_channels"]["values"]["positive_prompt"], "0")
        consumer, _ = self.process(response={"text": "0"})
        self.assertEqual(consumer.status, "succeeded", consumer.error_message)
        self.run = advance_production_run(self.run)
        self.assertEqual(self.run.current_stage_key, "script_generate", self.run.blocked_reason)
        self.assertEqual(list(self.run.stages.filter(stage_key__in=[s["key"] for s in declarations]).values_list("status", flat=True)), ["succeeded"] * 3)
        self.assertEqual(get_run_execution_plan(self.run).value, original_plan)
        advance_production_run(self.run)
        attempt.refresh_from_db()
        self.assertEqual(attempt.request_snapshot, request)
        self.assertEqual(GenerationAttempt.objects.filter(stage_run__production_run=self.run).count(), 6)
        self.assertNotEqual(self.run.status, "succeeded", "Generic steps do not prove media delivery")
