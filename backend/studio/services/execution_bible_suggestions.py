"""Editable input copies from the original, reviewed production Bible."""
from django.core.exceptions import ValidationError
from django.db import transaction

from studio.models import ProductionRun, StageRun
from studio.services.production_execution_sources import execution_source_stage_ids
from studio.services.production_execution_units import resolve_production_unit
from studio.services.production_reviewed_content import (
    BIBLE_PRIMARY_FIELDS, read_reviewed_bible, read_reviewed_entity_primary, reviewed_entity_row,
)


BIBLE_INPUT_REFERENCES = {
    "production.entity.description": "使用本轮剧本 Bible 描述",
    **{f"production.entity.primary.{field}": f"使用本轮{label}" for field, label in BIBLE_PRIMARY_FIELDS.items()},
}


@transaction.atomic
def read_bible_input_reference(*, owner, project_id, run_id, scope, selector,
        eligible_stage_ids=None, decision_id=None):
    if selector not in BIBLE_INPUT_REFERENCES or not project_id or not run_id:
        raise ValidationError("Bible 输入需要明确的本轮制作、项目和已登记字段")
    run = ProductionRun.objects.select_for_update().filter(pk=run_id, owner=owner, project_id=project_id).first()
    if run is None:
        raise ValidationError("制作轮不存在或无权读取")
    unit = resolve_production_unit(run, scope)
    if eligible_stage_ids is None:
        stage = StageRun.objects.filter(production_run=run, stage_key="bible_image_generation").first()
        if stage is None:
            raise ValidationError("本轮未声明 Bible 参考图生成步骤")
        eligible_stage_ids = execution_source_stage_ids(stage)
    reviewed = read_reviewed_bible(run=run, eligible_stage_ids=eligible_stage_ids, decision_id=decision_id)
    row = reviewed_entity_row(reviewed, unit)
    setting_origin = {}
    field = selector.rsplit(".", 1)[-1]
    if selector.startswith("production.entity.primary."):
        fields, reference = read_reviewed_entity_primary(row, unit)
        content = fields[field]
        setting_origin = {"setting": reference}
    else:
        content = row.get(field)
    if not isinstance(content, str) or not content.strip():
        raise ValidationError("所选 Bible 字段为空，请在原设定中补充并审核，或明确填写本次输入")
    return content, {"production_run_id": str(run.pk), "production_scope": unit["scope"],
        "run_plan_sha256": unit["run_plan_sha256"], "source_name": row["name"],
        "script_revision_id": str(run.script_revision_id) if run.script_revision_id else None,
        "script_adoption_decision_id": str(run.script_adoption_decision_id) if run.script_adoption_decision_id else None,
        "review": reviewed["review"], **setting_origin,
        "location": "本轮采用剧本 → Bible 来源与审核 → 对应人物／场景／道具",
        "note": "仅此对象的已审核字段；可编辑本次副本，不改剧本或 Bible，不包含整份剧本。"}
