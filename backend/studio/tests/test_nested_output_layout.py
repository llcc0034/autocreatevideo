from copy import deepcopy
from itertools import product

from django.core.exceptions import ValidationError
from django.test import TestCase

from studio.services.business_outputs import parser_contract, validate_parser_contract
from studio.services.production_visual_rules import parse_visual_rules_output
from studio.services.visual_rules_output_contract import (
    visual_rule_field_source_options, visual_rule_response_keys,
    reconfigure_visual_rule_sources, constrain_visual_rule_example,
)


class NestedOutputLayoutTests(TestCase):
    keys = {'continuity_summary': 'visual.rules.summary', 'color_script': 'visual.palette',
        'negative_prompt': 'constraints.avoid'}

    def test_all_formats_nested_layout_and_moves_preserve_values(self):
        self.assert_formats_roundtrip(self.keys)

    def test_all_formats_array_layout_and_moves_preserve_values(self):
        self.assert_formats_roundtrip({'continuity_summary': 'rules.0.summary', 'color_script': 'rules.1.palette',
            'negative_prompt': 'rules.2.avoid'})
        self.assert_formats_roundtrip({'continuity_summary': 'rules.0.0', 'color_script': 'rules.0.1',
            'negative_prompt': 'rules.1'})

    def assert_formats_roundtrip(self, keys):
        original = parser_contract('visual_rules.v2')
        options = visual_rule_field_source_options()
        for choices in product(*(([None, *row['choices']] if row.get('optional') else row['choices']) for row in options)):
            sources = {row['field']: choice['response_field'] for row, choice in zip(options, choices) if choice is not None}
            selected_keys = {**keys, **({'camera_guidance': 'camera'} if 'camera_guidance' in sources else {})}
            with self.subTest(sources=sources):
                flat = reconfigure_visual_rule_sources(original, sources)
                nested = reconfigure_visual_rule_sources(flat, sources, selected_keys)
                self.assertEqual(validate_parser_contract(nested), nested)
                parsed = parse_visual_rules_output(payload=nested['response_example'], field_sources=sources, response_keys=selected_keys)
                expected = parse_visual_rules_output(payload=flat['response_example'], field_sources=sources)
                self.assertEqual(parsed, expected)
                self.assertEqual(reconfigure_visual_rule_sources(nested, sources, {}), flat)
                swapped = reconfigure_visual_rule_sources(nested, sources, {
                    **selected_keys, 'continuity_summary': 'constraints.avoid', 'color_script': 'visual.rules.summary',
                    'negative_prompt': 'visual.palette'})
                self.assertEqual(validate_parser_contract(swapped), swapped)
                self.assertEqual(reconfigure_visual_rule_sources(swapped, sources, selected_keys), nested)
        self.assertEqual(original, parser_contract('visual_rules.v2'))

    def test_conflicting_or_executable_paths_fail_closed(self):
        for invalid in ('visual', 'visual.palette.child', 'a.b.c.d.e', 'a..b', 'a[0]', 'a.*',
                'a.constructor', 'a.prototype', 'a.__proto__', 'a.schema_version', 'a/' , 'a'*65):
            with self.subTest(invalid=invalid), self.assertRaises(ValidationError):
                visual_rule_response_keys(response_keys={**self.keys, 'continuity_summary': invalid})
        maximum = '.'.join(['a'*64]*4)
        self.assertEqual(visual_rule_response_keys(response_keys={**self.keys, 'continuity_summary': maximum})['continuity_summary'], maximum)

    def test_array_gaps_mixed_parents_and_noncanonical_indices_fail_closed(self):
        for path in ('items.1', 'items.999999999999999999999999', 'items.01', 'items.-1',
                'items.+0', 'items.1e0', '0.items', 'items.٠', 'items.0.length'):
            # 1e0 is not an object key (must start with a letter) or an index.
            with self.subTest(path=path), self.assertRaises(ValidationError):
                visual_rule_response_keys(response_keys={**self.keys, 'continuity_summary': path,
                    'color_script': 'items.name'})

    def test_array_payload_requires_exact_length_type_and_leaf_validation(self):
        keys = {'continuity_summary': 'rules.0.summary', 'color_script': 'rules.1.palette', 'negative_prompt': 'rules.2.avoid'}
        sample = reconfigure_visual_rule_sources(parser_contract('visual_rules.v2'), None, keys)['response_example']
        bad_arrays = [None, {}, {'0': sample['rules'][0]}, [], sample['rules'][:2], sample['rules']+[None],
            [None, *sample['rules'][1:]], [[], *sample['rules'][1:]],
            [{'summary': None}, *sample['rules'][1:]], [{'summary': 'Valid', 'extra': 1}, *sample['rules'][1:]],
            list(reversed(sample['rules']))]
        for items in bad_arrays:
            with self.subTest(items=items), self.assertRaises(ValidationError):
                parse_visual_rules_output(payload={**sample, 'rules': items}, response_keys=keys)
        constrained = constrain_visual_rule_example(sample, {'lighting': ['low_key']}, keys['color_script'])
        self.assertEqual(constrained['rules'][1]['palette']['lighting'], 'low_key')
        self.assertEqual(sample['rules'][1]['palette']['lighting'], 'soft_diffuse')

    def test_each_container_is_exact_and_leaf_types_are_still_validated(self):
        nested = reconfigure_visual_rule_sources(parser_contract('visual_rules.v2'), None, self.keys)
        sample = nested['response_example']
        malformed = [None, [], {**sample, 'extra': 1}, {**sample, 'visual': None},
            {**sample, 'visual': [sample['visual']]}, {**sample, 'visual': {**sample['visual'], 'extra': 1}},
            {**sample, 'visual': {**sample['visual'], 'rules': {}}},
            {**sample, 'visual': {**sample['visual'], 'rules': {'summary': None}}},
            {**sample, 'visual': {**sample['visual'], 'rules': {'summary': 'Safe', 'extra': 1}}},
            {**sample, 'visual': {**sample['visual'], 'palette': {**sample['visual']['palette'], 'extra': 1}}},
            {**sample, 'visual.rules.summary': 'Literal dotted key is not a path'}]
        for payload in malformed:
            with self.subTest(payload=payload), self.assertRaises(ValidationError):
                parse_visual_rules_output(payload=payload, response_keys=self.keys)
        empty = deepcopy(sample)
        empty['constraints']['avoid'] = []
        self.assertEqual(parse_visual_rules_output(payload=empty, response_keys=self.keys)['candidate']['negative_prompt'], '')
        constrained = constrain_visual_rule_example(sample, {'lighting': ['low_key']}, self.keys['color_script'])
        self.assertEqual(constrained['visual']['palette']['lighting'], 'low_key')
        self.assertEqual(sample['visual']['palette']['lighting'], 'soft_diffuse')
