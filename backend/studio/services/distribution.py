"""Managed, visibly disclosed distribution copies for individual task outputs.

ComfyUI outputs are production masters and are never edited.  A public copy is
created in a content-addressed directory under MEDIA_ROOT, then the complete
directory (media plus signed provenance) is atomically published.  Public
download always re-verifies the source master, the distribution bytes and the
signed sidecar, so stale tokens fail closed after either file is changed.
"""

from __future__ import annotations

from copy import deepcopy
import hashlib
import json
import os
from pathlib import Path
import re
import shutil
import subprocess
import sys
import time
import unicodedata
import uuid

from django.conf import settings
from django.core import signing
from django.core.exceptions import ValidationError
from django.db import transaction
from django.utils import timezone
from PIL import Image, ImageChops, ImageDraw, ImageFont, ImageOps, PngImagePlugin, UnidentifiedImageError

from studio.media_signing import classify_task_output
from studio.models import GenerationTask
from studio.services.delivery_disclosure import (
    DeliveryDisclosureError,
    resolve_delivery_disclosure,
)
from studio.services.configuration import (
    ConfigurationResolutionError,
    resolve_configuration,
)
from studio.services.media_processing_profiles import (
    MediaProcessingProfileError,
    ResolvedMediaProcessingProfile,
    resolve_distribution_media_profile,
    validate_distribution_profile,
)
from studio.services.provenance import (
    ProvenanceError,
    resolve_task_output_path,
    sha256_file,
    verified_task_manifest,
)
from studio.services.security_boundaries import boundary_bool
from studio.services.worker_heartbeat import WorkerHeartbeat


DISTRIBUTION_SCHEMA = "https://autoprompt.local/schemas/task-distribution-provenance/v1"
DISTRIBUTION_SIGNING_SALT = "autoprompt.task-distribution.v1"
DISTRIBUTION_PROTOCOL_VERSION = "shot-public-v1"
_SHA256_RE = re.compile(r"^[0-9a-f]{64}$")


class DistributionError(ValueError):
    """A public distribution copy cannot be created or safely verified."""


class DistributionClaimLost(DistributionError):
    """Late work must not replace the current source or another publisher."""


def _canonical_bytes(value: dict) -> bytes:
    return json.dumps(
        value, ensure_ascii=False, sort_keys=True, separators=(",", ":"),
    ).encode("utf-8")


def _canonical_sha256(value: dict) -> str:
    return hashlib.sha256(_canonical_bytes(value)).hexdigest()


def _seal(body: dict) -> dict:
    payload_sha256 = _canonical_sha256(body)
    signature = signing.Signer(salt=DISTRIBUTION_SIGNING_SALT).sign(payload_sha256)
    return {
        **body,
        "integrity": {
            "payload_sha256": payload_sha256,
            "signature": signature,
            "signature_scheme": "django-hmac-sha256",
        },
    }


def _write_json(path: Path, document: dict) -> None:
    with path.open("wb") as stream:
        stream.write(_canonical_bytes(document) + b"\n")
        stream.flush()
        os.fsync(stream.fileno())


def _assert_distribution_enabled() -> None:
    try:
        enabled = boundary_bool("security.delivery.public_distribution_enabled")
    except (ConfigurationResolutionError, ValidationError) as exc:
        raise DistributionError(
            "Managed public distribution security boundary is unavailable"
        ) from exc
    if not enabled:
        raise DistributionError("Managed public distribution copies are disabled")


def _task_disclosure(task: GenerationTask, media_kind: str, *, generation: dict) -> dict:
    _assert_distribution_enabled()
    if task.kind != media_kind:
        raise DistributionError("Task output kind does not match its model distribution profile")
    # The signed source was checked against the actual Preparation, including
    # the exact model version. Do not consult a mutable deployment or old trace.
    model = generation.get("model")
    model_key = generation.get("model_profile_version_id")
    if not isinstance(model, str) or not model.strip() or not isinstance(model_key, str) or not model_key:
        raise DistributionError("Task execution contract has no model identity")
    try:
        disclosure = resolve_delivery_disclosure(
            model=model,
            model_key=model_key,
            media_kind=media_kind,
        )
    except DeliveryDisclosureError as exc:
        raise DistributionError(str(exc)) from exc
    return {
        "model_key": disclosure.model_key,
        "model": disclosure.model,
        "media_kind": disclosure.media_kind,
        "label": disclosure.label,
        "description": disclosure.description,
    }


def _valid_version() -> str:
    return DISTRIBUTION_PROTOCOL_VERSION


