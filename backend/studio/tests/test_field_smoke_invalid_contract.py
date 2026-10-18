"""Invalid frozen leaf evidence is an explicit failure, never a fallback pass."""
from copy import deepcopy
from unittest.mock import patch

from django.core.exceptions import ValidationError
from django.test import TestCase, override_settings

from studio.models import AssetGenerationJob, ExecutionPreparation, ModelDeploymentSmokeAttempt
from studio.services.execution_catalog_readiness import ExecutionRequirements, catalog_capability_readiness
from studio.services.execution_field_smoke import verify_instance_field_fixture
from studio.tests import test_execution_field_smoke as fixtures


@override_settings(AI_PROVIDER_ALLOWED_HOSTS=("api.example.com",))
class InvalidFieldSmokeContractTests(TestCase):
    setUpTestData = fixtures.ExecutionFieldSmokeTests.__dict__["setUpTestData"]
    setUp = fixtures.ExecutionFieldSmokeTests.setUp
    version = fixtures.ExecutionFieldSmokeTests.version
    build = fixtures.ExecutionFieldSmokeTests.build
    smoke = fixtures.ExecutionFieldSmokeTests.smoke

    def malformed(self):
        source = self.build()
        source["source"]["value"]["items"][0]["field_contract"].pop("projection_primitive")
        return source

    def verify(self, source):
        return verify_instance_field_fixture(self.definition,
            {"operation_inputs": {"field_request": source["source"]["value"]}}, allow_retired=True)

    def test_invalid_frozen_rule_is_validation_error_and_new_api_allocates_nothing(self):
        valid = self.build()
        self.assertEqual(self.verify(valid).value, valid["source"]["value"])
        source = self.malformed()
        before = deepcopy(source)
        with self.assertRaises(ValidationError) as raised:
            self.verify(source)
        self.assertIn("items.0.field_contract", str(raised.exception))
        rejected = self.smoke(source, "invalid-rule")
        self.assertEqual(rejected.status_code, 400, rejected.data)
        self.assertEqual(source, before)
        self.assertFalse(ExecutionPreparation.objects.exists())
        self.assertFalse(ModelDeploymentSmokeAttempt.objects.exists())
        self.assertFalse(AssetGenerationJob.objects.exists())

    def test_catalog_keeps_primary_failure_and_conditional_warning_without_inventing_evidence(self):
        source = self.malformed()
        identifier = str(self.leaf.pk)
        for primary in (True, False):
            with self.subTest(primary=primary):
                requirements = ExecutionRequirements(versions={identifier: (self.leaf, self.definition)},
                    primary_version_ids={identifier} if primary else set(),
                    route_groups=[(identifier,)] if primary else [])
                with patch("studio.services.execution_resource_admission.read_execution_resource_state") as resource, \
                        patch("studio.services.execution_smoke_evidence.read_instance_smoke_facts",
                            side_effect=lambda *_: self.verify(source)) as reader:
                    resource.return_value.value = {"blockers": []}
                    report = catalog_capability_readiness(requirements,
                        worker_check=lambda _: {"ok": True}, toolchain_check=lambda: {"ok": True})
                row = report["text"]
                self.assertEqual(row["reason_codes"], ["execution_evidence_unverifiable"] if primary else [])
                self.assertEqual(row["warning_codes"], [] if primary else ["execution_auxiliary_attention"])
                self.assertEqual(row["state"], "blocked" if primary else "ready")
                self.assertEqual(row["viable_instance_count"], 0)
                self.assertNotIn("field_contract", str(report))
                reader.assert_called_once()
        self.assertFalse(ExecutionPreparation.objects.exists())
        self.assertFalse(ModelDeploymentSmokeAttempt.objects.exists())

    def test_unexpected_runtime_failure_is_not_relabelled_as_invalid_contract(self):
        source = self.build()
        with patch("studio.services.execution_field_smoke.verify_execution_field_request",
                side_effect=RuntimeError("unexpected-reader-failure")):
            with self.assertRaisesRegex(RuntimeError, "unexpected-reader-failure"):
                self.verify(source)
