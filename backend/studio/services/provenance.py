"""Tamper-evident provenance sidecars for generated media.

The manifest deliberately contains hashes and stable identifiers only.  It
never copies prompt text, local filesystem paths, credentials, or user names.
"""

from __future__ import annotations

from copy import deepcopy
from functools import lru_cache
import hashlib
import json
import os
from pathlib import Path
import re
import uuid

from django.conf import settings
from django.core import signing
from django.core.exceptions import ValidationError
from django.utils import timezone

from studio.media_signing import classify_task_output
from studio.models import AssemblyJob, GenerationTask
from studio.services.json_contracts import json_contract
PROVENANCE_SCHEMA = "https://autoprompt.local/schemas/provenance/v1"
PROVENANCE_SIGNING_SALT = "autoprompt.provenance.v1"
_SHA256_RE = re.compile(r"^[0-9a-f]{64}$")
_ASSEMBLY_SHOT_LINE_RE = re.compile(
    r"^# autoprompt-shot-id ([0-9a-f]{8}-[0-9a-f]{4}-[1-5][0-9a-f]{3}-[89ab][0-9a-f]{3}-[0-9a-f]{12})$",
    re.IGNORECASE,
)


class ProvenanceError(ValueError):
    """A provenance record cannot be created or verified safely."""


def sha256_file(path: str | Path) -> str:
    digest = hashlib.sha256()
    with Path(path).open("rb") as source:
        for chunk in iter(lambda: source.read(1024 * 1024), b""):
            digest.update(chunk)
    return digest.hexdigest()


@lru_cache(maxsize=512)
def _cached_sha256_file(
    resolved_path: str, byte_size: int, modified_ns: int, changed_ns: int,
) -> str:
    # The stat tuple is part of the cache key.  Normal playback range requests
    # therefore hash a large video once, while any ordinary byte replacement
    # invalidates the cached verification before another response is served.
    digest = sha256_file(resolved_path)
    after = Path(resolved_path).stat()
    if (
        after.st_size != byte_size
        or after.st_mtime_ns != modified_ns
        or after.st_ctime_ns != changed_ns
    ):
        raise ProvenanceError("Generated artifact changed during provenance verification")
    return digest


def _verified_artifact_sha256(path: Path) -> str:
    resolved = path.resolve()
    stat = resolved.stat()
    return _cached_sha256_file(
        str(resolved), stat.st_size, stat.st_mtime_ns, stat.st_ctime_ns,
    )


def verified_file_sha256(path: str | Path) -> str:
    """Verify managed bytes with the same stat-keyed cache used by provenance."""

    return _verified_artifact_sha256(Path(path))


def _canonical_bytes(value: dict) -> bytes:
    return json.dumps(
        value, ensure_ascii=False, sort_keys=True, separators=(",", ":"),
    ).encode("utf-8")


def _canonical_sha256(value: dict) -> str:
    return hashlib.sha256(_canonical_bytes(value)).hexdigest()


def _public_identifier(value: object) -> str:
    text = str(value or "").strip()
    if not text:
        return ""
    if re.match(r"^[A-Za-z]:[\\/]", text) or text.startswith(("/", "\\\\")):
        return re.split(r"[\\/]", text.rstrip("\\/"))[-1]
    return text[:240]


def _iso(value) -> str:
    moment = value or timezone.now()
    return moment.isoformat() if hasattr(moment, "isoformat") else str(moment)


def _seal(body: dict) -> dict:
    payload_sha256 = _canonical_sha256(body)
    signature = signing.Signer(salt=PROVENANCE_SIGNING_SALT).sign(payload_sha256)
    return {
        **body,
        "integrity": {
            "payload_sha256": payload_sha256,
            "signature": signature,
            "signature_scheme": "django-hmac-sha256",
        },
    }


