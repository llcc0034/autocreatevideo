"""Exact read-only mapping evidence, no provider/network/credential access."""
from copy import deepcopy
import json
from unittest.mock import patch

from django.core.exceptions import ValidationError
from django.db import connection
from django.test import TestCase, override_settings
from django.test.utils import CaptureQueriesContext
from rest_framework.test import APIClient

from studio.models import ExecutionVersionContract, ModelDeploymentSmokeAttempt, PlatformLifecycleAuditEvent
from studio.services.execution_definitions import build_execution_definition, read_execution_input_contract
from studio.services.execution_instances import create_execution_draft, publish_execution_version
from studio.services.execution_parameter_mappings import execution_parameter_mappings
from studio.services.workflow_versions import import_workflow_version, workflow_version_execution_snapshot
from studio.tests import test_execution_definitions as fixtures


@override_settings(AI_PROVIDER_ALLOWED_HOSTS=("api.example.com", "cdn.example.com"))
class ApiParameterMappingTests(TestCase):
    setUpTestData = classmethod(fixtures.ExecutionDefinitionTests.setUpTestData.__func__)
    setUp = fixtures.ExecutionDefinitionTests.setUp
    write_contract = staticmethod(fixtures.ExecutionDefinitionTests.write_contract)
    publish = fixtures.ExecutionDefinitionTests.publish
    definition = fixtures.ExecutionDefinitionTests.definition

    def read(self):
        return execution_parameter_mappings(self.version.pk, expected_definition_sha256=self.definition_sha)

    def test_maps_only_declared_scalars_without_copying_private_config_or_writing(self):
        self.write_contract(self.provider, "driver_config", {**self.config, "request_field_map": {
            **self.config["request_field_map"], "width": "render_width"}})
        published = self.publish()
        self.definition_sha = published.contract.sha256
        before = (ExecutionVersionContract.objects.count(), PlatformLifecycleAuditEvent.objects.count(), ModelDeploymentSmokeAttempt.objects.count())
        with CaptureQueriesContext(connection) as queries:
            result = self.read()
        self.assertEqual(result["status"], "verified")
        self.assertEqual(result["basis_kind"], "deployment")
        rows = {row["key"]: row["destinations"] for row in result["rows"]}
        self.assertEqual(rows["width"], [{"node_id": None, "field": "render_width"}])
        self.assertEqual(rows["duration_seconds"], [{"node_id": None, "field": "duration"}])
        self.assertEqual(set(rows), set(self.definition().value["interface"]["parameters"]))
        encoded = json.dumps(result)
        for private in ("base_url", "api.example.com", "timeout_seconds", "smoke_inputs", "idempotency_header"):
            self.assertNotIn(private, encoded)
        self.assertFalse(any(q["sql"].lstrip().startswith(("INSERT", "UPDATE", "DELETE")) for q in queries))
        self.assertEqual(before, (ExecutionVersionContract.objects.count(), PlatformLifecycleAuditEvent.objects.count(), ModelDeploymentSmokeAttempt.objects.count()))
        self.assertEqual(published.contract.sha256, self.definition_sha)

    def test_changed_valid_transport_is_unavailable_not_current_mapping(self):
        published = self.publish(); self.definition_sha = published.contract.sha256
        self.write_contract(self.provider, "driver_config", {**self.config, "request_field_map": {
            **self.config["request_field_map"], "width": "new_width"}})
        result = self.read()
        self.assertEqual(result["code"], "deployment_mapping_changed")
        self.assertEqual(result["rows"], [])
        self.assertNotIn("new_width", json.dumps(result))
        self.assertEqual(read_execution_input_contract(published.pk)["sha256"], self.definition_sha)

    def test_invalid_config_message_does_not_leak_private_error(self):
        published = self.publish(); self.definition_sha = published.contract.sha256
        with patch("studio.services.execution_parameter_mappings.deployment_execution_config_contract",
                   side_effect=ValidationError("secret endpoint/private-file must not escape")):
            result = self.read()
        self.assertEqual(result["code"], "mapping_evidence_invalid")
        self.assertNotIn("secret endpoint", json.dumps(result))

    def test_api_hash_permissions_query_contract_and_disabled_instance(self):
        published = self.publish(); self.definition_sha = published.contract.sha256
        client = APIClient()
        path = f"/api/v1/model-execution-instance-versions/{published.pk}/parameter-mappings/"
        query = {"expected_definition_sha256": self.definition_sha}
        self.assertIn(client.get(path, query).status_code, (401, 403))
        self.actor.is_staff = False; self.actor.save(update_fields=["is_staff"])
        client.force_authenticate(self.actor)
        response = client.get(path, query)
        self.assertEqual(response.status_code, 200, response.data)
        self.assertEqual(response["Cache-Control"], "private, no-store")
        self.assertEqual(response.data["definition_sha256"], self.definition_sha)
        self.assertEqual(client.get(path, {"expected_definition_sha256": "0" * 64}).status_code, 409)
        for invalid in ({}, {**query, "extra": "x"}, {"expected_definition_sha256": [self.definition_sha, self.definition_sha]}):
            self.assertEqual(client.get(path, invalid).status_code, 400)
        self.instance.is_active = False; self.instance.save(update_fields=["is_active"])
        self.assertEqual(client.get(path, query).status_code, 404)

    def test_draft_query_does_not_freeze_and_old_hash_is_rejected(self):
        self.definition_sha = self.definition().sha256
        self.assertEqual(self.read()["status"], "verified")
        self.assertFalse(ExecutionVersionContract.objects.exists())
        self.write_contract(self.provider, "driver_config", {**self.config, "timeout_seconds": 31})
        with self.assertRaises(ValidationError) as exc:
            self.read()
        self.assertEqual(exc.exception.code, "execution_definition_conflict")

    def test_unrecorded_text_adapter_never_infers_same_named_wire_keys(self):
        contract = read_execution_input_contract(self.version.pk)
        contract["definition"]["target"]["driver_type"] = "deepseek_api"
        with patch("studio.services.execution_parameter_mappings.read_execution_input_contract", return_value=contract):
            self.definition_sha = contract["sha256"]
            result = self.read()
        self.assertEqual((result["status"], result["rows"], result["basis_id"]), ("not_recorded", [], None))


