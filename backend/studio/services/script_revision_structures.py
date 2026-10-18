"""Immutable structured detail owned by ScriptRevision.

An internal persistence step for M2's original import/worker transactions. The
caller owns visibility and source creation; this step validates and records
the exact structure/mapping/relations, never approval or adoption. Reads
neither parse again nor consult the current output registry. No legacy fallback.
"""
from copy import deepcopy

from django.core.exceptions import ValidationError
from django.db import transaction

from studio.models import BusinessOutputVersion, ScriptRevision, ScriptRevisionStructure, ScriptSource
from studio.services.business_outputs import output_reference, verify_output_reference
from studio.services.data_structures import _value_errors, validate_data_structure_snapshot
from studio.services.json_contracts import assign_json_contract, json_contract, verify_json_contract
from studio.services.script_semantics import (
    SEMANTICS_KEY, ScriptSemanticValidationError, freeze_script_semantics, parse_script_semantic_output, script_semantic_schema,
)
from studio.services.script_sources import create_pasted_script_source, create_script_revision, verify_script_text_source
from studio.services.script_semantic_relations import VALIDATION_SCOPE
from studio.services.security_boundaries import boundary_int


_PROJECTION_KEYS = {"validation_scope", "configuration_sha256", "source_value", "value",
    "defaults", "provenance", "warnings"}


def script_structure_max_bytes():
    """Content/detail budget, not the small execution-configuration budget.

    ScriptRevisionStructure is an on-demand detail, never a list payload.
    Reuse the configured text snapshot boundary without enlarging configuration
    records or dropping original values, defaults, mappings or provenance.
    """
    return boundary_int("security.snapshot.text_max_bytes")


@transaction.atomic
def create_structured_script_source(*, project, user, content, output, source_type, max_bytes):
    """Exact raw revision plus verified detail, in the original caller transaction.

    Import/worker callers own visibility and source selection. They retain failed
    response text using their existing source/response mechanism. A failure here
    rolls back all three rows; it cannot leave a usable half-candidate. No call,
    confirmation, adoption or invented GenerationAttempt is performed here.
    """
    if source_type not in {ScriptSource.SourceType.PASTE, ScriptSource.SourceType.AI_GENERATED}:
        raise ValidationError("结构化文本来源必须明确为输入或实际模型返回，不能冒充上传文件")
    source, revision = create_pasted_script_source(project=project, user=user, content=content,
        source_format=ScriptSource.SourceFormat.PLAIN, source_type=source_type)
    detail = save_script_revision_structure(revision_id=revision.pk, project_id=project.pk,
        output=output, max_bytes=max_bytes)
    return source, revision, detail


def _revision_reference(revision, *, project_id):
    if revision.source.project_id != project_id:
        raise ValidationError("结构化剧本修订不属于指定项目")
    return verify_script_text_source(revision.content, source_kind="script_revision", source_reference={
        "id": str(revision.pk), "source_id": str(revision.source_id), "revision": revision.revision,
        "content_sha256": revision.content_sha256, "content_size_bytes": revision.content_size_bytes})


@transaction.atomic
def revise_structured_script(*, source, user, content, basis, max_bytes):
    """Another preview under the same frozen contract; never edit or adopt the basis."""
    source = ScriptSource.objects.select_for_update().get(pk=source.pk)
    detail = read_script_revision_structure(revision_id=basis["revision_id"],
        project_id=source.project_id, max_bytes=max_bytes)
    if (detail["document"]["revision"]["source_id"] != str(source.pk)
            or detail["document_sha256"] != basis["document_sha256"]):
        raise ValidationError({"structure_basis": "字段草稿依据与原来源或结构化详情不一致，请重新读取。"})
    if not detail["can_revise"]:
        raise ValidationError({"structure_basis": "此来源不能从公共修订入口另存；模型候选须沿原生产审核处理。"})
    revision = create_script_revision(source=source, user=user, content=content)
    save_script_revision_structure(revision_id=revision.pk, project_id=source.project_id,
        output=detail["document"]["output"], max_bytes=max_bytes)
    return revision


