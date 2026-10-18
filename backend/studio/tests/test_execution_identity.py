"""Execution drift tests with isolated resources; no external calls or Smoke."""
import json
from unittest.mock import patch
from uuid import uuid4

from django.core.exceptions import ValidationError
from django.test import TestCase
from django.db import connection
from django.test.utils import CaptureQueriesContext

from studio.models import ModelArtifact, ModelDeployment, ModelProfileVersion, ProviderProfile, SecretBinding, SecretVersion
from studio.services.execution_identity import execution_target_contract, execution_target_contracts
from studio.services.security_boundaries import security_boundary_resolution_cache
from studio.services.json_contracts import assign_json_contract


class ExecutionIdentityTests(TestCase):
    def setUp(self):
        self.provider = ProviderProfile(
            key="identity-fixture", name="Identity fixture", driver_type="openai_compatible", modalities="text",
        )
        self.config = {
            "base_url": "http://127.0.0.1:12345", "chat_path": "/v1/chat/completions",
            "timeout_seconds": 180, "capability_ttl_seconds": 3600,
            "pricing": {"currency": "USD", "fixed_request_cost": "0.01"},
        }
        self.write_config(self.provider, "driver_config", self.config)
        self.model = ModelProfileVersion(
            profile_key="identity-fixture", version=1, model_identifier="fixture-model",
            modality="text", provider=self.provider, status="published",
        )
        assign_json_contract(self.model, "specification", {"capabilities": ["prompt.normalize"]})
        self.model.save()
        self.deployment = ModelDeployment(key="identity-fixture", name="Identity fixture", profile_version=self.model)
        self.write_config(self.deployment, "runtime_config", {})
        for target in ("studio.services.provider_registry._request_json", "studio.services.provider_registry.resolve_credential"):
            guard = patch(target, side_effect=AssertionError("Identity must not call network/decrypt"))
            guard.start()
            self.addCleanup(guard.stop)

    @staticmethod
    def write_config(obj, field, value):
        assign_json_contract(obj, field, value)
        obj.save()

    def identity(self):
        return execution_target_contract(deployment_id=self.deployment.pk)

    def test_stable_and_no_raw_endpoint_or_config_in_identity(self):
        first = self.identity()
        self.assertEqual(first.sha256, self.identity().sha256)
        encoded = json.dumps(first.value)
        self.assertNotIn(self.config["base_url"], encoded)
        self.assertNotIn("timeout_seconds", encoded)
        self.assertEqual(first.value["model"]["id"], str(self.model.pk))

    def test_display_lifecycle_priority_and_ttl_price_do_not_change_target(self):
        before = self.identity().sha256
        ProviderProfile.objects.filter(pk=self.provider.pk).update(name="Renamed", is_active=False)
        ModelDeployment.objects.filter(pk=self.deployment.pk).update(name="Renamed", priority=500, is_default=True, status="disabled")
        self.provider.refresh_from_db()
        self.write_config(self.provider, "driver_config", {
            **self.config, "capability_ttl_seconds": 7200,
            "pricing": {"currency": "USD", "fixed_request_cost": "0.05"},
        })
        self.assertEqual(before, self.identity().sha256)

    def test_execution_config_changes_and_corruption_are_detected(self):
        before = self.identity().sha256
        for key, value in (("chat_path", "/v2/chat/completions"), ("timeout_seconds", 90), ("base_url", "http://127.0.0.1:12346")):
            with self.subTest(key=key):
                self.write_config(self.provider, "driver_config", {**self.config, key: value})
                self.assertNotEqual(before, self.identity().sha256)
        ProviderProfile.objects.filter(pk=self.provider.pk).update(driver_config={**self.config, "timeout_seconds": 75})
        with self.assertRaises(ValidationError):
            self.identity()

    def test_merged_config_does_not_drift_when_shadowed_provider_value_changes(self):
        self.write_config(self.deployment, "runtime_config", {"timeout_seconds": 60})
        before = self.identity().sha256
        self.write_config(self.provider, "driver_config", {**self.config, "timeout_seconds": 90})
        self.assertEqual(before, self.identity().sha256)
        self.write_config(self.deployment, "runtime_config", {"timeout_seconds": 61})
        self.assertNotEqual(before, self.identity().sha256)

    def test_transport_timeout_is_explicit_and_uses_the_selected_deployment_override(self):
        from studio.services.provider_registry import deployment_execution_config_contract

        self.write_config(self.deployment, "runtime_config", {"timeout_seconds": 12.5})
        self.assertEqual(deployment_execution_config_contract(self.deployment).value["timeout_seconds"], 12.5)
        self.write_config(self.deployment, "runtime_config", {})
        self.assertEqual(deployment_execution_config_contract(self.deployment).value["timeout_seconds"], 180)
        self.write_config(self.provider, "driver_config", {key: value for key, value in self.config.items() if key != "timeout_seconds"})
        with self.assertRaises(ValidationError):
            self.identity()
        self.write_config(self.provider, "driver_config", self.config)
        for value in (None, True, False, 0, -1, "12.5", "invalid"):
            with self.subTest(value=value):
                self.write_config(self.deployment, "runtime_config", {"timeout_seconds": value})
                with self.assertRaises(ValidationError):
                    self.identity()

    def test_model_identifier_specification_and_file_changes_are_detected(self):
        before = self.identity().sha256
        ModelProfileVersion.objects.filter(pk=self.model.pk).update(model_identifier="changed-model")
        self.assertNotEqual(before, self.identity().sha256)

        self.model.refresh_from_db()
        before = self.identity().sha256
        self.write_config(self.model, "specification", {"capabilities": ["prompt.normalize", "script.generate"]})
        self.assertNotEqual(before, self.identity().sha256)
        before = self.identity().sha256
        artifact = ModelArtifact.objects.create(profile_version=self.model, role="tokenizer", logical_name="tokenizer", filename="fixture.json", sha256="a" * 64)
        self.assertNotEqual(before, self.identity().sha256)
        before = self.identity().sha256
        ModelArtifact.objects.filter(pk=artifact.pk).update(status="available")
        self.assertEqual(before, self.identity().sha256)
        ModelArtifact.objects.filter(pk=artifact.pk).update(sha256="b" * 64)
        self.assertNotEqual(before, self.identity().sha256)

    def test_all_registered_text_transports_share_strict_numeric_timeout_validation(self):
        from studio.services.provider_registry import DeepSeekDriver, LocalQwenDriver, OpenAICompatibleDriver

        with security_boundary_resolution_cache():
            for driver_class in (OpenAICompatibleDriver, DeepSeekDriver, LocalQwenDriver):
                config = {**self.config, **({"runtime_source": "openai_compatible_endpoint"}
                    if driver_class is not OpenAICompatibleDriver else {})}
                driver = driver_class()
                driver.validate_config({**config, "timeout_seconds": 12.5})
                for value in (None, True, False, "12.5", 0, -1, float("nan"), float("inf")):
                    with self.subTest(driver=driver_class.__name__, value=value), self.assertRaises(ValidationError):
                        driver.validate_config({**config, "timeout_seconds": value})

    def binding(self, key):
        binding = SecretBinding.objects.create(key=key, label="Fixture", purpose="test")
        self.rotate(binding, 1)
        return binding

    @staticmethod
    def rotate(binding, number):
        # Deliberately not decryptable: this test proves reference-only reads.
        version = SecretVersion.objects.create(
            binding=binding, version=number, key_id="not-a-live-key",
            nonce_b64="fixture", ciphertext_b64="not-secret-material", ciphertext_sha256="a" * 64,
        )
        SecretBinding.objects.filter(pk=binding.pk).update(active_version=version)

    def test_credential_rotation_and_inheritance_are_current_and_reference_only(self):
        provider_binding = self.binding("identity-provider")
        ProviderProfile.objects.filter(pk=self.provider.pk).update(credential=provider_binding)
        before = self.identity().sha256
        self.rotate(provider_binding, 2)
        self.assertNotEqual(before, self.identity().sha256)
        own_binding = self.binding("identity-deployment")
        ModelDeployment.objects.filter(pk=self.deployment.pk).update(credential=own_binding)
        before = self.identity().sha256
        self.rotate(provider_binding, 3)
        self.assertEqual(before, self.identity().sha256)
        self.rotate(own_binding, 2)
        self.assertNotEqual(before, self.identity().sha256)
        self.assertNotIn("not-secret-material", json.dumps(self.identity().value))

    def test_invalid_runtime_config_does_not_become_an_identity(self):
        self.write_config(self.deployment, "runtime_config", {"unknown_execution_key": "no"})
        with self.assertRaises(ValidationError):
            self.identity()

    def test_batch_identity_matches_single_reads_and_queries_do_not_grow_per_resource(self):
        deployments = [self.deployment]
        for index in range(4):
            row = ModelDeployment(key=f"batch-{index}", name="Batch", profile_version=self.model)
            self.write_config(row, "runtime_config", {})
            deployments.append(row)
        ModelArtifact.objects.create(profile_version=self.model, role="tokenizer", logical_name="b",
            filename="b.json", sha256="b" * 64)
        ModelArtifact.objects.create(profile_version=self.model, role="tokenizer", logical_name="a",
            filename="a.json", sha256="a" * 64)
        ids = [row.pk for row in deployments]
        with security_boundary_resolution_cache():
            expected = {str(pk): execution_target_contract(deployment_id=pk) for pk in ids}
            with CaptureQueriesContext(connection) as one_queries:
                execution_target_contracts(deployment_ids=ids[:1])
            with CaptureQueriesContext(connection) as many_queries:
                actual = execution_target_contracts(deployment_ids=ids)
        self.assertEqual(actual, expected)
        self.assertEqual(len(many_queries), len(one_queries))
        self.assertEqual(len(many_queries), 2)
        if connection.vendor == "sqlite":
            with connection.cursor() as cursor:
                for query in many_queries:
                    cursor.execute("EXPLAIN QUERY PLAN " + query["sql"])
                    self.assertIn("INDEX", " ".join(row[-1] for row in cursor.fetchall()).upper())

    def test_batch_partial_failure_missing_resource_and_current_rotation_are_explicit(self):
        broken = ModelDeployment(key="broken-batch", name="Broken", profile_version=self.model)
        self.write_config(broken, "runtime_config", {"unknown_execution_key": "invalid"})
        missing_id = uuid4()
        ids = [self.deployment.pk, broken.pk, missing_id]
        result = execution_target_contracts(deployment_ids=ids)
        self.assertEqual(result[str(self.deployment.pk)], self.identity())
        self.assertIsInstance(result[str(broken.pk)], ValidationError)
        self.assertIsInstance(result[str(missing_id)], ValidationError)
        before = result[str(self.deployment.pk)]
        binding = self.binding("batch-credential")
        ProviderProfile.objects.filter(pk=self.provider.pk).update(credential=binding)
        current = execution_target_contracts(deployment_ids=ids)
        self.assertNotEqual(before, current[str(self.deployment.pk)])
        self.assertEqual(current[str(self.deployment.pk)], self.identity())
        with self.assertNumQueries(0):
            self.assertEqual(execution_target_contracts(deployment_ids=[]), {})