def _write_atomic(path: Path, document: dict) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    temporary = path.with_name(f".{path.name}.{uuid.uuid4().hex}.tmp")
    try:
        temporary.write_bytes(_canonical_bytes(document) + b"\n")
        os.replace(temporary, path)
    finally:
        temporary.unlink(missing_ok=True)


def resolve_task_output_path(task: GenerationTask, output_index: int) -> Path:
    if not isinstance(task.outputs, list) or not 0 <= output_index < len(task.outputs):
        raise ProvenanceError("Task output index is unavailable")
    item = task.outputs[output_index]
    if not classify_task_output(item):
        raise ProvenanceError("Task output is not browser media")
    filename = item.get("filename") if isinstance(item, dict) else None
    subfolder = item.get("subfolder", "") if isinstance(item, dict) else None
    if not isinstance(filename, str) or not isinstance(subfolder, str):
        raise ProvenanceError("Task output path is malformed")
    configured_root = str(settings.COMFYUI_OUTPUT_DIR or "").strip()
    if not configured_root:
        raise ProvenanceError("ComfyUI output directory is not configured")
    allowed_root = Path(configured_root).resolve()
    candidate = (allowed_root / subfolder / filename).resolve()
    if not candidate.is_relative_to(allowed_root) or not candidate.is_file():
        raise ProvenanceError("Task output media is unavailable")
    return candidate


def task_manifest_path(task: GenerationTask, output_index: int) -> Path:
    return (
        Path(settings.MEDIA_ROOT).resolve()
        / "provenance"
        / str(task.owner_id)
        / "tasks"
        / str(task.id)
        / f"{output_index}.json"
    )


def assembly_manifest_path(job: AssemblyJob) -> Path:
    if not job.output_path:
        raise ProvenanceError("Assembly output path is unavailable")
    output = Path(job.output_path).resolve()
    allowed_root = (
        Path(settings.MEDIA_ROOT).resolve()
        / "assemblies"
        / str(job.owner_id)
        / str(job.project_id)
    ).resolve()
    if not output.is_relative_to(allowed_root):
        raise ProvenanceError("Assembly output path crosses its tenant boundary")
    return output.with_name(f"{output.name}.provenance.json")


def _ordered_assembly_shot_ids(job: AssemblyJob, expected_count: int) -> list[str]:
    """Read the enqueue-time shot/input mapping without guessing history."""

    if not job.concat_manifest_path:
        raise ProvenanceError(
            "Assembly source mapping has no shot identifiers; reassemble before release"
        )
    candidate = Path(job.concat_manifest_path).resolve()
    allowed_root = (
        Path(settings.MEDIA_ROOT).resolve()
        / "assemblies" / str(job.owner_id) / str(job.project_id)
    ).resolve()
    if not candidate.is_relative_to(allowed_root) or not candidate.is_file():
        raise ProvenanceError("Assembly source mapping is unavailable")
    try:
        lines = candidate.read_text(encoding="utf-8").splitlines()
    except OSError as exc:
        raise ProvenanceError("Assembly source mapping is unavailable") from exc
    shot_ids: list[str] = []
    pending_shot_id = ""
    for raw_line in lines:
        line = raw_line.strip()
        match = _ASSEMBLY_SHOT_LINE_RE.fullmatch(line)
        if match:
            if pending_shot_id:
                raise ProvenanceError("Assembly source mapping has duplicate shot identifiers")
            pending_shot_id = str(uuid.UUID(match.group(1)))
            continue
        if line.startswith("file "):
            if not pending_shot_id:
                # Historical ffconcat files had only file rows. Do not infer a
                # shot from mutable prompts or task order; they must be rebuilt.
                raise ProvenanceError(
                    "Assembly source mapping has no shot identifiers; reassemble before release"
                )
            shot_ids.append(pending_shot_id)
            pending_shot_id = ""
    if pending_shot_id or len(shot_ids) != expected_count:
        raise ProvenanceError("Assembly source shot order does not match FFmpeg inputs")
    return shot_ids


