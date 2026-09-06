from decimal import Decimal
from copy import deepcopy
from datetime import timedelta
import hashlib
from pathlib import Path
import tempfile
from types import SimpleNamespace
from unittest.mock import patch
from uuid import uuid4

from django.core.exceptions import ValidationError
from django.test import TestCase, override_settings
from studio.tests.workflow_projector_fixtures import workflow_projector_reference
from django.utils import timezone
from PIL import Image
from rest_framework.test import APIClient

from studio.models import (
    Asset,
    AssetGenerationJob,
    AssetGenerationJobResolution,
    GeneratedWorkflow,
    GenerationTask,
    ModelDeployment,
    ModelDeploymentCapabilitySnapshot,
    ModelProfileVersion,
    ProviderProfile,
    PromptArtifact,
    WorkflowTemplate,
    WorkflowTemplateVersion,
    WorkflowSmokeAttempt,
)
from studio.services.asset_generation import (
    _eligible_workflow_route,
    _frozen_output_prefix,
    _workflow_user_parameters,
    claim_next_comfy_asset_job,
    claim_next_media_asset_job,
    claim_next_text_asset_job,
    process_media_asset_job,
    process_comfy_asset_job,
    process_text_asset_job,
    reconcile_uncertain_comfy_asset_jobs,
    recover_stale_asset_generation_jobs,
    resolve_uncertain_asset_generation_job,
)
from studio.services.task_queue import claim_next_task
from studio.services.workflow_smoke_queue import claim_next_workflow_smoke
from studio.services.workflow_smoke_resolution import resolve_workflow_smoke_attempt
from studio.services.deployment_smoke import record_deployment_capability_snapshot
from studio.services.json_contracts import assign_json_contract, json_contract
from studio.services.provider_registry import ProviderDriverError
from studio.services.workflow_versions import (
    import_workflow_version,
    workflow_execution_target_sha256,
)
from studio.tests.factories import build_story, series_concept_field_contract


class _TextDriver:
    def estimate(self, deployment, operation, payload):
        return {"amount": Decimal("0.010000"), "currency": "CNY"}

    def invoke(self, deployment, operation, payload):
        return {
            "schema_version": 1,
            "request_id": "fixture-request",
            "text": "# 第一集\n\n林默在雨夜发现门锁完好。",
            "usage": {},
        }


class _MediaDriver:
    def invoke(self, deployment, operation, payload):
        if operation == "submit":
            return {"schema_version": 1, "request_id": "audio-request-1", "status": "submitted"}
        if operation == "poll":
            return {
                "schema_version": 1,
                "request_id": "audio-request-1",
                "status": "succeeded",
                "terminal_success": True,
                "terminal_failure": False,
                "terminal_cancelled": False,
            }
        if operation == "collect":
            return {
                "schema_version": 1,
                "request_id": "audio-request-1",
                "status": "succeeded",
                "actual_cost": "0.020000",
                "currency": "CNY",
                "artifacts": [{"kind": "audio", "url": "http://127.0.0.1:19003/audio.wav"}],
            }
        raise AssertionError(operation)


class _CancelFailureDriver(_MediaDriver):
    def invoke(self, deployment, operation, payload):
        if operation == "cancel":
            return {
                "schema_version": 1,
                "request_id": "audio-request-1",
                "status": "failed",
                "terminal_success": False,
                "terminal_failure": True,
                "terminal_cancelled": False,
            }
        return super().invoke(deployment, operation, payload)


class _CancelSuccessDriver(_MediaDriver):
    def invoke(self, deployment, operation, payload):
        if operation == "cancel":
            return {
                "schema_version": 1,
                "request_id": "audio-request-1",
                "status": "succeeded",
                "terminal_success": True,
                "terminal_failure": False,
                "terminal_cancelled": False,
            }
        if operation == "collect":
            return {
                "schema_version": 1,
                "request_id": "audio-request-1",
                "status": "succeeded",
                "actual_cost": "0.020000",
                "currency": "CNY",
                "artifacts": [],
            }
        return super().invoke(deployment, operation, payload)


class _LongRequestIdMediaDriver(_MediaDriver):
    request_id = "r" * 192

    def invoke(self, deployment, operation, payload):
        if operation == "submit":
            return {
                "schema_version": 1,
                "request_id": self.request_id,
                "status": "submitted",
            }
        return super().invoke(deployment, operation, payload)


class _ComfyAssetDriver:
    def estimate(self, deployment, operation, payload):
        return {"amount": Decimal("0"), "currency": ""}

    def invoke(self, deployment, operation, payload):
        if operation == "upload":
            return {
                "name": payload["filename"],
                "subfolder": payload["subfolder"],
                "type": "input",
            }
        if operation == "submit":
            self.submitted_workflow = payload["workflow"]
            self.output_prefix = str(
                payload["workflow"]["7"]["inputs"]["filename_prefix"],
            )
            return {"request_id": payload["submission_token"]}
        if operation == "poll":
            return {"request_id": payload["request_id"], "status": "succeeded"}
        if operation == "collect":
            prefix = Path(self.output_prefix)
            return {
                "request_id": payload["request_id"],
                "status": "succeeded",
                "outputs": [{
                    "node_id": "7",
                    "kind": "images",
                    "filename": f"{prefix.name}_00001_.png",
                    "subfolder": prefix.parent.as_posix(),
                    "type": "output",
                }],
            }
        raise AssertionError(operation)


