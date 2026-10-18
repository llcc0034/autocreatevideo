"""Exact data-migration function on isolated rows; no live configuration writes."""
import importlib
from types import SimpleNamespace

from django.apps import apps
from django.contrib.auth import get_user_model
from django.db import connection
from django.test import TestCase
from rest_framework.test import APIClient

from studio.models import ConfigDefinition, ConfigRevision, ConfigValue, SeriesProject
from studio.services.configuration import resolve_configuration, create_config_revision
from studio.services.json_contracts import assign_json_contract
from studio.tests.test_execution_context_inputs import PROJECT_CONFIG


migration = importlib.import_module("studio.migrations.0214_retire_script_resource_advisory")


class RetireScriptResourceAdvisoryTests(TestCase):
    def test_catalog_hides_retired_key_and_new_configuration_rejects_it(self):
        from django.core.exceptions import ValidationError
        definition = ConfigDefinition.objects.get(key=migration.KEY)
        self.assertFalse(definition.is_active)
        self.assertFalse(definition.is_required)
        self.assertNotIn(migration.KEY, resolve_configuration().values)
        actor = get_user_model().objects.create_superuser(username="config-reader", password="isolated")
        client = APIClient(); client.force_authenticate(actor)
        response = client.get("/api/v1/config-definitions/", {"search": migration.KEY, "is_active": "true"})
        self.assertEqual(response.status_code, 200, response.data)
        self.assertNotIn(migration.KEY, str(response.data))
        with self.assertRaises(ValidationError):
            create_config_revision(scope_type="system", values={migration.KEY: {}},
                created_by=actor, change_summary="isolated rejected old setting")

    def test_retires_system_and_project_overrides_once_without_changing_prior_values(self):
        # Simulate a pre-cutover configuration on the already migrated test DB.
        actor = get_user_model().objects.create_user(username="config-migration-owner")
        project = SeriesProject.objects.create(owner=actor, title="Isolated settings", **PROJECT_CONFIG)
        definition = ConfigDefinition.objects.get(key=migration.KEY)
        ConfigDefinition.objects.filter(pk=definition.pk).update(is_active=True, is_required=True)
        system = ConfigRevision.objects.get(scope_type="system", status="published", activation_state="active")
        override = ConfigRevision.objects.create(scope_type="project", project=project,
            revision=1, status="published", activation_state="active")
        keep = ConfigDefinition.objects.get(key="production.default_include_audio")
        inherited = ConfigValue.objects.get(revision=system, definition=keep)
        copied = ConfigValue(revision=override, definition=keep)
        assign_json_contract(copied, "value", inherited.value); copied.save()
        for revision in (system, override):
            value = ConfigValue(revision=revision, definition=definition)
            assign_json_contract(value, "value", {"obsolete_fixture": True}); value.save()
        columns = ("id", "definition_id", "value", "value_schema_version", "value_size_bytes", "value_sha256", "credential_version_id")
        before = {row.pk: list(row.values.values(*columns)) for row in (system, override)}
        migration.forward(apps, SimpleNamespace(connection=connection))
        for previous in (system, override):
            previous.refresh_from_db()
            self.assertEqual((previous.status, previous.activation_state), ("superseded", "inactive"))
            self.assertEqual(list(previous.values.values(*columns)), before[previous.pk])
            successor = ConfigRevision.objects.get(based_on_revision=previous, activation_state="active")
            self.assertEqual((successor.scope_type, successor.project_id, successor.revision),
                (previous.scope_type, previous.project_id, previous.revision + 1))
            self.assertFalse(successor.values.filter(definition=definition).exists())
            preserved = {str(row["definition_id"]): {k: v for k, v in row.items() if k != "id"}
                for row in before[previous.pk] if row["definition_id"] != definition.pk}
            self.assertEqual({str(row["definition_id"]): row for row in successor.values.values(*columns[1:])}, preserved)
        resolved = resolve_configuration(project=project)
        self.assertNotIn(migration.KEY, resolved.values)
        self.assertEqual(resolved.values[keep.key], inherited.value)
        total = ConfigRevision.objects.count()
        migration.forward(apps, SimpleNamespace(connection=connection))
        self.assertEqual(ConfigRevision.objects.count(), total)
