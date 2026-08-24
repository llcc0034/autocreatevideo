from __future__ import annotations

from decimal import Decimal
from datetime import timedelta
from io import BytesIO
import hashlib
import json
from pathlib import Path
import tempfile
import uuid
from types import SimpleNamespace
from unittest.mock import patch

from django.contrib.auth import get_user_model
from django.test import SimpleTestCase, TestCase, override_settings
from django.utils import timezone
from PIL import Image
from rest_framework.test import APIClient

from studio.models import (
    AssemblySourceItem,
    Episode,
    GenerationAttempt,
    GeneratedArtifact,
    ModelDeployment,
    ModelDeploymentCapabilitySnapshot,
    ModelDeploymentSmokeAttempt,
    ModelProfileVersion,
    ProductionRecipeVersion,
    ProductionRun,
    PromptArtifact,
    ProviderProfile,
    ReleaseBundle,
    SeriesProject,
    StageRun,
    Shot,
    WorkflowTemplate,
    WorkflowTemplateVersion,
    WorkerLease,
)
from studio.services.async_media_contract import async_operation_contract, fresh_smoke_snapshot
from studio.services.deployment_smoke import probe_async_deployment
from studio.services.deployment_smoke_queue import (
    claim_next_deployment_smoke,
    enqueue_async_deployment_smoke,
    process_deployment_smoke,
)
from studio.services.json_contracts import assign_json_contract, json_contract
from studio.services.production_runs import (
    StageBlocked,
    _async_media_payload,
    _create_prompt_manifest,
    _definitive_media_failure,
    _execute_assembly,
    _execute_delivery,
    _poll_media_stage,
    _route_candidates,
    cancel_production_run,
    resolve_uncertain_attempt,
    select_run_artifact,
)
from studio.services.provider_registry import ProviderDriverError, RestrictedAsyncRestDriver
from studio.services.provider_registry import credential_binding_sha256
from studio.services.remote_artifacts import (
    _download_one, _validated_artifact_url, archive_remote_artifacts,
)
from studio.services.async_media_queue import (
    _succeed, process_async_media_attempt, recover_stale_async_media_attempts,
)
from studio.services.readiness import _media_capability_readiness
from studio.services.provenance import persist_assembly_provenance
from studio.services.release_bundles import verified_release_bundle
from studio.tests.factories import create_test_prompt_profile_binding


def _async_spec():
    def operation(kind):
        properties = {
            "prompt": {"type": "string", "minLength": 1, "maxLength": 2000},
            "model": {"type": "string", "minLength": 1, "maxLength": 255},
            "width": {"type": "integer", "minimum": 64, "maximum": 4096},
            "height": {"type": "integer", "minimum": 64, "maximum": 4096},
        }
        if kind == "video":
            properties.update({
                "duration_seconds": {"type": "number", "minimum": 0.1, "maximum": 30},
                "fps": {"type": "integer", "minimum": 1, "maximum": 120},
            })
        return {
            "output_kind": kind,
            "request_schema": {
                "type": "object",
                "additionalProperties": False,
                "required": ["prompt", "model", "width", "height"],
                "properties": properties,
            },
            "smoke_inputs": {
                "prompt": "smoke",
                "model": "cloud-v1",
                "width": 64,
                "height": 64,
                **({"duration_seconds": 0.5, "fps": 8} if kind == "video" else {}),
            },
        }
    return {
        "schema_version": 1,
        "capabilities": ["image.text_to_image", "video.text_to_video"],
        "async_media": {
            "schema_version": 1,
            "operations": {
                "image.text_to_image": operation("image"),
                "video.text_to_video": operation("video"),
            },
        },
    }


def _provider_config():
    return {
        "base_url": "https://api.example.com",
        "health_path": "/health",
        "submit_path": "/jobs",
        "poll_path": "/jobs/{request_id}",
        "cancel_path": "/jobs/{request_id}/cancel",
        "collect_path": "/jobs/{request_id}",
        "request_id_field": "id",
        "status_field": "status",
        "terminal_success": ["succeeded"],
        "terminal_failure": ["failed"],
        "terminal_cancelled": ["cancelled"],
        "artifact_allowed_hosts": ["cdn.example.com"],
        "request_field_map": {"model": "model", "prompt": "prompt", "width": "width", "height": "height"},
        "poll_interval_seconds": 1,
        "max_poll_seconds": 60,
        "capability_ttl_seconds": 3600,
        "pricing": {"currency": "USD", "fixed_per_request": "0.010000"},
    }


@override_settings(AI_PROVIDER_ALLOWED_HOSTS=("api.example.com", "cdn.example.com"))
class RestrictedAsyncRestDriverTests(SimpleTestCase):
    def test_submit_timeout_is_one_post_and_idempotency_is_header_only(self):
        config = _provider_config()
        provider = SimpleNamespace(driver_config=config, credential=None)
        deployment = SimpleNamespace(
            provider=provider,
            profile_version=SimpleNamespace(provider=provider),
            runtime_config={}, credential=None,
        )
        driver = RestrictedAsyncRestDriver()
        error = ProviderDriverError(
            "timeout", code="provider_timeout", retryable=True, submission_uncertain=True,
        )
        with patch("studio.services.provider_registry._combined_config", return_value=config), patch(
            "studio.services.provider_registry.validate_endpoint", return_value=config["base_url"],
        ), patch(
            "studio.services.provider_registry._request_json", side_effect=error,
        ) as request:
            with self.assertRaises(ProviderDriverError):
                driver.invoke(deployment, "submit", {
                    "schema_version": 1,
                    "idempotency_key": "persisted-key",
                    "model": "cloud-v1",
                    "prompt": "a lighthouse",
                    "width": 64,
                    "height": 64,
                })
        self.assertEqual(request.call_count, 1)
        args, kwargs = request.call_args
        self.assertEqual(args[0], "POST")
        self.assertEqual(kwargs["headers"]["Idempotency-Key"], "persisted-key")
        self.assertNotIn("idempotency_key", kwargs["payload"])

    def test_submit_success_without_request_id_is_uncertain(self):
        config = _provider_config()
        provider = SimpleNamespace(driver_config=config, credential=None)
        deployment = SimpleNamespace(
            provider=provider,
            profile_version=SimpleNamespace(provider=provider),
            runtime_config={},
            credential=None,
        )
        with patch(
            "studio.services.provider_registry._combined_config",
            return_value=config,
        ), patch(
            "studio.services.provider_registry.validate_endpoint",
            return_value=config["base_url"],
        ), patch(
            "studio.services.provider_registry._request_json",
            return_value={"status": "queued"},
        ):
            with self.assertRaises(ProviderDriverError) as captured:
                RestrictedAsyncRestDriver().invoke(deployment, "submit", {
                    "schema_version": 1,
                    "idempotency_key": "persisted-key",
                    "model": "cloud-v1",
                    "prompt": "a lighthouse",
                    "width": 64,
                    "height": 64,
                })
        self.assertEqual(captured.exception.code, "provider_response_invalid")
        self.assertTrue(captured.exception.submission_uncertain)


