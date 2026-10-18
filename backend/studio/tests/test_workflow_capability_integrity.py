from __future__ import annotations

from datetime import timedelta
from uuid import uuid4

from django.contrib.auth import get_user_model
from django.core.exceptions import ValidationError
from django.test import TestCase
from studio.tests.workflow_projector_fixtures import workflow_projector_reference
from django.utils import timezone

from studio.models import (
    ModelArtifact,
    ModelDeployment,
    ModelProfileVersion,
    ProviderProfile,
    WorkflowModelRequirement,
    WorkflowSmokeAttempt,
    WorkflowTemplate,
    WorkflowTemplateVersion,
)
from studio.services.json_contracts import assign_json_contract, json_contract
from studio.services.workflow_versions import (
    _execution_target_evidence,
    import_workflow_version,
    verify_published_workflow_capability,
    workflow_execution_target_sha256,
)
from studio.services.workflow_smoke_fixtures import FIXTURE_SCHEMA_VERSION


User = get_user_model()


class PublishedWorkflowCapabilityIntegrityTests(TestCase):
    def setUp(self):
        self.user = User.objects.create_user(username="workflow-capability-integrity")
        self.provider = ProviderProfile(
            key="workflow-capability-provider",
            name="Workflow capability provider",
            driver_type=ProviderProfile.DriverType.COMFYUI,
            modalities="image",
            operations="health,object_info,submit,poll,cancel,collect",
        )
        assign_json_contract(self.provider, "driver_config", {})
        self.provider.save()
        self.profile = ModelProfileVersion(
            profile_key="workflow-capability-model",
            version=1,
            provider=self.provider,
            model_identifier="workflow-capability-model",
            modality=ModelProfileVersion.Modality.IMAGE,
            status=ModelProfileVersion.Status.PUBLISHED,
        )
        assign_json_contract(self.profile, "specification", {
            "schema_version": 1,
            "model_family": "workflow_capability_fixture",
            "capabilities": ["image.text_to_image"],
        })
        self.profile.save()
        self.deployment = ModelDeployment(
            key="workflow-capability-deployment",
            name="Workflow capability deployment",
            profile_version=self.profile,
            status=ModelDeployment.Status.ACTIVE,
        )
        assign_json_contract(self.deployment, "runtime_config", {})
        self.deployment.save()
        self.template = WorkflowTemplate.objects.create(
            name="Workflow capability image",
            slug="workflow-capability-image",
            output_kind=WorkflowTemplate.OutputKind.IMAGE,
            is_system=True,
            is_active=True,
        )

    def _published_workflow(self, *, historical_unprojected=False) -> WorkflowTemplateVersion:
        version = import_workflow_version(
            template=self.template,
            graph={
                "6": {
                    "class_type": "EmptyLatentImage",
                    "inputs": {"width": 352, "height": 608, "batch_size": 1},
                },
                "7": {
                    "class_type": "SaveImage",
                    "inputs": {"images": ["6", 0], "filename_prefix": "smoke"},
                },
            },
            input_contract={
                **workflow_projector_reference(),
                "schema_version": 3,
                "workflow_mode": "text_to_image",
                "capability": "image.text_to_image",
                "compiler": {"id": "generic.comfy", "version": 1},
                "required_dimension_multiple": 8,
                "parameters": {
                    "image.width": {
                        "type": "integer", "required": True, "multiple_of": 8,
                    },
                    "image.height": {
                        "type": "integer", "required": True, "multiple_of": 8,
                    },
                    "output.prefix": {"type": "string", "required": True},
                },
                "resolution_strategy": {
                    "mode": "explicit_width_height",
                    "node_ids": ["6"],
                },
                "outputs": {
                    "primary": {"node_id": "7", "kind": "image", "required": True},
                },
            },
            semantic_bindings=[
                {
                    "semantic_key": "image.width",
                    "node_id": "6",
                    "input_name": "width",
                    "value_type": "integer",
                    "is_required": True,
                },
                {
                    "semantic_key": "image.height",
                    "node_id": "6",
                    "input_name": "height",
                    "value_type": "integer",
                    "is_required": True,
                },
                {
                    "semantic_key": "output.prefix",
                    "node_id": "7",
                    "input_name": "filename_prefix",
                    "value_type": "string",
                    "is_required": True,
                },
            ],
            model_requirements=[],
            provider=self.provider,
            model_profile_version=self.profile,
        )
        if historical_unprojected:
            from studio.services.workflow_versions import _binding_rows, _requirement_rows
            from studio.services.workflow_compiler import workflow_execution_contract
            version.input_contract.pop("contract_projector")
            assign_json_contract(version, "input_contract", version.input_contract)
            _, version.execution_contract_sha256 = workflow_execution_contract(
                compiler_id=version.compiler_id, compiler_version=version.compiler_version,
                graph_sha256=version.graph_sha256, input_contract_sha256=version.input_contract_sha256,
                bindings=_binding_rows(version), requirements=_requirement_rows(version))
            version.save()
        target_sha256 = workflow_execution_target_sha256(version, self.deployment)
        target = _execution_target_evidence(version, self.deployment)
        validation_evidence = {
            "schema_version": 3,
            "deployment_id": str(self.deployment.id),
            "graph_sha256": version.graph_sha256,
            "execution_contract_sha256": version.execution_contract_sha256,
            "execution_target": target,
            "execution_target_sha256": target_sha256,
        }
        version.validated_deployment = self.deployment
        version.validation_state = WorkflowTemplateVersion.ValidationState.PASSED
        version.validation_evidence = validation_evidence
        version.validation_evidence_sha256 = json_contract(validation_evidence).sha256
        version.status = WorkflowTemplateVersion.Status.PUBLISHED
        version.smoke_test_state = WorkflowTemplateVersion.ValidationState.PASSED
        version.is_current = True
        version.save()

        reference_bindings = []
        empty_bindings_sha256 = json_contract(reference_bindings).sha256
        fixture_values = {
            "schema_version": FIXTURE_SCHEMA_VERSION,
            "reference_bindings": reference_bindings,
            "materialized_graph_sha256": version.graph_sha256,
        }
        fixture_contract = json_contract(
            fixture_values,
            schema_version=FIXTURE_SCHEMA_VERSION,
        )
        fixture_sha256 = fixture_contract.sha256
        now = timezone.now()
        attempt = WorkflowSmokeAttempt(
            workflow_version=version,
            deployment=self.deployment,
            created_by=self.user,
            status=WorkflowSmokeAttempt.Status.SUCCEEDED,
            graph_sha256=version.graph_sha256,
            execution_contract_sha256=version.execution_contract_sha256,
            validation_evidence_sha256=version.validation_evidence_sha256,
            target_sha256=target_sha256,
            fixture_values=fixture_values,
            fixture_values_schema_version=fixture_contract.schema_version,
            fixture_values_size_bytes=fixture_contract.size_bytes,
            fixture_values_sha256=fixture_contract.sha256,
            fixture_sha256=fixture_sha256,
            deadline_started_at=now,
            hard_deadline_at=now + timedelta(minutes=10),
            idle_deadline_at=now + timedelta(minutes=5),
            last_meaningful_progress_at=now,
        )
        attempt.result = {
            "schema_version": 3,
            "execution_submitted": True,
            "smoke_attempt_id": str(attempt.id),
            "fixture_sha256": fixture_sha256,
            "reference_bindings_sha256": empty_bindings_sha256,
            "asset_bindings_sha256": empty_bindings_sha256,
            "materialized_graph_sha256": version.graph_sha256,
            "compiled_graph_sha256": version.graph_sha256,
        }
        attempt.save()
        smoke_evidence = {
            "schema_version": 3,
            "execution_submitted": True,
            "smoke_attempt_id": str(attempt.id),
            "fixture_sha256": fixture_sha256,
            "reference_bindings_sha256": empty_bindings_sha256,
            "asset_bindings_sha256": empty_bindings_sha256,
            "materialized_graph_sha256": version.graph_sha256,
            "compiled_graph_sha256": version.graph_sha256,
            "deployment_id": str(self.deployment.id),
            "graph_sha256": version.graph_sha256,
            "execution_contract_sha256": version.execution_contract_sha256,
            "execution_target": target,
            "execution_target_sha256": target_sha256,
            "output_kind": "image",
            "output_count": 1,
            "outputs": [{"kind": "image", "sha256": "f" * 64}],
        }
        version.smoke_test_evidence = smoke_evidence
        version.smoke_test_evidence_sha256 = json_contract(smoke_evidence).sha256
        version.save(update_fields=(
            "smoke_test_evidence",
            "smoke_test_evidence_sha256",
            "updated_at",
        ))
        return version

    def test_legacy_success_flags_without_original_preparation_are_rejected(self):
        version = self._published_workflow()
        with self.assertRaises(ValidationError):
            verify_published_workflow_capability(version, self.deployment, operation="image.text_to_image")

    def test_passed_legacy_contract_cannot_bypass_projector_admission(self):
        from studio.services.workflow_compiler import compile_frozen_workflow_values, WorkflowCompilerError
        from studio.services.workflow_versions import workflow_version_execution_snapshot, resolve_workflow_canvas_export_deployment

        version = self._published_workflow(historical_unprojected=True)
        with self.assertRaises(ValidationError) as rejected:
            verify_published_workflow_capability(version, self.deployment, operation="image.text_to_image")
        self.assertEqual(rejected.exception.code, "workflow_integrity_invalid")
        self.assertIn("Projector", str(rejected.exception.__cause__))
        with self.assertRaisesRegex(WorkflowCompilerError, "缺少冻结的 Projector"):
            compile_frozen_workflow_values(workflow_snapshot=workflow_version_execution_snapshot(version),
                scalar_values={}, resolve_asset=lambda *_: self.fail("Validate the frozen Projector first"),
                classified_groups=None, classified_bindings=[], output_kind="image", total_duration_ms=None)

        with self.assertRaisesRegex(ValidationError, "缺少冻结的 Projector"):
            resolve_workflow_canvas_export_deployment(version)

    def test_current_import_rejects_missing_projector_before_writing(self):
        from studio.services.workflow_versions import _binding_rows, _requirement_rows
        version = self._published_workflow()
        count = WorkflowTemplateVersion.objects.count()
        contract = dict(version.input_contract)
        contract.pop("contract_projector")
        with self.assertRaisesRegex(ValidationError, "缺少冻结的 Projector"):
            import_workflow_version(template=self.template, graph=version.graph, input_contract=contract,
                semantic_bindings=_binding_rows(version), model_requirements=_requirement_rows(version),
                provider=self.provider, model_profile_version=self.profile)
        self.assertEqual(WorkflowTemplateVersion.objects.count(), count)

    def test_passed_state_cannot_mask_graph_integrity_drift(self):
        version = self._published_workflow()
        WorkflowTemplateVersion.objects.filter(pk=version.pk).update(
            graph={"7": {"class_type": "SaveImage", "inputs": {"filename_prefix": "drift"}}},
        )
        version.refresh_from_db()

        with self.assertRaises(ValidationError) as raised:
            verify_published_workflow_capability(
                version,
                self.deployment,
                operation="image.text_to_image",
            )

        self.assertEqual(raised.exception.code, "workflow_integrity_invalid")

    def test_current_deployment_config_hash_drift_invalidates_smoke(self):
        version = self._published_workflow()
        assign_json_contract(self.deployment, "runtime_config", {"port": 8189})
        self.deployment.save(update_fields=(
            "runtime_config",
            "runtime_config_schema_version",
            "runtime_config_size_bytes",
            "runtime_config_sha256",
            "updated_at",
        ))

        with self.assertRaises(ValidationError) as raised:
            verify_published_workflow_capability(
                version,
                self.deployment,
                operation="image.text_to_image",
            )

        self.assertEqual(raised.exception.code, "workflow_target_drifted")

    def test_old_or_unrelated_smoke_attempt_is_not_admitted(self):
        version = self._published_workflow()
        version.smoke_test_evidence = {
            **version.smoke_test_evidence,
            "smoke_attempt_id": str(uuid4()),
        }
        version.smoke_test_evidence_sha256 = json_contract(
            version.smoke_test_evidence,
        ).sha256
        version.save(update_fields=(
            "smoke_test_evidence",
            "smoke_test_evidence_sha256",
            "updated_at",
        ))

        with self.assertRaises(ValidationError) as raised:
            verify_published_workflow_capability(
                version,
                self.deployment,
                operation="image.text_to_image",
            )

        self.assertEqual(raised.exception.code, "workflow_smoke_missing_or_stale")

    def test_operation_must_match_frozen_workflow_capability(self):
        version = self._published_workflow()

        with self.assertRaises(ValidationError) as raised:
            verify_published_workflow_capability(
                version,
                self.deployment,
                operation="image.image_to_image",
            )

        self.assertEqual(raised.exception.code, "workflow_operation_mismatch")

    def test_empty_inventory_selectors_do_not_change_execution_target_shape(self):
        version = self._published_workflow()
        artifact = ModelArtifact.objects.create(
            profile_version=self.profile,
            role=ModelArtifact.Role.DIFFUSION_MODEL,
            logical_name="fixture-model",
            filename="fixture.safetensors",
            inventory_folder="",
            inventory_node_type="",
            inventory_input_name="",
            sha256="a" * 64,
            status=ModelArtifact.Status.AVAILABLE,
        )
        WorkflowModelRequirement.objects.create(
            workflow_version=version,
            role=ModelArtifact.Role.DIFFUSION_MODEL,
            alias="fixture-model",
            artifact=artifact,
            expected_filename=artifact.filename,
            expected_sha256=artifact.sha256,
            is_required=True,
        )

        row = _execution_target_evidence(
            version,
            self.deployment,
        )["required_artifacts"][0]

        self.assertNotIn("artifact_inventory_folder", row)
        self.assertNotIn("artifact_inventory_node_type", row)
        self.assertNotIn("artifact_inventory_input_name", row)

        ModelArtifact.objects.filter(pk=artifact.pk).update(
            inventory_folder="diffusion_models",
        )
        row = _execution_target_evidence(
            version,
            self.deployment,
        )["required_artifacts"][0]
        self.assertEqual(row["artifact_inventory_folder"], "diffusion_models")
        self.assertEqual(row["artifact_inventory_node_type"], "")
        self.assertEqual(row["artifact_inventory_input_name"], "")
