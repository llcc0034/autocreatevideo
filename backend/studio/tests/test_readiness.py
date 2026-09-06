from datetime import timedelta
import os
from pathlib import Path
import tempfile
from types import SimpleNamespace
from unittest.mock import Mock, patch

from django.core.exceptions import ValidationError as DjangoValidationError
from django.db import OperationalError
from django.contrib.auth import get_user_model
from django.test import TestCase, override_settings
from django.utils import timezone
from rest_framework.test import APIClient

import studio.services.configuration as configuration_service
import studio.services.readiness as readiness_service
from studio.models import (
    ConfigRevision,
    ModelDeployment,
    ModelProfileVersion,
    PromptFieldDefinition,
    PromptProfileBinding,
    PromptProfileFieldRule,
    PromptProfileVersion,
    ProductionRecipeVersion,
    ProviderProfile,
    RuntimeHost,
    WorkerLease,
    WorkflowTemplate,
    WorkflowTemplateVersion,
)
from studio.services.configuration import create_config_revision, publish_config_revision
from studio.services.json_contracts import assign_json_contract
from studio.services.readiness import (
    _capability_readiness_for_requirements,
    _media_capability_readiness,
    _requirements_from_stages,
    _workflow_catalog_check,
    collect_production_readiness,
    collect_readiness,
)
from studio.services.security_boundaries import boundary_int
from studio.services.prompt_profiles import refresh_prompt_profile_contract
from studio.services.worker_heartbeat import touch_worker_heartbeat
from studio.tests.secret_fixtures import TEST_SECRET_ENV
from studio.tests.factories import (
    H3_REF2VA_OFFICIAL_RULE_SPECS,
    conditioning_contract,
)


User = get_user_model()


