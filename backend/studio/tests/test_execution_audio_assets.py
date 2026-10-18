"""Native audio instance options and original asset worker; isolated WAV/peer."""
from decimal import Decimal
from pathlib import Path
import tempfile
from unittest.mock import Mock, patch
from uuid import uuid4
import wave

from django.contrib.auth import get_user_model
from django.test import TestCase, override_settings
from django.utils import timezone
from rest_framework.test import APIClient

from studio.models import Asset, AssetGenerationJob, ModelDeployment, ModelProfileVersion, ProviderProfile
from studio.services.asset_generation import create_asset_generation_job, claim_next_media_asset_job, process_media_asset_job
from studio.services.execution_asset_dispatch import dispatch_asset_execution
from studio.services.json_contracts import assign_json_contract
from studio.tests.test_execution_asset_requests import freeze, isolated_observations


@override_settings(AI_PROVIDER_ALLOWED_HOSTS=("127.0.0.1",))
class ExecutionAudioAssetTests(TestCase):
    def setUp(self):
        self.user = get_user_model().objects.create_user(username="audio-asset-fixture", is_staff=True)
        self.client = APIClient()
        self.client.force_authenticate(self.user)
        self.media_root = self.enterContext(tempfile.TemporaryDirectory())
        self.enterContext(override_settings(MEDIA_ROOT=self.media_root))
        from studio.services.execution_definitions import build_execution_definition
        from studio.services.execution_instances import create_execution_instance, create_execution_draft, publish_execution_version

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
            "pricing": {"currency": "USD", "fixed_per_request": "0.020000"},
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
        instance = create_execution_instance(key=f"asset-audio-instance-{suffix}", name="音频执行实例", description="", actor=self.user)
        version = create_execution_draft(instance_id=instance.pk, expected_revision=instance.revision, actor=self.user,
            selection={"deployment": deployment, "operation": "audio.text_to_audio", "mode": "", "prompt_mode": "manual",
                "prompt_profile_version": None, "workflow_version": None})
        definition = build_execution_definition(version.pk)
        publish_execution_version(version_id=version.pk, expected_revision=version.revision,
            expected_definition_sha256=definition.sha256, actor=self.user)

        self.version, self.definition = version, definition

    def test_options_reference_exact_audio_instance_without_turning_smoke_examples_into_defaults(self):
        response = self.client.get(
            "/api/v1/asset-generation-jobs/options/",
            {"job_type": "asset", "media_type": "audio"},
        )
        self.assertEqual(response.status_code, 200, response.data)
        self.assertNotIn("routes", response.data)
        option = response.data["results"][0]
        self.assertEqual(option["operation"], "audio.text_to_audio")
        self.assertEqual(option["execution_instance_version_id"], str(self.version.pk))
        self.assertNotIn("parameter_defaults", option)
        duration = self.definition.value["interface"]["parameters"]["duration_seconds"]["rule"]
        self.assertEqual((duration["minimum"], duration["maximum"]), (2, 15))
        self.assertNotIn("default", duration)  # A Smoke example is not a business default.

    def test_original_queue_materializes_probed_wav_from_the_frozen_audio_request(self):
        self.enterContext(patch("studio.services.execution_asset_dispatch.collect_asset_execution_admission",
            side_effect=isolated_observations))
        job, _ = create_asset_generation_job(owner=self.user, client_idempotency_key="native-audio-asset",
            execution_instance_version_id=self.version.pk, expected_definition_sha256=self.definition.sha256,
            job_type="asset", media_type="audio", name="隔离雨声", parameters={"duration_seconds": 2},
            inputs={}, manual_channels={"positive_prompt": "生成两秒雨声"}, subject_facts=[],
            source_language="", target_language="", field_values=[], allow_external_calls=True, budget_limit=None)
        preparation = freeze(job, self.user)
        dispatched = dispatch_asset_execution(preparation_id=preparation.pk,
            expected_preparation_revision=preparation.revision, actor=self.user)
        self.assertEqual(dispatched.status, "queued", dispatched.error_message)
        peer = Mock()
        peer.invoke.side_effect = [
            {"request_id": "isolated-native-audio", "status": "submitted"},
            {"status": "succeeded", "terminal_success": True},
            {"request_id": "isolated-native-audio", "artifacts": [{"kind": "audio", "url": "http://127.0.0.1/audio.wav"}],
             "actual_cost": "0.02", "currency": "USD"},
        ]
        def download_fixture(*, target, **kwargs):
            self.assertTrue(Path(target).resolve().is_relative_to(Path(self.media_root).resolve()))
            self.assertEqual(kwargs["expected_kind"], "audio")
            target.parent.mkdir(parents=True, exist_ok=True)
            # Real deterministic WAV bytes, not a fake successful media probe.
            with wave.open(str(target), "wb") as stream:
                stream.setparams((1, 2, 16000, 0, "NONE", "not compressed"))
                stream.writeframes(b"\x00\x00" * 32000)
        with patch("studio.services.asset_generation.driver_for", return_value=peer), patch(
                "studio.services.asset_generation.download_remote_artifact", side_effect=download_fixture) as download:
            for state in ("submitted", "running", "succeeded"):
                AssetGenerationJob.objects.filter(pk=job.pk).update(next_poll_at=timezone.now())
                claim = claim_next_media_asset_job("isolated-native-audio")
                self.assertIsNotNone(claim)
                result = process_media_asset_job(claim)
                self.assertEqual(result.status, state, result.error_message)
        self.assertEqual(peer.invoke.call_args_list[0].args[2]["prompt"], "生成两秒雨声")
        self.assertEqual(peer.invoke.call_args_list[0].args[2]["duration_seconds"], 2)
        self.assertEqual([call.args[1] for call in peer.invoke.call_args_list], ["submit", "poll", "collect"])
        self.assertEqual(download.call_count, 1)
        self.assertEqual(result.actual_cost, Decimal("0.02"))
        self.assertEqual(result.cost_currency, "USD")
        asset = Asset.objects.get(pk=result.output_asset_id)
        self.assertEqual(asset.kind, Asset.Kind.REFERENCE_AUDIO)
        self.assertEqual(asset.metadata["generation_job_id"], str(job.pk))
        self.assertEqual(asset.metadata["preparation_id"], str(preparation.pk))
        self.assertEqual(asset.mime_type, "audio/wav")
        self.assertTrue(Path(asset.file.path).is_file())
        self.assertIsNone(claim_next_media_asset_job("no-second-submission"))
