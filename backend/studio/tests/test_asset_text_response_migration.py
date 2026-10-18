"""0222 owner evolution on isolated historical rows, not execution evidence."""
import hashlib

from django.core.files.base import ContentFile
from django.core.files.storage import default_storage
from django.db import connection, IntegrityError, transaction
from django.db.migrations.executor import MigrationExecutor
from django.test import override_settings

from studio.tests.test_configuration_migrations import _MigrationTestCase


@override_settings(STORAGES={
    "default": {"BACKEND": "django.core.files.storage.InMemoryStorage"},
    "staticfiles": {"BACKEND": "django.contrib.staticfiles.storage.StaticFilesStorage"},
})
class AssetTextResponseMigrationTests(_MigrationTestCase):
    migrate_from = "0221_shot_semantic_relations"
    migrate_to = "0222_asset_text_response_owner"

    def set_up_before_migration(self, apps):
        def model(name): return apps.get_model("studio", name)
        actor = apps.get_model("auth", "User").objects.create(username="response-schema-fixture")
        project = model("SeriesProject").objects.create(owner=actor, title="Isolated schema fixture",
            width=768, height=1024, fps=24, default_shot_duration_ms=5000, continuity_seed=1)
        provider = model("ProviderProfile").objects.create(key="response-schema", name="Fixture", driver_type="deepseek_api")
        profile = model("ModelProfileVersion").objects.create(profile_key="response-schema", version=1,
            model_identifier="fixture", modality="text", provider=provider)
        deployment = model("ModelDeployment").objects.create(key="response-schema", name="Fixture", profile_version=profile)
        instance = model("ModelExecutionInstance").objects.create(key="response-schema", name="Fixture")
        version = model("ModelExecutionInstanceVersion").objects.create(instance=instance, version=1,
            deployment=deployment, operation="prompt.draft", prompt_mode="manual")
        run = model("ProductionRun").objects.create(owner=actor, project=project, plan_source="manual",
            idempotency_key="schema-fixture", request_sha256="a" * 64)
        stage = model("StageRun").objects.create(production_run=run, stage_key="script_generate", ordinal=1,
            mode="manual", idempotency_key="schema-stage")
        prep = model("ExecutionPreparation").objects.create(stage_run=stage, execution_instance_version=version,
            purpose="production", unit_key="schema", request_key="schema", status="awaiting_input",
            created_by=actor, intent_sha256="a" * 64)
        attempt = model("GenerationAttempt").objects.create(stage_run=stage, execution_instance_version=version,
            preparation=prep, role="primary", unit_key="schema", attempt_number=1, status="failed",
            request_sha256="a" * 64, request_snapshot_key="schema-only-not-a-real-request")
        self.job_id = model("AssetGenerationJob").objects.create(owner=actor, model_deployment=deployment,
            execution_instance_version=version, job_type="prompt", media_type="script", operation="prompt.draft",
            idempotency_key="schema-asset", request_snapshot_sha256="b" * 64).pk
        self.original = "  Isolated stored bytes\r\n原文未变化".encode("utf-8")
        key = default_storage.save("execution-responses/schema-fixture.txt", ContentFile(self.original))
        row = model("ExecutionTextResponseReview").objects.create(attempt=attempt, original_file=key,
            original_size=len(self.original), original_sha256=hashlib.sha256(self.original).hexdigest(),
            diagnostics={}, diagnostics_sha256=hashlib.sha256(b"{}").hexdigest())
        self.before = model("ExecutionTextResponseReview").objects.filter(pk=row.pk).values().get()

    def assert_preserved(self):
        rows = self.apps.get_model("studio", "ExecutionTextResponseReview").objects
        actual = rows.get(pk=self.before["id"])
        self.assertEqual({key: getattr(actual, key) for key in self.before}, self.before)
        self.assertIsNone(actual.asset_job_id)
        self.assertEqual(rows.count(), 1)
        self.assertFalse(rows.filter(asset_job_id=self.job_id).exists())
        with default_storage.open(actual.original_file, "rb") as stream:
            self.assertEqual(stream.read(), self.original)
        connection.check_constraints()

    def test_upgrade_preserves_original_owner_manifest_and_bytes_without_backfill(self):
        self.assert_preserved()

    def test_roundtrip_before_asset_responses_preserves_existing_production_response(self):
        MigrationExecutor(connection).migrate([("studio", self.migrate_from)])
        MigrationExecutor(connection).migrate([("studio", self.migrate_to)])
        self.assert_preserved()

    def test_database_rejects_two_owners_and_missing_owner_but_accepts_asset_owner(self):
        rows = self.apps.get_model("studio", "ExecutionTextResponseReview").objects
        for changes in ({"asset_job_id": self.job_id}, {"attempt_id": None}):
            with self.subTest(changes=changes), self.assertRaises(IntegrityError), transaction.atomic():
                rows.filter(pk=self.before["id"]).update(**changes)
        values = {key: value for key, value in self.before.items() if key not in ("id", "created_at", "updated_at", "attempt_id")}
        row = rows.create(asset_job_id=self.job_id, **values)
        self.assertIsNone(row.attempt_id)
        with self.assertRaises(IntegrityError), transaction.atomic():
            rows.create(asset_job_id=self.job_id, **values)
        connection.check_constraints()
