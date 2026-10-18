from uuid import uuid4

from django.db import connection
from django.db.models import OuterRef
from django.test import TestCase
from django.test.utils import CaptureQueriesContext
from rest_framework.test import APIClient

from studio.models import (ContentSourceVersion, DataStructureNode, DataStructureVersion,
    ExecutionInputSpec, FieldDefinition, FieldDefinitionVersion, PromptProfileFieldRule,
    PromptProfileVersion, BusinessOutputVersion)
from studio.services.registry_relations import fields, groups, row_summary, summary_queryset
from studio.tests.test_registry_relations import RegistryRelationsTests


class FieldRegistryRelationsTests(TestCase):
    page = RegistryRelationsTests.page

    @classmethod
    def setUpTestData(cls):
        RegistryRelationsTests.setUpTestData.__func__(cls)
        cls.field = FieldDefinition.objects.create(key='reference_motion', is_active=False)
        cls.field_v1 = FieldDefinitionVersion.objects.create(field_definition=cls.field, version=1,
            label='同名动作', category='test', value_type='text', status='retired')
        cls.field_v2 = FieldDefinitionVersion.objects.create(field_definition=cls.field, version=2,
            label='同名动作', category='test', value_type='text', status='published')
        cls.structure = DataStructureVersion.objects.create(name='结构 A')
        cls.other = DataStructureVersion.objects.create(name='结构 A', structure_key=cls.structure.structure_key, version=2)
        for index in range(2):
            DataStructureNode.objects.create(structure=cls.structure, node_key=f'node{index}',
                field_definition_version=cls.field_v1, position=index)
        DataStructureNode.objects.create(structure=cls.other, node_key='root', field_definition_version=cls.field_v2)
        cls.source = ContentSourceVersion.objects.create(name='来源 A', reader_selector='request.text',
            data_structure_version=cls.structure, contract_size_bytes=2, contract_sha256='f'*64)
        BusinessOutputVersion.objects.filter(pk=cls.output.pk).update(data_structure_version=cls.structure)
        PromptProfileVersion.objects.filter(pk=cls.prompt.pk).update(input_structure_version=cls.structure)
        PromptProfileFieldRule.objects.create(profile_version=cls.prompt, field_definition=cls.field,
            field_definition_version=cls.field_v1, source_version=cls.source, source_selector='source.value',
            channel='positive_prompt', position=1)
        for index in range(2):
            ExecutionInputSpec.objects.create(version=cls.execution, key=f'input{index}', position=index,
                kind='json', required=False, min_items=0, max_items=1, semantic_contract={}, data_structure_version=cls.structure)

    def test_exact_config_chain_counts_drilldown_and_links(self):
        with CaptureQueriesContext(connection) as queries:
            for kind in ('field', 'field_version', 'structure', 'source'):
                for row in self.page(kind=kind)['results']:
                    for group in row['groups']:
                        actual = self.page(kind=group['kind'], source_kind=kind, source_id=row['id'], relation=group['key'])
                        self.assertEqual(actual['count'], group['count'])
            row = self.page(kind='field', ids=str(self.field.pk))['results'][0]
            self.assertEqual(row['status'], 'inactive')
            self.assertEqual({g['key']: g['count'] for g in row['groups']}, {'versions': 2, 'structures': 2, 'prompts': 1})
            self.assertIn('field_definition='+str(self.field.pk), row['config_url'])
            version = self.page(kind='field_version', ids=str(self.field_v1.pk))['results'][0]
            self.assertIn('field_version='+str(self.field_v1.pk), version['config_url'])
            exact = self.page(kind='structure', source_kind='field_version', source_id=str(self.field_v1.pk), relation='structures')
            self.assertEqual([r['id'] for r in exact['results']], [str(self.structure.pk)])
            self.assertIn('同名动作 v1', exact['context_label'])
            row = exact['results'][0]
            self.assertEqual({g['key']: g['count'] for g in row['groups']}, {'fields': 1, 'sources': 1, 'outputs': 1, 'prompts': 1, 'executions': 1})
            self.assertIn('structure_version='+str(self.structure.pk), row['config_url'])
            source = self.page(kind='source', ids=str(self.source.pk))['results'][0]
            self.assertIn('source_version='+str(self.source.pk), source['config_url'])
            prompts = self.page(kind='prompt', source_kind='source', source_id=str(self.source.pk), relation='prompts')
            self.assertEqual([r['id'] for r in prompts['results']], [str(self.prompt.pk)])
            self.assertEqual(self.page(kind='execution', source_kind='prompt', source_id=str(self.prompt.pk), relation='executions')['count'], 1)
            self.assertEqual(self.page(kind='recipe', source_kind='execution', source_id=str(self.execution.pk), relation='recipes')['count'], 1)
        self.assertFalse(any(q['sql'].lstrip().upper().startswith(('INSERT', 'UPDATE', 'DELETE')) for q in queries))

    def test_pagination_search_absent_source_and_nonstaff(self):
        first = self.page(kind='field_version', search='reference_motion', page_size=1)
        self.assertEqual((first['count'], len(first['results'])), (2, 1))
        second = self.page(kind='field_version', search='reference_motion', page_size=1, page=2)
        self.assertNotEqual(first['results'][0]['id'], second['results'][0]['id'])
        self.assertEqual(self.page(kind='structure', ids=str(uuid4()))['count'], 0)
        client = APIClient(); client.force_authenticate(self.user)
        self.assertEqual(client.get('/api/v1/registry-relations/', {'kind':'structure', 'source_kind':'field_version', 'source_id':str(uuid4()), 'relation':'structures'}).status_code, 404)
        self.user.is_staff = False; self.user.save(update_fields=['is_staff'])
        self.assertEqual(client.get('/api/v1/registry-relations/', {'kind':'field'}).status_code, 403)

    def test_summary_uses_narrow_indexed_queries_without_per_row_fetches(self):
        for kind in ('field', 'field_version', 'structure', 'source'):
            query = summary_queryset(kind).values(*fields(kind), *['relation_'+g.key for g in groups(kind, OuterRef)])
            with CaptureQueriesContext(connection) as queries:
                result = [row_summary(kind, row) for row in query]
            self.assertTrue(result)
            self.assertEqual(len(queries), 1)
            for name in ('reader_contract', 'description', 'definition', 'default_value'):
                self.assertNotIn('"'+name+'"', queries[0]['sql'])
            self.assertIn('INDEX', query.explain().upper())
