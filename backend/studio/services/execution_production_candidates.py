"""Verified, on-demand candidate details for the original Artifact registry."""
from copy import deepcopy
from hashlib import sha256

from django.core.exceptions import ValidationError

from studio.models import ExecutionPreparation, GeneratedArtifact, GenerationAttempt
from studio.services.execution_instances import execution_configuration_max_bytes
from studio.services.execution_production_text_contracts import PRODUCTION_TEXT_OPERATIONS, PRODUCTION_TEXT_ROLES
from studio.services.json_contracts import assign_json_contract, json_contract, verify_json_contract


def new_production_text_candidate(attempt, stage, run, *, result, max_bytes, recovery=None):
    """Shared Artifact identity/manifest builder; does not save or settle a call."""
    if attempt.operation not in PRODUCTION_TEXT_ROLES:
        raise ValidationError("此能力没有生产文本产物角色")
    if recovery is not None:
        verify_text_candidate_recovery(attempt, recovery)
    contract = json_contract(result, max_bytes=max_bytes)
    artifact = GeneratedArtifact(production_run=run, stage_run=stage, generation_attempt=attempt,
        manifest_key=f"run:{run.pk}:prepared-text:{attempt.pk}",
        variant_group=f"stage:{stage.pk}:{sha256(attempt.unit_key.encode()).hexdigest()}",
        variant_index=attempt.attempt_number, status="available", storage_backend="database",
        role=PRODUCTION_TEXT_ROLES[attempt.operation])
    metadata = {"schema_version": 1, "operation": attempt.operation, "generation_attempt_id": str(attempt.pk),
        "preparation_id": str(attempt.preparation_id), "request_sha256": attempt.request_sha256,
        "result": contract.value, "result_sha256": contract.sha256}
    if recovery is not None:
        metadata["human_response_recovery"] = deepcopy(recovery)
    return artifact, metadata, contract


def save_production_text_candidate(artifact, metadata, *, max_bytes):
    """One manifest writer for original text candidates; caller owns transaction."""
    assign_json_contract(artifact, "manifest_metadata", metadata, max_bytes=max_bytes)
    artifact.save(force_insert=True)


def verify_text_candidate_recovery(attempt, recovery):
    """Read the real correction record inside the original write/read transaction."""
    from studio.models import ExecutionTextResponseReview
    from studio.services.execution_text_response_review import verified_diagnostics

    review = ExecutionTextResponseReview.objects.filter(attempt=attempt).first()
    if (review is None or review.confirmed_at is None or not review.confirmed_by_id or not isinstance(recovery, dict)
            or recovery != {"review_id": str(review.pk), "original_sha256": review.original_sha256,
                "corrected_sha256": review.corrected_sha256, "confirmed_by": str(review.confirmed_by_id)}
            or sha256(review.original_text.encode("utf-8")).hexdigest() != review.original_sha256
            or sha256(review.corrected_text.encode("utf-8")).hexdigest() != review.corrected_sha256):
        raise ValidationError("人工补全候选与已确认响应记录不一致")
    verified_diagnostics(review)


