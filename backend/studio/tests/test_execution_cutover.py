"""One-off cutover on synthetic 0144 data, never on the running database."""
import hashlib
import tempfile
from pathlib import Path
from unittest.mock import patch
from uuid import uuid4

from django.apps import apps
from django.core.exceptions import SuspiciousFileOperation
from django.core.files.uploadedfile import SimpleUploadedFile
from django.core.management import call_command
from django.core.management.base import CommandError
from django.db import connection
from django.db.migrations.executor import MigrationExecutor
from django.test import SimpleTestCase, TestCase, override_settings

from studio.management.execution_cutover import (
    CONFIRMATION, _owned_file, build_execution_cutover_plan, execute_execution_cutover,
)
from studio.models import Asset
from studio.services.configuration import resolve_configuration
from studio.tests.test_configuration_migrations import _MigrationTestCase
from studio.tests import test_execution_cutover_rehearsal as rehearsal


class ExecutionCutoverEntryTests(SimpleTestCase):
    def test_no_implicit_execute_or_mixed_maintenance_scope(self):
        for options in ({"execution_cutover": True, "execute": True},
                {"execution_cutover": True, "backup_manifest": "no"},
                {"execution_cutover": True, "confirmation": CONFIRMATION}, {"operator": "user"}):
            with self.subTest(options=options), self.assertRaises(CommandError):
                call_command("purge_incompatible_workflows", **options)


class CutoverOwnedFileTests(TestCase):
    def setUp(self):
        temporary = tempfile.TemporaryDirectory(prefix="autoprompt-cutover-file-test-")
        self.addCleanup(temporary.cleanup)
        media = override_settings(MEDIA_ROOT=temporary.name)
        media.enable()
        self.addCleanup(media.disable)
        content = b"Synthetic owned script, not a real user asset."
        digest = hashlib.sha256(content).hexdigest()
        self.asset = Asset.objects.create(kind="script", name="Isolated owned output",
            file=SimpleUploadedFile("cutover-script.txt", content), checksum=digest,
            metadata={"size_bytes": len(content), "generated_sha256": digest})
        self.selected = {Asset: {self.asset.pk}}

    def test_file_preview_verifies_without_removing_physical_file(self):
        fact = _owned_file(self.asset, self.selected, apps)
        self.assertEqual(fact["sha256"], self.asset.checksum)
        self.assertTrue(fact["path"].is_file())

    def test_shared_file_and_json_provenance_are_not_deleted(self):
        shared = Asset.objects.create(kind="script", name="Shared file", file=self.asset.file.name)
        with self.assertRaisesMessage(CommandError, "共享"):
            _owned_file(self.asset, self.selected, apps)
        shared.file = ""
        shared.metadata = {"source_file": self.asset.file.name}
        shared.save()
        with self.assertRaisesMessage(CommandError, "JSON引用"):
            _owned_file(self.asset, self.selected, apps)
        self.assertTrue(Path(self.asset.file.path).is_file())

    def test_modified_bytes_are_not_silently_deleted(self):
        Path(self.asset.file.path).write_bytes(b"Changed fixture")
        with self.assertRaisesMessage(CommandError, "校验信息不一致"):
            _owned_file(self.asset, self.selected, apps)

    def test_path_outside_media_root_is_rejected(self):
        self.asset.file.name = "../outside.txt"
        with self.assertRaises(SuspiciousFileOperation):
            _owned_file(self.asset, self.selected, apps)


