"""Explicit one-off SQLite configuration repair, not a daily deletion API.

The operator supplies reviewed UUIDs and reasons. Neither validation failures
nor version numbers automatically select data. Kept references only block;
they never expand deletion into business history, assets or shared resources.
"""
from collections import defaultdict
from copy import deepcopy
from types import SimpleNamespace
from uuid import UUID

from django.apps import apps
from django.contrib.auth import get_user_model
from django.core.management.base import CommandError
from django.db import connection
from django.db.models import F

from studio import models as m
from studio.management.commands.purge_incompatible_workflows import (
    _hard_delete_plan, _incoming_fields, _payload_references, _semantic_reference_hits, _selected_uuid_strings,
)
from studio.management.execution_cutover import cutover_operator
from studio.management.execution_cutover_inventory import _digest
from studio.management.production_run_cleanup import _backup_matches_plan, _facts
from studio.services.registry_reference_lock import registry_reference_write_guard

CONFIRMATION = 'DELETE_REVIEWED_OBSOLETE_CONFIGURATION_KEEP_CONTENT'
ROOTS = frozenset('''ModelProfileVersion ModelDeployment ModelArtifact WorkflowProjector
WorkflowProjectorVersion WorkflowTemplate WorkflowTemplateVersion ModelExecutionInstance
ModelExecutionInstanceVersion PromptProfileVersion ProductionRecipeVersion BusinessOutputVersion
ContentSourceVersion DataStructureVersion FieldDefinition FieldDefinitionVersion
ComfyWorkflowSource ComfyWorkflowContractDraft'''.split())
OWNED = (
    ('ExecutionInputSpec', 'version', 'ModelExecutionInstanceVersion'),
    ('ExecutionInputTarget', 'input_spec', 'ExecutionInputSpec'),
    ('ExecutionParameterSpec', 'version', 'ModelExecutionInstanceVersion'),
    ('ExecutionAuxiliaryBinding', 'version', 'ModelExecutionInstanceVersion'),
    ('ExecutionVersionContract', 'version', 'ModelExecutionInstanceVersion'),
    ('PromptProfileFieldRule', 'profile_version', 'PromptProfileVersion'),
    ('DataStructureNode', 'structure', 'DataStructureVersion'),
    ('WorkflowSemanticBinding', 'workflow_version', 'WorkflowTemplateVersion'),
    ('WorkflowModelRequirement', 'workflow_version', 'WorkflowTemplateVersion'),
    ('ModelArtifact', 'profile_version', 'ModelProfileVersion'),
    ('RuntimeModelAvailability', 'deployment', 'ModelDeployment'),
)
PRESERVE = frozenset('''Asset AssetReferenceSeries AssetReferenceVersion SeriesProject Episode
Shot ProductionRun ProductionRunPlan ScriptSource ScriptRevision CharacterBible SceneBible PropBible'''.split())


