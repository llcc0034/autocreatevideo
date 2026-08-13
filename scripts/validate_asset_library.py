"""Validate the bundled asset library and its technical provenance metadata.

This verifies that the manifest has not silently lost its provenance fields and
that every derived crop still points at the exact declared source master.  It
also verifies the generated files, dimensions, MIME types and generation tool
records without relying on non-technical declarations.
"""

from __future__ import annotations

import argparse
from collections import Counter
import hashlib
import json
from pathlib import Path
import re

from PIL import Image


SHA256_RE = re.compile(r"^[0-9a-f]{64}$")
EXPECTED_KINDS = ("character", "scene", "prop")


def digest(path: Path) -> str:
    value = hashlib.sha256()
    with path.open("rb") as handle:
        for block in iter(lambda: handle.read(1024 * 1024), b""):
            value.update(block)
    return value.hexdigest()


def fail(message: str) -> None:
    raise ValueError(message)


def safe_file(project_root: Path, relative_path: object, *, label: str) -> Path:
    if not isinstance(relative_path, str) or not relative_path.strip():
        fail(f"{label} must be a non-empty relative path")
    candidate = Path(relative_path)
    if candidate.is_absolute():
        fail(f"{label} must be relative: {relative_path}")
    path = (project_root / candidate).resolve()
    if not path.is_relative_to(project_root) or not path.is_file():
        fail(f"unsafe or missing {label}: {path}")
    return path


def require_sha256(value: object, *, label: str) -> str:
    normalized = str(value or "").strip().lower()
    if not SHA256_RE.fullmatch(normalized):
        fail(f"{label} must be a lowercase SHA-256 digest")
    return normalized


