from studio.tests.test_configuration_migrations import _MigrationTestCase


class ComfySavedWorkflowDirectoryMigrationTests(_MigrationTestCase):
    migrate_from = "0133_comfy_workflow_source_scope"
    migrate_to = "0134_comfy_saved_workflow_directory_config"

    config_key = "workflow.comfyui_saved_workflow_directory"

    def set_up_before_migration(self, apps):
        Definition = apps.get_model("studio", "ConfigDefinition")
        Revision = apps.get_model("studio", "ConfigRevision")
        Value = apps.get_model("studio", "ConfigValue")
        definition_ids = Definition.objects.filter(
            key=self.config_key,
        ).values_list("id", flat=True)
        Value.objects.filter(definition_id__in=definition_ids).delete()
        Definition.objects.filter(key=self.config_key).delete()
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

    def test_forward_registers_directory_and_clones_active_revision(self):
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
        self.assertEqual(values[self.config_key], "workflows")

        definition = Definition.objects.get(key=self.config_key)
        self.assertEqual(definition.configuration_class, "standard")
        self.assertEqual(definition.domain, "workflow")
        self.assertEqual(definition.value_type, "string")
        self.assertEqual(definition.allowed_scopes, "system")
        self.assertFalse(definition.restart_required)
        self.assertTrue(definition.is_required)
        self.assertEqual(
            definition.value_schema["format"],
            "comfyui-saved-workflow-directory",
        )