def build_plan(spec):
    if connection.vendor != 'sqlite':
        raise CommandError('本次明确配置修复仅支持已核对的SQLite')
    if not isinstance(spec, dict) or set(spec) != {'schema_version', 'targets'} or spec['schema_version'] != 1:
        raise CommandError('必须提供schema_version=1及明确targets')
    if not isinstance(spec['targets'], list) or not spec['targets']:
        raise CommandError('目标清单不得为空；不允许默认全库删除')
    selected, targets = defaultdict(set), []
    for entry in spec['targets']:
        if not isinstance(entry, dict) or set(entry) != {'model', 'id', 'reason'} or entry['model'] not in ROOTS:
            raise CommandError('目标只能是明确支持的配置类型，不能包含业务、素材、账号或凭据')
        if not isinstance(entry['reason'], str) or not entry['reason'].strip() or len(entry['reason']) > 1000:
            raise CommandError('每项必须填写本次已核对的删除原因')
        try: identifier = UUID(str(entry['id']))
        except (ValueError, TypeError) as exc: raise CommandError('目标必须为精确UUID') from exc
        model = apps.get_model('studio', entry['model'])
        if identifier in selected[model] or not model._base_manager.filter(pk=identifier).exists():
            raise CommandError('目标重复或已不存在，须重新盘点，不重发删除')
        selected[model].add(identifier)
        targets.append({'model': entry['model'], 'id': str(identifier), 'reason': entry['reason']})
    changed = True
    while changed:
        changed = False
        for child_name, field, parent_name in OWNED:
            child, parent = apps.get_model('studio', child_name), apps.get_model('studio', parent_name)
            ids = set(child._base_manager.filter(**{f'{field}_id__in': selected.get(parent, ())}).values_list('pk', flat=True))
            if ids - selected.get(child, set()):
                selected[child].update(ids); changed = True
    selected = {model: ids for model, ids in selected.items() if ids}
    blockers, provenance = [], []
    for parent, ids in selected.items():
        # All relations, including SET_NULL, are protected. No silent detachment.
        for child, field in _incoming_fields(parent):
            rows = child._base_manager.filter(**{f'{field.attname}__in': ids}).exclude(pk__in=selected.get(child, ()))
            blockers.extend({'model':child.__name__, 'id':str(pk), 'field':field.name, 'reason':'retained_foreign_key'}
                for pk in rows.values_list('pk',flat=True))
    selected_uuids = _selected_uuid_strings(selected)
    for hit in _semantic_reference_hits(selected_uuids, all_fields=True):
        if hit['id'] in selected.get(hit['model'], ()):
            continue
        # Operation audit is historical description, not an executable binding.
        # Preserve it unchanged; never cascade audit into configuration cleanup.
        if hit['model'] is m.PlatformLifecycleAuditEvent:
            continue
        if hit['model'] is m.ComfyWorkflowSource and hit['field'] == 'conversion_report':
            value = m.ComfyWorkflowSource.objects.get(pk=hit['id']).conversion_report
            if isinstance(value, dict) and isinstance(value.get('successor_of'), str):
                remainder = {k:v for k,v in value.items() if k != 'successor_of'}
                if not _payload_references(remainder, selected_uuids):
                    provenance.append({'asset_id':str(hit['id']), 'field':'ComfyWorkflowSource.conversion_report.successor_of'})
                    continue
        if hit['model'] is m.HostCapabilitySnapshot and hit['field'] == 'details':
            value = deepcopy(m.HostCapabilitySnapshot.objects.get(pk=hit['id']).details)
            # Probe's immutable inventory description, not the executable
            # deployment registry. Retain the observation exactly as recorded.
            rows = value.get('comfyui', {}).get('deployments', []) if isinstance(value, dict) else []
            removed = False
            deployment_ids = {str(pk) for pk in selected.get(m.ModelDeployment, ())}
            for row in rows if isinstance(rows, list) else []:
                if isinstance(row, dict) and row.get('deployment_id') in deployment_ids:
                    row.pop('deployment_id'); removed = True
            if removed and not _payload_references(value, selected_uuids):
                provenance.append({'asset_id':str(hit['id']), 'field':'HostCapabilitySnapshot.details.comfyui.deployments.deployment_id'})
                continue
        if hit['model'] is m.Asset and hit['field'] == 'metadata':
            metadata = m.Asset.objects.get(pk=hit['id']).metadata
            # Asset creation provenance describes the original generator; it
            # is not a dependency for reusing the file. Only the known origin
            # fields are descriptive; unknown/active references block. Never edit
            # the original metadata or rewrite which version generated a file.
            if isinstance(metadata, dict):
                historical_keys = {'cleared_history_origin', 'execution_instance_version_id',
                    'model_deployment_id', 'workflow_template_version_id'}
                for key in sorted(historical_keys & metadata.keys()):
                    if _payload_references(metadata[key], selected_uuids):
                        provenance.append({'asset_id':str(hit['id']), 'field':'metadata.'+key})
                if not _payload_references({k:v for k,v in metadata.items() if k not in historical_keys}, selected_uuids):
                    continue
        blockers.append({'model':hit['model'].__name__, 'id':str(hit['id']), 'field':hit['field'], 'reason':'retained_json_reference'})
    if any(model in selected for model in (m.ModelProfileVersion,m.ModelDeployment,m.ModelArtifact)):
        blockers.extend({'model':'RuntimeHostProbeAttempt','id':str(pk),'field':'status','reason':'model_inventory_probe_in_progress'}
            for pk in m.RuntimeHostProbeAttempt.objects.exclude(status__in=('succeeded','failed','cancelled')).values_list('pk',flat=True))
    models = {model.__name__:model for model in apps.get_app_config('studio').get_models()}
    # Whole retained configuration and business tables are hashed; secrets are
    # not read into reports, and background host/probe liveness is not a gate.
    preserved = {models[name]:set(models[name]._base_manager.exclude(pk__in=selected.get(models[name], ())).values_list('pk',flat=True))
        for name in ROOTS | PRESERVE | {name for name,_,_ in OWNED}}
    kept_facts = {model._meta.label:_facts(model, ids) for model,ids in preserved.items()}
    report = {'schema_version':1, 'targets':sorted(targets,key=lambda row:(row['model'],row['id'])),
        'rows':{model._meta.label:_facts(model,ids) for model,ids in sorted(selected.items(),key=lambda item:item[0]._meta.label)},
        'preserved':kept_facts, 'blockers':sorted(blockers,key=lambda row:(row['model'],row['id'],row['field'])),
        'preserved_origin_descriptions':sorted(provenance,key=lambda row:(row['asset_id'],row['field'])),
        'confirmation':CONFIRMATION, 'files_deleted':False}
    report['counts']={key:len(rows) for key,rows in report['rows'].items()}
    report['can_execute']=not blockers
    report['plan_sha256']=_digest(report)
    return SimpleNamespace(selected=selected,preserved=preserved,nullifications={},report=report)


