from django.db import connection
from django.db.migrations.executor import MigrationExecutor

from studio.tests.test_configuration_migrations import _MigrationTestCase
from studio.tests.test_execution_context_inputs import PROJECT_CONFIG


class WorkflowSourceTargetMigrationTests(_MigrationTestCase):
    migrate_from = "0164_remove_deferred_generation_request"
    migrate_to = "0165_remove_legacy_workflow_source_target"
    migrate_to_in_setup = False

    def set_up_before_migration(self, apps):
        actor = apps.get_model("auth", "User").objects.create(username="source-target-migration")
        project = apps.get_model("studio", "SeriesProject").objects.create(
            owner=actor, title="Source migration", **PROJECT_CONFIG)
        prompt = apps.get_model("studio", "PromptArtifact").objects.create(project=project, created_by=actor,
            prompt_type="video", prompt_text="Retired", is_multi_shot=False, include_audio=False, total_duration_ms=5000)
        template = apps.get_model("studio", "WorkflowTemplate").objects.create(
            owner=actor, name="Shared", slug="source-shared", output_kind="video")
        graph = apps.get_model("studio", "GeneratedWorkflow").objects.create(
            project=project, prompt_artifact=prompt, template=template, created_by=actor,
            workflow_json={"fixture": "old graph"})
        profile = apps.get_model("studio", "ModelProfileVersion").objects.get(profile_key="sdxl-image", version=1)
        deployment = apps.get_model("studio", "ModelDeployment").objects.create(
            key="source-migration", name="Shared resource", profile_version=profile)
        task = apps.get_model("studio", "GenerationTask").objects.create(
            owner=actor, project=project, kind="video", status="succeeded")
        source_model = apps.get_model("studio", "ComfyWorkflowSource")
        common = dict(source_deployment=deployment, imported_by=actor, source_scope_sha256="a" * 64,
            source_size_bytes=2, source_sha256="b" * 64, import_kind="execution_snapshot",
            conversion_report={"fixture": "do not recompute"}, conversion_report_sha256="c" * 64)
        self.old_source = source_model.objects.create(**common, source_path="old.json", source_path_key="old.json",
            source_path_sha256="d" * 64, generated_workflow=graph)
        self.native_source = source_model.objects.create(**common, source_path="native.json", source_path_key="native.json",
            source_path_sha256="e" * 64, generation_task=task)
        self.native_before = source_model.objects.filter(pk=self.native_source.pk).values().get()
        self.graph_id, self.task_id = graph.pk, task.pk

    def test_legacy_links_stop_migration_before_schema_loss_and_exact_cleanup_unblocks_it(self):
        with self.assertRaisesRegex(RuntimeError, "R7清单"):
            MigrationExecutor(connection).migrate([("studio", self.migrate_to)])
        self.assertTrue(self.apps.get_model("studio", "ComfyWorkflowSource").objects.filter(pk=self.old_source.pk).exists())
        with connection.cursor() as cursor:
            self.assertIn("generated_workflow_id", {row.name for row in connection.introspection.get_table_description(cursor, "studio_comfyworkflowsource")})
        # Emulate the explicitly selected R7 deletion in the isolated database.
        self.old_source.delete()
        executor = MigrationExecutor(connection)
        executor.migrate([("studio", self.migrate_to)])
        current = executor.loader.project_state([("studio", self.migrate_to)]).apps
        source = current.get_model("studio", "ComfyWorkflowSource")
        expected = {key: value for key, value in self.native_before.items() if key != "generated_workflow_id"}
        self.assertEqual(source.objects.filter(pk=self.native_source.pk).values().get(), expected)
        self.assertTrue(current.get_model("studio", "GenerationTask").objects.filter(pk=self.task_id).exists())
        self.assertTrue(current.get_model("studio", "GeneratedWorkflow").objects.filter(pk=self.graph_id).exists())
        self.assertNotIn("generated_workflow", {field.name for field in source._meta.fields})
