import hashlib
import json
import sqlite3
from contextlib import closing
from io import StringIO
from pathlib import Path
from tempfile import TemporaryDirectory
from unittest.mock import patch
from uuid import uuid4

from django.contrib.auth import get_user_model
from django.core.management import call_command
from django.core.management.base import CommandError
from django.db import connection
from django.test import TestCase, TransactionTestCase

from studio import models as m
from studio.management import registry_configuration_cleanup as cleanup
from studio.tests.test_execution_context_inputs import PROJECT_CONFIG


class Fixture:
    def setUp(self):
        self.actor=get_user_model().objects.create_superuser(username='registry-cleanup-test')
        self.family=m.WorkflowTemplate.objects.create(slug='old-config',name='Old')
        self.old=m.WorkflowTemplateVersion.objects.create(template=self.family,version=1)
        self.binding=m.WorkflowSemanticBinding.objects.create(workflow_version=self.old,semantic_key='prompt',node_id='1',input_name='text')
        self.kept=m.WorkflowTemplateVersion.objects.create(template=self.family,version=2)
        # Historical frozen-invalid fixture, never an executable generation fact.
        m.WorkflowTemplateVersion.objects.filter(pk=self.old.pk).update(status='published')
        self.project=m.SeriesProject.objects.create(owner=self.actor,title='Keep project',**PROJECT_CONFIG)
        self.spec={'schema_version':1,'targets':[{'model':'WorkflowTemplateVersion','id':str(self.old.pk),'reason':'reviewed obsolete fixture'}]}

    def plan(self):
        return cleanup.build_plan(self.spec)

    def execute(self,plan,**overrides):
        args=dict(expected_sha256=plan.report['plan_sha256'],confirmation=cleanup.CONFIRMATION,
            backup_manifest='fixture/manifest.json',operator=self.actor.username)
        args.update(overrides)
        return cleanup.execute(self.spec,**args)


