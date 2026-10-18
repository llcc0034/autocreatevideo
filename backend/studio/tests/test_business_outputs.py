from copy import deepcopy
import importlib
import json
from types import SimpleNamespace
from unittest.mock import patch

from django.contrib.auth import get_user_model
from django.core.exceptions import ValidationError
from django.test import TestCase
from django.test.utils import CaptureQueriesContext
from django.db import connection, transaction
from rest_framework.test import APIClient

from studio.models import BusinessOutputVersion, PlatformLifecycleAuditEvent
from studio.services.business_outputs import output_reference, verify_output_reference, write_output, output_contract, parser_contract
from studio.services.json_contracts import json_contract
from studio.tests.output_structure_fixtures import response_structure


class BusinessOutputTests(TestCase):
    def test_structure_is_explicit_published_and_paths_types_must_match(self):
        from uuid import uuid4
        from studio.services.data_structures import write_data_structure
        draft_structure = write_data_structure(content={'name': 'Not published', 'nodes': []})
        original = self.draft()
        url = f'{self.url}{original["id"]}/'
        base = {'name': 'No implicit structure', 'parser_key': 'visual_rules.v2'}
        for value in (None, str(uuid4()), str(draft_structure.pk)):
            result = self.client.post(self.url, {**base, 'data_structure_version': value}, format='json')
            self.assertEqual(result.status_code, 400, result.data)
        self.assertEqual(self.client.post(self.url, base, format='json').status_code, 400)
        sample = deepcopy(original['parser_contract']['response_example'])
        sample['continuity_summary'] = 3
        wrong = response_structure(example=sample)
        audits = PlatformLifecycleAuditEvent.objects.count()
        result = self.client.patch(url, {'expected_revision': 1, 'data_structure_version': str(wrong.pk)}, format='json')
        self.assertEqual(result.status_code, 400, result.data)
        self.assertIn('response_keys.continuity_summary', str(result.data))
        result = self.client.patch(url, {'expected_revision': 1, 'response_keys': {
            'continuity_summary': 'absent', 'color_script': 'color_design', 'negative_prompt': 'negative_constraints'}}, format='json')
        self.assertEqual(result.status_code, 400, result.data)
        self.assertIn('absent', str(result.data))
        self.assertEqual(PlatformLifecycleAuditEvent.objects.count(), audits)
        self.assertEqual(self.client.get(url).data['contract_sha256'], original['contract_sha256'])

    def test_frozen_structure_survives_retirement_and_rejects_rehashed_bad_nodes(self):
        from studio.services.data_structures import transition_data_structure
        published = self.action(self.draft(), 'publish')
        output = BusinessOutputVersion.objects.get(pk=published['id'])
        snapshot = output_reference(output)
        self.assertEqual(snapshot['data_structure']['id'], str(published['data_structure_version']))
        transition_data_structure(version_id=output.data_structure_version_id, status='retired')
        # Existing parser safety ceilings still read the public configuration;
        # frozen output/structure/field metadata must not look up current rows.
        with CaptureQueriesContext(connection) as queries:
            self.assertEqual(verify_output_reference(snapshot), snapshot)
        self.assertFalse(any(table in query['sql'].lower() for query in queries for table in (
            'studio_businessoutputversion', 'studio_datastructure', 'studio_fielddefinition')))
        successor = self.action(published, 'successor')
        self.assertEqual(successor['data_structure_version'], published['data_structure_version'])
        rejected = self.client.post(f'{self.url}{successor["id"]}/publish/', {'expected_revision': 1}, format='json')
        self.assertEqual(rejected.status_code, 400)
        changed = deepcopy(snapshot)
        changed['data_structure']['nodes'][0]['parent_key'] = 'missing'
        changed['sha256'] = json_contract({key: value for key, value in changed.items() if key != 'sha256'}).sha256
        with self.assertRaises(ValidationError):
            verify_output_reference(changed)

    def test_saved_preview_and_actual_response_share_frozen_structure_constraints(self):
        from studio.services.production_visual_rules import finalize_visual_rules
        structure = response_structure(constraints={'continuity_summary': {'max_length': 80}})
        output = write_output(actor=self.staff, data={'name': 'Typed response', 'parser_key': 'visual_rules.v2',
            'data_structure_version': structure.pk})
        output = write_output(actor=self.staff, output_id=output.pk, expected_revision=1, action='publish')
        snapshot = output_reference(output)
        sample = deepcopy(snapshot['parser']['response_example'])
        body = {'output_version_id': str(output.pk), 'expected_revision': output.revision}
        url = self.url + 'preview-visual-rules-output/'
        self.assertTrue(self.client.post(url, {**body, 'response_text': json.dumps(sample)}, format='json').data['valid'])
        sample['continuity_summary'] = 'Complete continuity rule. ' * 8
        before = deepcopy(sample)
        report = self.client.post(url, {**body, 'response_text': json.dumps(sample)}, format='json')
        self.assertFalse(report.data['valid'], report.data)
        self.assertIn('response.continuity_summary', report.data['errors'][0]['message'])
        with self.assertRaises(ValidationError) as failure:
            finalize_visual_rules(plan={'business_output': snapshot, 'resource_estimate': {}}, payload=sample)
        self.assertIn('response.continuity_summary', failure.exception.message_dict)
        self.assertEqual(sample, before, 'invalid values must remain complete, never truncated')

    def test_unconfigured_old_output_is_not_repaired_when_read_or_selected(self):
        output = BusinessOutputVersion.objects.create(name='Old record', parser_key='visual_rules.v2',
            parser_contract=parser_contract('visual_rules.v2'), contract_sha256='a'*64, contract_size_bytes=1)
        self.assertEqual(self.client.get(self.url).data['results'][0]['data_structure_version'], None)
        self.assertEqual(self.client.get(f'{self.url}{output.pk}/').status_code, 400)
        output.refresh_from_db()
        self.assertIsNone(output.data_structure_version_id)
        self.assertEqual(output.contract_sha256, 'a'*64)

    def test_optional_camera_field_version_lifecycle_and_strict_preview(self):
        from studio.services.visual_rules_output_contract import normalize_visual_rule_field_sources
        original = self.action(self.draft(), 'publish')
        draft = self.action(original, 'successor')
        sources = {**normalize_visual_rule_field_sources(), 'camera_guidance': 'camera_guidance'}
        changed = self.client.patch(f'{self.url}{draft["id"]}/', {'expected_revision': 1,
            'field_sources': sources, 'data_structure_version': str(response_structure(field_sources=sources).pk)}, format='json')
        self.assertEqual(changed.status_code, 200, changed.data)
        published = self.action(changed.data, 'publish')
        sample = published['parser_contract']['response_example']
        body = {'output_version_id': published['id'], 'expected_revision': published['revision']}
        url = self.url + 'preview-visual-rules-output/'
        result = self.client.post(url, {**body, 'response_text': json.dumps(sample)}, format='json')
        self.assertTrue(result.data['valid'], result.data)
        self.assertEqual(result.data['candidate']['camera_guidance'], sample['camera_guidance'])
        for value in ('', None, [], '中文镜头', '<b>camera</b>', 'line\nbreak'):
            response = self.client.post(url, {**body, 'response_text': json.dumps({**sample, 'camera_guidance': value})}, format='json')
            self.assertFalse(response.data['valid'], response.data)
        missing = {key: value for key, value in sample.items() if key != 'camera_guidance'}
        self.assertFalse(self.client.post(url, {**body, 'response_text': json.dumps(missing)}, format='json').data['valid'])
        successor = self.action(published, 'successor')
        removed = self.client.patch(f'{self.url}{successor["id"]}/', {'expected_revision': 1,
            'field_sources': normalize_visual_rule_field_sources(), 'data_structure_version': str(response_structure().pk)}, format='json')
        self.assertEqual(removed.status_code, 200, removed.data)
        self.assertEqual(len(removed.data['parser_contract']['fields']), 3)
        self.assertEqual(self.client.get(f'{self.url}{original["id"]}/').data['parser_contract'], original['parser_contract'])
        self.assertEqual(self.client.get(f'{self.url}{published["id"]}/').data['parser_contract'], published['parser_contract'])

    def test_nested_response_layout_is_versioned_and_preview_uses_saved_tree(self):
        self.assert_versioned_layout({'continuity_summary': 'visual.continuity', 'color_script': 'visual.palette', 'negative_prompt': 'avoid.items'})

    def test_array_response_layout_is_versioned_and_preview_uses_saved_tree(self):
        self.assert_versioned_layout({'continuity_summary': 'rules.0.summary', 'color_script': 'rules.1.palette', 'negative_prompt': 'rules.2.avoid'})

    def assert_versioned_layout(self, keys):
        from studio.services.visual_rules_output_contract import visual_rule_response_value
        original = self.action(self.draft(), 'publish')
        draft = self.action(original, 'successor')
        url = f'{self.url}{draft["id"]}/'
        changed = self.client.patch(url, {'expected_revision': 1, 'response_keys': keys,
            'data_structure_version': str(response_structure(response_keys=keys).pk),
            'allowed_values': {'lighting': ['low_key']}}, format='json')
        self.assertEqual(changed.status_code, 200, changed.data)
        sample = changed.data['parser_contract']['response_example']
        self.assertEqual(visual_rule_response_value(sample, keys['color_script'])['lighting'], 'low_key')
        published = self.action(changed.data, 'publish')
        before = PlatformLifecycleAuditEvent.objects.count()
        preview = self.client.post(self.url + 'preview-visual-rules-output/', {
            'output_version_id': published['id'], 'expected_revision': published['revision'],
            'response_text': json.dumps(sample)}, format='json')
        self.assertEqual(preview.status_code, 200, preview.data)
        self.assertTrue(preview.data['valid'], preview.data)
        self.assertEqual(preview.data['candidate']['continuity_summary'], visual_rule_response_value(sample, keys['continuity_summary']))
        self.assertEqual(PlatformLifecycleAuditEvent.objects.count(), before)
        successor = self.action(published, 'successor')
        restored = self.client.patch(f'{self.url}{successor["id"]}/', {'expected_revision': 1, 'response_keys': {}, 'data_structure_version': str(response_structure().pk)}, format='json')
        self.assertEqual(restored.status_code, 200, restored.data)
        self.assertEqual(restored.data['parser_contract']['response_example']['color_design'], visual_rule_response_value(sample, keys['color_script']))
        for row in (original, published):
            self.assertEqual(self.client.get(f'{self.url}{row["id"]}/').data['parser_contract'], row['parser_contract'])

    def test_response_keys_are_exact_frozen_names_not_aliases(self):
        keys = {'continuity_summary': 'continuity', 'color_script': 'palette', 'negative_prompt': 'avoid'}
        original = self.action(self.draft(), 'publish')
        draft = self.action(original, 'successor')
        changed = self.client.patch(f'{self.url}{draft["id"]}/', {'expected_revision': 1,
            'response_keys': keys, 'data_structure_version': str(response_structure(response_keys=keys).pk), 'allowed_values': {'lighting': ['low_key']}}, format='json')
        self.assertEqual(changed.status_code, 200, changed.data)
        sample = changed.data['parser_contract']['response_example']
        self.assertEqual(set(sample), {'schema_version', *keys.values()})
        self.assertEqual(sample['palette']['lighting'], 'low_key')
        published = self.action(changed.data, 'publish')
        frozen = output_reference(BusinessOutputVersion.objects.get(pk=published['id']))
        self.assertEqual(verify_output_reference(frozen), frozen)
        body = {'output_version_id': published['id'], 'expected_revision': published['revision']}
        url = self.url + 'preview-visual-rules-output/'
        before = PlatformLifecycleAuditEvent.objects.count()
        result = self.client.post(url, {**body, 'response_text': json.dumps(sample)}, format='json')
        self.assertTrue(result.data['valid'], result.data)
        self.assertEqual(set(result.data['candidate']), set(keys))
        for invalid in (original['parser_contract']['response_example'], {**sample, 'color_design': sample['palette']},
                {**sample, 'palette': 'Wrong type'}):
            report = self.client.post(url, {**body, 'response_text': json.dumps(invalid)}, format='json')
            self.assertEqual(report.status_code, 200, report.data)
            self.assertFalse(report.data['valid'])
            self.assertIsNone(report.data['candidate'])
        self.assertEqual(self.client.post(url, {**body, 'response_text': json.dumps(sample), 'response_keys': {}}, format='json').status_code, 400)
        self.assertEqual(PlatformLifecycleAuditEvent.objects.count(), before)
        successor = self.action(published, 'successor')
        self.assertEqual(successor['parser_contract'], frozen['parser'])
        cleared = self.client.patch(f'{self.url}{successor["id"]}/', {'expected_revision': 1, 'response_keys': {}, 'data_structure_version': str(response_structure().pk)}, format='json')
        self.assertEqual(cleared.status_code, 200, cleared.data)
        self.assertNotIn('response_keys', cleared.data['parser_contract'])
        self.assertEqual(cleared.data['parser_contract']['response_example']['color_design'], sample['palette'])
        self.assertEqual(self.client.get(f'{self.url}{original["id"]}/').data['parser_contract'], original['parser_contract'])
        self.assertEqual(output_reference(BusinessOutputVersion.objects.get(pk=published['id'])), frozen)

    def test_response_key_validation_rejects_ambiguity_and_key_swap_preserves_values(self):
        draft = self.draft()
        url = f'{self.url}{draft["id"]}/'
        keys = {'continuity_summary': 'summary', 'color_script': 'palette', 'negative_prompt': 'avoid'}
        before = PlatformLifecycleAuditEvent.objects.count()
        invalids = [None, [], {'continuity_summary': 'summary'}, {**keys, 'extra': 'extra'}]
        invalids += [{**keys, 'color_script': key} for key in ('summary', 'schema_version', 'constructor', 'prototype',
            '__proto__', 'nested..path', 'a.b.c.d.e', 'a[0]', 'a.*', 'a.schema_version', 'a.constructor', 'summary.child', '$.value', 'Name', 'a'*65, ' trailing ', 'tail\n', '', 42)]
        for value in invalids:
            with self.subTest(value=value):
                response = self.client.patch(url, {'expected_revision': 1, 'response_keys': value}, format='json')
                self.assertEqual(response.status_code, 400, response.data)
        self.assertEqual(PlatformLifecycleAuditEvent.objects.count(), before)
        self.assertEqual(self.client.get(url).data['revision'], 1)
        # Names may be swapped, but values must never be overwritten or guessed.
        swapped = {'continuity_summary': 'color_design', 'color_script': 'continuity_summary', 'negative_prompt': 'negative_constraints'}
        response = self.client.patch(url, {'expected_revision': 1, 'response_keys': swapped, 'data_structure_version': str(response_structure(response_keys=swapped).pk)}, format='json')
        self.assertEqual(response.status_code, 200, response.data)
        sample = response.data['parser_contract']['response_example']
        self.assertEqual(sample['color_design'], draft['parser_contract']['response_example']['continuity_summary'])
        self.assertEqual(sample['continuity_summary'], draft['parser_contract']['response_example']['color_design'])
        sources = {'continuity_summary': 'continuity_points', 'color_script': 'color_script', 'negative_prompt': 'negative_prompt'}
        changed = self.client.patch(url, {'expected_revision': 2, 'field_sources': sources, 'data_structure_version': str(response_structure(field_sources=sources, response_keys=swapped).pk)}, format='json')
        self.assertEqual(changed.status_code, 200, changed.data)
        self.assertEqual(changed.data['parser_contract']['response_keys'], swapped)
        self.assertIsInstance(changed.data['parser_contract']['response_example']['color_design'], list)

    @classmethod
    def setUpTestData(cls):
        cls.staff = get_user_model().objects.create_user('business-output-staff', is_staff=True)
        cls.member = get_user_model().objects.create_user('business-output-member')

    def setUp(self):
        self.client = APIClient()
        self.client.force_authenticate(self.staff)
        self.url = '/api/v1/business-output-versions/'
        self.enterContext(patch('studio.services.provider_registry._request_json', side_effect=AssertionError('No network')))

    def draft(self):
        response = self.client.post(self.url, {'name': '全剧视觉输出', 'parser_key': 'visual_rules.v2',
            'data_structure_version': str(response_structure().pk),
            'quality_advisories': {'schema_version': 1, 'continuity_summary_max_chars': 1}}, format='json')
        self.assertEqual(response.status_code, 201, response.data)
        return response.data

    def action(self, row, action):
        response = self.client.post(f'{self.url}{row["id"]}/{action}/', {'expected_revision': row['revision']}, format='json')
        self.assertEqual(response.status_code, 201 if action == 'successor' else 200, response.data)
        return response.data

    def test_lifecycle_keeps_old_contract_and_requires_explicit_new_reference(self):
        draft = self.draft()
        with self.assertRaises(ValidationError):
            output_reference(BusinessOutputVersion.objects.get(pk=draft['id']))
        published = self.action(draft, 'publish')
        original = output_reference(BusinessOutputVersion.objects.get(pk=draft['id']))
        successor = self.action(published, 'successor')
        self.assertEqual(successor['version'], 2)
        self.assertEqual(successor['status'], 'draft')
        self.assertEqual(successor['output_key'], published['output_key'])
        response = self.client.patch(f'{self.url}{successor["id"]}/', {'expected_revision': successor['revision'],
            'quality_advisories': {}}, format='json')
        self.assertEqual(response.status_code, 200, response.data)
        self.assertEqual(output_reference(BusinessOutputVersion.objects.get(pk=draft['id'])), original)
        self.action(published, 'retire')
        retired = BusinessOutputVersion.objects.get(pk=draft['id'])
        with self.assertRaises(ValidationError):
            output_reference(retired, require_published=True)
        self.assertEqual(verify_output_reference(original), original)
        self.assertEqual(output_reference(retired), original)
        self.assertTrue(PlatformLifecycleAuditEvent.objects.filter(object_type='business_output_version', object_id=draft['id']).exists())

    def test_immutable_conflict_and_invalid_quality_do_not_write(self):
        draft = self.draft()
        url = f'{self.url}{draft["id"]}/'
        for payload in ({'expected_revision': 999, 'name': 'conflict'}, {'name': 'missing revision'},
                {'expected_revision': True, 'name': 'boolean'}, {'expected_revision': 1, 'status': 'published'},
                {'expected_revision': 1, 'parser_key': 'custom.python'},
                {'expected_revision': 1, 'quality_advisories': {'schema_version': 1, 'base_colors_min_items': 22}}):
            with self.subTest(payload=payload):
                response = self.client.patch(url, payload, format='json')
                self.assertEqual(response.status_code, 400, response.data)
        self.assertEqual(self.client.get(url).data['revision'], 1)
        published = self.action(draft, 'publish')
        self.assertEqual(self.client.patch(url, {'expected_revision': published['revision'], 'name': 'changed'}, format='json').status_code, 400)
        self.assertEqual(self.client.delete(url).status_code, 405)
        self.action(published, 'successor')
        response = self.client.post(url+'successor/', {'expected_revision': published['revision']}, format='json')
        self.assertEqual(response.status_code, 400)

    def test_options_and_paged_list_are_metadata_only_and_permissions_apply(self):
        draft = self.draft()
        response = self.client.get(self.url+'parsers/')
        self.assertEqual(response.status_code, 200)
        self.assertEqual(response.data['parser']['key'], 'visual_rules.v2')
        self.assertEqual(len(response.data['parser']['fields']), 3)
        page = self.client.get(self.url, {'page_size': 1}).data
        self.assertEqual(page['count'], 1)
        self.assertNotIn('quality_advisories', page['results'][0])
        self.assertNotIn('parser_contract', page['results'][0])
        self.assertEqual(self.client.get(self.url, {'status': 'published'}).data['count'], 0)
        self.client.force_authenticate(self.member)
        for url in (self.url, self.url+'parsers/', self.url+draft['id']+'/'):
            self.assertEqual(self.client.get(url).status_code, 403)
        self.assertEqual(self.client.post(self.url, {}, format='json').status_code, 403)

    def test_frozen_reference_checks_digest_identity_parser_and_quality(self):
        published = self.action(self.draft(), 'publish')
        row = BusinessOutputVersion.objects.get(pk=published['id'])
        original = output_reference(row)
        for change in ({'version': True}, {'id': 'arbitrary'}, {'parser': {'key': 'unknown'}},
                {'quality_advisories': {'schema_version': 1, 'wrong': 1}}, {'name': ''}):
            value = {**deepcopy(original), **change}
            value['sha256'] = json_contract({k: v for k, v in value.items() if k != 'sha256'}).sha256
            with self.subTest(change=change), self.assertRaises(ValidationError):
                verify_output_reference(value)
        row.name = 'unexpected overwrite'
        row.save(update_fields=['name'])
        with self.assertRaises(ValidationError):
            output_reference(row)

    def test_catalog_changes_cannot_rebuild_saved_or_frozen_contracts(self):
        published = self.action(self.draft(), 'publish')
        row = BusinessOutputVersion.objects.get(pk=published['id'])
        original = output_reference(row)
        with patch('studio.services.business_outputs.parser_contract', side_effect=AssertionError('No current catalog')):
            self.assertEqual(output_reference(row), original)
            self.assertEqual(verify_output_reference(original), original)
            successor = self.action(published, 'successor')
        self.assertEqual(successor['parser_contract'], row.parser_contract)
        row.parser_contract = {}
        with self.assertRaises(ValidationError):
            output_contract(row)
        for field in ('response_field', 'source_selector'):
            snapshot = deepcopy(original)
            snapshot['parser']['fields'][0][field] = 'unsupported.path'
            snapshot['sha256'] = json_contract({k: v for k, v in snapshot.items() if k != 'sha256'}).sha256
            with self.assertRaises(ValidationError):
                verify_output_reference(snapshot)

    def test_saved_preview_binds_revision_digest_and_keeps_all_facts(self):
        published = self.action(self.draft(), 'publish')
        retired = self.action(published, 'retire')
        audits = PlatformLifecycleAuditEvent.objects.count()
        body = {'response_text': json.dumps(parser_contract('visual_rules.v2')['response_example']),
            'output_version_id': retired['id'], 'expected_revision': retired['revision']}
        url = self.url+'preview-visual-rules-output/'
        with patch('studio.services.business_outputs.parser_contract', side_effect=AssertionError('No current catalog')):
            result = self.client.post(url, body, format='json')
        self.assertEqual(result.status_code, 200, result.data)
        self.assertTrue(result.data['valid'])
        self.assertTrue(result.data['warnings'])
        self.assertEqual(result.data['output_contract_sha256'], retired['contract_sha256'])
        self.assertEqual(result.data['output_revision'], retired['revision'])
        self.assertEqual(result.data['output_version_id'], retired['id'])
        for changes in ({'expected_revision': 1}, {'expected_revision': True}, {'quality_advisories': {}},
                {'output_version_id': '00000000-0000-0000-0000-000000000001'}):
            response = self.client.post(url, {**body, **changes}, format='json')
            self.assertEqual(response.status_code, 400, response.data)
        response = self.client.post(url, {**body, 'response_text': '{}'}, format='json')
        self.assertEqual(response.status_code, 200)
        self.assertFalse(response.data['valid'])
        self.assertEqual(response.data['output_contract_sha256'], retired['contract_sha256'])
        self.assertEqual(PlatformLifecycleAuditEvent.objects.count(), audits)
        self.assertEqual(BusinessOutputVersion.objects.count(), 1)
        self.client.force_authenticate(self.member)
        self.assertEqual(self.client.post(url, body, format='json').status_code, 403)

    def test_materialization_preserves_hash_revision_timestamps_and_rejects_unreproducible_record(self):
        published = self.action(self.draft(), 'publish')
        row = BusinessOutputVersion.objects.get(pk=published['id'])
        # Exercise the historical migration with an explicit historical fixture,
        # not a v2 contract disguised as an old record.
        historical = output_contract(row).value
        historical.pop('data_structure')
        historical['schema_version'] = 1
        contract = json_contract(historical)
        row.contract_sha256, row.contract_size_bytes = contract.sha256, contract.size_bytes
        row.save(update_fields=['contract_sha256', 'contract_size_bytes'])
        before = (row.contract_sha256, row.contract_size_bytes, row.revision, row.updated_at, row.published_at)
        migration = importlib.import_module('studio.migrations.0186_freeze_business_output_parser')
        self.assertEqual(migration.PARSER, row.parser_contract)
        apps = SimpleNamespace(get_model=lambda *_: BusinessOutputVersion)
        editor = SimpleNamespace(connection=connection)
        BusinessOutputVersion.objects.filter(pk=row.pk).update(parser_contract={})
        migration.freeze_parsers(apps, editor)
        row.refresh_from_db()
        self.assertEqual((row.contract_sha256, row.contract_size_bytes, row.revision, row.updated_at, row.published_at), before)
        self.assertEqual(row.parser_contract, migration.PARSER)
        with self.assertRaises(ValidationError):
            output_reference(row)
        BusinessOutputVersion.objects.filter(pk=row.pk).update(contract_sha256='0'*64, parser_contract={})
        with self.assertRaises(RuntimeError), transaction.atomic():
            migration.freeze_parsers(apps, editor)
        row.refresh_from_db()
        self.assertEqual(row.parser_contract, {})
        self.assertEqual(row.contract_sha256, '0'*64)
        detail = self.client.get(f'{self.url}{row.pk}/')
        self.assertEqual(detail.status_code, 400)
        with self.assertRaises(ValidationError):
            write_output(actor=self.staff, output_id=row.pk, expected_revision=row.revision,
                data={'description': 'Must not silently repair a damaged contract'})

    def test_allowed_values_authoring_preview_successor_and_no_mutation(self):
        allowed = {'base_colors': ['navy'], 'lighting': ['low_key'], 'preserve_natural_skin_exposure': [False]}
        created = self.client.post(self.url, {'name': '受控色彩', 'parser_key': 'visual_rules.v2', 'allowed_values': allowed, 'data_structure_version': str(response_structure().pk)}, format='json')
        self.assertEqual(created.status_code, 201, created.data)
        draft = created.data
        self.assertEqual(draft['parser_contract']['allowed_values'], allowed)
        sample = draft['parser_contract']['response_example']
        self.assertEqual(sample['color_design']['base_colors'], ['navy'])
        self.assertFalse(sample['color_design']['preserve_natural_skin_exposure'])
        published = self.action(draft, 'publish')
        ref = output_reference(BusinessOutputVersion.objects.get(pk=draft['id']))
        successor = self.action(published, 'successor')
        self.assertEqual(successor['parser_contract'], published['parser_contract'])
        cleared = self.client.patch(f'{self.url}{successor["id"]}/', {'expected_revision': 1, 'allowed_values': {}}, format='json')
        self.assertEqual(cleared.status_code, 200, cleared.data)
        self.assertNotIn('allowed_values', cleared.data['parser_contract'])
        self.assertEqual(output_reference(BusinessOutputVersion.objects.get(pk=draft['id'])), ref)
        url = self.url+'preview-visual-rules-output/'
        body = {'response_text': json.dumps(sample), 'output_version_id': published['id'], 'expected_revision': published['revision']}
        audits = PlatformLifecycleAuditEvent.objects.count()
        self.assertTrue(self.client.post(url, body, format='json').data['valid'])
        sample = deepcopy(sample)
        sample['color_design']['lighting'] = 'soft_diffuse'
        response = self.client.post(url, {**body, 'response_text': json.dumps(sample)}, format='json')
        self.assertEqual(response.status_code, 200, response.data)
        self.assertFalse(response.data['valid'])
        self.assertEqual(response.data['errors'][0]['code'], 'visual_rules_output_value_not_allowed')
        self.assertIsNone(response.data['candidate'])
        self.assertEqual(response.data['output_contract_sha256'], published['contract_sha256'])
        self.assertEqual(self.client.post(url, {**body, 'allowed_values': {}}, format='json').status_code, 400)
        self.assertEqual(PlatformLifecycleAuditEvent.objects.count(), audits)
        self.assertTrue(self.client.post(url, {'response_text': json.dumps(sample), 'allowed_values': {}}, format='json').data['valid'])

    def test_invalid_allowed_values_do_not_write_or_silently_disable_checks(self):
        draft = self.draft()
        audits = PlatformLifecycleAuditEvent.objects.count()
        for invalid in (None, [], {'wrong.path': ['blue']}, {'lighting': []}, {'lighting': ['invented']},
                {'lighting': ['low_key', 'low_key']}, {'preserve_natural_skin_exposure': [0]},
                {'preserve_natural_skin_exposure': ['false']}, {'base_colors': ['none']}):
            with self.subTest(value=invalid):
                response = self.client.patch(f'{self.url}{draft["id"]}/', {'expected_revision': 1, 'allowed_values': invalid}, format='json')
                self.assertEqual(response.status_code, 400, response.data)
        self.assertEqual(PlatformLifecycleAuditEvent.objects.count(), audits)
        self.assertEqual(self.client.get(f'{self.url}{draft["id"]}/').data['revision'], 1)
        from studio.services.business_outputs import validate_parser_contract
        with self.assertRaises(ValidationError):
            validate_parser_contract({**parser_contract('visual_rules.v2'), 'allowed_values': None})
        fields = self.client.get(self.url+'parsers/').data['allowed_value_fields']
        self.assertEqual(len(fields), 9)
        self.assertEqual(next(row for row in fields if row['key'] == 'preserve_natural_skin_exposure')['choices'], [True, False])

    def test_source_mapping_selects_one_response_shape_and_preserves_frozen_versions(self):
        from studio.services.visual_rules_output_contract import normalize_visual_rule_field_sources
        sources = {**normalize_visual_rule_field_sources(), 'color_script': 'color_script'}
        original = self.action(self.draft(), 'publish')
        draft = self.action(original, 'successor')
        url = f'{self.url}{draft["id"]}/'
        changed = self.client.patch(url, {'expected_revision': 1, 'field_sources': sources, 'data_structure_version': str(response_structure(field_sources=sources).pk)}, format='json')
        self.assertEqual(changed.status_code, 200, changed.data)
        parser = changed.data['parser_contract']
        self.assertEqual(next(row for row in parser['fields'] if row['field']=='color_script')['response_field'], 'color_script')
        self.assertNotIn('color_design', parser['response_example'])
        self.assertEqual(self.client.get(f'{self.url}{original["id"]}/').data['parser_contract'], original['parser_contract'])
        preview = self.url+'preview-visual-rules-output/'
        body = {'output_version_id': draft['id'], 'expected_revision': 2}
        sample = deepcopy(parser['response_example'])
        sample['color_script'] = 'An intact, deliberately authored English color description.'
        valid = self.client.post(preview, {**body, 'response_text': json.dumps(sample)}, format='json')
        self.assertTrue(valid.data['valid'], valid.data)
        self.assertEqual(valid.data['candidate']['color_script'], sample['color_script'])
        for invalid in (original['parser_contract']['response_example'], {**sample, 'color_design': {}},
                {**sample, 'color_script': {}}, {**sample, 'color_script': '<script>bad</script>'}, {**sample, 'color_script': ''}):
            result = self.client.post(preview, {**body, 'response_text': json.dumps(invalid)}, format='json')
            self.assertEqual(result.status_code, 200, result.data)
            self.assertFalse(result.data['valid'], result.data)
            self.assertIsNone(result.data['candidate'])
        self.assertEqual(self.client.post(preview, {**body, 'response_text': json.dumps(sample), 'field_sources': sources}, format='json').status_code, 400)
        omitted = self.client.patch(url, {'expected_revision': 2, 'description': 'No mapping change'}, format='json')
        self.assertEqual(omitted.data['parser_contract'], parser)

    def test_source_mapping_conflicts_require_explicit_cleanup_and_no_writes(self):
        from studio.services.visual_rules_output_contract import normalize_visual_rule_field_sources
        sources = {**normalize_visual_rule_field_sources(), 'color_script': 'color_script'}
        draft = self.draft()
        url = f'{self.url}{draft["id"]}/'
        configured = self.client.patch(url, {'expected_revision': 1, 'allowed_values': {'lighting': ['low_key']},
            'quality_advisories': {'schema_version': 1, 'base_colors_min_items': 0}}, format='json')
        self.assertEqual(configured.status_code, 200)
        audits = PlatformLifecycleAuditEvent.objects.count()
        for payload in ({'field_sources': sources}, {'field_sources': sources, 'allowed_values': {}},
                {'field_sources': sources, 'quality_advisories': {}}, {'field_sources': {}}, {'field_sources': None},
                {'field_sources': {**sources, 'color_script': '$.new.path'}}, {'field_sources': {**sources, 'extra': 'color_design'}}):
            response = self.client.patch(url, {'expected_revision': 2, **payload}, format='json')
            self.assertEqual(response.status_code, 400, response.data)
        self.assertEqual(PlatformLifecycleAuditEvent.objects.count(), audits)
        self.assertEqual(self.client.get(url).data['contract_sha256'], configured.data['contract_sha256'])
        cleared = self.client.patch(url, {'expected_revision': 2, 'field_sources': sources, 'allowed_values': {}, 'quality_advisories': {}, 'data_structure_version': str(response_structure(field_sources=sources).pk)}, format='json')
        self.assertEqual(cleared.status_code, 200, cleared.data)
        self.assertEqual(cleared.data['revision'], 3)
        self.assertEqual(len(self.client.get(self.url+'parsers/').data['field_source_options'][1]['choices']), 2)

    def test_all_field_mappings_survive_publish_successor_and_exact_saved_preview(self):
        original = self.action(self.draft(), 'publish')
        draft = self.action(original, 'successor')
        sources = {'continuity_summary': 'continuity_points', 'color_script': 'color_script', 'negative_prompt': 'negative_prompt'}
        changed = self.client.patch(f'{self.url}{draft["id"]}/', {'expected_revision': 1, 'field_sources': sources, 'data_structure_version': str(response_structure(field_sources=sources).pk)}, format='json')
        self.assertEqual(changed.status_code, 200, changed.data)
        published = self.action(changed.data, 'publish')
        saved = output_reference(BusinessOutputVersion.objects.get(pk=published['id']))
        successor = self.action(published, 'successor')
        self.assertEqual(successor['parser_contract'], saved['parser'])
        self.assertEqual(set(saved['parser']['response_example']), {'schema_version', *sources.values()})
        response = self.client.post(self.url+'preview-visual-rules-output/', {'output_version_id': published['id'],
            'expected_revision': published['revision'], 'response_text': json.dumps(saved['parser']['response_example'])}, format='json')
        self.assertTrue(response.data['valid'], response.data)
        self.assertEqual(response.data['output_contract_sha256'], saved['sha256'])
        for field in sources:
            self.assertIsInstance(response.data['candidate'][field], str)
        self.assertEqual(self.client.get(f'{self.url}{original["id"]}/').data['parser_contract'], original['parser_contract'])
