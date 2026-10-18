from studio.tests.field_definition_fixtures import create_field_definition, make_field_definition
from unittest.mock import patch

from django.contrib.auth import get_user_model
from django.test import SimpleTestCase, TestCase
from rest_framework.test import APIClient
from rest_framework.exceptions import ValidationError

from studio.tests.field_definition_fixtures import create_profile_field_rule, make_profile_field_rule
from studio.models import FieldDefinition, PromptProfileFieldRule, PromptProfileVersion
from studio.platform_serializers import FieldDefinitionSerializer, PromptProfileFieldRuleSerializer
from studio.services.prompt_profiles import (
    PromptProfileError, assemble_prompt_profile, prompt_profile_contract_payload, validate_profile_rule, refresh_prompt_profile_contract,
)


class PromptRuleValueOwnershipTests(SimpleTestCase):
    def setUp(self):
        self.enterContext(patch("studio.services.prompt_profiles.boundary_int", return_value=262144))
        self.field = make_field_definition(key="arbitrary_input", label="输入", category="custom", value_type="text")
        self.profile = PromptProfileVersion(profile_key="ownership", version=1, name="测试", model_family="test",
            operation="video.generate", mode="T2VA", source_projection_key="direct_v1")

    def rule(self, **changes):
        return make_profile_field_rule(profile_version=self.profile, field_definition=self.field,
            source_selector="request.theme", generation_source_selector="request.theme", channel="positive_prompt",
            position=1, language_policy="any", **changes)

    def render(self, rule, sources):
        return assemble_prompt_profile(self.profile, sources=sources, rules=[rule], allow_draft=True)

    def test_dictionary_has_no_global_required_or_value_and_rejects_obsolete_writes(self):
        names = {field.name for field in FieldDefinition._meta.fields}
        self.assertFalse({"default_value", "is_required"} & names)
        for name, value in (("default_value", "hidden"), ("is_required", True)):
            with self.subTest(name=name), self.assertRaises(ValidationError):
                FieldDefinitionSerializer().to_internal_value({name: value})
        self.assertTrue({"fixed_value", "has_default", "default_value", "required"}
            <= set(PromptProfileFieldRuleSerializer().fields))

    def test_default_only_applies_to_absent_key_not_explicit_null_or_empty(self):
        rule = self.rule(has_default=True, default_value="fallback")
        absent = self.render(rule, {})
        self.assertEqual(absent["structured_fields"][self.field.key], "fallback")
        self.assertTrue(absent["trace"][0]["defaulted"])
        for value in (None, "", []):
            with self.subTest(value=value):
                self.assertEqual(self.render(rule, {"request.theme": value})["structured_fields"], {})
        rule.required = True
        with self.assertRaises(PromptProfileError):
            self.render(rule, {"request.theme": ""})

    def test_false_zero_and_empty_list_are_not_replaced_with_defaults(self):
        for kind, default, value in (("boolean", True, False), ("decimal", 3, 0), ("list", ["default"], [])):
            with self.subTest(kind=kind):
                self.field._fixture_version.value_type = kind
                rendered = self.render(self.rule(has_default=True, default_value=default), {"request.theme": value})
                self.assertNotEqual(rendered["structured_fields"].get(self.field.key), default)
                if value != []:
                    self.assertEqual(rendered["structured_fields"][self.field.key], value)
                    self.assertFalse(rendered["trace"][0]["defaulted"])

    def test_shared_identity_rules_keep_independent_defaults_and_required_flags(self):
        first, second = self.rule(has_default=True, default_value="first"), self.rule(required=True)
        self.assertEqual(self.render(first, {})["structured_fields"][self.field.key], "first")
        with self.assertRaises(PromptProfileError):
            self.render(second, {})
        second.required, second.has_default, second.default_value = False, True, "second"
        self.assertEqual(self.render(second, {})["structured_fields"][self.field.key], "second")
        self.assertEqual(first.default_value, "first")

    def test_fixed_value_is_explicit_not_dictionary_fallback_and_is_frozen(self):
        rule = self.rule(fixed_value="locked")
        rule.source_selector = rule.generation_source_selector = "fixed.value"
        validate_profile_rule(rule)
        result = self.render(rule, {"request.theme": "caller"})
        self.assertEqual(result["structured_fields"][self.field.key], "locked")
        self.assertFalse(result["trace"][0]["defaulted"])
        payload = prompt_profile_contract_payload(self.profile, rules=[rule])
        frozen = payload["rules"][0]
        self.assertEqual(payload["schema_version"], 8)
        self.assertEqual(frozen["fixed_value"], "locked")
        self.assertNotIn("field_default_value", frozen)
        self.assertNotIn("field_required", frozen)
        rule.has_default = True
        with self.assertRaises(PromptProfileError):
            validate_profile_rule(rule)
    def test_old_contract_does_not_silently_receive_new_value_semantics(self):
        self.profile.contract_schema_version = 6
        with self.assertRaises(PromptProfileError) as failure:
            self.render(self.rule(), {})
        self.assertEqual(failure.exception.code, "prompt_profile_contract_schema_unsupported")

    def test_inactive_default_and_wrong_typed_fixed_value_are_rejected(self):
        with self.assertRaises(PromptProfileError):
            validate_profile_rule(self.rule(default_value="unused"))
        self.field._fixture_version.value_type = "boolean"
        rule = self.rule(fixed_value="not a bool")
        rule.source_selector = rule.generation_source_selector = "fixed.value"
        with self.assertRaises(PromptProfileError):
            validate_profile_rule(rule)


class PromptRuleValueApiTests(TestCase):
    def test_rule_patch_persists_false_fixed_value_and_freezes_local_defaults(self):
        actor = get_user_model().objects.create_superuser(username="rule-owner", password="Test-only-password")
        client = APIClient()
        client.force_authenticate(actor)
        field = create_field_definition(key="local_rule_flag", label="标志", category="custom", value_type="boolean")
        profile = PromptProfileVersion.objects.create(profile_key="local-rule-api", version=1, name="测试",
            model_family="test", operation="video.generate", mode="T2VA", source_projection_key="direct_v1")
        rule = create_profile_field_rule(profile_version=profile, field_definition=field,
            source_selector="fixed.value", generation_source_selector="fixed.value", fixed_value=True,
            channel="positive_prompt", position=1, language_policy="any")
        refresh_prompt_profile_contract(profile)
        original_hash = profile.contract_sha256
        response = client.patch(f"/api/v1/prompt-profile-field-rules/{rule.pk}/", {"fixed_value": False}, format="json")
        self.assertEqual(response.status_code, 200, response.data)
        self.assertIs(response.data["fixed_value"], False)
        rule.refresh_from_db()
        profile.refresh_from_db()
        self.assertNotEqual(profile.contract_sha256, original_hash)
        self.assertIs(assemble_prompt_profile(profile, sources={}, allow_draft=True)["structured_fields"][field.key], False)
        response = client.patch(f"/api/v1/prompt-profile-field-rules/{rule.pk}/", {
            "source_selector": "request.theme", "generation_source_selector": "request.theme", "fixed_value": None,
            "has_default": True, "default_value": False, "required": True,
        }, format="json")
        self.assertEqual(response.status_code, 200, response.data)
        profile.refresh_from_db()
        self.assertIs(prompt_profile_contract_payload(profile)["rules"][0]["default_value"], False)
        response = client.patch(f"/api/v1/field-definitions/{field.pk}/", {"is_required": True}, format="json")
        self.assertEqual(response.status_code, 400)
