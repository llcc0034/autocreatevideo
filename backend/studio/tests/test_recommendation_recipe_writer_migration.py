"""Isolated historical schema; no actual environment cleanup or model requests."""
from django.core.exceptions import FieldDoesNotExist
from django.db import connection
from django.db.migrations.executor import MigrationExecutor

from studio.tests.test_configuration_migrations import _MigrationTestCase


class RecommendationRecipeWriterMigrationTests(_MigrationTestCase):
    migrate_from = "0168_remove_retired_prompt_workflow_storage"
    migrate_to = "0169_remove_recommendation_recipe_writer"
    migrate_to_in_setup = False

    def set_up_before_migration(self, apps):
        self.old_apps = apps
        self.actor = apps.get_model("auth", "User").objects.create(username="recommendation-migration")
        host = apps.get_model("studio", "RuntimeHost").objects.get(key="local")
        self.snapshot = apps.get_model("studio", "HostCapabilitySnapshot").objects.create(
            host=host, revision=1, probe_status="partial", gpu_count=0)
        self.recommendation = apps.get_model("studio", "RecommendationSnapshot").objects.create(
            host_snapshot=self.snapshot, input_sha256="a" * 64, status="ready", created_by=self.actor)
        self.deployment = apps.get_model("studio", "ModelDeployment").objects.create(
            key="preserved-recommendation-resource", name="Shared resource",
            profile_version=apps.get_model("studio", "ModelProfileVersion").objects.get(
                profile_key="deepseek-text", version=1))
        self.before = apps.get_model("studio", "ModelDeployment").objects.filter(pk=self.deployment.pk).values().get()
        self.candidate = apps.get_model("studio", "RecommendationCandidate").objects.create(
            recommendation_snapshot=self.recommendation, deployment=self.deployment,
            capability_key="script.generate", status="smoke_test_required", suggested_values={
                "configuration": {"production.default_concurrency": 1}})

    def migrate(self):
        executor = MigrationExecutor(connection)
        executor.migrate([("studio", self.migrate_to)])
        self.apps = executor.loader.project_state([("studio", self.migrate_to)]).apps

    def assert_resource_unchanged(self, apps):
        self.assertEqual(apps.get_model("studio", "ModelDeployment").objects.filter(
            pk=self.deployment.pk).values().get(), self.before)
        self.assertFalse(apps.get_model("studio", "ModelExecutionInstanceVersion").objects.exists())

    def test_old_route_blocks_without_reinterpreting_resource_as_instance(self):
        self.candidate.suggested_values = {"route": {"model_deployment_id": str(self.deployment.pk)}}
        self.candidate.save()
        with self.assertRaisesRegex(RuntimeError, "R7"):
            self.migrate()
        self.assertTrue(self.old_apps.get_model("studio", "RecommendationCandidate").objects.filter(pk=self.candidate.pk).exists())
        self.assert_resource_unchanged(self.old_apps)

    def test_recommended_recipe_blocks_without_unlinking_decision(self):
        recipe = self.old_apps.get_model("studio", "ProductionRecipeVersion").objects.create(
            recipe_key="old-recommendation-draft", version=1, name="Old draft", status="draft")
        decision = self.old_apps.get_model("studio", "RecommendationDecision").objects.create(
            recommendation_snapshot=self.recommendation, candidate=self.candidate,
            decision="accept", scope_type="system", recipe_version=recipe, decided_by=self.actor)
        with self.assertRaisesRegex(RuntimeError, "R7"):
            self.migrate()
        decision.refresh_from_db()
        self.assertEqual(decision.recipe_version_id, recipe.pk)
        self.assert_resource_unchanged(self.old_apps)

    def test_current_resource_recommendation_survives_and_recipe_field_is_removed(self):
        decision = self.old_apps.get_model("studio", "RecommendationDecision").objects.create(
            recommendation_snapshot=self.recommendation, candidate=self.candidate,
            decision="reject", scope_type="system", decided_by=self.actor)
        self.migrate()
        model = self.apps.get_model("studio", "RecommendationDecision")
        with self.assertRaises(FieldDoesNotExist):
            model._meta.get_field("recipe_version")
        self.assertEqual(model.objects.get(pk=decision.pk).candidate_id, self.candidate.pk)
        self.assertEqual(self.apps.get_model("studio", "RecommendationCandidate").objects.get(
            pk=self.candidate.pk).suggested_values, self.candidate.suggested_values)
        self.assert_resource_unchanged(self.apps)
