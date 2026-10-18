"""Rehearse 0144 configuration cleanup -> formal authoring -> current schema.

Only synthetic rows in the isolated migration database are changed. This is
not a cleanup command and never invokes external models or platform services.
"""
import hashlib
import importlib
from collections import defaultdict
from types import SimpleNamespace

from django.contrib.auth import get_user_model
from django.core.exceptions import ValidationError
from django.db import connection, transaction
from django.db.migrations.executor import MigrationExecutor

from studio.management.commands.purge_incompatible_workflows import (
    _build_nullifications, _expand_reverse_dependencies, _hard_delete_plan,
)
from studio.management.execution_cutover_inventory import execution_cutover_inventory
from studio.services.configuration import create_config_revision, publish_config_revision, resolve_configuration
from studio.services.json_contracts import canonical_json_bytes, json_contract
from studio.tests.test_configuration_migrations import _MigrationTestCase


migration = importlib.import_module("studio.migrations.0166_execution_recommendation_configuration")
old_reference = importlib.import_module("studio.migrations.0060_prompt_normalization_resource_reference")


class ExecutionConfigurationCutoverRehearsalTests(_MigrationTestCase):
    migrate_from = "0144_execution_policy_configuration"
    migrate_to = "0170_prompt_profile_current_schema_default"
    migrate_to_in_setup = False

    def set_up_before_migration(self, apps):
        self.actor = get_user_model().objects.create_user(username="isolated-cutover", is_superuser=True, is_staff=True)
        self.Definition = apps.get_model("studio", "ConfigDefinition")
        self.Revision = apps.get_model("studio", "ConfigRevision")
        self.Value = apps.get_model("studio", "ConfigValue")
        # Downgrading the schema does not recreate intentionally deleted data.
        # Build this one known source-state fixture, not a legacy converter.
        self.Value.objects.filter(definition__key=migration.RECOMMENDATION_KEY).delete()
        self.Definition.objects.filter(key=migration.RECOMMENDATION_KEY).delete()
        self.source = self.Revision.objects.get(scope_type="system", activation_state="active")
        definition = self.Definition.objects.create(key=old_reference.KEY, domain="production",
            value_type="string", is_active=True, is_required=True, allowed_scopes="system,project,run",
            **migration._metadata("value_schema", old_reference.NEW_SCHEMA),
            **migration._metadata("initial_value", ""))
        self.Value.objects.create(revision=self.source, definition=definition, **migration._metadata("value", ""))
        self.values = {row.definition.key: row.value for row in self.source.values.select_related("definition")}
        self.source.created_by_id = self.actor.pk
        self.source.values_sha256 = hashlib.sha256(canonical_json_bytes(self.values)).hexdigest()
        self.source.save()
        self.preserved = {
            name: list(apps.get_model("studio", name).objects.order_by("pk").values())
            for name in ("ModelDeployment", "ModelProfileVersion", "PromptProfileVersion", "Asset")}
        self.report = execution_cutover_inventory()

    def rebuild_using_existing_services(self, values):
        selected = defaultdict(set)
        revision_ids = [row["id"] for row in self.report["configuration"]["affected_revisions"]]
        selected[self.Revision].update(self.Revision.objects.filter(pk__in=revision_ids).values_list("pk", flat=True))
        selected[self.Definition].update(self.Definition.objects.filter(
            key__in=migration.OBSOLETE_KEYS).values_list("pk", flat=True))
        _expand_reverse_dependencies(selected)
        plan = SimpleNamespace(selected=selected, nullifications=_build_nullifications(selected))
        with transaction.atomic():
            _hard_delete_plan(plan)
            revision = create_config_revision(scope_type="system", values=values, created_by=self.actor,
                clone_published=False, change_summary="Isolated reviewed cutover baseline; no inferred AI target")
            return publish_config_revision(revision, published_by=self.actor)

    def retained_values(self):
        return {key: {"value": value} for key, value in self.values.items() if key not in migration.OBSOLETE_KEYS}

    def test_formal_authoring_restores_complete_baseline_before_schema_migration(self):
        self.assertEqual(self.report["native_resource_contract_checks"]["status"], "blocked_by_configuration")
        self.assertEqual(self.report["configuration"]["active_baselines"][0]["storage_integrity"], "valid")
        revision = self.rebuild_using_existing_services(self.retained_values())
        self.assertEqual(revision.activation_state, "active")
        self.assertIsNone(revision.based_on_revision_id)
        self.assertFalse(self.Revision.objects.filter(pk=self.source.pk).exists())
        resolved = resolve_configuration()
        expected = {key: raw["value"] for key, raw in self.retained_values().items()}
        self.assertEqual(resolved.values, expected)
        executor = MigrationExecutor(connection)
        executor.migrate([("studio", self.migrate_to)])
        self.assertEqual(resolve_configuration().values, {**expected, migration.RECOMMENDATION_KEY: ""})
        registry = executor.loader.project_state([("studio", self.migrate_to)]).apps
        for name, rows in self.preserved.items():
            self.assertEqual(list(registry.get_model("studio", name).objects.order_by("pk").values()), rows)
        self.assertFalse(registry.get_model("studio", "ModelExecutionInstanceVersion").objects.exists())
        self.assertFalse(registry.get_model("studio", "ModelDeploymentSmokeAttempt").objects.exists())
        self.assertEqual(registry.get_model("studio", "ConfigRevision").objects.get(pk=revision.pk).values_sha256,
            json_contract(expected).sha256)

    def test_rejected_new_configuration_rolls_back_deletion_and_preserves_original_hashes(self):
        before_revisions = list(self.Revision.objects.order_by("pk").values())
        before_values = list(self.Value.objects.order_by("pk").values())
        values = self.retained_values()
        values["api.default_page_size"] = {"value": -1}
        with self.assertRaises(ValidationError):
            self.rebuild_using_existing_services(values)
        self.assertEqual(list(self.Revision.objects.order_by("pk").values()), before_revisions)
        self.assertEqual(list(self.Value.objects.order_by("pk").values()), before_values)
        self.assertTrue(self.Definition.objects.filter(key=old_reference.KEY).exists())
