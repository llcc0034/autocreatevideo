from studio.tests.test_configuration_migrations import _MigrationTestCase


class OperationalListAndRecoveryLimitsMigrationTests(_MigrationTestCase):
    migrate_from = "0124_remove_prompt_artifact_business_defaults"
    migrate_to = "0125_configurable_operational_list_and_recovery_limits"

    defaults = {
        "runtime.asset_text_submission_grace_seconds": 60,
        "runtime.asset_uncertain_reconcile_batch_size": 32,
        "api.default_page_size": 24,
        "workflow.node_metadata_default_page_size": 50,
        "workflow.node_metadata_input_preview_limit": 100,
        "security.runtime.max_reconcile_batch_items": 1000,
        "security.api.max_search_chars": 4096,
    }

    def set_up_before_migration(self, apps):
        Definition = apps.get_model("studio", "ConfigDefinition")
        Revision = apps.get_model("studio", "ConfigRevision")
        Value = apps.get_model("studio", "ConfigValue")
        definition_ids = Definition.objects.filter(
            key__in=self.defaults,
        ).values_list("id", flat=True)
        Value.objects.filter(definition_id__in=definition_ids).delete()
        Definition.objects.filter(key__in=self.defaults).delete()
        active = Revision.objects.get(
            scope_type="system",
            status="published",
            activation_state="active",
        )
        self.previous_revision_id = active.id
        self.previous_values = dict(
            Value.objects.filter(revision=active).values_list(
                "definition__key", "value",
            )
        )

    def test_forward_registers_limits_and_clones_active_revision(self):
        Definition = self.apps.get_model("studio", "ConfigDefinition")
        Revision = self.apps.get_model("studio", "ConfigRevision")
        Value = self.apps.get_model("studio", "ConfigValue")
        active = Revision.objects.get(
            scope_type="system",
            status="published",
            activation_state="active",
        )
        self.assertNotEqual(active.id, self.previous_revision_id)
        self.assertEqual(active.based_on_revision_id, self.previous_revision_id)
        values = dict(
            Value.objects.filter(revision=active).values_list(
                "definition__key", "value",
            )
        )
        for key, value in self.previous_values.items():
            self.assertEqual(values[key], value)
        for key, value in self.defaults.items():
            self.assertEqual(values[key], value)
            self.assertTrue(Definition.objects.get(key=key).is_required)

        for key in (
            "security.runtime.max_reconcile_batch_items",
            "security.api.max_search_chars",
        ):
            boundary = Definition.objects.get(key=key)
            self.assertEqual(boundary.configuration_class, "security_boundary")
            self.assertEqual(boundary.allowed_scopes, "system")

        self.assertEqual(
            Definition.objects.get(
                key="runtime.asset_uncertain_reconcile_batch_size",
            ).value_schema["security_ceiling_key"],
            "security.runtime.max_reconcile_batch_items",
        )
        self.assertEqual(
            Definition.objects.get(
                key="api.default_page_size",
            ).value_schema["security_ceiling_key"],
            "security.api.max_page_size",
        )
        self.assertEqual(
            Definition.objects.get(
                key="runtime.asset_text_submission_grace_seconds",
            ).value_schema["security_ceiling_key"],
            "security.runtime.max_interval_seconds",
        )
        self.assertEqual(
            Definition.objects.get(
                key="workflow.node_metadata_default_page_size",
            ).value_schema["security_ceiling_key"],
            "security.api.max_page_size",
        )
        self.assertEqual(
            Definition.objects.get(
                key="workflow.node_metadata_input_preview_limit",
            ).value_schema["security_ceiling_key"],
            "security.comfy.client.list_max_items",
        )
