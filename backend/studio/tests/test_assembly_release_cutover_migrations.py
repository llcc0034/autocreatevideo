"""Cutover guards reject old closures without deleting or inventing facts."""
from django.db import connection
from django.db.migrations.executor import MigrationExecutor

from studio.tests.test_configuration_migrations import _MigrationTestCase
from studio.tests.test_execution_context_inputs import PROJECT_CONFIG


class AssemblySourceCutoverTests(_MigrationTestCase):
    migrate_from = "0155_preparation_backed_generation_tasks"
    migrate_to = "0156_assembly_artifact_sources"
    migrate_to_in_setup = False

    def set_up_before_migration(self, apps):
        self.actor = apps.get_model("auth", "User").objects.create(username="assembly-cutover")
        self.project = apps.get_model("studio", "SeriesProject").objects.create(
            owner=self.actor, title="Preserve unrelated project", **PROJECT_CONFIG)
        self.episode = apps.get_model("studio", "Episode").objects.create(project=self.project, number=1, title="Episode")

    def test_empty_upgrade_removes_old_origins_without_creating_media(self):
        executor = MigrationExecutor(connection)
        executor.migrate([("studio", self.migrate_to)])
        apps = executor.loader.project_state([("studio", self.migrate_to)]).apps
        self.assertNotIn("source_tasks", {field.name for field in apps.get_model("studio", "AssemblyJob")._meta.get_fields()})
        sources = apps.get_model("studio", "AssemblySourceItem")
        self.assertNotIn("generation_task", {field.name for field in sources._meta.get_fields()})
        self.assertFalse(sources._meta.get_field("generated_artifact").null)
        self.assertEqual(apps.get_model("studio", "GeneratedArtifact").objects.count(), 0)
        self.assertTrue(apps.get_model("studio", "SeriesProject").objects.filter(pk=self.project.pk).exists())

    def test_unreviewed_old_assembly_blocks_before_removal_or_deletion(self):
        jobs = self.apps.get_model("studio", "AssemblyJob")
        job = jobs.objects.create(owner=self.actor, project=self.project, episode=self.episode,
            scope="episode", max_attempts=1, source_contract_version=4)
        with self.assertRaisesMessage(RuntimeError, "reviewed incompatible assemblies"):
            MigrationExecutor(connection).migrate([("studio", self.migrate_to)])
        self.assertTrue(jobs.objects.filter(pk=job.pk, source_contract_version=4).exists())
        self.assertFalse(MigrationExecutor(connection).loader.applied_migrations.get(("studio", self.migrate_to)))
        with connection.cursor() as cursor:
            columns = {row.name for row in connection.introspection.get_table_description(cursor, "studio_assemblysourceitem")}
        self.assertIn("generation_task_id", columns)


class ReleaseRunCutoverTests(_MigrationTestCase):
    migrate_from = "0156_assembly_artifact_sources"
    migrate_to = "0157_release_bundle_run_required"
    migrate_to_in_setup = False

    def set_up_before_migration(self, apps):
        self.actor = apps.get_model("auth", "User").objects.create(username="release-cutover")
        self.project = apps.get_model("studio", "SeriesProject").objects.create(
            owner=self.actor, title="Preserve unrelated project", **PROJECT_CONFIG)

    def test_empty_upgrade_requires_run_without_fabricating_one(self):
        executor = MigrationExecutor(connection)
        executor.migrate([("studio", self.migrate_to)])
        apps = executor.loader.project_state([("studio", self.migrate_to)]).apps
        self.assertFalse(apps.get_model("studio", "ReleaseBundle")._meta.get_field("production_run").null)
        self.assertEqual(apps.get_model("studio", "ProductionRun").objects.count(), 0)
        self.assertTrue(apps.get_model("studio", "SeriesProject").objects.filter(pk=self.project.pk).exists())

    def test_standalone_bundle_blocks_and_is_not_reparented_or_deleted(self):
        bundles = self.apps.get_model("studio", "ReleaseBundle")
        bundle = bundles.objects.create(owner=self.actor, project=self.project, created_by=self.actor, version=1)
        with self.assertRaisesMessage(RuntimeError, "reviewed standalone release closures"):
            MigrationExecutor(connection).migrate([("studio", self.migrate_to)])
        self.assertTrue(bundles.objects.filter(pk=bundle.pk, production_run__isnull=True).exists())
        self.assertEqual(self.apps.get_model("studio", "ProductionRun").objects.count(), 0)
        self.assertFalse(MigrationExecutor(connection).loader.applied_migrations.get(("studio", self.migrate_to)))

    def test_old_sealed_manifest_is_not_rewritten_or_resigned(self):
        run = self.apps.get_model("studio", "ProductionRun").objects.create(owner=self.actor, project=self.project,
            plan_source="manual", idempotency_key="old-release-fixture", request_sha256="a" * 64)
        bundles = self.apps.get_model("studio", "ReleaseBundle")
        manifest = {"schema": "autoprompt-production-release-bundle-v2", "fixture": "incompatible"}
        bundle = bundles.objects.create(owner=self.actor, project=self.project, created_by=self.actor, version=1,
            production_run=run, status="sealed", manifest=manifest, manifest_sha256="b" * 64)
        with self.assertRaisesMessage(RuntimeError, "reviewed incompatible release manifests"):
            MigrationExecutor(connection).migrate([("studio", self.migrate_to)])
        bundle.refresh_from_db()
        self.assertEqual(bundle.manifest, manifest)
        self.assertEqual(bundle.manifest_sha256, "b" * 64)
        self.assertFalse(MigrationExecutor(connection).loader.applied_migrations.get(("studio", self.migrate_to)))
