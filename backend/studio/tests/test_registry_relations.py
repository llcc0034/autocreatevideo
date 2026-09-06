from uuid import uuid4
from unittest.mock import patch
from django.contrib.auth import get_user_model
from django.db import connection
from django.db.models import OuterRef
from django.test import TestCase
from django.test.utils import CaptureQueriesContext
from rest_framework.test import APIClient
from studio.models import ProductionRecipeVersion, PromptProfileBinding, WorkflowTemplateVersion, ModelProfileVersion
from studio.tests import test_workflow_configuration_links as fixtures
from studio.services.registry_relations import MODELS, fields, groups, row_summary, summary_queryset


class RegistryRelationsTests(TestCase):
    @classmethod
    def setUpTestData(cls):
        fixtures.WorkflowConfigurationLinksTests.setUpTestData.__func__(cls)
        cls.binding = PromptProfileBinding.objects.get(key='links-binding-0')
        cls.recipe = ProductionRecipeVersion.objects.create(
            recipe_key='relations-recipe', name='显式配方', version=1,
            definition={'stages':[{'routing':{'routes':[{'model_deployment_id':str(cls.deployment.id), 'workflow_version_id':str(cls.workflow.id)}]},
                                   'prompt_profile_binding_key':cls.binding.key}],
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

    def test_candidates_are_not_validated_instances_and_prompt_families_are_not_exact(self):
        row = self.page(kind='deployment', ids=str(self.deployment.id))['results'][0]
        counts = {g['key']:g['count'] for g in row['groups']}
        self.assertEqual(counts['validated'], 1)
        self.assertEqual(counts['candidates'], 0)
        self.assertEqual(counts['bindings'], 6)
        exact = self.page(kind='model',source_kind='binding',source_id=str(self.binding.id),relation='models')
        self.assertEqual([str(r['id']) for r in exact['results']], [str(self.model.id)])
        self.assertNotIn('ready', row)

    def test_labels_are_short_business_names_without_runtime_disclaimers(self):
        labels = {group.label for kind in MODELS for group in groups(kind, OuterRef)}
        self.assertTrue({"提示词绑定", "适用模型", "引用配方", "校验工作流"}.issubset(labels))
        self.assertFalse(any("非运行" in label or "非有效证据" in label for label in labels))

    def test_recipe_reference_is_exact_key_based_and_bidirectional(self):
        row = self.page(kind='recipe', ids=str(self.recipe.id))['results'][0]
        self.assertEqual({g['key']:g['count'] for g in row['groups']}, dict(model=0,deployment=1,workflow=1,binding=1))
        self.assertEqual(self.page(kind='recipe',source_kind='model',source_id=str(self.other_model.id),relation='recipes')['count'],0)
        self.assertEqual(self.page(kind='recipe',source_kind='workflow',source_id=str(self.workflow.id),relation='recipes')['count'],1)
        ProductionRecipeVersion.objects.filter(pk=self.recipe.id).update(definition={'default_workflow_version_id':[str(self.workflow.id),str(self.workflow.id)]})
        row = self.page(kind='recipe', ids=str(self.recipe.id))['results'][0]
        self.assertEqual(next(g['count'] for g in row['groups'] if g['key']=='workflow'),1)

    def test_missing_context_invalid_filters_and_permission_do_not_fall_back(self):
        client=APIClient(); client.force_authenticate(self.user)
        for query,status in [({'kind':'workflow','source_kind':'model','source_id':str(uuid4()),'relation':'workflows'},404),
                             ({'kind':'model','source_id':str(self.model.id)},400),({'kind':'bad'},400),
                             ({'kind':'model','ids':'bad'},400),({'kind':'model','page_size':51},400),
                             ({'kind':'model','ids':','.join([str(uuid4())]*51)},400)]:
            response=client.get('/api/v1/registry-relations/',query)
            self.assertEqual(response.status_code,status,response.data)
        self.assertEqual(self.page(kind='model',ids=str(uuid4()))['results'],[])
        client.force_authenticate(get_user_model().objects.create_user(username='relations-nonstaff'))
        self.assertEqual(client.get('/api/v1/registry-relations/',{'kind':'model'}).status_code,403)
        client.force_authenticate(self.user)
        self.assertEqual(client.post('/api/v1/registry-relations/',{},format='json').status_code,405)

    def test_missing_model_does_not_claim_all_family_bindings(self):
        WorkflowTemplateVersion.objects.filter(pk=self.workflow.id).update(model_profile_version=None)
        row=self.page(kind='workflow',ids=str(self.workflow.id))['results'][0]
        self.assertEqual(next(g['count'] for g in row['groups'] if g['key']=='bindings'),0)

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
