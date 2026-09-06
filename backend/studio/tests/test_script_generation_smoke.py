from studio.tests.deployment_smoke_fixtures import run_test_deployment_smoke
import json
from unittest.mock import Mock, patch

from django.contrib.auth import get_user_model
from django.core.exceptions import ValidationError
from django.test import TestCase, override_settings
from rest_framework.test import APIClient

from studio.models import (
    ModelDeployment,
    ModelDeploymentCapabilitySnapshot,
    ModelDeploymentSmokeAttempt,
    ModelProfileVersion,
    ProductionRecipeVersion,
    ProviderProfile,
)

from studio.services.deployment_smoke_queue import (
    claim_next_deployment_smoke,
    process_deployment_smoke,
)
from studio.services.deployment_smoke import _script_generation_policy
from studio.services.json_contracts import assign_json_contract, json_contract
from studio.services.provider_registry import ProviderDriverError
from studio.services.readiness import collect_production_readiness
from studio.services.series_concept_drafts import SERIES_CONCEPT_DRAFT_SCHEMA_VERSION
from studio.tests.factories import series_concept_field_contract, create_test_prompt_profile_binding


@override_settings(AI_PROVIDER_ALLOWED_HOSTS=("approved.example",))
class ScriptGenerationDeploymentSmokeTests(TestCase):
    def setUp(self):
        self.staff = get_user_model().objects.create_user(
            username="script-smoke-staff",
            password="StrongPass123!",
            is_staff=True,
        )
        provider = ProviderProfile(
            key="script-smoke-provider",
            name="Script smoke provider",
            driver_type=ProviderProfile.DriverType.OPENAI_COMPATIBLE,
            modalities="text",
            operations="chat_completion,script.generate",
            is_active=True,
        )
        assign_json_contract(provider, "driver_config", {
            "base_url": "https://approved.example",
            "chat_path": "/v1/chat/completions",
            "timeout_seconds": 30,
            "capability_ttl_seconds": 3_600,
            "pricing": {"currency": "USD", "fixed_per_request": "0.01"},
        })
        provider.save()
        self.profile = ModelProfileVersion(
            profile_key="script-smoke-model",
            version=1,
            provider=provider,
            model_identifier="script-smoke-v1",
            modality=ModelProfileVersion.Modality.TEXT,
            status=ModelProfileVersion.Status.PUBLISHED,
        )
        assign_json_contract(self.profile, "specification", {
            "schema_version": 1,
            "model_family": "script-smoke",
            "capabilities": ["script.generate"],
            "series_concept": {
                "minimum_shot_duration_ms": 1000,
                "maximum_shot_duration_ms": 6000,
                "max_output_tokens": 4096,
                "timeout_seconds": 30,
                "field_contract": series_concept_field_contract(),
                "chat_request_options": {
                    "temperature": 0.3,
                    "response_format": {"type": "json_object"},
                },
            },
        })
        self.profile.save()
        self.deployment = ModelDeployment(
            key="script-smoke-deployment",
            name="Script smoke deployment",
            profile_version=self.profile,
            status=ModelDeployment.Status.ACTIVE,
        )
        assign_json_contract(self.deployment, "runtime_config", {})
        self.deployment.save()
        self.binding = create_test_prompt_profile_binding(actor=self.staff, model_profile_version=self.profile, operation="script.generate")

    @staticmethod
    def _candidate_text():
        return json.dumps({
            "schema_version": SERIES_CONCEPT_DRAFT_SCHEMA_VERSION,
            "title": "灯塔来信",
            "logline": "旅人在灯塔中发现一封改变航程的信。",
            "bible": {
                "characters": [{
                    "name": "旅人",
                    "description": "谨慎的独行者",
                    "visual_identity_en": "a cautious lone traveler in a dark coat",
                }],
                "scenes": [{
                    "name": "灯塔",
                    "description": "风雨中的旧灯塔",
                    "visual_identity_en": "an old lighthouse in a coastal storm",
                }],
                "props": [{
                    "name": "信",
                    "description": "一封密封的旧信",
                    "visual_identity_en": "a weathered sealed letter",
                }],
            },
            "episodes": [{
                "title": "来信",
                "shots": [{
                    "setting": "灯塔",
                    "action": "旅人拆开旧信。",
                    "duration_ms": 1000,
                    "dialogue": {
                        "speaker": "旅人",
                        "delivery": "quietly",
                        "text": "启程。",
                        "start_ms": 100,
                        "end_ms": 900,
                    },
                }],
            }],
        }, ensure_ascii=False)

    def _driver(self):
        driver = Mock()
        driver.estimate.return_value = {
            "schema_version": 1,
            "amount": "0.01",
            "currency": "USD",
            "basis": "fixed_per_request",
        }
        driver.invoke.return_value = {
            "schema_version": 1,
            "request_id": "provider-request-plaintext",
            "text": self._candidate_text(),
            "usage": {"prompt_tokens": 10, "completion_tokens": 20},
        }
        return driver

    def test_api_queues_tiny_strict_smoke_and_worker_persists_hash_only_evidence(self):
        client = APIClient()
        client.force_authenticate(self.staff)
        driver = self._driver()

        with patch("studio.services.deployment_smoke.driver_for", return_value=driver):
            response = client.post(
                f"/api/v1/model-deployments/{self.deployment.id}/smoke-test/",
                {"operation": "script.generate", "prompt_profile_binding_id": str(self.binding.id)},
                format="json",
            )
            self.assertEqual(response.status_code, 202, response.data)
            attempt = claim_next_deployment_smoke("script-smoke-test-worker")
            finished = process_deployment_smoke(attempt)

        self.assertEqual(response.status_code, 202, response.data)
        self.assertEqual(finished.status, ModelDeploymentSmokeAttempt.Status.SUCCEEDED)
        snapshot = finished.final_snapshot
        self.assertEqual(snapshot.status, ModelDeploymentCapabilitySnapshot.Status.PASSED)
        self.assertEqual(snapshot.operations, ["script.generate"])
        self.assertTrue(snapshot.evidence["execution_submitted"])
        self.assertEqual(snapshot.evidence["estimated_cost"], "0.010000")
        self.assertEqual(len(snapshot.evidence["request_sha256"]), 64)
        self.assertEqual(len(snapshot.evidence["response_sha256"]), 64)
        self.assertEqual(len(snapshot.evidence["result_sha256"]), 64)
        persisted = json.dumps(snapshot.evidence, ensure_ascii=False)
        self.assertNotIn("旅人在灯塔", persisted)
        self.assertNotIn("灯塔来信", persisted)
        self.assertNotIn("provider-request-plaintext", persisted)
        _, operation, payload = driver.invoke.call_args.args
        self.assertEqual(operation, "chat_completion")
        self.assertEqual(payload["max_tokens"], 4096)
        request_body = json.loads(payload["messages"][1]["content"])
        self.assertEqual(request_body["episode_count"], 1)
        self.assertEqual(request_body["shots_per_episode"], 1)

    def test_unknown_pricing_rejects_before_queuing_or_submission(self):
        driver = self._driver()
        driver.estimate.return_value = {
            "schema_version": 1,
            "amount": None,
            "currency": "",
            "basis": "unknown",
        }

        with patch("studio.services.deployment_smoke.driver_for", return_value=driver):
            with self.assertRaisesMessage(ValidationError, "pricing"):
                run_test_deployment_smoke(self.deployment, operation="script.generate")

        self.assertFalse(ModelDeploymentSmokeAttempt.objects.exists())
        driver.invoke.assert_not_called()

    def test_profile_requires_exact_capability_and_complete_series_policy(self):
        specification = dict(self.profile.specification)
        specification["capabilities"] = ["script.generate "]
        specification["operations"] = ["script.generate"]
        assign_json_contract(self.profile, "specification", specification)
        self.profile.save(update_fields=(
            "specification",
            "specification_schema_version",
            "specification_size_bytes",
            "specification_sha256",
            "updated_at",
        ))
        with self.assertRaisesMessage(ValidationError, "只能在 specification.capabilities"):
            _script_generation_policy(self.deployment)
        self.assertFalse(ModelDeploymentCapabilitySnapshot.objects.exists())

        specification.pop("operations")
        specification["capabilities"] = ["script.generate"]
        specification["series_concept"] = {
            "minimum_shot_duration_ms": 1000,
            "maximum_shot_duration_ms": 6000,
            "max_output_tokens": 4096,
            "chat_request_options": {
                "temperature": 0.3,
                "response_format": {"type": "json_object"},
            },
        }
        assign_json_contract(self.profile, "specification", specification)
        self.profile.save(update_fields=(
            "specification",
            "specification_schema_version",
            "specification_size_bytes",
            "specification_sha256",
            "updated_at",
        ))
        with self.assertRaisesMessage(ValidationError, "精确 series_concept policy"):
            _script_generation_policy(self.deployment)
        self.assertFalse(ModelDeploymentCapabilitySnapshot.objects.exists())

    def test_publish_rejects_unsupported_script_generate_profile_schema(self):
        draft = ModelProfileVersion(
            profile_key="unsupported-script-contract",
            version=1,
            provider=self.profile.provider,
            model_identifier="unsupported-script-contract",
            modality=ModelProfileVersion.Modality.TEXT,
        )
        assign_json_contract(draft, "specification", {
            **self.profile.specification,
            "schema_version": 2,
        })
        draft.save()
        client = APIClient()
        client.force_authenticate(self.staff)

        response = client.post(
            f"/api/v1/model-profile-versions/{draft.id}/publish/",
            {},
            format="json",
        )

        self.assertEqual(response.status_code, 400, response.data)
        self.assertIn("schema_version", str(response.data))
        draft.refresh_from_db()
        self.assertEqual(draft.status, ModelProfileVersion.Status.DRAFT)

    def test_historical_illegal_published_script_profile_is_retained_but_blocks_readiness(self):
        recipe = ProductionRecipeVersion(
            recipe_key="historical-illegal-script",
            version=1,
            name="Historical illegal script",
            status=ProductionRecipeVersion.Status.PUBLISHED,
        )
        assign_json_contract(recipe, "definition", {
            "schema_version": 1,
            "stages": [{
                "key": "script_generate",
                "mode": "ai",
                "approval": "auto",
                "routing": {
                    "mode": "fixed",
                    "routes": [{"model_deployment_id": str(self.deployment.id)}],
                    "variant_count": 1,
                },
            }],
        })
        recipe.save()
        invalid = {
            **self.profile.specification,
            "schema_version": 2,
        }
        contract = json_contract(invalid)
        ModelProfileVersion.objects.filter(pk=self.profile.pk).update(
            specification=contract.value,
            specification_schema_version=contract.schema_version,
            specification_size_bytes=contract.size_bytes,
            specification_sha256=contract.sha256,
        )

        report = collect_production_readiness()

        self.assertEqual(report["status"], "blocked")
        self.assertIn("script_generate_contract_invalid", report["reason_codes"])
        self.assertTrue(ProductionRecipeVersion.objects.filter(
            pk=recipe.pk,
            status=ProductionRecipeVersion.Status.PUBLISHED,
        ).exists())
        self.assertTrue(ModelProfileVersion.objects.filter(
            pk=self.profile.pk,
            status=ModelProfileVersion.Status.PUBLISHED,
        ).exists())

    def test_uncertain_submission_is_failed_and_never_persists_error_message(self):
        driver = self._driver()
        error = ProviderDriverError(
            "upstream may have received secret request body",
            code="provider_request_failed",
            submission_uncertain=True,
        )
        error.metadata = {"contract_error_code": "response_output_budget_exhausted"}
        driver.invoke.side_effect = error

        with patch("studio.services.deployment_smoke.driver_for", return_value=driver):
            snapshot = run_test_deployment_smoke(self.deployment, operation="script.generate")

        self.assertEqual(snapshot.status, ModelDeploymentCapabilitySnapshot.Status.FAILED)
        self.assertIs(snapshot.evidence["execution_submitted"], True)
        self.assertIs(snapshot.evidence["submission_uncertain"], True)
        self.assertEqual(snapshot.evidence["error_code"], "provider_request_failed")
        self.assertEqual(
            snapshot.evidence["contract_error_code"],
            "response_output_budget_exhausted",
        )
        self.assertNotIn("secret request body", json.dumps(snapshot.evidence))

    def test_strict_finalize_failure_records_only_response_hash_and_error_code(self):
        driver = self._driver()
        driver.invoke.return_value = {
            "schema_version": 1,
            "request_id": "invalid-response-id",
            "text": '{"title":"not-the-contract"}',
        }

        with patch("studio.services.deployment_smoke.driver_for", return_value=driver):
            snapshot = run_test_deployment_smoke(self.deployment, operation="script.generate")

        self.assertEqual(snapshot.status, ModelDeploymentCapabilitySnapshot.Status.FAILED)
        self.assertEqual(snapshot.evidence["error_code"], "series_concept_invalid_response")
        self.assertEqual(
            snapshot.evidence["contract_error_code"],
            "series_concept_invalid_candidate",
        )
        self.assertEqual(len(snapshot.evidence["response_sha256"]), 64)
        persisted = json.dumps(snapshot.evidence)
        self.assertNotIn("not-the-contract", persisted)
        self.assertNotIn("invalid-response-id", persisted)

    def test_target_drift_after_invoke_forces_failed_snapshot(self):
        driver = self._driver()

        def invoke_and_drift(*_args):
            assign_json_contract(self.deployment, "runtime_config", {
                "timeout_seconds": 31,
            })
            self.deployment.save(update_fields=(
                "runtime_config",
                "runtime_config_schema_version",
                "runtime_config_size_bytes",
                "runtime_config_sha256",
                "updated_at",
            ))
            return {
                "schema_version": 1,
                "request_id": "drift-request",
                "text": self._candidate_text(),
            }

        driver.invoke.side_effect = invoke_and_drift
        with patch("studio.services.deployment_smoke.driver_for", return_value=driver):
            snapshot = run_test_deployment_smoke(self.deployment, operation="script.generate")

        self.assertEqual(snapshot.status, ModelDeploymentCapabilitySnapshot.Status.FAILED)
        self.assertEqual(snapshot.evidence["error_code"], "target_changed_during_probe")
        self.assertTrue(snapshot.evidence["execution_submitted"])
        self.assertNotEqual(
            snapshot.evidence["probed_target_sha256"],
            snapshot.evidence["current_target_sha256"],
        )
