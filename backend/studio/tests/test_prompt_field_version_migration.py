from studio.tests.test_configuration_migrations import _MigrationTestCase


class PromptFieldVersionMigrationTests(_MigrationTestCase):
    migrate_from = "0200_prompt_rule_value_ownership"
    migrate_to = "0201_prompt_rule_field_versions"

    def set_up_before_migration(self, apps):
        definition = apps.get_model("studio", "FieldDefinition").objects.create(key="untouched_rule", label="测试", category="custom")
        profile = apps.get_model("studio", "PromptProfileVersion").objects.create(profile_key="untouched-profile", version=1,
            name="测试", model_family="test", operation="video.generate", status="published", contract_schema_version=7,
            contract_sha256="a" * 64, contract_size_bytes=876)
        self.profile_id = profile.pk
        self.rule_id = apps.get_model("studio", "PromptProfileFieldRule").objects.create(profile_version=profile,
            field_definition=definition, source_selector="request.theme", channel="positive_prompt", position=1).pk

    def test_old_rows_stay_unassigned_and_old_contract_is_not_rewritten(self):
        rule = self.apps.get_model("studio", "PromptProfileFieldRule").objects.get(pk=self.rule_id)
        profile_model = self.apps.get_model("studio", "PromptProfileVersion")
        profile = profile_model.objects.get(pk=self.profile_id)
        self.assertIsNone(rule.field_definition_version_id)
        self.assertEqual((profile.status, profile.contract_schema_version, profile.contract_sha256, profile.contract_size_bytes),
            ("published", 7, "a" * 64, 876))
        self.assertEqual(profile_model._meta.get_field("contract_schema_version").default, 8)