def _prepared_task_generation(task, context, definition):
    """Public identity from this actual frozen request, never today's routing."""
    attempt, media = context.attempt, context.media
    compiled = media["payload"]["compiled_workflow"]
    target = definition.value["target"]
    return {
        "task_id": str(task.pk), "generation_attempt_id": str(attempt.pk),
        "preparation_id": str(attempt.preparation_id),
        "execution_instance_version_id": str(attempt.execution_instance_version_id),
        "project_id": str(task.project_id), "production_run_id": str(attempt.stage_run.production_run_id),
        "stage_run_id": str(attempt.stage_run_id),
        "production_unit": media["production_unit"],
        "workflow_version_id": target["workflow"]["id"],
        "model_profile_version_id": target["model"]["id"],
        "model": _public_identifier(target["model"]["identifier"]),
        "definition_sha256": media["definition_sha256"],
        "request_sha256": attempt.request_sha256,
        "resolved_inputs_sha256": media["resolved_inputs_sha256"],
        "workflow_sha256": compiled["graph_sha256"],
        "workflow_contract_sha256": compiled["workflow_contract_sha256"],
        "resolved_semantics_sha256": compiled["resolved_semantics_sha256"],
        "asset_bindings_sha256": compiled["asset_bindings_sha256"],
        "model_requirements_sha256": compiled["model_requirements_sha256"],
        "config_snapshot_sha256": media["execution_deadlines"]["configuration_sha256"],
    }


def _task_body(
    task: GenerationTask, *, output_index: int, item: dict, artifact_path: Path,
    generated_at,
) -> dict:
    """Seal actual Preparation identity; old trace/PromptArtifact is not evidence."""
    from studio.services.execution_production_workflow_queue import read_prepared_generation_task
    from studio.services.execution_preparations import read_frozen_execution_request
    from studio.services.execution_workflow_staging import read_execution_workflow_receipt_sources
    from studio.services.execution_instances import execution_resolved_inputs_max_bytes

    context = read_prepared_generation_task(task)
    attempt, media = context.attempt, context.media
    detail, definition, transport = read_frozen_execution_request(attempt.preparation)
    sources = read_execution_workflow_receipt_sources(definition=definition,
        resolved_inputs=json_contract(detail.resolved_inputs["inputs"], max_bytes=execution_resolved_inputs_max_bytes()),
        transport=transport, owner_id=task.owner_id, root_id=task.pk, receipts=task.staged_inputs)
    observed = item.get("observed_media")
    if not isinstance(observed, dict):
        raise ProvenanceError("生成产物缺少真实媒体观测，不能封存")
    return {
        "schema": PROVENANCE_SCHEMA,
        "subject": {"type": "generation_task_output", "id": str(task.pk)},
        "disclosure": {"ai_generated": True, "label": "AI-generated", "method": "mandatory-signed-sidecar"},
        "artifact": {
            "output_index": output_index, "media_kind": media["output_kind"],
            "filename": artifact_path.name, "sha256": sha256_file(artifact_path),
            "byte_size": artifact_path.stat().st_size, "generated_at": _iso(generated_at),
            "observed_media": deepcopy(observed),
        },
        "generation": _prepared_task_generation(task, context, definition),
        "source_media": sources,
    }


def persist_task_provenance(
    task: GenerationTask, outputs: list[dict], *, generated_at=None,
) -> list[dict]:
    """Seal every real browser-media output and persist only safe digests."""

    original_outputs = task.outputs
    task.outputs = outputs
    enriched: list[dict] = []
    try:
        for index, raw in enumerate(outputs):
            item = dict(raw) if isinstance(raw, dict) else raw
            if not isinstance(item, dict) or not classify_task_output(item):
                enriched.append(item)
                continue
            artifact_path = resolve_task_output_path(task, index)
            document = _seal(_task_body(
                task,
                output_index=index,
                item=item,
                artifact_path=artifact_path,
                generated_at=generated_at,
            ))
            manifest_path = task_manifest_path(task, index)
            _write_atomic(manifest_path, document)
            item["provenance"] = {
                "schema": PROVENANCE_SCHEMA,
                "ai_generated": True,
                "artifact_sha256": document["artifact"]["sha256"],
                "manifest_sha256": document["integrity"]["payload_sha256"],
            }
            enriched.append(item)
    except Exception:
        task.outputs = original_outputs
        raise
    task.outputs = enriched
    task.save(update_fields=("outputs", "updated_at"))
    return enriched


