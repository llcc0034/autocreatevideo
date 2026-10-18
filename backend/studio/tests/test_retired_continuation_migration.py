from django.db import connection
from django.db.migrations.executor import MigrationExecutor

from studio.tests.test_configuration_migrations import _MigrationTestCase
from studio.tests.test_execution_context_inputs import PROJECT_CONFIG


class RetiredContinuationMigrationTests(_MigrationTestCase):
    migrate_from = "0163_shot_episode_timing"
    migrate_to = "0164_remove_deferred_generation_request"

    def set_up_before_migration(self, apps):
        # Schema-only fixtures; no model call, file copy, or invented acceptance.
        actor = apps.get_model("auth", "User").objects.create(username="retired-continuation")
        project = apps.get_model("studio", "SeriesProject").objects.create(
            owner=actor, title="Shared project", **PROJECT_CONFIG)
        episode = apps.get_model("studio", "Episode").objects.create(project=project, number=1, title="Episode")
        shot = apps.get_model("studio", "Shot").objects.create(
            episode=episode, number=1, narrative="Original facts", duration_ms=5000,
            media_type="video", include_audio=False)
        task_model = apps.get_model("studio", "GenerationTask")
        source = task_model.objects.create(owner=actor, project=project, episode=episode,
            kind="video", status="succeeded", outputs=[{"fixture": "source remains unchanged"}])
        destination = task_model.objects.create(owner=actor, project=project, episode=episode,
            kind="video", status="succeeded", outputs=[{"fixture": "destination remains unchanged"}])
        asset = apps.get_model("studio", "Asset").objects.create(owner=actor, kind="reference",
            name="Shared frame", checksum="a" * 64, metadata={"fixture": "shared asset unchanged"})
        template = apps.get_model("studio", "WorkflowTemplate").objects.create(
            owner=actor, name="Shared graph", slug="shared-graph", output_kind="video")
        workflow = apps.get_model("studio", "WorkflowTemplateVersion").objects.create(
            template=template, version=1, graph={"fixture": "unchanged graph"})
        request_model = apps.get_model("studio", "DeferredGenerationRequest")
        for state in ("waiting_upstream", "failed", "materialized"):
            request_model.objects.create(owner=actor, project=project, source_task=source,
                target_shot=shot, frame_slot="first_frame", target_workflow_version=workflow,
                idempotency_key=state, request_fingerprint="b" * 64,
                target_workflow_snapshot={"fixture": "retired request only"},
                target_workflow_snapshot_sha256="c" * 64, target_input_fingerprint="d" * 64,
                provisional_prompt_text="Old provisional prompt", provisional_prompt_sha256="e" * 64,
                max_attempts=1, retry_backoff_seconds=1, status=state, derived_asset=asset,
                materialized_task=destination if state == "materialized" else None)
        self.assertEqual(request_model.objects.filter(project=project).count(), 3)
        self.shared_rows = [(row._meta.object_name, row.pk, row.__class__.objects.filter(pk=row.pk).values().get())
            for row in (actor, project, episode, shot, source, destination, asset, template, workflow)]

    def _assert_shared_rows_unchanged(self):
        for name, pk, before in self.shared_rows:
            with self.subTest(model=name, pk=str(pk)):
                app_label = "auth" if name == "User" else "studio"
                actual = self.apps.get_model(app_label, name).objects.filter(pk=pk).values().get()
                self.assertEqual(actual, before)

    def test_retired_table_is_dropped_without_converting_or_deleting_shared_objects(self):
        self.assertNotIn("studio_deferredgenerationrequest", connection.introspection.table_names())
        with self.assertRaises(LookupError):
            self.apps.get_model("studio", "DeferredGenerationRequest")
        self._assert_shared_rows_unchanged()

    def test_schema_reversal_does_not_restore_discarded_history(self):
        executor = MigrationExecutor(connection)
        executor.migrate([("studio", self.migrate_from)])
        old_apps = executor.loader.project_state([("studio", self.migrate_from)]).apps
        self.assertEqual(old_apps.get_model("studio", "DeferredGenerationRequest").objects.count(), 0)
        MigrationExecutor(connection).migrate([("studio", self.migrate_to)])
        self._assert_shared_rows_unchanged()
