from __future__ import annotations

import hashlib
import json

from django.utils import timezone

from studio.tests.test_configuration_migrations import _MigrationTestCase


def _contract(value):
    payload = json.dumps(
        value,
        ensure_ascii=False,
        sort_keys=True,
        separators=(",", ":"),
        allow_nan=False,
    ).encode("utf-8")
    return len(payload), hashlib.sha256(payload).hexdigest()


class WorkflowDimensionLatticeMigrationTests(_MigrationTestCase):
    migrate_from = "0126_configurable_canonical_context_and_provider_limits"
    migrate_to = "0127_retire_workflows_without_frozen_dimension_lattice"

    def set_up_before_migration(self, apps):
        User = apps.get_model("auth", "User")
        Template = apps.get_model("studio", "WorkflowTemplate")
        Version = apps.get_model("studio", "WorkflowTemplateVersion")
        Binding = apps.get_model("studio", "WorkflowSemanticBinding")
        publisher = User.objects.create(username="dimension-lattice-migration-publisher")

        def create_version(
            *,
            slug,
            input_contract,
            status="published",
            is_current=True,
            width=512,
            height=512,
        ):
            template = Template.objects.create(
                name=slug,
                slug=slug,
                version=1,
                provider="comfyui",
                output_kind="image",
                definition={},
                input_schema={},
                is_system=True,
                is_active=True,
            )
            graph = {
                "3": {
                    "class_type": "EmptyLatentImage",
                    "inputs": {"width": width, "height": height, "batch_size": 1},
                },
                "4": {
                    "class_type": "SaveImage",
                    "inputs": {"images": ["3", 0], "filename_prefix": "output"},
                },
            }
            graph_size, graph_sha = _contract(graph)
            input_size, input_sha = _contract(input_contract)
            version = Version.objects.create(
                template=template,
                version=1,
                graph=graph,
                graph_schema_version=1,
                graph_size_bytes=graph_size,
                graph_sha256=graph_sha,
                input_contract=input_contract,
                input_contract_schema_version=1,
                input_contract_size_bytes=input_size,
                input_contract_sha256=input_sha,
                compiler_id="generic.comfy",
                compiler_version=1,
                semantic_bindings_sha256="a" * 64,
                model_requirements_sha256="b" * 64,
                execution_contract_sha256="c" * 64,
                status=status,
                validation_state="passed",
                validation_message="historical validation evidence",
                smoke_test_state="passed",
                smoke_test_message="historical real smoke evidence",
                validation_evidence={"historical": "validation"},
                validation_evidence_sha256="d" * 64,
                smoke_test_evidence={"historical": "smoke"},
                smoke_test_evidence_sha256="e" * 64,
                is_current=is_current,
                published_at=timezone.now() if status == "published" else None,
                published_by_id=publisher.id if status == "published" else None,
            )
            for semantic_key, input_name in (
                ("image.width", "width"),
                ("image.height", "height"),
            ):
                Binding.objects.create(
                    workflow_version=version,
                    semantic_key=semantic_key,
                    node_id="3",
                    input_name=input_name,
                    value_type="integer",
                    is_required=True,
                )
            return version

        base_parameters = {
            "image.width": {
                "type": "integer", "required": True, "multiple_of": 8,
                "default": 512,
            },
            "image.height": {
                "type": "integer", "required": True, "multiple_of": 8,
                "default": 512,
            },
        }
        self.invalid_published_id = create_version(
            slug="migration-workflow-missing-dimension-lattice",
            input_contract={
                "schema_version": 2,
                "workflow_mode": "text_to_image",
                "compiler": {"id": "generic.comfy", "version": 1},
                "parameters": base_parameters,
            },
        ).id
        self.valid_v2_id = create_version(
            slug="migration-workflow-proven-v2-dimension-lattice",
            input_contract={
                "schema_version": 2,
                "workflow_mode": "text_to_image",
                "compiler": {"id": "generic.comfy", "version": 1},
                "required_dimension_multiple": 8,
                "parameters": base_parameters,
            },
        ).id
        self.invalid_validated_id = create_version(
            slug="migration-workflow-validated-missing-dimension-lattice",
            input_contract={
                "schema_version": 3,
                "workflow_mode": "text_to_image",
                "compiler": {"id": "generic.comfy", "version": 1},
                "resolution_strategy": {
                    "mode": "explicit_width_height", "node_ids": ["3"],
                },
                "parameters": base_parameters,
            },
            status="validated",
            is_current=False,
        ).id
        misaligned_parameters = json.loads(json.dumps(base_parameters))
        misaligned_parameters["image.width"]["default"] = 513
        self.misaligned_published_id = create_version(
            slug="migration-workflow-misaligned-dimension-default",
            input_contract={
                "schema_version": 3,
                "workflow_mode": "text_to_image",
                "compiler": {"id": "generic.comfy", "version": 1},
                "resolution_strategy": {
                    "mode": "explicit_width_height", "node_ids": ["3"],
                },
                "required_dimension_multiple": 8,
                "parameters": misaligned_parameters,
            },
        ).id

    def test_unproven_published_workflows_are_retired_without_rewriting_evidence(self):
        Version = self.apps.get_model("studio", "WorkflowTemplateVersion")
        for version_id in (
            self.invalid_published_id,
            self.misaligned_published_id,
        ):
            version = Version.objects.get(pk=version_id)
            self.assertEqual((version.status, version.is_current), ("retired", False))
            self.assertEqual(version.validation_state, "passed")
            self.assertEqual(version.smoke_test_state, "passed")
            self.assertEqual(version.validation_evidence, {"historical": "validation"})
            self.assertEqual(version.smoke_test_evidence, {"historical": "smoke"})

    def test_proven_schema_v2_lattice_remains_admitted(self):
        Version = self.apps.get_model("studio", "WorkflowTemplateVersion")
        version = Version.objects.get(pk=self.valid_v2_id)
        self.assertEqual((version.status, version.is_current), ("published", True))

    def test_unproven_validated_workflow_returns_to_review(self):
        Version = self.apps.get_model("studio", "WorkflowTemplateVersion")
        version = Version.objects.get(pk=self.invalid_validated_id)
        self.assertEqual((version.status, version.is_current), ("draft", False))
        self.assertEqual(version.validation_state, "not_run")
        self.assertEqual(version.smoke_test_state, "not_run")
        self.assertEqual(version.validation_evidence, {})
        self.assertEqual(version.smoke_test_evidence, {})
        self.assertIn("冻结尺寸倍数合同", version.validation_message)
