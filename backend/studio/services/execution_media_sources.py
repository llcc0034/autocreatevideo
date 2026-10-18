"""Re-authorize concrete media bytes; local paths never enter request JSON.

Preparation and the original worker's staging adapter share the Asset reader.
Generated outputs retain their real selection/Artifact identity, not a fake
Asset. This module neither uploads nor creates/approves/dispatches any task.
"""
from dataclasses import dataclass
from pathlib import Path

from django.core.exceptions import ValidationError

from studio.models import Asset
from studio.services.execution_selected_outputs import resolve_selected_execution_media
from studio.services.execution_workflow_uses import read_execution_workflow_uses
from studio.services.generated_artifacts import verified_local_media_facts
from studio.services.provenance import ProvenanceError
from studio.services.reference_bindings import authorized_asset_source, ReferenceBindingError
from studio.services.security_boundaries import security_boundary_resolution_cache


@dataclass(frozen=True)
class ExecutionAssetMedia:
    asset: Asset
    path: Path
    media_facts: dict


@security_boundary_resolution_cache()
def resolve_execution_asset_media(*, owner, asset_id, expected_kind, max_media_bytes):
    """None is for library association; execution callers supply their own byte limit."""
    try:
        asset, path = authorized_asset_source(owner=owner, asset_id=asset_id)
        facts = verified_local_media_facts(path, expected_kind=expected_kind,
            max_bytes=path.stat().st_size if max_media_bytes is None else max_media_bytes,
            include_observed_media=True)
    except (ReferenceBindingError, ProvenanceError, OSError) as exc:
        raise ValidationError({"media_source": str(exc)}) from exc
    if facts["sha256"] != asset.checksum.casefold():
        raise ValidationError("素材在解析期间发生变化，不能冻结混合事实")
    return ExecutionAssetMedia(asset, path, facts)


@security_boundary_resolution_cache()
def resolve_execution_workflow_media_sources(definition, resolved_inputs, *, owner, max_media_bytes,
        production_run_id=None, eligible_stage_ids=()):
    """Return paths only for exact frozen graph uses, after current checks.

    eligible_stage_ids is provided by the root's frozen plan adapter. Never
    authorize a selected output using the stage IDs asserted by that output.
    Frozen descriptions stay frozen; current media identity and bytes must
    still match. Callers must not persist or expose the returned local paths.
    """
    uses = read_execution_workflow_uses(definition, resolved_inputs)
    required = {row["item_key"] for rows in uses.slots.values() for row in rows}
    result = {}
    for item_key, item in uses.items.items():
        if item_key not in required:
            continue
        origin = item["origin"]
        if origin.get("kind") == "video_audio":
            continue  # Authorize the concrete parent first, independent of input declaration order.
        if origin.get("kind") == "asset":
            if origin != {"kind": "asset", "asset_id": item["asset_id"]} or item["artifact_id"] is not None:
                raise ValidationError("冻结素材的来源与Asset身份不一致")
            source = resolve_execution_asset_media(owner=owner, asset_id=item["asset_id"],
                expected_kind=item["kind"], max_media_bytes=max_media_bytes)
        elif origin.get("kind") == "selected_output":
            source = resolve_selected_execution_media(owner=owner, decision_id=origin.get("decision_id"),
                artifact_id=item["artifact_id"], production_run_id=production_run_id,
                eligible_stage_ids=eligible_stage_ids, expected_kind=item["kind"], max_media_bytes=max_media_bytes)
            if source.selection != origin or (str(source.artifact.asset_id) if source.artifact.asset_id else None) != item["asset_id"]:
                raise ValidationError("冻结产物来源或选片决定已变化，不能替换当前来源")
        else:
            raise ValidationError("素材不是正式Asset或明确已采用产物")
        if source.media_facts != item["media_facts"]:
            raise ValidationError("实际媒体已偏离准备时的字节或观测事实，禁止沿用旧请求")
        result[item_key] = source.path
    for item_key, item in uses.items.items():
        if item_key in required and item["origin"].get("kind") == "video_audio":
            parent = item["paired_to"]
            if parent not in result:
                raise ValidationError("视频原音缺少本次已核对的父视频来源")
            result[item_key] = result[parent]
    return result
