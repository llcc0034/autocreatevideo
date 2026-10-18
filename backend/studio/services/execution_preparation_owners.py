"""One ownership and lock order for preparation creation and field changes."""
from contextlib import contextmanager

from django.core.exceptions import PermissionDenied, ValidationError
from django.db import transaction
from django.db.models import Q

from studio.models import (
    AssetGenerationJob, ExecutionPreparation, ModelDeployment, ModelDeploymentSmokeAttempt, ProductionRun, StageRun, WorkflowSmokeAttempt, WorkflowTemplateVersion,
)


PREPARATION_OWNER_MODELS = {
    "asset_job_id": AssetGenerationJob,
    "deployment_smoke_id": ModelDeploymentSmokeAttempt,
    "stage_run_id": StageRun,
    "workflow_smoke_id": WorkflowSmokeAttempt,
}


def visible_execution_preparations(actor):
    """Read visibility follows the same real root owners as locked mutations."""
    query = ExecutionPreparation.objects.all()
    if not actor or not actor.is_authenticated or not actor.is_active:
        return query.none()
    if actor.is_staff:
        return query
    return query.filter(Q(asset_job__owner_id=actor.pk) | Q(stage_run__production_run__owner_id=actor.pk))


def preparation_owner_ref(preparation):
    refs = [(key, getattr(preparation, key)) for key in PREPARATION_OWNER_MODELS if getattr(preparation, key)]
    if len(refs) != 1:
        raise ValidationError("执行准备必须且只能归属一个正式任务")
    return refs[0]


def nonproduction_preparation_root(preparation):
    """The original asset/Smoke root; never synthesize an asset parent."""
    key, _ = preparation_owner_ref(preparation)
    if key == "stage_run_id":
        raise ValidationError("生产阶段必须使用自身Run预算与调用记录")
    return getattr(preparation, key.removesuffix("_id"))


def preparation_actor(preparation):
    if preparation.asset_job_id:
        return preparation.asset_job.owner
    return preparation.created_by


def _authorize_owner(actor, key, owner):
    if not actor or not actor.is_authenticated or not actor.is_active:
        raise PermissionDenied("需要有效登录身份")
    if actor.is_staff:
        return
    if key == "asset_job_id":
        owner_id = owner.owner_id
    elif key == "stage_run_id":
        owner_id = owner.production_run.owner_id
    else:
        raise PermissionDenied("验收字段仅允许管理员操作")
    if owner_id != actor.pk:
        raise PermissionDenied("无权操作此执行准备")


def _with_auxiliary_root_owners(refs):
    """Include the real budget/cancellation root before taking any child lock."""
    expanded = set(refs)
    asset_ids = {pk for key, pk in refs if key == "asset_job_id"}
    parents = list(AssetGenerationJob.objects.filter(pk__in=asset_ids, parent_preparation__isnull=False)
        .values_list("parent_preparation_id", flat=True))
    parent_rows = list(ExecutionPreparation.objects.filter(pk__in=parents).only(
        "asset_job_id", "deployment_smoke_id", "workflow_smoke_id", "stage_run_id"))
    if AssetGenerationJob.objects.filter(pk__in=[row.asset_job_id for row in parent_rows if row.asset_job_id],
            parent_preparation__isnull=False).exists():
        raise ValidationError("辅助叶子不能递归拥有子准备")
    for parent in parent_rows:
        if parent.stage_run_id:
            raise ValidationError("素材字段子任务不能归属生产阶段")
        ref = preparation_owner_ref(parent)
        expanded.add((ref[0], str(ref[1])))
    return expanded


@contextmanager
def locked_preparation_owners(refs, actor):
    """Authorize the real roots, including a field child's budget root."""
    if not actor or not actor.is_authenticated or not actor.is_active:
        raise PermissionDenied("需要有效登录身份")
    with locked_preparation_owner_rows(refs) as owners:
        for (key, _), owner in owners.items():
            _authorize_owner(actor, key, owner)
        yield owners


@contextmanager
def locked_preparation_owner_rows(refs):
    """Registry guard (where needed) -> Run -> owner -> Preparation -> fields.

    Re-read identities rather than trusting caller-related object caches. All
    callers must acquire this guard before locking their preparation rows.
    This internal lock primitive is not an authorization check. User actions
    use the wrapper above; workers must verify their original claim inside
    this guard. Settlement is still recorded if the user was disabled after
    submission; that is not permission to start another external call.
    """
    refs = {(key, str(pk)) for key, pk in refs}
    if not refs or any(key not in PREPARATION_OWNER_MODELS for key, _ in refs):
        raise ValidationError("需要明确的正式准备任务来源")
    with transaction.atomic():
        refs = _with_auxiliary_root_owners(refs)
        owners = {}
        for key, model in PREPARATION_OWNER_MODELS.items():
            ids = {pk for kind, pk in refs if kind == key}
            query = model.objects.filter(pk__in=ids)
            if key == "stage_run_id":
                query = query.select_related("production_run")
            rows = list(query)
            if len(rows) != len(ids):
                raise model.DoesNotExist("准备所属任务不存在")
            for row in rows:
                owners[(key, str(row.pk))] = row
        # Original Smoke allocation/completion already locks Deployment first.
        # Include it for Smoke children too, before any child/root row lock.
        smoke_deployments = {row.deployment_id for (key, _), row in owners.items() if key in {"deployment_smoke_id", "workflow_smoke_id"}}
        list(ModelDeployment.objects.select_for_update().filter(pk__in=smoke_deployments).order_by("id"))
        workflow_ids = {row.workflow_version_id for (key, _), row in owners.items() if key == "workflow_smoke_id"}
        list(WorkflowTemplateVersion.objects.select_for_update().filter(pk__in=workflow_ids).order_by("id"))
        run_ids = {row.production_run_id for (key, _), row in owners.items() if key == "stage_run_id"}
        list(ProductionRun.objects.select_for_update().filter(pk__in=run_ids).order_by("id"))
        for key, model in PREPARATION_OWNER_MODELS.items():
            ids = {pk for kind, pk in refs if kind == key}
            rows = list(model.objects.select_for_update().filter(pk__in=ids).order_by("id"))
            if len(rows) != len(ids):
                raise model.DoesNotExist("准备所属任务已删除")
            for row in rows:
                if key == "stage_run_id" and row.production_run_id != owners[(key, str(row.pk))].production_run_id:
                    raise ValidationError("任务所属运行已变化，请重新读取", code="execution_preparation_conflict")
                if key == "asset_job_id" and row.parent_preparation_id != owners[(key, str(row.pk))].parent_preparation_id:
                    raise ValidationError("子任务所属准备已变化，请重新读取", code="execution_preparation_conflict")
                owners[(key, str(row.pk))] = row
        yield owners
