"""Canonical file suffixes for verified media formats, not format detection."""
from django.core.exceptions import ValidationError


def verified_media_extension(mime_type, media_kind):
    # Shared by normal Asset output and prepared-media upload. The existing
    # byte probe supplies MIME; object-store keys need not have extensions.
    suffix = {
        "image/jpeg": ".jpg", "image/png": ".png", "image/webp": ".webp",
        "image/bmp": ".bmp", "image/gif": ".gif", "image/tiff": ".tiff",
        "video/mp4": ".mp4", "video/webm": ".webm", "video/x-matroska": ".mkv",
        "audio/wav": ".wav", "audio/mpeg": ".mp3", "audio/flac": ".flac",
        "audio/ogg": ".ogg", "audio/mp4": ".m4a", "text/markdown": ".md",
    }.get(mime_type)
    if suffix is None or not mime_type.startswith(media_kind + "/"):
        raise ValidationError("已核验的媒体格式尚不支持文件输出，不能用文件名或默认扩展名代替")
    return suffix
