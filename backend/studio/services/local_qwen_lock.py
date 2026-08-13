"""Cross-process memory lease shared by local Qwen and video generation."""
from __future__ import annotations

from contextlib import contextmanager
import os
from pathlib import Path

from django.conf import settings


class LocalQwenResourceBusy(RuntimeError):
    pass


def local_qwen_lock_path() -> Path:
    configured = str(getattr(settings, "LOCAL_QWEN_LOCK_PATH", "") or "").strip()
    path = Path(configured).expanduser() if configured else (
        Path(settings.BASE_DIR).parent / ".runtime" / "locks" / "local-qwen.lock"
    )
    return path if path.is_absolute() else (Path(settings.BASE_DIR) / path).resolve()


def _lock(file_object) -> None:
    file_object.seek(0)
    if os.name == "nt":
        import msvcrt

        try:
            msvcrt.locking(file_object.fileno(), msvcrt.LK_NBLCK, 1)
        except OSError as exc:
            raise LocalQwenResourceBusy(
                "本地 Qwen 或视频生成正在占用共享计算资源，请等待当前任务结束后重试。"
            ) from exc
        return

    import fcntl

    try:
        fcntl.flock(file_object.fileno(), fcntl.LOCK_EX | fcntl.LOCK_NB)
    except (BlockingIOError, OSError) as exc:
        raise LocalQwenResourceBusy(
            "本地 Qwen 或视频生成正在占用共享计算资源，请等待当前任务结束后重试。"
        ) from exc


def _unlock(file_object) -> None:
    file_object.seek(0)
    if os.name == "nt":
        import msvcrt

        try:
            msvcrt.locking(file_object.fileno(), msvcrt.LK_UNLCK, 1)
        except OSError:
            pass
        return
    import fcntl

    try:
        fcntl.flock(file_object.fileno(), fcntl.LOCK_UN)
    except OSError:
        pass


@contextmanager
def local_qwen_runtime_lock():
    """Acquire the OS-visible Qwen/ComfyUI owner; never wait while holding HTTP."""
    path = local_qwen_lock_path()
    path.parent.mkdir(parents=True, exist_ok=True)
    # a+b guarantees a one-byte lock region exists on Windows while preserving
    # one stable inode/file identity for all Django and worker processes.
    with path.open("a+b") as handle:
        handle.seek(0, os.SEEK_END)
        if handle.tell() == 0:
            handle.write(b"0")
            handle.flush()
        _lock(handle)
        try:
            yield path
        finally:
            _unlock(handle)
