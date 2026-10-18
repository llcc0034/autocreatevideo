from copy import deepcopy
from types import SimpleNamespace
from unittest.mock import patch

from django.test import TestCase

from studio.services.workflow_classified_references import (
    ClassifiedReferenceError, MEDIA_GROUPS, MODEL_INPUTS, attach_plan,
    materialize, materialize_reference_groups, project_plan, validate_config, validate_frozen, probe_cases,
)
from studio.services.workflow_projectors import workflow_projector_creation_template, validate_workflow_projector_definition
from studio.services.workflow_compiler import compiler_contract_from_input, WorkflowCompilerError
from studio.services.execution_inputs import workflow_input_targets, allocate_workflow_input_slots
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

    def test_token_graph_preserves_separate_uses_without_inventing_asset_ids(self):
        groups = {'images': [{'token': 'use:first-role'}, {'token': 'use:second-role'}],
            'videos': [{'token': 'use:selected-video-artifact', 'paired_audio': None}], 'audios': []}
        before = deepcopy(groups)
        graph = materialize_reference_groups(self.graph, self.contract(), groups)
        self.assertEqual(graph['cr_images_0']['inputs']['image'], 'use:first-role')
        self.assertEqual(graph['cr_images_1']['inputs']['image'], 'use:second-role')
        self.assertEqual(graph['cr_videos_0']['inputs']['file'], 'use:selected-video-artifact')
        self.assertEqual(groups, before)
        self.assertNotIn('asset_id', str(groups))
        self.assertEqual(self.graph['receiver']['inputs'], {'text': 'fixture'})

    def test_token_graph_uses_exact_video_position_for_external_or_embedded_audio(self):
        self.definition['asset_slots'][2]['contract_source'] = 'workflow'
        self.config['dynamic_groups']['video_audios'].update(minimum_items=0, maximum_items=1)
        for audio in ({'kind': 'embedded'}, {'kind': 'external', 'token': 'use:audio'}):
            groups = {'images': [], 'audios': [], 'videos': [
                {'token': 'use:video-0', 'paired_audio': None}, {'token': 'use:video-1', 'paired_audio': None},
                {'token': 'use:video-2', 'paired_audio': audio}]}
            graph = materialize_reference_groups(self.graph, self.contract(), groups)
            self.assertNotIn('custom_video_audios.0', graph['receiver']['inputs'])
            expected = ['cr_components_2', 1] if audio['kind'] == 'embedded' else ['cr_paired_audio_2', 0]
            self.assertEqual(graph['receiver']['inputs']['custom_video_audios.2'], expected)

    def test_token_graph_rejects_missing_pairing_facts_or_ambiguous_audio(self):
        rows = [{'token': 'use:video'}, {'token': 'use:video', 'paired_audio': {}},
            {'token': 'use:video', 'paired_audio': {'kind': 'embedded', 'token': 'use:external'}},
            {'token': 'use:video', 'paired_audio': {'kind': 'external'}}]
        for row in rows:
            with self.subTest(row=row), self.assertRaises(ClassifiedReferenceError):
                materialize_reference_groups(self.graph, self.contract(), {'images': [], 'videos': [row], 'audios': []})

    def test_execution_input_allocator_matches_real_classified_materializer(self):
        self.definition['asset_slots'][2]['contract_source'] = 'workflow'
        self.config['dynamic_groups']['video_audios'].update(minimum_items=0, maximum_items=1)
        contract = self.contract()
        targets = workflow_input_targets(SimpleNamespace(
            input_contract=contract, semantic_bindings=SimpleNamespace(values=lambda *args: []),
        ))
        inputs = {
            'videos': [{'item_key': f'video-{i}', 'kind': 'video'} for i in range(3)],
            'video_audios': [{'item_key': 'audio-use', 'kind': 'audio', 'paired_to': 'video-2'}],
        }
        port = allocate_workflow_input_slots(targets, inputs)['video_audios'][0]['target_port_ref']
        graph = materialize(self.graph, contract, [
            {'asset_id': 'video-0', 'category': 'video'},
            {'asset_id': 'video-1', 'category': 'video'},
            {'asset_id': 'video-2', 'category': 'video', 'audio_asset_id': 'audio'},
        ])
        self.assertEqual(graph[port['node_id']]['inputs'][port['input_name']], ['cr_paired_audio_2', 0])
        self.assertNotIn('custom_video_audios.0', graph[port['node_id']]['inputs'])

    def test_explicit_projector_counts_narrow_model_and_preview_contract(self):
        self.definition['asset_slots'][0]['contract_source'] = 'workflow'
        self.config['dynamic_groups']['images'].update(minimum_items=1, maximum_items=2)
        contract = self.contract()
        self.assertEqual(contract['conditioning_contract']['modes'][0]['inputs']['entity_images']['maximum_items'], 2)
        with self.assertRaises(ClassifiedReferenceError): materialize(self.graph, contract, [])
        with self.assertRaises(ClassifiedReferenceError): materialize(self.graph, contract, [{'asset_id': str(i), 'category': 'prop'} for i in range(3)])
        self.config['dynamic_groups']['images']['maximum_items'] = 5
        with self.assertRaisesRegex(ClassifiedReferenceError, '不能超出'): self.contract()

    def test_projector_cannot_discard_total_image_minimum_when_freezing(self):
        mode = self.specification['conditioning_contract']['modes'][0]
        mode['inputs']['total_images']['minimum_items'] = 2
        before = deepcopy(self.specification)
        with self.assertRaisesRegex(ClassifiedReferenceError, 'images.*total_images.*2.*4'):
            self.contract()
        self.definition['asset_slots'][0]['contract_source'] = 'workflow'
        self.config['dynamic_groups']['images'].update(minimum_items=2, maximum_items=3)
        contract = self.contract()
        self.assertEqual(contract['conditioning_contract']['modes'][0]['inputs']['total_images'], {
            'minimum_items': 2, 'maximum_items': 3,
        })
        self.assertEqual(self.specification, before)
        validate_frozen(contract, self.graph, self.definition, self.specification)
        with self.assertRaises(ClassifiedReferenceError):
            materialize(self.graph, contract, [{'asset_id': 'one', 'category': 'prop'}])

    def test_range_conflicts_name_model_mode_input_and_both_bounds(self):
        self.definition['asset_slots'][1]['contract_source'] = 'workflow'
        self.config['dynamic_groups']['videos'].update(minimum_items=0, maximum_items=4)
        with self.assertRaisesRegex(ClassifiedReferenceError, 'videos.*0.*4.*References.*reference_videos.*0.*3'):
            self.contract()

    def test_narrowed_topology_conflict_is_domain_error_not_unhandled_validation(self):
        self.definition['asset_slots'][1]['contract_source'] = 'workflow'
        self.config['dynamic_groups']['videos'].update(minimum_items=0, maximum_items=0)
        self.definition['asset_slots'][2]['contract_source'] = 'workflow'
        self.config['dynamic_groups']['video_audios'].update(minimum_items=0, maximum_items=0)
        before = deepcopy((self.definition, self.specification))
        with self.assertRaisesRegex(ClassifiedReferenceError, 'References.*dynamic_reference_topology.*用途'):
            self.contract()
        self.assertEqual((self.definition, self.specification), before)

    def test_narrowed_model_projector_frozen_ranges_and_rules_stay_consistent(self):
        before = deepcopy(self.specification)
        for slot in self.definition['asset_slots']:
            slot['contract_source'] = 'workflow'
            self.config['dynamic_groups'][slot['key']].update(minimum_items=0, maximum_items=2)
        contract = self.contract()
        mode = contract['conditioning_contract']['modes'][0]
        topology = mode['dynamic_reference_topology']
        for key in ('videos', 'audios'):
            self.assertEqual(topology[key]['maximum_items'], 2)
            self.assertTrue(all(row['maximum_items'] <= 2 for row in topology[key]['purposes']))
        original_topology = before['conditioning_contract']['modes'][0]['dynamic_reference_topology']
        for key in ('preprocessing', 'pairing_rules', 'mutual_exclusion_rules'):
            self.assertEqual(topology[key], original_topology[key])
        validate_frozen(contract, self.graph, self.definition, self.specification)
        self.assertEqual(self.specification, before)
        for count in (0, 1, 2):
            with self.subTest(count=count):
                graph = materialize(self.graph, contract, [{'asset_id': str(i), 'category': 'prop'} for i in range(count)])
                self.assertEqual(len(graph['receiver']['inputs']), 1 + count)
        changed = deepcopy(contract)
        changed['conditioning_contract']['modes'][0]['dynamic_reference_topology']['videos']['purposes'][0]['maximum_items'] = 3
        with self.assertRaisesRegex(ClassifiedReferenceError, '不一致'):
            validate_frozen(changed, self.graph, self.definition, self.specification)

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


    def test_same_asset_distinct_uses_preserve_separate_physical_ports(self):
        contract = self.contract()
        references = [
            {'category': 'prop', 'asset_id': 'same-asset', 'input_slot': 1, 'input_label': '[source-a]'},
            {'category': 'prop', 'asset_id': 'same-asset', 'input_slot': 2, 'input_label': '[source-b]'},
        ]
        graph = materialize(self.graph, contract, references)
        self.assertEqual(len(references), 2)
        self.assertEqual(graph['receiver']['inputs']['custom_images.0'], ['cr_images_0', 0])
        self.assertEqual(graph['receiver']['inputs']['custom_images.1'], ['cr_images_1', 0])
        self.assertEqual(graph['cr_images_0']['inputs']['image'], graph['cr_images_1']['inputs']['image'])

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
