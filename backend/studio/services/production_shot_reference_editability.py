"""One execution-state boundary for per-shot selection in either Run source.

Callers hold the original Run/Project locks and authorize the frozen action.
Selection cannot rewrite prepared/submitted inputs, including uncertain calls.
"""
from django.db.models import Q

from studio.models import ExecutionPreparation, GenerationAttempt, ProductionAssemblyLink


def shot_reference_execution_editability(run, stage, shot_id):
    if run.status not in {"awaiting_input", "awaiting_approval", "running"}:
        return False, "run_closed", "本轮已停止或结束，不修改其素材选择。"
    if ProductionAssemblyLink.objects.filter(production_run=run).exists():
        return False, "assembly_frozen", "本轮已提交装配，不能改变其素材选择。"
    preparations = ExecutionPreparation.objects.filter(stage_run__production_run=run,
        stage_run__ordinal__gt=stage.ordinal, unit_key=f"shot:{shot_id}")
    unused_cancelled = Q(status="cancelled", cancelled_at__isnull=False, request_frozen_at__isnull=True,
        request_sha256__isnull=True, claim_token__isnull=True, claim_until__isnull=True, next_check_at__isnull=True)
    if preparations.exclude(unused_cancelled).exists() or GenerationAttempt.objects.filter(
            Q(preparation__in=preparations) | Q(parent_preparation__in=preparations)
            | Q(stage_run__production_run=run, unit_key=f"shot:{shot_id}")).exists():
        return False, "shot_prepared", "本镜已有执行准备或调用；选材不能改变原准备输入。可明确停止未调用且未冻结的准备后再选材，其他镜头不受影响。"
    return True, "editable", "仅修改本镜已声明用途；不等于通过审核或请求输入已经就绪。"
