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
from django.utils import timezone

from studio.media_signing import classify_task_output
from studio.models import AssemblyJob, GenerationTask


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


def _source_media_snapshot(task: GenerationTask) -> list[dict]:
    sources: dict[tuple[str, str, str], dict] = {}
    for row in task.staged_inputs or []:
        if not isinstance(row, dict):
            continue
        source_sha256 = str(row.get("source_sha256") or row.get("checksum") or "").casefold()
        submitted_sha256 = str(row.get("submitted_sha256") or source_sha256).casefold()
        if not _SHA256_RE.fullmatch(source_sha256):
            continue
        if not _SHA256_RE.fullmatch(submitted_sha256):
            submitted_sha256 = source_sha256
        asset_id = str(row.get("asset_id") or "")
        role = str(row.get("media_role") or "reference")[:80]
        item = {
            "asset_id": asset_id or None,
            "role": role,
            "source_sha256": source_sha256,
            "submitted_sha256": submitted_sha256,
        }
        sources[(asset_id, role, submitted_sha256)] = item
    return [sources[key] for key in sorted(sources)]


def _task_body(
    task: GenerationTask, *, output_index: int, item: dict, artifact_path: Path,
    generated_at,
) -> dict:
    workflow = task.generated_workflow
    prompt = workflow.prompt_artifact
    template = workflow.template
    prompt_text = str(prompt.prompt_text or "")
    negative_prompt = str(prompt.negative_prompt or "")
    sources = _source_media_snapshot(task)
    safe_input_snapshot = {
        "prompt_sha256": hashlib.sha256(prompt_text.encode("utf-8")).hexdigest(),
        "negative_prompt_sha256": (
            hashlib.sha256(negative_prompt.encode("utf-8")).hexdigest()
            if negative_prompt else None
        ),
        "workflow_sha256": workflow.content_hash,
        "seed": task.seed,
        "sources": sources,
    }
    artifact_sha256 = sha256_file(artifact_path)
    return {
        "schema": PROVENANCE_SCHEMA,
        "subject": {"type": "generation_task_output", "id": str(task.id)},
        "disclosure": {
            "ai_generated": True,
            "label": "AI-generated",
            "method": "mandatory-signed-sidecar",
        },
        "artifact": {
            "output_index": output_index,
            "media_kind": classify_task_output(item),
            "filename": Path(str(item.get("filename") or "output")).name,
            "sha256": artifact_sha256,
            "byte_size": artifact_path.stat().st_size,
            "generated_at": _iso(generated_at),
        },
        "generation": {
            "task_id": str(task.id),
            "project_id": str(task.project_id),
            "episode_id": str(task.episode_id) if task.episode_id else None,
            "shot_ids": [
                str(value) for value in (
                    (task.capability_snapshot or {}).get("shot_ids")
                    or (prompt.request_options or {}).get("shot_ids")
                    or ([prompt.shot_id] if prompt.shot_id else [])
                )
            ],
            "model": _public_identifier(prompt.model or template.model),
            "model_profile": _public_identifier(task.model_profile),
            "template_slug": template.slug,
            "template_version": task.workflow_version,
            "workflow_sha256": workflow.content_hash,
            "prompt_artifact_id": str(prompt.id),
            "prompt_sha256": safe_input_snapshot["prompt_sha256"],
            "negative_prompt_sha256": safe_input_snapshot["negative_prompt_sha256"],
            "input_snapshot_sha256": _canonical_sha256(safe_input_snapshot),
        },
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
    item = task.outputs[output_index] if isinstance(task.outputs, list) else None
    provenance = item.get("provenance") if isinstance(item, dict) else None
    if not isinstance(provenance, dict) or provenance.get("ai_generated") is not True:
        raise ProvenanceError("Task output has no sealed provenance")
    try:
        document = json.loads(task_manifest_path(task, output_index).read_text(encoding="utf-8"))
    except (OSError, json.JSONDecodeError) as exc:
        raise ProvenanceError("Task provenance sidecar is unavailable") from exc
    return _verify_document(
        document,
        artifact_path=resolve_task_output_path(task, output_index),
        subject_type="generation_task_output",
        subject_id=str(task.id),
        expected_artifact_sha256=str(provenance.get("artifact_sha256") or ""),
        expected_manifest_sha256=str(provenance.get("manifest_sha256") or ""),
    )


def persist_assembly_provenance(job: AssemblyJob, *, generated_at=None) -> dict:
    output = Path(job.output_path).resolve()
    if not output.is_file():
        raise ProvenanceError("Assembly output media is unavailable")
    source_tasks = list(
        job.source_tasks.select_related(
            "generated_workflow__prompt_artifact", "generated_workflow__template",
        ).order_by("id")
    )
    source_outputs = []
    models = set()
    output_lookup: dict[Path, tuple[GenerationTask, int]] = {}
    for task in source_tasks:
        prompt = task.generated_workflow.prompt_artifact
        models.add(_public_identifier(prompt.model or task.generated_workflow.template.model))
        for index, item in enumerate(task.outputs or []):
            if not isinstance(item, dict) or classify_task_output(item) != "video":
                continue
            output_lookup[resolve_task_output_path(task, index)] = (task, index)

    # FFmpeg's ordered ``-i`` arguments are the exact media inputs.  Using
    # those paths avoids falsely listing every video emitted by a source task
    # when assembly selected only one of them, and preserves repeated
    # multi-shot inputs as distinct clip records.
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
    if source_tasks and not input_paths:
        raise ProvenanceError("Assembly command has no auditable source inputs")
    shot_ids = _ordered_assembly_shot_ids(job, len(input_paths)) if source_tasks else []
    for clip_index, input_path in enumerate(input_paths):
        match = output_lookup.get(input_path)
        if match is None:
            if source_tasks:
                raise ProvenanceError("Assembly input does not match a persisted source task output")
            continue
        task, output_index = match
        item = task.outputs[output_index]
        provenance = item.get("provenance") if isinstance(item, dict) else None
        if not isinstance(provenance, dict):
            task.outputs = persist_task_provenance(
                task, list(task.outputs or []), generated_at=task.finished_at,
            )
        verified = verified_task_manifest(task, output_index)
        shot_id = shot_ids[clip_index]
        generation = verified.get("generation") or {}
        artifact = verified.get("artifact") or {}
        signed_shot_ids = generation.get("shot_ids")
        if (
            task.owner_id != job.owner_id
            or task.project_id != job.project_id
            or str(generation.get("task_id") or "") != str(task.id)
            or str(generation.get("project_id") or "") != str(job.project_id)
            or str(generation.get("episode_id") or "") != str(task.episode_id or "")
            or (
                job.scope == AssemblyJob.Scope.EPISODE
                and str(generation.get("episode_id") or "") != str(job.episode_id or "")
            )
            or not isinstance(signed_shot_ids, list)
            or shot_id not in {str(value) for value in signed_shot_ids}
            or artifact.get("output_index") != output_index
            or artifact.get("media_kind") != "video"
        ):
            raise ProvenanceError(
                "Assembly source shot/task/output binding does not match signed task provenance"
            )
        source_outputs.append({
            "clip_index": clip_index,
            "shot_id": shot_id,
            "task_id": str(task.id),
            "output_index": output_index,
            "artifact_sha256": verified["artifact"]["sha256"],
            "manifest_sha256": verified["integrity"]["payload_sha256"],
            "prompt_sha256": verified["generation"]["prompt_sha256"],
        })
    body = {
        "schema": PROVENANCE_SCHEMA,
        "subject": {"type": "assembly_output", "id": str(job.id)},
        "disclosure": {
            "ai_generated": True,
            "label": metadata_comment or "AI-generated",
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
            "models": sorted(filter(None, models)),
            "source_task_count": len(source_tasks),
        },
        "source_outputs": source_outputs,
    }
    document = _seal(body)
    _write_atomic(assembly_manifest_path(job), document)
    return {
        "schema": PROVENANCE_SCHEMA,
        "ai_generated": True,
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
