"""Historical Smoke scope is not guessed or backfilled during cutover."""
from django.db import connection
from django.db.migrations.executor import MigrationExecutor
from studio.tests.test_configuration_migrations import _MigrationTestCase


class ExecutionSmokeMigrationTests(_MigrationTestCase):
    migrate_from = "0145_execution_task_links"
    migrate_to = "0146_execution_smoke_scope"
    migrate_to_in_setup = False

    def set_up_before_migration(self, apps):
        provider = apps.get_model("studio", "ProviderProfile").objects.create(key="scope-migration", name="Fixture", driver_type="deepseek_api")
        model = apps.get_model("studio", "ModelProfileVersion").objects.create(profile_key="scope-migration", version=1,
            model_identifier="fixture", modality="text", provider=provider)
        self.deployment = apps.get_model("studio", "ModelDeployment").objects.create(key="scope-migration", name="Fixture", profile_version=model)

    def test_empty_upgrade_adds_scopes_without_invented_instances_or_preparations(self):
        MigrationExecutor(connection).migrate([("studio", self.migrate_to)])
        apps = MigrationExecutor(connection).loader.project_state([("studio", self.migrate_to)]).apps
        for name in ("WorkflowSmokeAttempt", "ModelDeploymentSmokeAttempt"):
            model = apps.get_model("studio", name)
            self.assertTrue(model._meta.get_field("execution_instance_version").null)
            self.assertTrue(model._meta.get_field("hard_deadline_at").null)
            self.assertIn("preparing", dict(model._meta.get_field("status").choices))
        self.assertFalse(apps.get_model("studio", "ModelExecutionInstanceVersion").objects.exists())
        self.assertFalse(apps.get_model("studio", "ExecutionPreparation").objects.exists())

    def test_old_attempt_stops_upgrade_before_scope_assignment_or_deletion(self):
        attempt = self.apps.get_model("studio", "ModelDeploymentSmokeAttempt").objects.create(deployment=self.deployment,
            operation="prompt.draft", status="succeeded", request_sha256="a" * 64, target_sha256="b" * 64)
        with self.assertRaisesMessage(RuntimeError, "reviewed cleanup"):
            MigrationExecutor(connection).migrate([("studio", self.migrate_to)])
        self.assertTrue(self.apps.get_model("studio", "ModelDeploymentSmokeAttempt").objects.filter(pk=attempt.pk).exists())
        with connection.cursor() as cursor:
            columns = {row.name for row in connection.introspection.get_table_description(cursor, "studio_modeldeploymentsmokeattempt")}
        self.assertNotIn("scope", columns)
