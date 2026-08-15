from __future__ import annotations

from datetime import timedelta
import os
from pathlib import Path
import socket
import subprocess
import time
import uuid

from django.conf import settings
from django.db import transaction
from django.db.models import F, Q
from django.utils import timezone

from studio.models import AssemblyJob, SeriesProject, WorkerLease
from studio.services.assembly import AssemblyError, _mark_scope_complete
from studio.services.provenance import (
    ProvenanceError,
    persist_assembly_provenance,
    verified_assembly_manifest,
)


ASSEMBLY_WORKER_HEARTBEAT_KEY = "assembly-worker-heartbeat"


def default_worker_id() -> str:
    return f"{socket.gethostname()}:{os.getpid()}"[:160]


def claim_next_assembly(*, worker_id: str | None = None) -> AssemblyJob | None:
    """Atomically claim the oldest executable assembly job.

    A durable mutex keeps the implementation correct on databases that do not
    support ``SKIP LOCKED`` while the conditional lifecycle update protects the
    claim if multiple worker processes wake at the same time.
    """

    claimed_by = (worker_id or default_worker_id())[:160]
    now = timezone.now()
    with transaction.atomic():
        WorkerLease.objects.get_or_create(key="assembly-claim")
        WorkerLease.objects.select_for_update().get(key="assembly-claim")
        AssemblyJob.objects.filter(
            status=AssemblyJob.Status.QUEUED,
            dry_run=False,
            attempts__gte=F("max_attempts"),
        ).update(
            status=AssemblyJob.Status.FAILED,
            error_message="Assembly retry limit was reached before the job could be claimed",
            finished_at=now,
            heartbeat_at=now,
            updated_at=now,
        )
        job = (
            AssemblyJob.objects.select_for_update()
            .select_related("project", "episode", "owner")
            .filter(
                status=AssemblyJob.Status.QUEUED,
                dry_run=False,
                attempts__lt=F("max_attempts"),
            )
            .order_by("queued_at", "created_at")
            .first()
        )
        if job is None:
            return None
        job.status = AssemblyJob.Status.RUNNING
        job.attempts += 1
        job.worker_id = claimed_by
        job.claim_token = uuid.uuid4()
        job.heartbeat_at = now
        if job.started_at is None:
            job.started_at = now
        job.finished_at = None
        job.error_message = ""
        job.save(update_fields=(
            "status", "attempts", "worker_id", "claim_token", "heartbeat_at",
            "started_at", "finished_at", "error_message", "updated_at",
        ))
        return job


def _safe_output_path(job: AssemblyJob) -> Path:
    if not job.output_path:
        raise AssemblyError("组装任务没有持久化输出路径")
    output = Path(job.output_path).resolve()
    allowed_root = (
        Path(settings.MEDIA_ROOT).resolve()
        / "assemblies"
        / str(job.owner_id)
        / str(job.project_id)
    ).resolve()
    try:
        output.relative_to(allowed_root)
    except ValueError as exc:
        raise AssemblyError("组装任务输出路径越过当前用户和项目目录") from exc
    return output


def _valid_output(job: AssemblyJob) -> bool:
    try:
        output = _safe_output_path(job)
        if not output.is_file() or output.stat().st_size <= 0:
            return False
        verified_assembly_manifest(job)
        return True
    except (AssemblyError, ProvenanceError, OSError):
        return False


def _temporary_output_path(output: Path, claim_token: uuid.UUID) -> Path:
    return output.with_name(f"{output.stem}.part-{claim_token.hex}{output.suffix}")


def _run_ffmpeg(command: list[str], *, heartbeat) -> subprocess.CompletedProcess[str]:
    """Run FFmpeg while persisting the job heartbeat from the main process.

    Repeated ``communicate(timeout=...)`` calls avoid pipe deadlocks and allow a
    separate worker to distinguish a live long encode from an abandoned claim.
    """

    timeout_seconds = max(1.0, float(settings.ASSEMBLY_TIMEOUT_SECONDS))
    heartbeat_seconds = max(
        0.25, float(getattr(settings, "ASSEMBLY_HEARTBEAT_SECONDS", 5)),
    )
    process = subprocess.Popen(
        command,
        stdout=subprocess.PIPE,
        stderr=subprocess.PIPE,
        text=True,
    )
    deadline = time.monotonic() + timeout_seconds
    while True:
        remaining = deadline - time.monotonic()
        if remaining <= 0:
            process.kill()
            stdout, stderr = process.communicate()
            raise subprocess.TimeoutExpired(command, timeout_seconds, stdout, stderr)
        try:
            stdout, stderr = process.communicate(timeout=min(heartbeat_seconds, remaining))
            return subprocess.CompletedProcess(command, process.returncode, stdout, stderr)
        except subprocess.TimeoutExpired:
            heartbeat()


