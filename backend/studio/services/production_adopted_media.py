"""Existing Asset -> original managed Artifact and explicit selection.

No execution instance, Preparation, Attempt, fake generated provenance or
automatic approval is created. The archived bytes remain the adopted facts.
"""
from hashlib import sha256
from pathlib import Path
from uuid import UUID

from django.contrib.auth import get_user_model
from django.core.exceptions import ValidationError
from django.db.models import Max

from studio.models import Asset, GeneratedArtifact
from studio.services.execution_instances import execution_configuration_max_bytes
from studio.services.generated_artifacts import _artifact_limit, archive_local_media, generated_artifact_storage_owner_id, select_artifact, verified_generated_artifact_media
from studio.services.json_contracts import assign_json_contract, json_contract, verify_json_contract
from studio.services.production_execution_units import resolve_production_unit, verify_production_unit
from studio.services.production_manual_commands import ManualActionResult
from studio.services.provenance import ProvenanceError
from studio.services.reference_bindings import ReferenceBindingError, authorized_asset_source


def read_adopted_media(artifact, *, max_media_bytes):
    """Read the archived adoption, never reread/replace the source Asset file."""
    if (artifact.asset_id is None or artifact.generation_attempt_id is not None or artifact.generation_task_id is not None
            or artifact.storage_backend != "managed_local" or artifact.status not in {"available", "selected"}
            or artifact.kind not in {"image", "video", "audio"} or artifact.role != "adopted_media"):
        raise ValidationError("采用素材必须有真实素材来源，不能混入模型调用归属")
    run, stage = artifact.production_run, artifact.stage_run
    if (run is None or run.plan_source != "manual" or stage is None or stage.mode != "manual"
            or stage.production_run_id != run.pk or stage.stage_key != "media_generation"):
        raise ValidationError("采用素材缺少本轮明确的媒体步骤")
    metadata = verify_json_contract(artifact.manifest_metadata, schema_version=artifact.manifest_schema_version,
        size_bytes=artifact.manifest_metadata_size_bytes, sha256=artifact.manifest_metadata_sha256,
        max_bytes=execution_configuration_max_bytes()).value
    keys = {"schema_version", "source", "tenant_owner_id", "production_run_id", "stage_run_id", "unit_key", "production_unit",
        "asset_id", "asset_sha256", "asset_owner_id", "adopted_by_id", "observed_media"}
    if (not isinstance(metadata, dict) or set(metadata) != keys or type(metadata["schema_version"]) is not int
            or metadata["schema_version"] != 1 or metadata["source"] != "adopted"
            or metadata["production_run_id"] != str(run.pk) or metadata["stage_run_id"] != str(stage.pk)
            or metadata["tenant_owner_id"] != str(run.owner_id) or metadata["asset_id"] != str(artifact.asset_id)
            or metadata["asset_sha256"] != artifact.sha256 or not isinstance(metadata["unit_key"], str)):
        raise ValidationError("采用素材清单与正式产物不一致")
    unit = verify_production_unit(run, metadata["production_unit"])
    if (metadata["unit_key"] != f"{unit['scope']['kind']}:{unit['scope']['id']}"
            or artifact.variant_group != f"stage:{stage.pk}:unit:{sha256(metadata['unit_key'].encode()).hexdigest()}"):
        raise ValidationError("采用素材的用途对象或候选分组不一致")
    try:
        actor_id = get_user_model()._meta.pk.to_python(metadata["adopted_by_id"])
        if not isinstance(metadata["adopted_by_id"], str) or str(actor_id) != metadata["adopted_by_id"]:
            raise ValueError
    except (ValidationError, ValueError, TypeError, AttributeError) as exc:
        raise ValidationError("采用素材缺少明确操作人") from exc
    asset = Asset.objects.only("id", "owner_id").filter(pk=artifact.asset_id).first()
    if (asset is None or asset.owner_id not in (None, run.owner_id)
            or metadata["asset_owner_id"] != (str(asset.owner_id) if asset.owner_id is not None else None)):
        raise ValidationError("采用素材的来源归属不一致")
    try:
        if generated_artifact_storage_owner_id(artifact) != str(run.owner_id):
            raise ProvenanceError("采用素材的存储租户不一致")
        path, facts = verified_generated_artifact_media(artifact, max_bytes=max_media_bytes, include_observed_media=True)
        if facts["observed_media"] != metadata["observed_media"]:
            raise ProvenanceError("采用媒体的实际规格与归档清单不一致")
    except (ProvenanceError, OSError, ValueError) as exc:
        raise ValidationError({"artifact": str(exc)}) from exc
    return {"source_kind": "asset", "unit_key": metadata["unit_key"], "preparation_id": None, "request_sha256": None,
        "production_unit": unit, "path": path, "media_facts": facts}


