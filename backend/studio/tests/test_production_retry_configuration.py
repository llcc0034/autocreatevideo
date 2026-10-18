"""Migration/configuration contract, no application database mutation."""
import importlib
from types import SimpleNamespace

from django.apps import apps
from django.db import connection
from django.test import TestCase

from studio.models import ConfigDefinition, ConfigRevision, ConfigValue
from studio.services.configuration import resolve_configuration


class RetryConfigurationTests(TestCase):
    def test_policy_is_single_active_key_and_migration_replay_is_noop(self):
        migration = importlib.import_module("studio.migrations.0237_unify_production_retry_policy")
        old = ConfigDefinition.objects.get(key="production.prompt_field_retry_limit")
        self.assertFalse(old.is_active)
        values = resolve_configuration().values
        self.assertNotIn(old.key, values)
        self.assertGreaterEqual(values["production.task_max_attempts"], 1)
        self.assertTrue(ConfigValue.objects.filter(definition=old).exists())  # historical values retained
        before = list(ConfigRevision.objects.values_list("pk", "values_sha256"))
        migration.forward(apps, SimpleNamespace(connection=connection))
        self.assertEqual(before, list(ConfigRevision.objects.values_list("pk", "values_sha256")))
