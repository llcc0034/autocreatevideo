"""Isolated SQLite schema roundtrip; never changes real business records."""
from django.db import connection
from django.db.migrations.executor import MigrationExecutor

from studio.services.json_contracts import canonical_json_bytes, json_contract
from studio.tests.test_configuration_migrations import _MigrationTestCase


class PreparationResolvedLimitMigrationTests(_MigrationTestCase):
    migrate_from = "0195_production_stage_input_amendment"
    migrate_to = "0196_preparation_resolved_inputs_limit"

    def set_up_before_migration(self, apps):
        actor = apps.get_model("auth", "User").objects.create(username="limit-migration")
        provider = apps.get_model("studio", "ProviderProfile").objects.create(key="limit", name="Limit", driver_type="deepseek_api")
        model = apps.get_model("studio", "ModelProfileVersion").objects.create(
            profile_key="limit", version=1, model_identifier="fixture", modality="text", provider=provider)
        deployment = apps.get_model("studio", "ModelDeployment").objects.create(key="limit", name="Limit", profile_version=model)
        smoke = apps.get_model("studio", "ModelDeploymentSmokeAttempt").objects.create(
            deployment=deployment, operation="prompt.draft", status="failed", request_sha256="a" * 64, target_sha256="b" * 64)
        facts = json_contract({"schema_version": 1, "field_requests": []})
        review = json_contract({"schema_version": 1, "mode": "manual"})
        intent = json_contract({"schema_version": 1, "request": {}, "resolved_inputs_sha256": facts.sha256, "review_contract_sha256": review.sha256})
        prep = apps.get_model("studio", "ExecutionPreparation").objects.create(
            deployment_smoke=smoke, purpose="resource_smoke", unit_key="sample", request_key="limit", status="blocked",
            created_by=actor, intent_sha256=intent.sha256)
        values = {"preparation": prep}
        for name, contract in (("intent", intent), ("resolved_inputs", facts), ("review_contract", review)):
            values.update({name: contract.value, name + "_schema_version": contract.schema_version,
                name + "_size_bytes": contract.size_bytes, name + "_sha256": contract.sha256})
        row = apps.get_model("studio", "ExecutionPreparationContract").objects.create(**values)
        self.row_id = row.pk
        self.original = apps.get_model("studio", "ExecutionPreparationContract").objects.values().get(pk=row.pk)

    def test_upgrade_and_small_record_rollback_preserve_every_column(self):
        model = self.apps.get_model("studio", "ExecutionPreparationContract")
        self.assertEqual(model.objects.values().get(pk=self.row_id), self.original)
        MigrationExecutor(connection).migrate([("studio", self.migrate_from)])
        self.assertEqual(model.objects.values().get(pk=self.row_id), self.original)
        MigrationExecutor(connection).migrate([("studio", self.migrate_to)])
        self.assertEqual(model.objects.values().get(pk=self.row_id), self.original)

    def test_large_record_blocks_rollback_before_any_schema_change(self):
        model = self.apps.get_model("studio", "ExecutionPreparationContract")
        value = {"fixture": "x" * 70000}
        # Historical models deliberately exercise the DB schema, not current save guards.
        contract = json_contract(value, max_bytes=262144)
        model.objects.filter(pk=self.row_id).update(resolved_inputs=value, resolved_inputs_size_bytes=len(canonical_json_bytes(value)),
            resolved_inputs_sha256=contract.sha256)
        with self.assertRaisesMessage(RuntimeError, "Cannot restore the 64 KiB"):
            MigrationExecutor(connection).migrate([("studio", self.migrate_from)])
        self.assertEqual(model.objects.get(pk=self.row_id).resolved_inputs, value)
        self.assertIn(("studio", self.migrate_to), MigrationExecutor(connection).loader.applied_migrations)
