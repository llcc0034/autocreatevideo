"""Stage exact prepared media uses through the existing Comfy upload client.

Called only inside the original worker's claimed, admitted execution. This is
not a queue or an API: ownership, cancel/lease checks and durable receipt writes
are supplied by that root adapter. No /prompt call or task success is made here.
"""
from copy import deepcopy
from dataclasses import dataclass
from pathlib import Path, PurePosixPath
import re
from tempfile import TemporaryDirectory
from uuid import UUID

from django.core.exceptions import ValidationError

from studio.services.comfyui import ComfyUIError
from studio.services.execution_definitions import verify_execution_definition_payload
from studio.services.execution_instances import execution_configuration_max_bytes
from studio.services.execution_request_storage import execution_request_max_bytes
from studio.services.execution_media_sources import resolve_execution_workflow_media_sources
from studio.services.execution_media_processing import execution_media_processing_uses
from studio.services.execution_workflow_media import prepare_execution_workflow_media
from studio.services.json_contracts import JsonContract, json_contract, verify_json_contract
from studio.services.media_extensions import verified_media_extension
from studio.services.provenance import sha256_file
from studio.services.reference_media import inspect_reference_video_contract, preprocess_reference_input
from studio.services.security_boundaries import security_boundary_resolution_cache
from studio.services.workflow_media_tokens import (
    collect_workflow_media_use_tokens, substitute_workflow_media_uses, MEDIA_USE_TOKEN_PREFIX,
)


@dataclass(frozen=True)
class StagedExecutionWorkflow:
    graph: dict
    uploaded_paths: tuple[str, ...]
    receipts: JsonContract


@dataclass(frozen=True)
class ExecutionWorkflowReceipts:
    graph: dict
    mapping: dict[str, str]
    missing_tokens: frozenset[str]
    sources: tuple[dict, ...]


class ExecutionWorkflowUploadClient:
    """One original provider upload protocol for every prepared root."""
    def __init__(self, driver, deployment):
        self.driver, self.deployment = driver, deployment

    def upload_input_file(self, source_path, *, subfolder, filename):
        return self.driver.invoke(self.deployment, "upload", {
            "source_path": str(source_path), "subfolder": subfolder, "filename": filename})


def _input_path(value, namespace):
    if (not isinstance(value, str) or not value.startswith(namespace + "/") or "\\" in value
            or any(ord(char) < 32 or char in ":?#%" for char in value)):
        raise ValidationError("上传回执必须属于本次租户和任务的input目录")
    path = PurePosixPath(value)
    if str(path) != value or str(path.parent) != namespace or path.name in {"", ".", ".."}:
        raise ValidationError("上传回执不能越过本次任务目录")
    return value


def _receipt_identity(binding, *, spec, by_item, root_key, owner_id, fingerprint, profile):
    parent_key = spec["paired_to"] if spec is not None else None
    return {"schema_version": 2, "token": binding["token"], "item_key": binding["item_key"],
        "root_id": root_key, "owner_id": str(owner_id), "target_fingerprint": fingerprint,
        "source_facts_sha256": binding["source_sha256"], "source_sha256": binding["media_facts"]["sha256"],
        "adapter": spec["contract"]["adapter_key"] if spec is not None else "source_bytes/v1",
        "processing_sha256": json_contract(spec, max_bytes=execution_configuration_max_bytes()).sha256,
        "media_profile_sha256": profile["sha256"] if spec is not None else None,
        "paired_video_item_key": parent_key,
        "paired_video_source_sha256": by_item[parent_key]["source_sha256"] if parent_key is not None else None}


def _verify_receipt(record, expected, namespace):
    if (set(record) != set(expected) | {"comfy_path", "submitted_sha256", "metadata", "metadata_sha256"}
            or any(record.get(key) != value for key, value in expected.items())
            or not isinstance(record["submitted_sha256"], str)
            or re.fullmatch(r"[a-f0-9]{64}", record["submitted_sha256"]) is None
            or not isinstance(record["metadata"], dict)
            or json_contract(record["metadata"], max_bytes=execution_configuration_max_bytes()).sha256 != record["metadata_sha256"]
            or (expected["adapter"] == "source_bytes/v1" and record["submitted_sha256"] != expected["source_sha256"])):
        raise ValidationError("暂存回执的来源、目标或任务已漂移，不能复用")
    return _input_path(record["comfy_path"], namespace)


