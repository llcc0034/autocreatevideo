from django.db import connection, IntegrityError, transaction
from django.db.migrations.executor import MigrationExecutor

from studio.tests.test_configuration_migrations import _MigrationTestCase
from studio.tests.test_execution_context_inputs import PROJECT_CONFIG


class TaskDistributionPhaseMigrationTests(_MigrationTestCase):
    migrate_from = "0157_release_bundle_run_required"
    migrate_to = "0158_task_distribution_phase"

    def set_up_before_migration(self, apps):
        actor = apps.get_model("auth", "User").objects.create(username="distribution-migration")
        project = apps.get_model("studio", "SeriesProject").objects.create(owner=actor, title="Original project", **PROJECT_CONFIG)
        self.task_id = apps.get_model("studio", "GenerationTask").objects.create(
            owner=actor, project=project, kind="image", status="succeeded", outputs=[{"fixture": "original bytes stay unchanged"}]).pk

    def test_additive_phase_does_not_invent_processing_or_rewrite_original_output(self):
        tasks = self.apps.get_model("studio", "GenerationTask")
        task = tasks.objects.get(pk=self.task_id)
        self.assertEqual(task.outputs, [{"fixture": "original bytes stay unchanged"}])
        self.assertEqual((task.status, task.distribution_state, task.distribution_revision), ("succeeded", "not_requested", 0))
        self.assertIsNone(task.distribution_claim)
        self.assertIsNone(task.distribution_heartbeat_at)
        with self.assertRaises(IntegrityError), transaction.atomic():
            tasks.objects.filter(pk=task.pk).update(distribution_state="ready")
        with connection.cursor() as cursor:
            constraints = connection.introspection.get_constraints(cursor, "studio_generationtask")
        self.assertEqual(constraints["idx_task_distribution_work"]["columns"], ["distribution_state", "distribution_requested_at", "id"])

    def test_schema_round_trip_keeps_original_task_without_creating_generation(self):
        executor = MigrationExecutor(connection)
        executor.migrate([("studio", self.migrate_from)])
        old = executor.loader.project_state([("studio", self.migrate_from)]).apps.get_model("studio", "GenerationTask")
        self.assertEqual(old.objects.get(pk=self.task_id).outputs, [{"fixture": "original bytes stay unchanged"}])
        MigrationExecutor(connection).migrate([("studio", self.migrate_to)])
        self.assertEqual(self.apps.get_model("studio", "GenerationTask").objects.count(), 1)
