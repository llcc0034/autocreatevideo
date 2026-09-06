from copy import deepcopy
from types import SimpleNamespace
from unittest.mock import patch

from django.test import TestCase

from studio.services.workflow_classified_references import (
    ClassifiedReferenceError, MEDIA_GROUPS, MODEL_INPUTS, attach_plan,
    materialize, project_plan, prompt_references, validate_config, validate_frozen, probe_cases,
)
from studio.services.workflow_projectors import workflow_projector_creation_template, validate_workflow_projector_definition
from studio.services.workflow_compiler import compiler_contract_from_input, WorkflowCompilerError
from studio.tests.factories import conditioning_contract


class ClassifiedReferenceTests(TestCase):
    def setUp(self):
        self.definition = workflow_projector_creation_template('generic.classified')['definition']
        self.definition.update(
            detector={'fallback': False, 'allow_other_nodes': False, 'node_counts': {'FutureReferenceEncoder': 1}, 'optional_node_counts': {}, 'input_equals': []},
            output_nodes={'SaveVideo': 'video'},
            node_roles={'target': {'node_type': 'FutureReferenceEncoder', 'cardinality': 'one'}},
            asset_slots=[{'key': key, 'media_kind': kind, 'dynamic': True, 'contract_source': 'model_profile'} for key, kind in MEDIA_GROUPS.items()],
        )
        self.config = self.definition['projection']['config']
        self.config.update(capability='video.reference_to_video', conditioning_mode='References', target_role='target')
        for key, row in self.config['dynamic_groups'].items():
            row['prefix'] = f'custom_{key}.'
        self.graph = {'receiver': {'class_type': 'FutureReferenceEncoder', 'inputs': {'text': 'fixture'}}}
        self.specification = {'capabilities': ['video.reference_to_video'], 'conditioning_contract': conditioning_contract(
            'References', input_kind='classified_references', entity_images=(0, 4), reference_videos=(0, 3), paired_video_audios=(0, 3), reference_audios=(0, 2),
        )}

    def contract(self):
        result = {'parameters': {}}
        attach_plan(result, self.definition, self.graph, self.specification)
        return result

    def test_model_and_node_names_are_only_configuration(self):
        validate_workflow_projector_definition(self.definition, engine_key='generic.classified', engine_version=1)
        contract = self.contract()
        self.assertEqual(contract['compiler'], {'id': 'generic.comfy', 'version': 3})
        self.assertEqual(contract['classified_references']['conditioning_mode'], 'References')
        self.assertEqual(materialize(self.graph, contract, []), self.graph)
        graph = materialize(self.graph, contract, [{'asset_id': str(i), 'category': 'character'} for i in range(4)])
        self.assertEqual(len(graph), 5)
        self.assertEqual(graph['receiver']['inputs']['custom_images.3'], ['cr_images_3', 0])
        self.assertEqual(self.graph['receiver']['inputs'], {'text': 'fixture'})

    def test_sparse_paired_audio_uses_video_index_not_audio_count(self):
        self.definition['asset_slots'][2]['contract_source'] = 'workflow'
        self.config['dynamic_groups']['video_audios'].update(minimum_items=0, maximum_items=1)
        contract = self.contract()
        self.assertEqual(len(contract['classified_references']['groups']['video_audios']['input_names']), 3)
        videos = [{'asset_id': str(i), 'category': 'video'} for i in range(3)]
        for audio in ({'use_embedded_audio': True}, {'audio_asset_id': 'external'}):
            with self.subTest(audio=audio):
                graph = materialize(self.graph, contract, [*videos[:2], {**videos[2], **audio}])
                self.assertNotIn('custom_video_audios.0', graph['receiver']['inputs'])
                link = graph['receiver']['inputs']['custom_video_audios.2']
                self.assertEqual(link, ['cr_components_2', 1] if audio.get('use_embedded_audio') else ['cr_paired_audio_2', 0])

    def test_explicit_projector_counts_narrow_model_and_preview_contract(self):
        self.definition['asset_slots'][0]['contract_source'] = 'workflow'
        self.config['dynamic_groups']['images'].update(minimum_items=1, maximum_items=2)
        contract = self.contract()
        self.assertEqual(contract['conditioning_contract']['modes'][0]['inputs']['entity_images']['maximum_items'], 2)
        with self.assertRaises(ClassifiedReferenceError): materialize(self.graph, contract, [])
        with self.assertRaises(ClassifiedReferenceError): materialize(self.graph, contract, [{'asset_id': str(i), 'category': 'prop'} for i in range(3)])
        self.config['dynamic_groups']['images']['maximum_items'] = 5
        with self.assertRaisesRegex(ClassifiedReferenceError, '不能超出'): self.contract()

    def test_explicit_ports_and_prefix_are_exclusive_and_cannot_overlap(self):
        row = self.config['dynamic_groups']['images']
        row.update(prefix='', input_names=['p0', 'p1', 'p2', 'p3'])
        self.assertEqual(self.contract()['classified_references']['groups']['images']['input_names'], row['input_names'])
        row['prefix'] = 'p'
        with self.assertRaises(ClassifiedReferenceError): self.contract()
        row.update(prefix='', input_names=['p0'] * 4)
        with self.assertRaises(ClassifiedReferenceError): self.contract()

    def test_rejects_existing_target_connection_and_unknown_fields(self):
        self.graph['receiver']['inputs']['custom_images.0'] = ['existing', 0]
        with self.assertRaisesRegex(ClassifiedReferenceError, '已有来源'): self.contract()
        self.graph['receiver']['inputs'].pop('custom_images.0')
        self.config['dynamic_groups']['images']['dense'] = True
        with self.assertRaises(ClassifiedReferenceError): self.contract()

    def test_frozen_plan_cannot_change_ports_or_counts(self):
        contract = self.contract()
        for mutation in ('ports', 'counts'):
            changed = deepcopy(contract)
            group = changed['classified_references']['groups']['images']
            if mutation == 'ports': group['input_names'][0] = 'different'
            else: group['max_items'] = 9
            with self.subTest(mutation=mutation), self.assertRaisesRegex(ClassifiedReferenceError, '不一致'):
                validate_frozen(changed, self.graph, self.definition, self.specification)

    def test_audio_source_exclusion_and_bounds(self):
        contract = self.contract()
        with self.assertRaises(ClassifiedReferenceError):
            materialize(self.graph, contract, [{'asset_id': 'video', 'category': 'video', 'audio_asset_id': 'audio', 'use_embedded_audio': True}])
        with self.assertRaises(ClassifiedReferenceError):
            materialize(self.graph, contract, [{'asset_id': str(i), 'category': 'audio'} for i in range(3)])

    def test_prompt_validation_uses_configured_mode_and_arbitrary_labels(self):
        contract = self.contract()
        reference = {'category': 'prop', 'asset_id': 'sample', 'input_slot': 1, 'input_label': '[source-a]'}
        prompt = SimpleNamespace(conditioning_mode='References', prompt_text='Use [source-a]', structured_payload={'effective_conditioning_mode': 'References', 'reference_bindings': [reference]})
        self.assertEqual(prompt_references(contract, prompt), [reference])
        prompt.structured_payload['reference_bindings'].append(reference)
        with self.assertRaisesRegex(ClassifiedReferenceError, '全局唯一'): prompt_references(contract, prompt)

    def test_template_keys_have_safe_validation(self):
        self.config['prompt_template_keys'] = [{}]
        with self.assertRaises(ClassifiedReferenceError): validate_config(self.definition)

    def test_frozen_fixed_policy_rejects_removed_const_and_rewired_binding(self):
        self.definition['parameter_policies'] = [{
            'semantic_key': 'example.text', 'node_role': 'target', 'input_name': 'text',
            'policy': 'fixed', 'value': 'fixture',
        }]
        contract = self.contract()
        contract['parameters']['example.text'] = {'type': 'string', 'default': 'fixture', 'const': 'fixture'}
        bindings = [{'semantic_key': 'example.text', 'node_id': 'receiver', 'input_name': 'text', 'value_type': 'string'}]
        validate_frozen(contract, self.graph, self.definition, self.specification, bindings, [])
        changed = deepcopy(contract)
        del changed['parameters']['example.text']['const']
        with self.assertRaisesRegex(ClassifiedReferenceError, '固定参数'):
            validate_frozen(changed, self.graph, self.definition, self.specification, bindings, [])
        bindings[0]['input_name'] = 'other'
        with self.assertRaisesRegex(ClassifiedReferenceError, '参数接线'):
            validate_frozen(contract, self.graph, self.definition, self.specification, bindings, [])

    def test_live_probe_covers_minimum_maximum_and_sparse_last_video_audio(self):
        cases = dict(probe_cases(self.contract()))
        self.assertEqual(cases['classified_references_minimum'], [])
        self.assertEqual(len(cases['classified_references_maximum']), 9)
        videos = [row for row in cases['classified_references_last_video_audio'] if row['category'] == 'video']
        self.assertFalse(videos[0].get('audio_asset_id'))
        self.assertFalse(videos[1].get('audio_asset_id'))
        self.assertTrue(videos[2]['audio_asset_id'])

    def test_legacy_compilers_are_not_registered(self):
        for version in (1, 2, 3):
            with self.subTest(version=version), self.assertRaisesRegex(WorkflowCompilerError, '未注册'):
                compiler_contract_from_input({'compiler': {'id': 'minimax_h3', 'version': version}})