class RegistryConfigurationCleanupTests(Fixture,TestCase):
    def test_published_exact_delete_not_retire_and_keep_unvalidated_latest(self):
        plan=self.plan()
        self.assertTrue(plan.report['can_execute'])
        self.assertEqual(plan.selected[m.WorkflowTemplateVersion],{self.old.pk})
        self.assertIn(self.kept.pk,plan.preserved[m.WorkflowTemplateVersion])
        with patch.object(cleanup,'_backup_matches_plan',return_value={'verified':True}):
            result=self.execute(plan)
        self.assertFalse(m.WorkflowTemplateVersion.objects.filter(pk=self.old.pk).exists())
        self.assertFalse(m.WorkflowSemanticBinding.objects.filter(pk=self.binding.pk).exists())
        self.assertTrue(m.WorkflowTemplateVersion.objects.filter(pk=self.kept.pk).exists())
        self.assertTrue(m.SeriesProject.objects.filter(pk=self.project.pk).exists())
        self.assertFalse(result['files_deleted'])

    def test_fk_and_json_consumers_block_without_cascade_or_detach(self):
        family_spec={'schema_version':1,'targets':[{'model':'WorkflowTemplate','id':str(self.family.pk),'reason':'fixture'}]}
        self.assertTrue(cleanup.build_plan(family_spec).report['blockers'])
        m.WorkflowTemplateVersion.objects.filter(pk=self.kept.pk).update(input_contract={'source':str(self.old.pk)})
        plan=self.plan()
        self.assertTrue(any(x['id']==str(self.kept.pk) and x['reason']=='retained_json_reference' for x in plan.report['blockers']))
        with self.assertRaises(CommandError): self.execute(plan)
        self.assertTrue(m.WorkflowTemplateVersion.objects.filter(pk=self.old.pk).exists())

    def test_same_id_content_change_and_new_consumer_stale_plan(self):
        plan=self.plan()
        m.WorkflowTemplateVersion.objects.filter(pk=self.old.pk).update(input_contract={'changed':True})
        with self.assertRaisesMessage(CommandError,'范围、数据或引用变化'): self.execute(plan)
        plan=self.plan()
        m.WorkflowTemplateVersion.objects.filter(pk=self.kept.pk).update(input_contract={'source':str(self.old.pk)})
        with self.assertRaisesMessage(CommandError,'范围、数据或引用变化'): self.execute(plan)

    def test_audit_failure_rolls_back_physical_delete(self):
        plan=self.plan()
        with patch.object(cleanup,'_backup_matches_plan',return_value={}), patch.object(m.PlatformLifecycleAuditEvent.objects,'create',side_effect=RuntimeError('audit unavailable')):
            with self.assertRaisesRegex(RuntimeError,'audit unavailable'): self.execute(plan)
        self.assertTrue(m.WorkflowTemplateVersion.objects.filter(pk=self.old.pk).exists())

    def test_asset_creation_provenance_is_descriptive_with_or_without_history_marker(self):
        original={'workflow_template_version_id':str(self.old.pk)}
        asset=m.Asset.objects.create(owner=self.actor,kind='reference',name='Keep bytes',metadata=original)
        self.assertTrue(self.plan().report['can_execute'])
        original['cleared_history_origin']={'original':{'preparation_id':str(uuid4())}}
        m.Asset.objects.filter(pk=asset.pk).update(metadata=original)
        plan=self.plan()
        self.assertTrue(plan.report['can_execute'])
        self.assertEqual(len(plan.report['preserved_origin_descriptions']),1)
        m.Asset.objects.filter(pk=asset.pk).update(metadata={**original,'active_binding':str(self.old.pk)})
        self.assertTrue(self.plan().report['blockers'])
        m.Asset.objects.filter(pk=asset.pk).update(metadata=original)
        with patch.object(cleanup,'_backup_matches_plan',return_value={}): self.execute(self.plan())
        asset.refresh_from_db()
        self.assertEqual(asset.metadata,original)

    def test_empty_missing_forbidden_duplicates_and_confirmation_rejected(self):
        for spec in ({'schema_version':1,'targets':[]}, {'schema_version':1,'targets':[{'model':'Asset','id':str(uuid4()),'reason':'not allowed'}]},
                     {'schema_version':1,'targets':[{'model':'WorkflowTemplateVersion','id':str(uuid4()),'reason':'missing'}]},
                     {'schema_version':1,'targets':self.spec['targets']*2}):
            with self.subTest(spec=spec),self.assertRaises(CommandError): cleanup.build_plan(spec)
        for override in ({'confirmation':''},{'backup_manifest':''},{'operator':'missing'}):
            with self.subTest(override=override),self.assertRaises(CommandError): self.execute(self.plan(),**override)

    def test_command_dispatches_explicit_configuration_before_old_discovery(self):
        with TemporaryDirectory() as directory:
            path=Path(directory)/'targets.json'
            path.write_text(json.dumps(self.spec),encoding='utf-8')
            output=StringIO()
            call_command('purge_incompatible_workflows',registry_configuration_plan=str(path),stdout=output)
            self.assertTrue(json.loads(output.getvalue())['can_execute'])
            for extra in ({'production_run':uuid4()},{'irreversible_without_backup':True},{'execution_cutover':True}):
                with self.assertRaises(CommandError):
                    call_command('purge_incompatible_workflows',registry_configuration_plan=str(path),stdout=StringIO(),**extra)

    def test_set_null_consumer_is_not_silently_detached(self):
        provider=m.ProviderProfile.objects.create(key='cleanup-only-provider',name='Fixture',driver_type='comfyui')
        profile=m.ModelProfileVersion.objects.create(profile_key='fixture-model',version=1,provider=provider,model_identifier='fixture',modality='image')
        deployment=m.ModelDeployment.objects.create(key='fixture-deployment',name='Fixture',profile_version=profile)
        m.WorkflowTemplateVersion.objects.filter(pk=self.kept.pk).update(validated_deployment=deployment)
        spec={'schema_version':1,'targets':[{'model':'ModelDeployment','id':str(deployment.pk),'reason':'fixture'}]}
        blockers=cleanup.build_plan(spec).report['blockers']
        self.assertTrue(any(row['field']=='validated_deployment' for row in blockers))
        self.kept.refresh_from_db()
        self.assertEqual(self.kept.validated_deployment_id,deployment.pk)

    def test_retained_run_recipe_reference_blocks(self):
        recipe=m.ProductionRecipeVersion.objects.create(recipe_key='fixture',version=1,name='Old recipe')
        m.ProductionRun.objects.create(owner=self.actor,project=self.project,plan_source='recipe',recipe_version=recipe,
            status='cancelled',idempotency_key=str(uuid4()),request_sha256='a'*64)
        spec={'schema_version':1,'targets':[{'model':'ProductionRecipeVersion','id':str(recipe.pk),'reason':'fixture'}]}
        self.assertTrue(any(row['model']=='ProductionRun' for row in cleanup.build_plan(spec).report['blockers']))

    def test_descriptive_successor_reference_keeps_source_unchanged(self):
        provider=m.ProviderProfile.objects.create(key='source-provider',name='Fixture',driver_type='comfyui')
        profile=m.ModelProfileVersion.objects.create(profile_key='source-model',version=1,provider=provider,model_identifier='fixture',modality='image')
        deployment=m.ModelDeployment.objects.create(key='source-deployment',name='Fixture',profile_version=profile)
        source=m.ComfyWorkflowSource.objects.create(source_deployment=deployment,imported_by=self.actor,
            conversion_report={'successor_of':str(self.old.pk)},source_path_sha256='a'*64,source_size_bytes=2,import_kind='candidate')
        plan=self.plan()
        self.assertTrue(plan.report['can_execute'])
        with patch.object(cleanup,'_backup_matches_plan',return_value={}): self.execute(plan)
        source.refresh_from_db()
        self.assertEqual(source.conversion_report,{'successor_of':str(self.old.pk)})

    def test_host_observation_keeps_descriptive_id_but_unknown_reference_blocks(self):
        provider=m.ProviderProfile.objects.create(key='probe-provider',name='Fixture',driver_type='comfyui')
        profile=m.ModelProfileVersion.objects.create(profile_key='probe-model',version=1,provider=provider,model_identifier='fixture',modality='image')
        deployment=m.ModelDeployment.objects.create(key='probe-deployment',name='Fixture',profile_version=profile)
        host=m.RuntimeHost.objects.create(key='probe-host',name='Fixture')
        details={'comfyui':{'deployments':[{'deployment_id':str(deployment.pk),'status':'available'}]}}
        snapshot=m.HostCapabilitySnapshot.objects.create(host=host,revision=1,probe_status='succeeded',details=details)
        spec={'schema_version':1,'targets':[{'model':'ModelDeployment','id':str(deployment.pk),'reason':'fixture'}]}
        self.assertTrue(cleanup.build_plan(spec).report['can_execute'])
        m.HostCapabilitySnapshot.objects.filter(pk=snapshot.pk).update(details={**details,'unknown':str(deployment.pk)})
        self.assertTrue(cleanup.build_plan(spec).report['blockers'])


class RegistryConfigurationBackupTests(Fixture,TransactionTestCase):
    def test_real_backup_delete_and_restore_evidence(self):
        with TemporaryDirectory() as directory:
            path=Path(directory)/'database.sqlite3'
            with closing(sqlite3.connect(path)) as db: connection.connection.backup(db)
            manifest={'database':{'kind':'sqlite','included':True,'backup_relative_path':path.name},
                'files':[{'relative_path':path.name,'sha256':hashlib.sha256(path.read_bytes()).hexdigest(),'bytes':path.stat().st_size}]}
            manifest_path=Path(directory)/'manifest.json'
            manifest_path.write_text(json.dumps(manifest),encoding='utf-8')
            (Path(directory)/'manifest.sha256').write_text(hashlib.sha256(manifest_path.read_bytes()).hexdigest(),encoding='ascii')
            self.execute(self.plan(),backup_manifest=str(manifest_path))
            self.assertFalse(m.WorkflowTemplateVersion.objects.filter(pk=self.old.pk).exists())
            with closing(sqlite3.connect(path)) as db:
                self.assertEqual(db.execute('SELECT COUNT(*) FROM studio_workflowtemplateversion WHERE id=?',[self.old.pk.hex]).fetchone()[0],1)