def _verify_document(
    document: dict, *, artifact_path: Path, subject_type: str, subject_id: str,
    expected_artifact_sha256: str = "", expected_manifest_sha256: str = "",
) -> dict:
    if not isinstance(document, dict):
        raise ProvenanceError("Provenance manifest is not a JSON object")
    integrity = document.get("integrity")
    if not isinstance(integrity, dict):
        raise ProvenanceError("Provenance integrity envelope is missing")
    body = deepcopy(document)
    body.pop("integrity", None)
    payload_sha256 = _canonical_sha256(body)
    if payload_sha256 != str(integrity.get("payload_sha256") or ""):
        raise ProvenanceError("Provenance payload hash does not match")
    try:
        signed_payload = signing.Signer(salt=PROVENANCE_SIGNING_SALT).unsign(
            str(integrity.get("signature") or ""),
        )
    except signing.BadSignature as exc:
        raise ProvenanceError("Provenance signature is invalid") from exc
    if signed_payload != payload_sha256:
        raise ProvenanceError("Provenance signature does not cover this payload")
    subject = body.get("subject") or {}
    if subject.get("type") != subject_type or str(subject.get("id")) != str(subject_id):
        raise ProvenanceError("Provenance subject does not match the requested artifact")
    recorded_artifact_sha256 = str((body.get("artifact") or {}).get("sha256") or "")
    if expected_manifest_sha256 and payload_sha256 != expected_manifest_sha256:
        raise ProvenanceError("Persisted provenance digest does not match the sidecar")
    if expected_artifact_sha256 and recorded_artifact_sha256 != expected_artifact_sha256:
        raise ProvenanceError("Persisted artifact digest does not match the sidecar")
    current_artifact_sha256 = _verified_artifact_sha256(artifact_path)
    if current_artifact_sha256 != recorded_artifact_sha256:
        raise ProvenanceError("Generated artifact has changed since provenance was sealed")
    return {
        **document,
        "verification": {
            "status": "verified",
            "signature_valid": True,
            "artifact_hash_valid": True,
            "verified_at": timezone.now().isoformat(),
        },
    }


def verified_task_manifest(task: GenerationTask, output_index: int) -> dict:
    if type(output_index) is not int or not isinstance(task.outputs, list) or not 0 <= output_index < len(task.outputs):
        raise ProvenanceError("Task output index is unavailable")
    item = task.outputs[output_index]
    provenance = item.get("provenance") if isinstance(item, dict) else None
    if not isinstance(provenance, dict) or provenance.get("ai_generated") is not True:
        raise ProvenanceError("Task output has no sealed provenance")
    try:
        document = json.loads(task_manifest_path(task, output_index).read_text(encoding="utf-8"))
    except (OSError, json.JSONDecodeError) as exc:
        raise ProvenanceError("Task provenance sidecar is unavailable") from exc
    verified = _verify_document(
        document,
        artifact_path=resolve_task_output_path(task, output_index),
        subject_type="generation_task_output",
        subject_id=str(task.id),
        expected_artifact_sha256=str(provenance.get("artifact_sha256") or ""),
        expected_manifest_sha256=str(provenance.get("manifest_sha256") or ""),
    )
    from studio.services.execution_preparations import read_frozen_execution_request
    from studio.services.execution_production_workflow_queue import read_prepared_generation_task
    try:
        context = read_prepared_generation_task(task)
        _detail, definition, _transport = read_frozen_execution_request(context.attempt.preparation)
    except ValidationError as exc:
        raise ProvenanceError("Task source preparation cannot be verified") from exc
    if verified.get("generation") != _prepared_task_generation(task, context, definition):
        raise ProvenanceError("Task provenance does not match its actual frozen preparation")
    artifact = verified.get("artifact")
    if (not isinstance(artifact, dict) or artifact.get("output_index") != output_index
            or artifact.get("media_kind") != context.media["output_kind"]):
        raise ProvenanceError("Task provenance output does not match its frozen contract")
    return verified


