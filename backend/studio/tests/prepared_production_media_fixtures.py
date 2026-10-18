"""Isolated original dispatch, staging and archival with decoded local media.

Admission/remote uploads are controlled test peers, not online acceptance.
No legacy Task fields, trace contracts or invented successful Smoke records.
"""
from pathlib import Path
import shutil
import subprocess

from django.conf import settings
from django.test import override_settings

from studio.services.execution_production_workflow_queue import read_prepared_generation_task
from studio.tests import test_execution_production_workflow_artifacts as images
from studio.tests import test_execution_production_workflow_queue as videos


class PreparedProductionMediaFixture:
    _create_version = videos.ProductionWorkflowQueueTests._create_version
    mark_submitted = images.ProductionWorkflowArtifactTests.mark_submitted

    def resolve(self, definition, stage):
        fixture = images.ProductionWorkflowArtifactTests if self.media_kind == "image" else videos.ProductionWorkflowQueueTests
        return fixture.resolve(self, definition, stage)

    def prepare_isolated_media(self, kind):
        if kind not in {"image", "video"}:
            raise ValueError("Fixture requires an explicit supported media kind")
        self.media_kind = kind
        if kind == "image":
            images.ProductionWorkflowArtifactTests.setUp(self)
            return self.task
        ffmpeg = shutil.which(settings.FFMPEG_BINARY)
        if not ffmpeg:
            self.skipTest("A real FFmpeg executable is required for video byte fixtures")
        videos.ProductionWorkflowQueueTests.setUp(self)
        videos.ProductionWorkflowQueueTests.stage_workflow(self)
        self.mark_submitted()
        media = read_prepared_generation_task(self.task).media
        values = media["payload"]["compiled_workflow"]["resolved_semantics"]
        self.output_root = Path(settings.MEDIA_ROOT) / "comfy-output"
        setting = override_settings(COMFYUI_OUTPUT_DIR=str(self.output_root))
        setting.enable()
        self.addCleanup(setting.disable)
        self.output = self.output_root / f'{media["output_prefix"]}_00001_.mp4'
        self.output.parent.mkdir(parents=True)
        # Use the frozen fixture request's dimensions and clock, not a made-up
        # observation that would bypass the production media validator.
        width, height = values["image.width"], values["image.height"]
        fps, frames = values["video.fps"], values["video.frames"]
        command = [ffmpeg, "-hide_banner", "-loglevel", "error", "-y",
            "-f", "lavfi", "-i", f"color=c=blue:s={width}x{height}:r={fps}"]
        if values["audio.enabled"]:
            command += ["-f", "lavfi", "-i", "anullsrc=channel_layout=stereo:sample_rate=48000"]
        command += ["-frames:v", str(frames), "-t", str(frames / fps), "-c:v", "libx264", "-pix_fmt", "yuv420p"]
        if values["audio.enabled"]:
            command += ["-c:a", "aac"]
        subprocess.run([*command, str(self.output)], check=True, capture_output=True, timeout=30)
        self.receipt = {"node_id": media["output_contract"]["primary"]["node_id"],
            "filename": self.output.name, "subfolder": self.output.parent.relative_to(self.output_root).as_posix(),
            "type": "output"}
        return self.task

    def archive_isolated_media(self, kind="image", *, collection=None):
        self.prepare_isolated_media(kind)
        if collection is not None:
            self.receipt["kind"] = collection
        images.ProductionWorkflowArtifactTests.archive(self)
        self.task.refresh_from_db()
        return self.task
