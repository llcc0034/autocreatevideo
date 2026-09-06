from __future__ import annotations

from pathlib import Path
import re
from time import monotonic

from django.conf import settings

from studio.models import Asset
from studio.services.provenance import sha256_file
from studio.services.security_boundaries import boundary_int, boundary_number


_SHA256 = re.compile(r"^[0-9a-f]{64}$")
_SUPPORTED_MEDIA_PREFIXES = ("image/", "video/", "audio/")


def _smoke_asset_max_items() -> int:
    return boundary_int("security.workflow_smoke.asset_max_items")


def _smoke_asset_max_file_bytes() -> int:
    return boundary_int("security.workflow_smoke.asset_max_file_bytes")


def _smoke_asset_max_total_bytes() -> int:
    return boundary_int("security.workflow_smoke.asset_max_total_bytes")


def _smoke_asset_scan_max_seconds() -> float:
    return boundary_number(
        "security.workflow_smoke.asset_scan_max_seconds",
        minimum=0.001,
    )


class ManagedSmokeAssetBudget:
    def __init__(self) -> None:
        self.started_at = monotonic()
        self.items = 0
        self.bytes = 0

    def claim(self, size: int) -> str:
        if size < 0 or size > _smoke_asset_max_file_bytes():
            return "source_too_large"
        if self.items >= _smoke_asset_max_items():
            return "eligibility_budget_exceeded"
        if self.bytes + size > _smoke_asset_max_total_bytes():
            return "eligibility_budget_exceeded"
        if monotonic() - self.started_at > _smoke_asset_scan_max_seconds():
            return "eligibility_budget_exceeded"
        self.items += 1
        self.bytes += size
        return ""


def managed_smoke_asset_eligibility(
    asset: Asset,
    *,
    budget: ManagedSmokeAssetBudget | None = None,
) -> tuple[bool, str]:
    """Return a value-free reason derived from current managed bytes.

    This projection is advisory for the UI. Import/validate still repeat the
    same authoritative checks immediately before saving or uploading.
    """

    if asset.owner_id is not None:
        return False, "not_global"
    if not asset.is_active:
        return False, "inactive"
    if asset.file or not asset.local_path:
        return False, "not_library_local_path"
    mime_type = str(asset.mime_type or "").strip().casefold()
    if not mime_type.startswith(_SUPPORTED_MEDIA_PREFIXES):
        return False, "unsupported_media_type"
    checksum = str(asset.checksum or "").strip().casefold()
    if not _SHA256.fullmatch(checksum):
        return False, "checksum_invalid"
    try:
        root = Path(settings.ASSET_LIBRARY_ROOT).resolve(strict=True)
        source = Path(asset.local_path).resolve(strict=True)
    except (OSError, RuntimeError, ValueError):
        return False, "source_unavailable"
    if not source.is_relative_to(root):
        return False, "source_outside_library"
    if not source.is_file():
        return False, "source_unavailable"
    try:
        size = source.stat().st_size
        budget_reason = budget.claim(size) if budget is not None else ""
        if budget_reason:
            return False, budget_reason
        actual = sha256_file(source)
    except OSError:
        return False, "source_unavailable"
    if actual != checksum:
        return False, "checksum_mismatch"
    return True, ""
