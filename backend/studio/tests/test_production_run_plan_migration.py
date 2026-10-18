"""The plan cutover must refuse old roots, never manufacture their history."""
from django.db import connection
from django.db.migrations.executor import MigrationExecutor

from studio.tests.test_configuration_migrations import _MigrationTestCase
from studio.tests.test_execution_context_inputs import PROJECT_CONFIG


class ProductionRunPlanMigrationTests(_MigrationTestCase):
    migrate_from = "0149_execution_capacity_observation"
    migrate_to = "0150_production_run_plan"
    migrate_to_in_setup = False

    def set_up_before_migration(self, apps):
        self.actor = apps.get_model("auth", "User").objects.create(username="run-plan-migration")
        self.project = apps.get_model("studio", "SeriesProject").objects.create(
            owner=self.actor, title="Preserved project", **PROJECT_CONFIG)
        self.recipe = apps.get_model("studio", "ProductionRecipeVersion").objects.create(
            recipe_key="old-plan", version=1, name="Old source")

    def test_empty_roots_add_source_constraints_without_inventing_plans_or_deleting_resources(self):
        MigrationExecutor(connection).migrate([("studio", self.migrate_to)])
        apps = MigrationExecutor(connection).loader.project_state([("studio", self.migrate_to)]).apps
        self.assertFalse(apps.get_model("studio", "ProductionRun")._meta.get_field("plan_source").has_default())
        self.assertTrue(apps.get_model("studio", "ProductionRun")._meta.get_field("recipe_version").null)
        self.assertEqual(apps.get_model("studio", "ProductionRunPlan").objects.count(), 0)
        self.assertEqual(apps.get_model("studio", "ProductionManualCommand").objects.count(), 0)
        self.assertTrue(apps.get_model("studio", "SeriesProject").objects.filter(pk=self.project.pk).exists())
        self.assertTrue(apps.get_model("studio", "ProductionRecipeVersion").objects.filter(pk=self.recipe.pk).exists())

    def test_unreviewed_old_runs_abort_before_source_column_or_cleanup(self):
        run = self.apps.get_model("studio", "ProductionRun").objects.create(owner=self.actor, project=self.project,
            recipe_version=self.recipe, idempotency_key="old-run", request_sha256="a" * 64,
            input_snapshot_sha256="b" * 64, config_snapshot_sha256="c" * 64)
        with self.assertRaisesMessage(RuntimeError, "reviewed cleanup"):
            MigrationExecutor(connection).migrate([("studio", self.migrate_to)])
        self.assertTrue(self.apps.get_model("studio", "ProductionRun").objects.filter(pk=run.pk).exists())
        with connection.cursor() as cursor:
            columns = {row.name for row in connection.introspection.get_table_description(cursor, "studio_productionrun")}
        self.assertNotIn("plan_source", columns)
        self.assertFalse(MigrationExecutor(connection).loader.applied_migrations.get(("studio", self.migrate_to)))
