"""0166 data phase plus the full fresh-install migration run used by Django.

Each test uses the actual 0165 schema and reconstructs a checksummed
installation baseline in the isolated test database, not in the operator DB.
"""
import importlib
from types import SimpleNamespace

from django.contrib.auth import get_user_model
from django.db import connection, transaction
from django.utils import timezone

from studio.tests.test_configuration_migrations import _MigrationTestCase


migration = importlib.import_module("studio.migrations.0166_execution_recommendation_configuration")


class ExecutionRecommendationMigrationTests(_MigrationTestCase):
    migrate_from = "0165_remove_legacy_workflow_source_target"
    migrate_to = migrate_from
    migrate_to_in_setup = False

    def set_up_before_migration(self, apps):
        self.apps = apps
        self.Definition = self.apps.get_model("studio", "ConfigDefinition")
        self.Revision = self.apps.get_model("studio", "ConfigRevision")
        self.Value = self.apps.get_model("studio", "ConfigValue")
        # Reconstruct only configuration test rows. No business or audit
        # fixtures are given synthetic route identities or successful proof.
        self.Revision.objects.update(based_on_revision=None)
        self.Value.objects.all().delete()
        self.Revision.objects.all().delete()
        self.Definition.objects.filter(key=migration.RECOMMENDATION_KEY).delete()
        self.source = self.Revision.objects.create(scope_type="system", revision=1,
            status="published", activation_state="active", published_at=timezone.now())
        self.expected = {"api.default_page_size": 27}
        self.Value.objects.create(revision=self.source,
            definition=self.Definition.objects.get(key="api.default_page_size"),
            **migration._metadata("value", 27))
        for key in migration.OBSOLETE_KEYS:
            definition, _ = self.Definition.objects.get_or_create(key=key, defaults={
                "domain": "production", "value_type": "string",
                **migration._metadata("value_schema", {}), **migration._metadata("initial_value", ""),
            })
            self.Value.objects.create(revision=self.source, definition=definition,
                **migration._metadata("value", "old-reference-not-an-execution-version"))
            self.expected[key] = "old-reference-not-an-execution-version"
        self.source.values_sha256 = migration.hashlib.sha256(migration._canonical(self.expected)).hexdigest()
        self.source.save()

    def apply(self):
        # Match RunPython's transactional boundary when checking failures.
        with transaction.atomic():
            migration.replace_obsolete_configuration(self.apps, SimpleNamespace(connection=connection))

    def test_seed_cleanup_creates_complete_new_baseline_without_route_conversion(self):
        self.apply()
        self.assertFalse(self.Revision.objects.filter(pk=self.source.pk).exists())
        self.assertFalse(self.Definition.objects.filter(key__in=migration.OBSOLETE_KEYS).exists())
        active = self.Revision.objects.get(scope_type="system", activation_state="active")
        values = {row.definition.key: row.value for row in self.Value.objects.filter(revision=active).select_related("definition")}
        self.assertEqual(values, {"api.default_page_size": 27, migration.RECOMMENDATION_KEY: ""})
        self.assertIsNone(active.based_on_revision_id)
        self.assertEqual(active.values_sha256, migration.hashlib.sha256(migration._canonical(values)).hexdigest())
        self.assertFalse(self.apps.get_model("studio", "ModelExecutionInstanceVersion").objects.exists())
        self.assertFalse(self.apps.get_model("studio", "ModelDeploymentSmokeAttempt").objects.exists())

    def test_authored_revision_requires_explicit_cutover_not_automatic_conversion(self):
        actor = get_user_model().objects.create_user(username="config-migration-owner")
        self.source.created_by_id = actor.pk
        self.source.save()
        before = list(self.Value.objects.values_list("pk", "value_sha256"))
        with self.assertRaisesMessage(RuntimeError, "人工修订"):
            self.apply()
        self.assertEqual(list(self.Value.objects.values_list("pk", "value_sha256")), before)
        self.assertFalse(self.Definition.objects.filter(key=migration.RECOMMENDATION_KEY).exists())

    def test_authored_derived_revision_is_part_of_the_protected_closure(self):
        actor = get_user_model().objects.create_user(username="config-derived-owner")
        self.Revision.objects.create(scope_type="system", revision=2,
            based_on_revision=self.source, created_by_id=actor.pk)
        with self.assertRaisesMessage(RuntimeError, "人工修订"):
            self.apply()
        self.assertEqual(self.Revision.objects.count(), 2)

    def test_invalid_value_digest_is_not_silently_repaired(self):
        row = self.Value.objects.get(revision=self.source, definition__key="api.default_page_size")
        row.value = 29
        row.save()
        with self.assertRaisesMessage(RuntimeError, "原配置值摘要不一致"):
            self.apply()
        self.assertTrue(self.Revision.objects.filter(pk=self.source.pk).exists())

    def test_invalid_revision_digest_is_not_silently_recomputed(self):
        self.source.values_sha256 = "0" * 64
        self.source.save()
        with self.assertRaisesMessage(RuntimeError, "原配置整版摘要不一致"):
            self.apply()
        self.assertTrue(self.Definition.objects.filter(key__in=migration.OBSOLETE_KEYS).exists())

    def test_missing_active_baseline_is_not_filled_from_defaults(self):
        self.source.activation_state = "inactive"
        self.source.save()
        with self.assertRaisesMessage(RuntimeError, "唯一有效的系统配置基线"):
            self.apply()

    def _add_credential_configuration(self):
        binding = self.apps.get_model("studio", "SecretBinding").objects.create(
            key="migration-config-secret", label="Isolated credential", purpose="test")
        version = self.apps.get_model("studio", "SecretVersion").objects.create(
            binding=binding, version=1, key_id="isolated", nonce_b64="isolated",
            ciphertext_b64="not-a-live-secret", ciphertext_sha256="a" * 64)
        definition = self.Definition.objects.create(key="test.credential", domain="test",
            value_type="string", is_secret=True, **migration._metadata("value_schema", {}))
        row = self.Value.objects.create(revision=self.source, definition=definition,
            value=None, credential_version=version, value_schema_version=1, value_size_bytes=0, value_sha256="")
        self.expected[definition.key] = {"credential_configured": True,
            "credential_binding_sha256": migration.hashlib.sha256(
                f"version:{version.pk}:{version.version}".encode("utf-8")).hexdigest()}
        self.source.values_sha256 = migration.hashlib.sha256(migration._canonical(self.expected)).hexdigest()
        self.source.save()
        return row, version

    def test_valid_credential_reference_survives_without_decryption_or_plaintext(self):
        row, version = self._add_credential_configuration()
        original_ciphertext = version.ciphertext_b64
        self.apply()
        active = self.Revision.objects.get(scope_type="system", activation_state="active")
        retained = self.Value.objects.get(revision=active, definition=row.definition)
        self.assertEqual(retained.credential_version_id, version.pk)
        self.assertEqual((retained.value, retained.value_size_bytes, retained.value_sha256), (None, 0, ""))
        version.refresh_from_db()
        self.assertEqual(version.ciphertext_b64, original_ciphertext)
        expected = {key: value for key, value in self.expected.items() if key not in migration.OBSOLETE_KEYS}
        expected[migration.RECOMMENDATION_KEY] = ""
        self.assertEqual(active.values_sha256, migration.hashlib.sha256(migration._canonical(expected)).hexdigest())

    def test_credential_plaintext_or_missing_reference_is_rejected_without_partial_cleanup(self):
        row, _version = self._add_credential_configuration()
        for changes in ({"value": "must-not-be-copied"}, {"credential_version_id": None},
                {"value_size_bytes": 4}, {"value_schema_version": 0}):
            with self.subTest(changes=changes):
                self.Value.objects.filter(pk=row.pk).update(value=None, credential_version_id=row.credential_version_id,
                    value_size_bytes=0, value_schema_version=1)
                self.Value.objects.filter(pk=row.pk).update(**changes)
                with self.assertRaisesMessage(RuntimeError, "原配置值摘要不一致"):
                    self.apply()
                self.assertTrue(self.Revision.objects.filter(pk=self.source.pk).exists())
                self.assertFalse(self.Definition.objects.filter(key=migration.RECOMMENDATION_KEY).exists())
