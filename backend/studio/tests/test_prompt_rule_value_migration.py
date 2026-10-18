from studio.tests.test_configuration_migrations import _MigrationTestCase


class PromptRuleValueMigrationTests(_MigrationTestCase):
    migrate_from = "0199_field_definition_versions"
    migrate_to = "0200_prompt_rule_value_ownership"

    def set_up_before_migration(self, apps):
        Definition = apps.get_model("studio", "FieldDefinition")
        Profile = apps.get_model("studio", "PromptProfileVersion")
        Rule = apps.get_model("studio", "PromptProfileFieldRule")
        field = Definition.objects.create(key="old_global_usage", label="旧字段", category="test",
            value_type="text", default_value="旧全局内容", is_required=True)
        profile = Profile.objects.create(profile_key="old-frozen-usage", version=1, name="旧配置",
            model_family="test", operation="video.generate", status="published", contract_schema_version=6,
            contract_size_bytes=123, contract_sha256="a" * 64)
        rule = Rule.objects.create(profile_version=profile, field_definition=field,
            source_selector="definition.default", generation_source_selector="definition.default",
            position=1, channel="positive_prompt", required=False)
        self.field_id, self.profile_id, self.rule_id = field.pk, profile.pk, rule.pk

    def test_removes_global_usage_without_rewriting_old_frozen_contract_as_new(self):
        Definition = self.apps.get_model("studio", "FieldDefinition")
        Profile = self.apps.get_model("studio", "PromptProfileVersion")
        Rule = self.apps.get_model("studio", "PromptProfileFieldRule")
        self.assertTrue(Definition.objects.filter(pk=self.field_id).exists())
        self.assertFalse({"default_value", "is_required"} & {item.name for item in Definition._meta.fields})
        profile = Profile.objects.get(pk=self.profile_id)
        self.assertEqual((profile.status, profile.contract_schema_version, profile.contract_size_bytes, profile.contract_sha256),
            ("published", 6, 123, "a" * 64))
        rule = Rule.objects.get(pk=self.rule_id)
        self.assertEqual(rule.field_definition_id, self.field_id)
        self.assertEqual(rule.source_selector, "definition.default")
        self.assertEqual((rule.required, rule.has_default, rule.default_value, rule.fixed_value), (False, False, None, None))
        self.assertEqual(Profile._meta.get_field("contract_schema_version").get_default(), 7)