class AssetGenerationTests(TestCase):
    def setUp(self):
        self.temp = tempfile.TemporaryDirectory()
        self.addCleanup(self.temp.cleanup)
        self.media_root = Path(self.temp.name) / "media"
        self.media_root.mkdir()
        self.override = override_settings(MEDIA_ROOT=self.media_root)
        self.override.enable()
        self.addCleanup(self.override.disable)
        self.user, self.project, *_ = build_story("asset-generation")
        self.client = APIClient()
        self.client.force_authenticate(self.user)

    def test_workflow_option_normalizes_numeric_default_to_declared_multiple(self):
        schema, defaults = _workflow_user_parameters({
            "parameters": {
                "parameter.h3.length": {
                    "type": "integer", "required": True, "default": 124,
                    "min": 5, "max": 3600, "multiple_of": 17,
                },
            },
        })
        self.assertEqual(schema["parameter.h3.length"]["multiple_of"], 17)
        self.assertEqual(defaults, {"parameter.h3.length": 119})

    def text_deployment(self) -> ModelDeployment:
        suffix = uuid4().hex[:10]
        provider = ProviderProfile(
            key=f"asset-text-provider-{suffix}",
            name="Asset text fixture",
            driver_type=ProviderProfile.DriverType.OPENAI_COMPATIBLE,
            modalities="text",
            operations="script.generate",
            is_active=True,
        )
        assign_json_contract(provider, "driver_config", {
            "base_url": "http://127.0.0.1:19001",
            "chat_path": "/v1/chat/completions",
            "timeout_seconds": 10,
            "capability_ttl_seconds": 3_600,
            "pricing": {"currency": "CNY", "fixed_per_request": "0.010000"},
        })
        provider.save()
        profile = ModelProfileVersion(
            profile_key=f"asset-text-{suffix}",
            version=1,
            provider=provider,
            model_identifier="fixture-text-model",
            modality=ModelProfileVersion.Modality.TEXT,
            status=ModelProfileVersion.Status.PUBLISHED,
            published_at=timezone.now(),
        )
        assign_json_contract(profile, "specification", {
            "schema_version": 1,
            "capabilities": ["script.generate"],
            "series_concept": {
                "minimum_shot_duration_ms": 1000,
                "maximum_shot_duration_ms": 15000,
                "max_output_tokens": 4096,
                "timeout_seconds": 30,
                "field_contract": series_concept_field_contract(),
                "chat_request_options": {
                    "temperature": 0.3,
                    "response_format": {"type": "json_object"},
                },
            },
        })
        profile.save()
        deployment = ModelDeployment(
            key=f"asset-text-deployment-{suffix}",
            name="Asset text deployment",
            profile_version=profile,
            status=ModelDeployment.Status.ACTIVE,
        )
        assign_json_contract(deployment, "runtime_config", {})
        deployment.save()
        record_deployment_capability_snapshot(
            deployment,
            probe_kind=ModelDeploymentCapabilitySnapshot.ProbeKind.SMOKE,
            status=ModelDeploymentCapabilitySnapshot.Status.PASSED,
            operations=["script.generate"],
            evidence={"schema_version": 1, "execution_submitted": True, "fixture": True},
        )
        return deployment

    def audio_deployment(self) -> ModelDeployment:
        suffix = uuid4().hex[:10]
        provider = ProviderProfile(
            key=f"asset-audio-worker-provider-{suffix}",
            name="Asset audio worker fixture",
            driver_type=ProviderProfile.DriverType.ASYNC_REST,
            modalities="audio",
            operations="audio.text_to_audio",
            is_active=True,
        )
        assign_json_contract(provider, "driver_config", {
            "base_url": "http://127.0.0.1:19003",
            "health_path": "/health",
            "submit_path": "/jobs",
            "poll_path": "/jobs/{request_id}",
            "cancel_path": "/jobs/{request_id}",
            "collect_path": "/jobs/{request_id}",
            "request_id_field": "id",
            "status_field": "status",
            "artifacts_field": "artifacts",
            "artifact_url_field": "url",
            "artifact_kind_field": "kind",
            "artifact_id_field": "id",
            "artifact_sha256_field": "sha256",
            "actual_cost_field": "actual_cost",
            "currency_field": "currency",
            "idempotency_header": "Idempotency-Key",
            "terminal_success": ["succeeded"],
            "terminal_failure": ["failed"],
            "terminal_cancelled": ["cancelled"],
            "request_field_map": {
                "model": "model", "prompt": "prompt", "duration_seconds": "duration",
            },
            "artifact_allowed_hosts": ["127.0.0.1"],
            "pricing": {"currency": "CNY", "fixed_per_request": "0.020000"},
            "timeout_seconds": 30,
            "poll_interval_seconds": 0.25,
            "max_poll_seconds": 10,
            "capability_ttl_seconds": 3_600,
        })
        provider.save()
        profile = ModelProfileVersion(
            profile_key=f"asset-audio-worker-{suffix}", version=1, provider=provider,
            model_identifier="fixture-audio-worker-model",
            modality=ModelProfileVersion.Modality.AUDIO,
            status=ModelProfileVersion.Status.PUBLISHED,
            published_at=timezone.now(),
        )
        assign_json_contract(profile, "specification", {
            "schema_version": 1,
            "capabilities": ["audio.text_to_audio"],
            "async_media": {
                "schema_version": 1,
                "operations": {
                    "audio.text_to_audio": {
                        "output_kind": "audio",
                        "request_schema": {
                            "type": "object", "additionalProperties": False,
                            "required": ["prompt", "duration_seconds"],
                            "properties": {
                                "prompt": {"type": "string", "minLength": 1, "maxLength": 60000},
                                "duration_seconds": {"type": "number", "minimum": 2, "maximum": 15},
                            },
                        },
                        "smoke_inputs": {"prompt": "雨声", "duration_seconds": 2},
                    },
                },
            },
        })
        profile.save()
        deployment = ModelDeployment(
            key=f"asset-audio-worker-deployment-{suffix}",
            name="Asset audio worker deployment",
            profile_version=profile,
            status=ModelDeployment.Status.ACTIVE,
        )
        assign_json_contract(deployment, "runtime_config", {})
        deployment.save()
        record_deployment_capability_snapshot(
            deployment,
            probe_kind=ModelDeploymentCapabilitySnapshot.ProbeKind.SMOKE,
            status=ModelDeploymentCapabilitySnapshot.Status.PASSED,
            operations=["audio.text_to_audio"],
            evidence={"schema_version": 1, "execution_submitted": True, "fixture": True},
        )
        return deployment

    def comfy_workflow_route(self):
        suffix = uuid4().hex[:10]
        provider = ProviderProfile(
            key=f"asset-comfy-provider-{suffix}",
            name="Asset Comfy fixture",
            driver_type=ProviderProfile.DriverType.COMFYUI,
            modalities="image,audio,video",
            operations="submit,poll,cancel,collect",
            is_active=True,
        )
        assign_json_contract(provider, "driver_config", {
            "base_url": "http://127.0.0.1:19004",
            "timeout_seconds": 30,
            "poll_interval_seconds": 2,
            "max_poll_seconds": 3_600,
            "capability_ttl_seconds": 3_600,
        })
        provider.save()
        profile = ModelProfileVersion(
            profile_key=f"asset-comfy-{suffix}",
            version=1,
            provider=provider,
            model_identifier="fixture-comfy-model",
            modality=ModelProfileVersion.Modality.IMAGE,
            status=ModelProfileVersion.Status.PUBLISHED,
            published_at=timezone.now(),
        )
        assign_json_contract(profile, "specification", {
            "schema_version": 1,
            "capabilities": ["image.text_to_image"],
        })
        profile.save()
        deployment = ModelDeployment(
            key=f"asset-comfy-deployment-{suffix}",
            name="Asset Comfy deployment",
            profile_version=profile,
            status=ModelDeployment.Status.ACTIVE,
        )
        assign_json_contract(deployment, "runtime_config", {})
        deployment.save()
        template = WorkflowTemplate.objects.create(
            owner=None,
            name="Asset Comfy image",
            slug=f"asset-comfy-image-{suffix}",
            output_kind=WorkflowTemplate.OutputKind.IMAGE,
            model="fixture-comfy-model",
            is_system=True,
            is_active=True,
        )
        graph = {
            "1": {
                "class_type": "EmptyLatentImage",
                "inputs": {"width": 512, "height": 512, "batch_size": 1},
            },
            "7": {
                "class_type": "SaveImage",
                "inputs": {"images": ["1", 0], "filename_prefix": "fixture"},
            },
        }
        contract = {
            "schema_version": 3,
            "workflow_mode": "asset-image-fixture",
            "capability": "image.text_to_image",
            "compiler": {"id": "generic.comfy", "version": 1},
            "resolution_strategy": {
                "mode": "explicit_width_height",
                "node_ids": ["1"],
            },
            "required_dimension_multiple": 8,
            "parameters": {
                "image.width": {
                    "type": "integer", "required": True, "min": 64,
                    "max": 2048, "multiple_of": 8, "default": 512,
                },
                "image.height": {
                    "type": "integer", "required": True, "min": 64,
                    "max": 2048, "multiple_of": 8, "default": 512,
                },
                "output.prefix": {"type": "string", "required": True},
            },
            "outputs": {"primary": {"node_id": "7", "kind": "image", "required": True}},
            "smoke_inputs": {"image.width": 512, "image.height": 512},
        }
        version = import_workflow_version(
            template=template,
            graph=graph,
            input_contract={**contract, **workflow_projector_reference()},
            semantic_bindings=[
                {
                    "semantic_key": "image.width",
                    "node_id": "1",
                    "input_name": "width",
                    "value_type": "integer",
                },
                {
                    "semantic_key": "image.height",
                    "node_id": "1",
                    "input_name": "height",
                    "value_type": "integer",
                },
                {
                    "semantic_key": "output.prefix",
                    "node_id": "7",
                    "input_name": "filename_prefix",
                    "value_type": "string",
                },
            ],
            model_requirements=[],
            provider=provider,
            model_profile_version=profile,
        )
        version.status = WorkflowTemplateVersion.Status.PUBLISHED
        version.validation_state = WorkflowTemplateVersion.ValidationState.PASSED
        version.smoke_test_state = WorkflowTemplateVersion.ValidationState.PASSED
        version.validated_deployment = deployment
        version.validation_evidence_sha256 = "v" * 64
        version.smoke_test_evidence_sha256 = "s" * 64
        version.is_current = True
        version.published_at = timezone.now()
        version.published_by = self.user
        version.save(update_fields=(
            "status", "validation_state", "smoke_test_state",
            "validated_deployment", "validation_evidence_sha256",
            "smoke_test_evidence_sha256", "is_current", "published_at",
            "published_by", "updated_at",
        ))
        return version, deployment

    @staticmethod
    def compiled_comfy_image(*_args, output_prefix: str, **_kwargs):
        graph = {
            "7": {
                "class_type": "SaveImage",
                "inputs": {"filename_prefix": output_prefix},
            },
        }
        return SimpleNamespace(
            graph=graph,
            graph_sha256=json_contract(graph).sha256,
            asset_bindings=[],
            output_contract={
                "schema_version": 1,
                "primary": {"node_id": "7", "kind": "image", "required": True},
            },
        )

    def create_comfy_image_job(self, *, key: str):
        version, deployment = self.comfy_workflow_route()
        smoke = SimpleNamespace(id=uuid4())
        target_sha256 = workflow_execution_target_sha256(version, deployment)
        route = (version, deployment, smoke, target_sha256)
        driver = _ComfyAssetDriver()
        with (
            patch(
                "studio.services.asset_generation._eligible_workflow_route",
                return_value=route,
            ),
            patch(
                "studio.services.asset_generation.materialize_workflow_execution_graph",
                side_effect=self.compiled_comfy_image,
            ),
            patch("studio.services.asset_generation.driver_for", return_value=driver),
        ):
            created = self.client.post(
                "/api/v1/asset-generation-jobs/",
                {
                    "job_type": "asset",
                    "media_type": "image",
                    "execution_kind": "comfy_workflow",
                    "workflow_template_version_id": str(version.id),
                    "image_category": "reference",
                    "name": "Comfy 图像",
                    "description": "正式工作流素材",
                    "prompt": "一盏雨夜路灯",
                    "workflow_inputs": {
                        "schema_version": 2,
                        "values": {},
                        "assets": [],
                        "references": {
                            "pictures": [],
                            "videos": [],
                            "audios": [],
                        },
                    },
                },
                format="json",
                HTTP_IDEMPOTENCY_KEY=key,
            )
        self.assertEqual(created.status_code, 201, created.data)
        return (
            AssetGenerationJob.objects.get(pk=created.data["id"]),
            version,
            deployment,
            route,
            driver,
        )

    def queued_generation_task(self, template: WorkflowTemplate) -> GenerationTask:
        episode = self.project.episodes.order_by("number").first()
        shot = episode.shots.order_by("number").first()
        prompt = PromptArtifact.objects.create(is_multi_shot=False, include_audio=False, 
            project=self.project,
            episode=episode,
            shot=shot,
            created_by=self.user,
            prompt_type=PromptArtifact.PromptType.IMAGE,
            total_duration_ms=0,
            prompt_text="GPU mutex fixture",
            structured_payload={},
            consistency_snapshot={},
            content_hash="a" * 64,
        )
        workflow = GeneratedWorkflow.objects.create(
            project=self.project,
            episode=episode,
            prompt_artifact=prompt,
            template=template,
            created_by=self.user,
            workflow_json=template.definition or {},
            content_hash="b" * 64,
        )
        return GenerationTask.objects.create(
            owner=self.user,
            project=self.project,
            episode=episode,
            generated_workflow=workflow,
            kind=GenerationTask.Kind.IMAGE,
            max_attempts=3,
        )

    def queued_workflow_smoke(
        self,
        version: WorkflowTemplateVersion,
        deployment: ModelDeployment,
    ) -> WorkflowSmokeAttempt:
        now = timezone.now()
        return WorkflowSmokeAttempt.objects.create(
            workflow_version=version,
            deployment=deployment,
            created_by=self.user,
            graph_sha256=version.graph_sha256,
            execution_contract_sha256=version.execution_contract_sha256,
            validation_evidence_sha256=version.validation_evidence_sha256,
            target_sha256=workflow_execution_target_sha256(version, deployment),
            fixture_sha256="f" * 64,
            deadline_started_at=now,
            hard_deadline_at=now + timedelta(minutes=10),
            idle_deadline_at=now + timedelta(minutes=5),
            last_meaningful_progress_at=now,
        )

    def unresolved_workflow_smoke(
        self,
        version: WorkflowTemplateVersion,
        deployment: ModelDeployment,
    ) -> WorkflowSmokeAttempt:
        attempt = self.queued_workflow_smoke(version, deployment)
        attempt.status = WorkflowSmokeAttempt.Status.UNCERTAIN
        attempt.error_code = "remote_state_uncertain"
        attempt.error_message = "远端仍可能执行。"
        attempt.save(update_fields=(
            "status", "error_code", "error_message", "updated_at",
        ))
        return attempt

    def resolve_workflow_smoke_as_not_submitted(
        self,
        attempt: WorkflowSmokeAttempt,
    ) -> None:
        if not self.user.is_staff:
            self.user.is_staff = True
            self.user.save(update_fields=("is_staff",))
        resolution, created = resolve_workflow_smoke_attempt(
            attempt,
            outcome="confirmed_not_submitted",
            note="管理员已核对远端历史，确认请求未提交。",
            provider_request_id="",
            user=self.user,
        )
        self.assertTrue(created)
        self.assertEqual(resolution.attempt_id, attempt.id)

    def test_prompt_job_is_persistent_and_worker_returns_editable_candidate(self):
        deployment = self.text_deployment()
        metadata = self.client.options("/api/v1/asset-generation-jobs/")
        self.assertEqual(metadata.status_code, 200, metadata.data)
        options = self.client.get(
            "/api/v1/asset-generation-jobs/options/",
            {"job_type": "prompt", "media_type": "image"},
        )
        self.assertEqual(options.status_code, 200, options.data)
        self.assertEqual(options.data["routes"][0]["target_id"], str(deployment.id))

        created = self.client.post(
            "/api/v1/asset-generation-jobs/",
            {
                "job_type": "prompt",
                "media_type": "image",
                "model_deployment_id": str(deployment.id),
                "prompt": "雨夜侦探人物定妆",
                "parameters": {},
            },
            format="json",
            HTTP_IDEMPOTENCY_KEY="prompt-fixture-1",
        )
        self.assertEqual(created.status_code, 201, created.data)
        self.assertEqual(created.data["status"], "queued")
        # A transport replay must return the accepted intent even if mutable
        # deployment readiness changes after the original response was lost.
        deployment.status = ModelDeployment.Status.DISABLED
        deployment.save(update_fields=("status", "updated_at"))
        replayed = self.client.post(
            "/api/v1/asset-generation-jobs/",
            {
                "job_type": "prompt",
                "media_type": "image",
                "model_deployment_id": str(deployment.id),
                "prompt": "雨夜侦探人物定妆",
                "parameters": {},
            },
            format="json",
            HTTP_IDEMPOTENCY_KEY="prompt-fixture-1",
        )
        self.assertEqual(replayed.status_code, 200, replayed.data)
        self.assertEqual(replayed.data["id"], created.data["id"])
        deployment.status = ModelDeployment.Status.ACTIVE
        deployment.save(update_fields=("status", "updated_at"))
        job = claim_next_text_asset_job("test-worker")
        self.assertIsNotNone(job)
        with patch("studio.services.asset_generation.driver_for", return_value=_TextDriver()):
            result = process_text_asset_job(job)
        self.assertEqual(result.status, AssetGenerationJob.Status.SUCCEEDED)
        self.assertIn("林默", result.result_text)
        self.assertIsNone(result.output_asset_id)

    def test_text_submitting_recovery_uses_explicit_grace_seconds(self):
        deployment = self.text_deployment()
        created = self.client.post(
            "/api/v1/asset-generation-jobs/",
            {
                "job_type": "prompt",
                "media_type": "image",
                "model_deployment_id": str(deployment.id),
                "prompt": "等待文本提交确认",
                "parameters": {},
            },
            format="json",
            HTTP_IDEMPOTENCY_KEY="text-submission-grace-fixture",
        )

        self.assertEqual(created.status_code, 201, created.data)
        job = AssetGenerationJob.objects.get(pk=created.data["id"])
        now = timezone.now()
        AssetGenerationJob.objects.filter(pk=job.pk).update(
            status=AssetGenerationJob.Status.SUBMITTING,
            submission_started_at=now - timedelta(seconds=90),
            claim_token=uuid4(),
            worker_id="lost-text-worker",
            claimed_at=now - timedelta(minutes=2),
            heartbeat_at=now - timedelta(minutes=2),
        )

        within_grace = recover_stale_asset_generation_jobs(
            1,
            text=True,
            text_submission_grace_seconds=100,
        )
        self.assertEqual(within_grace, {"released": 0, "uncertain": 0})
        job.refresh_from_db()
        self.assertEqual(job.status, AssetGenerationJob.Status.SUBMITTING)

        expired = recover_stale_asset_generation_jobs(
            1,
            text=True,
            text_submission_grace_seconds=1,
        )
        self.assertEqual(expired, {"released": 0, "uncertain": 1})
        job.refresh_from_db()
        self.assertEqual(job.status, AssetGenerationJob.Status.UNCERTAIN)

    def test_comfy_output_prefix_must_be_explicit_in_the_frozen_snapshot(self):
        with self.assertRaisesMessage(
            ProviderDriverError,
            "缺少冻结的输出前缀",
        ):
            _frozen_output_prefix({
                "payload": {
                    "workflow": {
                        "7": {
                            "class_type": "SaveImage",
                            "inputs": {"filename_prefix": "retired/graph/value"},
                        },
                    },
                },
                "compiled_output_contract": {
                    "primary": {"node_id": "7", "kind": "image"},
                },
            })

    def test_comfy_route_reuses_shared_capability_verifier_and_inactive_denies_staff(self):
        version, deployment = self.comfy_workflow_route()
        target_sha256 = workflow_execution_target_sha256(version, deployment)
        smoke = SimpleNamespace(id=uuid4())

        with (
            patch(
                "studio.services.asset_generation.resolve_workflow_canvas_export_deployment",
                return_value=(version, deployment, "version"),
            ),
            patch(
                "studio.services.asset_generation.verify_published_workflow_capability",
                return_value=(smoke, target_sha256),
            ) as verify_capability,
        ):
            eligible = _eligible_workflow_route(
                version,
                user=self.user,
                media_type=AssetGenerationJob.MediaType.IMAGE,
            )
            self.assertEqual(eligible[2].id, smoke.id)
            verify_capability.assert_called_once_with(
                version,
                deployment,
                operation="image.text_to_image",
            )

            verify_capability.side_effect = ValidationError("shared capability rejected")
            with self.assertRaisesRegex(ValidationError, "shared capability rejected"):
                _eligible_workflow_route(
                    version,
                    user=self.user,
                    media_type=AssetGenerationJob.MediaType.IMAGE,
                )

            version.template.is_active = False
            self.user.is_staff = True
            with self.assertRaisesRegex(ValidationError, "无权执行"):
                _eligible_workflow_route(
                    version,
                    user=self.user,
                    media_type=AssetGenerationJob.MediaType.IMAGE,
                )

    def test_comfy_workflow_route_freezes_graph_and_materializes_declared_output(self):
        job, version, deployment, route, driver = self.create_comfy_image_job(
            key="comfy-route-fixture",
        )
        self.assertEqual(job.execution_kind, "comfy_workflow")
        self.assertEqual(job.workflow_template_version_id, version.id)
        self.assertEqual(job.request_snapshot_schema_version, 3)
        self.assertEqual(
            job.request_snapshot["payload"]["submission_token"],
            str(job.id),
        )
        self.assertEqual(
            job.request_snapshot["compiled_graph_sha256"],
            json_contract(job.request_snapshot["payload"]["workflow"]).sha256,
        )

        output_root = Path(self.temp.name) / "comfy-output"
        output_root.mkdir()
        frozen_prefix = Path(job.request_snapshot["output_prefix"])
        generated_path = output_root / frozen_prefix.parent / (
            f"{frozen_prefix.name}_00001_.png"
        )
        generated_path.parent.mkdir(parents=True)
        Image.new("RGB", (8, 8), color=(20, 40, 60)).save(
            generated_path,
            format="PNG",
        )
        with (
            override_settings(COMFYUI_OUTPUT_DIR=output_root),
            patch(
                "studio.services.asset_generation._eligible_workflow_route",
                return_value=route,
            ),
            patch("studio.services.asset_generation.driver_for", return_value=driver),
        ):
            first = claim_next_comfy_asset_job("comfy-worker")
            submitted = process_comfy_asset_job(first)
            self.assertEqual(submitted.status, AssetGenerationJob.Status.SUBMITTED)
            AssetGenerationJob.objects.filter(pk=job.pk).update(next_poll_at=timezone.now())
            second = claim_next_comfy_asset_job("comfy-worker")
            running = process_comfy_asset_job(second)
            self.assertEqual(running.remote_status, "succeeded")
            AssetGenerationJob.objects.filter(pk=job.pk).update(next_poll_at=timezone.now())
            third = claim_next_comfy_asset_job("comfy-worker")
            result = process_comfy_asset_job(third)
        self.assertEqual(result.status, AssetGenerationJob.Status.SUCCEEDED)
        self.assertIsNotNone(result.output_asset_id)
        asset = Asset.objects.get(pk=result.output_asset_id)
        self.assertEqual(asset.metadata["execution_kind"], "comfy_workflow")
        self.assertEqual(asset.metadata["workflow_template_version_id"], str(version.id))
        self.assertEqual(
            asset.source_info,
            f"ComfyUI 工作流生成 · 素材生成任务 {result.id}",
        )

        rejected_pair = self.client.post(
            "/api/v1/asset-generation-jobs/",
            {
                "job_type": "asset",
                "media_type": "image",
                "execution_kind": "comfy_workflow",
                "workflow_template_version_id": str(version.id),
                "model_deployment_id": str(deployment.id),
                "name": "非法配对",
                "prompt": "不能接受客户端配对",
            },
            format="json",
        )
        self.assertEqual(rejected_pair.status_code, 400, rejected_pair.data)

    def test_comfy_collector_rejects_output_outside_frozen_derived_dimensions(self):
        job, _version, _deployment, route, driver = self.create_comfy_image_job(key="derived-output-mismatch")
        snapshot = deepcopy(job.request_snapshot)
        snapshot["compiled_output_contract"] = {
            **snapshot["compiled_output_contract"], "schema_version": 2,
            "resolution": {
                "adapter": "comfy.flux_kontext_image_scale/v1",
                "source_semantic_key": "asset.image.source", "source_asset_id": "fixture",
                "source_checksum": "a" * 64, "source_width": 600, "source_height": 600,
                "width": 1024, "height": 1024,
            },
        }
        assign_json_contract(job, "request_snapshot", snapshot)
        job.save()
        output_root = Path(self.temp.name) / "comfy-derived-output"
        prefix = Path(snapshot["output_prefix"])
        path = output_root / prefix.parent / f"{prefix.name}_00001_.png"
        path.parent.mkdir(parents=True)
        Image.new("RGB", (8, 8)).save(path)
        with (
            override_settings(COMFYUI_OUTPUT_DIR=output_root),
            patch("studio.services.asset_generation._eligible_workflow_route", return_value=route),
            patch("studio.services.asset_generation.driver_for", return_value=driver),
        ):
            for _ in range(3):
                AssetGenerationJob.objects.filter(pk=job.pk).update(next_poll_at=timezone.now())
                claimed = claim_next_comfy_asset_job("derived-worker")
                self.assertIsNotNone(claimed)
                result = process_comfy_asset_job(claimed)
        self.assertEqual(result.status, AssetGenerationJob.Status.FAILED)
        self.assertIsNone(result.output_asset_id)
        self.assertIn("实际图片尺寸", result.error_message)

    def test_comfy_asset_route_compiles_and_stages_all_declared_input_types(self):
        _fixture_version, deployment = self.comfy_workflow_route()
        provider = deployment.profile_version.provider
        template = WorkflowTemplate.objects.create(
            owner=None,
            name="Asset Comfy multi-input",
            slug=f"asset-comfy-multi-input-{uuid4().hex[:10]}",
            output_kind=WorkflowTemplate.OutputKind.IMAGE,
            model=deployment.profile_version.model_identifier,
            is_system=True,
            is_active=True,
        )
        graph = {
            "1": {"class_type": "LoadImage", "inputs": {"image": "one.png"}},
            "2": {"class_type": "LoadImage", "inputs": {"image": "two.png"}},
            "3": {"class_type": "LoadImage", "inputs": {"image": "three.png"}},
            "4": {"class_type": "EmptyLatentImage", "inputs": {
                "width": 512, "height": 512, "batch_size": 1,
            }},
            "5": {"class_type": "CLIPTextEncode", "inputs": {"text": ""}},
            "6": {"class_type": "KSampler", "inputs": {
                "seed": 0, "cfg": 7.0, "scheduler": "normal", "enabled": False,
            }},
            "7": {"class_type": "SaveImage", "inputs": {
                "images": ["4", 0], "filename_prefix": "asset-output",
            }},
        }
        parameters = {
            **{
                f"asset.image.source{'' if index == 1 else f'_{index}'}": {
                    "type": "asset_image", "required": True,
                    "input_slot": index, "asset_category": "image",
                    "min_items": 1, "max_items": 1,
                }
                for index in range(1, 4)
            },
            "prompt.positive": {"type": "string", "required": True},
            "sampling.seed": {"type": "integer", "required": True, "min": 0},
            "sampling.cfg_scale": {"type": "number", "required": True, "min": 0, "max": 20},
            "sampling.scheduler": {
                "type": "enum", "required": True, "options": ["normal", "karras"],
            },
            "audio.enabled": {"type": "boolean", "required": True},
            "image.width": {"type": "integer", "required": True, "multiple_of": 8},
            "image.height": {"type": "integer", "required": True, "multiple_of": 8},
            "generation.batch_size": {"type": "integer", "required": True, "min": 1, "max": 1},
            "output.prefix": {"type": "string", "required": True},
        }
        bindings = [
            *[
                {
                    "semantic_key": f"asset.image.source{'' if index == 1 else f'_{index}'}",
                    "node_id": str(index), "input_name": "image", "value_type": "asset_image",
                }
                for index in range(1, 4)
            ],
            {"semantic_key": "prompt.positive", "node_id": "5", "input_name": "text", "value_type": "string"},
            {"semantic_key": "sampling.seed", "node_id": "6", "input_name": "seed", "value_type": "integer"},
            {"semantic_key": "sampling.cfg_scale", "node_id": "6", "input_name": "cfg", "value_type": "number"},
            {"semantic_key": "sampling.scheduler", "node_id": "6", "input_name": "scheduler", "value_type": "enum"},
            {"semantic_key": "audio.enabled", "node_id": "6", "input_name": "enabled", "value_type": "boolean"},
            {"semantic_key": "image.width", "node_id": "4", "input_name": "width", "value_type": "integer"},
            {"semantic_key": "image.height", "node_id": "4", "input_name": "height", "value_type": "integer"},
            {"semantic_key": "generation.batch_size", "node_id": "4", "input_name": "batch_size", "value_type": "integer"},
            {"semantic_key": "output.prefix", "node_id": "7", "input_name": "filename_prefix", "value_type": "string"},
        ]
        version = import_workflow_version(
            template=template,
            graph=graph,
            input_contract={
                "schema_version": 3,
                "workflow_mode": "asset-multi-input",
                **workflow_projector_reference(),
                "capability": "image.text_to_image",
                "compiler": {"id": "generic.comfy", "version": 1},
                "resolution_strategy": {"mode": "explicit_width_height", "node_ids": ["4"]},
                "required_dimension_multiple": 8,
                "parameters": parameters,
                "outputs": {"primary": {"node_id": "7", "kind": "image", "required": True}},
            },
            semantic_bindings=bindings,
            model_requirements=[],
            provider=provider,
            model_profile_version=deployment.profile_version,
        )
        source_root = self.media_root / "assets" / str(self.user.id)
        source_root.mkdir(parents=True)
        assets = []
        for index, payload in enumerate((b"image-one", b"image-two", b"image-three"), start=1):
            path = source_root / f"source-{index}.png"
            path.write_bytes(payload)
            assets.append(Asset.objects.create(
                owner=self.user,
                kind=Asset.Kind.REFERENCE,
                name=f"source {index}",
                file=f"assets/{self.user.id}/{path.name}",
                mime_type="image/png",
                checksum=hashlib.sha256(payload).hexdigest(),
            ))
        route = (
            version,
            deployment,
            SimpleNamespace(id=uuid4()),
            workflow_execution_target_sha256(version, deployment),
        )
        driver = _ComfyAssetDriver()
        workflow_inputs = {
            "schema_version": 2,
            "values": {
                "sampling.seed": 42,
                "sampling.cfg_scale": 6.5,
                "sampling.scheduler": "karras",
                "audio.enabled": True,
                "image.width": 512,
                "image.height": 512,
            },
            "assets": [
                {
                    "semantic_key": f"asset.image.source{'' if index == 1 else f'_{index}'}",
                    "asset_id": str(asset.id),
                }
                for index, asset in enumerate(assets, start=1)
            ],
            "references": {"pictures": [], "videos": [], "audios": []},
        }
        with (
            patch("studio.services.asset_generation._eligible_workflow_route", return_value=route),
            patch("studio.services.asset_generation.driver_for", return_value=driver),
        ):
            response = self.client.post(
                "/api/v1/asset-generation-jobs/",
                {
                    "job_type": "asset",
                    "media_type": "image",
                    "execution_kind": "comfy_workflow",
                    "workflow_template_version_id": str(version.id),
                    "image_category": "reference",
                    "name": "多输入素材",
                    "description": "覆盖通用输入合同",
                    "prompt": "雨夜街道",
                    "workflow_inputs": workflow_inputs,
                },
                format="json",
                HTTP_IDEMPOTENCY_KEY="comfy-multi-input-contract",
            )
            self.assertEqual(response.status_code, 201, response.data)
            job = AssetGenerationJob.objects.get(pk=response.data["id"])
            snapshot = job.request_snapshot
            self.assertEqual(snapshot["server_managed_values"], {"generation.batch_size": 1})
            self.assertEqual(len(snapshot["asset_bindings"]), 3)
            self.assertEqual(snapshot["workflow_input_request"], workflow_inputs)

            claimed = claim_next_comfy_asset_job("multi-input-worker")
            submitted = process_comfy_asset_job(claimed)
        self.assertEqual(submitted.status, AssetGenerationJob.Status.SUBMITTED)
        self.assertEqual(len(submitted.staged_inputs), 3)
        for index, asset in enumerate(assets, start=1):
            self.assertEqual(
                driver.submitted_workflow[str(index)]["inputs"]["image"],
                f"autoprompt/{self.user.id}/{job.id}/{asset.id}.png",
            )
        self.assertEqual(driver.submitted_workflow["4"]["inputs"]["batch_size"], 1)
        self.assertEqual(driver.submitted_workflow["6"]["inputs"], {
            "seed": 42, "cfg": 6.5, "scheduler": "karras", "enabled": True,
        })

    def test_stale_comfy_submitting_recovers_same_token_and_collects(self):
        job, _version, _deployment, _route, driver = self.create_comfy_image_job(
            key="comfy-uncertain-recovery",
        )
        stale = timezone.now() - timedelta(minutes=20)
        AssetGenerationJob.objects.filter(pk=job.pk).update(
            status=AssetGenerationJob.Status.SUBMITTING,
            submission_started_at=stale,
            claim_token=uuid4(),
            worker_id="lost-worker",
            claimed_at=stale,
            heartbeat_at=stale,
        )
        recovered = recover_stale_asset_generation_jobs(
            10,
            text=False,
            execution_kind=AssetGenerationJob.ExecutionKind.COMFY_WORKFLOW,
            text_submission_grace_seconds=30,
        )
        self.assertEqual(recovered, {"released": 0, "uncertain": 1})
        job.refresh_from_db()
        self.assertEqual(job.status, AssetGenerationJob.Status.UNCERTAIN)

        driver.output_prefix = job.request_snapshot["output_prefix"]
        with patch("studio.services.asset_generation.driver_for", return_value=driver):
            reconciled = reconcile_uncertain_comfy_asset_jobs(limit=1)
        self.assertEqual(reconciled, {"resumed": 1, "terminal": 0, "unresolved": 0})
        job.refresh_from_db()
        self.assertEqual(job.status, AssetGenerationJob.Status.RUNNING)
        self.assertEqual(job.remote_status, "succeeded")
        self.assertEqual(job.provider_request_id, str(job.id))

        output_root = Path(self.temp.name) / "comfy-recovery-output"
        output_root.mkdir()
        frozen_prefix = Path(job.request_snapshot["output_prefix"])
        generated_path = output_root / frozen_prefix.parent / (
            f"{frozen_prefix.name}_00001_.png"
        )
        generated_path.parent.mkdir(parents=True)
        Image.new("RGB", (8, 8), color=(10, 30, 50)).save(generated_path, format="PNG")
        AssetGenerationJob.objects.filter(pk=job.pk).update(next_poll_at=timezone.now())
        with (
            override_settings(COMFYUI_OUTPUT_DIR=output_root),
            patch("studio.services.asset_generation.driver_for", return_value=driver),
        ):
            claimed = claim_next_comfy_asset_job("recovery-worker")
            result = process_comfy_asset_job(claimed)
        self.assertEqual(result.status, AssetGenerationJob.Status.SUCCEEDED)
        self.assertIsNotNone(result.output_asset_id)

    def test_reference_to_video_comfy_job_is_claimed_and_stale_claim_is_released(self):
        job, _version, _deployment, _route, _driver = self.create_comfy_image_job(
            key="comfy-reference-to-video-queue",
        )
        AssetGenerationJob.objects.filter(pk=job.pk).update(
            media_type=AssetGenerationJob.MediaType.VIDEO,
            operation="video.reference_to_video",
        )

        claimed = claim_next_comfy_asset_job("reference-video-worker")
        self.assertIsNotNone(claimed)
        self.assertEqual(claimed.id, job.id)
        self.assertEqual(claimed.operation, "video.reference_to_video")

        stale = timezone.now() - timedelta(minutes=20)
        AssetGenerationJob.objects.filter(pk=job.pk).update(
            heartbeat_at=stale,
            claimed_at=stale,
        )
        recovered = recover_stale_asset_generation_jobs(
            10,
            text=False,
            execution_kind=AssetGenerationJob.ExecutionKind.COMFY_WORKFLOW,
            text_submission_grace_seconds=30,
        )
        self.assertEqual(recovered, {"released": 1, "uncertain": 0})
        job.refresh_from_db()
        self.assertEqual(job.status, AssetGenerationJob.Status.QUEUED)
        self.assertIsNone(job.claim_token)

    def test_legacy_comfy_job_retry_is_rejected_instead_of_dropping_inputs(self):
        job, _version, _deployment, _route, _driver = self.create_comfy_image_job(
            key="legacy-comfy-retry",
        )
        AssetGenerationJob.objects.filter(pk=job.pk).update(
            status=AssetGenerationJob.Status.FAILED,
            request_snapshot_schema_version=2,
            finished_at=timezone.now(),
        )

        response = self.client.post(
            f"/api/v1/asset-generation-jobs/{job.id}/retry/",
            {},
            format="json",
            HTTP_IDEMPOTENCY_KEY="legacy-comfy-retry-attempt",
        )
        self.assertEqual(response.status_code, 400, response.data)
        self.assertIn("缺少 v3 workflow_inputs 冻结意图", str(response.data))

    def test_tampered_v3_comfy_snapshot_cannot_be_retried(self):
        job, _version, _deployment, _route, _driver = self.create_comfy_image_job(
            key="tampered-v3-comfy-retry",
        )
        tampered = {**job.request_snapshot, "workflow_input_request": {}}
        AssetGenerationJob.objects.filter(pk=job.pk).update(
            status=AssetGenerationJob.Status.FAILED,
            request_snapshot=tampered,
            finished_at=timezone.now(),
        )
        before = AssetGenerationJob.objects.count()

        response = self.client.post(
            f"/api/v1/asset-generation-jobs/{job.id}/retry/",
            {},
            format="json",
            HTTP_IDEMPOTENCY_KEY="tampered-v3-comfy-retry-attempt",
        )
        self.assertEqual(response.status_code, 400, response.data)
        self.assertEqual(AssetGenerationJob.objects.count(), before)

    def test_staff_resolution_resumes_comfy_collect_without_async_terminal_config(self):
        job, _version, _deployment, _route, _driver = self.create_comfy_image_job(
            key="comfy-staff-resolution",
        )
        AssetGenerationJob.objects.filter(pk=job.pk).update(
            status=AssetGenerationJob.Status.UNCERTAIN,
            error_code="uncertain_remote_response",
            finished_at=timezone.now(),
        )
        self.user.is_staff = True
        self.user.save(update_fields=("is_staff",))
        resolution, created = resolve_uncertain_asset_generation_job(
            job,
            outcome=AssetGenerationJobResolution.Outcome.CONFIRMED_REMOTE_SUCCEEDED,
            note="管理员已核对 ComfyUI 历史记录确认成功。",
            user=self.user,
            client_idempotency_key="comfy-resolution-1",
        )
        self.assertTrue(created)
        self.assertEqual(
            resolution.provider_request_id_sha256,
            hashlib.sha256(str(job.id).encode("utf-8")).hexdigest(),
        )
        job.refresh_from_db()
        self.assertEqual(job.status, AssetGenerationJob.Status.RUNNING)
        self.assertEqual(job.remote_status, "succeeded")
        self.assertEqual(job.provider_request_id, str(job.id))
        self.assertEqual(job.next_poll_at is not None, True)

    def test_claimed_comfy_asset_blocks_generation_task_and_workflow_smoke(self):
        _job, version, deployment, _route, _driver = self.create_comfy_image_job(
            key="comfy-gpu-mutex",
        )
        self.queued_generation_task(version.template)

        claimed = claim_next_comfy_asset_job("asset-gpu-owner")
        self.assertIsNotNone(claimed)
        self.queued_workflow_smoke(version, deployment)
        self.assertIsNone(claim_next_workflow_smoke("smoke-gpu-owner"))
        self.assertIsNone(claim_next_task())

    def test_running_generation_task_blocks_comfy_asset_and_workflow_smoke(self):
        _job, version, deployment, _route, _driver = self.create_comfy_image_job(
            key="task-gpu-mutex",
        )
        task = self.queued_generation_task(version.template)
        GenerationTask.objects.filter(pk=task.pk).update(
            status=GenerationTask.Status.RUNNING,
        )
        self.queued_workflow_smoke(version, deployment)

        self.assertIsNone(claim_next_comfy_asset_job("asset-worker"))
        self.assertIsNone(claim_next_workflow_smoke("smoke-worker"))

    def test_claimed_workflow_smoke_blocks_comfy_asset_and_generation_task(self):
        _job, version, deployment, _route, _driver = self.create_comfy_image_job(
            key="smoke-gpu-mutex",
        )
        self.queued_generation_task(version.template)
        self.queued_workflow_smoke(version, deployment)

        smoke = claim_next_workflow_smoke("smoke-gpu-owner")
        self.assertIsNotNone(smoke)
        self.assertIsNone(claim_next_comfy_asset_job("asset-worker"))
        self.assertIsNone(claim_next_task())

    def test_unresolved_workflow_smoke_blocks_comfy_asset_until_resolved(self):
        job, version, deployment, _route, _driver = self.create_comfy_image_job(
            key="uncertain-smoke-asset-gpu-mutex",
        )
        unresolved = self.unresolved_workflow_smoke(version, deployment)

        self.assertIsNone(claim_next_comfy_asset_job("asset-worker"))

        self.resolve_workflow_smoke_as_not_submitted(unresolved)
        claimed = claim_next_comfy_asset_job("asset-worker")
        self.assertIsNotNone(claimed)
        self.assertEqual(claimed.id, job.id)

    def test_unresolved_workflow_smoke_blocks_generation_task_until_resolved(self):
        version, deployment = self.comfy_workflow_route()
        task = self.queued_generation_task(version.template)
        unresolved = self.unresolved_workflow_smoke(version, deployment)

        self.assertIsNone(claim_next_task())

        self.resolve_workflow_smoke_as_not_submitted(unresolved)
        # This test owns only the cross-queue GPU gate. The deliberately
        # minimal GenerationTask fixture predates the immutable production
        # execution-capacity/monitoring contracts, so isolate those orthogonal
        # claim-time validators after the smoke ambiguity has been resolved.
        with patch(
            "studio.services.task_queue._task_execution_monitoring",
            return_value={
                "schema_version": 1,
                "default_idle_timeout_seconds": None,
                "node_type_overrides": {},
                "node_overrides": {},
            },
        ), patch(
            "studio.services.task_queue._execution_capacity",
            return_value={
                "effective_concurrency": 1,
                "effective_task_timeout_seconds": 600.0,
                "task_idle_timeout_seconds": 300.0,
            },
        ):
            claimed = claim_next_task()
        self.assertIsNotNone(claimed)
        self.assertEqual(claimed.id, task.id)

    def test_unresolved_workflow_smoke_blocks_other_smoke_until_resolved(self):
        unresolved_version, unresolved_deployment = self.comfy_workflow_route()
        unresolved = self.unresolved_workflow_smoke(
            unresolved_version,
            unresolved_deployment,
        )
        queued_version, queued_deployment = self.comfy_workflow_route()
        queued = self.queued_workflow_smoke(queued_version, queued_deployment)

        self.assertIsNone(claim_next_workflow_smoke("smoke-worker"))

        self.resolve_workflow_smoke_as_not_submitted(unresolved)
        claimed = claim_next_workflow_smoke("smoke-worker")
        self.assertIsNotNone(claimed)
        self.assertEqual(claimed.id, queued.id)

    def test_script_generation_materializes_downloadable_reusable_asset(self):
        deployment = self.text_deployment()
        created = self.client.post(
            "/api/v1/asset-generation-jobs/",
            {
                "job_type": "asset",
                "media_type": "script",
                "model_deployment_id": str(deployment.id),
                "name": "模型剧本",
                "description": "人工测试用生成剧本",
                "prompt": "生成一集雨夜悬疑短剧",
                "parameters": {},
            },
            format="json",
        )
        self.assertEqual(created.status_code, 201, created.data)
        job = claim_next_text_asset_job("test-worker")
        with patch("studio.services.asset_generation.driver_for", return_value=_TextDriver()):
            result = process_text_asset_job(job)
        self.assertEqual(result.status, AssetGenerationJob.Status.SUCCEEDED)
        self.assertIsNotNone(result.output_asset_id)
        asset = Asset.objects.get(pk=result.output_asset_id)
        self.assertEqual(asset.kind, Asset.Kind.SCRIPT)
        self.assertEqual(asset.metadata["generation_job_id"], str(result.id))
        self.assertEqual(
            asset.source_info,
            f"模型部署生成 · 素材生成任务 {result.id}",
        )
        detail = self.client.get(f"/api/v1/assets/{asset.id}/")
        self.assertEqual(detail.status_code, 200, detail.data)
        self.assertTrue(detail.data["download_url"].endswith("?download=1"))
        source_change = self.client.patch(
            f"/api/v1/assets/{asset.id}/",
            {"source_info": "伪造来源"},
            format="json",
        )
        self.assertEqual(source_change.status_code, 400, source_change.data)
        self.assertIn("客户端不能提交或改写", str(source_change.data))
        asset.refresh_from_db()
        self.assertEqual(
            asset.source_info,
            f"模型部署生成 · 素材生成任务 {result.id}",
        )
        changed = self.client.patch(
            f"/api/v1/assets/{asset.id}/",
            {"prompt_hint": "试图覆盖冻结提示词"},
            format="json",
        )
        self.assertEqual(changed.status_code, 400, changed.data)
        preview_url = detail.data["preview_url"]
        Path(asset.file.path).write_bytes(b"tampered generated script")
        tampered = self.client.get(preview_url)
        self.assertEqual(tampered.status_code, 404)
        rejected_import = self.client.post(
            "/api/v1/script-sources/",
            {"project_id": str(self.project.id), "asset_id": str(asset.id)},
            format="json",
        )
        self.assertEqual(rejected_import.status_code, 422, rejected_import.data)
        self.assertEqual(
            rejected_import.data["error"]["code"],
            "script_asset_integrity_mismatch",
        )
        deleted = self.client.delete(f"/api/v1/assets/{asset.id}/")
        self.assertEqual(deleted.status_code, 400, deleted.data)
        self.assertTrue(Asset.objects.filter(pk=asset.id).exists())

    def test_audio_generation_option_uses_formal_audio_operation(self):
        suffix = uuid4().hex[:10]
        provider = ProviderProfile(
            key=f"asset-audio-provider-{suffix}",
            name="Asset audio fixture",
            driver_type=ProviderProfile.DriverType.ASYNC_REST,
            modalities="audio",
            operations="audio.text_to_audio",
            is_active=True,
        )
        assign_json_contract(provider, "driver_config", {
            "base_url": "http://127.0.0.1:19002",
            "health_path": "/health",
            "submit_path": "/jobs",
            "poll_path": "/jobs/{request_id}",
            "cancel_path": "/jobs/{request_id}",
            "collect_path": "/jobs/{request_id}",
            "request_id_field": "id",
            "status_field": "status",
            "artifacts_field": "artifacts",
            "artifact_url_field": "url",
            "artifact_kind_field": "kind",
            "artifact_id_field": "id",
            "artifact_sha256_field": "sha256",
            "actual_cost_field": "actual_cost",
            "currency_field": "currency",
            "idempotency_header": "Idempotency-Key",
            "terminal_success": ["succeeded"],
            "terminal_failure": ["failed"],
            "terminal_cancelled": ["cancelled"],
            "request_field_map": {"model": "model", "prompt": "prompt", "duration_seconds": "duration"},
            "artifact_allowed_hosts": ["127.0.0.1"],
            "pricing": {"currency": "CNY", "fixed_per_request": "0.020000"},
            "timeout_seconds": 30,
            "poll_interval_seconds": 0.25,
            "max_poll_seconds": 10,
            "capability_ttl_seconds": 3_600,
        })
        provider.save()
        profile = ModelProfileVersion(
            profile_key=f"asset-audio-{suffix}", version=1, provider=provider,
            model_identifier="fixture-audio-model", modality=ModelProfileVersion.Modality.AUDIO,
            status=ModelProfileVersion.Status.PUBLISHED, published_at=timezone.now(),
        )
        operation = {
            "output_kind": "audio",
            "request_schema": {
                "type": "object", "additionalProperties": False,
                "required": ["prompt", "duration_seconds"],
                "properties": {
                    "prompt": {"type": "string", "minLength": 1, "maxLength": 60000},
                    "duration_seconds": {"type": "number", "minimum": 2, "maximum": 15},
                },
            },
            "smoke_inputs": {"prompt": "雨声", "duration_seconds": 2},
        }
        assign_json_contract(profile, "specification", {
            "schema_version": 1,
            "capabilities": ["audio.text_to_audio"],
            "async_media": {"schema_version": 1, "operations": {"audio.text_to_audio": operation}},
        })
        profile.save()
        deployment = ModelDeployment(
            key=f"asset-audio-deployment-{suffix}", name="Asset audio deployment",
            profile_version=profile, status=ModelDeployment.Status.ACTIVE,
        )
        assign_json_contract(deployment, "runtime_config", {})
        deployment.save()
        record_deployment_capability_snapshot(
            deployment,
            probe_kind=ModelDeploymentCapabilitySnapshot.ProbeKind.SMOKE,
            status=ModelDeploymentCapabilitySnapshot.Status.PASSED,
            operations=["audio.text_to_audio"],
            evidence={"schema_version": 1, "execution_submitted": True, "fixture": True},
        )
        response = self.client.get(
            "/api/v1/asset-generation-jobs/options/",
            {"job_type": "asset", "media_type": "audio"},
        )
        self.assertEqual(response.status_code, 200, response.data)
        self.assertEqual(response.data["routes"][0]["operation"], "audio.text_to_audio")
        self.assertEqual(response.data["routes"][0]["parameter_defaults"], {"duration_seconds": 2})

    def test_cancel_replay_preserves_first_deadline_and_never_invokes_text_model(self):
        deployment = self.text_deployment()
        created = self.client.post(
            "/api/v1/asset-generation-jobs/",
            {
                "job_type": "prompt",
                "media_type": "image",
                "model_deployment_id": str(deployment.id),
                "prompt": "取消这个提示词任务",
            },
            format="json",
            HTTP_IDEMPOTENCY_KEY="cancel-replay-fixture",
        )
        self.assertEqual(created.status_code, 201, created.data)
        claimed = claim_next_text_asset_job("cancel-worker")
        first = self.client.post(
            f"/api/v1/asset-generation-jobs/{claimed.id}/cancel/",
            {},
            format="json",
        )
        self.assertEqual(first.status_code, 200, first.data)
        first_deadline = AssetGenerationJob.objects.get(pk=claimed.id).cancel_requested_at
        replay = self.client.post(
            f"/api/v1/asset-generation-jobs/{claimed.id}/cancel/",
            {},
            format="json",
        )
        self.assertEqual(replay.status_code, 200, replay.data)
        self.assertEqual(
            AssetGenerationJob.objects.get(pk=claimed.id).cancel_requested_at,
            first_deadline,
        )
        with patch(
            "studio.services.asset_generation.driver_for",
            side_effect=AssertionError("cancelled task must not invoke a model"),
        ):
            result = process_text_asset_job(claimed)
        self.assertEqual(result.status, AssetGenerationJob.Status.CANCELLED)

    def test_audio_worker_polls_collects_and_materializes_reference_asset(self):
        deployment = self.audio_deployment()
        created = self.client.post(
            "/api/v1/asset-generation-jobs/",
            {
                "job_type": "asset",
                "media_type": "audio",
                "model_deployment_id": str(deployment.id),
                "name": "模型雨声",
                "description": "两秒雨声音效",
                "prompt": "生成两秒雨声",
                "parameters": {"duration_seconds": 2},
            },
            format="json",
            HTTP_IDEMPOTENCY_KEY="audio-worker-fixture",
        )
        self.assertEqual(created.status_code, 201, created.data)
        driver = _MediaDriver()
        with patch("studio.services.asset_generation.driver_for", return_value=driver):
            submitted = process_media_asset_job(claim_next_media_asset_job("media-worker"))
            self.assertEqual(submitted.status, AssetGenerationJob.Status.SUBMITTED)
            running = process_media_asset_job(claim_next_media_asset_job("media-worker"))
            self.assertEqual(running.status, AssetGenerationJob.Status.RUNNING)
            AssetGenerationJob.objects.filter(pk=running.pk).update(
                next_poll_at=timezone.now(),
            )
            payload = b"RIFF-fixture-audio"
            facts = {
                "size_bytes": len(payload),
                "sha256": hashlib.sha256(payload).hexdigest(),
                "mime_type": "audio/wav",
                "observed_media": {"duration_ms": 2000},
            }

            def download_fixture(*, target, **kwargs):
                target.parent.mkdir(parents=True, exist_ok=True)
                target.write_bytes(payload)
                return facts

            with patch(
                "studio.services.asset_generation.download_remote_artifact",
                side_effect=download_fixture,
            ), patch(
                "studio.services.asset_generation.verified_local_media_facts",
                return_value=facts,
            ):
                result = process_media_asset_job(claim_next_media_asset_job("media-worker"))
        self.assertEqual(result.status, AssetGenerationJob.Status.SUCCEEDED)
        asset = Asset.objects.get(pk=result.output_asset_id)
        self.assertEqual(asset.kind, Asset.Kind.REFERENCE_AUDIO)
        self.assertEqual(asset.checksum, facts["sha256"])
        self.assertEqual(asset.metadata["generation_job_id"], str(result.id))
        self.assertEqual(
            asset.source_info,
            f"模型部署生成 · 素材生成任务 {result.id}",
        )

    def test_uncertain_job_has_staff_audit_resolution_and_idempotent_retry(self):
        deployment = self.text_deployment()
        created = self.client.post(
            "/api/v1/asset-generation-jobs/",
            {
                "job_type": "prompt",
                "media_type": "image",
                "model_deployment_id": str(deployment.id),
                "prompt": "需要人工核验的提示词任务",
            },
            format="json",
            HTTP_IDEMPOTENCY_KEY="uncertain-resolution-fixture",
        )
        self.assertEqual(created.status_code, 201, created.data)
        job = AssetGenerationJob.objects.get(pk=created.data["id"])
        job.status = AssetGenerationJob.Status.UNCERTAIN
        job.error_code = "uncertain_remote_response"
        job.error_message = "fixture"
        job.submission_started_at = timezone.now()
        job.finished_at = timezone.now()
        job.save(update_fields=(
            "status", "error_code", "error_message", "submission_started_at",
            "finished_at", "updated_at",
        ))
        denied = self.client.post(
            f"/api/v1/asset-generation-jobs/{job.id}/resolve-uncertain/",
            {
                "outcome": "confirmed_not_submitted",
                "note": "已核对供应商控制台，确认没有创建远端请求。",
            },
            format="json",
        )
        self.assertEqual(denied.status_code, 403, denied.data)
        self.user.is_staff = True
        self.user.save(update_fields=("is_staff",))
        resolved = self.client.post(
            f"/api/v1/asset-generation-jobs/{job.id}/resolve-uncertain/",
            {
                "outcome": "confirmed_not_submitted",
                "note": "已核对供应商控制台，确认没有创建远端请求。",
            },
            format="json",
            HTTP_IDEMPOTENCY_KEY="uncertain-resolution-decision",
        )
        self.assertEqual(resolved.status_code, 201, resolved.data)
        self.assertEqual(resolved.data["job_status"], AssetGenerationJob.Status.FAILED)
        self.assertEqual(len(resolved.data["evidence_sha256"]), 64)
        replayed_resolution = self.client.post(
            f"/api/v1/asset-generation-jobs/{job.id}/resolve-uncertain/",
            {
                "outcome": "confirmed_not_submitted",
                "note": "已核对供应商控制台，确认没有创建远端请求。",
            },
            format="json",
            HTTP_IDEMPOTENCY_KEY="uncertain-resolution-decision",
        )
        self.assertEqual(replayed_resolution.status_code, 200, replayed_resolution.data)
        self.assertEqual(replayed_resolution.data["id"], resolved.data["id"])
        with self.assertRaises(ValidationError):
            AssetGenerationJobResolution.objects.filter(
                pk=resolved.data["id"],
            ).update(note="试图改写不可变消歧证据")
        with self.assertRaises(ValidationError):
            AssetGenerationJobResolution.objects.filter(
                pk=resolved.data["id"],
            ).delete()
        detail = self.client.get(f"/api/v1/asset-generation-jobs/{job.id}/")
        self.assertEqual(detail.status_code, 200, detail.data)
        self.assertEqual(detail.data["latest_resolution"]["id"], resolved.data["id"])
        first_retry = self.client.post(
            f"/api/v1/asset-generation-jobs/{job.id}/retry/",
            {},
            format="json",
            HTTP_IDEMPOTENCY_KEY="resolved-retry-fixture",
        )
        replayed_retry = self.client.post(
            f"/api/v1/asset-generation-jobs/{job.id}/retry/",
            {},
            format="json",
            HTTP_IDEMPOTENCY_KEY="resolved-retry-fixture",
        )
        self.assertEqual(first_retry.status_code, 201, first_retry.data)
        self.assertEqual(replayed_retry.status_code, 200, replayed_retry.data)
        self.assertEqual(first_retry.data["id"], replayed_retry.data["id"])

    def test_cancel_preserves_explicit_remote_failure_instead_of_clean_cancel(self):
        deployment = self.audio_deployment()
        created = self.client.post(
            "/api/v1/asset-generation-jobs/",
            {
                "job_type": "asset",
                "media_type": "audio",
                "model_deployment_id": str(deployment.id),
                "name": "取消失败事实",
                "prompt": "生成两秒雨声",
                "parameters": {"duration_seconds": 2},
            },
            format="json",
            HTTP_IDEMPOTENCY_KEY="cancel-remote-failure-fixture",
        )
        self.assertEqual(created.status_code, 201, created.data)
        driver = _CancelFailureDriver()
        with patch("studio.services.asset_generation.driver_for", return_value=driver):
            submitted = process_media_asset_job(claim_next_media_asset_job("media-worker"))
            self.assertEqual(submitted.status, AssetGenerationJob.Status.SUBMITTED)
            cancelled = self.client.post(
                f"/api/v1/asset-generation-jobs/{submitted.id}/cancel/",
                {},
                format="json",
            )
            self.assertEqual(cancelled.status_code, 200, cancelled.data)
            result = process_media_asset_job(claim_next_media_asset_job("media-worker"))
        self.assertEqual(result.status, AssetGenerationJob.Status.FAILED)
        self.assertEqual(result.error_code, "remote_generation_failed_during_cancel")
        self.assertEqual(result.remote_status, "failed")

    def test_cancelled_replay_is_idempotent_before_worker_claim(self):
        deployment = self.audio_deployment()
        created = self.client.post(
            "/api/v1/asset-generation-jobs/",
            {
                "job_type": "asset",
                "media_type": "audio",
                "model_deployment_id": str(deployment.id),
                "name": "排队取消重放",
                "prompt": "生成两秒雨声",
                "parameters": {"duration_seconds": 2},
            },
            format="json",
            HTTP_IDEMPOTENCY_KEY="queued-cancel-replay-fixture",
        )
        self.assertEqual(created.status_code, 201, created.data)
        endpoint = f"/api/v1/asset-generation-jobs/{created.data['id']}/cancel/"
        first = self.client.post(endpoint, {}, format="json")
        replayed = self.client.post(endpoint, {}, format="json")
        self.assertEqual(first.status_code, 200, first.data)
        self.assertEqual(replayed.status_code, 200, replayed.data)
        self.assertEqual(first.data["status"], AssetGenerationJob.Status.CANCELLED)
        self.assertTrue(replayed.data["ignore_late_result"])

    def test_remote_success_after_cancel_collects_cost_but_never_materializes(self):
        deployment = self.audio_deployment()
        created = self.client.post(
            "/api/v1/asset-generation-jobs/",
            {
                "job_type": "asset",
                "media_type": "audio",
                "model_deployment_id": str(deployment.id),
                "name": "取消后远端成功",
                "prompt": "生成两秒雨声",
                "parameters": {"duration_seconds": 2},
            },
            format="json",
            HTTP_IDEMPOTENCY_KEY="cancel-late-success-fixture",
        )
        self.assertEqual(created.status_code, 201, created.data)
        driver = _CancelSuccessDriver()
        with patch("studio.services.asset_generation.driver_for", return_value=driver):
            submitted = process_media_asset_job(claim_next_media_asset_job("media-worker"))
            self.client.post(
                f"/api/v1/asset-generation-jobs/{submitted.id}/cancel/",
                {},
                format="json",
            )
            awaiting_cost = process_media_asset_job(
                claim_next_media_asset_job("media-worker"),
            )
            self.assertEqual(
                awaiting_cost.status,
                AssetGenerationJob.Status.CANCEL_REQUESTED,
            )
            AssetGenerationJob.objects.filter(pk=awaiting_cost.pk).update(
                next_poll_at=timezone.now(),
            )
            result = process_media_asset_job(claim_next_media_asset_job("media-worker"))
        self.assertEqual(result.status, AssetGenerationJob.Status.CANCELLED)
        self.assertEqual(result.error_code, "late_result_ignored")
        self.assertEqual(result.actual_cost, Decimal("0.020000"))
        self.assertIsNone(result.output_asset_id)

    def test_uncertain_cancel_intent_cannot_be_resolved_into_a_visible_asset(self):
        deployment = self.audio_deployment()
        created = self.client.post(
            "/api/v1/asset-generation-jobs/",
            {
                "job_type": "asset",
                "media_type": "audio",
                "model_deployment_id": str(deployment.id),
                "name": "取消意图消歧",
                "prompt": "生成两秒雨声",
                "parameters": {"duration_seconds": 2},
            },
            format="json",
            HTTP_IDEMPOTENCY_KEY="cancel-resolution-fixture",
        )
        job = AssetGenerationJob.objects.get(pk=created.data["id"])
        now = timezone.now()
        job.status = AssetGenerationJob.Status.UNCERTAIN
        job.provider_request_id = "audio-request-1"
        job.provider_request_id_sha256 = hashlib.sha256(b"audio-request-1").hexdigest()
        job.submission_started_at = now
        job.submitted_at = now
        job.cancel_requested_at = now
        job.ignore_late_result = True
        job.finished_at = now
        job.save(update_fields=(
            "status", "provider_request_id", "provider_request_id_sha256",
            "submission_started_at", "submitted_at", "cancel_requested_at",
            "ignore_late_result", "finished_at", "updated_at",
        ))
        self.user.is_staff = True
        self.user.save(update_fields=("is_staff",))
        resolved = self.client.post(
            f"/api/v1/asset-generation-jobs/{job.id}/resolve-uncertain/",
            {
                "outcome": "confirmed_remote_succeeded",
                "provider_request_id": "audio-request-1",
                "actual_cost": "0.020000",
                "cost_currency": "CNY",
                "note": "已核对远端成功及账单，但用户取消意图必须继续生效。",
            },
            format="json",
            HTTP_IDEMPOTENCY_KEY="cancel-resolution-decision",
        )
        self.assertEqual(resolved.status_code, 201, resolved.data)
        self.assertEqual(
            resolved.data["job_status"],
            AssetGenerationJob.Status.CANCELLED,
        )
        result = AssetGenerationJob.objects.get(pk=job.pk)
        self.assertEqual(result.status, AssetGenerationJob.Status.CANCELLED)
        self.assertEqual(result.error_code, "late_result_ignored")
        self.assertEqual(result.actual_cost, Decimal("0.020000"))
        self.assertIsNone(result.output_asset_id)

    def test_overlong_provider_request_id_is_hashed_and_never_truncated(self):
        deployment = self.audio_deployment()
        created = self.client.post(
            "/api/v1/asset-generation-jobs/",
            {
                "job_type": "asset",
                "media_type": "audio",
                "model_deployment_id": str(deployment.id),
                "name": "超长远端身份",
                "prompt": "生成两秒雨声",
                "parameters": {"duration_seconds": 2},
            },
            format="json",
            HTTP_IDEMPOTENCY_KEY="long-provider-request-id-fixture",
        )
        self.assertEqual(created.status_code, 201, created.data)
        driver = _LongRequestIdMediaDriver()
        with patch("studio.services.asset_generation.driver_for", return_value=driver):
            result = process_media_asset_job(claim_next_media_asset_job("media-worker"))
        self.assertEqual(result.status, AssetGenerationJob.Status.UNCERTAIN)
        self.assertEqual(result.provider_request_id, "")
        self.assertEqual(
            result.provider_request_id_sha256,
            hashlib.sha256(driver.request_id.encode("utf-8")).hexdigest(),
        )
        self.user.is_staff = True
        self.user.save(update_fields=("is_staff",))
        resolved = self.client.post(
            f"/api/v1/asset-generation-jobs/{result.id}/resolve-uncertain/",
            {
                "outcome": "confirmed_remote_failed",
                "note": "已按完整 request id 摘要核对，远端明确失败。",
            },
            format="json",
        )
        self.assertEqual(resolved.status_code, 201, resolved.data)
        refreshed = AssetGenerationJob.objects.get(pk=result.pk)
        self.assertEqual(refreshed.status, AssetGenerationJob.Status.FAILED)
        self.assertEqual(refreshed.remote_status, "operator_confirmed_remote_failed")

    def test_collect_cost_cannot_overwrite_staff_attested_resolution_cost(self):
        deployment = self.audio_deployment()
        created = self.client.post(
            "/api/v1/asset-generation-jobs/",
            {
                "job_type": "asset",
                "media_type": "audio",
                "model_deployment_id": str(deployment.id),
                "name": "费用对账冲突",
                "prompt": "生成两秒雨声",
                "parameters": {"duration_seconds": 2},
            },
            format="json",
            HTTP_IDEMPOTENCY_KEY="cost-resolution-fixture",
        )
        self.assertEqual(created.status_code, 201, created.data)
        job = AssetGenerationJob.objects.get(pk=created.data["id"])
        job.status = AssetGenerationJob.Status.UNCERTAIN
        job.provider_request_id = "audio-request-1"
        job.submission_started_at = timezone.now()
        job.submitted_at = timezone.now()
        job.finished_at = timezone.now()
        job.save(update_fields=(
            "status", "provider_request_id", "submission_started_at", "submitted_at",
            "finished_at", "updated_at",
        ))
        self.user.is_staff = True
        self.user.save(update_fields=("is_staff",))
        resolved = self.client.post(
            f"/api/v1/asset-generation-jobs/{job.id}/resolve-uncertain/",
            {
                "outcome": "confirmed_remote_succeeded",
                "provider_request_id": "audio-request-1",
                "actual_cost": "0.010000",
                "cost_currency": "CNY",
                "note": "已核对供应商账单，实际扣费为零点零一元。",
            },
            format="json",
        )
        self.assertEqual(resolved.status_code, 201, resolved.data)
        with patch("studio.services.asset_generation.driver_for", return_value=_MediaDriver()):
            result = process_media_asset_job(claim_next_media_asset_job("media-worker"))
        self.assertEqual(result.status, AssetGenerationJob.Status.UNCERTAIN)
        self.assertEqual(result.error_code, "actual_cost_reconciliation_required")
        self.assertEqual(result.actual_cost, Decimal("0.010000"))
        carried = self.client.post(
            f"/api/v1/asset-generation-jobs/{job.id}/resolve-uncertain/",
            {
                "outcome": "confirmed_remote_succeeded",
                "provider_request_id": "audio-request-1",
                "note": "再次核对远端成功事实，沿用上一轮已确认费用。",
            },
            format="json",
            HTTP_IDEMPOTENCY_KEY="cost-resolution-carried-forward",
        )
        self.assertEqual(carried.status_code, 201, carried.data)
        self.assertEqual(
            carried.data["evidence"]["actual_cost_source"],
            "carried_forward",
        )
        with patch("studio.services.asset_generation.driver_for", return_value=_MediaDriver()):
            second_result = process_media_asset_job(
                claim_next_media_asset_job("media-worker"),
            )
        self.assertEqual(second_result.status, AssetGenerationJob.Status.UNCERTAIN)
        self.assertEqual(
            second_result.error_code,
            "actual_cost_reconciliation_required",
        )
        self.assertEqual(second_result.actual_cost, Decimal("0.010000"))
