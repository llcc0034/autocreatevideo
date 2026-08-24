from __future__ import annotations

import json
from datetime import timedelta
from unittest.mock import patch

from django.contrib.auth import get_user_model
from django.core.exceptions import ValidationError
from django.test import SimpleTestCase, TestCase, override_settings
from django.utils import timezone
from rest_framework.test import APIClient

from studio.models import (
    ConfigDefinition,
    ConfigRevision,
    ModelDeployment,
    ModelDeploymentCapabilitySnapshot,
    ModelDeploymentSmokeAttempt,
    ModelDeploymentSmokeAttemptResolution,
    ModelProfileVersion,
    PromptFieldTranslation,
    ProviderProfile,
    RuntimeHost,
    SeriesProject,
)
from studio.services.configuration import (
    activate_config_revision,
    create_config_revision,
    publish_config_revision,
    resolve_configuration,
)
from studio.services.async_media_contract import fresh_smoke_snapshot
from studio.services.json_contracts import assign_json_contract
from studio.services.prompt_normalization import (
    PROMPT_NORMALIZE_OPERATION,
    ROUTE_CONTEXT_DURABLE_ATTEMPT,
    invoke_prompt_normalization,
    prompt_normalization_capability,
    route_state,
    smoke_prompt_normalization_deployment,
    validate_prompt_normalization_model_profile,
)
from studio.services.prompt_normalization_contract import (
    PromptNormalizationContractError,
    parse_prompt_normalization_chat_response,
    prompt_normalization_chat_payload,
    validate_prompt_normalization_items,
)
from studio.services.provider_registry import (
    OpenAICompatibleDriver,
    ProviderDriverError,
    driver_for,
)
from studio.services.secret_store import rotate_secret_binding, set_secret_binding_active
from studio.tests.secret_fixtures import create_test_secret, enable_test_keyring


def _request_payload() -> dict:
    return {
        "schema_version": 1,
        "items": [{
            "request_id": "row-1",
            "text": "缓慢推近 __AUTOPROMPT_LITERAL_TEST_0001__",
            "source_lang": "Chinese",
            "target_lang": "English",
            "field_key": "shot.camera",
            "label": "Camera",
            "context": "{}",
            "protected_tokens": ["__AUTOPROMPT_LITERAL_TEST_0001__"],
        }],
    }


