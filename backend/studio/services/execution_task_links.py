"""Validate real task/preparation identities, without resolving current routes."""
from django.core.exceptions import ObjectDoesNotExist, ValidationError

from studio.models import (AssetGenerationJob, ExecutionPreparation, GenerationAttempt, ModelExecutionInstanceVersion,
    ModelDeploymentSmokeAttempt, WorkflowSmokeAttempt)
from studio.services.execution_prompts import AUXILIARY_OPERATIONS


def validate_execution_task_links(task):
    if isinstance(task, GenerationAttempt):
        from studio.services.execution_production_attempts import validate_retry_link
        validate_retry_link(task)
    """Cross-table checks supplement required FKs and local DB constraints.

    Call under the original root/preparation locks before allocation or dispatch.
    These are identity checks, not publication, Smoke, budget or I/O admission.
    """
    if not isinstance(task, (AssetGenerationJob, GenerationAttempt)) or not task.execution_instance_version_id:
        raise ValidationError("AI任务必须引用精确执行实例版本")
    try:
        version = ModelExecutionInstanceVersion.objects.only("deployment_id", "workflow_version_id", "prompt_profile_version_id", "operation").get(
            pk=task.execution_instance_version_id)
    except ObjectDoesNotExist as exc:
        raise ValidationError("AI任务引用的执行实例版本不存在") from exc
    if task.model_deployment_id != version.deployment_id or task.operation != version.operation:
        raise ValidationError("任务部署或执行能力与精确执行实例不一致")
    if isinstance(task, AssetGenerationJob):
        if task.workflow_template_version_id != version.workflow_version_id:
            raise ValidationError("任务工作流与精确执行实例不一致")
        if task.retry_of_id:
            source = AssetGenerationJob.objects.only("owner_id", "execution_instance_version_id", "job_type", "media_type", "parent_preparation_id").get(pk=task.retry_of_id)
            if (source.pk == task.pk or source.parent_preparation_id or task.parent_preparation_id
                    or any(getattr(task, key) != getattr(source, key) for key in ("owner_id", "execution_instance_version_id", "job_type", "media_type"))):
                raise ValidationError("素材重试必须沿用原所有者、精确实例和产物类型，不能伪装为字段子任务")
        if task.parent_preparation_id:
            parent = ExecutionPreparation.objects.select_related("asset_job", "deployment_smoke", "workflow_smoke").get(pk=task.parent_preparation_id)
            if parent.stage_run_id or not parent.execution_instance_version_id:
                raise ValidationError("素材字段子任务必须归属有执行实例的素材或验收准备")
            if parent.asset_job_id and parent.asset_job.owner_id != task.owner_id:
                raise ValidationError("字段子任务不能跨素材任务所有者")
            if not parent.asset_job_id and parent.created_by_id != task.owner_id:
                raise ValidationError("验收字段子任务必须沿用根准备的操作人")
            if parent.asset_job_id and parent.asset_job.parent_preparation_id:
                raise ValidationError("辅助叶子不能递归创建字段子任务")
            if not parent.execution_instance_version.auxiliaries.filter(
                    purpose=task.auxiliary_purpose, target_version_id=version.pk).exists():
                raise ValidationError("字段子任务不是父准备明确绑定的辅助实例")
            if version.operation != AUXILIARY_OPERATIONS.get(task.auxiliary_purpose):
                raise ValidationError("字段子任务的辅助用途与执行能力不一致")
    else:
        if not task.preparation_id:
            raise ValidationError("生产调用必须引用真实执行准备")
        preparation = ExecutionPreparation.objects.only("stage_run_id", "execution_instance_version_id", "unit_key").get(pk=task.preparation_id)
        if (preparation.stage_run_id != task.stage_run_id or preparation.execution_instance_version_id != task.execution_instance_version_id
                or task.unit_key != preparation.unit_key):
            raise ValidationError("调用尝试的阶段、准备与执行实例不一致")
        if task.prompt_profile_version_id != version.prompt_profile_version_id:
            raise ValidationError("调用尝试的提示词策略与精确执行实例不一致")
        if task.role == "auxiliary" and version.operation != AUXILIARY_OPERATIONS.get(task.auxiliary_purpose):
            raise ValidationError("辅助调用用途与叶子执行能力不一致")
        from studio.services.execution_auxiliary_requests import read_production_field_context
        if task.role == "auxiliary":
            context = read_production_field_context(preparation)
            if (context is None or task.parent_preparation_id != context[0].pk
                    or task.field_batch_key != context[1].sha256 or task.auxiliary_purpose != context[2]["purpose"]
                    or task.attempt_number != context[2]["attempt_number"]):
                raise ValidationError("辅助调用必须分别记录自身准备与接收字段的主准备")
        elif task.parent_preparation_id or task.operation in AUXILIARY_OPERATIONS.values():
            raise ValidationError("主调用不能携带辅助父准备或字段批次身份")
        if task.generation_task_id:
            from studio.models import GenerationTask
            queued = GenerationTask.objects.only("project_id", "owner_id").get(pk=task.generation_task_id)
            run = task.stage_run.production_run
            if (task.role != "primary" or version.workflow_version_id is None
                    or queued.project_id != run.project_id or queued.owner_id != run.owner_id):
                raise ValidationError("ComfyUI任务必须归属同一项目/所有者的主调用及其精确工作流")


def validate_asset_preparation_link(job, version_id):
    if str(job.execution_instance_version_id) != str(version_id):
        raise ValidationError("素材根任务与准备必须引用同一执行实例", code="execution_preparation_conflict")
    validate_execution_task_links(job)


def validate_smoke_preparation_link(attempt, version_id):
    """Validate the original root in its own scope, never a caller waiver."""
    if not isinstance(attempt, (ModelDeploymentSmokeAttempt, WorkflowSmokeAttempt)):
        raise ValidationError("验收准备必须归属正式烟测任务")
    if version_id is None:
        from studio.services.execution_resource_smoke_definition import read_resource_smoke_definition
        read_resource_smoke_definition(attempt)
        return
    if attempt.scope != "instance" or str(attempt.execution_instance_version_id) != str(version_id):
        raise ValidationError("组合验收与准备必须引用同一执行实例", code="execution_preparation_conflict")
    version = ModelExecutionInstanceVersion.objects.only("deployment_id", "workflow_version_id", "operation").get(pk=version_id)
    if attempt.deployment_id != version.deployment_id:
        raise ValidationError("组合验收的部署与执行实例不一致")
    if isinstance(attempt, WorkflowSmokeAttempt):
        if attempt.workflow_version_id != version.workflow_version_id:
            raise ValidationError("组合验收的工作流与执行实例不一致")
    elif version.workflow_version_id is not None or attempt.operation != version.operation:
        raise ValidationError("模型组合验收不能代替工作流验收，操作能力必须与实例一致")
