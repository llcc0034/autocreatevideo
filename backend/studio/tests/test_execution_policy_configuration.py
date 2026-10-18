from unittest.mock import patch

from django.contrib.auth import get_user_model
from django.core.exceptions import ValidationError
from django.test import TestCase

from studio.models import ConfigDefinition, ConfigRevision, ConfigValue
from studio.services.configuration import (
    ConfigurationResolutionError, create_config_revision,
    publish_config_revision, validate_config_value,
)
from studio.services.execution_policy_configuration import (
    CAPACITY_POLICY, CLAIM_TIMEOUT, EXECUTION_POLICY_KEYS, RECHECK_INTERVAL, SMOKE_POLICY, read_execution_policies,
)


class ExecutionPolicyConfigurationTests(TestCase):
    def test_registered_initial_values_and_exact_system_revision_are_read(self):
        policies = read_execution_policies()
        self.assertEqual((policies.smoke_policy, policies.capacity_policy,
            policies.recheck_interval_seconds, policies.claim_timeout_seconds), ("require_passed", "warn", 10, 60))
        self.assertEqual(set(policies.configuration.value["values"]), set(EXECUTION_POLICY_KEYS))
        for source in policies.configuration.value["sources"].values():
            self.assertEqual(source["scope"], "system")
            self.assertIn(source["revision_id"], policies.configuration.value["revision_ids"])

    def test_formal_revision_changes_runtime_policy_without_changing_instance(self):
        actor = get_user_model().objects.create_superuser(username="execution-policy-admin", password="test-only")
        before = read_execution_policies()
        revision = create_config_revision(scope_type="system", values={SMOKE_POLICY: "require_fresh", CAPACITY_POLICY: "require_evidence",
            RECHECK_INTERVAL: 3, CLAIM_TIMEOUT: 30}, created_by=actor, change_summary="isolated execution policy test")
        revision = publish_config_revision(revision, published_by=actor)
        self.assertEqual(revision.activation_state, "active")
        after = read_execution_policies()
        self.assertEqual((after.smoke_policy, after.capacity_policy, after.recheck_interval_seconds, after.claim_timeout_seconds),
            ("require_fresh", "require_evidence", 3, 30))
        self.assertNotEqual(after.configuration.sha256, before.configuration.sha256)
        self.assertEqual(after.configuration.value["revision_ids"], [str(revision.pk)])

    def test_configured_schema_rejects_bad_choices_coercion_and_out_of_range(self):
        for key, invalid in ((SMOKE_POLICY, "off"), (CAPACITY_POLICY, "skip"),
                (RECHECK_INTERVAL, 0), (RECHECK_INTERVAL, 301), (CLAIM_TIMEOUT, 9), (CLAIM_TIMEOUT, 601),
                (RECHECK_INTERVAL, True), (CLAIM_TIMEOUT, "60")):
            with self.subTest(key=key, invalid=invalid), self.assertRaises(ValidationError):
                validate_config_value(ConfigDefinition.objects.get(key=key), invalid)

    def test_missing_active_revision_does_not_fall_back_to_initial_values(self):
        ConfigRevision.objects.filter(scope_type="system", activation_state="active").update(activation_state="inactive")
        with self.assertRaises(ConfigurationResolutionError):
            read_execution_policies()

    def test_tampered_value_is_not_treated_as_warn(self):
        ConfigValue.objects.filter(revision__activation_state="active", definition__key=CAPACITY_POLICY).update(value="off")
        with self.assertRaises((ValidationError, ConfigurationResolutionError)):
            read_execution_policies()

    def test_configuration_failure_is_not_swallowed(self):
        with patch("studio.services.execution_policy_configuration.resolve_configuration", side_effect=ConfigurationResolutionError("test failure", code="test_configuration_failure")):
            with self.assertRaises(ConfigurationResolutionError):
                read_execution_policies()

    def test_definitions_offer_only_system_scope_and_server_enum_options(self):
        rows = list(ConfigDefinition.objects.filter(key__in=EXECUTION_POLICY_KEYS))
        self.assertEqual(len(rows), 4)
        for row in rows:
            self.assertEqual((row.domain, row.allowed_scopes, row.allowed_default_sources), ("execution", "system", ""))
            self.assertTrue(row.is_required)
            self.assertFalse(row.restart_required)
        self.assertEqual(ConfigDefinition.objects.get(key=SMOKE_POLICY).value_schema["enum"], ["require_passed", "require_fresh"])