@override_settings(AI_PROVIDER_ALLOWED_HOSTS=("cdn.example.com",))
class RemoteArtifactSecurityTests(SimpleTestCase):
    def _png(self):
        output = BytesIO()
        Image.new("RGB", (2, 2), (10, 20, 30)).save(output, format="PNG")
        return output.getvalue()

    def _response(self, body: bytes, *, status=200, headers=None):
        return SimpleNamespace(
            status_code=status,
            headers=headers or {"Content-Length": str(len(body))},
            iter_content=lambda chunk_size: [body],
            close=lambda: None,
        )

    def test_private_dns_and_redirect_are_fail_closed(self):
        config = {"artifact_allowed_hosts": ["cdn.example.com"]}
        with patch("studio.services.remote_artifacts.socket.getaddrinfo", return_value=[
            (None, None, None, None, ("127.0.0.1", 443)),
        ]):
            with self.assertRaises(ProviderDriverError) as private:
                _validated_artifact_url("https://cdn.example.com/result.png", config)
        self.assertEqual(private.exception.code, "artifact_peer_forbidden")

        body = self._png()
        response = self._response(body, status=302, headers={"Location": "https://elsewhere.test/x"})
        with tempfile.TemporaryDirectory() as directory, patch(
            "studio.services.remote_artifacts.socket.getaddrinfo", return_value=[
                (None, None, None, None, ("93.184.216.34", 443)),
            ],
        ), patch("studio.services.remote_artifacts.requests.get", return_value=response), patch(
            "studio.services.remote_artifacts._peer_address", return_value="93.184.216.34",
        ):
            with self.assertRaises(ProviderDriverError) as redirect:
                _download_one(
                    url="https://cdn.example.com/result.png", provider_config=config,
                    target=Path(directory) / "result.media", expected_kind="image",
                    expected_sha256="", max_bytes=1024 * 1024,
                )
        self.assertEqual(redirect.exception.code, "artifact_redirect_forbidden")

    def test_size_mime_hash_and_valid_image(self):
        config = {"artifact_allowed_hosts": ["cdn.example.com"], "timeout_seconds": 1}
        body = self._png()
        import hashlib
        digest = hashlib.sha256(body).hexdigest()
        dns = patch("studio.services.remote_artifacts.socket.getaddrinfo", return_value=[
            (None, None, None, None, ("93.184.216.34", 443)),
        ])
        peer = patch("studio.services.remote_artifacts._peer_address", return_value="93.184.216.34")
        with tempfile.TemporaryDirectory() as directory, dns, peer:
            target = Path(directory) / "result.media"
            with patch(
                "studio.services.remote_artifacts.requests.get",
                return_value=self._response(body),
            ):
                facts = _download_one(
                    url="https://cdn.example.com/result.png", provider_config=config,
                    target=target, expected_kind="image", expected_sha256=digest,
                    max_bytes=1024 * 1024,
                )
            self.assertEqual(facts["sha256"], digest)
            with patch(
                "studio.services.remote_artifacts.requests.get",
                return_value=self._response(body, headers={"Content-Length": str(len(body))}),
            ):
                with self.assertRaises(ProviderDriverError) as too_large:
                    _download_one(
                        url="https://cdn.example.com/result.png", provider_config=config,
                        target=target, expected_kind="image", expected_sha256="", max_bytes=1,
                    )
            self.assertEqual(too_large.exception.code, "artifact_too_large")
            with patch(
                "studio.services.remote_artifacts.requests.get",
                return_value=self._response(body),
            ):
                with self.assertRaises(ProviderDriverError) as mismatch:
                    _download_one(
                        url="https://cdn.example.com/result.png", provider_config=config,
                        target=target, expected_kind="image", expected_sha256="0" * 64,
                        max_bytes=1024 * 1024,
                    )
            self.assertEqual(mismatch.exception.code, "artifact_checksum_mismatch")
            with patch(
                "studio.services.remote_artifacts.requests.get",
                return_value=self._response(b"not an image"),
            ):
                with self.assertRaises(ProviderDriverError):
                    _download_one(
                        url="https://cdn.example.com/result.png", provider_config=config,
                        target=target, expected_kind="image", expected_sha256="",
                        max_bytes=1024 * 1024,
                    )