def _read_detail(row, revision, *, project_id, max_bytes):
    reference = _revision_reference(revision, project_id=project_id)
    doc = verify_json_contract(row.document, schema_version=row.document_schema_version,
        size_bytes=row.document_size_bytes, sha256=row.document_sha256,
        max_bytes=script_structure_max_bytes()).value
    if (row.document_schema_version != 1 or not isinstance(doc, dict)
            or set(doc) != {"schema_version", "revision", "output", "projection"}
            or type(doc["schema_version"]) is not int or doc["schema_version"] != 1
            or row.revision_id != revision.pk or doc["revision"] != reference):
        raise ValidationError("结构化内容与原剧本修订身份或正文不一致")
    output = verify_output_reference(doc["output"])
    if str(row.output_version_id) != output["id"] or output["parser"]["key"] != SEMANTICS_KEY:
        raise ValidationError("结构化内容与确切剧本输出版本不一致")
    projection = doc["projection"]
    if (not isinstance(projection, dict) or set(projection) != _PROJECTION_KEYS
            or projection["validation_scope"] != VALIDATION_SCOPE
            or not isinstance(projection["configuration_sha256"], str)
            or any(not isinstance(projection[key], list) for key in ("defaults", "warnings", "provenance"))):
        raise ValidationError("结构化剧本缺少完整的保存结果")
    semantics = freeze_script_semantics(source_contract=output["data_structure"],
        mapping=output["parser"]["mapping"], max_bytes=max_bytes)
    if semantics["sha256"] != projection["configuration_sha256"]:
        raise ValidationError("结构化剧本投影与保存的语义配置不一致")
    schema = validate_data_structure_snapshot(output["data_structure"])
    errors = _value_errors(schema, projection["source_value"], path="source_value")
    errors.update(_value_errors(script_semantic_schema(), projection["value"], path="value"))
    if errors:
        raise ValidationError(errors)
    return {"document": deepcopy(doc), "document_sha256": row.document_sha256, "original_text": revision.content,
        "json_schema": schema, "can_revise": not revision.origin_generation_attempt_id
            and revision.source.source_type != ScriptSource.SourceType.AI_GENERATED
            and revision.source.status != ScriptSource.Status.FAILED}


def read_script_revision_structure(*, revision_id, project_id, max_bytes):
    """Read one detail by the revision's unique key; no list hydration or writes."""
    row = ScriptRevisionStructure.objects.select_related("revision__source").only(
        "revision_id", "output_version_id", "document", "document_schema_version", "document_size_bytes", "document_sha256",
        "revision__id", "revision__source_id", "revision__revision", "revision__content",
        "revision__content_sha256", "revision__content_size_bytes", "revision__origin_generation_attempt_id",
        "revision__source__id", "revision__source__project_id", "revision__source__source_type", "revision__source__status",
    ).filter(revision_id=revision_id, revision__source__project_id=project_id).first()
    if row is None:
        raise ValidationError("本项目的剧本修订尚无结构化内容；不会从旧格式或当前配置补造")
    return _read_detail(row, row.revision, project_id=project_id, max_bytes=max_bytes)


def freeze_script_input_structure(*, project_id, reference, max_bytes):
    """Freeze optional structure for an already verified confirmed input.

    Raw text is a valid input to AI structuring. Absence is explicit, not a
    fallback after a failed read: any existing but invalid detail must fail.
    The enclosing adoption transaction owns the run and confirmation reference.
    """
    if not ScriptRevisionStructure.objects.filter(revision_id=reference["id"]).exists():
        return None
    return read_confirmed_script_structure(project_id=project_id, reference=reference,
        max_bytes=max_bytes)["source"]


def read_confirmed_script_structure(*, project_id, reference, max_bytes, expected_source=None):
    """One exact confirmed source for downstream consumers, without materializing.

    The caller supplies its explicit adopted revision, never a project manuscript
    or latest source revision. The small receipt may be frozen in its existing
    decision/stage snapshot. Full values remain owned by the original detail.
    Import, original model output and human revision use this same contract;
    reading it neither invents nor requires a model call for human-authored text.
    """
    from studio.services.production_run_plans import confirmed_script_reference

    if not isinstance(reference, dict):
        raise ValidationError("下游结构化输入需要明确的已确认剧本引用")
    actual = confirmed_script_reference(project_id, {"kind": "script_revision",
        "id": reference.get("id"), "expected_sha256": reference.get("sha256")})
    if actual != reference:
        raise ValidationError("下游剧本引用与原确认修订不一致；不能选择最新修订替代")
    detail = read_script_revision_structure(revision_id=actual["id"], project_id=project_id, max_bytes=max_bytes)
    document = detail["document"]
    saved = document["revision"]
    if (saved["id"] != actual["id"] or saved["source_id"] != actual["source_id"]
            or saved["revision"] != actual["revision"] or saved["content_sha256"] != actual["sha256"]
            or saved["content_size_bytes"] != actual["size_bytes"]):
        raise ValidationError("下游结构化详情与采用剧本的身份或正文不一致")
    output = document["output"]
    source = json_contract({"schema_version": 1, "kind": "confirmed_script_structure",
        "script": actual, "document_sha256": detail["document_sha256"],
        "output": {key: output[key] for key in ("id", "version", "sha256")},
        "configuration_sha256": document["projection"]["configuration_sha256"]}, max_bytes=max_bytes).value
    if expected_source is not None and source != expected_source:
        raise ValidationError("下游结构化依据与已冻结来源不一致；不能重新映射或覆盖旧依据")
    return {"source": source, "structure": detail}


