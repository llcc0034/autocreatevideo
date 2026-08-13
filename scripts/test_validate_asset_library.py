from __future__ import annotations

from copy import deepcopy
import json
from pathlib import Path
import unittest

from validate_asset_library import validate_manifest


ROOT = Path(__file__).resolve().parents[1]
MANIFEST = ROOT / "assets" / "library" / "manifest.json"


class AssetLibraryProvenanceTests(unittest.TestCase):
    @classmethod
    def setUpClass(cls):
        cls.payload = json.loads(MANIFEST.read_text(encoding="utf-8"))

    def test_current_library_has_complete_integrity_metadata(self):
        validate_manifest(deepcopy(self.payload), ROOT)

    def test_missing_generation_tool_is_rejected(self):
        payload = deepcopy(self.payload)
        asset_id = payload["assets"][0]["id"]
        payload["assets"][0]["metadata"]["generated_with"] = ""
        matching = next(
            item for item in payload["styles"][0]["assets"] if item["id"] == asset_id
        )
        matching["metadata"]["generated_with"] = ""
        with self.assertRaisesRegex(ValueError, "generation tool"):
            validate_manifest(payload, ROOT)

    def test_source_master_hash_tampering_is_rejected(self):
        payload = deepcopy(self.payload)
        payload["assets"][0]["metadata"]["source_master_sha256"] = "0" * 64
        with self.assertRaisesRegex(ValueError, "source master hash"):
            validate_manifest(payload, ROOT)

    def test_style_and_global_records_cannot_diverge(self):
        payload = deepcopy(self.payload)
        payload["styles"][0]["assets"][0]["metadata"]["crop_box"][0] += 1
        with self.assertRaisesRegex(ValueError, "diverges"):
            validate_manifest(payload, ROOT)

    def test_prompt_hint_language_is_advisory_not_an_integrity_gate(self):
        payload = deepcopy(self.payload)
        asset_id = payload["assets"][0]["id"]
        payload["assets"][0]["prompt_hint"] = "preserve identity, materials and colors"
        matching = next(
            item for item in payload["styles"][0]["assets"] if item["id"] == asset_id
        )
        matching["prompt_hint"] = payload["assets"][0]["prompt_hint"]
        validate_manifest(payload, ROOT)


if __name__ == "__main__":
    unittest.main()
