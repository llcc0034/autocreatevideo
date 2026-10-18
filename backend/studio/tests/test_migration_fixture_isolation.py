"""Test-only schema staging must not alter real migration rollback contracts."""
from types import SimpleNamespace
from unittest.mock import Mock, patch

from django.db import connection, migrations
from django.core.management import call_command
from django.db.migrations.exceptions import IrreversibleError
from django.db.migrations.executor import MigrationExecutor
from django.db.migrations.loader import MigrationLoader
from django.test import SimpleTestCase, TransactionTestCase

from studio.tests import test_configuration_migrations as fixtures


class MigrationFixtureExecutorTests(SimpleTestCase):
    def test_non_test_database_is_rejected_before_loading_or_changing_schema(self):
        with patch.object(fixtures, "connection", SimpleNamespace()), patch.object(fixtures, "MigrationExecutor") as executor:
            with self.assertRaisesMessage(RuntimeError, "isolated test database"):
                fixtures._migrate_fixture_schema([("studio", None)])
        executor.assert_not_called()

    def assert_scoped_staging(self, *, migration_name="0186_freeze_business_output_parser", fail=False):
        loader = MigrationLoader(None)
        target = ("studio", migration_name)
        migration = loader.graph.nodes[target]
        original_operations = migration.operations
        irreversible = original_operations[-1]
        self.assertFalse(irreversible.reversible)
        unknown = SimpleNamespace(app_label="studio", name="9999_unknown_forward_only",
            operations=[migrations.RunPython(lambda *_: None)])
        forward = loader.graph.nodes[("studio", "0175_configurable_style_categories")]
        forward_operations = forward.operations
        executor = Mock()
        executor.migration_plan.return_value = [(migration, True), (unknown, True), (forward, False)]

        def inspect(targets):
            self.assertEqual(targets, [("studio", "0185_business_output_versions")])
            self.assertIs(migration.operations[0], original_operations[0])
            replacement = migration.operations[-1]
            self.assertIsNot(replacement, irreversible)
            self.assertIs(replacement.code, irreversible.code)
            self.assertIs(replacement.reverse_code, migrations.RunPython.noop)
            self.assertFalse(irreversible.reversible)
            # Migration.__init__ only shallow-copies operations. Changing the
            # original RunPython would leak into every later real executor.
            self.assertIs(type(migration).operations[-1], irreversible)
            self.assertIs(forward.operations, forward_operations)
            self.assertFalse(next(op for op in forward.operations if type(op) is migrations.RunPython).reversible)
            self.assertFalse(unknown.operations[0].reversible)
            if fail:
                raise IrreversibleError("unlisted operation remains irreversible")

        executor.migrate.side_effect = inspect
        with patch.object(fixtures, "connection", SimpleNamespace(_test_serialized_contents="isolated snapshot")), \
                patch.object(fixtures, "MigrationExecutor", return_value=executor):
            if fail:
                with self.assertRaisesMessage(IrreversibleError, "unlisted operation"):
                    fixtures._migrate_fixture_schema([("studio", "0185_business_output_versions")])
            else:
                fixtures._migrate_fixture_schema([("studio", "0185_business_output_versions")])
        self.assertIs(migration.operations, original_operations)
        self.assertFalse(irreversible.reversible)
        fresh = MigrationLoader(None)
        for key in fixtures._SCHEMA_FIXTURE_ONLY_DATA_MIGRATIONS:
            self.assertTrue(any(not op.reversible for op in fresh.graph.nodes[key].operations))

    def test_only_allowlisted_backward_setup_is_copied_and_restored(self):
        for migration_name in ("0186_freeze_business_output_parser", "0188_freeze_content_source_reader"):
            with self.subTest(migration=migration_name):
                self.assert_scoped_staging(migration_name=migration_name)

    def test_failed_setup_restores_operations_and_does_not_swallow_error(self):
        for migration_name in ("0186_freeze_business_output_parser", "0188_freeze_content_source_reader"):
            with self.subTest(migration=migration_name):
                self.assert_scoped_staging(migration_name=migration_name, fail=True)


class MigrationFixtureRestorationTests(fixtures._MigrationTestCase):
    # Keep the base's fixture/serialization lifecycle, but enter a historical
    # schema explicitly below so the outer test can observe cleanup afterwards.
    setUp = TransactionTestCase.setUp

    def test_seed_restoration_after_plain_transaction_flush_keeps_access_policy_available(self):
        from django.contrib.contenttypes.models import ContentType
        from studio.models import ConfigDefinition
        from studio.tests.transaction_fixtures import SeededTransactionTestCase
        from studio.throttling import DatabaseUserRateThrottle, RuntimeConfigurationUnavailable

        before = list(ConfigDefinition.objects.order_by("key").values_list("key", "initial_value"))
        rate = DatabaseUserRateThrottle().get_rate()
        self.assertTrue(before)
        # Reproduce Django's ordinary TransactionTestCase teardown: permissions
        # return via post_migrate, but migration-authored access policies do not.
        call_command("flush", verbosity=0, interactive=False, database=connection.alias,
            reset_sequences=False)
        self.assertFalse(ConfigDefinition.objects.exists())
        self.assertTrue(ContentType.objects.exists())
        with self.assertRaises(RuntimeConfigurationUnavailable):
            DatabaseUserRateThrottle().get_rate()
        SeededTransactionTestCase._fixture_setup()
        self.assertEqual(list(ConfigDefinition.objects.order_by("key").values_list("key", "initial_value")), before)
        self.assertEqual(DatabaseUserRateThrottle().get_rate(), rate)

    def test_failed_historical_fixture_restores_latest_schema_and_seed_facts(self):
        from studio.models import ConfigDefinition
        before_migrations = set(MigrationExecutor(connection).loader.applied_migrations)
        definition_id, description = ConfigDefinition.objects.order_by("pk").values_list("pk", "description").first()

        class FailingFixture(fixtures._MigrationTestCase):
            migrate_from = "0185_business_output_versions"
            migrate_to = "0186_freeze_business_output_parser"

            def set_up_before_migration(self, apps):
                fields = {field.name for field in apps.get_model("studio", "BusinessOutputVersion")._meta.fields}
                self.assertNotIn("parser_contract", fields)
                apps.get_model("studio", "ConfigDefinition").objects.filter(pk=definition_id).update(description="isolated fixture mutation")
                raise RuntimeError("isolated fixture failure")

        fixture = FailingFixture()
        try:
            with self.assertRaisesMessage(RuntimeError, "isolated fixture failure"):
                fixture.setUp()
        finally:
            self.assertTrue(fixture.doCleanups())
        self.assertEqual(set(MigrationExecutor(connection).loader.applied_migrations), before_migrations)
        if connection.vendor == "sqlite":
            self.assertEqual(ConfigDefinition.objects.get(pk=definition_id).description, description)
        self.assertFalse(ConfigDefinition.objects.filter(description="isolated fixture mutation").exists())
        with connection.cursor() as cursor:
            columns = {field.name for field in connection.introspection.get_table_description(cursor, "studio_businessoutputversion")}
        self.assertIn("parser_contract", columns)
        # The ordinary executor still refuses a production data rollback.
        with self.assertRaises(IrreversibleError):
            MigrationExecutor(connection).migrate([("studio", "0185_business_output_versions")])
        # Restore any later reversible steps unapplied before the ordinary
        # executor refused to reverse the frozen reader/parser data migration.
        MigrationExecutor(connection).migrate(MigrationExecutor(connection).loader.graph.leaf_nodes())