class ReadinessTests(TestCase):
    def setUp(self):
        self.temp_dir = tempfile.TemporaryDirectory()
        self.addCleanup(self.temp_dir.cleanup)
        WorkerLease.objects.create(key="generation-worker-heartbeat")
        WorkerLease.objects.create(key="assembly-worker-heartbeat")
        self.runtime_settings = override_settings(
            MEDIA_ROOT=Path(self.temp_dir.name),
            COMFYUI_OUTPUT_DIR=self.temp_dir.name,
            FFMPEG_BINARY="ffmpeg-test",
            FFPROBE_BINARY="ffprobe-test",
        )
        self.runtime_settings.enable()
        self.addCleanup(self.runtime_settings.disable)
        self.keyring = patch.dict(os.environ, TEST_SECRET_ENV, clear=False)
        self.keyring.start()
        self.addCleanup(self.keyring.stop)
        self.superuser = User.objects.create_superuser(
            username="readiness-superadmin",
            email="readiness@example.test",
            password="ReadinessPass123!",
        )

    def _set_worker_requirements(self, **overrides):
        values = {
            "runtime.readiness_generation_worker_required": True,
            "runtime.readiness_text_generation_worker_required": True,
            "runtime.readiness_assembly_worker_required": True,
        }
        values.update({
            f"runtime.readiness_{key}_worker_required": value
            for key, value in overrides.items()
        })
        revision = create_config_revision(
            scope_type=ConfigRevision.ScopeType.SYSTEM,
            values=values,
            created_by=self.superuser,
        )
        publish_config_revision(revision, published_by=self.superuser)

    def _published_local_workflow(self):
        provider = ProviderProfile(
            key="readiness-comfyui",
            name="Readiness ComfyUI",
            driver_type=ProviderProfile.DriverType.COMFYUI,
            modalities="image",
            operations="health,object_info,submit,poll,cancel,collect",
        )
        assign_json_contract(provider, "driver_config", {"base_url": "http://127.0.0.1:8188"})
        provider.save()

        profile = ModelProfileVersion(
            profile_key="readiness-image",
            version=1,
            provider=provider,
            model_identifier="readiness-image",
            modality=ModelProfileVersion.Modality.IMAGE,
            status=ModelProfileVersion.Status.PUBLISHED,
        )
        assign_json_contract(profile, "specification", {"capabilities": ["image.text_to_image"]})
        profile.save()

        host = RuntimeHost.objects.create(key="readiness-host", name="Readiness host")
        deployment = ModelDeployment(
            key="readiness-deployment",
            name="Readiness deployment",
            profile_version=profile,
            runtime_host=host,
            status=ModelDeployment.Status.ACTIVE,
        )
        assign_json_contract(deployment, "runtime_config", {"base_url": "http://127.0.0.1:8188"})
        deployment.save()

        template = WorkflowTemplate.objects.create(
            name="Readiness workflow",
            slug="readiness-workflow",
            output_kind=WorkflowTemplate.OutputKind.IMAGE,
            is_system=True,
        )
        version = WorkflowTemplateVersion(
            template=template,
            version=1,
            provider=provider,
            model_profile_version=profile,
            validated_deployment=deployment,
            status=WorkflowTemplateVersion.Status.PUBLISHED,
            validation_state=WorkflowTemplateVersion.ValidationState.PASSED,
            smoke_test_state=WorkflowTemplateVersion.ValidationState.PASSED,
            is_current=True,
        )
        assign_json_contract(version, "graph", {
            "1": {"class_type": "CLIPTextEncode", "inputs": {"text": "hello"}},
        })
        assign_json_contract(version, "input_contract", {
            "schema_version": 1,
            "workflow_mode": "text_to_image",
            "capability": "image.text_to_image",
        })
        version.save()
        return version, deployment

    def test_catalog_without_published_local_workflow_does_not_require_gpu(self):
        WorkflowTemplateVersion.objects.update(
            status=WorkflowTemplateVersion.Status.DRAFT,
            is_current=False,
        )

        result = _workflow_catalog_check()

        self.assertTrue(result["ok"], result)
        self.assertFalse(result["local_required"])
        self.assertNotIn("cuda", str(result).lower())

    def test_media_readiness_reports_missing_explicit_model_family_without_crashing(self):
        workflow, deployment = self._published_local_workflow()

        result = _media_capability_readiness([{
            "workflow_version_id": str(workflow.id),
            "model_deployment_id": str(deployment.id),
        }], modality="image")

        self.assertEqual(result["state"], "blocked")
        self.assertIn("model_profile_contract_invalid", result["reason_codes"])
        self.assertNotIn("readiness_evaluation_failed", result["reason_codes"])

    def test_published_local_workflow_uses_bound_deployment_and_live_contract(self):
        version, deployment = self._published_local_workflow()
        driver = Mock()
        driver.invoke.side_effect = lambda bound, operation, payload: (
            {"system": {"comfyui_version": "test"}, "devices": []}
            if operation == "health"
            else {
                "CLIPTextEncode": {
                    "input": {"required": {"text": ["STRING", {}]}},
                    "output": ["CONDITIONING"],
                    "output_name": ["CONDITIONING"],
                },
            }
        )

        with (
            patch("studio.services.readiness.driver_for", return_value=driver),
            patch("studio.services.readiness.verify_published_workflow_capability"),
        ):
            result = _workflow_catalog_check()

        self.assertTrue(result["ok"], result)
        self.assertTrue(result["local_required"])
        self.assertEqual(result["failures"], {})
        self.assertEqual(
            [call.args[:2] for call in driver.invoke.call_args_list],
            [(deployment, "health"), (deployment, "object_info")],
        )
        self.assertEqual(result["production_workflow_count"], 1)
        self.assertEqual(version.validated_deployment_id, deployment.id)

    def test_missing_bound_deployment_is_explicitly_not_ready(self):
        version, _ = self._published_local_workflow()
        version.validated_deployment = None
        version.save(update_fields=["validated_deployment", "updated_at"])

        result = _workflow_catalog_check()

        self.assertFalse(result["ok"])
        self.assertEqual(
            result["failures"][str(version.id)],
            "validated_deployment_unavailable",
        )

    def test_live_schema_drift_is_explicitly_not_ready(self):
        version, _ = self._published_local_workflow()
        driver = Mock()
        driver.invoke.side_effect = lambda deployment, operation, payload: (
            {} if operation == "health" else {}
        )

        with (
            patch("studio.services.readiness.driver_for", return_value=driver),
            patch("studio.services.readiness.verify_published_workflow_capability"),
        ):
            result = _workflow_catalog_check()

        self.assertFalse(result["ok"])
        self.assertEqual(result["failures"][str(version.id)], "live_contract_unavailable")

    def test_stale_unrequired_worker_is_diagnostic_not_global_503(self):
        self._set_worker_requirements(generation=False)
        stale = timezone.now() - timedelta(minutes=5)
        WorkerLease.objects.filter(pk="generation-worker-heartbeat").update(updated_at=stale)
        with (
            patch("studio.services.readiness._binary_available", return_value=True),
            patch(
                "studio.services.readiness._readiness_runtime_values",
                return_value={
                    "worker_max_age_seconds": 10,
                    "minimum_disk_free_bytes": 1,
                    "generation_worker_required": False,
                    "text_generation_worker_required": True,
                    "assembly_worker_required": True,
                },
            ),
        ):
            payload = collect_readiness()

        self.assertEqual(payload["status"], "ready")
        self.assertTrue(payload["diagnostics"]["generation_worker"]["ok"])
        self.assertFalse(payload["diagnostics"]["generation_worker"]["required"])
        self.assertIn("未设为必需", payload["diagnostics"]["generation_worker"]["message"])

    def test_worker_heartbeat_touch_creates_and_refreshes_lease(self):
        WorkerLease.objects.filter(pk="generation-worker-heartbeat").delete()
        touch_worker_heartbeat("generation-worker-heartbeat")
        first = WorkerLease.objects.get(pk="generation-worker-heartbeat").updated_at
        WorkerLease.objects.filter(pk="generation-worker-heartbeat").update(
            updated_at=first - timedelta(minutes=1),
        )

        touch_worker_heartbeat("generation-worker-heartbeat")

        refreshed = WorkerLease.objects.get(pk="generation-worker-heartbeat").updated_at
        self.assertGreaterEqual(refreshed, first)

    def test_missing_toolchain_or_comfyui_output_is_capability_diagnostic(self):
        self._set_worker_requirements(generation=False, assembly=False)
        with patch("studio.services.readiness._binary_available", return_value=False):
            toolchain = collect_readiness()
        self.assertEqual(toolchain["status"], "ready")
        self.assertFalse(toolchain["diagnostics"]["toolchain"]["ok"])

        missing_output = Path(self.temp_dir.name) / "missing-comfy-output"
        with self.settings(COMFYUI_OUTPUT_DIR=missing_output):
            output = collect_readiness()
        self.assertEqual(output["status"], "ready")
        self.assertFalse(output["diagnostics"]["comfyui_output"]["ok"])

    def test_comfyui_output_uses_the_storage_minimum(self):
        self._set_worker_requirements(generation=False, assembly=False)
        root = Path(self.temp_dir.name)
        media = root / "media"
        output = root / "comfy-output"
        media.mkdir()
        output.mkdir()

        def disk_usage(path):
            return SimpleNamespace(free=1_000 if Path(path) == media else 99)

        with (
            self.settings(
                MEDIA_ROOT=media,
                COMFYUI_OUTPUT_DIR=output,
            ),
            patch("studio.services.readiness.shutil.disk_usage", side_effect=disk_usage),
            patch(
                "studio.services.readiness._readiness_runtime_values",
                return_value={
                    "worker_max_age_seconds": 30,
                    "minimum_disk_free_bytes": 100,
                    "generation_worker_required": False,
                    "text_generation_worker_required": True,
                    "assembly_worker_required": False,
                },
            ),
        ):
            payload = collect_readiness()

        self.assertTrue(payload["checks"]["storage"]["ok"])
        self.assertFalse(payload["diagnostics"]["comfyui_output"]["ok"])
        self.assertEqual(payload["diagnostics"]["comfyui_output"]["minimum_free_bytes"], 100)

    def test_readiness_endpoint_status_follows_payload(self):
        ready_payload = {"status": "ready", "checks": {}}
        unavailable_payload = {"status": "not_ready", "checks": {"database": {"ok": False}}}
        with patch("studio.health_views.collect_readiness", return_value=ready_payload):
            ready = self.client.get("/api/v1/health/ready/")
        with patch("studio.health_views.collect_readiness", return_value=unavailable_payload):
            unavailable = self.client.get("/api/v1/health/ready/")

        self.assertEqual(ready.status_code, 200)
        self.assertEqual(unavailable.status_code, 503)

    def test_readiness_reuses_runtime_and_security_resolutions_per_request(self):
        target_boundary = "security.api.max_search_chars"
        original_boundary_value = boundary_int(target_boundary)
        capability_report = {
            "schema_version": 1,
            **{
                key: {
                    "state": "not_required",
                    "required": False,
                    "reason_codes": [],
                }
                for key in ("text", "image", "video", "assembly")
            },
        }
        observed_values = []

        def repeated_boundary_consumer(*_args, **_kwargs):
            first = boundary_int(target_boundary)
            second = boundary_int(target_boundary)
            self.assertEqual(first, second)
            observed_values.append(first)
            return capability_report

        original_runtime = readiness_service._readiness_runtime_values
        original_security = configuration_service.resolve_security_boundaries
        with (
            patch(
                "studio.services.readiness._readiness_runtime_values",
                wraps=original_runtime,
            ) as runtime_resolver,
            patch(
                "studio.services.readiness._capability_readiness_for_requirements",
                side_effect=repeated_boundary_consumer,
            ),
            patch.object(
                configuration_service,
                "resolve_security_boundaries",
                wraps=original_security,
            ) as security_resolver,
        ):
            payload = collect_readiness()
            first_target_call_count = sum(
                1
                for call in security_resolver.call_args_list
                if tuple(sorted(call.kwargs["required_keys"])) == (target_boundary,)
            )

            revision = create_config_revision(
                scope_type=ConfigRevision.ScopeType.SYSTEM,
                values={target_boundary: original_boundary_value + 1},
                created_by=self.superuser,
            )
            publish_config_revision(revision, published_by=self.superuser)
            target_calls_before_second_request = sum(
                1
                for call in security_resolver.call_args_list
                if tuple(sorted(call.kwargs["required_keys"])) == (target_boundary,)
            )
            second_payload = collect_readiness()
            target_calls_after_second_request = sum(
                1
                for call in security_resolver.call_args_list
                if tuple(sorted(call.kwargs["required_keys"])) == (target_boundary,)
            )

        self.assertEqual(
            payload["status"],
            "ready" if all(item["ok"] for item in payload["checks"].values()) else "not_ready",
        )
        self.assertEqual(payload["capabilities"], capability_report)
        self.assertEqual(second_payload["capabilities"], capability_report)
        self.assertEqual(runtime_resolver.call_count, 2)
        self.assertEqual(first_target_call_count, 1)
        self.assertEqual(
            target_calls_after_second_request - target_calls_before_second_request,
            1,
        )
        self.assertEqual(
            observed_values,
            [original_boundary_value, original_boundary_value + 1],
        )

    def test_production_readiness_is_superuser_only_and_empty_catalog_is_unconfigured(self):
        ProductionRecipeVersion.objects.update(status=ProductionRecipeVersion.Status.RETIRED)
        client = APIClient()
        client.force_authenticate(self.superuser)
        response = client.get("/api/v1/production-readiness/")

        self.assertEqual(response.status_code, 200, response.data)
        self.assertEqual(response.data["status"], "unconfigured")
        self.assertIn("published_recipe_missing", response.data["reason_codes"])

        member = User.objects.create_user(
            username="readiness-member",
            password="StrongPass123!",
        )
        client.force_authenticate(member)
        forbidden = client.get("/api/v1/production-readiness/")
        self.assertEqual(forbidden.status_code, 403)

    def test_video_readiness_accepts_generic_and_official_context_ir_profiles(self):
        provider = ProviderProfile(
            key="readiness-ref2va-provider",
            name="Readiness Ref2VA provider",
            driver_type=ProviderProfile.DriverType.COMFYUI,
            modalities="video",
            operations="health,object_info,submit,poll,cancel,collect",
        )
        assign_json_contract(provider, "driver_config", {})
        provider.save()
        model = ModelProfileVersion(
            profile_key="readiness-ref2va-model",
            version=1,
            provider=provider,
            model_identifier="readiness-ref2va-model",
            modality=ModelProfileVersion.Modality.VIDEO,
            status=ModelProfileVersion.Status.PUBLISHED,
        )
        assign_json_contract(model, "specification", {
            "schema_version": 1,
            "model_family": "minimax_h3",
        })
        model.save()
        deployment = ModelDeployment(
            key="readiness-ref2va-deployment",
            name="Readiness Ref2VA deployment",
            profile_version=model,
            status=ModelDeployment.Status.ACTIVE,
        )
        assign_json_contract(deployment, "runtime_config", {})
        deployment.save()
        template = WorkflowTemplate.objects.create(
            name="Readiness Ref2VA workflow",
            slug="readiness-ref2va-workflow",
            output_kind=WorkflowTemplate.OutputKind.VIDEO,
            is_system=True,
        )
        workflow = WorkflowTemplateVersion(
            template=template,
            version=1,
            provider=provider,
            model_profile_version=model,
            validated_deployment=deployment,
            status=WorkflowTemplateVersion.Status.PUBLISHED,
            validation_state=WorkflowTemplateVersion.ValidationState.PASSED,
            smoke_test_state=WorkflowTemplateVersion.ValidationState.PASSED,
            is_current=True,
        )
        assign_json_contract(workflow, "graph", {
            "1": {"class_type": "TestRef2VA", "inputs": {}},
        })
        assign_json_contract(workflow, "input_contract", {
            "schema_version": 1,
            "workflow_mode": "reference_to_video",
            "capability": "video.reference_to_video",
            "conditioning_contract": conditioning_contract(
                "Ref2VA",
                input_kind="classified_references",
                requires_bible=True,
                entity_images=(0, 9),
                reference_videos=(0, 3),
                paired_video_audios=(0, 3),
                reference_audios=(0, 3),
            ),
        })
        workflow.save()

        subject = PromptFieldDefinition.objects.create(
            key="readiness_ref2va_subject",
            label="Ref2VA subject",
            category="qa",
            description="Readiness fixture subject",
            impact="Positive prompt",
            value_type=PromptFieldDefinition.ValueType.LONG_TEXT,
        )

        def generic_profile(*, version: int):
            profile = PromptProfileVersion.objects.create(
                profile_key="readiness.ref2va",
                version=version,
                name=f"Readiness Ref2VA v{version}",
                model_family="minimax_h3",
                operation="video.generate",
                mode="Ref2VA",
                review_state=PromptProfileVersion.ReviewState.READY,
            )
            PromptProfileFieldRule.objects.create(
                profile_version=profile,
                field_definition=subject,
                source_selector=PromptProfileFieldRule.SourceSelector.REQUEST_SUBJECT,
                channel=PromptProfileFieldRule.Channel.POSITIVE_PROMPT,
                position=1,
            )
            refresh_prompt_profile_contract(profile)
            # Readiness resolves the active, frozen binding generically. Protocol-
            # specific publication compatibility belongs to the projection adapter.
            profile.status = PromptProfileVersion.Status.PUBLISHED
            profile.save(update_fields=("status", "updated_at"))
            PromptProfileBinding.objects.create(
                key=f"readiness-ref2va-v{version}",
                name=f"Readiness Ref2VA v{version}",
                operation="video.generate",
                model_family="minimax_h3",
                mode="Ref2VA",
                model_profile_version=model,
                prompt_profile_version=profile,
                priority=version * 100,
                is_active=True,
            )

        def official_profile(*, version: int):
            profile = PromptProfileVersion.objects.create(
                profile_key="readiness.ref2va",
                version=version,
                name=f"Readiness Ref2VA official v{version}",
                model_family="minimax_h3",
                operation="video.generate",
                mode="Ref2VA",
                review_state=PromptProfileVersion.ReviewState.READY,
            )
            for position, (selector, field_key, label_style) in enumerate(
                H3_REF2VA_OFFICIAL_RULE_SPECS,
                start=1,
            ):
                field, _created = PromptFieldDefinition.objects.get_or_create(
                    key=field_key,
                    defaults={
                        "label": field_key,
                        "category": "qa",
                        "description": "Official H3 Ref2VA Context-IR field",
                        "impact": "Positive prompt",
                        "value_type": PromptFieldDefinition.ValueType.LONG_TEXT,
                    },
                )
                PromptProfileFieldRule.objects.create(
                    profile_version=profile,
                    field_definition=field,
                    source_selector=selector,
                    channel=PromptProfileFieldRule.Channel.POSITIVE_PROMPT,
                    position=position,
                    required=True,
                    omit_if_empty=False,
                    separator=PromptProfileFieldRule.Separator.NEWLINE,
                    label_style=label_style,
                )
            refresh_prompt_profile_contract(profile)
            profile.status = PromptProfileVersion.Status.PUBLISHED
            profile.save(update_fields=("status", "updated_at"))
            PromptProfileBinding.objects.create(
                key=f"readiness-ref2va-official-v{version}",
                name=f"Readiness Ref2VA official v{version}",
                operation="video.generate",
                model_family="minimax_h3",
                mode="Ref2VA",
                model_profile_version=model,
                prompt_profile_version=profile,
                priority=version * 100,
                is_active=True,
            )

        generic_profile(version=1)
        route = [{
            "workflow_version_id": str(workflow.id),
            "model_deployment_id": str(deployment.id),
        }]
        with (
            patch(
                "studio.services.readiness.verify_published_workflow_capability",
                side_effect=DjangoValidationError(
                    "stale smoke",
                    code="workflow_smoke_missing_or_stale",
                ),
            ),
            patch(
                "studio.services.readiness._worker_check",
                return_value={"ok": True},
            ),
        ):
            stale = _media_capability_readiness(route, modality="video")
        self.assertEqual(stale["state"], "blocked", stale)
        self.assertIn("workflow_smoke_missing_or_stale", stale["reason_codes"])

        with patch(
            "studio.services.readiness._worker_check",
            return_value={"ok": True},
        ), patch(
            "studio.services.readiness.verify_published_workflow_capability",
        ):
            generic_ready = _media_capability_readiness(route, modality="video")
        self.assertEqual(generic_ready["state"], "ready", generic_ready)
        self.assertNotIn(
            "prompt_profile_binding_missing_or_invalid",
            generic_ready["reason_codes"],
        )

        official_profile(version=2)
        with patch(
            "studio.services.readiness._worker_check",
            return_value={"ok": True},
        ), patch(
            "studio.services.readiness.verify_published_workflow_capability",
        ):
            ready = _media_capability_readiness(route, modality="video")
        self.assertEqual(ready["state"], "ready", ready)
        self.assertNotIn(
            "prompt_profile_binding_missing_or_invalid",
            ready["reason_codes"],
        )

    def test_production_readiness_collects_bible_image_routes(self):
        provider = ProviderProfile(
            key="readiness-bible-image-provider",
            name="Readiness Bible image provider",
            driver_type=ProviderProfile.DriverType.ASYNC_REST,
            modalities="image",
            operations="image.text_to_image",
        )
        assign_json_contract(provider, "driver_config", {})
        provider.save()
        profile = ModelProfileVersion(
            profile_key="readiness-bible-image",
            version=1,
            provider=provider,
            model_identifier="readiness-bible-image",
            modality=ModelProfileVersion.Modality.IMAGE,
            status=ModelProfileVersion.Status.PUBLISHED,
        )
        assign_json_contract(profile, "specification", {
            "schema_version": 1,
            "model_family": "readiness_bible_image",
            "capabilities": ["image.text_to_image"],
        })
        profile.save()
        deployment = ModelDeployment(
            key="readiness-bible-image",
            name="Readiness Bible image",
            profile_version=profile,
            status=ModelDeployment.Status.ACTIVE,
        )
        assign_json_contract(deployment, "runtime_config", {})
        deployment.save()
        stage = {
            "key": "bible_image_generation",
            "mode": "existing_pipeline",
            "approval": "auto",
            "routing": {
                "mode": "fixed",
                "routes": [{"model_deployment_id": str(deployment.id)}],
                "variant_count": 1,
                "evaluator": None,
            },
        }
        recipe = ProductionRecipeVersion(
            recipe_key="readiness-bible-image",
            version=1,
            name="Readiness Bible image",
            status=ProductionRecipeVersion.Status.PUBLISHED,
        )
        assign_json_contract(recipe, "definition", {
            "schema_version": 1,
            "stages": [stage],
        })
        recipe.save()

        with patch(
            "studio.services.production_runs.validate_production_recipe_executability",
            return_value=[stage],
        ):
            report = collect_production_readiness()

        row = next(item for item in report["recipes"] if item["id"] == str(recipe.id))
        self.assertEqual(row["state"], "blocked", row)
        self.assertTrue(row["capabilities"]["image"]["required"])
        self.assertEqual(row["capabilities"]["image"]["state"], "blocked")
        self.assertIn(
            "prompt_profile_binding_missing_or_invalid",
            row["capabilities"]["image"]["reason_codes"],
        )

    def test_text_requirements_collect_visual_rules_without_dropping_existing_operations(self):
        deployment_id = "shared-text-deployment"
        stages = [
            {
                "key": stage_key,
                "mode": "ai",
                "routing": {"routes": [{"model_deployment_id": deployment_id}]},
            }
            for stage_key in (
                "script_generate",
                "script_structure",
                "visual_rules_generate",
                "script_normalize",
            )
        ]

        requirements = _requirements_from_stages(stages)

        self.assertEqual(requirements["text_deployments"], {deployment_id})
        self.assertEqual(requirements["text_operations"][deployment_id], {
            "script.generate",
            "script.structure",
            "visual_rules.generate",
            "prompt.normalize",
        })

    def test_text_readiness_checks_visual_rules_contract_and_dedicated_smoke(self):
        provider = ProviderProfile(
            key="readiness-visual-rules-provider",
            name="Readiness visual rules provider",
            driver_type=ProviderProfile.DriverType.LOCAL_QWEN,
            modalities="text",
            operations="chat_completion",
        )
        assign_json_contract(provider, "driver_config", {})
        provider.save()
        profile = ModelProfileVersion(
            profile_key="readiness-visual-rules",
            version=1,
            provider=provider,
            model_identifier="readiness-visual-rules",
            modality=ModelProfileVersion.Modality.TEXT,
            status=ModelProfileVersion.Status.PUBLISHED,
        )
        assign_json_contract(profile, "specification", {
            "schema_version": 1,
            "model_family": "readiness_visual_rules",
        })
        profile.save()
        deployment = ModelDeployment(
            key="readiness-visual-rules",
            name="Readiness visual rules",
            profile_version=profile,
            status=ModelDeployment.Status.ACTIVE,
        )
        assign_json_contract(deployment, "runtime_config", {})
        deployment.save()
        deployment_id = str(deployment.id)
        required_operations = {
            "script.generate",
            "script.structure",
            "visual_rules.generate",
            "prompt.normalize",
        }

        def smoke_snapshot(_deployment, operation):
            return None if operation == "visual_rules.generate" else SimpleNamespace()

        with (
            patch(
                "studio.services.readiness.validate_script_generation_model_profile",
                return_value={"max_input_bytes": 1},
            ) as script_contract,
            patch(
                "studio.services.readiness.validate_script_structure_model_profile",
                return_value={"max_input_bytes": 1},
            ) as structure_contract,
            patch(
                "studio.services.readiness.validate_visual_rules_model_profile",
                return_value={"max_input_bytes": 1},
            ) as visual_rules_contract,
            patch(
                "studio.services.readiness.validate_prompt_normalization_model_profile",
                return_value={"max_input_bytes": 1},
            ) as normalize_contract,
            patch("studio.services.readiness.require_prompt_profile_binding"),
            patch(
                "studio.services.readiness.deployment_supports_script_generation",
                return_value=True,
            ),
            patch(
                "studio.services.readiness.fresh_smoke_snapshot",
                side_effect=smoke_snapshot,
            ) as smoke,
            patch(
                "studio.services.readiness._worker_check",
                return_value={"ok": True},
            ),
        ):
            report = _capability_readiness_for_requirements({
                "text_deployments": {deployment_id},
                "text_operations": {deployment_id: required_operations},
                "image_routes": [],
                "video_routes": [],
                "assembly": False,
            })

        self.assertEqual(report["text"]["state"], "blocked")
        self.assertIn(
            "visual_rules_smoke_missing_or_stale",
            report["text"]["reason_codes"],
        )
        self.assertEqual(
            {call.args[1] for call in smoke.call_args_list},
            required_operations,
        )
        for contract in (
            script_contract,
            structure_contract,
            visual_rules_contract,
            normalize_contract,
        ):
            contract.assert_called_once_with(profile)

    def test_readiness_requires_an_active_usable_superuser(self):
        self.superuser.set_unusable_password()
        self.superuser.save(update_fields=("password",))

        payload = collect_readiness()

        self.assertEqual(payload["status"], "not_ready")
        self.assertFalse(payload["checks"]["access_control"]["ok"])
        self.assertEqual(payload["checks"]["access_control"]["active_superuser_count"], 0)

    def test_readiness_validates_keyring_even_without_secret_references(self):
        with patch.dict(os.environ, {
            "AUTOPROMPT_CONFIG_MASTER_KEYS": "",
            "AUTOPROMPT_CONFIG_ACTIVE_KEY_ID": "",
        }, clear=False):
            payload = collect_readiness()

        self.assertEqual(payload["status"], "not_ready")
        self.assertFalse(payload["checks"]["secret_store"]["ok"])
        self.assertIn("secret_keyring_unconfigured", payload["checks"]["secret_store"]["failure_codes"])

    def test_liveness_endpoint_does_not_require_database_workers_or_gpu(self):
        WorkerLease.objects.all().delete()

        response = self.client.get("/api/v1/health/live/")

        self.assertEqual(response.status_code, 200)
        self.assertEqual(response.data["status"], "ok")

    def test_database_failure_keeps_liveness_up_and_readiness_structured(self):
        with patch(
            "studio.services.readiness.connection.cursor",
            side_effect=OperationalError("database unavailable"),
        ):
            live = self.client.get("/api/v1/health/live/")
            ready = self.client.get("/api/v1/health/ready/")

        self.assertEqual(live.status_code, 200)
        self.assertEqual(ready.status_code, 503)
        self.assertFalse(ready.data["checks"]["database"]["ok"])
        self.assertFalse(ready.data["checks"]["migrations"]["ok"])
        self.assertEqual(ready.data["diagnostics"], {})
        self.assertEqual(
            ready.data["capabilities"]["text"]["reason_codes"],
            ["database_or_migrations_unavailable"],
        )
