from pathlib import Path
import hashlib
import shutil
import subprocess
import tempfile
import unittest
from unittest.mock import patch

from django.test import SimpleTestCase, override_settings

from studio.services.reference_media import (
    ReferenceMediaNormalizationError,
    inspect_reference_video,
    normalize_paired_audio,
    normalize_reference_video,
    probe_media,
)


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
                inspect_reference_video("ignored-by-mock.mp4")


@override_settings(FFMPEG_BINARY="ffmpeg", FFPROBE_BINARY="ffprobe")
@unittest.skipUnless(
    shutil.which("ffmpeg") and shutil.which("ffprobe"),
    "ffmpeg/ffprobe are not installed",
)
class ReferenceMediaFFmpegIntegrationTests(SimpleTestCase):
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

            plan = inspect_reference_video(source_video)
            video_metadata = normalize_reference_video(
                source_video,
                normalized_video,
                include_embedded_audio=True,
                plan=plan,
            )
            audio_metadata = normalize_paired_audio(
                source_audio,
                normalized_audio,
                paired_video_asset_id="video-asset-id",
                effective_duration_seconds=plan.effective_duration_seconds,
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

            output_probe = probe_media(normalized_video)
            output_video = next(
                item for item in output_probe["streams"] if item.get("codec_type") == "video"
            )
            self.assertEqual(output_video["avg_frame_rate"], "24/1")
            self.assertEqual(output_video["r_frame_rate"], "24/1")