def _font_candidates() -> list[Path]:
    configured = str(getattr(settings, "TASK_DISTRIBUTION_FONTFILE", "") or "").strip()
    if configured:
        candidate = Path(os.path.expandvars(configured)).expanduser()
        if not candidate.is_file():
            raise DistributionError(
                "The configured disclosure font is unavailable; set TASK_DISTRIBUTION_FONTFILE to a readable font"
            )
        return [candidate]
    candidates: list[Path] = []
    if sys.platform == "win32":
        root = Path(os.environ.get("WINDIR", r"C:\Windows")) / "Fonts"
        candidates.extend(root / name for name in (
            "msyh.ttc", "arial.ttf", "segoeui.ttf", "simhei.ttf",
        ))
    else:
        candidates.extend(Path(name) for name in (
            "/usr/share/fonts/opentype/noto/NotoSansCJK-Regular.ttc",
            "/usr/share/fonts/truetype/noto/NotoSans-Regular.ttf",
            "/usr/share/fonts/truetype/dejavu/DejaVuSans.ttf",
            "/usr/share/fonts/truetype/liberation2/LiberationSans-Regular.ttf",
        ))
    return candidates


def _resolve_font() -> Path:
    for candidate in _font_candidates():
        if candidate.is_file():
            try:
                # Pillow is the stricter common denominator for image and
                # video publication; this also rejects corrupt font files
                # before either renderer is invoked.
                ImageFont.truetype(str(candidate), 16)
            except OSError:
                continue
            return candidate.resolve()
    raise DistributionError(
        "No readable disclosure font was found; configure TASK_DISTRIBUTION_FONTFILE"
    )


def _drawtext_value(value: str) -> str:
    return (
        value.replace("\\", "\\\\").replace(":", "\\:")
        .replace("'", "\\'").replace("%", "\\%")
    )


def _release_config(
    *, source_sha256: str, media_kind: str, font_sha256: str, disclosure: dict,
    media_profile: ResolvedMediaProcessingProfile,
) -> dict:
    return {
        "source_sha256": source_sha256,
        "media_kind": media_kind,
        "model_key": disclosure["model_key"],
        "model": disclosure["model"],
        "label": disclosure["label"],
        "description": disclosure["description"],
        "version": _valid_version(),
        "font_sha256": font_sha256,
        "media_profile_key": media_profile.key,
        "media_profile_sha256": media_profile.sha256,
        "media_profile": media_profile.value,
        "image_profile": "png-rgba-visible-box-v1",
    }


def distribution_configuration() -> dict:
    """Return a path-free, verified snapshot of the publication renderer config."""

    _assert_distribution_enabled()
    font = _resolve_font()
    try:
        media_profile = resolve_distribution_media_profile()
    except MediaProcessingProfileError as exc:
        raise DistributionError(str(exc)) from exc
    try:
        disclosure = resolve_delivery_disclosure()
    except DeliveryDisclosureError as exc:
        raise DistributionError(str(exc)) from exc
    return {
        "enabled": True,
        "base_label": disclosure.base_label,
        "version": _valid_version(),
        "lock_stale_seconds": _lock_stale_seconds(),
        "font_sha256": sha256_file(font),
        "media_profile_key": media_profile.key,
        "media_profile_sha256": media_profile.sha256,
        "media_profile": media_profile.value,
    }


def _distribution_parent(task: GenerationTask, output_index: int) -> Path:
    return (
        Path(settings.MEDIA_ROOT).resolve()
        / "distributions" / str(task.owner_id) / "tasks" / str(task.id) / str(output_index)
    )


def _release_paths(task: GenerationTask, output_index: int, release_id: str, media_kind: str) -> tuple[Path, Path, Path]:
    if not _SHA256_RE.fullmatch(release_id):
        raise DistributionError("Distribution release identifier is invalid")
    parent = _distribution_parent(task, output_index)
    release_dir = (parent / release_id).resolve()
    if not release_dir.is_relative_to(parent.resolve()):
        raise DistributionError("Distribution path crosses its tenant boundary")
    suffix = ".mp4" if media_kind == "video" else ".png"
    return release_dir, release_dir / f"public{suffix}", release_dir / "provenance.json"


def _lock_stale_seconds() -> float:
    try:
        value = resolve_configuration(required_keys=(
            "runtime.distribution_lock_stale_seconds",
        )).values["runtime.distribution_lock_stale_seconds"]
    except (ConfigurationResolutionError, ValidationError) as exc:
        raise DistributionError("distribution lock stale configuration is unavailable") from exc
    if type(value) is not int or value < 1:
        raise DistributionError("runtime.distribution_lock_stale_seconds is invalid")
    return float(value)


def _lock_document(owner_token: str) -> dict:
    now = time.time()
    return {
        "owner_token": owner_token,
        "pid": os.getpid(),
        "started_at_epoch": now,
        "started_at": timezone.now().isoformat(),
    }