def read_production_media_candidate(artifact, *, max_media_bytes):
    """Verify an unselected or selected media candidate; caller owns visibility.

    Consumers use the archived bytes and the actual primary preparation, not
    an old prompt/graph row, a current instance choice or the latest candidate.
    This performs no adoption, repair, provider call or new Asset creation.
    """
    from studio.services.execution_production_media import read_prepared_production_media
    from studio.services.generated_artifacts import generated_artifact_storage_owner_id, verified_generated_artifact_media
    from studio.services.json_contracts import canonical_json_bytes
    from studio.services.provenance import ProvenanceError

    if isinstance(artifact.manifest_metadata, dict) and artifact.manifest_metadata.get("source") == "adopted":
        from studio.services.production_adopted_media import read_adopted_media
        return read_adopted_media(artifact, max_media_bytes=max_media_bytes)
    if artifact.kind not in {"image", "video", "audio"} or artifact.storage_backend != "managed_local":
        raise ValidationError("需要已正式归档的媒体候选")
    if artifact.status not in {"available", "selected"} or not artifact.generation_attempt_id:
        raise ValidationError("媒体候选不可用或缺少真实生成调用")
    attempt = GenerationAttempt.objects.select_related("preparation", "stage_run__production_run__project").filter(
        pk=artifact.generation_attempt_id, stage_run_id=artifact.stage_run_id,
        stage_run__production_run_id=artifact.production_run_id).first()
    if attempt is None or attempt.status != "succeeded" or attempt.ignore_late_result:
        raise ValidationError("媒体候选没有属于同一运行、阶段且明确成功的主调用")
    media = read_prepared_production_media(attempt)
    if artifact.kind != media["output_kind"]:
        raise ValidationError("媒体候选类型与冻结调用不一致")
    run = attempt.stage_run.production_run
    metadata = verify_json_contract(artifact.manifest_metadata, schema_version=artifact.manifest_schema_version,
        size_bytes=artifact.manifest_metadata_size_bytes, sha256=artifact.manifest_metadata_sha256,
        max_bytes=execution_configuration_max_bytes()).value
    source = "comfyui" if media["driver_type"] == "comfyui" else "restricted_async_rest"
    identity = {"source": source, "tenant_owner_id": str(run.owner_id), "generation_attempt_id": str(attempt.pk),
        "production_run_id": str(run.pk), "stage_run_id": str(attempt.stage_run_id), "unit_key": attempt.unit_key,
        "preparation_id": str(attempt.preparation_id), "execution_instance_version_id": str(attempt.execution_instance_version_id),
        "request_sha256": attempt.request_sha256, "definition_sha256": media["definition_sha256"],
        "resolved_inputs_sha256": media["resolved_inputs_sha256"], "production_unit": media["production_unit"]}
    if (not isinstance(metadata, dict) or type(metadata.get("schema_version")) is not int or metadata["schema_version"] != 1
            or any(metadata.get(key) != value for key, value in identity.items())
            or artifact.variant_group != f"stage:{attempt.stage_run_id}:unit:{sha256(attempt.unit_key.encode()).hexdigest()}"):
        raise ValidationError("媒体候选清单与实际执行准备不一致")
    if media["driver_type"] == "comfyui":
        if (not artifact.generation_task_id or artifact.generation_task_id != attempt.generation_task_id
                or metadata.get("task_id") != str(artifact.generation_task_id)
                or artifact.generation_task.status != "succeeded"):
            raise ValidationError("工作流候选缺少同次成功任务")
    elif artifact.generation_task_id is not None or attempt.generation_task_id is not None:
        raise ValidationError("异步媒体候选不能伪造工作流任务归属")
    members = list(GeneratedArtifact.objects.filter(generation_attempt=attempt).order_by("variant_index", "id").values("id", "sha256"))
    result_sha = sha256(canonical_json_bytes([{"id": str(row["id"]), "sha256": row["sha256"]} for row in members])).hexdigest()
    if not members or attempt.result_sha256 != result_sha:
        raise ValidationError("媒体候选集与成功调用的结果摘要不一致")
    try:
        if generated_artifact_storage_owner_id(artifact) != str(run.owner_id):
            raise ProvenanceError("媒体候选存储租户与运行所有者不一致")
        path, facts = verified_generated_artifact_media(artifact, max_bytes=max_media_bytes, include_observed_media=True)
    except (ProvenanceError, OSError, ValueError) as exc:
        raise ValidationError({"artifact": str(exc)}) from exc
    return {"source_kind": "execution", "unit_key": attempt.unit_key,
        "preparation_id": str(attempt.preparation_id), "production_unit": media["production_unit"],
        "request_sha256": attempt.request_sha256, "path": path, "media_facts": facts}


