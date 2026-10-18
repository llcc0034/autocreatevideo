from copy import deepcopy

from django.test import SimpleTestCase

from studio.services.media_processing_profiles import REFERENCE_PROFILE_KEY
from studio.services.reference_media import REFERENCE_PREPROCESSING_DESCRIPTIONS
from studio.task_center_media_processing import processing_summary


class MediaProcessingSummaryTests(SimpleTestCase):
    def evidence(self, adapter="cfr_video_v1"):
        slot, role = ("paired_video_audios", "paired_reference_audio") if adapter == "aligned_audio_v1" else (
            ("reference_audios", "reference_audio") if adapter == "passthrough_audio_v1" else ("videos", "reference_video"))
        return dict(adapter=adapter, spec={"slot": slot, "contract": {"adapter_key": adapter,
            "minimum_duration_ms": 1, "maximum_duration_ms": 10000, "target_fps": 24, "requires_source_fps": False}},
            profile={"sha256": "a" * 64}, receipt={"source_sha256": "b" * 64, "submitted_sha256": "c" * 64},
            metadata={"staging_version": 2, "preprocessing_adapter_key": adapter, "media_role": role,
                "media_processing_profile_key": REFERENCE_PROFILE_KEY, "media_processing_profile_sha256": "a" * 64,
                "source_duration_ms": 1001, "effective_duration_ms": 1000, "original_asset_preserved": True,
                "source_fps": 23.976, "effective_fps": 24, "effective_frames": 24, "verified_frame_count": 24,
                "verified_video_duration_ms": 1000, "verified_audio_duration_ms": None,
                "cfr_guaranteed": False, "embedded_audio_policy": "not_staged", "source_bytes_submitted": True,
                "duration_aligned": True})

    def test_all_registered_adapters_keep_declared_effective_and_probed_values_separate(self):
        for adapter in REFERENCE_PREPROCESSING_DESCRIPTIONS:
            with self.subTest(adapter=adapter):
                data = self.evidence(adapter)
                before = deepcopy(data)
                result = processing_summary(**data)
                self.assertEqual(result["state"], "recorded")
                self.assertEqual(result["label"], REFERENCE_PREPROCESSING_DESCRIPTIONS[adapter][0])
                self.assertEqual(result["byte_identity"], "changed")
                rows = {r["key"]: r for r in result["observed"]}
                self.assertEqual(rows["source_duration_ms"]["value"], 1001)
                self.assertEqual(rows["effective_duration_ms"]["value"], 1000)
                self.assertEqual(rows["source_duration_ms"]["unit"], "ms")
                self.assertEqual(rows["source_duration_ms"]["source"], "processing_metadata.source_duration_ms")
                self.assertEqual(data, before)

    def test_no_receipt_no_inferred_results_and_context_is_not_applicable(self):
        data = self.evidence(); data["receipt"] = None
        result = processing_summary(**data)
        self.assertEqual(result["state"], "declared")
        self.assertEqual(result["observed"], [])
        self.assertEqual(result["byte_identity"], "unavailable")
        data["adapter"] = None
        result = processing_summary(**data)
        self.assertEqual(result["state"], "not_applicable")
        self.assertEqual(result["declared"], [])

    def test_source_bytes_receipt_does_not_invent_probe_or_conversion(self):
        data = self.evidence(); data.update(adapter="source_bytes/v1", spec=None, profile=None, metadata={})
        data["receipt"]["submitted_sha256"] = data["receipt"]["source_sha256"]
        result = processing_summary(**data)
        self.assertEqual(result["state"], "recorded")
        self.assertEqual(result["observed"], [])
        self.assertEqual(result["byte_identity"], "same")
        data["metadata"] = {"effective_fps": 24}
        self.assertEqual(processing_summary(**data)["state"], "unverifiable")

    def test_wrong_metadata_identity_or_version_never_borrows_current_contract(self):
        for key, value in [("staging_version", 3), ("staging_version", True), ("preprocessing_adapter_key", "other"),
                           ("media_role", "other"), ("media_processing_profile_key", "other"), ("media_processing_profile_sha256", "b" * 64)]:
            with self.subTest(key=key, value=value):
                data = self.evidence(); data["metadata"][key] = value
                result = processing_summary(**data)
                self.assertEqual(result["state"], "unverifiable")
                self.assertEqual(result["observed"], [])
                self.assertEqual(result["byte_identity"], "changed")

    def test_missing_null_zero_false_and_invalid_observations_are_distinct(self):
        data = self.evidence(); del data["metadata"]["effective_frames"]
        data["metadata"]["source_duration_ms"] = 0
        result = processing_summary(**data)
        self.assertEqual(result["state"], "partial")
        rows = {r["key"]: r for r in result["observed"]}
        self.assertEqual(rows["effective_frames"]["state"], "missing")
        self.assertEqual(rows["verified_audio_duration_ms"]["state"], "unavailable")
        self.assertEqual(rows["source_duration_ms"]["value"], 0)
        self.assertIs(rows["cfr_guaranteed"]["value"], False)
        for invalid in [True, -1, "1000", float("nan"), float("inf"), {"private": "not projected"}]:
            data["metadata"]["effective_duration_ms"] = invalid
            result = processing_summary(**data)
            self.assertEqual(result["state"], "unverifiable")
            self.assertEqual(next(r for r in result["observed"] if r["key"] == "effective_duration_ms")["value"], None)

    def test_embedded_audio_explicitly_shares_parent_record(self):
        result = processing_summary(**self.evidence(), shared_with="video-use-2")
        self.assertEqual(result["shared_with"], "video-use-2")
        self.assertEqual(result["label"], "恒定帧率视频")

    def test_unknown_metadata_values_are_not_exposed_by_summary(self):
        data = self.evidence(); data["metadata"]["unrecognized_private_field"] = "private path"
        result = processing_summary(**data)
        self.assertNotIn("private path", str(result))
        data["metadata"]["embedded_audio_policy"] = "unreviewed"
        self.assertEqual(processing_summary(**data)["state"], "unverifiable")
