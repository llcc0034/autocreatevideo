"""0227 on isolated configuration facts; never migrate the running database."""
import importlib
from types import SimpleNamespace
from unittest.mock import patch

from django.apps import apps
from django.contrib.auth import get_user_model
from django.core.exceptions import ValidationError
from django.db import connection, transaction
from django.test import TestCase
from rest_framework.test import APIClient

from studio.models import ConfigDefinition, ConfigRevision, ConfigValue, SeriesProject
from studio.services.configuration import create_config_revision, resolve_configuration
from studio.services.json_contracts import assign_json_contract
from studio.tests.test_execution_context_inputs import PROJECT_CONFIG


migration = importlib.import_module("studio.migrations.0227_retire_script_breakdown_policies")


class RetireScriptBreakdownPoliciesTests(TestCase):
    def setUp(self):
        self.actor = get_user_model().objects.create_superuser(username="retired-split-policy", password="isolated")
        self.client = APIClient(); self.client.force_authenticate(self.actor)

    def test_catalog_resolution_and_new_revisions_exclude_all_retired_settings(self):
        values = resolve_configuration().values
        for key in migration.KEYS:
            definition = ConfigDefinition.objects.get(key=key)
            self.assertFalse(definition.is_active)
            self.assertFalse(definition.is_required)
            self.assertNotIn(key, values)
            response = self.client.get("/api/v1/config-definitions/", {"search": key, "is_active": "true"})
            self.assertEqual(response.status_code, 200, response.data)
            self.assertNotIn(key, str(response.data))
            with self.assertRaises(ValidationError):
                create_config_revision(scope_type="system", values={key: 1}, created_by=self.actor)

    def test_production_capability_does_not_require_or_expose_split_values(self):
        from studio.capability_views import ProductionCapabilityView
        self.assertFalse(set(migration.KEYS) & set(ProductionCapabilityView.CONFIG_KEYS))
        with patch("studio.capability_views.configured_video_conditioning_modes", return_value=[]):
            result = self.client.get("/api/v1/capabilities/production/")
        self.assertEqual(result.status_code, 200, result.data)
        self.assertNotIn("script_breakdown_auto_split_trigger_chars", result.data)
        self.assertNotIn("script_breakdown_auto_split_target_chars", result.data)

    def seed_previous(self):
        project = SeriesProject.objects.create(owner=self.actor, title="Isolated retirement", **PROJECT_CONFIG)
        system = ConfigRevision.objects.get(scope_type="system", status="published", activation_state="active")
        override = ConfigRevision.objects.create(scope_type="project", project=project, revision=1,
            status="published", activation_state="active")
        draft = ConfigRevision.objects.create(scope_type="project", project=project, revision=2, status="draft")
        for definition in ConfigDefinition.objects.filter(key__in=migration.KEYS):
            ConfigDefinition.objects.filter(pk=definition.pk).update(is_active=True, is_required=True)
            for revision in (system, override, draft):
                row = ConfigValue(revision=revision, definition=definition)
                assign_json_contract(row, "value", {"isolated_old_value": definition.key}); row.save()
        keep = ConfigDefinition.objects.get(key="production.default_include_audio")
        for revision in (override, draft):
            row = ConfigValue(revision=revision, definition=keep)
            assign_json_contract(row, "value", False); row.save()
        return system, override, draft

    def test_successors_preserve_unrelated_values_and_original_rows_without_duplicate_successors(self):
        from django.test.utils import CaptureQueriesContext
        system, override, draft = self.seed_previous()
        columns = ("definition_id", "value", "value_schema_version", "value_size_bytes", "value_sha256", "credential_version_id")
        before = {row.pk: list(row.values.order_by("definition_id").values(*columns)) for row in (system, override, draft)}
        draft_before = ConfigRevision.objects.values().get(pk=draft.pk)
        retired_ids = set(ConfigDefinition.objects.filter(key__in=migration.KEYS).values_list("pk", flat=True))
        with transaction.atomic():
            migration.forward(apps, SimpleNamespace(connection=connection))
        for old in (system, override):
            old.refresh_from_db()
            self.assertEqual((old.status, old.activation_state), ("superseded", "inactive"))
            self.assertEqual(list(old.values.order_by("definition_id").values(*columns)), before[old.pk])
            new = ConfigRevision.objects.get(based_on_revision=old, activation_state="active")
            self.assertEqual(list(new.values.order_by("definition_id").values(*columns)),
                [row for row in before[old.pk] if row["definition_id"] not in retired_ids])
            self.assertEqual(new.revision, 3 if old.pk == override.pk else old.revision + 1)
        self.assertEqual(ConfigRevision.objects.values().get(pk=draft.pk), draft_before)
        self.assertEqual(list(draft.values.order_by("definition_id").values(*columns)), before[draft.pk])
        self.assertFalse(resolve_configuration(project=override.project).values["production.default_include_audio"])
        total = ConfigRevision.objects.count()
        with CaptureQueriesContext(connection) as queries:
            migration.forward(apps, SimpleNamespace(connection=connection))
        self.assertEqual(ConfigRevision.objects.count(), total)
        self.assertFalse(any('INSERT INTO "studio_configrevision"' in row["sql"] for row in queries))

    def test_migration_failure_rolls_back_catalog_and_successor_in_its_transaction(self):
        self.seed_previous()
        models = (ConfigDefinition, ConfigRevision, ConfigValue)
        before = {model.__name__: list(model.objects.order_by("pk").values()) for model in models}
        with patch.object(ConfigValue.objects.get_queryset().__class__, "bulk_create", side_effect=RuntimeError("isolated failure")):
            with self.assertRaises(RuntimeError), transaction.atomic():
                migration.forward(apps, SimpleNamespace(connection=connection))
        self.assertEqual({model.__name__: list(model.objects.order_by("pk").values()) for model in models}, before)
