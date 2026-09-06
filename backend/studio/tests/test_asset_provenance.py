from datetime import datetime, timezone
from types import SimpleNamespace
from uuid import UUID

from django.test import SimpleTestCase

from studio.services.asset_provenance import (
    asset_snapshot_sha256,
    provenance_snapshots_equal,
)


class AssetProvenanceTests(SimpleTestCase):
    @staticmethod
    def _asset(
        *,
        description: str = "冷青色站台",
        source_info: str = "ProductionRun run-1 审核绑定的 Bible 参考图",
    ):
        return SimpleNamespace(
            id=UUID("11111111-1111-4111-8111-111111111111"),
            kind="scene",
            name="废弃车站",
            description=description,
            prompt_hint="rainy abandoned station",
            source_info=source_info,
            style=SimpleNamespace(
                name="都市悬疑",
                description="冷色写实",
                visual_prompt="cinematic urban noir",
            ),
            checksum="A" * 64,
            updated_at=datetime(2026, 8, 29, 2, 0, tzinfo=timezone.utc),
        )

    def test_snapshot_hash_is_canonical_and_changes_with_authored_metadata(self):
        first = self._asset()
        duplicate = self._asset()
        changed = self._asset(description="暖色室内")
        source_changed = self._asset(source_info="系统素材清单导入")

        self.assertEqual(
            asset_snapshot_sha256([first]),
            asset_snapshot_sha256([duplicate]),
        )
        self.assertNotEqual(
            asset_snapshot_sha256([first]),
            asset_snapshot_sha256([changed]),
        )
        self.assertEqual(
            asset_snapshot_sha256([first]),
            asset_snapshot_sha256([source_changed]),
        )

    def test_provenance_comparison_normalizes_timezone_offset_only(self):
        left = {"asset_id": "asset-1", "asset_updated_at": "2026-08-29T10:00:00+08:00"}
        right = {"asset_id": "asset-1", "asset_updated_at": "2026-08-29T02:00:00Z"}

        self.assertTrue(provenance_snapshots_equal(left, right))
        self.assertFalse(provenance_snapshots_equal(
            left,
            {**right, "asset_id": "asset-2"},
        ))
        self.assertFalse(provenance_snapshots_equal(
            left,
            {**right, "asset_updated_at": "not-a-time"},
        ))
