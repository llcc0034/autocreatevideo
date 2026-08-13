from django.core import signing


MEDIA_SIGNING_SALT = "autoprompt.media.v1"

VIDEO_OUTPUT_SUFFIXES = frozenset({".mp4", ".mov", ".mkv", ".webm", ".avi", ".m4v"})
IMAGE_OUTPUT_SUFFIXES = frozenset({".png", ".jpg", ".jpeg", ".webp", ".gif", ".bmp", ".tif", ".tiff"})
AUDIO_OUTPUT_SUFFIXES = frozenset({".wav", ".mp3", ".m4a", ".aac", ".ogg", ".flac", ".opus"})


def classify_task_output(item: object) -> str | None:
    """Return a browser media kind for an exact persisted ComfyUI output item.

    Some ComfyUI video-combine nodes report MP4 files in their ``images``
    collection.  The filename suffix therefore takes precedence over the
    collection name.
    """

    if not isinstance(item, dict):
        return None
    filename = item.get("filename")
    if not isinstance(filename, str) or not filename.strip():
        return None
    suffix = "." + filename.rsplit(".", 1)[-1].casefold() if "." in filename else ""
    if suffix in VIDEO_OUTPUT_SUFFIXES:
        return "video"
    if suffix in IMAGE_OUTPUT_SUFFIXES:
        return "image"
    if suffix in AUDIO_OUTPUT_SUFFIXES:
        return "audio"
    return None


def signed_media_token(*, purpose: str, object_id, owner_id, claims: dict | None = None) -> str:
    payload = dict(claims or {})
    payload.update({
        "purpose": purpose,
        "object_id": str(object_id),
        "owner_id": str(owner_id) if owner_id is not None else None,
    })
    return signing.dumps(
        payload,
        salt=MEDIA_SIGNING_SALT,
        compress=True,
    )


def load_signed_media_token(token: str, *, max_age: int) -> dict:
    return signing.loads(token, salt=MEDIA_SIGNING_SALT, max_age=max_age)
