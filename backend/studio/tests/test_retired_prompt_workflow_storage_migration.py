"""Explicit retired-data cleanup in an isolated historical schema only."""
from django.db import connection
from django.db.migrations.executor import MigrationExecutor

from studio.tests.test_configuration_migrations import _MigrationTestCase


class RetiredPromptWorkflowStorageMigrationTests(_MigrationTestCase):
    migrate_from = "0167_remove_retired_prompt_bindings"
    migrate_to = "0168_remove_retired_prompt_workflow_storage"
    migrate_to_in_setup = False

    def set_up_before_migration(self, apps):
        self.old_apps = apps
        self.user = apps.get_model("auth", "User").objects.create(username="retired-storage-owner")
        self.project = apps.get_model("studio", "SeriesProject").objects.create(
            owner=self.user, title="Preserved project", language="zh", aspect_ratio="16:9",
            width=640, height=360, fps=24, default_shot_duration_ms=1000, continuity_seed=1)
        self.before = apps.get_model("studio", "SeriesProject").objects.filter(pk=self.project.pk).values().get()
        self.template = apps.get_model("studio", "WorkflowTemplate").objects.create(
            slug="preserved-storage-template", name="Preserved template")
        audit = apps.get_model("studio", "PlatformLifecycleAuditEvent")
        self.old_event = audit.objects.create(action="create", object_type="prompt_artifact",
            object_id=self.project.pk, reason_code="isolated_retired_record")
        self.shared_event = audit.objects.create(action="create", object_type="workflow_template",
            object_id=self.template.pk, reason_code="isolated_shared_resource")

    def prompt(self):
        return self.old_apps.get_model("studio", "PromptArtifact").objects.create(
            project=self.project, created_by=self.user, is_multi_shot=False, include_audio=False,
            total_duration_ms=1000, prompt_type="video", prompt_text="retired fixture")

    def migrate(self):
        executor = MigrationExecutor(connection)
        executor.migrate([("studio", self.migrate_to)])
        self.apps = executor.loader.project_state([("studio", self.migrate_to)]).apps

    def test_retired_prompt_blocks_before_audit_or_shared_data_is_deleted(self):
        prompt = self.prompt()
        with self.assertRaisesRegex(RuntimeError, "PromptArtifact.*R7"):
            self.migrate()
        self.assertTrue(self.old_apps.get_model("studio", "PromptArtifact").objects.filter(pk=prompt.pk).exists())
        self.assertTrue(self.old_apps.get_model("studio", "PlatformLifecycleAuditEvent").objects.filter(pk=self.old_event.pk).exists())
        self.assertEqual(self.old_apps.get_model("studio", "SeriesProject").objects.filter(pk=self.project.pk).values().get(), self.before)

    def test_retired_workflow_blocks_without_repointing_to_current_preparation(self):
        workflow = self.old_apps.get_model("studio", "GeneratedWorkflow").objects.create(
            project=self.project, prompt_artifact=self.prompt(), template=self.template, created_by=self.user)
        with self.assertRaisesRegex(RuntimeError, "GeneratedWorkflow.*R7"):
            self.migrate()
        self.assertTrue(self.old_apps.get_model("studio", "GeneratedWorkflow").objects.filter(pk=workflow.pk).exists())
        self.assertFalse(self.old_apps.get_model("studio", "ExecutionPreparation").objects.exists())

    def test_empty_retired_storage_is_removed_without_recreating_execution_or_shared_data(self):
        self.migrate()
        for name in ("PromptArtifact", "GeneratedWorkflow"):
            with self.subTest(model=name), self.assertRaises(LookupError):
                self.apps.get_model("studio", name)
            self.assertNotIn("studio_" + name.lower(), connection.introspection.table_names())
        self.assertEqual(self.apps.get_model("studio", "SeriesProject").objects.filter(pk=self.project.pk).values().get(), self.before)
        self.assertTrue(self.apps.get_model("studio", "WorkflowTemplate").objects.filter(pk=self.template.pk).exists())
        audit = self.apps.get_model("studio", "PlatformLifecycleAuditEvent")
        self.assertFalse(audit.objects.filter(pk=self.old_event.pk).exists())
        self.assertTrue(audit.objects.filter(pk=self.shared_event.pk).exists())
        self.assertFalse(self.apps.get_model("studio", "ExecutionPreparation").objects.exists())
