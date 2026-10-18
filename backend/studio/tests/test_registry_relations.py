from uuid import uuid4
from unittest.mock import patch
from django.contrib.auth import get_user_model
from django.db import connection
from django.db.models import OuterRef
from django.test import TestCase
from django.test.utils import CaptureQueriesContext
from django.utils import timezone
from rest_framework.test import APIClient
from studio.models import (BusinessOutputVersion, ProductionRecipeVersion, WorkflowTemplateVersion, ModelProfileVersion,
    ModelExecutionInstance, ModelExecutionInstanceVersion, ExecutionAuxiliaryBinding)
from studio.tests import test_workflow_configuration_links as fixtures
from studio.services.registry_relations import MODELS, fields, groups, row_summary, summary_queryset


class RegistryRelationsTests(TestCase):
    @classmethod
    def setUpTestData(cls):
        fixtures.WorkflowConfigurationLinksTests._resource_data.__func__(cls)
        instance = ModelExecutionInstance.objects.create(key='exact-execution', name='精确执行实例')
        cls.execution = ModelExecutionInstanceVersion.objects.create(instance=instance, version=1,
            deployment=cls.deployment, workflow_version=cls.workflow, prompt_mode='profile',
            prompt_profile_version=cls.prompt, operation='image.edit')
        cls.leaf = ModelExecutionInstanceVersion.objects.create(instance=instance, version=2,
            deployment=cls.deployment, prompt_mode='none', operation='prompt.fields.generate')
        cls.output = BusinessOutputVersion.objects.create(name='精确业务输出', version=1,
            parser_key='visual_rules.v2', status='published', contract_sha256='b'*64, contract_size_bytes=2)
        cls.output_next = BusinessOutputVersion.objects.create(name='精确业务输出', version=2,
            output_key=cls.output.output_key, parser_key='visual_rules.v2', contract_sha256='c'*64, contract_size_bytes=2)
        ModelExecutionInstanceVersion.objects.filter(pk=cls.execution.pk).update(output_version=cls.output)
        for purpose in ('generate_fields', 'translate_fields'):
            ExecutionAuxiliaryBinding.objects.create(version=cls.execution, target_version=cls.leaf, purpose=purpose)
        cls.route = {'execution_instance_version_id':str(cls.execution.pk), 'expected_definition_sha256':'a'*64,
            'input_sources':{}, 'parameter_policies':[]}
        cls.recipe = ProductionRecipeVersion.objects.create(
            recipe_key='relations-recipe', name='显式配方', version=1,
            definition={'schema_version':3, 'stages':[{'routing':{'routes':[cls.route, cls.route]}}],
                        'description':str(cls.other_model.id)},
        )

    def page(self, **query):
        client = APIClient(); client.force_authenticate(self.user)
        response = client.get('/api/v1/registry-relations/', query)
        self.assertEqual(response.status_code, 200, response.data)
        return response.data

    def test_counts_and_drill_down_use_identical_predicates_for_every_kind(self):
        for kind in MODELS:
            page = self.page(kind=kind, page_size=50)
            for row in page['results']:
                for group in row['groups']:
                    detail = self.page(kind=group['kind'], source_kind=kind, source_id=row['id'], relation=group['key'], page_size=50)
                    self.assertEqual(detail['count'], group['count'], (kind, row['label'], group))

    def test_resource_relations_use_exact_execution_foreign_keys_not_model_family(self):
        row = self.page(kind='deployment', ids=str(self.deployment.id))['results'][0]
        counts = {g['key']:g['count'] for g in row['groups']}
        self.assertEqual(counts['validated'], 1)
        self.assertEqual(counts['candidates'], 0)
        self.assertEqual(counts['executions'], 2)
        exact = self.page(kind='execution', source_kind='prompt',source_id=str(self.prompt.id),relation='executions')
        self.assertEqual([str(r['id']) for r in exact['results']], [str(self.execution.pk)])
        self.assertEqual(self.page(kind='execution', source_kind='model', source_id=str(self.other_model.pk), relation='executions')['count'], 0)
        self.assertNotIn('ready', row)

    def test_labels_are_short_business_names_without_runtime_disclaimers(self):
        labels = {group.label for kind in MODELS for group in groups(kind, OuterRef)}
        self.assertTrue({"执行实例版本", "辅助执行实例", "模型部署", "引用配方", "校验工作流"}.issubset(labels))
        self.assertNotIn('提示词绑定', labels)
        self.assertFalse(any("非运行" in label or "非有效证据" in label for label in labels))

    def test_recipe_reference_is_exact_key_based_and_bidirectional(self):
        row = self.page(kind='recipe', ids=str(self.recipe.id))['results'][0]
        self.assertEqual({g['key']:g['count'] for g in row['groups']}, dict(executions=1))
        self.assertEqual(self.page(kind='recipe',source_kind='execution',source_id=str(self.execution.id),relation='recipes')['count'],1)
        self.assertEqual(self.page(kind='recipe',source_kind='execution',source_id=str(self.leaf.id),relation='recipes')['count'],0)
        ProductionRecipeVersion.objects.filter(pk=self.recipe.id).update(definition={'schema_version':3,
            'stages':[{'routing':{'routes':[{**self.route, 'execution_instance_version_id':str(self.leaf.pk),
                'input_sources':{'literal':{'value':{'execution_instance_version_id':str(self.execution.pk)}}}}]}}]})
        row = self.page(kind='recipe', ids=str(self.recipe.id))['results'][0]
        self.assertEqual(next(g['count'] for g in row['groups'] if g['key']=='executions'),1)
        self.assertEqual(self.page(kind='recipe', source_kind='execution', source_id=str(self.execution.pk), relation='recipes')['count'], 0)

    def test_missing_context_invalid_filters_and_permission_do_not_fall_back(self):
        client=APIClient(); client.force_authenticate(self.user)
        for query,status in [({'kind':'workflow','source_kind':'model','source_id':str(uuid4()),'relation':'workflows'},404),
                             ({'kind':'model','source_id':str(self.model.id)},400),({'kind':'bad'},400),
                             ({'kind':'binding'},400),
                             ({'kind':'model','ids':'bad'},400),({'kind':'model','page_size':51},400),
                             ({'kind':'model','ids':','.join([str(uuid4())]*51)},400)]:
            response=client.get('/api/v1/registry-relations/',query)
            self.assertEqual(response.status_code,status,response.data)
        self.assertEqual(self.page(kind='model',ids=str(uuid4()))['results'],[])
        client.force_authenticate(get_user_model().objects.create_user(username='relations-nonstaff'))
        self.assertEqual(client.get('/api/v1/registry-relations/',{'kind':'model'}).status_code,403)
        client.force_authenticate(self.user)
        self.assertEqual(client.post('/api/v1/registry-relations/',{},format='json').status_code,405)

    def test_execution_links_remain_explicit_even_if_workflow_model_is_missing(self):
        WorkflowTemplateVersion.objects.filter(pk=self.workflow.id).update(model_profile_version=None)
        row=self.page(kind='workflow',ids=str(self.workflow.id))['results'][0]
        self.assertEqual(next(g['count'] for g in row['groups'] if g['key']=='model'),0)
        self.assertEqual(next(g['count'] for g in row['groups'] if g['key']=='executions'),1)

    def test_auxiliary_reverse_links_are_unique_and_navigation_is_exact(self):
        row=self.page(kind='execution', ids=str(self.execution.pk))['results'][0]
        self.assertEqual({group['key']:group['count'] for group in row['groups']},
            dict(deployment=1, workflow=1, prompt=1, output=1, structures=0, auxiliaries=1, consumers=0, recipes=1))
        self.assertIn('execution_version='+str(self.execution.pk), row['config_url'])
        self.assertEqual(row['acceptance_url'], '')  # Draft is not a Smoke target.
        self.assertEqual(self.page(kind='execution', source_kind='execution', source_id=str(self.leaf.pk), relation='consumers')['count'], 1)
        self.assertEqual(self.page(kind='execution', search='精确执行实例')['count'], 2)

    def test_execution_summary_does_not_read_configuration_or_add_per_row_queries(self):
        query=summary_queryset('execution').values(*fields('execution'), *['relation_'+g.key for g in groups('execution', OuterRef)])
        with CaptureQueriesContext(connection) as queries:
            result=[row_summary('execution', row) for row in query]
        self.assertEqual(len(queries), 1)
        self.assertEqual(len(result), 2)
        for field in ('"definition" AS', '"semantic_contract"', '"driver_config"', '"runtime_config"'):
            self.assertNotIn(field, queries[0]['sql'])
        self.assertIn('INDEX', query.explain().upper())

    def test_list_query_cost_is_constant_and_does_not_select_graph_or_definitions(self):
        def read():
            with CaptureQueriesContext(connection) as context:
                rows=list(summary_queryset('model').values(*fields('model'),*['relation_'+g.key for g in groups('model',OuterRef)]))
                [row_summary('model',row) for row in rows]
            return context
        first=read()
        for index in range(10):
            ModelProfileVersion.objects.create(profile_key='relations-batch',version=index+1,provider=self.provider,
                                              model_identifier='batch',modality='image',specification={'model_family':'links-family'})
        second=read()
        self.assertEqual(len(first),1); self.assertEqual(len(second),1)
        self.assertNotIn('"graph"',second[0]['sql'])
        self.assertNotIn('"specification" FROM',second[0]['sql'])

    def test_pagination_includes_historical_records_without_external_execution(self):
        with patch('studio.services.provider_registry.driver_for',side_effect=AssertionError('must not invoke driver')):
            page=self.page(kind='projector',page_size=1,search='links.projector')
        self.assertEqual(page['count'],2)
        self.assertEqual(len(page['results']),1)
        self.assertIsNotNone(page['next'])
        self.assertNotIn('definition',page['results'][0])

    def test_output_relations_are_exact_bidirectional_and_preserve_retired_references(self):
        with patch('studio.services.provider_registry.driver_for', side_effect=AssertionError('read only')):
            page = self.page(kind='output', search='visual_rules.v2', page_size=1)
            self.assertEqual(page['count'], 2)
            self.assertIsNotNone(page['next'])
            row = self.page(kind='output', ids=str(self.output.id))['results'][0]
            self.assertEqual(row['capabilities'], ['visual_rules.v2'])
            self.assertEqual(row['groups'][0]['count'], 1)
            self.assertEqual(row['acceptance_url'], '')
            self.assertIn('output_version='+str(self.output.id), row['config_url'])
            forward = self.page(kind='output', source_kind='execution', source_id=str(self.execution.id), relation='output')
            self.assertEqual([item['id'] for item in forward['results']], [str(self.output.id)])
            self.assertEqual(self.page(kind='output', source_kind='execution', source_id=str(self.leaf.id), relation='output')['count'], 0)
            self.assertEqual(self.page(kind='execution', source_kind='output', source_id=str(self.output_next.id), relation='executions')['count'], 0)
            BusinessOutputVersion.objects.filter(pk=self.output.id).update(status='retired')
            ModelExecutionInstanceVersion.objects.filter(pk=self.execution.id).update(
                status='retired', published_at=timezone.now(), published_by=self.user)
            ModelExecutionInstance.objects.filter(pk=self.execution.instance_id).update(is_active=False)
            reverse = self.page(kind='execution', source_kind='output', source_id=str(self.output.id), relation='executions')
            self.assertEqual([item['id'] for item in reverse['results']], [str(self.execution.id)])
            self.assertIn('精确业务输出 v1', reverse['context_label'])
            retired = self.page(kind='output', ids=str(self.output.id))['results'][0]
            self.assertEqual(retired['status'], 'retired')
            self.assertEqual(retired['groups'][0]['count'], 1)
            self.assertTrue(any('不是成功调用' in item for item in retired['notices']))
            self.output.refresh_from_db()
            self.assertEqual(self.output.contract_sha256, 'b'*64)

    def test_output_summary_is_narrow_and_uses_existing_foreign_key_index(self):
        query = summary_queryset('output').values(*fields('output'), 'relation_executions', 'relation_structure')
        with CaptureQueriesContext(connection) as queries:
            result = [row_summary('output', row) for row in query]
        self.assertEqual(len(queries), 1)
        self.assertEqual(len(result), 2)
        for name in ('quality_advisories', 'contract_sha256', 'definition', 'description'):
            self.assertNotIn('"'+name+'"', queries[0]['sql'])
        self.assertIn('output_version_id', queries[0]['sql'])
        self.assertIn('INDEX', query.explain().upper())