def _try_create_lock(path: Path, owner_token: str) -> bool:
    try:
        handle = os.open(str(path), os.O_CREAT | os.O_EXCL | os.O_WRONLY)
    except FileExistsError:
        return False
    try:
        os.write(handle, _canonical_bytes(_lock_document(owner_token)) + b"\n")
        os.fsync(handle)
    except Exception:
        os.close(handle)
        try:
            path.unlink(missing_ok=True)
        except OSError:
            pass
        raise
    os.close(handle)
    return True


def _lock_is_stale(path: Path) -> bool:
    try:
        stat = path.stat()
    except OSError:
        return False
    now = time.time()
    mtime_age = max(0.0, now - stat.st_mtime)
    try:
        payload = json.loads(path.read_text(encoding="utf-8"))
        started = float(payload.get("started_at_epoch"))
        # A corrupt/future timestamp cannot pin a lock indefinitely; fall back
        # to the filesystem timestamp when it is implausible.
        if not 0 < started <= now:
            raise ValueError
        age = min(mtime_age, max(0.0, now - started))
    except (OSError, ValueError, TypeError, json.JSONDecodeError, AttributeError):
        age = mtime_age
    return age > _lock_stale_seconds()


def _unlink_owned_lock(path: Path, owner_token: str) -> None:
    try:
        payload = json.loads(path.read_text(encoding="utf-8"))
        if payload.get("owner_token") != owner_token:
            return
        path.unlink(missing_ok=True)
    except (OSError, json.JSONDecodeError, AttributeError):
        return


def _reclaim_stale_lock(path: Path) -> bool:
    """Serialize stale recovery and never remove a lock observed as fresh."""

    if not _lock_is_stale(path):
        return False
    recovery_path = path.with_name(f"{path.name}.recovery")
    recovery_owner = uuid.uuid4().hex
    if not _try_create_lock(recovery_path, recovery_owner):
        # Recovery itself can also be orphaned by a process crash.
        if not _lock_is_stale(recovery_path):
            return False
        try:
            recovery_path.unlink(missing_ok=True)
        except OSError:
            return False
        if not _try_create_lock(recovery_path, recovery_owner):
            return False
    try:
        # Re-read under the recovery mutex. Another recovery winner may already
        # have installed a fresh producer lock; that lock must never be removed.
        if not _lock_is_stale(path):
            return not path.exists()
        try:
            path.unlink(missing_ok=True)
        except OSError:
            return False
        return not path.exists()
    finally:
        _unlink_owned_lock(recovery_path, recovery_owner)


def _output_distribution(task: GenerationTask, output_index: int) -> dict:
    if not isinstance(task.outputs, list) or not 0 <= output_index < len(task.outputs):
        raise DistributionError("Task output index is unavailable")
    item = task.outputs[output_index]
    if not isinstance(item, dict):
        raise DistributionError("Task output is malformed")
    record = item.get("distribution")
    if not isinstance(record, dict):
        raise DistributionError("Task output has no managed distribution copy")
    return record


def distribution_artifact_path(task: GenerationTask, output_index: int) -> Path:
    record = _output_distribution(task, output_index)
    media_kind = str(record.get("media_kind") or "")
    if media_kind not in {"video", "image"}:
        raise DistributionError("Managed distribution media kind is invalid")
    _, artifact, _ = _release_paths(
        task, output_index, str(record.get("release_id") or ""), media_kind,
    )
    return artifact


def distribution_manifest_path(task: GenerationTask, output_index: int) -> Path:
    record = _output_distribution(task, output_index)
    media_kind = str(record.get("media_kind") or "")
    if media_kind not in {"video", "image"}:
        raise DistributionError("Managed distribution media kind is invalid")
    _, _, manifest = _release_paths(
        task, output_index, str(record.get("release_id") or ""), media_kind,
    )
    return manifest


def _video_metadata(path: Path, *, media_profile: dict) -> dict:
    profile = validate_distribution_profile(media_profile)
    command = [
        str(getattr(settings, "FFPROBE_BINARY", "ffprobe")),
        "-v", "error", "-show_streams", "-show_format", "-of", "json", str(path),
    ]
    try:
        result = subprocess.run(
            command, capture_output=True, text=True, encoding="utf-8", errors="strict",
            timeout=profile["probe_timeout_seconds"],
        )
    except (OSError, subprocess.SubprocessError) as exc:
        raise DistributionError("FFprobe could not verify the managed video copy") from exc
    if result.returncode != 0:
        raise DistributionError("FFprobe rejected the managed video copy")
    try:
        payload = json.loads(result.stdout)
    except (TypeError, json.JSONDecodeError) as exc:
        raise DistributionError("FFprobe returned invalid verification data") from exc
    streams = payload.get("streams") if isinstance(payload, dict) else None
    if not isinstance(streams, list) or not any(row.get("codec_type") == "video" for row in streams if isinstance(row, dict)):
        raise DistributionError("Managed video copy has no video stream")
    tags = ((payload.get("format") or {}).get("tags") or {}) if isinstance(payload, dict) else {}
    normalized = {str(key).casefold(): str(value) for key, value in tags.items()}
    return {
        "comment": normalized.get("comment", ""),
        "description": normalized.get("description", ""),
    }


