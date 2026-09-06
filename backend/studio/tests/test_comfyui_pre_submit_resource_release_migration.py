from __future__ import annotations

import hashlib
import json
from copy import deepcopy

from django.utils import timezone

from studio.tests.test_configuration_migrations import _MigrationTestCase


class ComfyUIPreSubmitResourceReleaseMigrationTests(_MigrationTestCase):
    migrate_from = "0081_prompt_normalization_smoke_capacity"
    migrate_to = "0082_comfyui_pre_submit_resource_release"

    route_keys = (
        "comfyui-minimax-h3-ref2va-0p2mp",
        "z-image-turbo-local",
    )

    @staticmethod
    def _contract(value):
        encoded = json.dumps(
            value,
            ensure_ascii=False,
            sort_keys=True,
            separators=(",", ":"),
            allow_nan=False,
        ).encode("utf-8")
        return len(encoded), hashlib.sha256(encoded).hexdigest()

    def set_up_before_migration(self, apps):
        Provider = apps.get_model("studio", "ProviderProfile")
        Profile = apps.get_model("studio", "ModelProfileVersion")
        Deployment = apps.get_model("studio", "ModelDeployment")
        Template = apps.get_model("studio", "WorkflowTemplate")
        Workflow = apps.get_model("studio", "WorkflowTemplateVersion")

        provider = Provider.objects.filter(key="local-comfyui").first()
        if provider is None:
            driver_config = {}
            size_bytes, digest = self._contract(driver_config)
            provider = Provider.objects.create(
                key="local-comfyui",
                name="Local ComfyUI",
                driver_type="comfyui",
                modalities="image,video,audio",
                operations="submit,poll,collect",
                driver_config=driver_config,
                driver_config_schema_version=1,
                driver_config_size_bytes=size_bytes,
                driver_config_sha256=digest,
                is_active=True,
            )

        self.deployments = {}
        self.workflows = {}
        for index, key in enumerate(self.route_keys, start=1):
            specification = {
                "schema_version": 1,
                "capabilities": [
                    "video.reference_to_video" if index == 1 else "image.text_to_image",
                ],
            }
            spec_size, spec_sha = self._contract(specification)
            profile = Profile.objects.create(
                profile_key=f"migration-0082-comfy-{index}",
                version=1,
                provider_id=provider.id,
                model_identifier=f"migration-model-{index}",
                modality="video" if index == 1 else "image",
                specification=specification,
                specification_schema_version=1,
                specification_size_bytes=spec_size,
                specification_sha256=spec_sha,
                status="published",
                published_at=timezone.now(),
            )
            runtime_config = {
                "base_url": "http://127.0.0.1:8188",
                "timeout_seconds": 30,
            }
            config_size, config_sha = self._contract(runtime_config)
            deployment = Deployment.objects.create(
                key=key,
                name=f"Migration route {index}",
                profile_version_id=profile.id,
                runtime_config=runtime_config,
                runtime_config_schema_version=1,
                runtime_config_size_bytes=config_size,
                runtime_config_sha256=config_sha,
                status="active",
            )
            template = Template.objects.create(
                name=f"Migration workflow {index}",
                slug=f"migration-0082-workflow-{index}",
                provider="comfyui",
                output_kind="video" if index == 1 else "image",
                definition={},
                is_active=True,
            )
            graph = {"1": {"class_type": "TestNode", "inputs": {}}}
            graph_size, graph_sha = self._contract(graph)
            input_contract = {"schema_version": 1}
            input_size, input_sha = self._contract(input_contract)
            validation_evidence = {
                "schema_version": 1,
                "deployment_id": str(deployment.id),
                "execution_target_sha256": "a" * 64,
            }
            smoke_evidence = {
                "schema_version": 3,
                "deployment_id": str(deployment.id),
                "execution_target_sha256": "a" * 64,
                "execution_submitted": True,
            }
            workflow = Workflow.objects.create(
                template_id=template.id,
                version=1,
                provider_id=provider.id,
                model_profile_version_id=profile.id,
                graph=graph,
                graph_schema_version=1,
                graph_size_bytes=graph_size,
                graph_sha256=graph_sha,
                input_contract=input_contract,
                input_contract_schema_version=1,
                input_contract_size_bytes=input_size,
                input_contract_sha256=input_sha,
                semantic_bindings_sha256=self._contract([])[1],
                model_requirements_sha256=self._contract([])[1],
                execution_contract_sha256="b" * 64,
                status="published",
                validation_state="passed",
                validation_message="old validation",
                smoke_test_state="passed",
                smoke_test_message="old smoke",
                validated_deployment_id=deployment.id,
                validation_evidence=validation_evidence,
                validation_evidence_sha256=self._contract(validation_evidence)[1],
                smoke_test_evidence=smoke_evidence,
                smoke_test_evidence_sha256=self._contract(smoke_evidence)[1],
                is_current=True,
                published_at=timezone.now(),
            )
            self.deployments[key] = {
                "id": deployment.id,
                "runtime_config": deepcopy(runtime_config),
                "runtime_config_sha256": config_sha,
            }
            self.workflows[key] = workflow.id

    def test_current_routes_gain_exact_policy_and_stale_workflow_evidence_is_invalidated(self):
        Deployment = self.apps.get_model("studio", "ModelDeployment")
        Workflow = self.apps.get_model("studio", "WorkflowTemplateVersion")
        expected_policy = {
            "schema_version": 1,
            "enabled": True,
            "unload_models": True,
            "free_memory": True,
        }

        for key in self.route_keys:
            before = self.deployments[key]
            deployment = Deployment.objects.get(pk=before["id"])
            self.assertEqual(
                deployment.runtime_config,
                {**before["runtime_config"], "pre_submit_resource_release": expected_policy},
            )
            size_bytes, digest = self._contract(deployment.runtime_config)
            self.assertEqual(deployment.runtime_config_size_bytes, size_bytes)
            self.assertEqual(deployment.runtime_config_sha256, digest)
            self.assertNotEqual(deployment.runtime_config_sha256, before["runtime_config_sha256"])

            workflow = Workflow.objects.get(pk=self.workflows[key])
            self.assertEqual(workflow.status, "draft")
            self.assertEqual(workflow.validation_state, "not_run")
            self.assertEqual(workflow.smoke_test_state, "not_run")
            self.assertIsNone(workflow.validated_deployment_id)
            self.assertFalse(workflow.is_current)
            self.assertIsNone(workflow.published_at)
            self.assertEqual(workflow.validation_evidence, {})
            self.assertEqual(workflow.validation_evidence_sha256, "")
            self.assertEqual(workflow.smoke_test_evidence, {})
            self.assertEqual(workflow.smoke_test_evidence_sha256, "")
            self.assertIn("重新 live validate", workflow.validation_message)