class PromptNormalizationContractTests(SimpleTestCase):
    def test_chat_payload_freezes_actual_request_ids_in_response_skeleton(self):
        payload = prompt_normalization_chat_payload(_request_payload())
        encoded = json.loads(payload["messages"][1]["content"])

        self.assertNotIn("output_contract", encoded)
        self.assertEqual(encoded["response_skeleton"], {
            "schema_version": 1,
            "items": [{"request_id": "row-1", "translation": ""}],
        })
        self.assertIn("every request_id unchanged", payload["messages"][0]["content"])
        self.assertGreaterEqual(payload["max_tokens"], 2048)

    def test_chat_parser_exposes_stable_non_sensitive_failure_code(self):
        with self.assertRaises(PromptNormalizationContractError) as captured:
            parse_prompt_normalization_chat_response(
                _request_payload(),
                {"text": "not-json"},
            )

        self.assertEqual(captured.exception.code, "response_not_strict_json")

    def test_chat_parser_accepts_one_complete_json_fence_but_rejects_prose(self):
        document = json.dumps({
            "schema_version": 1,
            "items": [{
                "request_id": "row-1",
                "translation": "Slow push-in __AUTOPROMPT_LITERAL_TEST_0001__",
            }],
        })

        parsed = parse_prompt_normalization_chat_response(
            _request_payload(),
            {"text": f"```json\n{document}\n```"},
        )

        self.assertEqual(parsed["metadata"]["response_envelope"], "json_code_fence")
        for invalid in (
            f"Here is the result:\n```json\n{document}\n```",
            f"```json\n{document}\n```\nAdditional text",
        ):
            with self.subTest(invalid=invalid), self.assertRaises(
                PromptNormalizationContractError,
            ):
                parse_prompt_normalization_chat_response(
                    _request_payload(),
                    {"text": invalid},
                )

    def test_shared_validator_accepts_canonical_driver_rows(self):
        rows = validate_prompt_normalization_items(_request_payload(), [{
            "request_id": "row-1",
            "ok": True,
            "translation": "Slow push-in __AUTOPROMPT_LITERAL_TEST_0001__",
        }])

        self.assertEqual(rows[0]["ok"], True)
        self.assertEqual(rows[0]["request_id"], "row-1")

    def test_shared_validator_rejects_echoed_chinese_and_bad_status(self):
        for row in (
            {
                "request_id": "row-1",
                "ok": True,
                "translation": "缓慢推近 __AUTOPROMPT_LITERAL_TEST_0001__",
            },
            {
                "request_id": "row-1",
                "ok": False,
                "translation": "Slow push-in __AUTOPROMPT_LITERAL_TEST_0001__",
            },
            {
                "request_id": "row-1",
                "ok": True,
                "translation": "__AUTOPROMPT_LITERAL_TEST_0001__ 123 !",
            },
        ):
            with self.subTest(row=row), self.assertRaises(PromptNormalizationContractError):
                validate_prompt_normalization_items(_request_payload(), [row])

    def test_shared_validator_rejects_bidi_and_zero_width_format_controls(self):
        for translation in (
            "Slow push-in \u202e hidden __AUTOPROMPT_LITERAL_TEST_0001__",
            "Slow\u200b push-in __AUTOPROMPT_LITERAL_TEST_0001__",
            "Slow push-in \u0007 __AUTOPROMPT_LITERAL_TEST_0001__",
        ):
            with self.subTest(translation=translation), self.assertRaises(
                PromptNormalizationContractError,
            ):
                validate_prompt_normalization_items(_request_payload(), [{
                    "request_id": "row-1",
                    "ok": True,
                    "translation": translation,
                }])

    def test_shared_validator_rejects_token_drift(self):
        with self.assertRaises(PromptNormalizationContractError):
            validate_prompt_normalization_items(_request_payload(), [{
                "request_id": "row-1",
                "ok": True,
                "translation": (
                    "Slow push-in __AUTOPROMPT_LITERAL_TEST_0001__ "
                    "__AUTOPROMPT_LITERAL_UNKNOWN_0002__"
                ),
            }])

        invalid_request = _request_payload()
        invalid_request["items"][0]["protected_tokens"] = [
            "__AUTOPROMPT_LITERAL_MISSING_0002__",
        ]
        with self.assertRaises(PromptNormalizationContractError):
            validate_prompt_normalization_items(invalid_request, [])

    def test_chat_parser_and_smoke_share_request_id_token_and_language_contract(self):
        response = {
            "text": json.dumps({
                "schema_version": 1,
                "items": [{
                    "request_id": "row-1",
                    "translation": "Slow push-in __AUTOPROMPT_LITERAL_TEST_0001__",
                }],
            }),
            "request_id": "provider-request",
            "usage": {
                "prompt_tokens": 12,
                "completion_tokens": 3,
                "total_tokens": 15,
                "authorization": "Bearer provider-controlled-secret",
                "nested": {"input_tokens": 99},
                "output_tokens": -1,
            },
        }

        parsed = parse_prompt_normalization_chat_response(_request_payload(), response)

        self.assertEqual(parsed["items"][0]["ok"], True)
        self.assertEqual(parsed["request_id"], "provider-request")
        self.assertEqual(parsed["metadata"]["usage"], {
            "prompt_tokens": 12,
            "completion_tokens": 3,
            "total_tokens": 15,
        })