def _render_video(
    source: Path, target: Path, *, label: str, description: str, font: Path,
    media_profile: dict,
) -> dict:
    profile = validate_distribution_profile(media_profile)
    overlay = profile["video_overlay"]
    drawtext = (
        f"drawtext=fontfile='{_drawtext_value(font.as_posix())}':"
        f"text='{_drawtext_value(label)}':fontcolor={overlay['font_color']}:"
        f"fontsize=h/{overlay['font_size_divisor']}:box=1:"
        f"boxcolor={overlay['box_color']}@{overlay['box_opacity']}:"
        f"boxborderw={overlay['box_border_width_px']}:"
        f"x=w-tw-{overlay['margin_x_px']}:y=h-th-{overlay['margin_y_px']}"
    )
    command = [
        str(settings.FFMPEG_BINARY), "-hide_banner", "-loglevel", "error", "-nostdin", "-y",
        "-i", str(source), "-map_metadata", "-1", "-map", "0:v:0", "-map", "0:a?",
        "-vf", drawtext, "-c:v", profile["video_codec"],
        "-preset", profile["video_preset"], "-crf", str(profile["video_crf"]),
        "-pix_fmt", profile["pixel_format"], "-c:a", profile["audio_codec"],
        "-b:a", f"{profile['audio_bitrate_kbps']}k",
        "-ar", str(profile["audio_sample_rate_hz"]), "-sn", "-dn",
        "-metadata", f"comment={label}",
        "-metadata", f"description={description}",
        "-movflags", "+faststart", str(target),
    ]
    try:
        result = subprocess.run(
            command, capture_output=True, text=True, encoding="utf-8", errors="replace",
            timeout=profile["render_timeout_seconds"],
        )
    except subprocess.TimeoutExpired as exc:
        raise DistributionError("Managed video copy timed out before publication") from exc
    except OSError as exc:
        raise DistributionError("FFmpeg could not create the managed video copy") from exc
    if result.returncode != 0 or not target.is_file() or target.stat().st_size <= 0:
        raise DistributionError("FFmpeg failed to create the managed video copy")
    metadata = _video_metadata(target, media_profile=profile)
    normalized_comment = unicodedata.normalize("NFC", metadata.get("comment", "")).strip()
    normalized_description = unicodedata.normalize(
        "NFC", metadata.get("description", ""),
    ).strip()
    if (
        normalized_comment != unicodedata.normalize("NFC", label).strip()
        or normalized_description != unicodedata.normalize("NFC", description).strip()
    ):
        raise DistributionError(
            "Managed video copy is missing required AI disclosure metadata "
            f"(comment={metadata.get('comment')!r}, description={metadata.get('description')!r})"
        )
    return {
        "media_profile": profile,
        "visible_overlay": True,
        "overlay_filter": "configured bottom-right boxed drawtext",
        "metadata_verified": True,
    }


