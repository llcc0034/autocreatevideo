"""Canonical provenance helpers for persisted library assets.

These helpers describe saved metadata and file identity only.  They do not
perform model inference or inspect image pixels.
"""
from __future__ import annotations

import hashlib
import json
from typing import Any, Iterable

from django.utils.dateparse import parse_datetime

from studio.models import Asset


def provenance_snapshots_equal(left: Any, right: Any) -> bool:
    """Compare immutable provenance while normalizing timezone offsets."""

    if not isinstance(left, dict) or not isinstance(right, dict):
        return left == right
    left_copy = dict(left)
    right_copy = dict(right)
    left_time = left_copy.pop("asset_updated_at", None)
    right_time = right_copy.pop("asset_updated_at", None)
    try:
        parsed_left = parse_datetime(str(left_time or ""))
        parsed_right = parse_datetime(str(right_time or ""))
    except (TypeError, ValueError, OverflowError):
        return False
    return bool(
        parsed_left is not None
        and parsed_right is not None
        and parsed_left == parsed_right
        and left_copy == right_copy
    )


def _asset_metadata_snapshot(asset: Asset) -> dict[str, Any]:
    """Return the authored metadata and immutable file facts used by audits."""

    style = asset.style
    return {
        "asset_id": str(asset.id),
        "kind": asset.kind,
        "name": str(asset.name or "").strip(),
        "description": str(asset.description or "").strip(),
        "prompt_hint": str(asset.prompt_hint or "").strip(),
        "style": (
            {
                "name": str(style.name or "").strip(),
                "description": str(style.description or "").strip(),
                "visual_prompt": str(style.visual_prompt or "").strip(),
            }
            if style is not None
            else None
        ),
        "checksum": str(asset.checksum or "").strip().casefold(),
        "updated_at": asset.updated_at.isoformat(),
    }


def asset_snapshot_sha256(assets: Iterable[Asset]) -> str:
    """Hash ordered asset metadata using the canonical audit serialization."""

    encoded = json.dumps(
        [_asset_metadata_snapshot(asset) for asset in assets],
        ensure_ascii=False,
        sort_keys=True,
        separators=(",", ":"),
    ).encode("utf-8")
    return hashlib.sha256(encoded).hexdigest()
