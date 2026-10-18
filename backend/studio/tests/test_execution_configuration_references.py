"""Version recommendations are explicit configuration, not runtime admission."""
from copy import deepcopy
from unittest.mock import patch
from uuid import uuid4

from django.core.exceptions import ValidationError
from django.db import connection
from django.test.utils import CaptureQueriesContext
from rest_framework.test import APIClient

from studio.models import ConfigDefinition, ExecutionVersionContract, ModelExecutionInstanceVersion
from studio.services.configuration import (
    config_resource_reference_candidates, config_resource_reference_options,
    validate_config_resource_reference_schema, validate_config_value,
)
from studio.services.json_contracts import assign_json_contract
from studio.services.security_boundaries import security_boundary_resolution_cache, boundary_int
from studio.tests import test_execution_definitions as fixtures


class ExecutionConfigurationReferenceTests(fixtures.ExecutionDefinitionTests):
    def setUp(self):
        super().setUp()
        self.reference = {"resource": "execution_instance_version", "capability": self.version.operation, "allow_empty": True}
        self.config_definition = ConfigDefinition(key="execution.test_recommendation", domain="execution",
            value_type="string", allowed_scopes="system", is_required=False)
        assign_json_contract(self.config_definition, "value_schema", {"resource_ref": self.reference})
        assign_json_contract(self.config_definition, "initial_value", "")
        self.config_definition.save()

    def options(self):
        return config_resource_reference_options(self.config_definition,
            config_resource_reference_candidates(self.config_definition))

    def test_published_exact_reference_does_not_require_or_create_smoke(self):
        published = self.publish()
        with patch("studio.services.execution_admission.evaluate_execution_admission", side_effect=AssertionError("Not a call")):
            validate_config_value(self.config_definition, str(published.pk))
            rows = self.options()
        self.assertEqual([(r["value"], r["state"], r["is_selectable"]) for r in rows], [(str(published.pk), "configured", True)])
        self.assertFalse(published.deployment_smoke_attempts.exists())

    def test_draft_missing_foreign_operation_and_deployment_uuid_are_rejected(self):
        self.assertEqual(self.options()[0]["reason_code"], "execution_instance_inactive")
        for value in (self.version.pk, self.deployment.pk, uuid4()):
            with self.subTest(value=value), self.assertRaises(ValidationError):
                validate_config_value(self.config_definition, str(value))
        published = self.publish()
        other = deepcopy(self.reference)
        other["capability"] = "image.text_to_image"
        assign_json_contract(self.config_definition, "value_schema", {"resource_ref": other})
        self.config_definition.save()
        self.assertEqual(self.options(), [])
        with self.assertRaises(ValidationError):
            validate_config_value(self.config_definition, str(published.pk))

    def test_empty_is_not_implicit_selection_and_unknown_schema_is_rejected(self):
        validate_config_value(self.config_definition, "")
        for change in ({"resource": "model_deployment"}, {"require_ready": True}, {"allow_empty": "true"}, {"capability": ""}):
            with self.subTest(change=change), self.assertRaises(ValidationError):
                validate_config_resource_reference_schema({**self.reference, **change})
        self.config_definition.value_schema["resource_ref"]["allow_empty"] = False
        with self.assertRaises(ValidationError):
            validate_config_value(self.config_definition, "")

    def test_disabled_or_tampered_target_is_not_a_valid_new_recommendation(self):
        published = self.publish()
        published.instance.is_active = False
        published.instance.save()
        self.assertFalse(self.options()[0]["is_selectable"])
        with self.assertRaises(ValidationError):
            validate_config_value(self.config_definition, str(published.pk))
        # Reading an unchanged revision is not a live availability decision.
        validate_config_value(self.config_definition, str(published.pk), validate_resource_state=False)
        published.instance.is_active = True
        published.instance.save()
        ExecutionVersionContract.objects.filter(version=published).update(sha256="0" * 64)
        self.assertEqual(self.options()[0]["reason_code"], "execution_definition_conflict")
        with self.assertRaises(ValidationError):
            validate_config_value(self.config_definition, str(published.pk))

    def test_live_definition_drift_rechecked_on_write_not_hidden_by_stored_hash(self):
        published = self.publish()
        ModelExecutionInstanceVersion.objects.filter(pk=published.pk).update(mode="I2VA")
        with self.assertRaises(ValidationError):
            validate_config_value(self.config_definition, str(published.pk))

    def test_options_are_paginated_staff_only_and_do_not_fetch_another_page(self):
        self.publish()
        ModelExecutionInstanceVersion.objects.create(instance=self.instance, version=2,
            deployment=self.deployment, operation=self.version.operation, mode="T2VA", prompt_mode="manual")
        client = APIClient()
        url = f"/api/v1/config-definitions/{self.config_definition.pk}/resource-options/"
        self.assertIn(client.get(url).status_code, (401, 403))
        client.force_authenticate(self.actor)
        response = client.get(url, {"page_size": 1})
        self.assertEqual(response.status_code, 200, response.data)
        self.assertEqual(response.data["count"], 2)
        self.assertEqual(len(response.data["results"]), 1)
        self.assertIsNotNone(response.data["next"])
        self.assertEqual(response.data["results"][0]["state"], "unavailable")
        with security_boundary_resolution_cache():
            boundary_int("security.snapshot.stage_max_bytes")
            with CaptureQueriesContext(connection) as queries:
                rows = config_resource_reference_options(self.config_definition,
                    config_resource_reference_candidates(self.config_definition)[:1])
            self.assertEqual(len(rows), 1)
            self.assertEqual(len(queries), 1)
