from pathlib import Path
import hashlib
import shutil
import subprocess
import tempfile
import unittest
from unittest.mock import patch

from django.test import SimpleTestCase, TestCase, override_settings

from studio.services.reference_media import (
    ReferenceMediaNormalizationError,
    inspect_reference_video,
    normalize_paired_audio,
    normalize_reference_video,
    probe_media,
)


REFERENCE_MEDIA_PROFILE = {
    "schema_version": 1,
    "probe_timeout_seconds": 45,
    "video_normalize_timeout_seconds": 180,
    "audio_normalize_timeout_seconds": 120,
    "video_codec": "libx264",
    "video_preset": "medium",
    "video_crf": 12,
    "pixel_format": "yuv420p",
    "embedded_audio_codec": "aac",
    "embedded_audio_bitrate_kbps": 192,
    "paired_audio_codec": "pcm_s16le",
    "audio_sample_rate_hz": 48_000,
    "video_duration_tolerance_ms": 60,
    "embedded_audio_duration_tolerance_ms": 80,
    "paired_audio_duration_tolerance_ms": 50,
}


class ReferenceMediaValidationTests(SimpleTestCase):
    def test_unknown_source_fps_is_explicitly_blocked(self):
        with patch("studio.services.reference_media.probe_media", return_value={
            "streams": [{
                "codec_type": "video", "duration": "5.0",
                "avg_frame_rate": "0/0", "r_frame_rate": "0/0",
            }],
            "format": {"duration": "5.0"},
        }):
            with self.assertRaisesRegex(
                ReferenceMediaNormalizationError, "无法可靠确定参考视频源 FPS",
            ):
                inspect_reference_video(
                    "ignored-by-mock.mp4",
                    target_fps=24,
                    minimum_duration_ms=2000,
                    maximum_duration_ms=15000,
                    security_max_fps=480,
                    security_max_duration_ms=86_400_000,
                )

    def test_fictional_model_contract_controls_target_fps_and_duration(self):
        with patch("studio.services.reference_media.probe_media", return_value={
            "streams": [{
                "codec_type": "video",
                "duration": "1.2",
                "avg_frame_rate": "30/1",
                "r_frame_rate": "30/1",
            }],
            "format": {"duration": "1.2"},
        }):
            plan = inspect_reference_video(
                "fictional-video.mp4",
                target_fps=25,
                minimum_duration_ms=800,
                maximum_duration_ms=2400,
                security_max_fps=480,
                security_max_duration_ms=86_400_000,
                media_profile=REFERENCE_MEDIA_PROFILE,
            )

        self.assertEqual(plan.target_fps, 25)
        self.assertEqual(plan.effective_frames, 30)
        self.assertEqual(plan.effective_duration_seconds, 1.2)


