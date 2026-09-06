from __future__ import annotations

from io import StringIO
from types import SimpleNamespace
from unittest.mock import patch
from uuid import uuid4

from django.core.management.base import CommandError
from django.core.management import call_command
from django.test import SimpleTestCase, TestCase

from studio.management.commands import purge_incompatible_workflows as purge
from studio.models import (
    GenerationAttempt, ScriptRevision, SeriesProject,
    WorkflowTemplate, WorkflowTemplateVersion,
    WorkflowProjector, WorkflowProjectorVersion,
)


class _OrderedVersions:
    def __init__(self, versions):
        self.versions = versions

    def order_by(self, *_fields):
        return self.versions


class PurgeIncompatibleWorkflowsSafetyTests(SimpleTestCase):
    def test_reverse_dependency_discovery_includes_protected_one_to_one(self):
        incoming = {
            (model, field.name)
            for model, field in purge._incoming_fields(GenerationAttempt)
        }

        self.assertIn(
            (ScriptRevision, "origin_generation_attempt"),
            incoming,
        )

    def test_shared_upstream_rows_fail_closed(self):
        with self.assertRaises(CommandError):
            purge._assert_no_preserved_shared_rows({
                SeriesProject: {uuid4()},
            })

    def test_unexpected_contract_check_error_is_not_classified_as_incompatible(self):
        version = SimpleNamespace(
            id=uuid4(),
            template=SimpleNamespace(slug="unexpected-error"),
            version=1,
            status="draft",
            validation_state="not_run",
        )
        queryset = _OrderedVersions([version])

        with (
            patch.object(
                purge.WorkflowTemplateVersion.objects,
                "select_related",
                return_value=queryset,
            ),
            patch.object(
                purge,
                "verify_workflow_version_integrity",
                side_effect=RuntimeError("database unavailable"),
            ),
        ):
            with self.assertRaisesRegex(RuntimeError, "database unavailable"):
                purge._incompatible_workflow_versions()