def read_production_text_candidate_envelope(artifact, *, max_bytes, for_audit=False):
    """Verify original candidate/call identities, independent of result protocol."""
    if artifact.storage_backend != "database" or not artifact.generation_attempt_id:
        return None
    if not for_audit and artifact.status not in {"available", "selected"}:
        raise ValidationError("文本候选已无效或被拒绝，不能作为可采用内容读取")
    attempt = GenerationAttempt.objects.filter(pk=artifact.generation_attempt_id, stage_run_id=artifact.stage_run_id,
        stage_run__production_run_id=artifact.production_run_id).select_related("preparation").only(
            "id", "role", "status", "operation", "unit_key", "attempt_number", "ignore_late_result", "preparation_id", "execution_instance_version_id", "request_sha256", "result_sha256",
            "stage_run_id", "model_deployment_id", "prompt_profile_version_id", "parent_preparation_id", "retry_of_id",
            "generation_task_id", "route_index", "request_snapshot_key", "request_snapshot_schema_version",
            "request_snapshot_size_bytes", "request_snapshot_sha256",
            "preparation__id", "preparation__stage_run_id", "preparation__request_sha256", "preparation__request_frozen_at",
            "preparation__purpose", "preparation__execution_instance_version_id").first()
    if (attempt is None or attempt.operation not in PRODUCTION_TEXT_OPERATIONS or attempt.status != "succeeded"
            or attempt.ignore_late_result or attempt.role != "primary" or not attempt.preparation_id
            or attempt.preparation.stage_run_id != artifact.stage_run_id
            or attempt.preparation.purpose != "production" or attempt.preparation.request_frozen_at is None
            or len(attempt.request_sha256) != 64
            or attempt.execution_instance_version_id != attempt.preparation.execution_instance_version_id
            or attempt.preparation.request_sha256 != attempt.request_sha256):
        raise ValidationError("文本候选缺少同一正式阶段的成功准备与主调用")
    maximum = max_bytes
    metadata = verify_json_contract(artifact.manifest_metadata, schema_version=artifact.manifest_schema_version,
        size_bytes=artifact.manifest_metadata_size_bytes, sha256=artifact.manifest_metadata_sha256, max_bytes=maximum).value
    script = attempt.operation in {"script.generate", "script.structure"}
    expected_role = PRODUCTION_TEXT_ROLES[attempt.operation]
    if artifact.kind != ("script" if script else "other") or artifact.role != expected_role:
        raise ValidationError("文本候选的产物类型与业务协议不一致")
    keys = {"schema_version", "operation", "generation_attempt_id", "preparation_id", "request_sha256", "result", "result_sha256"}
    if script:
        keys |= {"script_source_id", "script_revision_id"}
    recovery = metadata.get("human_response_recovery") if isinstance(metadata, dict) else None
    if recovery is not None:
        verify_text_candidate_recovery(attempt, recovery)
        keys.add("human_response_recovery")
    if (not isinstance(metadata, dict) or set(metadata) != keys or type(metadata["schema_version"]) is not int
            or metadata["schema_version"] != 1 or metadata["operation"] != attempt.operation
            or metadata["generation_attempt_id"] != str(attempt.pk) or metadata["preparation_id"] != str(attempt.preparation_id)
            or metadata["request_sha256"] != attempt.request_sha256):
        raise ValidationError("文本候选的来源身份与正式调用不一致")
    result = json_contract(metadata["result"], max_bytes=maximum)
    if metadata["result_sha256"] != result.sha256 or attempt.result_sha256 != result.sha256:
        raise ValidationError("文本候选内容与成功调用的结果摘要不一致")
    return attempt, metadata, result


def read_production_text_candidate(artifact, *, for_audit=False):
    """Caller owns visibility. No current-source reads, repair or adoption."""
    if artifact.storage_backend != "database" or not artifact.generation_attempt_id:
        return None
    envelope = read_production_text_candidate_envelope(artifact,
        max_bytes=execution_configuration_max_bytes(), for_audit=for_audit)
    if envelope is None:
        return None
    attempt, metadata, result = envelope
    if attempt.operation in {"script.generate", "script.structure"}:
        from studio.services.execution_preparations import read_production_attempt_request
        from studio.services.script_semantic_candidates import _read_script_semantic_candidate

        # Dispatch by the original call's frozen interface, never by a result's
        # shape or by guessing from current registry configuration.
        _, definition, _ = read_production_attempt_request(attempt)
        output = definition.value["interface"].get("business_output")
        if output is None:
            raise ValidationError("剧本候选的原冻结实例缺少确切业务输出版本")
        return {**_read_script_semantic_candidate(artifact, expected_output=output,
            max_bytes=execution_configuration_max_bytes(), for_audit=for_audit, envelope=envelope),
            "adoption_block_reason": "", "quality_advisories": None, "review_contract": None}
    if (not isinstance(result.value, dict) or set(result.value) != {"result", "warnings"}
            or not isinstance(result.value["result"], dict) or not isinstance(result.value["warnings"], list)):
        raise ValidationError("文本候选内容与成功调用的结果摘要不一致")
    recovery = metadata.get("human_response_recovery")
    structured = attempt.operation == "text.structured"
    output_contract = None
    warnings = result.value["warnings"]
    if structured:
        from studio.services.execution_preparations import read_frozen_execution_request
        from studio.services.execution_structured_text import (
            verify_structured_text_plan, verify_structured_text_result, structured_text_warning_messages,
        )
        _, definition, transport = read_frozen_execution_request(attempt.preparation)
        output_contract = verify_structured_text_plan(transport.value.get("operation_plan"),
            expected_output=definition.value["interface"].get("business_output"))
        candidate = verify_structured_text_result(result.value["result"], output_contract)
        warnings = structured_text_warning_messages(warnings)
    else:
        candidate = result.value["result"].get("candidate")
        if not isinstance(candidate, dict):
            raise ValidationError("业务文本候选必须包含对应协议的对象结果")
    content = None
    if (artifact.sha256 != result.sha256 or artifact.size_bytes != result.size_bytes
            or artifact.object_key != f"generated-artifacts/{artifact.pk}/manifest_metadata/result"):
        raise ValidationError("文本候选内容与产物摘要不一致")
    adoption_block_reason = ""
    review_contract = None
    if attempt.operation == "visual_rules.generate":
        from studio.services.production_visual_rules import VISUAL_RULES_SCHEMA_VERSION
        if result.value["result"].get("schema_version") != VISUAL_RULES_SCHEMA_VERSION:
            adoption_block_reason = "此候选使用旧影视规则协议，仅保留查看；请使用新执行实例版本创建准备并审核新候选。"
        else:
            from studio.services.visual_rules_inputs import verify_visual_rules_candidate_source
            review_contract = verify_visual_rules_candidate_source(attempt.preparation, result.value["result"])
            if review_contract is None:
                adoption_block_reason = "此候选未冻结独立业务输出版本，仅保留查看；请绑定输出版本后生成新候选，不会从当前目录补造审核字段。"
    return {"operation": attempt.operation, "preparation_id": str(attempt.preparation_id),
        "adoption_block_reason": adoption_block_reason,
        "result_sha256": result.sha256, "candidate": deepcopy(candidate), "text": content,
        "warnings": deepcopy(warnings), "human_response_recovery": deepcopy(recovery),
        **(_structured_text_display(output_contract, result.value["warnings"]) if structured else {}),
        "quality_advisories": deepcopy(result.value["result"].get("quality_advisories")),
        "review_contract": review_contract}


