from __future__ import annotations

import hashlib
import json

from django.db import connection
from django.db.migrations.executor import MigrationExecutor

from studio.tests.test_configuration_migrations import _MigrationTestCase


OLD_KEY = "production.h3_prompt_language_gate"
NEW_KEY = "production.prompt_language_gate"


def _canonical(value):
    return json.dumps(
        value,
        ensure_ascii=False,
        sort_keys=True,
        separators=(",", ":"),
        allow_nan=False,
    ).encode("utf-8")


def _revision_digest(ConfigValue, revision_id):
    values = {}
    rows = ConfigValue.objects.filter(revision_id=revision_id).select_related(
        "definition", "credential_version",
    )
    for row in rows:
        if row.definition.is_secret:
            version = row.credential_version
            identity = f"version:{version.id}:{version.version}"
            values[row.definition.key] = {
                "credential_configured": True,
                "credential_binding_sha256": hashlib.sha256(
                    identity.encode("utf-8"),
                ).hexdigest(),
            }
        else:
            values[row.definition.key] = row.value
    return hashlib.sha256(_canonical(values)).hexdigest()


class PromptLanguageGateConfigMigrationTests(_MigrationTestCase):
    bootstrap_from = "0049_database_runtime_configuration"
    migrate_from = "0074_generic_runtime_contracts"
    migrate_to = "0075_generic_prompt_language_gate_config"

    def set_up_before_migration(self, apps):
        ConfigDefinition = apps.get_model("studio", "ConfigDefinition")
        ConfigRevision = apps.get_model("studio", "ConfigRevision")
        ConfigValue = apps.get_model("studio", "ConfigValue")
        definition = ConfigDefinition.objects.get(key=OLD_KEY)
        active = ConfigRevision.objects.get(
            scope_type="system",
            status="published",
            activation_state="active",
        )
        value = ConfigValue.objects.get(
            revision=active,
            definition=definition,
        )
        encoded = _canonical("warn")
        value.value = "warn"
        value.value_size_bytes = len(encoded)
        value.value_sha256 = hashlib.sha256(encoded).hexdigest()
        value.save(update_fields=(
            "value", "value_size_bytes", "value_sha256", "updated_at",
        ))
        active.values_sha256 = _revision_digest(ConfigValue, active.id)
        active.save(update_fields=("values_sha256", "updated_at"))
        self.definition_id = definition.id
        self.active_revision_id = active.id
        self.value_id = value.id
        self.value_contract = (
            value.value,
            value.value_schema_version,
            value.value_size_bytes,
            value.value_sha256,
            value.credential_version_id,
        )
        affected_values = ConfigValue.objects.filter(definition_id=definition.id)
        self.affected_revision_ids = set(
            affected_values.values_list("revision_id", flat=True)
        )
        self.affected_value_contracts = {
            row.id: (
                row.value,
                row.value_schema_version,
                row.value_size_bytes,
                row.value_sha256,
                row.credential_version_id,
            )
            for row in affected_values
        }
        self.assertGreater(len(self.affected_revision_ids), 1)

    def test_generic_key_replaces_old_key_without_losing_published_value(self):
        ConfigDefinition = self.apps.get_model("studio", "ConfigDefinition")
        ConfigRevision = self.apps.get_model("studio", "ConfigRevision")
        ConfigValue = self.apps.get_model("studio", "ConfigValue")

        self.assertFalse(ConfigDefinition.objects.filter(key=OLD_KEY).exists())
        definition = ConfigDefinition.objects.get(key=NEW_KEY)
        self.assertEqual(definition.id, self.definition_id)
        self.assertEqual(
            definition.description,
            "生产提示词仍含待改写内容时的通用语言门禁策略",
        )
        value = ConfigValue.objects.get(pk=self.value_id)
        self.assertEqual(value.definition_id, definition.id)
        self.assertEqual((
            value.value,
            value.value_schema_version,
            value.value_size_bytes,
            value.value_sha256,
            value.credential_version_id,
        ), self.value_contract)
        active = ConfigRevision.objects.get(pk=self.active_revision_id)
        self.assertEqual(active.values_sha256, _revision_digest(ConfigValue, active.id))
        migrated_values = ConfigValue.objects.filter(definition_id=definition.id)
        self.assertEqual(
            {
                row.id: (
                    row.value,
                    row.value_schema_version,
                    row.value_size_bytes,
                    row.value_sha256,
                    row.credential_version_id,
                )
                for row in migrated_values
            },
            self.affected_value_contracts,
        )
        self.assertEqual(
            set(migrated_values.values_list("revision_id", flat=True)),
            self.affected_revision_ids,
        )
        for revision in ConfigRevision.objects.filter(
            id__in=self.affected_revision_ids,
        ):
            self.assertEqual(
                revision.values_sha256,
                _revision_digest(ConfigValue, revision.id),
            )


class PromptLanguageGateConfigConflictMigrationTests(_MigrationTestCase):
    bootstrap_from = "0049_database_runtime_configuration"
    migrate_from = "0074_generic_runtime_contracts"
    migrate_to = "0075_generic_prompt_language_gate_config"
    migrate_to_in_setup = False

    def set_up_before_migration(self, apps):
        ConfigDefinition = apps.get_model("studio", "ConfigDefinition")
        old = ConfigDefinition.objects.get(key=OLD_KEY)
        values = {
            field.name: getattr(old, field.name)
            for field in old._meta.fields
            if field.name not in {"id", "key", "created_at", "updated_at"}
        }
        ConfigDefinition.objects.create(key=NEW_KEY, **values)

    def test_migration_fails_closed_when_old_and_new_definitions_coexist(self):
        ConfigDefinition = self.apps.get_model("studio", "ConfigDefinition")

        executor = MigrationExecutor(connection)
        with self.assertRaisesRegex(RuntimeError, "同时存在"):
            executor.migrate([("studio", self.migrate_to)])

        ConfigDefinition.objects.filter(key=NEW_KEY).delete()
        executor = MigrationExecutor(connection)
        executor.migrate([("studio", self.migrate_to)])
