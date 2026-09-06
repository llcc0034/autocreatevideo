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


class WorkflowModeRetirementMigrationTests(_MigrationTestCase):
    migrate_from = "0094_queue_capacity_advisory_contract"
    migrate_to = "0095_retire_workflows_without_explicit_mode"

    def set_up_before_migration(self, apps):
        User = apps.get_model("auth", "User")
        Template = apps.get_model("studio", "WorkflowTemplate")
        Version = apps.get_model("studio", "WorkflowTemplateVersion")
        publisher = User.objects.create(username="workflow-mode-migration-publisher")

        def create_version(*, slug, input_contract, status, is_current):
            template = Template.objects.create(
                name=slug,
                slug=slug,
                version=1,
                provider="comfyui",
                output_kind="video",
                definition={},
                input_schema={},
                is_system=True,
                is_active=True,
            )
            graph = {"1": {"class_type": "SaveVideo", "inputs": {}}}
            graph_size, graph_sha = _contract(graph)
            input_size, input_sha = _contract(input_contract)
            return Version.objects.create(
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

        self.invalid_id = create_version(
            slug="migration-workflow-missing-mode",
            input_contract={
                "schema_version": 3,
                "compiler": {"id": "generic.comfy", "version": 1},
            },
            status="published",
            is_current=True,
        ).id
        self.valid_id = create_version(
            slug="migration-workflow-explicit-mode",
            input_contract={
                "schema_version": 3,
                "workflow_mode": "reference_to_video",
                "compiler": {"id": "generic.comfy", "version": 1},
            },
            status="published",
            is_current=True,
        ).id
        self.draft_id = create_version(
            slug="migration-workflow-draft-missing-mode",
            input_contract={"schema_version": 3},
            status="draft",
            is_current=False,
        ).id

    def test_missing_mode_is_retired_without_rewriting_historical_evidence(self):
        Version = self.apps.get_model("studio", "WorkflowTemplateVersion")
        invalid = Version.objects.get(pk=self.invalid_id)

        self.assertEqual(invalid.status, "retired")
        self.assertFalse(invalid.is_current)
        self.assertEqual(invalid.validation_state, "passed")
        self.assertEqual(invalid.smoke_test_state, "passed")
        self.assertEqual(invalid.validation_evidence, {"historical": "validation"})
        self.assertEqual(invalid.validation_evidence_sha256, "d" * 64)
        self.assertEqual(invalid.smoke_test_evidence, {"historical": "smoke"})
        self.assertEqual(invalid.smoke_test_evidence_sha256, "e" * 64)

    def test_explicit_mode_published_row_and_unpublished_draft_are_unchanged(self):
        Version = self.apps.get_model("studio", "WorkflowTemplateVersion")
        valid = Version.objects.get(pk=self.valid_id)
        draft = Version.objects.get(pk=self.draft_id)

        self.assertEqual((valid.status, valid.is_current), ("published", True))
        self.assertEqual((draft.status, draft.is_current), ("draft", False))