class WorkflowParameterMappingTests(TestCase):
    setUp = fixtures.WorkflowExecutionDefinitionTests.setUp

    def freeze(self):
        definition = build_execution_definition(self.version.pk)
        self.version = publish_execution_version(version_id=self.version.pk, expected_revision=self.version.revision,
            expected_definition_sha256=definition.sha256, actor=self.user)
        self.definition_sha = definition.sha256

    def read(self):
        return execution_parameter_mappings(self.version.pk, expected_definition_sha256=self.definition_sha)

    def test_frozen_bindings_are_exact_and_queries_do_not_scale_per_parameter(self):
        self.freeze()
        with CaptureQueriesContext(connection) as queries:
            result = self.read()
        self.assertEqual((result["status"], result["basis_kind"]), ("verified", "workflow"))
        self.assertEqual(result["basis_sha256"], self.workflow.execution_contract_sha256)
        rows = {row["key"]: row["destinations"] for row in result["rows"]}
        self.assertEqual(rows["image.width"], [{"node_id": "4", "field": "width"}])
        self.assertEqual(rows["sampling.seed"], [{"node_id": "5", "field": "seed"}])
        self.assertNotIn("prompt.positive", rows)
        self.assertNotIn("imported sample only", json.dumps(result))
        bindings_queries = [q["sql"] for q in queries if 'FROM "studio_workflowsemanticbinding"' in q["sql"]]
        self.assertEqual(len(bindings_queries), 1)
        # One request-local boundary snapshot, not hundreds of repeated config reads.
        self.assertLess(len(queries), 100)

    def test_one_parameter_to_multiple_declared_nodes_keeps_all_destinations(self):
        snapshot = workflow_version_execution_snapshot(self.workflow)
        snapshot["graph"]["8"] = deepcopy(snapshot["graph"]["4"])
        snapshot["semantic_bindings"].append({"semantic_key": "image.width", "node_id": "8", "input_name": "width", "value_type": "integer", "is_required": True})
        snapshot["semantic_bindings"].append({"semantic_key": "image.height", "node_id": "8", "input_name": "height", "value_type": "integer", "is_required": True})
        snapshot["input_contract"]["resolution_strategy"]["node_ids"].append("8")
        workflow = import_workflow_version(template=self.workflow.template, graph=snapshot["graph"],
            input_contract=snapshot["input_contract"], semantic_bindings=snapshot["semantic_bindings"],
            model_requirements=snapshot["model_requirements"], provider=self.provider, model_profile_version=self.profile)
        workflow.status = "published"; workflow.validated_deployment = self.deployment; workflow.save()
        self.instance.refresh_from_db()
        self.version = create_execution_draft(instance_id=self.instance.pk, expected_revision=self.instance.revision,
            actor=self.user, selection={"deployment": self.deployment, "operation": "image.text_to_image", "mode": "",
                "prompt_mode": "manual", "prompt_profile_version": None, "workflow_version": workflow})
        self.freeze()
        result = self.read()
        row = next(row for row in result["rows"] if row["key"] == "image.width")
        self.assertEqual(row["destinations"], [{"node_id": "4", "field": "width"}, {"node_id": "8", "field": "width"}])

    def test_tampered_binding_cannot_be_shown_as_verified_history(self):
        self.freeze()
        self.workflow.semantic_bindings.filter(semantic_key="image.width").update(input_name="batch_size")
        result = self.read()
        self.assertEqual((result["status"], result["code"], result["rows"]), ("unavailable", "mapping_evidence_invalid", []))
        self.assertEqual(read_execution_input_contract(self.version.pk)["sha256"], self.definition_sha)

    def test_missing_exact_resource_is_not_replaced(self):
        self.freeze()
        with patch("studio.services.execution_parameter_mappings.WorkflowTemplateVersion.objects.select_related") as lookup:
            from studio.models import WorkflowTemplateVersion
            lookup.return_value.prefetch_related.return_value.get.side_effect = WorkflowTemplateVersion.DoesNotExist()
            result = self.read()
        self.assertEqual((result["status"], result["code"]), ("unavailable", "mapping_resource_missing"))
