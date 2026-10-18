from django.db import connection
from django.db.migrations.executor import MigrationExecutor

from studio.tests.test_configuration_migrations import _MigrationTestCase
from studio.tests.test_execution_context_inputs import PROJECT_CONFIG


class ProjectTranslationSourceMigrationTests(_MigrationTestCase):
    migrate_from = "0158_task_distribution_phase"
    migrate_to = "0159_project_translation_sources"

    def set_up_before_migration(self, apps):
        model = lambda name: apps.get_model("studio", name)
        actor = apps.get_model("auth", "User").objects.create(username="translation-migration")
        project = model("SeriesProject").objects.create(owner=actor, title="Migration source", **PROJECT_CONFIG)
        provider = model("ProviderProfile").objects.create(key="migration-translator", name="Migration provider", driver_type="openai_compatible")
        version = model("ModelProfileVersion").objects.create(profile_key="migration-translator", version=1, provider=provider, model_identifier="explicit", modality="text")
        deployment = model("ModelDeployment").objects.create(key="migration-translator", name="Migration resource", profile_version=version)
        instance = model("ModelExecutionInstance").objects.create(key="migration-translator", name="Migration instance")
        execution = model("ModelExecutionInstanceVersion").objects.create(instance=instance, version=1, deployment=deployment, operation="prompt.normalize", prompt_mode="manual")
        self.job_fields = {"owner_id": actor.pk, "model_deployment_id": deployment.pk, "execution_instance_version_id": execution.pk,
            "job_type": "prompt", "status": "cancelled", "operation": "prompt.normalize"}
        self.project_id = project.pk
        self.job_id = model("AssetGenerationJob").objects.create(**self.job_fields, media_type="image", idempotency_key="migration-original").pk

    def test_additive_sources_do_not_rewrite_or_invent_history(self):
        job = self.apps.get_model("studio", "AssetGenerationJob").objects.get(pk=self.job_id)
        self.assertEqual((job.media_type, job.status), ("image", "cancelled"))
        self.assertFalse(self.apps.get_model("studio", "ProjectTranslationRequest").objects.exists())
        self.assertFalse(self.apps.get_model("studio", "ProjectTranslationSource").objects.exists())
        with connection.cursor() as cursor:
            constraints = connection.introspection.get_constraints(cursor, "studio_projecttranslationrequest")
        self.assertEqual(constraints["idx_translation_request"]["columns"], ["project_id", "created_at", "id"])

    def test_empty_source_schema_round_trip_preserves_original_job(self):
        executor = MigrationExecutor(connection)
        executor.migrate([("studio", self.migrate_from)])
        old = executor.loader.project_state([("studio", self.migrate_from)]).apps.get_model("studio", "AssetGenerationJob")
        self.assertEqual(old.objects.get(pk=self.job_id).media_type, "image")
        MigrationExecutor(connection).migrate([("studio", self.migrate_to)])
        self.assertEqual(self.apps.get_model("studio", "AssetGenerationJob").objects.count(), 1)

    def test_reverse_refuses_to_erase_new_source_or_reinterpret_text_job(self):
        job = self.apps.get_model("studio", "AssetGenerationJob").objects.create(
            **self.job_fields, media_type="text", idempotency_key="migration-new-text")
        request = self.apps.get_model("studio", "ProjectTranslationRequest").objects.create(
            project_id=self.project_id, job=job, source_lang="Chinese", target_lang="English", request_sha256="a" * 64)
        with self.assertRaisesMessage(RuntimeError, "不自动转换或删除"):
            MigrationExecutor(connection).migrate([("studio", self.migrate_from)])
        self.assertTrue(self.apps.get_model("studio", "ProjectTranslationRequest").objects.filter(pk=request.pk).exists())
        self.assertEqual(self.apps.get_model("studio", "AssetGenerationJob").objects.get(pk=job.pk).media_type, "text")
