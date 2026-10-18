"""One explicit candidate admission check for manual text review stages."""
from django.core.exceptions import ValidationError

from studio.models import GeneratedArtifact
from studio.services.execution_production_candidates import read_production_text_candidate
from studio.services.production_stage_catalog import PROJECT_TEXT_REVIEW_SOURCES, STAGE_EXECUTION_OPERATIONS


def manual_text_candidate(*, run, stage, plan, selection, unit_key):
    if not isinstance(selection, dict) or set(selection) != {"artifact_id", "expected_sha256", "expected_manifest_sha256"}:
        raise ValidationError("请明确选择候选及已读取的正文、清单摘要")
    artifact = GeneratedArtifact.objects.select_for_update().filter(pk=selection["artifact_id"], production_run=run).first()
    if (artifact is None or artifact.status != "available" or not artifact.variant_group
            or artifact.sha256 != selection["expected_sha256"]
            or artifact.manifest_metadata_sha256 != selection["expected_manifest_sha256"]):
        raise ValidationError("候选不可用、不属于本轮或已发生变化，未采用", code="manual_selection_conflict")
    source = artifact.stage_run
    ordinals = {row["key"]: row["ordinal"] for row in plan.value["stage_catalog"]["value"]["stages"]}
    if (source.stage_key not in PROJECT_TEXT_REVIEW_SOURCES[stage.stage_key] or source.mode != "manual"
            or source.ordinal != ordinals.get(source.stage_key) or stage.ordinal != ordinals.get(stage.stage_key)
            or source.ordinal >= stage.ordinal or not artifact.generation_attempt_id
            or artifact.generation_attempt.unit_key != unit_key):
        raise ValidationError("候选不是本轮明确项目单元和已登记来源步骤的文本结果")
    detail = read_production_text_candidate(artifact)
    if detail is None or detail["operation"] != STAGE_EXECUTION_OPERATIONS[source.stage_key]:
        raise ValidationError("候选操作能力与来源阶段不一致")
    return artifact, detail