@override_settings(AI_PROVIDER_ALLOWED_HOSTS=("api.example.com", "cdn.example.com"))
class AsyncProductionContractTests(TestCase):
    def setUp(self):
        self.user = get_user_model().objects.create_user(username="async-user", password="x")
        self.project = SeriesProject.objects.create(owner=self.user, title="Cloud media", width=768, height=1344)
        provider_contract = json_contract(_provider_config())
        self.provider = ProviderProfile.objects.create(
            key="async.test", name="Async test", driver_type=ProviderProfile.DriverType.ASYNC_REST,
            modalities="image,video", operations="image.text_to_image,video.text_to_video",
            driver_config=provider_contract.value,
            driver_config_schema_version=provider_contract.schema_version,
            driver_config_size_bytes=provider_contract.size_bytes,
            driver_config_sha256=provider_contract.sha256,
        )
        spec_contract = json_contract(_async_spec())
        self.profile = ModelProfileVersion.objects.create(
            profile_key="async.cloud", version=1, provider=self.provider,
            model_identifier="cloud-v1", modality=ModelProfileVersion.Modality.MULTIMODAL,
            specification=spec_contract.value,
            specification_schema_version=spec_contract.schema_version,
            specification_size_bytes=spec_contract.size_bytes,
            specification_sha256=spec_contract.sha256,
            status=ModelProfileVersion.Status.PUBLISHED,
        )
        create_test_prompt_profile_binding(
            actor=self.user,
            model_profile_version=self.profile,
            operation="image.generate",
        )
        create_test_prompt_profile_binding(
            actor=self.user,
            model_profile_version=self.profile,
            operation="video.generate",
            mode=Shot.H3Mode.T2VA,
        )
        runtime_contract = json_contract({})
        self.deployment = ModelDeployment.objects.create(
            key="async.cloud.primary", name="Async cloud", profile_version=self.profile,
            runtime_config=runtime_contract.value,
            runtime_config_schema_version=runtime_contract.schema_version,
            runtime_config_size_bytes=runtime_contract.size_bytes,
            runtime_config_sha256=runtime_contract.sha256,
        )

    def _run_stage(self, key):
        recipe_contract = json_contract({"schema_version": 1, "stages": []})
        recipe = ProductionRecipeVersion.objects.create(
            recipe_key=f"recipe-{key}", version=1, name=key,
            definition=recipe_contract.value,
            definition_schema_version=recipe_contract.schema_version,
            definition_size_bytes=recipe_contract.size_bytes,
            definition_sha256=recipe_contract.sha256,
        )
        empty = json_contract({"schema_version": 1})
        run = ProductionRun.objects.create(
            owner=self.user, project=self.project, recipe_version=recipe,
            idempotency_key=f"run-{key}", request_sha256="b" * 64,
            status=ProductionRun.Status.RUNNING, current_stage_key="media_generation",
            max_generation_tasks=8, generation_task_count=1, allow_external_calls=True,
            cost_currency="USD", committed_cost_total=Decimal("0.010000"),
            config_snapshot=empty.value, config_snapshot_schema_version=empty.schema_version,
            config_snapshot_size_bytes=empty.size_bytes, config_snapshot_sha256=empty.sha256,
            input_snapshot=empty.value, input_snapshot_schema_version=empty.schema_version,
            input_snapshot_size_bytes=empty.size_bytes, input_snapshot_sha256=empty.sha256,
        )
        routing = json_contract({"schema_version": 1})
        stage = StageRun.objects.create(
            production_run=run, stage_key="media_generation", ordinal=1,
            mode="existing_pipeline", status=StageRun.Status.RUNNING,
            idempotency_key=f"stage-{key}", routing=routing.value,
            routing_schema_version=routing.schema_version,
            routing_size_bytes=routing.size_bytes, routing_sha256=routing.sha256,
            routing_decision=routing.value,
            routing_decision_schema_version=routing.schema_version,
            routing_decision_size_bytes=routing.size_bytes,
            routing_decision_sha256=routing.sha256,
        )
        return run, stage

    def _add_artifact_review_gate(self, run, media_stage):
        assign_json_contract(media_stage, "config_snapshot", {"schema_version": 1})
        assign_json_contract(media_stage, "input_snapshot", {
            "schema_version": 1,
            "stage_key": "media_generation",
        })
        media_stage.save(update_fields=(
            "config_snapshot", "config_snapshot_schema_version",
            "config_snapshot_size_bytes", "config_snapshot_sha256",
            "input_snapshot", "input_snapshot_schema_version",
            "input_snapshot_size_bytes", "input_snapshot_sha256", "updated_at",
        ))
        gate = StageRun(
            production_run=run,
            stage_key="artifact_selection",
            ordinal=media_stage.ordinal + 1,
            mode="review",
            approval_required=True,
            idempotency_key=f"artifact-review-{run.id}",
        )
        for field, payload in (
            ("routing", {"schema_version": 1}),
            ("routing_decision", {"schema_version": 1}),
            ("config_snapshot", {"schema_version": 1}),
            ("input_snapshot", {
                "schema_version": 1,
                "stage_key": "artifact_selection",
            }),
        ):
            assign_json_contract(gate, field, payload)
        gate.save()
        return gate

    def test_uniform_async_route_without_shots_rejects_non_t2va_project_default(self):
        self.project.production_settings = {"default_h3_mode": Shot.H3Mode.REF2VA}
        self.project.save(update_fields=("production_settings", "updated_at"))
        Episode.objects.create(project=self.project, number=1, title="Future storyboard")
        recipe = ProductionRecipeVersion(
            recipe_key="future-async-media-route",
            version=1,
            name="Future async media route",
            status=ProductionRecipeVersion.Status.PUBLISHED,
        )
        assign_json_contract(recipe, "definition", {
            "schema_version": 1,
            "stages": [{
                "key": "media_generation",
                "mode": "existing_pipeline",
                "approval": "auto",
                "routing": {
                    "mode": "fixed",
                    "routes": [{"model_deployment_id": str(self.deployment.id)}],
                    "variant_count": 1,
                    "evaluator": None,
                },
            }],
        })
        recipe.save()
        client = APIClient()
        client.force_authenticate(self.user)

        now = timezone.now()
        smoke = SimpleNamespace(
            id=uuid.uuid4(),
            target_sha256="a" * 64,
            evidence_sha256="b" * 64,
            captured_at=now,
            expires_at=now + timedelta(hours=1),
        )
        with patch(
            "studio.services.production_runs.fresh_smoke_snapshot",
            return_value=smoke,
        ):
            response = client.post("/api/v1/production-runs/", {
                "project_id": str(self.project.id),
                "recipe_version_id": str(recipe.id),
                "idempotency_key": "future-async-ref2va-mismatch",
            }, format="json")

        self.assertEqual(response.status_code, 400, response.data)
        self.assertIn("async REST", str(response.data))
        self.assertIn("T2VA", str(response.data))
        self.assertFalse(
            ProductionRun.objects.filter(
                idempotency_key="future-async-ref2va-mismatch",
            ).exists(),
        )

    def test_async_deployment_smoke_is_persisted_and_processed_without_repeat_submit(self):
        class SmokeDriver:
            def __init__(inner):
                inner.calls = []

            def estimate(inner, deployment, operation, payload):
                return {"amount": "0.010000", "currency": "USD", "basis": "fixed"}

            def invoke(inner, deployment, operation, payload):
                inner.calls.append(operation)
                if operation == "submit":
                    return {"request_id": "remote-smoke-queue", "status": "queued"}
                if operation == "poll":
                    return {"status": "succeeded", "terminal_success": True}
                if operation == "collect":
                    return {
                        "artifacts": [{"url": "https://cdn.example.com/short-lived", "kind": "image"}],
                        "actual_cost": "0.010000", "currency": "USD",
                    }
                raise AssertionError(operation)

        driver = SmokeDriver()
        with patch("studio.services.deployment_smoke_queue.driver_for", return_value=driver), patch(
            "studio.services.deployment_smoke_queue.verify_remote_artifacts_for_smoke",
            return_value=[{
                "mime_type": "image/png", "size_bytes": 10, "sha256": "d" * 64,
                "width": 64, "height": 64,
            }],
        ):
            attempt, created = enqueue_async_deployment_smoke(
                self.deployment, operation="image.text_to_image",
            )
            self.assertTrue(created)
            claimed = claim_next_deployment_smoke("test-worker")
            self.assertEqual(claimed.id, attempt.id)
            submitted = process_deployment_smoke(claimed)
            self.assertEqual(submitted.status, ModelDeploymentSmokeAttempt.Status.SUBMITTED)
            claimed = claim_next_deployment_smoke("test-worker")
            self.assertEqual(claimed.id, attempt.id)
            finished = process_deployment_smoke(claimed)
        self.assertEqual(finished.status, ModelDeploymentSmokeAttempt.Status.SUCCEEDED)
        self.assertEqual(driver.calls.count("submit"), 1)
        self.assertEqual(driver.calls, ["submit", "poll", "collect"])

    def _route(self):
        return {
            "index": 0,
            "model_deployment_id": str(self.deployment.id),
            "model_profile_version_id": str(self.profile.id),
            "model_identifier": self.profile.model_identifier,
            "model_specification": self.profile.specification,
            "frozen_model_specification_sha256": json_contract(self.profile.specification).sha256,
            "provider_id": str(self.provider.id),
            "driver_type": ProviderProfile.DriverType.ASYNC_REST,
            "credential_binding_sha256": credential_binding_sha256(self.deployment),
            "provider_config": self.provider.driver_config,
            "provider_config_sha256": json_contract(self.provider.driver_config).sha256,
            "deployment_config": self.deployment.runtime_config,
            "deployment_config_sha256": json_contract(self.deployment.runtime_config).sha256,
        }

    def _ordered_fallback_fixture(self, key, *, error_code="provider_request_rejected"):
        run, stage = self._run_stage(key)
        run.budget_limit = Decimal("1.000000")
        run.committed_cost_total = Decimal("0")
        run.save(update_fields=("budget_limit", "committed_cost_total", "updated_at"))
        route_zero = {
            **self._route(),
            "index": 0,
            "is_external": True,
            "media_operations": ["image.text_to_image", "video.text_to_video"],
            "smoke_evidence": {"image.text_to_image": {"snapshot_id": "smoke-0"}},
        }
        route_one = {
            **self._route(),
            "index": 1,
            "is_external": True,
            "media_operations": ["image.text_to_image", "video.text_to_video"],
            "smoke_evidence": {"image.text_to_image": {"snapshot_id": "smoke-1"}},
        }
        assign_json_contract(stage, "routing", {
            "schema_version": 1,
            "mode": "ordered_fallback",
            "routes": [route_zero, route_one],
            "variant_count": 1,
            "evaluator": None,
        })
        assign_json_contract(stage, "routing_decision", {
            "schema_version": 1,
            "state": "selected",
            "selected_route_index": 0,
        })
        assign_json_contract(stage, "config_snapshot", {
            "schema_version": 1,
            "base": {"values": {
                "production.task_max_attempts": 1,
                "production.default_concurrency": 1,
                "production.task_timeout_seconds": 300,
                "runtime.task_idle_timeout_seconds": 120,
                "production.default_resolution_width": 576,
                "production.default_resolution_height": 1024,
            }},
            "routes": {},
        })
        stage.save(update_fields=(
            "routing", "routing_schema_version", "routing_size_bytes", "routing_sha256",
            "routing_decision", "routing_decision_schema_version",
            "routing_decision_size_bytes", "routing_decision_sha256",
            "config_snapshot", "config_snapshot_schema_version",
            "config_snapshot_size_bytes", "config_snapshot_sha256", "updated_at",
        ))
        prompt = PromptArtifact.objects.create(
            project=self.project,
            created_by=self.user,
            prompt_type=PromptArtifact.PromptType.IMAGE,
            prompt_text="a lighthouse at night",
            negative_prompt="",
            content_hash="f" * 64,
        )
        _create_prompt_manifest(run, stage, prompt, shot_id="")
        old_id = uuid.uuid4()
        old_request = json_contract({
            "schema_version": 1,
            "transport": "restricted_async_rest",
            "operation": "image.text_to_image",
            "output_kind": "image",
            "prompt_artifact_id": str(prompt.id),
            "route": route_zero,
            "payload": {
                "schema_version": 1,
                "idempotency_key": f"production-attempt:{old_id}",
            },
        })
        attempt = GenerationAttempt.objects.create(
            id=old_id,
            stage_run=stage,
            unit_key=f"prompt:{prompt.id}:variant:1",
            attempt_number=1,
            model_deployment=self.deployment,
            route_index=0,
            operation="image.text_to_image",
            is_external=True,
            status=GenerationAttempt.Status.FAILED,
            error_code=error_code,
            request_sha256=old_request.sha256,
            request_snapshot=old_request.value,
            request_snapshot_schema_version=old_request.schema_version,
            request_snapshot_size_bytes=old_request.size_bytes,
            request_snapshot_sha256=old_request.sha256,
        )
        return run, stage, attempt

    def test_t2i_and_t2v_build_only_declared_standard_payloads(self):
        for prompt_type, operation in (
            (PromptArtifact.PromptType.IMAGE, "image.text_to_image"),
            (PromptArtifact.PromptType.VIDEO, "video.text_to_video"),
        ):
            prompt = PromptArtifact(
                project=self.project, created_by=self.user, prompt_type=prompt_type,
                prompt_text="a lighthouse", total_duration_ms=500, content_hash="a" * 64,
            )
            payload = _async_media_payload(
                run=SimpleNamespace(), prompt=prompt, variant_number=1,
                deployment=self.deployment,
                contract=async_operation_contract(self.profile, operation),
                attempt_id="00000000-0000-0000-0000-000000000001",
                width=576,
                height=1024,
            )
            self.assertEqual(payload["model"], "cloud-v1")
            self.assertEqual(payload["prompt"], "a lighthouse")
            self.assertNotIn("reference_bindings", payload)

    def test_image_to_video_reference_route_is_explicitly_blocked(self):
        recipe_contract = json_contract({"schema_version": 1, "stages": []})
        recipe = ProductionRecipeVersion.objects.create(
            recipe_key="async-test", version=1, name="async test",
            definition=recipe_contract.value,
            definition_schema_version=recipe_contract.schema_version,
            definition_size_bytes=recipe_contract.size_bytes,
            definition_sha256=recipe_contract.sha256,
        )
        empty = json_contract({"schema_version": 1})
        run = ProductionRun.objects.create(
            owner=self.user, project=self.project, recipe_version=recipe,
            idempotency_key="async-block", request_sha256="b" * 64,
            status=ProductionRun.Status.RUNNING, max_generation_tasks=1,
            config_snapshot=empty.value, config_snapshot_schema_version=empty.schema_version,
            config_snapshot_size_bytes=empty.size_bytes, config_snapshot_sha256=empty.sha256,
            input_snapshot=empty.value, input_snapshot_schema_version=empty.schema_version,
            input_snapshot_size_bytes=empty.size_bytes, input_snapshot_sha256=empty.sha256,
        )
        routing = json_contract({
            "schema_version": 1, "mode": "fixed", "variant_count": 1, "evaluator": None,
            "routes": [{
                "index": 0, "driver_type": ProviderProfile.DriverType.ASYNC_REST,
                "media_operations": ["video.text_to_video"],
            }],
        })
        decision = json_contract({"schema_version": 1})
        stage = StageRun.objects.create(
            production_run=run, stage_key="media_generation", ordinal=1,
            mode="existing_pipeline", status=StageRun.Status.RUNNING, idempotency_key="media",
            routing=routing.value, routing_schema_version=routing.schema_version,
            routing_size_bytes=routing.size_bytes, routing_sha256=routing.sha256,
            routing_decision=decision.value,
            routing_decision_schema_version=decision.schema_version,
            routing_decision_size_bytes=decision.size_bytes,
            routing_decision_sha256=decision.sha256,
        )
        prompt = PromptArtifact.objects.create(
            project=self.project, created_by=self.user, prompt_type=PromptArtifact.PromptType.VIDEO,
            prompt_text="animate this frame", h3_mode="I2V",
            structured_payload={"reference_bindings": [{"asset_id": "unsafe"}]},
            content_hash="c" * 64,
        )
        with self.assertRaises(StageBlocked) as captured:
            _route_candidates(stage, unit_key="i2v", prompt=prompt)
        self.assertEqual(captured.exception.code, "provider_upload_contract_missing")

    def test_t2i_and_t2v_worker_submit_poll_collect_to_generated_artifact(self):
        class FakeDriver:
            def __init__(self):
                self.calls = []

            def invoke(inner, deployment, operation, payload):
                inner.calls.append(operation)
                if operation == "submit":
                    return {"request_id": f"remote-{len(inner.calls)}", "status": "queued"}
                if operation == "poll":
                    return {"status": "succeeded", "terminal_success": True}
                if operation == "collect":
                    return {
                        "artifacts": [{"url": "https://cdn.example.com/short-lived", "kind": payload_kind}],
                        "actual_cost": "0.010000", "currency": "USD",
                    }
                raise AssertionError(operation)

        for index, (operation, payload_kind) in enumerate((
            ("image.text_to_image", "image"),
            ("video.text_to_video", "video"),
        ), start=1):
            run, stage = self._run_stage(f"media-{index}")
            review_gate = self._add_artifact_review_gate(run, stage)
            contract = async_operation_contract(self.profile, operation)
            payload = dict(contract["smoke_inputs"])
            attempt_id = uuid.uuid4()
            payload.update({"schema_version": 1, "idempotency_key": f"production-attempt:{attempt_id}"})
            request = json_contract({
                "schema_version": 1, "transport": "restricted_async_rest",
                "operation": operation, "output_kind": payload_kind,
                "route": self._route(), "payload": payload,
                "execution_deadlines": {
                    "schema_version": 1,
                    "hard_timeout_seconds": 300,
                    "idle_timeout_seconds": 120,
                },
            })
            attempt = GenerationAttempt.objects.create(
                id=attempt_id, stage_run=stage, unit_key=f"unit-{index}", attempt_number=1,
                model_deployment=self.deployment, operation=operation, is_external=True,
                status=GenerationAttempt.Status.QUEUED, request_sha256=request.sha256,
                request_snapshot=request.value,
                request_snapshot_schema_version=request.schema_version,
                request_snapshot_size_bytes=request.size_bytes,
                request_snapshot_sha256=request.sha256,
                estimated_cost=Decimal("0.010000"), committed_cost=Decimal("0.010000"),
                cost_currency="USD", claim_token=uuid.uuid4(), worker_id="test",
            )
            driver = FakeDriver()

            def archive(current, **kwargs):
                metadata = json_contract({"schema_version": 1, "source": "test"})
                return [GeneratedArtifact.objects.create(
                    manifest_key=f"async:{current.id}:1", production_run=run,
                    stage_run=stage, generation_attempt=current, kind=payload_kind,
                    role="primary_generation_output", variant_group=f"unit-{index}",
                    status=GeneratedArtifact.Status.AVAILABLE,
                    storage_backend="managed_local", object_key=f"test/{current.id}.media",
                    mime_type="image/png" if payload_kind == "image" else "video/mp4",
                    size_bytes=1, sha256=str(index) * 64,
                    manifest_metadata=metadata.value,
                    manifest_schema_version=metadata.schema_version,
                    manifest_metadata_size_bytes=metadata.size_bytes,
                    manifest_metadata_sha256=metadata.sha256,
                )]

            with patch("studio.services.async_media_queue.driver_for", return_value=driver), patch(
                "studio.services.async_media_queue.archive_remote_artifacts", side_effect=archive,
            ):
                process_async_media_attempt(attempt)
                attempt.refresh_from_db()
                self.assertEqual(
                    attempt.status, GenerationAttempt.Status.SUBMITTED,
                    f"{attempt.error_code}: {attempt.error_message}",
                )
                attempt.claim_token = uuid.uuid4()
                attempt.save(update_fields=("claim_token", "updated_at"))
                process_async_media_attempt(attempt)
                attempt.refresh_from_db()
                self.assertEqual(attempt.remote_status, "succeeded")
                self.assertEqual(attempt.status, GenerationAttempt.Status.SUBMITTED)
                attempt.claim_token = uuid.uuid4()
                attempt.save(update_fields=("claim_token", "updated_at"))
                with self.captureOnCommitCallbacks(execute=True):
                    process_async_media_attempt(attempt)
            attempt.refresh_from_db()
            self.assertEqual(attempt.status, GenerationAttempt.Status.SUCCEEDED)
            self.assertEqual(attempt.actual_cost, Decimal("0.010000"))
            self.assertEqual(driver.calls, ["submit", "poll", "collect"])
            self.assertEqual(attempt.artifacts.count(), 1)
            run.refresh_from_db()
            stage.refresh_from_db()
            review_gate.refresh_from_db()
            self.assertEqual(stage.status, StageRun.Status.SUCCEEDED)
            self.assertEqual(review_gate.status, StageRun.Status.AWAITING_APPROVAL)
            self.assertEqual(run.status, ProductionRun.Status.AWAITING_APPROVAL)
            self.assertEqual(run.current_stage_key, "artifact_selection")

    def test_known_async_failure_auto_blocks_media_stage_after_commit(self):
        class RejectDriver:
            def invoke(self, deployment, operation, payload):
                raise ProviderDriverError(
                    "provider rejected the request",
                    code="provider_request_rejected",
                )

        run, stage = self._run_stage("known-failure-auto-advance")
        self._add_artifact_review_gate(run, stage)
        operation = "image.text_to_image"
        attempt_id = uuid.uuid4()
        payload = dict(async_operation_contract(self.profile, operation)["smoke_inputs"])
        payload.update({
            "schema_version": 1,
            "idempotency_key": f"production-attempt:{attempt_id}",
        })
        request = json_contract({
            "schema_version": 1,
            "transport": "restricted_async_rest",
            "operation": operation,
            "output_kind": "image",
            "route": self._route(),
            "payload": payload,
        })
        attempt = GenerationAttempt.objects.create(
            id=attempt_id,
            stage_run=stage,
            unit_key="known-failure",
            attempt_number=1,
            model_deployment=self.deployment,
            operation=operation,
            status=GenerationAttempt.Status.QUEUED,
            request_sha256=request.sha256,
            request_snapshot=request.value,
            request_snapshot_schema_version=request.schema_version,
            request_snapshot_size_bytes=request.size_bytes,
            request_snapshot_sha256=request.sha256,
            claim_token=uuid.uuid4(),
            worker_id="test",
        )

        with patch(
            "studio.services.async_media_queue.driver_for",
            return_value=RejectDriver(),
        ), self.captureOnCommitCallbacks(execute=True):
            result = process_async_media_attempt(attempt)

        self.assertEqual(result.status, GenerationAttempt.Status.FAILED)
        self.assertEqual(result.error_code, "provider_request_rejected")
        run.refresh_from_db()
        stage.refresh_from_db()
        self.assertEqual(stage.status, StageRun.Status.BLOCKED)
        self.assertEqual(run.status, ProductionRun.Status.BLOCKED)
        self.assertEqual(run.current_stage_key, "media_generation")
        self.assertEqual(run.blocked_code, "generation_tasks_failed")

    def test_single_selected_async_artifact_is_complete_and_failed_attempt_cannot_be_selected(self):
        run, stage = self._run_stage("single-selected")
        attempt = GenerationAttempt.objects.create(
            stage_run=stage, unit_key="single", attempt_number=1,
            model_deployment=self.deployment, operation="video.text_to_video",
            status=GenerationAttempt.Status.SUCCEEDED, request_sha256="a" * 64,
        )
        metadata = json_contract({"schema_version": 1})
        artifact = GeneratedArtifact.objects.create(
            manifest_key=f"async:{attempt.id}:1", production_run=run,
            stage_run=stage, generation_attempt=attempt, kind=GeneratedArtifact.Kind.VIDEO,
            role="primary_generation_output", variant_group="single", variant_index=1,
            status=GeneratedArtifact.Status.SELECTED, storage_backend="managed_local",
            object_key="test/single.mp4", mime_type="video/mp4", size_bytes=1,
            sha256="a" * 64, manifest_metadata=metadata.value,
            manifest_schema_version=metadata.schema_version,
            manifest_metadata_size_bytes=metadata.size_bytes,
            manifest_metadata_sha256=metadata.sha256,
        )
        self.assertEqual(_poll_media_stage(run, stage), "generated-artifacts:1")

        attempt.status = GenerationAttempt.Status.FAILED
        attempt.save(update_fields=("status", "updated_at"))
        artifact.status = GeneratedArtifact.Status.AVAILABLE
        artifact.save(update_fields=("status", "updated_at"))
        with self.assertRaisesMessage(Exception, "成功 GenerationAttempt"):
            select_run_artifact(run, artifact, user=self.user)

    def test_known_submit_rejection_and_queued_cancel_release_reservation(self):
        run, stage = self._run_stage("reservation-release")
        operation = "image.text_to_image"
        payload = dict(async_operation_contract(self.profile, operation)["smoke_inputs"])
        attempt_id = uuid.uuid4()
        payload.update({"schema_version": 1, "idempotency_key": f"production-attempt:{attempt_id}"})
        request = json_contract({
            "schema_version": 1, "transport": "restricted_async_rest",
            "operation": operation, "output_kind": "image",
            "route": self._route(), "payload": payload,
        })
        attempt = GenerationAttempt.objects.create(
            id=attempt_id, stage_run=stage, unit_key="known-reject", attempt_number=1,
            model_deployment=self.deployment, operation=operation, status=GenerationAttempt.Status.QUEUED,
            request_sha256=request.sha256, request_snapshot=request.value,
            request_snapshot_schema_version=request.schema_version,
            request_snapshot_size_bytes=request.size_bytes,
            request_snapshot_sha256=request.sha256,
            committed_cost=Decimal("0.010000"), cost_currency="USD",
            claim_token=uuid.uuid4(), worker_id="test",
        )
        driver = SimpleNamespace(invoke=lambda *args, **kwargs: (_ for _ in ()).throw(
            ProviderDriverError("rejected", code="provider_request_rejected")
        ))
        with patch("studio.services.async_media_queue.driver_for", return_value=driver):
            process_async_media_attempt(attempt)
        attempt.refresh_from_db()
        run.refresh_from_db()
        self.assertEqual(attempt.status, GenerationAttempt.Status.FAILED)
        self.assertEqual(attempt.committed_cost, Decimal("0"))
        self.assertEqual(run.committed_cost_total, Decimal("0"))

        queued = GenerationAttempt.objects.create(
            stage_run=stage, unit_key="queued-cancel", attempt_number=1,
            model_deployment=self.deployment, operation=operation,
            status=GenerationAttempt.Status.QUEUED, request_sha256="b" * 64,
            committed_cost=Decimal("0.010000"), cost_currency="USD",
        )
        run.status = ProductionRun.Status.RUNNING
        run.committed_cost_total = Decimal("0.010000")
        run.save(update_fields=("status", "committed_cost_total", "updated_at"))
        cancel_production_run(run, user=self.user)
        queued.refresh_from_db()
        run.refresh_from_db()
        self.assertEqual(queued.status, GenerationAttempt.Status.CANCELLED)
        self.assertEqual(queued.committed_cost, Decimal("0"))
        self.assertEqual(run.committed_cost_total, Decimal("0"))

    def test_async_definitive_failure_builds_new_attempt_identity_on_next_frozen_route(self):
        run, stage, first = self._ordered_fallback_fixture("async-fallback")

        self.assertIsNone(_poll_media_stage(run, stage))

        attempts = list(stage.generation_attempts.order_by("attempt_number"))
        self.assertEqual(len(attempts), 2)
        second = attempts[1]
        self.assertEqual((first.route_index, second.route_index), (0, 1))
        self.assertNotEqual(first.id, second.id)
        self.assertEqual(
            second.request_snapshot["payload"]["idempotency_key"],
            f"production-attempt:{second.id}",
        )
        self.assertNotEqual(
            first.request_snapshot["payload"]["idempotency_key"],
            second.request_snapshot["payload"]["idempotency_key"],
        )
        events = list(stage.route_events.filter(
            event_type__in=("route_failed", "route_selected"),
        ).order_by("sequence").values_list("event_type", "route_index"))
        self.assertIn(("route_failed", 0), events)
        self.assertGreater(events.index(("route_selected", 1)), events.index(("route_failed", 0)))
        self.assertIsNone(_poll_media_stage(run, stage))
        self.assertEqual(stage.generation_attempts.count(), 2)

    def test_async_unknown_or_invalid_submit_response_never_falls_back(self):
        for index, error_code in enumerate((
            "uncertain_remote_response", "provider_response_invalid",
        ), start=1):
            with self.subTest(error_code=error_code):
                run, stage, first = self._ordered_fallback_fixture(
                    f"async-unsafe-{index}", error_code=error_code,
                )
                definitive, classified = _definitive_media_failure(first)
                self.assertFalse(definitive)
                self.assertEqual(classified, error_code)
                with self.assertRaises(StageBlocked):
                    _poll_media_stage(run, stage)
                self.assertEqual(stage.generation_attempts.count(), 1)

    def test_actual_cost_overrun_blocks_and_releases_reservation(self):
        run, stage = self._run_stage("actual-overrun")
        run.budget_limit = Decimal("0.005000")
        run.committed_cost_total = Decimal("0.005000")
        run.save(update_fields=("budget_limit", "committed_cost_total", "updated_at"))
        attempt = GenerationAttempt.objects.create(
            stage_run=stage, unit_key="overrun", attempt_number=1,
            model_deployment=self.deployment, operation="image.text_to_image",
            status=GenerationAttempt.Status.SUBMITTED, request_sha256="c" * 64,
            committed_cost=Decimal("0.005000"), cost_currency="USD",
            claim_token=uuid.uuid4(), worker_id="test",
        )
        metadata = json_contract({"schema_version": 1})
        artifact = GeneratedArtifact.objects.create(
            manifest_key=f"async:{attempt.id}:1", production_run=run,
            stage_run=stage, generation_attempt=attempt, kind=GeneratedArtifact.Kind.IMAGE,
            role="primary_generation_output", variant_group="overrun", variant_index=1,
            status=GeneratedArtifact.Status.AVAILABLE, storage_backend="managed_local",
            object_key="test/overrun.png", mime_type="image/png", size_bytes=1,
            sha256="c" * 64, manifest_metadata=metadata.value,
            manifest_schema_version=metadata.schema_version,
            manifest_metadata_size_bytes=metadata.size_bytes,
            manifest_metadata_sha256=metadata.sha256,
        )
        _succeed(
            attempt.id, attempt.claim_token, artifacts=[artifact],
            actual_cost=Decimal("0.010000"), currency="USD",
        )
        run.refresh_from_db()
        stage.refresh_from_db()
        self.assertEqual(run.actual_cost_total, Decimal("0.010000"))
        self.assertEqual(run.committed_cost_total, Decimal("0"))
        attempt.refresh_from_db()
        self.assertEqual(attempt.committed_cost, Decimal("0"))
        self.assertEqual(run.status, ProductionRun.Status.AWAITING_APPROVAL)
        self.assertEqual(stage.reason_code, "actual_budget_exceeded")

    def test_long_submit_claim_is_not_recovered_before_frozen_timeout(self):
        run, stage = self._run_stage("claim-timeout")
        operation = "image.text_to_image"
        payload = dict(async_operation_contract(self.profile, operation)["smoke_inputs"])
        attempt_id = uuid.uuid4()
        payload.update({"schema_version": 1, "idempotency_key": f"production-attempt:{attempt_id}"})
        route = self._route()
        route["deployment_config"] = {"timeout_seconds": 600}
        route["deployment_config_sha256"] = json_contract(route["deployment_config"]).sha256
        request = json_contract({
            "schema_version": 1, "transport": "restricted_async_rest",
            "operation": operation, "output_kind": "image", "route": route, "payload": payload,
        })
        attempt = GenerationAttempt.objects.create(
            id=attempt_id, stage_run=stage, unit_key="slow-submit", attempt_number=1,
            model_deployment=self.deployment, operation=operation,
            status=GenerationAttempt.Status.SUBMITTING, request_sha256=request.sha256,
            request_snapshot=request.value, request_snapshot_schema_version=request.schema_version,
            request_snapshot_size_bytes=request.size_bytes,
            request_snapshot_sha256=request.sha256, claim_token=uuid.uuid4(), worker_id="slow",
            claimed_at=timezone.now() - timedelta(minutes=2),
            heartbeat_at=timezone.now() - timedelta(minutes=2),
        )
        result = recover_stale_async_media_attempts(minutes=1)
        attempt.refresh_from_db()
        self.assertEqual(result, {"recovered": 0, "uncertain": 0})
        self.assertEqual(attempt.status, GenerationAttempt.Status.SUBMITTING)
        self.assertIsNotNone(attempt.claim_token)

    def test_archive_remote_artifact_freezes_only_public_comparison_parameters(self):
        run, stage = self._run_stage("comparison-parameters")
        stage_config = json_contract({
            "schema_version": 1,
            "base": {"values": {
                "production.default_include_audio": True,
                "production.default_shot_duration_ms": 1000,
            }},
            "routes": {"0": {"values": {
                "production.default_include_audio": False,
                "production.default_shot_duration_ms": 2400,
                "provider.credential_ref": "secret-binding",
                "artifact.output_path": "C:\\private\\artifact.png",
            }}},
        })
        stage.config_snapshot = stage_config.value
        stage.config_snapshot_schema_version = stage_config.schema_version
        stage.config_snapshot_size_bytes = stage_config.size_bytes
        stage.config_snapshot_sha256 = stage_config.sha256
        stage.selected_route_index = 0
        stage.save(update_fields=(
            "config_snapshot", "config_snapshot_schema_version",
            "config_snapshot_size_bytes", "config_snapshot_sha256",
            "selected_route_index", "updated_at",
        ))
        request = json_contract({
            "schema_version": 1,
            "transport": "restricted_async_rest",
            "operation": "image.text_to_image",
            "route": {
                **self._route(),
                "credential_ref": "must-not-leak",
                "workflow_snapshot": {
                    "workflow_version": 3,
                    "template": {"slug": "cloud-image"},
                    "input_contract": {"workflow_mode": "text_to_image"},
                    "execution_contract_sha256": "d" * 64,
                },
            },
            "payload": {
                "schema_version": 1,
                "model": "cloud-v1",
                "width": 768,
                "height": 1344,
                "seed": 9876,
                "fps": 24,
                "duration_seconds": 2.4,
                "audio": False,
                "prompt": "private prompt",
                "idempotency_key": "private-request-key",
                "output_path": "C:\\private\\artifact.png",
            },
        })
        attempt = GenerationAttempt.objects.create(
            stage_run=stage,
            unit_key="shot:comparison:variant:2",
            attempt_number=1,
            model_deployment=self.deployment,
            route_index=0,
            operation="image.text_to_image",
            status=GenerationAttempt.Status.SUBMITTED,
            request_sha256=request.sha256,
            request_snapshot=request.value,
            request_snapshot_schema_version=request.schema_version,
            request_snapshot_size_bytes=request.size_bytes,
            request_snapshot_sha256=request.sha256,
        )
        body = b"managed-remote-image"
        facts = {
            "mime_type": "image/png",
            "size_bytes": len(body),
            "sha256": hashlib.sha256(body).hexdigest(),
        }

        def download(**kwargs):
            kwargs["target"].parent.mkdir(parents=True, exist_ok=True)
            kwargs["target"].write_bytes(body)
            return facts

        with tempfile.TemporaryDirectory() as directory, override_settings(MEDIA_ROOT=directory), patch(
            "studio.services.remote_artifacts._artifact_limit", return_value=1024,
        ), patch("studio.services.remote_artifacts._download_one", side_effect=download):
            artifacts = archive_remote_artifacts(
                attempt,
                artifacts=[{"url": "https://cdn.example.com/result.png", "kind": "image"}],
                provider_config=_provider_config(),
                expected_kind="image",
            )

        self.assertEqual(artifacts[0].comparison_parameters, {
            "schema_version": 1,
            "model": "cloud-v1",
            "operation": "image.text_to_image",
            "generation": {
                "seed": 9876, "width": 768, "height": 1344, "fps": 24,
                "duration_seconds": 2.4, "include_audio": False,
            },
            "workflow": {"template": "cloud-image", "version": 3, "mode": "text_to_image"},
            "configuration": {"include_audio": False, "default_shot_duration_ms": 2400},
        })
        serialized = json.dumps(artifacts[0].comparison_parameters, sort_keys=True)
        for forbidden in ("credential", "idempotency", "private", "sha256", "prompt"):
            self.assertNotIn(forbidden, serialized)

    def test_cancel_during_collect_removes_unmanaged_download(self):
        run, stage = self._run_stage("cancel-collect")
        attempt = GenerationAttempt.objects.create(
            stage_run=stage, unit_key="cancel-collect", attempt_number=1,
            model_deployment=self.deployment, operation="image.text_to_image",
            status=GenerationAttempt.Status.SUBMITTED, request_sha256="e" * 64,
        )
        body = b"downloaded-before-cancel"
        import hashlib
        facts = {
            "mime_type": "image/png", "size_bytes": len(body),
            "sha256": hashlib.sha256(body).hexdigest(),
        }

        def download(**kwargs):
            kwargs["target"].parent.mkdir(parents=True, exist_ok=True)
            kwargs["target"].write_bytes(body)
            GenerationAttempt.objects.filter(pk=attempt.id).update(ignore_late_result=True)
            return facts

        with tempfile.TemporaryDirectory() as directory, override_settings(MEDIA_ROOT=directory), patch(
            "studio.services.remote_artifacts._artifact_limit", return_value=1024,
        ), patch("studio.services.remote_artifacts._download_one", side_effect=download):
            orphan = (
                Path(directory) / "generated-artifacts" / str(run.owner_id)
                / str(run.project_id) / "async" / str(attempt.id) / "artifact-1.media"
            )
            orphan.parent.mkdir(parents=True, exist_ok=True)
            orphan.write_bytes(b"orphan-from-crashed-collect")
            with self.assertRaises(ProviderDriverError) as captured:
                archive_remote_artifacts(
                    attempt,
                    artifacts=[{"url": "https://cdn.example.com/late", "kind": "image"}],
                    provider_config=_provider_config(), expected_kind="image",
                )
            leftovers = list(Path(directory).rglob("*.media"))
        self.assertEqual(captured.exception.code, "late_result_ignored")
        self.assertEqual(leftovers, [])
        claim_token = uuid.uuid4()
        GenerationAttempt.objects.filter(pk=attempt.id).update(
            claim_token=claim_token,
            ignore_late_result=True,
            committed_cost=Decimal("0.010000"),
            cost_currency="USD",
        )
        ProductionRun.objects.filter(pk=run.id).update(
            committed_cost_total=Decimal("0.010000"),
        )
        _succeed(
            attempt.id,
            claim_token,
            artifacts=[],
            actual_cost=Decimal("0.003000"),
            currency="USD",
        )
        attempt.refresh_from_db()
        run.refresh_from_db()
        self.assertEqual(attempt.status, GenerationAttempt.Status.CANCELLED)
        self.assertEqual(attempt.error_code, "late_result_ignored")
        self.assertEqual(attempt.actual_cost, Decimal("0.003000"))
        self.assertEqual(attempt.committed_cost, Decimal("0"))
        self.assertEqual(run.actual_cost_total, Decimal("0.003000"))
        self.assertEqual(run.committed_cost_total, Decimal("0"))

    def test_managed_mixed_media_is_frozen_in_storyboard_order_for_assembly(self):
        from studio.models import Episode
        import hashlib

        run, stage = self._run_stage("managed-assembly")
        episode = Episode.objects.create(project=self.project, number=1, title="Episode")
        image_shot = Shot.objects.create(
            episode=episode, number=1, narrative="cloud image",
            media_type=Shot.MediaType.IMAGE, duration_ms=500,
        )
        video_shot = Shot.objects.create(
            episode=episode, number=2, narrative="cloud video",
            media_type=Shot.MediaType.VIDEO, duration_ms=700,
        )
        with tempfile.TemporaryDirectory() as directory:
            root = Path(directory)
            artifacts = []
            for index, (shot, kind, filename, content) in enumerate((
                (image_shot, GeneratedArtifact.Kind.IMAGE, "cloud.png", b"managed-image"),
                (video_shot, GeneratedArtifact.Kind.VIDEO, "cloud.mp4", b"managed-video"),
            ), start=1):
                prompt = PromptArtifact.objects.create(
                    project=self.project, episode=episode, shot=shot, created_by=self.user,
                    prompt_type=kind, prompt_text=f"cloud {kind}",
                    total_duration_ms=shot.duration_ms, content_hash=str(index) * 64,
                )
                snapshot = json_contract({
                    "schema_version": 1, "prompt_artifact_id": str(prompt.id),
                })
                attempt = GenerationAttempt.objects.create(
                    stage_run=stage, unit_key=f"assembly-{kind}", attempt_number=1,
                    model_deployment=self.deployment,
                    operation=(
                        "image.text_to_image"
                        if kind == GeneratedArtifact.Kind.IMAGE
                        else "video.text_to_video"
                    ),
                    status=GenerationAttempt.Status.SUCCEEDED, request_sha256=snapshot.sha256,
                    request_snapshot=snapshot.value,
                    request_snapshot_schema_version=snapshot.schema_version,
                    request_snapshot_size_bytes=snapshot.size_bytes,
                    request_snapshot_sha256=snapshot.sha256,
                )
                media = root / "generated-artifacts" / filename
                media.parent.mkdir(parents=True, exist_ok=True)
                media.write_bytes(content)
                digest = hashlib.sha256(media.read_bytes()).hexdigest()
                metadata = json_contract({"schema_version": 1})
                artifacts.append(GeneratedArtifact.objects.create(
                    manifest_key=f"async:{attempt.id}:1", production_run=run,
                    stage_run=stage, generation_attempt=attempt, kind=kind,
                    role="primary_generation_output", variant_group=f"assembly-{kind}", variant_index=1,
                    status=GeneratedArtifact.Status.SELECTED, storage_backend="managed_local",
                    object_key=media.relative_to(root).as_posix(),
                    mime_type="image/png" if kind == GeneratedArtifact.Kind.IMAGE else "video/mp4",
                    size_bytes=media.stat().st_size, sha256=digest,
                    manifest_metadata=metadata.value, manifest_schema_version=metadata.schema_version,
                    manifest_metadata_size_bytes=metadata.size_bytes,
                    manifest_metadata_sha256=metadata.sha256,
                ))

            def media_facts(path, *, expected_kind, **kwargs):
                target = Path(path)
                return {
                    "mime_type": "image/png" if expected_kind == "image" else "video/mp4",
                    "size_bytes": target.stat().st_size,
                    "sha256": hashlib.sha256(target.read_bytes()).hexdigest(),
                }

            with override_settings(
                MEDIA_ROOT=root, COMFYUI_OUTPUT_DIR=root, ASSEMBLY_AI_LABEL_ENABLED=False,
            ), patch(
                "studio.services.assembly.verified_local_media_facts", side_effect=media_facts,
            ), patch(
                "studio.services.assembly._probe_media_streams", return_value=frozenset({"video"}),
            ), patch(
                "studio.services.release_bundles.verified_local_media_facts", side_effect=media_facts,
            ):
                self.assertEqual(_poll_media_stage(run, stage), "generated-artifacts:2")
                routing = json_contract({"schema_version": 1})
                assembly_stage = StageRun.objects.create(
                    production_run=run, stage_key="assembly", ordinal=2,
                    mode="existing_pipeline", status=StageRun.Status.RUNNING,
                    idempotency_key="stage-managed-assembly-assembly",
                    routing=routing.value,
                    routing_schema_version=routing.schema_version,
                    routing_size_bytes=routing.size_bytes,
                    routing_sha256=routing.sha256,
                    routing_decision=routing.value,
                    routing_decision_schema_version=routing.schema_version,
                    routing_decision_size_bytes=routing.size_bytes,
                    routing_decision_sha256=routing.sha256,
                )
                self.assertIsNone(_execute_assembly(run, assembly_stage))
                job = run.assembly_links.get(episode=episode).assembly_job
                sources = list(AssemblySourceItem.objects.filter(assembly_job=job).order_by("ordinal"))
                self.assertEqual([source.shot_id for source in sources], [image_shot.id, video_shot.id])
                self.assertEqual([source.generated_artifact_id for source in sources], [
                    artifacts[0].id, artifacts[1].id,
                ])
                self.assertEqual([source.media_kind for source in sources], ["image", "video"])
                image_input = job.ffmpeg_command.index(str(root / artifacts[0].object_key))
                self.assertEqual(job.ffmpeg_command[image_input - 5:image_input], [
                    "-loop", "1", "-framerate", "24", "-i",
                ])

                output = Path(job.output_path)
                output.write_bytes(b"episode-master")
                persist_assembly_provenance(job, generated_at=timezone.now())
                job.status = job.Status.SUCCEEDED
                job.finished_at = timezone.now()
                job.save(update_fields=("status", "finished_at", "updated_at"))
                result = _execute_assembly(run, assembly_stage)
                self.assertEqual(result, "episode-masters:1")
                delivery_stage = StageRun.objects.create(
                    production_run=run, stage_key="delivery", ordinal=3,
                    mode="existing_pipeline", status=StageRun.Status.RUNNING,
                    idempotency_key="stage-managed-assembly-delivery",
                    routing=routing.value,
                    routing_schema_version=routing.schema_version,
                    routing_size_bytes=routing.size_bytes,
                    routing_sha256=routing.sha256,
                    routing_decision=routing.value,
                    routing_decision_schema_version=routing.schema_version,
                    routing_decision_size_bytes=routing.size_bytes,
                    routing_decision_sha256=routing.sha256,
                )
                result = _execute_delivery(run, delivery_stage)
                bundle = ReleaseBundle.objects.get(production_run=run)
                self.assertEqual(result, f"release-bundle:{bundle.id}")
                self.assertEqual(bundle.status, ReleaseBundle.Status.SEALED)
                document = verified_release_bundle(bundle)
                self.assertEqual(
                    {
                        row["shot_id"]: row["artifact_id"]
                        for row in document["selection"]["shots"]
                    },
                    {
                        str(image_shot.id): str(artifacts[0].id),
                        str(video_shot.id): str(artifacts[1].id),
                    },
                )
                master = GeneratedArtifact.objects.get(
                    production_run=run, manifest_key=f"assembly:{job.id}",
                )
                self.assertEqual(
                    document["selection"]["episodes"],
                    [{"episode_id": str(episode.id), "artifact_id": str(master.id)}],
                )

    def test_real_smoke_evidence_gates_cloud_readiness_without_gpu(self):
        class SmokeDriver:
            def estimate(inner, deployment, operation, payload):
                return {"amount": "0.010000", "currency": "USD", "basis": "fixed"}

            def invoke(inner, deployment, operation, payload):
                if operation == "submit":
                    return {"request_id": "remote-smoke-1", "status": "queued"}
                if operation == "poll":
                    return {"status": "succeeded", "terminal_success": True}
                if operation == "collect":
                    return {
                        "artifacts": [{"url": "https://cdn.example.com/expiring", "kind": "image"}],
                        "actual_cost": "0.010000", "currency": "USD",
                    }
                raise AssertionError(operation)

        with patch("studio.services.deployment_smoke.driver_for", return_value=SmokeDriver()), patch(
            "studio.services.deployment_smoke.verify_remote_artifacts_for_smoke",
            return_value=[{
                "mime_type": "image/png", "size_bytes": 10, "sha256": "d" * 64,
                "width": 64, "height": 64,
            }],
        ):
            snapshot = probe_async_deployment(
                self.deployment, operation="image.text_to_image",
            )
        self.assertEqual(snapshot.status, ModelDeploymentCapabilitySnapshot.Status.PASSED)
        self.assertNotIn("https://", str(snapshot.evidence))
        self.assertNotIn("remote-smoke-1", str(snapshot.evidence))
        self.assertEqual(fresh_smoke_snapshot(self.deployment, "image.text_to_image"), snapshot)
        WorkerLease.objects.create(key="async-media-worker-heartbeat")
        result = _media_capability_readiness([{
            "model_deployment_id": str(self.deployment.id),
        }], modality="image")
        self.assertEqual(result["state"], "ready")
        snapshot.expires_at = snapshot.captured_at
        snapshot.save(update_fields=("expires_at", "updated_at"))
        result = _media_capability_readiness([{
            "model_deployment_id": str(self.deployment.id),
        }], modality="image")
        self.assertEqual(result["state"], "blocked")
        self.assertIn("async_smoke_missing_or_stale", result["reason_codes"])

    def test_mixed_fallback_is_ready_when_async_route_and_worker_are_viable(self):
        provider_config = json_contract({"base_url": "http://127.0.0.1:8188"})
        provider = ProviderProfile.objects.create(
            key="comfy.mixed-readiness", name="Comfy mixed readiness",
            driver_type=ProviderProfile.DriverType.COMFYUI, is_active=True,
            driver_config=provider_config.value,
            driver_config_schema_version=provider_config.schema_version,
            driver_config_size_bytes=provider_config.size_bytes,
            driver_config_sha256=provider_config.sha256,
        )
        spec = json_contract({"schema_version": 1})
        profile = ModelProfileVersion.objects.create(
            profile_key="comfy.mixed-readiness", version=1, provider=provider,
            model_identifier="local-video", modality=ModelProfileVersion.Modality.VIDEO,
            status=ModelProfileVersion.Status.PUBLISHED,
            specification=spec.value, specification_schema_version=spec.schema_version,
            specification_size_bytes=spec.size_bytes, specification_sha256=spec.sha256,
        )
        runtime = json_contract({})
        deployment = ModelDeployment.objects.create(
            key="comfy.mixed-readiness", name="Comfy mixed readiness",
            profile_version=profile, status=ModelDeployment.Status.ACTIVE,
            runtime_config=runtime.value, runtime_config_schema_version=runtime.schema_version,
            runtime_config_size_bytes=runtime.size_bytes, runtime_config_sha256=runtime.sha256,
        )
        create_test_prompt_profile_binding(
            actor=self.user,
            model_profile_version=profile,
            operation="video.generate",
        )
        template = WorkflowTemplate.objects.create(
            slug="mixed-readiness-video", version=1, name="mixed readiness",
            output_kind=WorkflowTemplate.OutputKind.VIDEO, definition={}, input_schema={},
        )
        graph = json_contract({"schema_version": 1})
        workflow = WorkflowTemplateVersion.objects.create(
            template=template, version=1, provider=provider, model_profile_version=profile,
            graph=graph.value, graph_schema_version=graph.schema_version,
            graph_size_bytes=graph.size_bytes, graph_sha256=graph.sha256,
            input_contract=graph.value, input_contract_schema_version=graph.schema_version,
            input_contract_size_bytes=graph.size_bytes, input_contract_sha256=graph.sha256,
            status=WorkflowTemplateVersion.Status.PUBLISHED,
            validation_state=WorkflowTemplateVersion.ValidationState.PASSED,
            smoke_test_state=WorkflowTemplateVersion.ValidationState.PASSED,
            is_current=True,
        )
        routes = [
            {"model_deployment_id": str(deployment.id), "workflow_version_id": str(workflow.id)},
            {"model_deployment_id": str(self.deployment.id)},
        ]

        def worker(key, _label, *, required):
            return {"ok": (not required) or key == "async-media-worker-heartbeat"}

        with patch(
            "studio.services.readiness.fresh_smoke_snapshot", return_value=SimpleNamespace(),
        ), patch("studio.services.readiness._worker_check", side_effect=worker):
            result = _media_capability_readiness(routes, modality="video")
        self.assertEqual(result["state"], "ready")
        self.assertIn("generation_worker_unavailable", result["reason_codes"])

    def test_confirmed_remote_submitted_recovers_poll_and_attempt_api_redacts_snapshot(self):
        run, stage = self._run_stage("uncertain")
        operation = "image.text_to_image"
        payload = dict(async_operation_contract(self.profile, operation)["smoke_inputs"])
        attempt_id = uuid.uuid4()
        payload.update({"schema_version": 1, "idempotency_key": f"production-attempt:{attempt_id}"})
        request = json_contract({
            "schema_version": 1, "transport": "restricted_async_rest",
            "operation": operation, "output_kind": "image",
            "route": self._route(),
            "payload": {**payload, "prompt": "must-not-leak https://signed.example/x"},
        })
        attempt = GenerationAttempt.objects.create(
            id=attempt_id, stage_run=stage, unit_key="uncertain", attempt_number=1,
            model_deployment=self.deployment, operation=operation, is_external=True,
            status=GenerationAttempt.Status.FAILED, error_code="uncertain_remote_response",
            request_sha256=request.sha256, request_snapshot=request.value,
            request_snapshot_schema_version=request.schema_version,
            request_snapshot_size_bytes=request.size_bytes,
            request_snapshot_sha256=request.sha256,
            estimated_cost=Decimal("0.010000"), committed_cost=Decimal("0.010000"),
            cost_currency="USD",
        )
        self.user.is_staff = True
        self.user.save(update_fields=("is_staff",))
        resolve_uncertain_attempt(
            run, attempt_id=attempt.id,
            outcome="confirmed_remote_submitted",
            note="verified in provider control plane", user=self.user,
            provider_request_id="remote-confirmed-1",
        )
        attempt.refresh_from_db()
        self.assertEqual(attempt.status, GenerationAttempt.Status.SUBMITTED)
        self.assertEqual(attempt.provider_request_id, "remote-confirmed-1")
        self.assertIsNotNone(attempt.next_poll_at)
        client = APIClient()
        client.force_authenticate(self.user)
        response = client.get(f"/api/v1/generation-attempts/{attempt.id}/")
        self.assertEqual(response.status_code, 200)
        self.assertNotIn("request_snapshot", response.json())
        self.assertNotIn("must-not-leak", response.content.decode("utf-8"))