def read_recipe_script_structure(*, stage, max_bytes):
    """Read the recipe's original adoption receipt, never infer from a run FK.

    Only declared, completed upstream input/review can supply this consumer.
    The existing integrity checks own artifact/decision verification; this
    adapter neither creates a missing receipt nor reselects a newer revision.
    """
    from studio.services.production_recipe_preparations import recipe_stage_context
    from studio.services.production_runs import _assert_stage_output_integrity, _script_artifact, _verify_stage_snapshot

    plan, declaration = recipe_stage_context(stage)
    run = stage.production_run
    stages = plan.value["recipe"]["definition"]["stages"]
    prior = [row["key"] for row in stages[:stages.index(declaration)]]
    key = "script_review" if "script_review" in prior else "script_input"
    source_stage = run.stages.filter(stage_key=key, ordinal__lt=stage.ordinal).first() if key in prior else None
    if source_stage is None or source_stage.status != "succeeded":
        raise ValidationError("下游结构化输入缺少本轮已完成的剧本输入/采用步骤")
    _assert_stage_output_integrity(run, source_stage)
    if key == "script_review":
        source = _verify_stage_snapshot(source_stage, "output_snapshot")["review_payload"].get("structure_source")
    else:
        source = _script_artifact(run, source_stage).manifest_metadata.get("structure_source")
    if not isinstance(source, dict):
        raise ValidationError("本轮采用剧本尚无结构化详情，请先完成结构化", code="script_structure_required")
    return read_confirmed_script_structure(project_id=run.project_id, reference=source.get("script"),
        expected_source=source, max_bytes=max_bytes)


@transaction.atomic
def save_script_revision_structure(*, revision_id, project_id, output, max_bytes):
    """Parse the stored original once and attach its detail in the caller's transaction.

    The enclosing writer must persist exact original text before calling here.
    Replays of the same revision/output return the saved detail; changed output
    or content requires a new ScriptRevision. Invalid input leaves no detail.
    """
    revision = ScriptRevision.objects.select_for_update().select_related("source").filter(
        pk=revision_id, source__project_id=project_id).first()
    if revision is None:
        raise ValidationError("指定项目的剧本修订不存在")
    source = _revision_reference(revision, project_id=project_id)
    output = verify_output_reference(output)
    if output["parser"]["key"] != SEMANTICS_KEY:
        raise ValidationError("剧本内容必须明确引用剧本语义映射输出版本")
    existing = ScriptRevisionStructure.objects.filter(revision=revision).first()
    if existing is not None:
        detail = _read_detail(existing, revision, project_id=project_id, max_bytes=max_bytes)
        if detail["document"]["output"] != output:
            raise ValidationError("此剧本修订已绑定其他输出合同；请另存修订")
        return detail
    if revision.status != ScriptRevision.Status.PREVIEW:
        raise ValidationError("只能为新的预览修订保存结构化内容，不能改写已确认剧本")
    registered = BusinessOutputVersion.objects.select_for_update().select_related("data_structure_version").filter(
        pk=output["id"], status__in=("published", "retired")).first()
    # A frozen request may finish after retirement. Identity, not today's active
    # version, is what matters; authoring/import callers select published inputs.
    if registered is None or output_reference(registered) != output:
        raise ValidationError("结构化内容需要真实的确切输出版本，不能保存草稿或伪造合同")
    # The caller's max_bytes still governs the reusable configuration. Actual
    # script values and their expansion/evidence use the content owner's limit.
    freeze_script_semantics(source_contract=output["data_structure"],
        mapping=output["parser"]["mapping"], max_bytes=max_bytes)
    content_limit = script_structure_max_bytes()
    report = parse_script_semantic_output(reference=output, response_text=revision.content, max_bytes=content_limit)
    if not report["valid"]:
        raise ScriptSemanticValidationError(report)
    document = json_contract({"schema_version": 1, "revision": source, "output": output,
        "projection": {key: report[key] for key in _PROJECTION_KEYS}}, max_bytes=content_limit)
    row = ScriptRevisionStructure(revision=revision, output_version=registered)
    assign_json_contract(row, "document", document.value, max_bytes=content_limit)
    row.save(force_insert=True)
    return _read_detail(row, revision, project_id=project_id, max_bytes=max_bytes)
