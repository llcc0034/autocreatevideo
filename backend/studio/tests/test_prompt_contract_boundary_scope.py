from types import SimpleNamespace
from unittest.mock import patch

from django.db import connection
from django.test import TestCase
from django.test.utils import CaptureQueriesContext

from studio.services.configuration import ConfigurationResolutionError
from studio.services.prompt_normalization_contract import (
    PromptNormalizationContractError,
    _validated_fact_tree,
)


class PromptContractBoundaryScopeTests(TestCase):
    def test_recursive_text_checks_do_not_repeat_configuration_queries(self):
        with CaptureQueriesContext(connection) as small:
            self.assertEqual(_validated_fact_tree(["fact"], label="facts"), ["fact"])
        facts = [{"description": "fact", "parts": ["first", "second"]} for _ in range(100)]
        with CaptureQueriesContext(connection) as large:
            self.assertEqual(_validated_fact_tree(facts, label="facts"), facts)
        self.assertGreater(len(small), 0)
        self.assertEqual(len(large), len(small))

    def test_next_validation_observes_changed_boundary_and_error_does_not_leak_cache(self):
        limits = {
            "security.prompt_normalization.fact_tree_max_depth": 5,
            "security.prompt_normalization.fact_tree_max_fields": 40,
            "security.prompt_normalization.fact_tree_key_max_characters": 80,
            "security.prompt_normalization.text_max_characters": 10,
        }

        def resolve(*, required_keys):
            return SimpleNamespace(values={key: limits[key] for key in required_keys})

        with patch("studio.services.configuration.resolve_security_boundaries", side_effect=resolve):
            self.assertEqual(_validated_fact_tree(["long fact"], label="facts"), ["long fact"])
            limits["security.prompt_normalization.text_max_characters"] = 4
            with self.assertRaises(PromptNormalizationContractError):
                _validated_fact_tree(["long fact"], label="facts")
            limits["security.prompt_normalization.text_max_characters"] = 10
            self.assertEqual(_validated_fact_tree(["long fact"], label="facts"), ["long fact"])

    def test_configuration_failure_is_not_replaced_by_prior_result(self):
        _validated_fact_tree("fact", label="facts")
        with patch(
            "studio.services.configuration.resolve_security_boundaries",
            side_effect=ConfigurationResolutionError("unavailable", code="test_unavailable"),
        ):
            with self.assertRaises(ConfigurationResolutionError):
                _validated_fact_tree("fact", label="facts")
