"""M8-03: real isolated clips, exact source failures, decoded order/audio.

No live business data or model calls. Reuses the formal adopted-media fixture;
only source media is synthetic. Existing queue/FFmpeg/provenance remain real.
"""
from array import array
from copy import deepcopy
import json
from pathlib import Path
import subprocess
import sys
from unittest.mock import patch

from django.conf import settings
from django.test import TestCase

from studio.models import ArtifactSelectionDecision, AssemblyJob
from studio.services.assembly import AssemblyError, create_assembly, resolve_assembly_configuration
from studio.services.assembly_queue import claim_next_assembly, process_assembly_job
from studio.services.production_runs import _selected_assembly_sources
from studio.tests.test_assembly import frozen_sources_for_test
from studio.tests import test_manual_assembly_multiepisode as fixtures


class AssemblySourceAccuracyTests(TestCase):
    setUp = fixtures.ManualMultiEpisodeAssemblyTests.setUp
    create = fixtures.ManualMultiEpisodeAssemblyTests.create
    post = fixtures.ManualMultiEpisodeAssemblyTests.post
    clip = fixtures.ManualMultiEpisodeAssemblyTests.clip
    assert_no_generation = fixtures.ManualMultiEpisodeAssemblyTests.assert_no_generation

    def sources(self):
        references = {str(shot.pk): {"generated_artifact_id": str(artifact.pk)}
            for shot, artifact in zip(self.shots, self.artifacts, strict=True)}
        return {"schema_version": 1, "episodes": [
            frozen_sources_for_test(episode, references)["episodes"][0] for episode in self.episodes]}

    def enqueue(self, sources):
        configuration = resolve_assembly_configuration(self.project)
        configuration.update(width=160, height=96, fps=24)
        return create_assembly(owner=self.actor, project=self.project, episode=None,
            dry_run=False, frozen_sources=sources, frozen_configuration=configuration)

    def assert_item_error(self, message, index):
        shot, artifact = self.shots[index], self.artifacts[index]
        self.assertIn(f"第 {shot.episode.number} 集", message)
        self.assertIn(f"分镜 {shot.number}", message)
        self.assertIn(str(artifact.pk), message)

    def test_series_decodes_four_exact_clips_in_episode_shot_and_audio_order(self):
        job = self.enqueue(self.sources())
        before = list(ArtifactSelectionDecision.objects.values_list("pk", flat=True))
        self.assertEqual(list(job.source_items.order_by("ordinal").values_list("generated_artifact_id", flat=True)),
            [artifact.pk for artifact in self.artifacts])
        result = process_assembly_job(claim_next_assembly(worker_id="isolated-m8-source-accuracy"))
        self.assertEqual(result.status, "succeeded", result.error_message)
        probe = subprocess.run([settings.FFPROBE_BINARY, "-v", "error", "-show_format", "-show_streams",
            "-of", "json", result.output_path], check=True, capture_output=True, timeout=20)
        facts = json.loads(probe.stdout)
        self.assertAlmostEqual(float(facts["format"]["duration"]), 4, delta=0.15)
        self.assertEqual({row["codec_type"] for row in facts["streams"]}, {"video", "audio"})
        for index, shot in enumerate(self.shots):
            with self.subTest(episode=shot.episode.number, shot=shot.number):
                expected_blue = (shot.episode.number + shot.number) % 2
                frame = subprocess.run([settings.FFMPEG_BINARY, "-v", "error", "-ss", str(index + .5),
                    "-i", result.output_path, "-frames:v", "1", "-vf", "crop=16:16:(iw-16)/2:(ih-16)/2,scale=1:1",
                    "-pix_fmt", "rgb24", "-f", "rawvideo", "pipe:1"], check=True, capture_output=True, timeout=20).stdout
                self.assertEqual(len(frame), 3)
                self.assertGreater(frame[2 if expected_blue else 0], 180)
                self.assertLess(frame[0 if expected_blue else 2], 70)
                pcm = subprocess.run([settings.FFMPEG_BINARY, "-v", "error", "-ss", str(index + .25),
                    "-i", result.output_path, "-t", "0.5", "-vn", "-ac", "1", "-ar", "8000",
                    "-f", "s16le", "pipe:1"], check=True, capture_output=True, timeout=20).stdout
                samples = array("h", pcm)
                if sys.byteorder != "little":
                    samples.byteswap()
                self.assertEqual(len(samples), 4000)
                self.assertGreater(max(abs(value) for value in samples), 500)
                crossings = sum(left <= 0 < right for left, right in zip(samples, samples[1:]))
                self.assertAlmostEqual(crossings / .5, 880 if expected_blue else 440, delta=8)
        self.assertEqual(list(ArtifactSelectionDecision.objects.values_list("pk", flat=True)), before)
        self.assert_no_generation()

    def test_missing_input_before_enqueue_identifies_exact_episode_shot_and_artifact(self):
        sources = self.sources()
        Path(settings.MEDIA_ROOT, self.artifacts[2].object_key).unlink()
        with self.assertRaises(AssemblyError) as error:
            self.enqueue(sources)
        self.assert_item_error(str(error.exception), 2)
        self.assertFalse(AssemblyJob.objects.exists())
        self.assert_no_generation()

    def test_corrupt_queued_input_identifies_exact_source_and_never_runs_encoder(self):
        self.enqueue(self.sources())
        Path(settings.MEDIA_ROOT, self.artifacts[3].object_key).write_bytes(b"invalid isolated media")
        with patch("studio.services.assembly_queue._run_ffmpeg", side_effect=AssertionError("Bad source cannot encode")) as encoder:
            result = process_assembly_job(claim_next_assembly(worker_id="isolated-corrupt-source"))
        self.assertNotEqual(result.status, "succeeded")
        self.assert_item_error(result.error_message, 3)
        encoder.assert_not_called()
        self.assertFalse(Path(result.output_path).exists())
        self.assertEqual(ArtifactSelectionDecision.objects.count(), 4)
        self.assert_no_generation()

    def test_unfilled_shot_is_located_without_using_another_candidate(self):
        artifact = self.artifacts[2]
        artifact.status = "available"
        artifact.save(update_fields=("status",))
        with self.assertRaises(AssemblyError) as error:
            _selected_assembly_sources(self.run)
        self.assertIn("第 2 集", str(error.exception))
        self.assertIn("分镜 1", str(error.exception))
        self.assertIn(str(self.shots[2].pk), str(error.exception))
        self.assertFalse(AssemblyJob.objects.exists())
        self.assertEqual(ArtifactSelectionDecision.objects.count(), 4)
        self.assert_no_generation()

    def test_probe_failure_or_missing_video_stream_names_the_exact_input(self):
        # Inject only the decoder boundary. Adoption/files/selection are real;
        # no failed probe may be skipped or turn into an assembly job.
        from studio.services.assembly import _probe_media_streams
        sources = self.sources()
        target = Path(settings.MEDIA_ROOT, self.artifacts[2].object_key)
        for outcome in (AssemblyError("isolated unsupported decoder"), frozenset({"audio"})):
            def probe(path, **kwargs):
                if path == target:
                    if isinstance(outcome, Exception):
                        raise outcome
                    return outcome
                return _probe_media_streams(path, **kwargs)
            with self.subTest(outcome=outcome), patch("studio.services.assembly._probe_media_streams", side_effect=probe):
                with self.assertRaises(AssemblyError) as error:
                    self.enqueue(sources)
                self.assert_item_error(str(error.exception), 2)
        self.assertFalse(AssemblyJob.objects.exists())
        self.assert_no_generation()

    def test_reversed_frozen_order_is_rejected_without_new_selection_or_job(self):
        sources = self.sources()
        for field in ("episodes", "shots"):
            changed = deepcopy(sources)
            if field == "episodes":
                changed["episodes"].reverse()
            else:
                changed["episodes"][0]["shots"].reverse()
            with self.subTest(field=field), self.assertRaises(AssemblyError):
                self.enqueue(changed)
        self.assertFalse(AssemblyJob.objects.exists())
        self.assertEqual(ArtifactSelectionDecision.objects.count(), 4)
        self.assert_no_generation()