class ExecutionCutoverTests(_MigrationTestCase):
    migrate_from = "0144_execution_policy_configuration"
    migrate_to = "0170_prompt_profile_current_schema_default"
    migrate_to_in_setup = False
    def set_up_before_migration(self, apps):
        rehearsal.ExecutionConfigurationCutoverRehearsalTests.set_up_before_migration(self, apps)
        # Bootstrap migrations supply four deliberately incomplete workflow
        # examples. They are not the validated resources in the reviewed
        # running database; this fixture tests configuration-only cutover.
        apps.get_model("studio", "WorkflowTemplateVersion").objects.all().delete()
        # These migration seeds really are obsolete under the schema-6-only
        # runtime. Include them in the reviewed deletion set, not a bypass of
        # native contract checks or a silent conversion to the current schema.
        self.obsolete_prompt_ids = list(map(str, apps.get_model(
            "studio", "PromptProfileVersion").objects.exclude(
                contract_schema_version=7).values_list("pk", flat=True)))

    def plan(self):
        return build_execution_cutover_plan(self.obsolete_prompt_ids)

    def execute(self, plan, **overrides):
        return execute_execution_cutover(**{
            "prompt_ids": plan.report["obsolete_prompt_version_ids"], "expected_sha256": plan.report["plan_sha256"],
            "confirmation": CONFIRMATION, "operator": self.actor.username, **overrides})

    def test_full_cutover_uses_formal_configuration_and_then_migrates(self):
        plan = self.plan()
        report = self.execute(plan)
        self.assertFalse(report["backup_created"])
        self.assertFalse(report["migrations_applied"])
        self.assertEqual(report["model_calls"], 0)
        expected = {key: value for key, value in self.values.items() if key not in
            ("production.prompt_normalization_deployment_id", "production.prompt_language_gate",
             "runtime.deferred_generation_retry_backoff_seconds")}
        self.assertEqual(resolve_configuration().values, expected)
        executor = MigrationExecutor(connection)
        executor.migrate([("studio", self.migrate_to)])
        self.assertEqual(resolve_configuration().values, {**expected, rehearsal.migration.RECOMMENDATION_KEY: ""})
        registry = executor.loader.project_state([("studio", self.migrate_to)]).apps
        self.assertFalse(registry.get_model("studio", "ModelExecutionInstanceVersion").objects.exists())
        self.assertFalse(registry.get_model("studio", "ModelDeploymentSmokeAttempt").objects.exists())

    def test_drift_aborts_before_any_deletion(self):
        plan = self.plan()
        self.source.change_summary = "Changed after review"
        self.source.save()
        before = list(self.Revision.objects.order_by("pk").values())
        with self.assertRaisesMessage(CommandError, "重新预览"):
            self.execute(plan)
        self.assertEqual(list(self.Revision.objects.order_by("pk").values()), before)

    def test_current_valid_prompt_must_not_be_deleted_by_operator_guess(self):
        # Exercise disagreement after formal baseline publication; all prior
        # deletions and the new baseline must roll back together.
        plan = self.plan()
        before = list(self.Revision.objects.order_by("pk").values())
        with patch("studio.management.execution_cutover._native_resource_checks", return_value={
                "status": "checked", "resources": {"PromptProfileVersion": [
                    {"id": "unexpected-incompatible-resource", "contract_check": "failed"}]}}), self.assertRaisesMessage(
                CommandError, "策略集合不一致"):
            self.execute(plan)
        self.assertEqual(list(self.Revision.objects.order_by("pk").values()), before)
        self.assertTrue(self.Definition.objects.filter(key="production.prompt_normalization_deployment_id").exists())

    def test_retained_resource_json_reference_is_not_cascaded(self):
        model = self.apps.get_model("studio", "ModelProfileVersion")
        row = model.objects.first()
        row.specification = {"source": str(self.source.pk)}
        row.save()
        with self.assertRaisesMessage(CommandError, "保留对象含旧引用"):
            self.plan()
        self.assertTrue(model.objects.filter(pk=row.pk).exists())

    def test_unreviewed_workflow_failure_rolls_back_instead_of_auto_deleting_it(self):
        workflow = self.apps.get_model("studio", "WorkflowTemplateVersion")
        template = self.apps.get_model("studio", "WorkflowTemplate").objects.first()
        invalid = workflow.objects.create(template=template, version=1)
        plan = self.plan()
        with self.assertRaisesMessage(CommandError, "WorkflowTemplateVersion"):
            self.execute(plan)
        self.assertTrue(workflow.objects.filter(pk=invalid.pk).exists())
        self.assertTrue(self.Revision.objects.filter(pk=self.source.pk).exists())

    def test_old_audit_closure_is_removed_without_touching_unrelated_audit(self):
        audit = self.apps.get_model("studio", "PlatformLifecycleAuditEvent")
        old = audit.objects.create(action="create", object_type="config_revision", object_id=str(self.source.pk),
            reason_code="isolated_old_config", before_summary={"source": str(self.source.pk)})
        unrelated = audit.objects.create(action="create", object_type="test", object_id=uuid4(),
            reason_code="isolated_unrelated")
        plan = self.plan()
        self.assertIn(old.pk, plan.selected[plan.registry.get_model("studio", "PlatformLifecycleAuditEvent")])
        self.execute(plan)
        self.assertFalse(audit.objects.filter(pk=old.pk).exists())
        self.assertTrue(audit.objects.filter(pk=unrelated.pk).exists())
