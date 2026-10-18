"""Read the original storyboard review, not a new approval or media store."""
from django.core.exceptions import ValidationError
from django.db import transaction

from studio.models import ProductionRun, SeriesProject
from studio.services.production_execution_sources import execution_source_stage_ids
from studio.services.production_execution_units import resolve_production_unit
from studio.services.production_reference_mapping import reference_use_records
from studio.services.production_reviewed_content import read_reviewed_storyboard, reviewed_shot_row
from studio.services.shot_reference_sources import resolve_shot_reference_sources


@transaction.atomic
def read_reviewed_shot_references(*, stage, shot_id):
    """Caller authorizes/locks the Run; source and file integrity stay explicit."""
    run = ProductionRun.objects.select_for_update().get(pk=stage.production_run_id)
    # Selection writers take the same project lock. Verify the original review
    # and resolve its saved choices in one read boundary, never from an old pool.
    SeriesProject.objects.select_for_update().get(pk=run.project_id)
    unit = resolve_production_unit(stage.production_run, {"kind": "shot", "id": shot_id})
    reviewed = read_reviewed_storyboard(run=stage.production_run, eligible_stage_ids=execution_source_stage_ids(stage))
    shot = reviewed_shot_row(reviewed, unit)
    if not isinstance(shot.get("reference_uses"), list):
        raise ValidationError("审核结果未明确覆盖当前镜头的参考清单", code="reference_review_invalid")
    sources = resolve_shot_reference_sources(actor=run.owner, project_id=run.project_id, shot_ids=[shot_id]).value
    return {"records": reference_use_records(sources["shots"][0]["uses"]), "unit": unit, "review": reviewed["review"]}