class ScopedWorkflowPurgeTests(TestCase):
    def setUp(self):
        self.target = self._version("purge-target")
        self.other = self._version("purge-unrelated")
        self.original_ids = set(WorkflowTemplateVersion.objects.values_list("pk", flat=True))

    @staticmethod
    def _version(slug):
        template = WorkflowTemplate.objects.create(slug=slug, name=slug)
        # Deliberately invalid historical contracts, not executable fixtures.
        return WorkflowTemplateVersion.objects.create(template=template, version=1)

    def test_preview_only_selects_the_explicit_version(self):
        plan = purge.build_purge_plan(frozenset({self.target.pk}))
        self.assertEqual(plan.selected[WorkflowTemplateVersion], {self.target.pk})
        self.assertEqual(
            {item["id"] for item in plan.kept_versions},
            {str(pk) for pk in self.original_ids - {self.target.pk}},
        )
        self.assertNotIn(self.other.template_id, plan.selected[WorkflowTemplate])
        self.assertTrue(WorkflowTemplateVersion.objects.filter(pk=self.target.pk).exists())

    def test_empty_scope_cannot_become_global_cleanup(self):
        with self.assertRaisesRegex(CommandError, "范围不能为空"):
            purge.build_purge_plan(frozenset())

    def test_unknown_uuid_fails_before_preview_or_deletion(self):
        with self.assertRaisesRegex(CommandError, "版本不存在"):
            call_command("purge_incompatible_workflows", workflow_version=[uuid4()], stdout=StringIO())
        self.assertEqual(set(WorkflowTemplateVersion.objects.values_list("pk", flat=True)), self.original_ids)

    def test_dependency_cannot_expand_to_another_workflow(self):
        self.other.input_contract = {"source_workflow_id": str(self.target.pk)}
        self.other.save(update_fields=("input_contract",))
        with self.assertRaisesRegex(CommandError, "未指定的工作流版本"):
            purge.build_purge_plan(frozenset({self.target.pk}))

    def test_plan_hash_changes_with_scope(self):
        target = purge.build_purge_plan(frozenset({self.target.pk}))
        all_versions = purge.build_purge_plan()
        self.assertNotEqual(target.plan_sha256, all_versions.plan_sha256)

    def test_execute_keeps_unrelated_invalid_version(self):
        plan = purge.build_purge_plan(frozenset({self.target.pk}))
        with patch.object(purge, "_validate_backup_manifest", return_value={"verified": True}):
            call_command(
                "purge_incompatible_workflows", workflow_version=[self.target.pk],
                execute=True, expected_plan_sha256=plan.plan_sha256,
                confirmation=plan.confirmation, backup_manifest="test/manifest.json",
                stdout=StringIO(),
            )
        self.assertFalse(WorkflowTemplateVersion.objects.filter(pk=self.target.pk).exists())
        self.assertTrue(WorkflowTemplateVersion.objects.filter(pk=self.other.pk).exists())
        self.assertTrue(WorkflowTemplate.objects.filter(pk=self.other.template_id).exists())
        self.assertEqual(purge.build_purge_plan(frozenset({self.target.pk})).incompatible_versions, [])

    def test_explicit_irreversible_delete_is_scoped_and_needs_plan_confirmation(self):
        plan = purge.build_purge_plan(frozenset({self.target.pk}))
        with self.assertRaises(CommandError):
            call_command("purge_incompatible_workflows", workflow_version=[self.target.pk], execute=True,
                         irreversible_without_backup=True, stdout=StringIO())
        self.assertTrue(WorkflowTemplateVersion.objects.filter(pk=self.target.pk).exists())
        call_command("purge_incompatible_workflows", workflow_version=[self.target.pk], execute=True,
                     expected_plan_sha256=plan.plan_sha256, confirmation=plan.confirmation,
                     irreversible_without_backup=True, stdout=StringIO())
        self.assertFalse(WorkflowTemplateVersion.objects.filter(pk=self.target.pk).exists())
        self.assertTrue(WorkflowTemplateVersion.objects.filter(pk=self.other.pk).exists())

    def test_irreversible_option_cannot_delete_unscoped_catalog(self):
        plan = purge.build_purge_plan()
        with self.assertRaisesRegex(CommandError, "精确工作流范围"):
            call_command("purge_incompatible_workflows", execute=True, irreversible_without_backup=True,
                         expected_plan_sha256=plan.plan_sha256, confirmation=plan.confirmation, stdout=StringIO())
        self.assertTrue(WorkflowTemplateVersion.objects.filter(pk=self.target.pk).exists())

    def test_obsolete_projector_cleanup_rejects_supported_engine_and_shared_workflow(self):
        family = WorkflowProjector.objects.create(key="purge.old", name="old")
        projector = WorkflowProjectorVersion.objects.create(projector=family, version=1, engine_key="generic.fixed", engine_version=1)
        with self.assertRaisesRegex(CommandError, "仍受支持"):
            purge.build_purge_plan(frozenset({self.target.pk}), frozenset({projector.pk}))
        projector.engine_key = "retired.test"
        projector.save(update_fields=("engine_key",))
        self.other.input_contract = {"contract_projector": {"version_id": str(projector.pk)}}
        self.other.save(update_fields=("input_contract",))
        with self.assertRaisesRegex(CommandError, "范围外工作流"):
            purge.build_purge_plan(frozenset({self.target.pk}), frozenset({projector.pk}))
        self.other.input_contract = {}
        self.other.save(update_fields=("input_contract",))
        plan = purge.build_purge_plan(frozenset({self.target.pk}), frozenset({projector.pk}))
        self.assertEqual(plan.selected[WorkflowProjectorVersion], {projector.pk})
        self.assertEqual(plan.selected[WorkflowProjector], {family.pk})