def execute(spec, *, expected_sha256, confirmation, backup_manifest, operator):
    if confirmation != CONFIRMATION or not backup_manifest or len(expected_sha256) != 64:
        raise CommandError('必须明确确认、备份及本次预览SHA')
    actor=cutover_operator(operator)
    with registry_reference_write_guard():
        if connection.vendor != 'sqlite':
            raise CommandError('当前修复仅支持SQLite')
        # Same cross-process exclusion used by formal business-history cleanup.
        get_user_model().objects.filter(pk=actor.pk).update(last_login=F('last_login'))
        plan=build_plan(spec)
        if plan.report['plan_sha256'] != expected_sha256:
            raise CommandError('范围、数据或引用变化，未删除；请重新预览')
        if plan.report['blockers']:
            raise CommandError('保留对象仍引用清理目标，未删除；不能扩大级联范围')
        backup=_backup_matches_plan(plan,backup_manifest)
        deleted=_hard_delete_plan(plan)
        for model,ids in plan.preserved.items():
            if _facts(model,ids) != plan.report['preserved'][model._meta.label]:
                raise CommandError('保留数据变化，回滚本次删除')
        first=spec['targets'][0]
        before={'sha256':expected_sha256,'status':'reviewed'}
        after={'status':'deleted','sha256':_digest(deleted),'contract_sha256':backup.get('database_sha256','')}
        m.PlatformLifecycleAuditEvent.objects.create(actor=actor,action='registry_configuration_cleanup',
            object_type='maintenance_configuration',object_id=first['id'],reason_code='reviewed.obsolete_configuration',
            before_sha256=_digest(before),after_sha256=_digest(after),before_summary=before,after_summary=after)
        return {'mode':'registry_configuration_cleanup_completed','plan_sha256':expected_sha256,
            'deleted_counts':deleted,'files_deleted':False,'backup':backup}