def _persist_job_heartbeat(job_id, claim_token) -> bool:
    return bool(AssemblyJob.objects.filter(
        pk=job_id,
        status=AssemblyJob.Status.RUNNING,
        claim_token=claim_token,
    ).update(heartbeat_at=timezone.now(), updated_at=timezone.now()))


def _failure_message(exc: BaseException) -> str:
    if isinstance(exc, subprocess.TimeoutExpired):
        return f"FFmpeg timed out after {float(exc.timeout):g} seconds"
    return str(exc).strip() or exc.__class__.__name__


def _record_execution_failure(job: AssemblyJob, claim_token, message: str) -> AssemblyJob:
    now = timezone.now()
    bounded = message[-4000:]
    with transaction.atomic():
        current = AssemblyJob.objects.select_for_update().get(pk=job.pk)
        if current.status != AssemblyJob.Status.RUNNING or current.claim_token != claim_token:
            return current
        if current.attempts < current.max_attempts:
            current.status = AssemblyJob.Status.QUEUED
            current.queued_at = now
            current.worker_id = ""
            current.claim_token = None
            current.heartbeat_at = None
            current.finished_at = None
            current.error_message = (
                f"Attempt {current.attempts}/{current.max_attempts} failed; queued for retry: {bounded}"
            )[-4000:]
            current.save(update_fields=(
                "status", "queued_at", "worker_id", "claim_token", "heartbeat_at",
                "finished_at", "error_message", "updated_at",
            ))
        else:
            current.status = AssemblyJob.Status.FAILED
            current.finished_at = now
            current.heartbeat_at = now
            current.error_message = (
                f"Attempt {current.attempts}/{current.max_attempts} failed: {bounded}"
            )[-4000:]
            current.save(update_fields=(
                "status", "finished_at", "heartbeat_at", "error_message", "updated_at",
            ))
        return current


def _mark_succeeded(job: AssemblyJob, claim_token) -> AssemblyJob:
    now = timezone.now()
    with transaction.atomic():
        locked_project = SeriesProject.objects.select_for_update().get(pk=job.project_id)
        current = (
            AssemblyJob.objects.select_for_update()
            .select_related("project", "episode")
            .get(pk=job.pk)
        )
        if current.status == AssemblyJob.Status.SUCCEEDED:
            return current
        if current.status != AssemblyJob.Status.RUNNING or current.claim_token != claim_token:
            return current
        current.status = AssemblyJob.Status.SUCCEEDED
        current.finished_at = now
        current.heartbeat_at = now
        current.error_message = ""
        current.save(update_fields=(
            "status", "finished_at", "heartbeat_at", "error_message", "updated_at",
        ))
        _mark_scope_complete(locked_project, current.episode)
        return current


def process_assembly_job(job: AssemblyJob) -> AssemblyJob:
    """Execute one claimed job and persist every terminal or retry transition."""

    job = AssemblyJob.objects.select_related("project", "episode", "owner").get(pk=job.pk)
    claim_token = job.claim_token
    if job.status != AssemblyJob.Status.RUNNING or claim_token is None:
        raise AssemblyError("Only a currently claimed assembly job can be executed", job=job)

    temporary_output: Path | None = None
    try:
        output = _safe_output_path(job)
        if _valid_output(job):
            return _mark_succeeded(job, claim_token)
        command = list(job.ffmpeg_command or [])
        if len(command) < 2:
            raise AssemblyError("组装任务没有持久化 FFmpeg 命令")
        command_output = Path(str(command[-1])).resolve()
        if command_output != output:
            raise AssemblyError("持久化 FFmpeg 命令与任务输出路径不一致")
        output.parent.mkdir(parents=True, exist_ok=True)
        temporary_output = _temporary_output_path(output, claim_token)
        if temporary_output.exists():
            temporary_output.unlink()
        runtime_command = [str(value) for value in command]
        runtime_command[-1] = str(temporary_output)

        def heartbeat():
            if not _persist_job_heartbeat(job.pk, claim_token):
                raise AssemblyError("组装任务领取已失效，停止提交执行结果")

        heartbeat()
        result = _run_ffmpeg(runtime_command, heartbeat=heartbeat)
        if result.returncode != 0:
            raise AssemblyError(
                (result.stderr or result.stdout or "FFmpeg exited with an error")[-4000:]
            )
        if not temporary_output.is_file() or temporary_output.stat().st_size == 0:
            raise AssemblyError("FFmpeg 未生成有效输出文件")
        heartbeat()
        os.replace(temporary_output, output)
        temporary_output = None
        persist_assembly_provenance(job, generated_at=timezone.now())
        return _mark_succeeded(job, claim_token)
    except (OSError, subprocess.SubprocessError, AssemblyError, ProvenanceError) as exc:
        return _record_execution_failure(job, claim_token, _failure_message(exc))
    finally:
        if temporary_output is not None:
            try:
                temporary_output.unlink(missing_ok=True)
            except OSError:
                pass


