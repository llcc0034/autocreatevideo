"""Required task links cannot be backfilled with invented historical targets."""
from django.db import connection
from django.db.migrations.executor import MigrationExecutor

from studio.tests.test_configuration_migrations import _MigrationTestCase


class ExecutionTaskMigrationTests(_MigrationTestCase):
    migrate_from = "0144_execution_policy_configuration"
    migrate_to = "0145_execution_task_links"
    migrate_to_in_setup = False

    def set_up_before_migration(self, apps):
        self.actor = apps.get_model("auth", "User").objects.create(username="migration-task-owner")
        provider = apps.get_model("studio", "ProviderProfile").objects.create(key="migration-target", name="Fixture", driver_type="deepseek_api")
        model = apps.get_model("studio", "ModelProfileVersion").objects.create(profile_key="migration-target", version=1,
            model_identifier="fixture", modality="text", provider=provider)
        self.deployment = apps.get_model("studio", "ModelDeployment").objects.create(key="migration-target", name="Fixture", profile_version=model)

    def migrate(self):
        MigrationExecutor(connection).migrate([("studio", self.migrate_to)])

    def test_empty_root_upgrade_adds_nonnullable_fks_without_fabricated_data(self):
        self.migrate()
        apps = MigrationExecutor(connection).loader.project_state([("studio", self.migrate_to)]).apps
        for model, field in (("AssetGenerationJob", "execution_instance_version"),
                ("GenerationAttempt", "execution_instance_version"), ("GenerationAttempt", "preparation")):
            self.assertFalse(apps.get_model("studio", model)._meta.get_field(field).null)
        self.assertEqual(apps.get_model("studio", "ModelExecutionInstanceVersion").objects.count(), 0)
        self.assertEqual(apps.get_model("studio", "ExecutionPreparation").objects.count(), 0)
        self.assertTrue(apps.get_model("studio", "ModelDeployment").objects.filter(pk=self.deployment.pk).exists())

    def test_unreviewed_old_root_blocks_before_any_schema_change_or_deletion(self):
        job = self.apps.get_model("studio", "AssetGenerationJob").objects.create(owner=self.actor,
            model_deployment=self.deployment, job_type="prompt", media_type="script", operation="prompt.draft",
            idempotency_key="old-root", request_snapshot_sha256="a" * 64)
        with self.assertRaisesMessage(RuntimeError, "reviewed cleanup"):
            self.migrate()
        self.assertTrue(self.apps.get_model("studio", "AssetGenerationJob").objects.filter(pk=job.pk).exists())
        with connection.cursor() as cursor:
            columns = {row.name for row in connection.introspection.get_table_description(cursor, "studio_assetgenerationjob")}
        self.assertNotIn("execution_instance_version_id", columns)
        self.assertFalse(MigrationExecutor(connection).loader.applied_migrations.get(("studio", self.migrate_to)))
