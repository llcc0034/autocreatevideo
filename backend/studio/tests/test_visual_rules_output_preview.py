from copy import deepcopy
from itertools import product
import json
from unittest.mock import patch

from django.contrib.auth import get_user_model
from django.core.exceptions import ValidationError
from django.db import connection
from django.test import TestCase
from django.test.utils import CaptureQueriesContext
from rest_framework.test import APIClient

from studio.services.json_contracts import parse_json_object_text
from studio.services.visual_rules_output_contract import (
    VISUAL_RULE_SOURCE_FIELDS, VISUAL_RULES_REVIEW_SCHEMA_VERSION, visual_rule_response_example,
)
from studio.services.visual_rules_output_preview import preview_visual_rules_output


class VisualRulesOutputPreviewTests(TestCase):
    @classmethod
    def setUpTestData(cls):
        cls.staff = get_user_model().objects.create_user("output-preview-staff", is_staff=True)
        cls.member = get_user_model().objects.create_user("output-preview-member")

    def setUp(self):
        self.client = APIClient()
        self.client.force_authenticate(self.staff)
        self.url = "/api/v1/business-output-versions/preview-visual-rules-output/"
        self.sample = visual_rule_response_example()
        self.enterContext(patch("studio.services.provider_registry._request_json", side_effect=AssertionError("No network")))
        self.enterContext(patch("studio.services.provider_registry.resolve_credential", side_effect=AssertionError("No credential read")))

    def test_options_example_prompt_and_consumed_fields_use_one_contract(self):
        from studio.services.production_visual_rules import visual_rules_output_editor_options
        from studio.services.execution_source_readers import reviewed_visual_source_reader
        options = visual_rules_output_editor_options()
        self.assertEqual(options["response_example"], self.sample)
        result = preview_visual_rules_output(response_text=json.dumps(self.sample))
        self.assertTrue(result["valid"])
        self.assertEqual({row["field"] for row in options["outputs"]}, set(result["candidate"]))
        self.assertEqual(set(self.sample), {"schema_version", *(row["response_field"] for row in options["outputs"])})
        for key in VISUAL_RULE_SOURCE_FIELDS:
            self.assertEqual(reviewed_visual_source_reader(key)["output_schema_version"], VISUAL_RULES_REVIEW_SCHEMA_VERSION)
        self.sample["color_design"]["base_colors"].clear()
        self.assertNotEqual(self.sample, visual_rule_response_example())

    def test_staff_preview_uses_current_advisories_without_writes_or_generation(self):
        with CaptureQueriesContext(connection) as queries:
            response = self.client.post(self.url, {"response_text": json.dumps(self.sample),
                "quality_advisories": {"schema_version": 1, "continuity_summary_max_chars": 1,
                    "recommended_negative_clauses": ["identity drift"]}}, format="json")
        self.assertEqual(response.status_code, 200, response.data)
        report = response.data
        self.assertTrue(report["preview_only"])
        self.assertTrue(report["valid"])
        self.assertEqual(report["candidate"]["continuity_summary"], self.sample["continuity_summary"])
        self.assertIn("identity drift", report["candidate"]["negative_prompt"])
        self.assertTrue(report["warnings"])
        self.assertNotIn("冻结模型", "".join(report["warnings"]))
        self.assertNotIn("进入审核", "".join(report["warnings"]))
        self.assertEqual(report["errors"], [])
        self.assertFalse(any(q["sql"].lstrip().upper().startswith(("INSERT", "UPDATE", "DELETE")) for q in queries))
        self.assertNotIn("task_id", report)
        self.assertNotIn("approval_id", report)

    def test_invalid_samples_report_specific_errors_no_candidate_and_do_not_repair(self):
        cases = [
            ("{}", "visual_rules_response_invalid"), ("", "native_text_response_invalid"),
            ("[]", "native_text_response_invalid"),
            ('{"schema_version":2,"schema_version":2}', "native_text_response_invalid"),
            ('{"x":NaN}', "native_text_response_invalid"),
            ('{"x":Infinity}', "native_text_response_invalid"),
            ("[", "native_text_response_invalid"),
            ("\u0060\u0060\u0060json\n" + json.dumps(self.sample) + "\n\u0060\u0060\u0060", "native_text_response_invalid"),
        ]
        for payload, code in cases:
            with self.subTest(payload=payload):
                response = self.client.post(self.url, {"response_text": payload}, format="json")
                self.assertEqual(response.status_code, 200, response.data)
                self.assertFalse(response.data["valid"])
                self.assertIsNone(response.data["candidate"])
                self.assertEqual(response.data["errors"][0]["code"], code)
        for mutate, code in [
            (lambda v: v.update(schema_version=True), "visual_rules_source_mismatch"),
            (lambda v: v["color_design"].update(lighting="invented"), "color_design_enum_invalid"),
            (lambda v: v.update(negative_constraints=["one,two"]), "negative_constraint_delimiter_invalid"),
            (lambda v: v.update(negative_constraints=["drift", "DRIFT"]), "negative_constraints_duplicate"),
            (lambda v: v.update(continuity_summary="<b>invalid</b>"), "continuity_summary_text_invalid"),
        ]:
            value = deepcopy(self.sample)
            mutate(value)
            report = preview_visual_rules_output(response_text=json.dumps(value))
            self.assertFalse(report["valid"])
            self.assertEqual(report["errors"][0]["code"], code)

    def test_request_permissions_unknown_fields_and_bad_advisories(self):
        self.client.force_authenticate(self.member)
        self.assertEqual(self.client.post(self.url, {"response_text": "{}"}, format="json").status_code, 403)
        self.client.force_authenticate(None)
        self.assertIn(self.client.post(self.url, {"response_text": "{}"}, format="json").status_code, (401, 403))
        self.client.force_authenticate(self.staff)
        for body in [{}, {"response_text": 2}, {"response_text": "{}", "task_id": "not-allowed"},
            {"response_text": "{}", "quality_advisories": {"schema_version": 1, "base_colors_min_items": 22}},
            {"response_text": "{}", "quality_advisories": None}]:
            self.assertEqual(self.client.post(self.url, body, format="json").status_code, 400, body)

    def test_all_eight_declared_shapes_use_shared_parser_without_inference_or_writes(self):
        from studio.services.production_visual_rules import parse_visual_rules_output
        from studio.services.business_outputs import parser_contract
        from studio.services.visual_rules_output_contract import reconfigure_visual_rule_sources, visual_rule_field_source_options
        options = visual_rule_field_source_options()
        for choices in product(*(row['choices'] for row in options)):
            sources = {row['field']: choice['response_field'] for row, choice in zip(options, choices)}
            sample = reconfigure_visual_rule_sources(parser_contract('visual_rules.v2'), sources)['response_example']
            original = deepcopy(sample)
            quality = {'schema_version': 1, 'negative_constraints_max_items': 0,
                'negative_constraint_max_chars': 1, 'continuity_summary_max_chars': 1,
                'recommended_negative_clauses': ['identity drift']}
            with self.subTest(sources=sources), CaptureQueriesContext(connection) as queries:
                preview = preview_visual_rules_output(response_text=json.dumps(sample), field_sources=sources, quality_advisories=quality)
                parsed = parse_visual_rules_output(payload=sample, field_sources=sources, quality_advisories=quality)
            self.assertTrue(preview['valid'], preview)
            self.assertEqual(preview['candidate'], parsed['candidate'])
            self.assertTrue(preview['warnings'])
            self.assertEqual(sample, original)
            self.assertFalse(any(q['sql'].lstrip().upper().startswith(('INSERT', 'UPDATE', 'DELETE')) for q in queries))
            if sources['continuity_summary'] == 'continuity_points':
                self.assertEqual(parsed['candidate']['continuity_summary'], ' '.join(sample['continuity_points']))
            if sources['negative_prompt'] == 'negative_prompt':
                self.assertEqual(parsed['candidate']['negative_prompt'], 'identity drift, inconsistent lighting')
                count = next(row for row in parsed['advisory_audit'] if row['code'] == 'negative_constraints_count_outside_recommendation')
                self.assertEqual((count['field'], count['observed']), ('negative_prompt', 2))

    def test_alternate_shapes_reject_empty_items_duplicates_wrong_types_and_extra_keys(self):
        sources = {'continuity_summary': 'continuity_points', 'color_script': 'color_script', 'negative_prompt': 'negative_prompt'}
        sample = {'schema_version': 2, 'continuity_points': ['First rule.', 'Second rule.'],
            'color_script': 'Restrained natural colors.', 'negative_prompt': ''}
        self.assertEqual(preview_visual_rules_output(response_text=json.dumps(sample), field_sources=sources)['candidate']['negative_prompt'], '')
        cases = [('continuity_points', value) for value in ([], '', {}, [None], [''], ['<b>unsafe</b>'], ['control\x00'], ['bidi\u202e'])]
        cases += [('negative_prompt', value) for value in ([], {}, None, ',drift', 'drift,', 'drift,,noise', 'drift,DRIFT', 'drift;noise', 'drift\n', '<b>unsafe</b>')]
        cases += [('negative_constraints', []), ('continuity_summary', 'Do not accept both representations.')]
        for key, value in cases:
            body = {**deepcopy(sample), key: value}
            with self.subTest(key=key, value=value):
                report = preview_visual_rules_output(response_text=json.dumps(body), field_sources=sources)
                self.assertFalse(report['valid'], report)
                self.assertIsNone(report['candidate'])
        # Only the selected format is accepted; no auto-detection or stringification.
        self.assertFalse(preview_visual_rules_output(response_text=json.dumps(sample))['valid'])

    def test_visual_text_language_and_multiline_prose_are_strategy_owned(self):
        sources = {'continuity_summary': 'continuity_points', 'color_script': 'color_script',
            'negative_prompt': 'negative_prompt', 'camera_guidance': 'camera_guidance'}
        sample = {'schema_version': 2, 'continuity_points': ['林舟的服装一致。', 'Keep café lighting.\n第二行。'],
            'color_script': '柔和暖色 — lumière douce.', 'negative_prompt': '身份漂移, flicker',
            'camera_guidance': '缓慢推进。\nThen hold.'}
        report = preview_visual_rules_output(response_text=json.dumps(sample), field_sources=sources)
        self.assertTrue(report['valid'], report)
        self.assertEqual(report['candidate']['continuity_summary'], ' '.join(sample['continuity_points']))
        for key in ('color_script', 'negative_prompt', 'camera_guidance'):
            self.assertEqual(report['candidate'][key], sample[key])

    def test_raw_bytes_unicode_nesting_and_nonfinite_boundary_is_shared(self):
        for text in [" " * 40 + "{}", "\ud800", "[" * 2000, '{"x":NaN}', "[]"]:
            with self.assertRaises(ValidationError):
                parse_json_object_text(text, max_bytes=32)
        with patch("studio.services.visual_rules_output_preview._provider_response_max_bytes", return_value=16):
            report = preview_visual_rules_output(response_text=json.dumps(self.sample))
        self.assertFalse(report["valid"])
        self.assertEqual(report["errors"][0]["code"], "native_text_response_too_large")
