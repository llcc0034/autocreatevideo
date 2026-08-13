from __future__ import annotations

import hashlib
import json
from pathlib import Path

from django.conf import settings
from django.core.management.base import BaseCommand, CommandError
from django.db import transaction

from studio.models import Asset, Style


KIND_ALIASES = {
    "character": Asset.Kind.CHARACTER,
    "characters": Asset.Kind.CHARACTER,
    "人物": Asset.Kind.CHARACTER,
    "scene": Asset.Kind.SCENE,
    "scenes": Asset.Kind.SCENE,
    "场景": Asset.Kind.SCENE,
    "prop": Asset.Kind.PROP,
    "props": Asset.Kind.PROP,
    "道具": Asset.Kind.PROP,
}


class Command(BaseCommand):
    help = "Import the local original material library from assets/library/manifest.json."

    def add_arguments(self, parser):
        parser.add_argument("--manifest", default="assets/library/manifest.json")
        parser.add_argument("--deactivate-missing", action="store_true")

    def _resolve_manifest(self, raw: str) -> Path:
        requested = Path(raw).expanduser()
        candidates = [requested] if requested.is_absolute() else [
            Path.cwd() / requested,
            settings.BASE_DIR.parent / requested,
            settings.BASE_DIR / requested,
        ]
        for candidate in candidates:
            if candidate.is_file():
                return candidate.resolve()
        raise CommandError(f"Manifest not found. Checked: {', '.join(str(item) for item in candidates)}")

    @staticmethod
    def _rows(payload):
        if isinstance(payload, list):
            return payload
        if isinstance(payload, dict):
            for key in ("assets", "materials", "items"):
                if isinstance(payload.get(key), list):
                    return payload[key]
        raise CommandError("Manifest must be an array or contain an assets/materials/items array")

    @staticmethod
    def _resolve_asset_path(raw: str, manifest: Path) -> Path:
        value = Path(raw).expanduser()
        if value.is_absolute():
            return value.resolve()
        candidates = [
            Path.cwd() / value,
            settings.BASE_DIR.parent / value,
            manifest.parent / value,
        ]
        for candidate in candidates:
            if candidate.is_file():
                return candidate.resolve()
        return candidates[1].resolve()

    @staticmethod
    def _checksum(path: Path) -> str:
        digest = hashlib.sha256()
        with path.open("rb") as stream:
            for chunk in iter(lambda: stream.read(1024 * 1024), b""):
                digest.update(chunk)
        return digest.hexdigest()

    @transaction.atomic
    def handle(self, *args, **options):
        manifest = self._resolve_manifest(options["manifest"])
        try:
            payload = json.loads(manifest.read_text(encoding="utf-8-sig"))
        except (OSError, json.JSONDecodeError) as exc:
            raise CommandError(f"Cannot read manifest: {exc}") from exc
        rows = self._rows(payload)
        imported_ids = []
        errors = []
        for index, row in enumerate(rows, start=1):
            if not isinstance(row, dict):
                errors.append(f"row {index}: expected object")
                continue
            style_slug = row.get("style_slug") or row.get("style")
            raw_kind = str(row.get("kind") or row.get("asset_kind") or "").strip().lower()
            name = str(row.get("name") or row.get("title") or "").strip()
            raw_path = row.get("local_path") or row.get("path") or row.get("file")
            try:
                style = Style.objects.get(owner=None, slug=style_slug, is_active=True)
            except Style.DoesNotExist:
                errors.append(f"row {index}: unknown active style_slug {style_slug!r}")
                continue
            kind = KIND_ALIASES.get(raw_kind)
            if not kind or not name or not raw_path:
                errors.append(f"row {index}: style_slug, supported kind, name and local_path are required")
                continue
            path = self._resolve_asset_path(str(raw_path), manifest)
            if not path.is_file():
                errors.append(f"row {index}: file not found {path}")
                continue
            checksum = str(row.get("checksum") or "").strip() or self._checksum(path)
            metadata = row.get("metadata") if isinstance(row.get("metadata"), dict) else {}
            metadata = {**metadata, "manifest": str(manifest), "manifest_index": index}
            asset, _ = Asset.objects.update_or_create(
                owner=None, style=style, kind=kind, name=name,
                defaults={
                    "description": str(row.get("description") or ""),
                    "prompt_hint": str(row.get("prompt_hint") or row.get("prompt") or ""),
                    "local_path": str(path),
                    "mime_type": str(row.get("mime_type") or "image/png"),
                    "checksum": checksum,
                    "metadata": metadata,
                    "is_active": True,
                },
            )
            imported_ids.append(asset.id)
        if errors:
            raise CommandError("Manifest validation failed:\n" + "\n".join(errors))
        if options["deactivate_missing"]:
            Asset.objects.filter(owner=None, kind__in=(
                Asset.Kind.CHARACTER, Asset.Kind.SCENE, Asset.Kind.PROP,
            )).exclude(id__in=imported_ids).update(is_active=False)
        self.stdout.write(self.style.SUCCESS(f"Imported or updated {len(imported_ids)} assets from {manifest}"))
