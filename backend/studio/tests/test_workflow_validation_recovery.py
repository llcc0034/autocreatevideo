from datetime import timedelta
from unittest.mock import patch

from django.contrib.auth import get_user_model
from django.core.exceptions import ValidationError
from django.test import TestCase
from studio.tests.workflow_projector_fixtures import workflow_projector_reference
from django.utils import timezone
from rest_framework.test import APIClient

from studio.models import (
    HostCapabilitySnapshot,
    ModelArtifact,
    ModelDeployment,
    ModelProfileVersion,
    ProviderProfile,
    RuntimeHost,
    RuntimeModelAvailability,
    WorkflowTemplate,
)
from studio.services.json_contracts import assign_json_contract
from studio.services.runtime_platform import verified_artifact_availability
from studio.services.workflow_versions import (
    WorkflowModelEvidenceUnavailable,
    import_workflow_version,
)


class WorkflowValidationRecoveryApiTests(TestCase):
    def setUp(self):
        self.admin = get_user_model().objects.create_superuser(
            username="workflow-recovery-admin",
            password="StrongPass123!",
            email="workflow-recovery@example.test",
        )
        self.client = APIClient()
        self.client.force_authenticate(self.admin)
        self.provider = ProviderProfile.objects.get(key="local-comfyui")
        self.profile = ModelProfileVersion.objects.get(profile_key="sdxl-image", version=1)
        self.host = RuntimeHost.objects.create(
            key="workflow-recovery-host",
            name="Workflow recovery host",
        )
        self.deployment = ModelDeployment(
            key="workflow-recovery-deployment",
            name="Workflow recovery deployment",
            profile_version=self.profile,
            runtime_host=self.host,
        )
        assign_json_contract(self.deployment, "runtime_config", {
            "base_url": "http://127.0.0.1:8188",
            "timeout_seconds": 10,
            "poll_interval_seconds": 1,
            "max_poll_seconds": 60,
            "capability_ttl_seconds": 300,
        })
        self.deployment.save()
        self.artifact = ModelArtifact.objects.create(
            profile_version=self.profile,
            role=ModelArtifact.Role.CHECKPOINT,
            logical_name="workflow-recovery-checkpoint",
            filename="workflow-recovery.safetensors",
            sha256="a" * 64,
            size_bytes=128,
            status=ModelArtifact.Status.AVAILABLE,
        )
        self.snapshot = HostCapabilitySnapshot(
            host=self.host,
            revision=1,
            probe_status=HostCapabilitySnapshot.ProbeStatus.SUCCEEDED,
            captured_at=timezone.now(),
            expires_at=timezone.now() + timedelta(hours=1),
        )
        assign_json_contract(self.snapshot, "details", {"schema_version": 1})
        self.snapshot.save()
        RuntimeModelAvailability.objects.create(
            snapshot=self.snapshot,
            deployment=self.deployment,
            artifact=self.artifact,
            availability_key="workflow-recovery-checkpoint",
            status=RuntimeModelAvailability.Status.AVAILABLE,
            observed_size_bytes=128,
            observed_sha256="a" * 64,
            observation_source=RuntimeModelAvailability.ObservationSource.LOCAL_FILESYSTEM,
        )
        template = WorkflowTemplate.objects.create(
            owner=None,
            slug="workflow-recovery-template",
            version=1,
            name="Workflow recovery template",
            provider="comfyui",
            output_kind=WorkflowTemplate.OutputKind.IMAGE,
            definition={},
            input_schema={},
            is_system=True,
            is_active=False,
        )
        self.version = import_workflow_version(
            template=template,
            provider=self.provider,
            model_profile_version=self.profile,
            graph={
                "1": {"class_type": "CLIPTextEncode", "inputs": {"text": "hello"}},
                "2": {"class_type": "SaveImage", "inputs": {
                    "images": "smoke", "filename_prefix": "output",
                }},
                "3": {"class_type": "EmptyLatentImage", "inputs": {
                    "width": 512, "height": 512, "batch_size": 1,
                }},
            },
            input_contract={
                **workflow_projector_reference(),
                "schema_version": 3,
                "workflow_mode": "semantic",
                "capability": "image.text_to_image",
                "compiler": {"id": "generic.comfy", "version": 1},
                "resolution_strategy": {
                    "mode": "explicit_width_height",
                    "node_ids": ["3"],
                },
                "required_dimension_multiple": 8,
                "parameters": {
                    "prompt.positive": {"type": "string", "required": True},
                    "output.prefix": {"type": "string", "required": True},
                    "image.width": {"type": "integer", "required": True, "multiple_of": 8},
                    "image.height": {"type": "integer", "required": True, "multiple_of": 8},
                },
                "outputs": {
                    "primary": {"node_id": "2", "kind": "image", "required": True},
                },
            },
            semantic_bindings=[
                {"semantic_key": "prompt.positive", "node_id": "1", "input_name": "text", "value_type": "string"},
                {"semantic_key": "output.prefix", "node_id": "2", "input_name": "filename_prefix", "value_type": "string"},
                {"semantic_key": "image.width", "node_id": "3", "input_name": "width", "value_type": "integer"},
                {"semantic_key": "image.height", "node_id": "3", "input_name": "height", "value_type": "integer"},
            ],
            model_requirements=[],
        )

    def _failure(self):
        return WorkflowModelEvidenceUnavailable(
            reason_code="host_snapshot_stale",
            requirement_alias="checkpoint",
            artifact_id=str(self.artifact.id),
        )

    def _validate(self):
        with patch(
            "studio.platform_views.validate_workflow_version",
            side_effect=self._failure(),
        ):
            return self.client.post(
                f"/api/v1/workflow-template-versions/{self.version.id}/validate/",
                {"deployment_id": str(self.deployment.id)},
                format="json",
            )

    def test_guidance_detects_completed_new_evidence_and_offers_retry(self):
        response = self._validate()

        self.assertEqual(response.status_code, 400, response.data)
        self.assertEqual(response.data["error"]["code"], "host_snapshot_stale")
        details = response.data["error"]["details"]
        self.assertEqual(details["recovery"]["state"], "ready_to_retry")
        self.assertEqual(
            details["recovery"]["actions"],
            [{"kind": "retry_live_validate", "label": "重新校验"}],
        )
        self.assertIn("工作流模板和模型文件都不需要修改", details["detail"])
        self.assertNotIn("a" * 64, str(response.data))
        self.assertNotIn(self.artifact.filename, str(response.data))

    def test_stale_evidence_explains_host_probe_then_retry(self):
        self.snapshot.expires_at = timezone.now() - timedelta(seconds=1)
        self.snapshot.save(update_fields=("expires_at", "updated_at"))

        with self.assertRaises(ValidationError) as caught:
            verified_artifact_availability(
                snapshot=self.snapshot,
                deployment=self.deployment,
                artifact=self.artifact,
            )
        self.assertEqual(caught.exception.error_list[0].code, "host_snapshot_stale")

        response = self._validate()

        self.assertEqual(response.status_code, 400, response.data)
        recovery = response.data["error"]["details"]["recovery"]
        self.assertEqual(recovery["state"], "host_probe_required")
        self.assertEqual(
            [item["section"] for item in recovery["actions"] if item["kind"] == "open_settings"],
            ["runtime"],
        )
        self.assertEqual(recovery["actions"][-1]["kind"], "retry_live_validate")
