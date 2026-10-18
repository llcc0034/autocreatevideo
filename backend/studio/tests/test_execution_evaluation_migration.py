"""The diagnostic schema neither runs nor fabricates a preparation evaluation."""
from django.db import connection
from django.db.migrations.executor import MigrationExecutor

from studio.tests.test_configuration_migrations import _MigrationTestCase


class ExecutionEvaluationMigrationTests(_MigrationTestCase):
    migrate_from = "0142_execution_preparation_records"
    migrate_to = "0143_execution_preparation_evaluation"

    def set_up_before_migration(self, apps):
        # Historical ORM rows test the additive Schema only, not an admitted
        # execution or a source whose missing detail may be reconstructed.
        actor = apps.get_model("auth", "User").objects.create(username="evaluation-schema-owner")
        provider = apps.get_model("studio", "ProviderProfile").objects.create(key="evaluation-schema", name="Schema fixture", driver_type="openai_compatible")
        profile = apps.get_model("studio", "ModelProfileVersion").objects.create(profile_key="evaluation-schema", provider=provider,
            version=1, model_identifier="schema-only", modality="text")
        deployment = apps.get_model("studio", "ModelDeployment").objects.create(key="evaluation-schema", name="Schema fixture", profile_version=profile)
        smoke = apps.get_model("studio", "ModelDeploymentSmokeAttempt").objects.create(deployment=deployment,
            operation="prompt.draft", status="queued", request_sha256="a" * 64, target_sha256="b" * 64)
        self.preparation_id = apps.get_model("studio", "ExecutionPreparation").objects.create(deployment_smoke=smoke,
            purpose="resource_smoke", unit_key="schema", request_key="schema", revision=1, status="preparing",
            created_by=actor, intent_sha256="c" * 64).pk
        self.smoke_count = apps.get_model("studio", "ModelDeploymentSmokeAttempt").objects.count()

    def test_existing_row_gets_no_fabricated_evaluation_or_state_change(self):
        prep = self.apps.get_model("studio", "ExecutionPreparation").objects.get(pk=self.preparation_id)
        self.assertEqual((prep.status, prep.revision), ("preparing", 1))
        self.assertEqual((prep.reason_code, prep.reason), ("", ""))
        self.assertIsNone(prep.evaluation_revision)
        self.assertIsNone(prep.evaluated_at)
        self.assertEqual(self.apps.get_model("studio", "ModelDeploymentSmokeAttempt").objects.count(), self.smoke_count)

    def test_schema_roundtrip_keeps_original_preparation_identity(self):
        MigrationExecutor(connection).migrate([("studio", self.migrate_from)])
        MigrationExecutor(connection).migrate([("studio", self.migrate_to)])
        prep = self.apps.get_model("studio", "ExecutionPreparation").objects.get(pk=self.preparation_id)
        self.assertEqual((prep.status, prep.intent_sha256), ("preparing", "c" * 64))
        self.assertIsNone(prep.evaluated_at)
