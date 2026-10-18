from django.core.exceptions import FieldDoesNotExist
from studio.tests.test_configuration_migrations import _MigrationTestCase


class FieldIdentityMigrationTests(_MigrationTestCase):
    migrate_from = "0201_prompt_rule_field_versions"
    migrate_to = "0202_field_identity_only"

    def set_up_before_migration(self, apps):
        definition = apps.get_model("studio", "FieldDefinition").objects.create(
            key="preserved_identity", label="不再使用的标签", category="old", value_type="text", display_order=29)
        self.identity_id = definition.pk
        version = apps.get_model("studio", "FieldDefinitionVersion").objects.create(field_definition=definition,
            version=3, status="published", label="冻结含义", category="custom", value_type="enum", unit="份", value_options=["甲", "乙"])
        self.version_id = version.pk
        profile = apps.get_model("studio", "PromptProfileVersion").objects.create(profile_key="migration-field", version=1,
            name="隔离夹具", model_family="test", operation="video.generate", status="published", contract_schema_version=8,
            contract_sha256="b" * 64, contract_size_bytes=456)
        self.rule_id = apps.get_model("studio", "PromptProfileFieldRule").objects.create(profile_version=profile,
            field_definition=definition, field_definition_version=version, source_selector="request.theme", channel="positive_prompt", position=1).pk

    def test_identity_version_and_frozen_rule_survive_without_duplicate_meaning(self):
        identity_model = self.apps.get_model("studio", "FieldDefinition")
        identity = identity_model.objects.get(pk=self.identity_id)
        self.assertEqual((identity.key, identity.is_active, identity.display_order), ("preserved_identity", True, 29))
        for name in ("label", "category", "description", "impact", "value_type", "value_options"):
            with self.subTest(name=name), self.assertRaises(FieldDoesNotExist):
                identity_model._meta.get_field(name)
        version = self.apps.get_model("studio", "FieldDefinitionVersion").objects.get(pk=self.version_id)
        self.assertEqual((version.field_definition_id, version.version, version.status, version.label, version.unit, version.value_options),
            (self.identity_id, 3, "published", "冻结含义", "份", ["甲", "乙"]))
        rule = self.apps.get_model("studio", "PromptProfileFieldRule").objects.select_related("profile_version").get(pk=self.rule_id)
        self.assertEqual((rule.field_definition_id, rule.field_definition_version_id), (self.identity_id, self.version_id))
        self.assertEqual((rule.profile_version.contract_sha256, rule.profile_version.contract_size_bytes), ("b" * 64, 456))