def _fit_font(
    font_path: Path,
    label: str,
    width: int,
    height: int,
    *,
    image_overlay: dict,
) -> tuple[ImageFont.FreeTypeFont, tuple[int, int, int, int]]:
    minimum_size = image_overlay["minimum_font_size_px"]
    maximum_width = max(minimum_size, width - image_overlay["width_inset_px"])
    size = max(minimum_size, min(width, height) // image_overlay["font_size_divisor"])
    while True:
        font = ImageFont.truetype(str(font_path), size)
        bounds = font.getbbox(label)
        if bounds[2] - bounds[0] <= maximum_width or size <= minimum_size:
            return font, bounds
        size -= 1


def _render_image(
    source: Path, target: Path, *, label: str, description: str, font: Path,
    source_sha256: str, version: str, media_profile: dict,
) -> dict:
    profile = validate_distribution_profile(media_profile)
    image_overlay = profile["image_overlay"]
    try:
        with Image.open(source) as opened:
            opened.load()
            base = ImageOps.exif_transpose(opened).convert("RGBA")
    except (OSError, UnidentifiedImageError) as exc:
        raise DistributionError("Source image cannot be decoded for managed publication") from exc
    width, height = base.size
    if (
        width < image_overlay["minimum_source_dimension_px"]
        or height < image_overlay["minimum_source_dimension_px"]
    ):
        raise DistributionError("Source image is too small for a clear AI disclosure label")
    font_object, bounds = _fit_font(
        font, label, width, height, image_overlay=image_overlay,
    )
    text_width = bounds[2] - bounds[0]
    text_height = bounds[3] - bounds[1]
    padding = max(
        image_overlay["minimum_padding_px"],
        min(width, height) // image_overlay["padding_divisor"],
    )
    margin = max(
        image_overlay["minimum_margin_px"],
        min(width, height) // image_overlay["margin_divisor"],
    )
    left = max(0, width - margin - text_width - padding * 2)
    top = max(0, height - margin - text_height - padding * 2)
    right = min(width, left + text_width + padding * 2)
    bottom = min(height, top + text_height + padding * 2)
    overlay = Image.new("RGBA", base.size, (0, 0, 0, 0))
    draw = ImageDraw.Draw(overlay)
    draw.rounded_rectangle(
        (left, top, right, bottom),
        radius=max(image_overlay["minimum_corner_radius_px"], padding),
        fill=tuple(image_overlay["box_rgba"]),
    )
    draw.text(
        (left + padding - bounds[0], top + padding - bounds[1]),
        label, font=font_object, fill=tuple(image_overlay["text_rgba"]),
    )
    rendered = Image.alpha_composite(base, overlay).convert("RGB")
    metadata = PngImagePlugin.PngInfo()
    metadata.add_text("Comment", label)
    metadata.add_text("Description", description)
    metadata.add_text("Software", f"AutoPrompt managed distribution copy/{version}")
    metadata.add_text("SourceArtifactSHA256", source_sha256)
    metadata.add_text("DistributionVersion", version)
    rendered.save(target, format="PNG", pnginfo=metadata, optimize=True)
    try:
        with Image.open(target) as verification:
            verification.load()
            info = {str(key): str(value) for key, value in verification.info.items()}
            output_rgb = verification.convert("RGB")
    except (OSError, UnidentifiedImageError) as exc:
        raise DistributionError("Managed image copy could not be verified") from exc
    required = {
        "Comment": label,
        "Description": description,
        "SourceArtifactSHA256": source_sha256,
        "DistributionVersion": version,
    }
    if any(info.get(key) != value for key, value in required.items()):
        raise DistributionError("Managed image copy is missing required AI disclosure metadata")
    source_rgb = base.convert("RGB")
    difference = ImageChops.difference(source_rgb, output_rgb).getbbox()
    if difference is None or not (
        difference[2] > left and difference[0] < right
        and difference[3] > top and difference[1] < bottom
    ):
        raise DistributionError("Managed image copy has no verifiable visible disclosure pixels")
    return {
        "profile": "png-rgba-visible-box-v1",
        "media_profile": profile,
        "visible_overlay": True,
        "label_bbox": [left, top, right, bottom],
        "metadata_verified": True,
    }


def _manifest_body(
    task: GenerationTask, output_index: int, *, release_id: str, media_kind: str,
    source_manifest: dict, artifact: Path, config: dict, render: dict,
) -> dict:
    return {
        "schema": DISTRIBUTION_SCHEMA,
        "subject": {
            "type": "generation_task_distribution_copy",
            "id": f"{task.id}:{output_index}:{release_id}",
        },
        "disclosure": {
            "ai_generated": True,
            "label": config["label"],
            "description": config["description"],
            "version": config["version"],
            "method": "visible-overlay+embedded-metadata+mandatory-signed-sidecar",
            "visible_overlay": True,
            "embedded_metadata": True,
        },
        "artifact": {
            "media_kind": media_kind,
            "filename": artifact.name,
            "sha256": sha256_file(artifact),
            "byte_size": artifact.stat().st_size,
            "published_at": timezone.now().isoformat(),
        },
        "source_artifact": {
            "task_id": str(task.id),
            "output_index": output_index,
            "sha256": source_manifest["artifact"]["sha256"],
            "manifest_sha256": source_manifest["integrity"]["payload_sha256"],
        },
        "release": {
            "release_id": release_id,
            "project_id": str(task.project_id),
            "episode_id": str(task.episode_id) if task.episode_id else None,
            "font_sha256": config["font_sha256"],
            "model_key": config["model_key"],
            "model": config["model"],
            "render": render,
        },
    }


def _record_from_document(document: dict) -> dict:
    return {
        "status": "ready",
        "schema": DISTRIBUTION_SCHEMA,
        "release_id": document["release"]["release_id"],
        "media_kind": document["artifact"]["media_kind"],
        "label": document["disclosure"]["label"],
        "description": document["disclosure"]["description"],
        "version": document["disclosure"]["version"],
        "model_key": document["release"]["model_key"],
        "model": document["release"]["model"],
        "source_artifact_sha256": document["source_artifact"]["sha256"],
        "artifact_sha256": document["artifact"]["sha256"],
        "manifest_sha256": document["integrity"]["payload_sha256"],
        "published_at": document["artifact"]["published_at"],
    }


def _publication_guard(task: GenerationTask, output_index: int) -> dict:
    if (task.status != GenerationTask.Status.SUCCEEDED or type(output_index) is not int
            or not isinstance(task.outputs, list) or not 0 <= output_index < len(task.outputs)
            or not isinstance(task.outputs[output_index], dict)):
        raise DistributionError("Successful task output index is unavailable")
    item = deepcopy(task.outputs[output_index])
    distribution = item.pop("distribution", None)
    return {"owner_id": task.owner_id, "project_id": task.project_id, "kind": task.kind,
        "source_sha256": _canonical_sha256(item), "distribution": distribution,
        "claim": task.distribution_claim, "revision": task.distribution_revision}


def _require_publication_lock(path: Path, owner_token: str) -> None:
    try:
        document = json.loads(path.read_text(encoding="utf-8"))
        if not isinstance(document, dict) or document.get("owner_token") != owner_token:
            raise ValueError
    except (OSError, ValueError) as exc:
        raise DistributionClaimLost("Distribution publisher no longer owns its processing lock") from exc


class _PublicationLockHeartbeat(WorkerHeartbeat):
    def __init__(self, path, owner_token):
        super().__init__(f"publication-{owner_token}")
        if self.interval >= _lock_stale_seconds():
            raise DistributionError("Distribution stale-lock interval must exceed the task heartbeat interval")
        self.path, self.owner_token = path, owner_token

    def _touch(self):
        _require_publication_lock(self.path, self.owner_token)
        os.utime(self.path, None)


def _persist_record(task_id, output_index: int, record: dict, *, expected: dict,
        lock: Path | None = None, lock_owner: str = "") -> GenerationTask:
    with transaction.atomic():
        current = GenerationTask.objects.select_for_update().get(pk=task_id)
        if lock is not None:
            _require_publication_lock(lock, lock_owner)
        try:
            actual = _publication_guard(current, output_index)
        except DistributionError as exc:
            raise DistributionClaimLost("Task output no longer permits distribution publication") from exc
        if actual != expected:
            raise DistributionClaimLost("Task source or distribution changed before publication")
        outputs = deepcopy(current.outputs)
        item = outputs[output_index]
        item["distribution"] = record
        current.outputs = outputs
        current.save(update_fields=("outputs", "updated_at"))
        return current


def record_distribution_failure(task: GenerationTask, output_index: int, exc: BaseException, *, expected: dict,
        lock: Path | None = None, lock_owner: str = "") -> None:
    message = str(exc).strip() or exc.__class__.__name__
    # The persisted/API-visible message is deliberately bounded and contains no
    # source/output paths or FFmpeg stderr.
    safe_message = message[:500]
    try:
        _persist_record(task.pk, output_index, {
            "status": "failed",
            "schema": DISTRIBUTION_SCHEMA,
            "error": safe_message,
            "failed_at": timezone.now().isoformat(),
        }, expected=expected, lock=lock, lock_owner=lock_owner)
    except (GenerationTask.DoesNotExist, DistributionClaimLost):
        # The caller still receives failure; no late write to a newer fact.
        return


def _verify_document(task: GenerationTask, output_index: int, document: dict, artifact: Path, *, expected: dict | None = None) -> dict:
    if not isinstance(document, dict):
        raise DistributionError("Distribution provenance is not a JSON object")
    integrity = document.get("integrity")
    if not isinstance(integrity, dict):
        raise DistributionError("Distribution provenance signature is missing")
    body = deepcopy(document)
    body.pop("integrity", None)
    payload_sha256 = _canonical_sha256(body)
    if payload_sha256 != str(integrity.get("payload_sha256") or ""):
        raise DistributionError("Distribution provenance payload hash does not match")
    try:
        signed_payload = signing.Signer(salt=DISTRIBUTION_SIGNING_SALT).unsign(
            str(integrity.get("signature") or ""),
        )
    except signing.BadSignature as exc:
        raise DistributionError("Distribution provenance signature is invalid") from exc
    if signed_payload != payload_sha256:
        raise DistributionError("Distribution provenance signature does not cover the payload")
    release = body.get("release") or {}
    release_id = str(release.get("release_id") or "")
    subject = body.get("subject") or {}
    if subject != {
        "type": "generation_task_distribution_copy",
        "id": f"{task.id}:{output_index}:{release_id}",
    }:
        raise DistributionError("Distribution provenance subject does not match")
    disclosure = body.get("disclosure") or {}
    artifact_body = body.get("artifact") or {}
    source = verified_task_manifest(task, output_index)
    expected_profile = _task_disclosure(
        task, str(artifact_body.get("media_kind") or ""), generation=source["generation"],
    )
    if (
        disclosure.get("ai_generated") is not True
        or disclosure.get("visible_overlay") is not True
        or disclosure.get("embedded_metadata") is not True
        or disclosure.get("label") != expected_profile["label"]
        or disclosure.get("version") != _valid_version()
        or disclosure.get("description") != expected_profile["description"]
        or (body.get("release") or {}).get("model_key") != expected_profile["model_key"]
        or (body.get("release") or {}).get("model") != expected_profile["model"]
    ):
        raise DistributionError("Distribution disclosure does not match the approved configuration")
    source_artifact = body.get("source_artifact") or {}
    if (
        source_artifact.get("sha256") != source["artifact"]["sha256"]
        or source_artifact.get("manifest_sha256") != source["integrity"]["payload_sha256"]
    ):
        raise DistributionError("Distribution copy is not bound to the current source master")
    if not artifact.is_file() or sha256_file(artifact) != artifact_body.get("sha256"):
        raise DistributionError("Managed distribution artifact has changed since publication")
    if artifact.stat().st_size != artifact_body.get("byte_size"):
        raise DistributionError("Managed distribution artifact size does not match provenance")
    if expected:
        fields = {
            "release_id": release_id,
            "media_kind": artifact_body.get("media_kind"),
            "label": disclosure.get("label"),
            "description": disclosure.get("description"),
            "version": disclosure.get("version"),
            "model_key": release.get("model_key"),
            "model": release.get("model"),
            "source_artifact_sha256": source_artifact.get("sha256"),
            "artifact_sha256": artifact_body.get("sha256"),
            "manifest_sha256": payload_sha256,
        }
        if any(str(expected.get(key) or "") != str(value or "") for key, value in fields.items()):
            raise DistributionError("Persisted distribution record does not match signed provenance")
    return {
        **document,
        "verification": {
            "status": "verified",
            "signature_valid": True,
            "source_hash_valid": True,
            "artifact_hash_valid": True,
            "verified_at": timezone.now().isoformat(),
        },
    }


def verified_distribution_manifest(task: GenerationTask, output_index: int) -> dict:
    record = _output_distribution(task, output_index)
    if record.get("status") != "ready":
        raise DistributionError("Managed distribution copy is not ready")
    artifact = distribution_artifact_path(task, output_index)
    manifest = distribution_manifest_path(task, output_index)
    try:
        document = json.loads(manifest.read_text(encoding="utf-8"))
    except (OSError, json.JSONDecodeError) as exc:
        raise DistributionError("Distribution provenance sidecar is unavailable") from exc
    return _verify_document(task, output_index, document, artifact, expected=record)


def prepare_task_distribution(task: GenerationTask, output_index: int, *, claim=None) -> dict:
    """Create or idempotently reuse one managed task-output distribution copy."""

    task = GenerationTask.objects.get(pk=task.pk)
    if claim is not None and (task.distribution_claim != claim or task.distribution_state != "running"):
        raise DistributionClaimLost("Distribution task claim has expired")
    if task.status != GenerationTask.Status.SUCCEEDED:
        raise DistributionError("Only a successful generation task can be published")
    expected = _publication_guard(task, output_index)
    item = task.outputs[output_index]
    media_kind = classify_task_output(item)
    if media_kind not in {"video", "image"}:
        raise DistributionError("Only video and image outputs support managed public copies")
    try:
        try:
            source_manifest = verified_task_manifest(task, output_index)
        except ProvenanceError as exc:
            raise DistributionError("Source master provenance could not be verified") from exc
        source = resolve_task_output_path(task, output_index)
        source_sha256 = source_manifest["artifact"]["sha256"]
        font = _resolve_font()
        try:
            media_profile = resolve_distribution_media_profile(project=task.project)
        except MediaProcessingProfileError as exc:
            raise DistributionError(str(exc)) from exc
        disclosure = _task_disclosure(task, media_kind, generation=source_manifest["generation"])
        config = _release_config(
            source_sha256=source_sha256,
            media_kind=media_kind,
            font_sha256=sha256_file(font),
            disclosure=disclosure,
            media_profile=media_profile,
        )
    except DistributionError as exc:
        record_distribution_failure(task, output_index, exc, expected=expected)
        raise
    release_id = _canonical_sha256(config)
    parent = _distribution_parent(task, output_index)
    release_dir, artifact, manifest = _release_paths(task, output_index, release_id, media_kind)
    parent.mkdir(parents=True, exist_ok=True)
    lock = parent / f".{release_id}.lock"
    lock_owner = uuid.uuid4().hex
    if not _try_create_lock(lock, lock_owner):
        # A completed copy is still idempotently reusable. A live/incomplete
        # producer is reported explicitly instead of exposing partial bytes.
        try:
            task.refresh_from_db(fields=("outputs",))
            return verified_distribution_manifest(task, output_index)
        except (DistributionError, ProvenanceError, OSError):
            if not _reclaim_stale_lock(lock) or not _try_create_lock(lock, lock_owner):
                raise DistributionError("A managed distribution copy is already being prepared")
    temporary_dir = parent / f".{release_id}.{uuid.uuid4().hex}.tmp"
    heartbeat = None
    try:
        heartbeat = _PublicationLockHeartbeat(lock, lock_owner)
        heartbeat.__enter__()
        task.refresh_from_db(fields=("outputs",))
        # A previous producer may have committed between our initial read and
        # acquiring the per-release lock. Reuse its exact record, never adopt
        # a changed master as if it were this render's source.
        current_guard = _publication_guard(task, output_index)
        if {k: v for k, v in current_guard.items() if k != "distribution"} != {
                k: v for k, v in expected.items() if k != "distribution"}:
            raise DistributionClaimLost("Task source changed before distribution rendering")
        expected = current_guard
        _require_publication_lock(lock, lock_owner)
        try:
            existing = verified_distribution_manifest(task, output_index)
            if existing["release"]["release_id"] == release_id:
                return existing
        except (DistributionError, ProvenanceError, OSError):
            pass
        if release_dir.exists():
            # A process can crash after the atomic directory publication but
            # before the JSONField pointer is committed. Adopt only a complete,
            # correctly signed release for this exact source/config; anything
            # else remains fail-closed and is never overwritten.
            try:
                document = json.loads(manifest.read_text(encoding="utf-8"))
                verified = _verify_document(task, output_index, document, artifact)
                if verified["release"]["release_id"] != release_id:
                    raise DistributionError("Existing managed distribution release does not match configuration")
                record = _record_from_document(verified)
                task = _persist_record(task.pk, output_index, record, expected=expected, lock=lock, lock_owner=lock_owner)
                return verified_distribution_manifest(task, output_index)
            except (OSError, json.JSONDecodeError, KeyError, DistributionError) as exc:
                raise DistributionError(
                    "Existing managed distribution release failed verification"
                ) from exc
        temporary_dir.mkdir(parents=False)
        temporary_artifact = temporary_dir / artifact.name
        if media_kind == "video":
            render = _render_video(
                source, temporary_artifact, label=config["label"],
                description=config["description"], font=font,
                media_profile=media_profile.value,
            )
        else:
            render = _render_image(
                source, temporary_artifact, label=config["label"],
                description=config["description"], font=font,
                source_sha256=source_sha256, version=config["version"],
                media_profile=media_profile.value,
            )
        # The renderer is not allowed to mutate the ComfyUI master.
        source_after = verified_task_manifest(task, output_index)
        if source_after["artifact"]["sha256"] != source_sha256:
            raise DistributionError("Source master changed while its distribution copy was rendered")
        document = _seal(_manifest_body(
            task, output_index, release_id=release_id, media_kind=media_kind,
            source_manifest=source_after, artifact=temporary_artifact,
            config=config, render=render,
        ))
        _write_json(temporary_dir / manifest.name, document)
        # Verify the complete temporary release before one atomic directory
        # rename makes media and provenance visible together.
        _verify_document(task, output_index, document, temporary_artifact)
        _require_publication_lock(lock, lock_owner)
        os.replace(temporary_dir, release_dir)
        record = _record_from_document(document)
        task = _persist_record(task.pk, output_index, record, expected=expected, lock=lock, lock_owner=lock_owner)
        return verified_distribution_manifest(task, output_index)
    except DistributionClaimLost:
        raise
    except Exception as exc:
        if isinstance(exc, DistributionError):
            record_distribution_failure(task, output_index, exc, expected=expected, lock=lock, lock_owner=lock_owner)
            raise
        wrapped = DistributionError("Managed distribution copy could not be published")
        record_distribution_failure(task, output_index, wrapped, expected=expected, lock=lock, lock_owner=lock_owner)
        raise wrapped from exc
    finally:
        if heartbeat is not None:
            heartbeat.__exit__(None, None, None)
        shutil.rmtree(temporary_dir, ignore_errors=True)
        _unlink_owned_lock(lock, lock_owner)


def prepare_all_task_distributions(task: GenerationTask, *, claim=None) -> list[str]:
    """Best-effort post-generation publication; masters remain successful on failure."""

    errors: list[str] = []
    if not isinstance(task.outputs, list) or not any(classify_task_output(item) in {"video", "image"} for item in task.outputs):
        raise DistributionError("Successful task has no distributable output")
    for index, item in enumerate(task.outputs or []):
        if classify_task_output(item) not in {"video", "image"}:
            continue
        try:
            prepare_task_distribution(task, index, claim=claim)
        except DistributionClaimLost:
            raise
        except DistributionError as exc:
            errors.append(f"output {index}: {exc}")
        task.refresh_from_db(fields=("outputs",))
    return errors
