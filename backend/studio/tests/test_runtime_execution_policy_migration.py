from studio.tests.test_configuration_migrations import _MigrationTestCase


class RuntimeExecutionPolicyMigrationTests(_MigrationTestCase):
    migrate_from = "0119_content_contract_policy"
    migrate_to = "0120_runtime_execution_policy"

    defaults = {
        "production.project_creation_policy": {
            "schema_version": 1,
            "width": 768,
            "height": 1344,
            "fps": 24,
            "default_shot_duration_ms": 5000,
            "default_episode_count": 1,
            "default_shots_per_episode": 8,
            "default_episode_duration_seconds": 90,
            "default_max_generation_tasks": 20,
            "continuity_seed_policy": {"mode": "random_per_project", "value": 1},
        },
        "workflow.smoke_poll_interval_seconds": 0.25,
        "runtime.worker_stale_recovery_minutes": 30,
        "runtime.deferred_generation_retry_backoff_seconds": 30,
        "delivery.assembly_max_attempts": 3,
        "security.delivery.assembly_max_attempts": 10,
    }

    def set_up_before_migration(self, apps):
        Definition = apps.get_model("studio", "ConfigDefinition")
        Revision = apps.get_model("studio", "ConfigRevision")
        Value = apps.get_model("studio", "ConfigValue")
        definition_ids = Definition.objects.filter(key__in=self.defaults).values_list("id", flat=True)
        Value.objects.filter(definition_id__in=definition_ids).delete()
        Definition.objects.filter(key__in=self.defaults).delete()
        active = Revision.objects.get(
            scope_type="system", status="published", activation_state="active",
        )
        self.previous_revision_id = active.id
        self.previous_values = dict(
            Value.objects.filter(revision=active).values_list("definition__key", "value"),
        )

    def test_forward_registers_execution_policies_and_clones_active_revision(self):
        Definition = self.apps.get_model("studio", "ConfigDefinition")
        Revision = self.apps.get_model("studio", "ConfigRevision")
        Value = self.apps.get_model("studio", "ConfigValue")
        active = Revision.objects.get(
            scope_type="system", status="published", activation_state="active",
        )
        self.assertNotEqual(active.id, self.previous_revision_id)
        self.assertEqual(active.based_on_revision_id, self.previous_revision_id)
        values = dict(Value.objects.filter(revision=active).values_list("definition__key", "value"))
        for key, value in self.previous_values.items():
            self.assertEqual(values[key], value)
        for key, value in self.defaults.items():
            self.assertEqual(values[key], value)
            self.assertTrue(Definition.objects.get(key=key).is_required)

        creation = Definition.objects.get(key="production.project_creation_policy")
        self.assertEqual(creation.allowed_scopes, "system")
        self.assertTrue(creation.value_schema["closed_contract"])
        self.assertEqual(
            creation.value_schema["security_ceiling_paths"]["fps"],
            "security.media.max_fps",
        )
        self.assertEqual(
            creation.value_schema["security_ceiling_paths"]["default_max_generation_tasks"],
            "security.production.max_generation_tasks",
        )
        assembly_limit = Definition.objects.get(key="delivery.assembly_max_attempts")
        self.assertEqual(
            assembly_limit.value_schema["security_ceiling_key"],
            "security.delivery.assembly_max_attempts",
        )