def recover_stale_assemblies(
    stale_minutes: float,
    *,
    reason: str = "Recovered an abandoned assembly worker claim",
) -> dict[str, int]:
    """Recover stale RUNNING jobs without duplicating their final output.

    FFmpeg always writes to a claim-specific temporary file and the worker only
    atomically replaces the final path after a successful exit.  Therefore a
    stale claim with no final output is safe to requeue under a new token.
    """

    now = timezone.now()
    threshold = now - timedelta(minutes=max(0.0, float(stale_minutes)))
    counts = {"succeeded": 0, "requeued": 0, "failed": 0}
    completed: list[AssemblyJob] = []
    stale_temporary_paths: list[Path] = []
    stale_jobs = (
        AssemblyJob.objects.filter(dry_run=False)
        .filter(
            Q(status=AssemblyJob.Status.RUNNING)
            | Q(status=AssemblyJob.Status.PLANNED)
        )
        .filter(
            Q(heartbeat_at__lte=threshold)
            | Q(heartbeat_at__isnull=True, updated_at__lte=threshold)
        )
    )
    with transaction.atomic():
        project_ids = list(
            stale_jobs.order_by().values_list("project_id", flat=True).distinct()
        )
        list(
            SeriesProject.objects.select_for_update()
            .filter(pk__in=project_ids)
            .order_by("pk")
            .only("pk")
        )
        jobs = list(
            stale_jobs.select_for_update()
            .select_related("project", "episode")
        )
        for job in jobs:
            old_token = job.claim_token
            if old_token and job.output_path:
                try:
                    stale_temporary_paths.append(
                        _temporary_output_path(_safe_output_path(job), old_token)
                    )
                except AssemblyError:
                    pass
            if _valid_output(job):
                job.status = AssemblyJob.Status.SUCCEEDED
                job.finished_at = now
                job.heartbeat_at = now
                job.error_message = ""
                job.save(update_fields=(
                    "status", "finished_at", "heartbeat_at", "error_message", "updated_at",
                ))
                completed.append(job)
                counts["succeeded"] += 1
            elif job.status == AssemblyJob.Status.PLANNED:
                # Legacy synchronous versions could be interrupted after the
                # command was persisted but before status changed to RUNNING.
                # A complete plan is safe to adopt into the durable queue;
                # an incomplete one cannot be reconstructed from the row.
                if job.output_path and job.ffmpeg_command:
                    job.status = AssemblyJob.Status.QUEUED
                    job.queued_at = now
                    job.worker_id = ""
                    job.claim_token = None
                    job.heartbeat_at = None
                    job.finished_at = None
                    job.error_message = f"{reason}; legacy prepared job was queued"[-4000:]
                    job.save(update_fields=(
                        "status", "queued_at", "worker_id", "claim_token", "heartbeat_at",
                        "finished_at", "error_message", "updated_at",
                    ))
                    counts["requeued"] += 1
                else:
                    job.status = AssemblyJob.Status.FAILED
                    job.finished_at = now
                    job.heartbeat_at = now
                    job.error_message = f"{reason}; legacy job has no executable command"[-4000:]
                    job.save(update_fields=(
                        "status", "finished_at", "heartbeat_at", "error_message", "updated_at",
                    ))
                    counts["failed"] += 1
            elif job.attempts < job.max_attempts:
                job.status = AssemblyJob.Status.QUEUED
                job.queued_at = now
                job.worker_id = ""
                job.claim_token = None
                job.heartbeat_at = None
                job.finished_at = None
                job.error_message = f"{reason}; previous claim will be retried"[-4000:]
                job.save(update_fields=(
                    "status", "queued_at", "worker_id", "claim_token", "heartbeat_at",
                    "finished_at", "error_message", "updated_at",
                ))
                counts["requeued"] += 1
            else:
                job.status = AssemblyJob.Status.FAILED
                job.finished_at = now
                job.heartbeat_at = now
                job.error_message = f"{reason}; retry limit reached"[-4000:]
                job.save(update_fields=(
                    "status", "finished_at", "heartbeat_at", "error_message", "updated_at",
                ))
                counts["failed"] += 1
        for job in completed:
            _mark_scope_complete(job.project, job.episode)

    for temporary_path in stale_temporary_paths:
        try:
            temporary_path.unlink(missing_ok=True)
        except OSError:
            pass
    return counts
