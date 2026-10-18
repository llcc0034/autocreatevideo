from studio.tests.test_configuration_migrations import _MigrationTestCase


class FieldDefinitionVersionMigrationTests(_MigrationTestCase):
    migrate_from = "0198_script_bible_source_bindings"
    migrate_to = "0199_field_definition_versions"

    def set_up_before_migration(self, apps):
        Definition = apps.get_model("studio", "PromptFieldDefinition")
        Profile = apps.get_model("studio", "PromptProfileVersion")
        Rule = apps.get_model("studio", "PromptProfileFieldRule")
        self.ids = {}
        for kind in ("text", "number", "string_list", "enum"):
            field = Definition.objects.create(
                key=f"migration_field_{kind}", label=f"定义{kind}", category="custom",
                value_type=kind, value_options=["a", "b"] if kind == "enum" else [],
                default_value="旧值", is_required=True,
            )
            self.ids[kind] = field.pk
        self.profile = Profile.objects.create(
            profile_key="field-migration", version=1, name="fixture", model_family="fixture", operation="fixture",
        )
        self.rule_id = Rule.objects.create(
            profile_version=self.profile, field_definition_id=self.ids["text"],
            source_selector="definition.default", channel="positive_prompt", position=1,
        ).pk
        self.existing_ids = set(Definition.objects.values_list("pk", flat=True))

    def test_rename_preserves_ids_relations_and_only_seeds_explicit_drafts(self):
        Definition = self.apps.get_model("studio", "FieldDefinition")
        Version = self.apps.get_model("studio", "FieldDefinitionVersion")
        Rule = self.apps.get_model("studio", "PromptProfileFieldRule")
        self.assertEqual(set(Definition.objects.values_list("pk", flat=True)), self.existing_ids)
        self.assertEqual(Rule.objects.get(pk=self.rule_id).field_definition_id, self.ids["text"])
        self.assertEqual(Version.objects.count(), len(self.existing_ids))
        self.assertFalse(Version.objects.exclude(status="draft").exists())
        for old, new in (("text", "text"), ("number", "decimal"), ("string_list", "list"), ("enum", "enum")):
            version = Version.objects.get(field_definition_id=self.ids[old])
            self.assertEqual((version.version, version.value_type, version.unit), (1, new, ""))
            self.assertEqual(version.label, f"定义{old}")
        self.assertNotIn("default_value", {field.name for field in Version._meta.fields})
        self.assertNotIn("is_required", {field.name for field in Version._meta.fields})
