from studio.tests.field_definition_fixtures import create_field_definition
from copy import deepcopy
from unittest.mock import patch

from django.test import TestCase, SimpleTestCase
from studio.models import FieldDefinition, FieldDefinitionVersion, PromptProfileFieldRule, PromptProfileVersion
from studio.platform_serializers import PromptProfileFieldRuleSerializer
from studio.services.field_definitions import create_field_definition_version, publish_field_definition_version, retire_field_definition_version, validate_field_definition_snapshot
from studio.services.prompt_profiles import (PromptProfileError, assemble_prompt_profile, prompt_profile_contract_payload,
    refresh_prompt_profile_contract, validate_profile_rule, clone_prompt_profile_successor)
from studio.services.prompt_normalization_contract import validate_frozen_prompt_field_rule, PromptNormalizationContractError
from studio.tests.field_definition_fixtures import create_profile_field_rule
from studio.tests.test_execution_field_contracts import field_rule


class PromptFieldVersionConsumerTests(TestCase):
    def setUp(self):
        self.field = create_field_definition(key="versioned_choice", label="旧身份标签", category="custom", value_type="enum", value_options=["a", "b"])
        self.profile = PromptProfileVersion.objects.create(profile_key="exact-version", version=1, name="版本测试",
            model_family="fixture", operation="video.generate", mode="T2VA", source_projection_key="direct_v1")
        self.rule = create_profile_field_rule(profile_version=self.profile, field_definition=self.field,
            source_selector="request.theme", generation_source_selector="request.theme", channel="positive_prompt", position=1,
            label_style="label_colon", language_policy="any")
        refresh_prompt_profile_contract(self.profile)

    def test_new_version_and_identity_edits_do_not_reinterpret_selected_version(self):
        frozen = deepcopy(prompt_profile_contract_payload(self.profile))
        old_hash = self.profile.contract_sha256
        version = create_field_definition_version(field_definition_id=self.field.pk, content={
            "label": "新整数含义", "category": "custom", "value_type": "integer", "unit": "秒"})
        publish_field_definition_version(version_id=version.pk)
        self.field.display_order = 77
        self.field.save()
        refresh_prompt_profile_contract(self.profile)
        self.assertEqual(self.profile.contract_sha256, old_hash)
        self.assertEqual(prompt_profile_contract_payload(self.profile), frozen)
        self.assertEqual(assemble_prompt_profile(self.profile, sources={"request.theme": "a"}, allow_draft=True)["structured_fields"], {self.field.key: "a"})
        with self.assertRaises(PromptProfileError):
            assemble_prompt_profile(self.profile, sources={"request.theme": 2}, allow_draft=True)
        serialized = PromptProfileFieldRuleSerializer(self.rule).data
        self.assertEqual(serialized["field_value_type"], "enum")
        self.assertEqual(str(serialized["field_definition_version"]), str(self.rule.field_definition_version_id))

    def test_missing_cross_identity_and_draft_version_are_not_guessed(self):
        original = self.rule.field_definition_version
        other = create_field_definition(key="other_identity", label="Other", category="custom")
        draft = create_field_definition_version(field_definition_id=self.field.pk, content={"label": "草稿"})
        wrong = FieldDefinitionVersion(field_definition=other, version=1, status="published")
        for version in (None, draft, wrong):
            with self.subTest(version=version):
                self.rule.field_definition_version = version
                with self.assertRaises(PromptProfileError):
                    validate_profile_rule(self.rule)
        self.rule.field_definition_version = original
        validate_profile_rule(self.rule)

    def test_retired_version_preserves_existing_contract_but_is_not_new_authoring_choice(self):
        frozen = prompt_profile_contract_payload(self.profile)
        retire_field_definition_version(version_id=self.rule.field_definition_version_id)
        self.rule.refresh_from_db()
        self.assertEqual(prompt_profile_contract_payload(self.profile), frozen)
        with self.assertRaises(PromptProfileError):
            validate_profile_rule(self.rule)
        validate_profile_rule(self.rule, require_published_source=False)

    def test_successor_keeps_exact_version_even_when_new_definition_exists(self):
        newer = create_field_definition_version(field_definition_id=self.field.pk,
            source_version_id=self.rule.field_definition_version_id, content={"label": "后续定义"})
        publish_field_definition_version(version_id=newer.pk)
        self.profile.status = "published"
        self.profile.save(update_fields=("status",))
        clone = clone_prompt_profile_successor(self.profile, expected_source_contract_sha256=self.profile.contract_sha256, name="后继")
        self.assertEqual(clone.field_rules.get().field_definition_version_id, self.rule.field_definition_version_id)

    def test_explicit_new_version_changes_contract_hash(self):
        old_hash = self.profile.contract_sha256
        version = create_field_definition_version(field_definition_id=self.field.pk,
            source_version_id=self.rule.field_definition_version_id, content={"label": "新名称", "unit": "种"})
        version = publish_field_definition_version(version_id=version.pk)
        self.rule.field_definition_version = version
        self.rule.save(update_fields=("field_definition_version",))
        refresh_prompt_profile_contract(self.profile)
        self.assertNotEqual(old_hash, self.profile.contract_sha256)
        self.assertEqual(prompt_profile_contract_payload(self.profile)["rules"][0]["field_definition"]["unit"], "种")


class FrozenFieldDefinitionConsumerTests(SimpleTestCase):
    def setUp(self):
        # The enclosing prompt validator also reads configured size limits.
        # Supply those explicitly; only field-definition resolution is query-free.
        self.enterContext(patch("studio.services.prompt_normalization_contract.boundary_int", return_value=262144))

    def test_snapshot_is_validated_without_database_reads(self):
        rule = field_rule()
        self.assertEqual(validate_field_definition_snapshot(rule["field_definition"]), rule["field_definition"])
        validate_frozen_prompt_field_rule(rule, label="fixture", schema_version=8)
        for key, value in (("field_value_type", "text"), ("field_key", "another"), ("field_value_options", ["a"])):
            with self.subTest(key=key), self.assertRaises(PromptNormalizationContractError):
                validate_frozen_prompt_field_rule({**rule, key: value}, label="fixture", schema_version=8)

    def test_snapshot_missing_or_malformed_is_not_rebuilt(self):
        rule = field_rule()
        with self.assertRaises(PromptNormalizationContractError):
            validate_frozen_prompt_field_rule(rule, label="fixture", schema_version=7)
        for content in (None, {}, {**rule["field_definition"], "field_definition_version_id": "latest"}):
            with self.subTest(content=content), self.assertRaises(PromptNormalizationContractError):
                validate_frozen_prompt_field_rule({**rule, "field_definition": content}, label="fixture", schema_version=8)