@security_boundary_resolution_cache()
def read_execution_workflow_receipts(*, definition, resolved_inputs, transport, owner_id, root_id, receipts):
    """Verify available per-use evidence without uploads or mutable lookups.

    Missing evidence is distinct from corrupt evidence. Read-only inspection
    can show the frozen, unstaged graph; collection must require completeness.
    """
    from studio.services.execution_workflow_requests import verify_execution_workflow_transport
    graph = verify_execution_workflow_transport(definition, transport)["graph"]
    media = prepare_execution_workflow_media(definition, resolved_inputs).value
    if media != transport.value["payload"]["media_preparation"]:
        raise ValidationError("暂存回执必须使用本次冻结素材目录")
    bindings = media["bindings"]
    if (not isinstance(receipts, list) or any(not isinstance(row, dict) or not isinstance(row.get("token"), str) for row in receipts)
            or len({row["token"] for row in receipts}) != len(receipts)
            or not {row["token"] for row in receipts}.issubset({row["token"] for row in bindings})):
        raise ValidationError("暂存回执包含重复或其他请求的素材使用")
    processing = execution_media_processing_uses(definition, resolved_inputs)
    by_item = {row["item_key"]: row for row in bindings}
    by_token = {row["token"]: row for row in receipts}
    root_key = str(UUID(str(root_id)))
    namespace = f"autoprompt/{owner_id}/{root_key}"
    sources, mapping = [], {}
    for binding in bindings:
        if binding["token"] not in by_token:
            continue
        expected = _receipt_identity(binding, spec=processing.get(binding["item_key"]), by_item=by_item,
            root_key=root_key, owner_id=owner_id, fingerprint=definition.value["target_fingerprint"],
            profile=media["reference_media_profile"])
        receipt = by_token[binding["token"]]
        mapping[binding["token"]] = _verify_receipt(receipt, expected, namespace)
        # Preserve repeated uses and their order. Public evidence never contains
        # filesystem paths, prompt prose or the provider's connection settings.
        sources.append({"item_key": binding["item_key"], "asset_id": binding["asset_id"],
            "artifact_id": binding["artifact_id"], "source_sha256": expected["source_sha256"],
            "source_facts_sha256": expected["source_facts_sha256"], "submitted_sha256": receipt["submitted_sha256"],
            "processing_sha256": expected["processing_sha256"], "metadata_sha256": receipt["metadata_sha256"]})
    return ExecutionWorkflowReceipts(graph, mapping,
        frozenset(row["token"] for row in bindings) - frozenset(mapping), tuple(sources))


def read_execution_workflow_receipt_sources(**kwargs):
    checked = read_execution_workflow_receipts(**kwargs)
    if checked.missing_tokens:
        raise ValidationError("归档需要本次全部素材使用的唯一暂存回执")
    return list(checked.sources)


