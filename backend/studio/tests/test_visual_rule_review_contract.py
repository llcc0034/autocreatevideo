"""Exact output definition travels with a verified candidate, not current catalog labels."""
from copy import deepcopy
from types import SimpleNamespace
from unittest.mock import patch
from uuid import uuid4

from django.core.exceptions import ValidationError
from django.test import SimpleTestCase

from studio.services.business_outputs import parser_contract, visual_rule_review_contract
from studio.services.json_contracts import json_contract
from studio.services.visual_rules_inputs import input_digest, verify_visual_rules_candidate_source
from studio.services.visual_rules_output_contract import reconfigure_visual_rule_sources, visual_rule_field_sources
from studio.tests.output_structure_fixtures import frozen_response_structure


class VisualRuleReviewContractTests(SimpleTestCase):
    def setUp(self):
        # These isolated parser tests use explicit safety ceilings; integration
        # tests below use the actual configuration registry in their test DB.
        for target in ("studio.services.production_visual_rules._provider_response_max_bytes",
                "studio.services.visual_rules_inputs.execution_configuration_max_bytes"):
            boundary = patch(target, return_value=65536)
            boundary.start()
            self.addCleanup(boundary.stop)
        parser = parser_contract("visual_rules.v2")
        parser = reconfigure_visual_rule_sources(parser, visual_rule_field_sources(parser), {
            "continuity_summary": "rules.0.summary", "color_script": "rules.1.palette", "negative_prompt": "rules.2.avoid"})
        parser["fields"][0]["label"] = "原版本连续性字段名称"
        self.reference = {"schema_version": 2, "id": str(uuid4()), "output_key": str(uuid4()), "version": 4,
            "data_structure": frozen_response_structure(parser["response_example"]),
            "name": "Original output", "description": "", "parser": parser, "quality_advisories": {}}
        self.reference["sha256"] = json_contract(self.reference).sha256
        self.rules = {"continuity_summary": "Keep continuity.", "color_script": "Muted color.", "negative_prompt": ""}

    def test_exact_fields_positions_labels_order_and_nullability_without_current_catalog(self):
        with patch("studio.services.business_outputs.parser_contract", side_effect=AssertionError("not today's registry")):
            contract = visual_rule_review_contract(self.reference)
        self.assertEqual(contract["output"]["id"], self.reference["id"])
        self.assertEqual(contract["output"]["sha256"], self.reference["sha256"])
        self.assertEqual(contract["fields"][0]["label"], "原版本连续性字段名称")
        self.assertEqual(contract["fields"][0]["response_path"], "rules.0.summary")
        self.assertFalse(contract["fields"][0]["allow_blank"])
        self.assertTrue(contract["fields"][2]["allow_blank"])
        self.assertEqual({row["source_selector"] for row in contract["fields"]}, {
            "reviewed_visual_rules." + key for key in self.rules})
        with self.assertRaises(ValidationError):
            visual_rule_review_contract({**self.reference, "name": "Unhashed replacement"})

    def verify(self, candidate):
        baseline = {"exact": "original input"}
        assembly = {"positive_prompt": "Explicit request"}
        plan = {"input_baseline": baseline, "prompt_profile_assembly": assembly,
            "business_input_sha256": input_digest(assembly), "business_output": self.reference, "resource_estimate": {}}
        value = {"input_baseline": baseline, "business_input_sha256": plan["business_input_sha256"],
            "business_output": deepcopy(self.reference), "candidate": self.rules, "candidate_sha256": input_digest(self.rules)}
        value.update(candidate)
        request = (SimpleNamespace(resolved_inputs={"inputs": {}}), SimpleNamespace(value={}),
            SimpleNamespace(value={"operation_plan": {"plan": plan}}))
        with patch("studio.services.execution_preparations.read_frozen_execution_request", return_value=request), \
                patch("studio.services.execution_native_text.native_text_source_reference", return_value=baseline):
            return verify_visual_rules_candidate_source(object(), value)

    def test_original_request_binds_output_version_and_advice_not_only_self_consistent_hash(self):
        self.assertEqual(self.verify({}), visual_rule_review_contract(self.reference))
        replacement = {key: deepcopy(value) for key, value in self.reference.items() if key != "sha256"}
        replacement["id"] = str(uuid4())
        replacement["sha256"] = json_contract(replacement).sha256
        for change in ({"business_output": None}, {"business_output": replacement},
                {"quality_advisories": {"schema_version": 1, "continuity_summary_max_chars": 1}}):
            with self.subTest(change=change), self.assertRaisesMessage(ValidationError, "原准备不一致"):
                self.verify(change)

    def test_missing_extra_wrong_type_or_rehashed_noncanonical_fields_rejected(self):
        for rules in ({"color_script": "missing"}, {**self.rules, "undeclared": "not declared"},
                {**self.rules, "color_script": []}, {**self.rules, "continuity_summary": " Keep continuity. "}):
            with self.subTest(rules=rules), self.assertRaises(ValidationError):
                self.verify({"candidate": rules, "candidate_sha256": input_digest(rules)})
        with self.assertRaises(ValidationError):
            self.verify({"candidate_sha256": "0" * 64})