@override_settings(FFMPEG_BINARY="ffmpeg", FFPROBE_BINARY="ffprobe")
@unittest.skipUnless(
    shutil.which("ffmpeg") and shutil.which("ffprobe"),
    "ffmpeg/ffprobe are not installed",
)
class ReferenceMediaFFmpegIntegrationTests(TestCase):
    @staticmethod
    def _run(command):
        completed = subprocess.run(
            command, capture_output=True, text=True, timeout=90, check=False,
        )
        if completed.returncode:
            raise AssertionError(completed.stderr or completed.stdout)

    def test_30fps_video_and_both_audio_modes_become_duration_aligned_cfr24(self):
        with tempfile.TemporaryDirectory() as temp:
            root = Path(temp)
            source_video = root / "source-30fps.mp4"
            source_audio = root / "external-3s.wav"
            normalized_video = root / "normalized-cfr24.mp4"
            normalized_audio = root / "normalized-paired.wav"
            self._run([
                "ffmpeg", "-hide_banner", "-loglevel", "error", "-nostdin", "-y",
                "-f", "lavfi", "-i", "testsrc2=size=160x96:rate=30:duration=2.2",
                "-f", "lavfi", "-i", "sine=frequency=880:sample_rate=48000:duration=1.5",
                "-map", "0:v:0", "-map", "1:a:0", "-c:v", "libx264",
                "-pix_fmt", "yuv420p", "-c:a", "aac", str(source_video),
            ])
            self._run([
                "ffmpeg", "-hide_banner", "-loglevel", "error", "-nostdin", "-y",
                "-f", "lavfi", "-i", "sine=frequency=440:sample_rate=48000:duration=3",
                "-c:a", "pcm_s16le", str(source_audio),
            ])
            original_digest = hashlib.sha256(source_video.read_bytes()).hexdigest()

            plan = inspect_reference_video(
                source_video,
                target_fps=24,
                minimum_duration_ms=2000,
                maximum_duration_ms=15000,
                security_max_fps=480,
                security_max_duration_ms=86_400_000,
                media_profile=REFERENCE_MEDIA_PROFILE,
            )
            video_metadata = normalize_reference_video(
                source_video,
                normalized_video,
                include_embedded_audio=True,
                plan=plan,
                media_profile=REFERENCE_MEDIA_PROFILE,
            )
            audio_metadata = normalize_paired_audio(
                source_audio,
                normalized_audio,
                effective_duration_seconds=plan.effective_duration_seconds,
                effective_fps=plan.target_fps,
                minimum_duration_ms=2000,
                maximum_duration_ms=15000,
                security_max_duration_ms=86_400_000,
                media_profile=REFERENCE_MEDIA_PROFILE,
            )

            self.assertAlmostEqual(plan.source_fps, 30.0, places=4)
            self.assertEqual(video_metadata["effective_fps"], 24)
            self.assertTrue(video_metadata["cfr_guaranteed"])
            self.assertEqual(
                video_metadata["verified_frame_count"], video_metadata["effective_frames"],
            )
            self.assertLessEqual(
                abs(
                    video_metadata["verified_audio_duration_ms"]
                    - video_metadata["effective_duration_ms"]
                ),
                80,
            )
            self.assertTrue(audio_metadata["duration_aligned"])
            self.assertLessEqual(
                abs(
                    audio_metadata["verified_audio_duration_ms"]
                    - video_metadata["effective_duration_ms"]
                ),
                50,
            )
            self.assertEqual(hashlib.sha256(source_video.read_bytes()).hexdigest(), original_digest)

            output_probe = probe_media(
                normalized_video,
                media_profile=REFERENCE_MEDIA_PROFILE,
            )
            output_video = next(
                item for item in output_probe["streams"] if item.get("codec_type") == "video"
            )
            self.assertEqual(output_video["avg_frame_rate"], "24/1")
            self.assertEqual(output_video["r_frame_rate"], "24/1")

            # Real local FFmpeg/probe metadata feeds the same read-only summary;
            # no upload receipt, provider call or business task is fabricated.
            from studio.task_center_media_processing import processing_summary
            from studio.services.media_processing_profiles import media_profile_sha256
            for slot, adapter, source, output, metadata in (
                ("videos", "cfr_video_v1", source_video, normalized_video, video_metadata),
                ("paired_video_audios", "aligned_audio_v1", source_audio, normalized_audio, audio_metadata),
            ):
                summary = processing_summary(adapter=adapter,
                    spec={"slot": slot, "contract": {"adapter_key": adapter, "minimum_duration_ms": 2000, "maximum_duration_ms": 15000}},
                    profile={"sha256": media_profile_sha256(REFERENCE_MEDIA_PROFILE)},
                    receipt={"source_sha256": hashlib.sha256(source.read_bytes()).hexdigest(),
                             "submitted_sha256": hashlib.sha256(output.read_bytes()).hexdigest()}, metadata=metadata)
                self.assertEqual(summary["state"], "recorded")
                self.assertEqual(summary["byte_identity"], "changed")
                observed = {row["key"]: row for row in summary["observed"]}
                self.assertEqual(observed["effective_duration_ms"]["value"], metadata["effective_duration_ms"])
                self.assertEqual(observed["verified_audio_duration_ms"]["value"], metadata["verified_audio_duration_ms"])
