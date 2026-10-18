"""Source-stage eligibility shared by preparation and media staging.

This grants no selection approval. The selected-output reader still verifies the
exact Decision, current review, ownership and archived bytes under the Run lock.
"""
from django.core.exceptions import ValidationError

from studio.models import StageRun
from studio.services.production_run_plans import get_run_execution_plan


def execution_source_stage_ids(stage):
    run = stage.production_run
    plan = get_run_execution_plan(run)
    if plan.value["source"] == "manual":
        expected = {row["key"]: (row["ordinal"], "manual")
            for row in plan.value["stage_catalog"]["value"]["stages"]}
    else:
        # Recipe stages are an explicit ordered subset, not their ordinal in
        # the complete manual catalog. Match the actual frozen declaration.
        expected = {row["key"]: (index, row["mode"])
            for index, row in enumerate(plan.value["recipe"]["definition"]["stages"], start=1)}
    if expected.get(stage.stage_key) != (stage.ordinal, stage.mode):
        raise ValidationError("当前执行步骤与冻结运行计划不一致")
    eligible = []
    for source in StageRun.objects.filter(production_run_id=run.pk).order_by().values("id", "stage_key", "ordinal", "mode", "status"):
        if expected.get(source["stage_key"]) != (source["ordinal"], source["mode"]):
            raise ValidationError("来源步骤与冻结运行计划不一致，不能据当前顺序猜测来源")
        # A manual producer remains open for more candidates. Its already
        # adopted outputs are usable only while the separate media review is
        # current, including as references for the next call in this producer.
        manual_media = (plan.value["source"] == "manual" and stage.stage_key == "media_generation"
            and source["stage_key"] == "media_generation" and source["mode"] == "manual")
        if manual_media or source["ordinal"] < stage.ordinal and source["status"] == "succeeded":
            eligible.append(str(source["id"]))
    return tuple(eligible)