def adopt_asset_media(*, run, stage, plan, request, actor):
    """Caller holds original Run/Stage transaction and validates its action."""
    from studio.services.production_manual_media import media_selection_state

    source = request["payload"]["source"]
    if set(source) != {"kind", "asset_id", "expected_sha256", "media_kind"} or source["kind"] != "asset":
        raise ValidationError("采用素材须提供素材ID、已查看的文件摘要及媒体类型")
    try:
        identifier = str(UUID(source["asset_id"]))
    except (ValueError, TypeError, AttributeError) as exc:
        raise ValidationError("请选择正式素材库素材") from exc
    digest = source["expected_sha256"]
    if (not isinstance(source["media_kind"], str) or source["media_kind"] not in {"image", "video", "audio"} or not isinstance(digest, str)
            or len(digest) != 64 or any(c not in "0123456789abcdef" for c in digest)):
        raise ValidationError("素材类型或文件摘要无效")
    unit = resolve_production_unit(run, request["scope"])
    if unit["run_plan_sha256"] != plan.sha256 or request["unit_key"] != f"{request['scope']['kind']}:{request['scope']['id']}":
        raise ValidationError("请选择本轮正式制作对象，不能自行拼接素材用途")
    group = f"stage:{stage.pk}:unit:{sha256(request['unit_key'].encode()).hexdigest()}"
    state = media_selection_state(run_id=run.pk, stage_id=stage.pk, group=group)
    if request["expected_selection_revision"] != (state.revision if state else 0):
        raise ValidationError("此制作对象的采用已变化，请重新读取后核对", code="manual_selection_conflict")
    # Lock the formal source against concurrent management writes. Authorization
    # uses the business owner, not staff's ability to browse another tenant.
    if not Asset.objects.select_for_update().filter(pk=identifier).exists():
        raise ValidationError("素材不存在")
    try:
        asset, path = authorized_asset_source(owner=run.owner, asset_id=identifier)
    except (ReferenceBindingError, OSError, ValueError) as exc:
        raise ValidationError({"asset": str(exc)}) from exc
    if asset.checksum.lower() != digest or not asset.mime_type.startswith(source["media_kind"] + "/"):
        raise ValidationError("素材内容或类型已变化，请重新查看")
    if not isinstance(asset.metadata, dict) or asset.metadata.get("usage") == "project_cover":
        raise ValidationError("项目封面不是本轮可采用的生产素材")
    identity = {"production_run_id": str(run.pk), "stage_run_id": str(stage.pk), "production_unit": unit,
        "unit_key": request["unit_key"], "asset_id": identifier, "asset_sha256": digest, "kind": source["media_kind"]}
    key = "adopted:" + json_contract(identity, max_bytes=execution_configuration_max_bytes()).sha256
    artifact = GeneratedArtifact.objects.filter(manifest_key=key).first()
    limit = _artifact_limit(run)
    if artifact is None:
        relative = Path("generated-artifacts", str(run.owner_id), str(run.project_id), "adopted", key.split(":", 1)[1], digest)
        try:
            archived = archive_local_media(path, relative=relative, expected_kind=source["media_kind"], expected_sha256=digest, max_bytes=limit)
        except (ProvenanceError, OSError, ValueError) as exc:
            raise ValidationError({"asset": str(exc)}) from exc
        observed = archived.pop("observed_media")
        last = GeneratedArtifact.objects.filter(production_run=run, stage_run=stage, variant_group=group).aggregate(value=Max("variant_index"))["value"] or 0
        artifact = GeneratedArtifact(production_run=run, stage_run=stage, asset=asset, manifest_key=key,
            variant_group=group, variant_index=last + 1, kind=source["media_kind"], role="adopted_media", status="available", **archived)
        assign_json_contract(artifact, "manifest_metadata", {"schema_version": 1, "source": "adopted",
            "tenant_owner_id": str(run.owner_id), **{k: v for k, v in identity.items() if k != "kind"},
            "asset_owner_id": str(asset.owner_id) if asset.owner_id is not None else None,
            "adopted_by_id": str(actor.pk), "observed_media": observed}, max_bytes=execution_configuration_max_bytes())
        artifact.save()
    verified = read_adopted_media(artifact, max_media_bytes=limit)
    if (artifact.production_run_id != run.pk or artifact.stage_run_id != stage.pk or artifact.asset_id != asset.pk
            or artifact.sha256 != digest or artifact.kind != source["media_kind"] or verified["production_unit"] != unit):
        raise ValidationError("已有采用产物与本次明确来源不一致，不能覆盖")
    if state is not None and state.artifact_id == artifact.pk:
        return ManualActionResult("artifact_selection", state.pk)
    select_artifact(artifact, user=actor)
    decision = media_selection_state(run_id=run.pk, stage_id=stage.pk, group=group)
    return ManualActionResult("artifact_selection", decision.pk)
