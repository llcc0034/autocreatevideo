from django.db import connection
from django.db.migrations.executor import MigrationExecutor

from studio.tests.test_configuration_migrations import _MigrationTestCase


class AssemblyConsumerMigrationTests(_MigrationTestCase):
    migrate_from = "0233_alter_promptprofilefieldrule_source_selector"
    migrate_to = "0234_assembly_consumer_input"

    def set_up_before_migration(self, apps):
        actor = apps.get_model("auth", "User").objects.create(username="assembly-migration-fixture")
        project = apps.get_model("studio", "SeriesProject").objects.create(owner=actor, title="Isolated migration",
            width=768, height=1024, fps=24, default_shot_duration_ms=6000, continuity_seed=0)
        job = apps.get_model("studio", "AssemblyJob").objects.create(owner=actor, project=project,
            scope="series", max_attempts=1, status="failed", error_message="Original failure", source_signature="b" * 64)
        self.before = apps.get_model("studio", "AssemblyJob").objects.values().get(pk=job.pk)

    def test_upgrade_and_schema_reverse_preserve_old_job_without_inventing_consumer(self):
        after = self.apps.get_model("studio", "AssemblyJob").objects.values().get(pk=self.before["id"])
        self.assertIsNone(after.pop("consumer_stage_id"))
        self.assertEqual(after.pop("consumer_input_sha256"), "")
        self.assertEqual(after, self.before)
        executor = MigrationExecutor(connection)
        executor.migrate([("studio", self.migrate_from)])
        old = executor.loader.project_state([("studio", self.migrate_from)]).apps
        self.assertEqual(old.get_model("studio", "AssemblyJob").objects.values().get(pk=self.before["id"]), self.before)
        # Restore in-test using the real forward migration, not a fixture bypass.
        MigrationExecutor(connection).migrate([("studio", self.migrate_to)])
        connection.check_constraints()