@security_boundary_resolution_cache()
def stage_execution_workflow_media(*, definition, resolved_inputs, transport, owner, root_id,
        client, staged_inputs, persist_staged_inputs, check_active, max_media_bytes,
        production_run_id=None, eligible_stage_ids=()):
    """Upload per-use derivatives under the original frozen processing contracts.

    Fixed graph inputs without a conditioning mode retain their original bytes;
    dynamic video/audio must use the same registered adapters as production.
    A receipt is durable after each upload. A replay rechecks current source
    authorization and bytes; it never trusts the cached path as authority.
    """
    definition = verify_execution_definition_payload(definition)
    if not isinstance(transport, JsonContract):
        raise ValidationError("上传必须引用已冻结的完整请求")
    checked = verify_json_contract(transport.value, schema_version=transport.schema_version,
        size_bytes=transport.size_bytes, sha256=transport.sha256, max_bytes=execution_request_max_bytes()).value
    if (checked.get("definition_sha256") != definition.sha256 or checked.get("driver_type") != "comfyui"
            or checked.get("target_fingerprint") != definition.value["target_fingerprint"]):
        raise ValidationError("上传请求与冻结执行目标不一致")
    if not callable(persist_staged_inputs) or not callable(check_active) or not callable(getattr(client, "upload_input_file", None)):
        raise ValidationError("必须使用原worker的上传客户端、任务状态检查及持久回执接口")
    try:
        root_key = str(UUID(str(root_id)))
    except (ValueError, TypeError, AttributeError) as exc:
        raise ValidationError("上传目录必须引用本次正式根任务UUID") from exc
    namespace = f"autoprompt/{owner.pk}/{root_key}"
    media = prepare_execution_workflow_media(definition, resolved_inputs).value
    payload = checked.get("payload")
    compiled = payload.get("compiled_workflow") if isinstance(payload, dict) else None
    if not isinstance(compiled, dict) or payload.get("media_preparation") != media:
        raise ValidationError("上传必须使用原请求的素材使用目录，不能替换当前输入")
    from studio.services.execution_workflow_requests import verify_execution_workflow_transport
    graph = verify_execution_workflow_transport(definition, transport)["graph"]
    bindings = {row["token"]: row for row in media["bindings"]}
    try:
        required = collect_workflow_media_use_tokens(graph)
    except ValueError as exc:
        raise ValidationError(str(exc)) from exc
    if required != set(bindings):
        raise ValidationError("图占位符与素材使用目录不一致，不能丢弃或增加素材")
    processing = execution_media_processing_uses(definition, resolved_inputs)
    frozen_profile = media["reference_media_profile"]
    profile = frozen_profile["value"] if frozen_profile is not None else None
    if not isinstance(staged_inputs, list) or any(not isinstance(row, dict) or not isinstance(row.get("token"), str) for row in staged_inputs):
        raise ValidationError("暂存回执必须为本次明确记录，不能读取其他任务缓存")
    records = deepcopy(staged_inputs)
    if len({row.get("token") for row in records}) != len(records) or any(row.get("token") not in bindings for row in records):
        raise ValidationError("暂存回执包含重复或其他请求的素材使用")
    check_active()
    paths = resolve_execution_workflow_media_sources(definition, resolved_inputs, owner=owner,
        max_media_bytes=max_media_bytes, production_run_id=production_run_id, eligible_stage_ids=eligible_stage_ids)
    mapping, video_plans = {}, {}
    by_item = {row["item_key"]: row for row in bindings.values()}
    def video_plan(item_key):
        if item_key not in video_plans:
            spec = processing.get(item_key)
            if spec is None or spec["slot"] != "videos":
                raise ValidationError("配套音轨的父视频没有本次预处理合同")
            video_plans[item_key] = inspect_reference_video_contract(paths[item_key], spec["contract"], profile)
        return video_plans[item_key]
    for token, binding in bindings.items():
        check_active()
        spec = processing.get(binding["item_key"])
        parent_key = spec["paired_to"] if spec is not None else None
        expected = _receipt_identity(binding, spec=spec, by_item=by_item, root_key=root_key, owner_id=owner.pk,
            fingerprint=checked["target_fingerprint"], profile=frozen_profile)
        record = next((row for row in records if row["token"] == token), None)
        if record is not None:
            path = _verify_receipt(record, expected, namespace)
            checker = getattr(type(client), "staged_input_sha256", None)
            if callable(checker) and checker(client, path) != record["submitted_sha256"]:
                raise ValidationError("已上传输入的字节摘要发生变化，不能复用")
            mapping[token] = path
            continue
        source = paths[binding["item_key"]]
        if sha256_file(source) != expected["source_sha256"]:
            raise ValidationError("上传前素材字节已变化")
        related = {binding["item_key"]: expected["source_sha256"]}
        if parent_key is not None:
            related[parent_key] = by_item[parent_key]["media_facts"]["sha256"]
        with TemporaryDirectory(prefix="autoprompt-reference-") as directory:
            staged_path, metadata = source, {}
            if spec is not None:
                plan_key = binding["item_key"] if spec["slot"] == "videos" else parent_key
                plan = video_plan(plan_key) if plan_key is not None else None
                # Native graph audio shares the normalized video. An audio-only
                # port uses the same original paired-audio adapter on its verified
                # video source; the existing receipt records derivative bytes.
                staged_path, metadata = preprocess_reference_input(source, Path(directory), slot=spec["slot"],
                    contract=spec["contract"], media_profile=profile, video_plan=plan,
                    include_embedded_audio=bool(spec.get("embedded_audio_sources")))
            suffix = (staged_path.suffix.lower() if spec is not None
                else verified_media_extension(binding["media_facts"]["mime_type"], binding["kind"]))
            if not re.fullmatch(r"\.[a-z0-9]{1,10}", suffix):
                raise ValidationError("受管素材文件扩展名无法用于安全上传")
            if not 0 < staged_path.stat().st_size <= max_media_bytes:
                raise ValidationError("实际待上传素材大小超出本次允许范围")
            submitted_sha256 = sha256_file(staged_path)
            check_active()
            if any(sha256_file(paths[key]) != digest for key, digest in related.items()):
                raise ValidationError("素材在预处理期间变化，不能提交")
            response = client.upload_input_file(staged_path, subfolder=namespace,
                filename=token[len(MEDIA_USE_TOKEN_PREFIX):] + suffix)
            if sha256_file(staged_path) != submitted_sha256:
                raise ValidationError("预处理产物在上传期间变化，不能提交")
        if (not isinstance(response, dict) or response.get("type") != "input" or response.get("subfolder") != namespace
                or not isinstance(response.get("name"), str) or "/" in response["name"]):
            raise ComfyUIError("ComfyUI未返回本次任务的有效input上传回执")
        remote_path = _input_path(namespace + "/" + response["name"], namespace)
        if any(sha256_file(paths[key]) != digest for key, digest in related.items()):
            raise ValidationError("素材在上传期间变化，不能提交可能混合的输入")
        record = {**expected, "comfy_path": remote_path, "submitted_sha256": submitted_sha256,
            "metadata": metadata, "metadata_sha256": json_contract(metadata, max_bytes=execution_configuration_max_bytes()).sha256}
        records.append(record)
        receipt = json_contract(records, max_bytes=execution_configuration_max_bytes())
        # The adapter writes under the original task lease/cancellation check.
        # No receipt is fabricated if either the upload or this write fails.
        persist_staged_inputs(deepcopy(receipt.value))
        mapping[token] = remote_path
    check_active()
    try:
        graph = substitute_workflow_media_uses(graph, mapping)
    except ValueError as exc:
        raise ValidationError(str(exc)) from exc
    return StagedExecutionWorkflow(graph, tuple(mapping.values()),
        json_contract(records, max_bytes=execution_configuration_max_bytes()))