def resolve_declared_task_output(task: GenerationTask, role: str = "primary") -> dict:
    """Resolve the one frozen primary using the same verifier as archival."""
    if role != "primary":
        raise ProvenanceError("Only the frozen primary output role is supported")
    if task.status != GenerationTask.Status.SUCCEEDED:
        raise ProvenanceError("Source generation task has not succeeded")
    from studio.services.generated_artifacts import _prepared_task_primary
    from studio.services.provider_registry import ProviderDriverError
    try:
        context, output_index, item, source, facts, _limit = _prepared_task_primary(task, task.outputs)
    except (ValidationError, ProviderDriverError) as exc:
        raise ProvenanceError("Source task primary output contract cannot be verified") from exc
    manifest = verified_task_manifest(task, output_index)
    artifact = manifest["artifact"]
    integrity = manifest["integrity"]
    if (artifact["media_kind"] != context.media["output_kind"]
            or artifact["sha256"] != facts["sha256"]
            or artifact.get("observed_media") != facts["observed_media"]):
        raise ProvenanceError("Source task signed media does not match its actual primary output")
    return {
        "output_index": output_index, "item": deepcopy(item), "path": source,
        "manifest": manifest, "artifact_sha256": artifact["sha256"],
        "manifest_sha256": integrity["payload_sha256"],
    }