@override_settings(AI_PROVIDER_ALLOWED_HOSTS=("localhost", "api.example.test"))
class PromptNormalizationRoutingTests(TestCase):
    def setUp(self):
        enable_test_keyring(self)
        self.user = get_user_model().objects.create_user(
            username="normalization-owner",
            password="StrongPass123!",
        )
        self.admin = get_user_model().objects.create_superuser(
            username="normalization-admin",
            password="StrongPass123!",
            email="normalization-admin@example.test",
        )
        self.credential = create_test_secret(
            key="prompt.normalize.third-local",
            actor=self.admin,
            value="secret-v1",
        )
        self.project = SeriesProject.objects.create(
            owner=self.user,
            title="动态翻译部署测试",
        )
        self.host = RuntimeHost.objects.create(
            key="third-local-host",
            name="Third local host",
        )
        self.deployment = self._deployment(
            key="third-local-normalizer",
            base_url="http://localhost:9123",
        )

    def _deployment(
        self,
        *,
        key: str,
        base_url: str,
        status: str = ModelProfileVersion.Status.PUBLISHED,
        specification: dict | None = None,
    ) -> ModelDeployment:
        provider = ProviderProfile(
            key=f"{key}-provider",
            name=f"{key} provider",
            driver_type=ProviderProfile.DriverType.OPENAI_COMPATIBLE,
            modalities="text",
            operations=PROMPT_NORMALIZE_OPERATION,
            credential=self.credential,
            is_active=True,
        )
        assign_json_contract(provider, "driver_config", {
            "base_url": base_url,
            "chat_path": "/v1/chat/completions",
            "timeout_seconds": 10,
            "pricing": {"currency": "USD", "fixed_per_request": "0.001"},
        })
        provider.save()
        profile = ModelProfileVersion(
            profile_key=f"{key}-profile",
            version=1,
            provider=provider,
            model_identifier=f"{key}-model",
            modality=ModelProfileVersion.Modality.TEXT,
            status=status,
            published_at=timezone.now() if status == ModelProfileVersion.Status.PUBLISHED else None,
        )
        assign_json_contract(profile, "specification", specification or {
            "schema_version": 1,
            "capabilities": [PROMPT_NORMALIZE_OPERATION],
        })
        profile.save()
        deployment = ModelDeployment(
            key=key,
            name=key.replace("-", " ").title(),
            profile_version=profile,
            runtime_host=self.host if base_url.startswith("http://localhost") else None,
            status=ModelDeployment.Status.ACTIVE,
        )
        assign_json_contract(deployment, "runtime_config", {})
        deployment.save()
        return deployment

    def _publish_project_default(self, deployment: ModelDeployment) -> None:
        revision = create_config_revision(
            scope_type=ConfigRevision.ScopeType.PROJECT,
            project=self.project,
            values={
                "production.prompt_normalization_deployment_id": str(deployment.id),
            },
            created_by=self.user,
        )
        publish_config_revision(revision, published_by=self.user)

    @staticmethod
    def _driver_result(text: str) -> dict:
        return {
            "schema_version": 1,
            "items": [{
                "request_id": "prompt-normalize-smoke",
                "ok": True,
                "translation": text,
            }],
            "metadata": {},
        }

    def _pass_smoke(self) -> ModelDeploymentCapabilitySnapshot:
        result = self._driver_result(
            "Slow push-in __AUTOPROMPT_LITERAL_SMOKE_0001__",
        )
        with patch.object(
            OpenAICompatibleDriver,
            "invoke",
            return_value=result,
        ):
            return smoke_prompt_normalization_deployment(self.deployment)

    def test_third_local_openai_compatible_deployment_is_dynamic_and_default_is_explicit(self):
        snapshot = self._pass_smoke()
        self.assertEqual(snapshot.status, ModelDeploymentCapabilitySnapshot.Status.PASSED)
        self._publish_project_default(self.deployment)
        self.client = APIClient()
        self.client.force_authenticate(self.user)

        response = self.client.get(
            f"/api/v1/projects/{self.project.id}/capabilities/prompt-normalization/",
        )

        self.assertEqual(response.status_code, 200, response.data)
        self.assertEqual(
            response.data["default_model_deployment_id"],
            str(self.deployment.id),
        )
        row = next(
            item for item in response.data["deployments"]
            if item["model_deployment_id"] == str(self.deployment.id)
        )
        self.assertEqual(row["provider_driver"], ProviderProfile.DriverType.OPENAI_COMPATIBLE)
        self.assertEqual(row["state"], "ready")
        self.assertTrue(row["ready"])

    def test_config_resource_reference_rejects_missing_or_unready_deployment(self):
        for value, expected in (
            ("00000000-0000-0000-0000-000000000001", "不存在"),
            (str(self.deployment.id), "prompt_normalize_smoke_required"),
        ):
            with self.subTest(value=value), self.assertRaises(ValidationError) as captured:
                create_config_revision(
                    scope_type=ConfigRevision.ScopeType.PROJECT,
                    project=self.project,
                    values={
                        "production.prompt_normalization_deployment_id": value,
                    },
                    created_by=self.user,
                )
            self.assertIn(expected, str(captured.exception))

    def test_config_resource_reference_accepts_exact_ready_deployment(self):
        self._pass_smoke()

        revision = create_config_revision(
            scope_type=ConfigRevision.ScopeType.PROJECT,
            project=self.project,
            values={
                "production.prompt_normalization_deployment_id": str(self.deployment.id),
            },
            created_by=self.user,
        )

        self.assertEqual(
            revision.values.get(
                definition__key="production.prompt_normalization_deployment_id",
            ).value,
            str(self.deployment.id),
        )

        self.client = APIClient()
        self.client.force_authenticate(self.admin)
        definition = ConfigDefinition.objects.get(
            key="production.prompt_normalization_deployment_id",
        )
        response = self.client.get(
            f"/api/v1/config-definitions/{definition.id}/resource-options/",
        )
        self.assertEqual(response.status_code, 200, response.data)
        selected = next(
            row for row in response.data if row["value"] == str(self.deployment.id)
        )
        self.assertTrue(selected["is_selectable"])
        self.assertEqual(selected["capability"], PROMPT_NORMALIZE_OPERATION)
        self.assertNotIn("base_url", response.content.decode("utf-8"))

    def test_config_resource_reference_is_revalidated_at_publish(self):
        self._pass_smoke()
        revision = create_config_revision(
            scope_type=ConfigRevision.ScopeType.PROJECT,
            project=self.project,
            values={
                "production.prompt_normalization_deployment_id": str(self.deployment.id),
            },
            created_by=self.user,
        )
        self.deployment.status = ModelDeployment.Status.DISABLED
        self.deployment.save(update_fields=("status", "updated_at"))

        with self.assertRaises(ValidationError) as captured:
            publish_config_revision(revision, published_by=self.user)

        self.assertIn("deployment_inactive", str(captured.exception))
        revision.refresh_from_db()
        self.assertEqual(revision.status, ConfigRevision.Status.DRAFT)
        self.assertFalse(ConfigRevision.objects.filter(
            scope_type=ConfigRevision.ScopeType.PROJECT,
            project=self.project,
            activation_state=ConfigRevision.ActivationState.ACTIVE,
        ).exists())

    def test_config_resource_reference_is_revalidated_at_activation(self):
        self._pass_smoke()
        ConfigDefinition.objects.filter(
            key="production.prompt_normalization_deployment_id",
        ).update(restart_required=True)
        revision = create_config_revision(
            scope_type=ConfigRevision.ScopeType.SYSTEM,
            values={
                "production.prompt_normalization_deployment_id": str(self.deployment.id),
            },
            created_by=self.admin,
        )
        publish_config_revision(revision, published_by=self.admin)
        revision.refresh_from_db()
        self.assertEqual(
            revision.activation_state,
            ConfigRevision.ActivationState.PENDING,
        )
        self.deployment.status = ModelDeployment.Status.DISABLED
        self.deployment.save(update_fields=("status", "updated_at"))

        with self.assertRaises(ValidationError) as captured:
            activate_config_revision(revision, activated_by=self.admin)

        self.assertIn("deployment_inactive", str(captured.exception))
        revision.refresh_from_db()
        self.assertEqual(
            revision.activation_state,
            ConfigRevision.ActivationState.FAILED,
        )
        self.assertIn("deployment_inactive", revision.activation_error)

    def test_active_reference_stays_readable_when_live_route_becomes_unready(self):
        smoke = self._pass_smoke()
        revision = create_config_revision(
            scope_type=ConfigRevision.ScopeType.SYSTEM,
            values={
                "production.prompt_normalization_deployment_id": str(self.deployment.id),
            },
            created_by=self.admin,
        )
        publish_config_revision(revision, published_by=self.admin)
        smoke.expires_at = timezone.now() - timedelta(seconds=1)
        smoke.save(update_fields=("expires_at", "updated_at"))

        resolved = resolve_configuration(required_keys=(
            "production.prompt_normalization_deployment_id",
            "runtime.task_worker_poll_seconds",
            "runtime.task_stale_recovery_interval_seconds",
        ))
        self.assertEqual(
            resolved.values["production.prompt_normalization_deployment_id"],
            str(self.deployment.id),
        )
        from studio.management.commands.run_generation_worker import Command
        parser = Command().create_parser("manage.py", "run_generation_worker")
        self.assertIsNotNone(parser)

        state, reason, _route = route_state(
            self.deployment,
            execution_context=ROUTE_CONTEXT_DURABLE_ATTEMPT,
        )
        self.assertEqual((state, reason), (
            "smoke_test_required", "prompt_normalize_smoke_required",
        ))
        capability = prompt_normalization_capability(project=self.project)
        row = next(
            item for item in capability["deployments"]
            if item["model_deployment_id"] == str(self.deployment.id)
        )
        self.assertFalse(row["ready"])
        self.assertEqual(row["reason_code"], "prompt_normalize_smoke_required")

        self.deployment.status = ModelDeployment.Status.DISABLED
        self.deployment.save(update_fields=("status", "updated_at"))
        disabled = resolve_configuration(required_keys=(
            "production.prompt_normalization_deployment_id",
            "runtime.task_worker_poll_seconds",
        ))
        self.assertEqual(
            disabled.values["production.prompt_normalization_deployment_id"],
            str(self.deployment.id),
        )
        state, reason, route = route_state(
            self.deployment,
            execution_context=ROUTE_CONTEXT_DURABLE_ATTEMPT,
        )
        self.assertEqual((state, reason, route), (
            "unavailable", "deployment_inactive", None,
        ))

    def test_no_default_is_returned_as_empty_selection(self):
        capability = prompt_normalization_capability(project=self.project)

        self.assertIsNone(capability["default_model_deployment_id"])

    def test_echoed_chinese_smoke_fails_and_never_becomes_ready(self):
        result = self._driver_result(
            "缓慢推近 __AUTOPROMPT_LITERAL_SMOKE_0001__",
        )
        with patch.object(
            OpenAICompatibleDriver,
            "invoke",
            return_value=result,
        ):
            snapshot = smoke_prompt_normalization_deployment(self.deployment)
            capability = prompt_normalization_capability(project=self.project)

        self.assertEqual(snapshot.status, ModelDeploymentCapabilitySnapshot.Status.FAILED)
        self.assertTrue(snapshot.evidence["execution_submitted"])
        row = next(
            item for item in capability["deployments"]
            if item["model_deployment_id"] == str(self.deployment.id)
        )
        self.assertEqual(row["state"], "smoke_test_required")
        self.assertFalse(row["ready"])

    def test_failed_smoke_records_safe_contract_reason_without_response_text(self):
        failure = ProviderDriverError(
            "strict response rejected",
            code="provider_response_invalid",
            submission_uncertain=False,
        )
        failure.metadata = {"contract_error_code": "response_root_invalid"}
        with patch.object(OpenAICompatibleDriver, "invoke", side_effect=failure):
            snapshot = smoke_prompt_normalization_deployment(self.deployment)

        self.assertEqual(snapshot.status, ModelDeploymentCapabilitySnapshot.Status.FAILED)
        self.assertEqual(snapshot.evidence["contract_error_code"], "response_root_invalid")
        self.assertNotIn("strict response rejected", json.dumps(snapshot.evidence))

    def test_formal_invocation_reuses_shared_language_validator(self):
        self._pass_smoke()
        result = {
            "schema_version": 1,
            "items": [{
                "request_id": "row-1",
                "ok": True,
                "translation": "缓慢推近 __AUTOPROMPT_LITERAL_TEST_0001__",
            }],
            "metadata": {},
        }
        with patch.object(
            OpenAICompatibleDriver,
            "invoke",
            return_value=result,
        ), self.assertRaises(ProviderDriverError) as context:
            invoke_prompt_normalization(
                deployment_id=self.deployment.id,
                payload=_request_payload(),
            )

        self.assertEqual(context.exception.code, "prompt_normalize_contract_failed")

    def test_external_standalone_is_blocked_but_durable_smoke_submits_once(self):
        external = self._deployment(
            key="external-normalizer",
            base_url="https://api.example.test",
        )
        result = self._driver_result(
            "Slow push-in __AUTOPROMPT_LITERAL_SMOKE_0001__",
        )
        result["request_id"] = "provider-smoke-secret-id"
        with patch.object(OpenAICompatibleDriver, "invoke", return_value=result) as invoke:
            snapshot = smoke_prompt_normalization_deployment(external)
            capability = prompt_normalization_capability(project=self.project)
            with self.assertRaises(ProviderDriverError) as context:
                invoke_prompt_normalization(
                    deployment_id=external.id,
                    payload=_request_payload(),
                )
            with override_settings(AI_PROVIDER_ALLOWED_HOSTS=("localhost",)):
                no_longer_allowlisted = prompt_normalization_capability(
                    project=self.project,
                )

        invoke.assert_called_once()
        self.assertEqual(snapshot.status, ModelDeploymentCapabilitySnapshot.Status.PASSED)
        self.assertTrue(snapshot.evidence["execution_submitted"])
        self.assertEqual(context.exception.code, "external_durable_attempt_required")
        attempt = ModelDeploymentSmokeAttempt.objects.get(deployment=external)
        self.assertEqual(attempt.status, ModelDeploymentSmokeAttempt.Status.SUCCEEDED)
        self.assertEqual(attempt.final_snapshot, snapshot)
        serialized_evidence = json.dumps(snapshot.evidence, ensure_ascii=False)
        self.assertNotIn("provider-smoke-secret-id", serialized_evidence)
        self.assertNotIn("Slow push-in", serialized_evidence)
        row = next(
            item for item in capability["deployments"]
            if item["model_deployment_id"] == str(external.id)
        )
        self.assertEqual(row["state"], "unavailable")
        self.assertEqual(row["reason_code"], "external_durable_attempt_required")
        no_longer_allowlisted_row = next(
            item for item in no_longer_allowlisted["deployments"]
            if item["model_deployment_id"] == str(external.id)
        )
        self.assertEqual(
            no_longer_allowlisted_row["reason_code"],
            "external_durable_attempt_required",
        )

    def test_external_uncertain_smoke_blocks_replay_until_append_only_resolution(self):
        external = self._deployment(
            key="external-uncertain-normalizer",
            base_url="https://api.example.test",
        )
        failure = ProviderDriverError(
            "connection lost after post",
            code="provider_request_failed",
            submission_uncertain=True,
        )
        with patch.object(OpenAICompatibleDriver, "invoke", side_effect=failure) as invoke:
            snapshot = smoke_prompt_normalization_deployment(external)
            blocked = self.client.post(
                f"/api/v1/model-deployments/{external.id}/smoke-test/",
                {"operation": PROMPT_NORMALIZE_OPERATION},
                content_type="application/json",
            )

        self.assertEqual(snapshot.status, ModelDeploymentCapabilitySnapshot.Status.FAILED)
        attempt = ModelDeploymentSmokeAttempt.objects.get(deployment=external)
        self.assertEqual(attempt.status, ModelDeploymentSmokeAttempt.Status.UNCERTAIN)
        self.assertEqual(invoke.call_count, 1)
        # Anonymous access is rejected before the state contract is disclosed.
        self.assertIn(blocked.status_code, {401, 403})

        client = APIClient()
        client.force_authenticate(self.admin)
        replay = client.post(
            f"/api/v1/model-deployments/{external.id}/smoke-test/",
            {"operation": PROMPT_NORMALIZE_OPERATION},
            format="json",
        )
        self.assertEqual(replay.status_code, 409, replay.data)
        self.assertEqual(replay.data["error"]["code"], "unresolved_smoke_submission")
        resolved = client.post(
            f"/api/v1/model-deployments/{external.id}/resolve-smoke-uncertain/",
            {
                "smoke_attempt_id": str(attempt.id),
                "outcome": ModelDeploymentSmokeAttemptResolution.Outcome.CONFIRMED_NOT_SUBMITTED,
                "note": "Provider audit proves that no request reached the service.",
            },
            format="json",
        )
        self.assertEqual(resolved.status_code, 201, resolved.data)
        self.assertNotIn("provider_request_id", resolved.data)
        resolution = ModelDeploymentSmokeAttemptResolution.objects.get(attempt=attempt)
        with self.assertRaises(ValidationError):
            resolution.save()
        with self.assertRaises(ValidationError):
            resolution.delete()

    def test_chat_path_cannot_override_the_local_provider_origin(self):
        config = dict(self.deployment.provider.driver_config)
        config["chat_path"] = "/https://api.example.test/v1/chat/completions"
        assign_json_contract(self.deployment.provider, "driver_config", config)
        self.deployment.provider.save()

        with patch("studio.services.provider_registry._request_json") as request:
            snapshot = smoke_prompt_normalization_deployment(self.deployment)

        request.assert_not_called()
        self.assertEqual(snapshot.status, ModelDeploymentCapabilitySnapshot.Status.FAILED)
        self.assertFalse(snapshot.evidence["execution_submitted"])
        self.assertEqual(
            snapshot.evidence["error_code"],
            "deployment_configuration_invalid",
        )

    def test_smoke_cannot_issue_passed_evidence_for_a_changed_target(self):
        result = self._driver_result(
            "Slow push-in __AUTOPROMPT_LITERAL_SMOKE_0001__",
        )

        def mutate_target(*_args, **_kwargs):
            provider = ProviderProfile.objects.get(
                pk=self.deployment.profile_version.provider_id,
            )
            config = dict(provider.driver_config)
            config["timeout_seconds"] = 11
            assign_json_contract(provider, "driver_config", config)
            provider.save()
            return result

        with patch.object(
            OpenAICompatibleDriver,
            "invoke",
            side_effect=mutate_target,
        ):
            snapshot = smoke_prompt_normalization_deployment(self.deployment)

        self.assertEqual(snapshot.status, ModelDeploymentCapabilitySnapshot.Status.FAILED)
        self.assertEqual(
            snapshot.evidence["error_code"],
            "target_changed_during_probe",
        )
        self.assertTrue(snapshot.evidence["execution_submitted"])
        capability = prompt_normalization_capability(project=self.project)
        row = next(
            item for item in capability["deployments"]
            if item["model_deployment_id"] == str(self.deployment.id)
        )
        self.assertEqual(row["state"], "smoke_test_required")

    def test_translation_detail_redacts_runtime_and_revision_metadata(self):
        source = "缓慢推近。"
        self.project.continuity_summary = source
        self.project.save(update_fields=("continuity_summary", "updated_at"))
        entry = PromptFieldTranslation.objects.create(
            project=self.project,
            scope_type=PromptFieldTranslation.ScopeType.PROJECT,
            scope_id=self.project.id,
            field_key="project.continuity_summary",
            source_text=source,
            source_hash="a" * 64,
            translated_text="Slow push-in.",
            runtime_metadata={
                "usage": {"prompt_tokens": 12},
                "authorization": "Bearer runtime-secret",
                "credential_ref": "runtime-secret-ref",
            },
            revision_history=[{
                "event": "translation_generated",
                "runtime_metadata": {
                    "api_token": "revision-secret",
                    "usage": {"total_tokens": 15},
                },
            }],
        )
        client = APIClient()
        client.force_authenticate(self.user)

        response = client.get(f"/api/v1/translations/{entry.id}/")

        self.assertEqual(response.status_code, 200, response.data)
        serialized = json.dumps(response.data, ensure_ascii=False, default=str)
        self.assertNotIn("runtime-secret", serialized)
        self.assertNotIn("runtime-secret-ref", serialized)
        self.assertNotIn("revision-secret", serialized)
        self.assertNotIn("credential_ref", serialized)
        self.assertEqual(response.data["runtime_metadata"]["usage"]["prompt_tokens"], 12)

    def test_credential_missing_rotation_and_expiry_invalidate_readiness(self):
        set_secret_binding_active(
            self.credential, is_active=False, actor=self.admin, reason="test_inactive",
        )
        missing = prompt_normalization_capability(project=self.project)
        missing_row = next(
            item for item in missing["deployments"]
            if item["model_deployment_id"] == str(self.deployment.id)
        )
        self.assertEqual(missing_row["reason_code"], "credential_unavailable")

        set_secret_binding_active(
            self.credential, is_active=True, actor=self.admin, reason="test_reactivate",
        )
        snapshot = self._pass_smoke()
        self.credential = rotate_secret_binding(
            self.credential,
            secret_value="secret-v2",
            actor=self.admin,
            reason="test_rotation",
        )
        drifted = prompt_normalization_capability(project=self.project)
        drifted_row = next(
            item for item in drifted["deployments"]
            if item["model_deployment_id"] == str(self.deployment.id)
        )
        self.assertEqual(drifted_row["state"], "smoke_test_required")

        snapshot.expires_at = timezone.now() - timedelta(seconds=1)
        snapshot.save(update_fields=("expires_at", "updated_at"))
        expired = prompt_normalization_capability(project=self.project)
        expired_row = next(
            item for item in expired["deployments"]
            if item["model_deployment_id"] == str(self.deployment.id)
        )
        self.assertEqual(expired_row["state"], "smoke_test_required")

    def test_driver_type_change_invalidates_existing_smoke_target(self):
        snapshot = self._pass_smoke()
        self.assertEqual(snapshot.status, ModelDeploymentCapabilitySnapshot.Status.PASSED)
        self.assertIsNotNone(
            fresh_smoke_snapshot(self.deployment, PROMPT_NORMALIZE_OPERATION),
        )

        ProviderProfile.objects.filter(
            pk=self.deployment.profile_version.provider_id,
        ).update(driver_type=ProviderProfile.DriverType.DEEPSEEK_API)
        self.deployment.refresh_from_db()
        self.deployment.profile_version.provider.refresh_from_db()

        self.assertIsNone(
            fresh_smoke_snapshot(self.deployment, PROMPT_NORMALIZE_OPERATION),
        )

        client = APIClient()
        client.force_authenticate(self.admin)
        response = client.get(f"/api/v1/model-deployments/{self.deployment.id}/")
        self.assertEqual(response.status_code, 200, response.data)
        self.assertFalse(response.data["latest_smoke"]["fresh"])

    def test_capability_redacts_credentials_and_invalid_signed_config(self):
        secret_marker = "must-not-leak"
        ProviderProfile.objects.filter(pk=self.deployment.provider.id).update(
            driver_config={
                "base_url": f"https://api.example.test/{secret_marker}",
                "credential": secret_marker,
            },
        )

        capability = prompt_normalization_capability(project=self.project)
        serialized = json.dumps(capability, ensure_ascii=False, default=str)
        row = next(
            item for item in capability["deployments"]
            if item["model_deployment_id"] == str(self.deployment.id)
        )

        self.assertEqual(row["state"], "unavailable")
        self.assertEqual(row["reason_code"], "deployment_configuration_invalid")
        self.assertTrue(row["data_egress"])
        self.assertNotIn(secret_marker, serialized)
        self.assertNotIn("third-local-test", serialized)
        self.assertNotIn("credential_ref", serialized)

    def test_publish_requires_exact_capabilities_and_driver_does_not_claim_script_generate(self):
        alias = self._deployment(
            key="alias-normalizer",
            base_url="http://localhost:9124",
            status=ModelProfileVersion.Status.DRAFT,
            specification={
                "schema_version": 1,
                "capabilities": [],
                "operations": [PROMPT_NORMALIZE_OPERATION],
            },
        )
        with self.assertRaises(ValidationError):
            validate_prompt_normalization_model_profile(alias.profile_version)
        client = APIClient()
        client.force_authenticate(self.admin)
        rejected = client.post(
            f"/api/v1/model-profile-versions/{alias.profile_version_id}/publish/",
            {},
            format="json",
        )
        self.assertEqual(rejected.status_code, 400, rejected.data)

        exact = self._deployment(
            key="exact-normalizer",
            base_url="http://localhost:9125",
            status=ModelProfileVersion.Status.DRAFT,
        )
        published = client.post(
            f"/api/v1/model-profile-versions/{exact.profile_version_id}/publish/",
            {},
            format="json",
        )
        self.assertEqual(published.status_code, 200, published.data)
        self.assertEqual(published.data["status"], ModelProfileVersion.Status.PUBLISHED)

        self.assertNotIn(
            "script.generate",
            driver_for(self.deployment.provider).capabilities()["operations"],
        )