def _structured_text_display(output_contract, warnings):
    from studio.services.data_structures import validate_data_structure_snapshot

    # Same compiler as execution; never consult a newer registry version.
    return {"output_contract": deepcopy(output_contract), "structure_warnings": deepcopy(warnings),
        "json_schema": validate_data_structure_snapshot(output_contract["data_structure"])}


def reviewed_text_candidate(*, run, review_stage, selection, expected_source_snapshot_sha256):
    """Verify an explicit candidate under the caller's Run/Stage transaction."""
    from studio.services.production_project_text_stages import (
        project_text_candidate_manifest, project_text_input_contract, text_review_source,
    )
    from studio.services.production_recipe_preparations import recipe_project_stage_context
    from studio.services.production_runs import ProductionRunConflict, _verify_stage_snapshot

    if not isinstance(selection, dict) or set(selection) != {"artifact_id", "expected_sha256"}:
        raise ValidationError({"text_candidate": "请明确选择本次要采用的候选及其摘要"})
    source = text_review_source(review_stage)
    if (review_stage.production_run_id != run.pk
            or _verify_stage_snapshot(review_stage, "input_snapshot") != project_text_input_contract(review_stage).value
            or expected_source_snapshot_sha256 != source.output_snapshot_sha256):
        raise ProductionRunConflict({"detail": "审核来源已变化，请刷新候选后重新核对。"}, code="text_candidate_stale")
    from studio.services.production_recipe_routing import read_recipe_candidate_route
    plan, declaration = recipe_project_stage_context(source)
    choices = [read_recipe_candidate_route(stage=source, plan=plan, declaration=declaration,
        unit_key=f"project:{run.project_id}", candidate_number=n)
        for n in range(1, declaration["routing"]["variant_count"] + 1)]
    ids = [row.preparation.pk for row in choices if row.preparation is not None]
    if len(ids) != len(choices) or _verify_stage_snapshot(source, "output_snapshot").get("review_payload") != project_text_candidate_manifest(source, ids):
        raise ValidationError("生成阶段的候选清单已漂移，不能采用未在本次清单中的结果")
    artifact = GeneratedArtifact.objects.select_for_update().filter(pk=selection["artifact_id"],
        production_run=run, stage_run=source, generation_attempt__preparation_id__in=ids).first()
    if (artifact is None or artifact.status != "available" or artifact.sha256 != selection["expected_sha256"]
            or not artifact.variant_group):
        raise ValidationError("所选文本候选不可用、不属于本次审核或摘要已变化")
    detail = read_production_text_candidate(artifact)
    if detail is None:
        raise ValidationError("所选产物不是有正式来源的文本候选")
    return source, artifact, detail
