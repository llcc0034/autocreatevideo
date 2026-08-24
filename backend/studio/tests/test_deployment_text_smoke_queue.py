from __future__ import annotations

from datetime import timedelta
import json
from unittest.mock import Mock, patch

from django.contrib.auth import get_user_model
from django.test import TestCase, override_settings
from django.utils import timezone
from rest_framework.test import APIClient

from studio.models import (
    ModelDeployment,
    ModelDeploymentCapabilitySnapshot,
    ModelDeploymentSmokeAttempt,
    ModelDeploymentSmokeAttemptResolution,
    ModelProfileVersion,
    ProviderProfile,
)
from studio.services.deployment_smoke_queue import (
    claim_next_deployment_smoke,
    process_deployment_smoke,
    recover_stale_deployment_smokes,
)
from studio.services.json_contracts import assign_json_contract
from studio.tests.secret_fixtures import create_test_secret, enable_test_keyring


@override_settings(AI_PROVIDER_ALLOWED_HOSTS=("approved.example",))
class DeploymentTextSmokeQueueTests(TestCase):
    def setUp(self):
        enable_test_keyring(self)
        self.admin = get_user_model().objects.create_superuser(
            username="text-smoke-queue-admin",
            password="StrongPass123!",
            email="text-smoke-queue@example.test",
        )
        credential = create_test_secret(
            key="text-smoke-queue-secret",
            actor=self.admin,
            value="test-only-secret",
        )
        provider = ProviderProfile(
            key="text-smoke-queue-provider",
            name="Text smoke queue provider",
            driver_type=ProviderProfile.DriverType.OPENAI_COMPATIBLE,
            modalities="text",
            operations="chat_completion,prompt.normalize,script.generate,script.structure",
            credential=credential,
            is_active=True,
        )
        assign_json_contract(provider, "driver_config", {
            "base_url": "https://approved.example",
            "chat_path": "/v1/chat/completions",
            "timeout_seconds": 30,
            "pricing": {"currency": "USD", "fixed_per_request": "0.01"},
        })
        provider.save()
        profile = ModelProfileVersion(
            profile_key="text-smoke-queue-model",
            version=1,
            provider=provider,
            model_identifier="text-smoke-queue-v1",
            modality=ModelProfileVersion.Modality.TEXT,
            status=ModelProfileVersion.Status.PUBLISHED,
        )
        assign_json_contract(profile, "specification", {
            "schema_version": 1,
            "capabilities": [
                "script.generate",
                "prompt.normalize",
                "script.structure",
            ],
            "series_concept": {
                "minimum_shot_duration_ms": 1000,
                "maximum_shot_duration_ms": 6000,
                "max_output_tokens": 4096,
                "timeout_seconds": 30,
            },
            "script_structure": {
                "minimum_shot_duration_ms": 1000,
                "maximum_shot_duration_ms": 6000,
                "max_input_bytes": 256000,
                "max_output_tokens": 8192,
                "timeout_seconds": 30,
            },
        })
        profile.save()
        self.deployment = ModelDeployment(
            key="text-smoke-queue-deployment",
            name="Text smoke queue deployment",
            profile_version=profile,
            status=ModelDeployment.Status.ACTIVE,
        )
        assign_json_contract(self.deployment, "runtime_config", {})
        self.deployment.save()
        self.client = APIClient()
        self.client.force_authenticate(self.admin)

    def _start(self, operation: str):
        return self.client.post(
            f"/api/v1/model-deployments/{self.deployment.id}/smoke-test/",
            {"operation": operation},
            format="json",
        )

    @staticmethod
    def _script_candidate() -> str:
        return json.dumps({
            "schema_version": 2,
            "title": "灯塔来信",
            "logline": "旅人在灯塔中发现一封改变航程的信。",
            "bible": {"characters": [], "scenes": [], "props": []},
            "episodes": [{
                "title": "来信",
                "shots": [{
                    "setting": "灯塔",
                    "action": "旅人拆开旧信。",
                    "duration_ms": 1000,
                    "dialogue": {"speaker": "", "delivery": "", "text": ""},
                }],
            }],
        }, ensure_ascii=False)

    def test_api_returns_202_for_every_text_operation_without_invoking_provider(self):
        with patch("studio.services.provider_registry.OpenAICompatibleDriver.invoke") as invoke:
            responses = [self._start(operation) for operation in (
                "script.generate", "prompt.normalize", "script.structure",
            )]

        self.assertTrue(all(response.status_code == 202 for response in responses), responses)
        self.assertEqual(ModelDeploymentSmokeAttempt.objects.count(), 3)
        self.assertEqual(ModelDeploymentCapabilitySnapshot.objects.count(), 0)
        invoke.assert_not_called()
        for response in responses:
            self.assertEqual(response.data["status"], ModelDeploymentSmokeAttempt.Status.QUEUED)
            self.assertNotIn("request_snapshot", response.data)
            self.assertNotIn("provider_request_id", response.data)
            self.assertTrue(response.headers["Location"].endswith(f"/{response.data['id']}/"))

    def test_repeated_start_reuses_the_single_active_attempt(self):
        first = self._start("script.generate")
        second = self._start("script.generate")

        self.assertEqual(first.status_code, 202, first.data)
        self.assertEqual(second.status_code, 202, second.data)
        self.assertEqual(first.data["id"], second.data["id"])
        self.assertEqual(ModelDeploymentSmokeAttempt.objects.count(), 1)

    def test_worker_executes_script_generate_once_and_persists_hash_only_evidence(self):
        started = self._start("script.generate")
        self.assertEqual(started.status_code, 202, started.data)
        driver = Mock()
        driver.invoke.return_value = {
            "schema_version": 1,
            "request_id": "secret-provider-request-id",
            "text": self._script_candidate(),
        }
        attempt = claim_next_deployment_smoke("text-smoke-worker")

        with patch("studio.services.deployment_smoke.driver_for", return_value=driver):
            finished = process_deployment_smoke(attempt)

        self.assertEqual(finished.status, ModelDeploymentSmokeAttempt.Status.SUCCEEDED)
        driver.invoke.assert_called_once()
        snapshot = finished.final_snapshot
        self.assertEqual(snapshot.status, ModelDeploymentCapabilitySnapshot.Status.PASSED)
        self.assertEqual(snapshot.operations, ["script.generate"])
        persisted = json.dumps(snapshot.evidence, ensure_ascii=False)
        self.assertNotIn("灯塔来信", persisted)
        self.assertNotIn("secret-provider-request-id", persisted)
        self.assertEqual(len(snapshot.evidence["result_sha256"]), 64)

    def test_worker_executes_prompt_normalize_and_script_structure_contracts(self):
        normalize = self._start("prompt.normalize")
        structure = self._start("script.structure")
        self.assertEqual((normalize.status_code, structure.status_code), (202, 202))

        normalize_attempt = claim_next_deployment_smoke("normalize-smoke-worker")
        normalize_driver = Mock()
        normalize_driver.capabilities.return_value = {
            "schema_version": 1,
            "operations": ["prompt.normalize", "chat_completion"],
        }
        normalize_driver.estimate.return_value = {
            "schema_version": 1,
            "amount": "0.01",
            "currency": "USD",
            "basis": "fixed_per_request",
        }
        normalize_driver.invoke.return_value = {
            "schema_version": 1,
            "request_id": "normalize-request-secret",
            "items": [{
                "request_id": "prompt-normalize-smoke",
                "ok": True,
                "translation": "Slow push-in __AUTOPROMPT_LITERAL_SMOKE_0001__",
            }],
            "metadata": {},
        }
        with patch("studio.services.prompt_normalization.driver_for", return_value=normalize_driver):
            normalized = process_deployment_smoke(normalize_attempt)
        self.assertEqual(
            normalized.status,
            ModelDeploymentSmokeAttempt.Status.SUCCEEDED,
            (normalized.error_code, normalized.error_message, normalized.final_snapshot.evidence),
        )

        structure_attempt = claim_next_deployment_smoke("structure-smoke-worker")
        plan = structure_attempt.request_snapshot["plan"]
        source = plan["source_revision"]
        request_body = plan["request"]
        structure_driver = Mock()
        structure_driver.invoke.return_value = {
            "schema_version": 1,
            "request_id": "structure-request-secret",
            "text": json.dumps({
                "schema_version": 1,
                "source_revision_id": source["id"],
                "source_sha256": source["content_sha256"],
                "source_size_bytes": source["content_size_bytes"],
                "title": "灯塔来信",
                "logline": "旅人在灯塔中发现旧信。",
                "bible": {"characters": [], "scenes": [], "props": []},
                "episodes": [{
                    "title": "来信",
                    "shots": [{
                        "source_lines": request_body["non_empty_source_line_numbers"],
                        "setting": "风雨灯塔",
                        "action": "旅人进入灯塔并发现旧信。",
                        "duration_ms": 1000,
                        "dialogue": {"speaker": "", "delivery": "", "text": ""},
                    }],
                }],
            }, ensure_ascii=False),
        }
        with patch("studio.services.deployment_smoke.driver_for", return_value=structure_driver):
            structured = process_deployment_smoke(structure_attempt)
        self.assertEqual(structured.status, ModelDeploymentSmokeAttempt.Status.SUCCEEDED)
        self.assertEqual(structured.final_snapshot.operations, ["script.structure"])

    def test_stale_submitting_recovery_never_replays_the_provider_request(self):
        started = self._start("script.generate")
        attempt = claim_next_deployment_smoke("crashed-text-smoke-worker")
        attempt.status = ModelDeploymentSmokeAttempt.Status.SUBMITTING
        attempt.heartbeat_at = timezone.now() - timedelta(minutes=2)
        attempt.save(update_fields=("status", "heartbeat_at", "updated_at"))

        self.assertEqual(recover_stale_deployment_smokes(minutes=1), 1)
        recovered = claim_next_deployment_smoke("replacement-text-smoke-worker")
        driver = Mock()
        with patch("studio.services.deployment_smoke.driver_for", return_value=driver):
            finished = process_deployment_smoke(recovered)

        self.assertEqual(str(finished.id), started.data["id"])
        self.assertEqual(finished.status, ModelDeploymentSmokeAttempt.Status.UNCERTAIN)
        driver.invoke.assert_not_called()
        self.assertEqual(finished.error_code, "submission_uncertain")

    def test_tampered_text_plan_fails_before_provider_invocation(self):
        started = self._start("script.generate")
        attempt = ModelDeploymentSmokeAttempt.objects.get(pk=started.data["id"])
        attempt.request_snapshot["plan"]["tampered"] = True
        attempt.save(update_fields=("request_snapshot", "updated_at"))
        claimed = claim_next_deployment_smoke("tamper-check-worker")
        driver = Mock()

        with patch("studio.services.deployment_smoke.driver_for", return_value=driver):
            finished = process_deployment_smoke(claimed)

        self.assertEqual(finished.status, ModelDeploymentSmokeAttempt.Status.FAILED)
        self.assertEqual(finished.error_code, "text_smoke_pre_submit_invalid")
        self.assertFalse(finished.final_snapshot.evidence["execution_submitted"])
        driver.invoke.assert_not_called()

    def test_recovered_worker_with_lost_claim_cannot_append_orphan_snapshot(self):
        self._start("script.generate")
        stale = claim_next_deployment_smoke("stale-owner")
        stale.heartbeat_at = timezone.now() - timedelta(minutes=2)
        stale.save(update_fields=("heartbeat_at", "updated_at"))
        self.assertEqual(recover_stale_deployment_smokes(minutes=1), 1)
        replacement = claim_next_deployment_smoke("replacement-owner")
        self.assertNotEqual(stale.claim_token, replacement.claim_token)
        driver = Mock()

        with patch("studio.services.deployment_smoke.driver_for", return_value=driver):
            observed = process_deployment_smoke(stale)

        self.assertEqual(observed.claim_token, replacement.claim_token)
        self.assertEqual(observed.status, ModelDeploymentSmokeAttempt.Status.QUEUED)
        self.assertEqual(ModelDeploymentCapabilitySnapshot.objects.count(), 0)
        driver.invoke.assert_not_called()

    def test_queued_text_smoke_can_use_shared_cancel_endpoint(self):
        started = self._start("script.structure")

        cancelled = self.client.post(
            f"/api/v1/model-deployment-smoke-attempts/{started.data['id']}/cancel/",
            {},
            format="json",
        )

        self.assertEqual(cancelled.status_code, 202, cancelled.data)
        self.assertEqual(cancelled.data["status"], ModelDeploymentSmokeAttempt.Status.CANCELLED)
        self.assertEqual(cancelled.data["error_code"], "cancelled_before_submission")

    def test_uncertain_text_smoke_blocks_replay_until_append_only_resolution(self):
        started = self._start("script.generate")
        attempt = ModelDeploymentSmokeAttempt.objects.get(pk=started.data["id"])
        attempt.status = ModelDeploymentSmokeAttempt.Status.UNCERTAIN
        attempt.claim_token = None
        attempt.worker_id = ""
        attempt.claimed_at = None
        attempt.error_code = "provider_request_failed"
        attempt.save(update_fields=(
            "status", "claim_token", "worker_id", "claimed_at", "error_code", "updated_at",
        ))

        blocked = self._start("script.generate")
        self.assertEqual(blocked.status_code, 409, blocked.data)
        self.assertEqual(blocked.data["error"]["code"], "unresolved_smoke_submission")

        resolved = self.client.post(
            f"/api/v1/model-deployments/{self.deployment.id}/resolve-smoke-uncertain/",
            {
                "smoke_attempt_id": str(attempt.id),
                "outcome": ModelDeploymentSmokeAttemptResolution.Outcome.CONFIRMED_NOT_SUBMITTED,
                "note": "Provider audit confirms no request reached the service.",
            },
            format="json",
        )
        self.assertEqual(resolved.status_code, 201, resolved.data)
        self.assertEqual(attempt.resolutions.count(), 1)

        restarted = self._start("script.generate")
        self.assertEqual(restarted.status_code, 202, restarted.data)
        self.assertNotEqual(restarted.data["id"], str(attempt.id))