def persist_assembly_provenance(job: AssemblyJob, *, generated_at=None) -> dict:
    output = Path(job.output_path).resolve()
    if not output.is_file():
        raise ProvenanceError("Assembly output media is unavailable")
    source_outputs = []
    models = set()
    # Command inputs must equal the ordered frozen artifact members. Never
    # reconstruct them from old tasks, PromptArtifacts or output filenames.
    command = list(job.ffmpeg_command or [])
    filter_complex_values = [
        str(command[index + 1])
        for index, value in enumerate(command[:-1])
        if value == "-filter_complex"
    ]
    metadata_values = [
        str(command[index + 1])
        for index, value in enumerate(command[:-1])
        if value == "-metadata"
    ]
    metadata_comment = next(
        (value.removeprefix("comment=") for value in metadata_values if value.startswith("comment=")),
        "",
    )
    metadata_description = next(
        (
            value.removeprefix("description=")
            for value in metadata_values if value.startswith("description=")
        ),
        "",
    )
    visible_overlay = any("drawtext=" in value for value in filter_complex_values)
    mp4_metadata = bool(metadata_comment and metadata_description)
    input_paths = [
        Path(command[index + 1]).resolve()
        for index, value in enumerate(command[:-1])
        if value == "-i"
    ]
    source_items = list(job.source_items.select_related(
        "shot__episode", "generated_artifact__generation_attempt__preparation",
    ).order_by("ordinal"))
    if not source_items or len(source_items) != len(input_paths):
        raise ProvenanceError("Assembly requires a complete ordered frozen artifact manifest")
    from studio.services.assembly import AssemblyError, _assembly_artifact_evidence
    from studio.services.execution_preparations import read_frozen_execution_request
    for clip_index, (source, input_path) in enumerate(zip(source_items, input_paths, strict=True)):
        generated = source.generated_artifact
        if generated is None:
            raise ProvenanceError("Assembly source must be an explicitly adopted managed artifact")
        try:
            evidence, _ = _assembly_artifact_evidence(generated, source.shot)
        except AssemblyError as exc:
            raise ProvenanceError(str(exc)) from exc
        expected = (Path(settings.MEDIA_ROOT).resolve() / source.object_key).resolve()
        if (
            input_path != expected or input_path != evidence["path"]
            or generated.storage_backend != source.storage_backend
            or generated.object_key != source.object_key
            or generated.sha256 != source.sha256
            or generated.size_bytes != source.size_bytes
            or generated.mime_type != source.mime_type
            or sha256_file(input_path) != source.sha256
        ):
            raise ProvenanceError("Assembly managed artifact source facts do not match")
        attempt = generated.generation_attempt
        if evidence["source_kind"] == "execution":
            _, definition, _ = read_frozen_execution_request(attempt.preparation)
            models.add(_public_identifier(definition.value["target"]["model"]["identifier"]))
        source_outputs.append({
            "clip_index": clip_index, "shot_id": str(source.shot_id),
            "media_kind": source.media_kind, "generated_artifact_id": str(generated.pk),
            "source_kind": evidence["source_kind"],
            "generation_attempt_id": str(attempt.pk) if attempt is not None else None,
            "execution_instance_version_id": str(attempt.execution_instance_version_id) if attempt is not None else None,
            "preparation_id": evidence["preparation_id"],
            "asset_id": str(generated.asset_id) if evidence["source_kind"] == "asset" else None,
            "artifact_sha256": source.sha256, "manifest_sha256": generated.manifest_metadata_sha256,
            "request_sha256": evidence["request_sha256"],
        })
    # A library Asset may itself be AI-generated, but adoption does not prove
    # it. True means at least one verified model-produced source; null means
    # origin is not established, never an invented human-authored claim.
    ai_generated = True if any(row["source_kind"] == "execution" for row in source_outputs) else None
    body = {
        "schema": PROVENANCE_SCHEMA,
        "subject": {"type": "assembly_output", "id": str(job.id)},
        "disclosure": {
            "ai_generated": ai_generated,
            "label": metadata_comment or None,
            "method": (
                "visible-overlay+mp4-metadata+mandatory-signed-sidecar"
                if visible_overlay and mp4_metadata
                else "mp4-metadata+mandatory-signed-sidecar"
                if mp4_metadata
                else "mandatory-signed-sidecar"
            ),
            "visible_overlay": visible_overlay,
            "mp4_metadata": mp4_metadata,
            "metadata_description": metadata_description or None,
        },
        "artifact": {
            "media_kind": "video",
            "filename": output.name,
            "sha256": sha256_file(output),
            "byte_size": output.stat().st_size,
            "generated_at": _iso(generated_at),
        },
        "assembly": {
            "job_id": str(job.id),
            "project_id": str(job.project_id),
            "episode_id": str(job.episode_id) if job.episode_id else None,
            "scope": job.scope,
            "source_signature": job.source_signature,
            "source_contract_version": job.source_contract_version,
            "models": sorted(filter(None, models)),
            "source_artifact_count": sum(1 for item in source_items if item.generated_artifact_id),
        },
        "source_outputs": source_outputs,
    }
    document = _seal(body)
    _write_atomic(assembly_manifest_path(job), document)
    return {
        "schema": PROVENANCE_SCHEMA,
        "ai_generated": ai_generated,
        "artifact_sha256": document["artifact"]["sha256"],
        "manifest_sha256": document["integrity"]["payload_sha256"],
    }


def verified_assembly_manifest(job: AssemblyJob) -> dict:
    output = Path(job.output_path).resolve()
    try:
        document = json.loads(assembly_manifest_path(job).read_text(encoding="utf-8"))
    except (OSError, json.JSONDecodeError) as exc:
        raise ProvenanceError("Assembly provenance sidecar is unavailable") from exc
    return _verify_document(
        document,
        artifact_path=output,
        subject_type="assembly_output",
        subject_id=str(job.id),
    )