def validate_manifest(payload: dict, project_root: Path) -> None:
    project_root = project_root.resolve()
    if payload.get("schema_version") != 1:
        fail("unsupported or missing asset manifest schema_version")
    if payload.get("source_type") != "AI-generated original contact sheets":
        fail("asset library source_type must explicitly declare AI-generated originals")

    styles = payload.get("styles", [])
    assets = payload.get("assets", [])
    if not styles:
        fail("asset library must contain at least one style")
    if payload.get("style_count") != len(styles):
        fail("asset library style_count must match the style index")
    if payload.get("asset_count") != len(assets):
        fail("asset library asset_count must match the flat asset index")

    style_slugs = [str(style.get("slug") or "") for style in styles]
    if any(not slug for slug in style_slugs) or len(set(style_slugs)) != len(style_slugs):
        fail("style slugs must be non-empty and unique")

    masters: dict[str, tuple[Path, str, tuple[int, int]]] = {}
    for style in styles:
        slug = style["slug"]
        master = style.get("master")
        if not isinstance(master, dict):
            fail(f"style {slug} is missing its source master record")
        master_path = safe_file(project_root, master.get("local_path"), label=f"{slug} source master")
        declared_hash = require_sha256(master.get("sha256"), label=f"{slug} source master hash")
        if digest(master_path) != declared_hash:
            fail(f"source master checksum mismatch: {master_path}")
        with Image.open(master_path) as image:
            image.verify()
        with Image.open(master_path) as image:
            size = image.size
            mime_type = Image.MIME.get(image.format, "")
        if size != (master.get("width"), master.get("height")):
            fail(f"source master dimensions do not match manifest: {master_path}")
        if mime_type != master.get("mime_type"):
            fail(f"source master mime type does not match manifest: {master_path}")
        masters[slug] = (master_path, declared_hash, size)

    ids: set[str] = set()
    global_assets: dict[str, dict] = {}
    for asset in assets:
        asset_id = str(asset.get("id") or "")
        if not asset_id or asset_id in ids:
            fail(f"missing or duplicate asset id: {asset_id!r}")
        ids.add(asset_id)
        global_assets[asset_id] = asset

        slug = asset.get("style_slug")
        if slug not in masters:
            fail(f"asset {asset_id} references an unknown style: {slug!r}")
        if asset.get("kind") not in EXPECTED_KINDS:
            fail(f"asset {asset_id} has an unsupported kind: {asset.get('kind')!r}")
        path = safe_file(project_root, asset.get("local_path"), label=f"asset {asset_id}")
        declared_hash = require_sha256(asset.get("checksum"), label=f"asset {asset_id} checksum")
        if digest(path) != declared_hash:
            fail(f"checksum mismatch: {path}")
        with Image.open(path) as image:
            image.verify()
        with Image.open(path) as image:
            crop_size = image.size
            mime_type = Image.MIME.get(image.format, "")
        if mime_type != asset.get("mime_type"):
            fail(f"asset mime type does not match manifest: {path}")

        metadata = asset.get("metadata")
        if not isinstance(metadata, dict):
            fail(f"asset {asset_id} is missing provenance metadata")
        if not str(metadata.get("generated_with") or "").strip():
            fail(f"asset {asset_id} is missing its generation tool declaration")

        master_path, master_hash, master_size = masters[slug]
        source_master = safe_file(
            project_root, metadata.get("source_master"), label=f"asset {asset_id} source master",
        )
        if source_master != master_path:
            fail(f"asset {asset_id} source master does not match style {slug}")
        source_hash = require_sha256(
            metadata.get("source_master_sha256"), label=f"asset {asset_id} source master hash",
        )
        if source_hash != master_hash:
            fail(f"asset {asset_id} source master hash does not match style {slug}")
        crop_box = metadata.get("crop_box")
        if (
            not isinstance(crop_box, list) or len(crop_box) != 4
            or any(isinstance(value, bool) or not isinstance(value, int) for value in crop_box)
        ):
            fail(f"asset {asset_id} has an invalid crop_box")
        left, top, right, bottom = crop_box
        if not (0 <= left < right <= master_size[0] and 0 <= top < bottom <= master_size[1]):
            fail(f"asset {asset_id} crop_box lies outside its source master")
        if crop_size != (right - left, bottom - top):
            fail(f"asset {asset_id} dimensions do not match its declared crop_box")

    indexed_ids: list[str] = []
    for style in styles:
        slug = style["slug"]
        style_assets = style.get("assets", [])
        if style.get("asset_count") != 9 or len(style_assets) != 9:
            fail(f"style {slug} must index exactly 9 assets")
        grouped = Counter(asset.get("kind") for asset in style_assets)
        if {kind: grouped[kind] for kind in EXPECTED_KINDS} != {
            "character": 3, "scene": 3, "prop": 3,
        } or any(kind not in EXPECTED_KINDS for kind in grouped):
            fail(f"invalid asset grouping for {slug}: {dict(grouped)}")
        for indexed in style_assets:
            asset_id = str(indexed.get("id") or "")
            indexed_ids.append(asset_id)
            canonical = global_assets.get(asset_id)
            if canonical is None or canonical != indexed:
                fail(f"style index for {asset_id!r} diverges from the global asset record")
            if indexed.get("style_slug") != slug:
                fail(f"style index {slug} contains an asset from another style")
    if Counter(indexed_ids) != Counter(global_assets.keys()):
        fail("style indexes must cover every global asset exactly once")


def main() -> int:
    parser = argparse.ArgumentParser()
    parser.add_argument("--manifest", type=Path, default=Path("assets/library/manifest.json"))
    args = parser.parse_args()
    manifest_path = args.manifest.resolve()
    project_root = manifest_path.parents[2]
    payload = json.loads(manifest_path.read_text(encoding="utf-8"))
    try:
        validate_manifest(payload, project_root)
    except (KeyError, TypeError, ValueError) as exc:
        raise SystemExit(str(exc)) from exc
    print(
        f"Asset library OK: {payload.get('style_count')} styles, "
        f"{payload.get('asset_count')} images, checksums, source crops, "
        "generation metadata valid."
    )
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
