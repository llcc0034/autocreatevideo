"""INT-02: original image/reference-video workers -> real isolated FFmpeg.

Only live admission observations and external Comfy transport are fixtures.
No source resolver, stage writer, prompt compiler, archive, selection or
assembly success is mocked. This is isolated integration, never live H3.
"""
from copy import deepcopy
from hashlib import sha256
from pathlib import Path
import json
import subprocess
from types import SimpleNamespace
from unittest.mock import Mock, patch

from PIL import Image
from django.conf import settings
from django.test import TestCase, override_settings
from django.utils import timezone

from studio.models import (ApprovalDecision, Asset, AssetGenerationJob, AssetReferenceVersion,
    AssemblyJob, BibleVariantRevision, CharacterBible, ExecutionPreparation, GeneratedArtifact,
    GenerationAttempt, ProductionMaterialFulfillment, ReleaseBundle, SceneBible, PropBible, ShotReferenceUse)
from studio.services.assembly_queue import claim_next_assembly, process_assembly_job
from studio.services.execution_production_media import read_prepared_production_media
from studio.services.production_runs import advance_production_run
from studio.services.task_queue import claim_next_task, process_task
from studio.tests.int_contract_fixtures import IntegrationContracts
from studio.tests.test_execution_production_auxiliaries import production_observations
from studio.tests.test_execution_workflow_staging import RecordingUploadClient


@override_settings(AI_PROVIDER_ALLOWED_HOSTS=("127.0.0.1",))
class IntegrationProductionJourneyTests(IntegrationContracts, TestCase):
    def setUp(self):
        self.int_setup()
        for name in ("execution_production_admission", "execution_production_dispatch"):
            self.enterContext(patch(f"studio.services.{name}.collect_production_execution_admission", side_effect=production_observations))
        self.submissions = []

    def tearDown(self):
        for guard in self.network_guards:
            guard.assert_not_called()
        self.assertFalse(AssetGenerationJob.objects.exists())

    def advance(self):
        self.sample.run.refresh_from_db()
        # Django TestCase wraps the test in a rollback transaction. Execute the
        # original commit callbacks (material registration and recipe wakes),
        # not a substitute registration writer or fake terminal state.
        with self.captureOnCommitCallbacks(execute=True):
            self.sample.run = advance_production_run(self.sample.run, automatic=True)
        self.sample.run.refresh_from_db()
        return self.sample.run

    def state(self):
        run = self.sample.run
        return {"run": (run.status, run.current_stage_key, run.blocked_code, run.blocked_reason),
            "preparations": list(ExecutionPreparation.objects.filter(stage_run__production_run=run)
                .values("unit_key", "status", "reason_code", "reason")),
            "attempts": list(GenerationAttempt.objects.filter(stage_run__production_run=run).values("unit_key", "status"))}

    def collect(self, task):
        attempt = GenerationAttempt.objects.get(generation_task=task)
        media = read_prepared_production_media(attempt)
        kind = media["output_contract"]["primary"]["kind"]
        filename = f"{attempt.preparation_id}_00001_.{'png' if kind == 'image' else 'mp4'}"
        output = Path(settings.COMFYUI_OUTPUT_DIR) / "autoprompt" / "execution" / filename
        self.assertTrue(output.resolve().is_relative_to(self.int_root))
        output.parent.mkdir(parents=True, exist_ok=True)
        color = "orange" if len(self.submissions) % 2 == 0 else "green"
        if kind == "image":
            Image.new("RGB", (512, 320), color).save(output)
        else:
            subprocess.run([settings.FFMPEG_BINARY, "-v", "error", "-y", "-f", "lavfi", "-i",
                f"color=c={color}:s=640x384:r=24:d=5.166667", "-f", "lavfi", "-i",
                "anullsrc=r=48000:cl=stereo", "-t", "5.166667", "-c:v", "libx264", "-pix_fmt", "yuv420p",
                "-c:a", "aac", str(output)], check=True, capture_output=True, timeout=30)
        peer = RecordingUploadClient()
        now = timezone.now().isoformat()
        peer.release_resources_before_submit = Mock(return_value={"schema_version": 1, "status": "succeeded",
            "policy_sha256": "d" * 64, "unload_models": True, "free_memory": True,
            "attempted_at": now, "completed_at": now, "error_code": "", "http_status": 200})
        peer.queue_prompt = Mock(side_effect=lambda graph, client_id, *, prompt_id: SimpleNamespace(prompt_id=prompt_id, node_errors={}))
        peer.extract_outputs = lambda _: [{"node_id": media["output_contract"]["primary"]["node_id"],
            "filename": filename, "subfolder": "autoprompt/execution", "type": "output"}]
        with patch("studio.services.task_queue.preflight_comfyui_workflow", return_value=SimpleNamespace(
                as_dict=lambda: {"node_count": 8, "link_count": 7, "class_types": ["IsolatedPeer"], "schema_sha256": "c" * 64})), patch(
                "studio.services.task_queue._wait_for_task_completion", return_value={"status": {"status_str": "success", "completed": True}}):
            result = process_task(task, peer)
        self.assertEqual(result.status, "succeeded", result.error_message)
        result.refresh_from_db()
        self.assertEqual(result.distribution_state, "ready", result.distribution_error)
        peer.queue_prompt.assert_called_once()
        artifact = GeneratedArtifact.objects.get(generation_attempt=attempt)
        self.assertEqual(artifact.sha256, sha256(output.read_bytes()).hexdigest())
        self.submissions.append({"attempt": attempt, "kind": kind, "graph": deepcopy(peer.queue_prompt.call_args.args[0]),
            "uploads": deepcopy(peer.files), "artifact": artifact})
        return artifact

    def assert_media_lineage_and_delivery(self, run):
        # Both actual variant submissions must consume the same exact selected
        # primary bytes. File equality alone must not erase version identity.
        variants = []
        for row in self.submissions:
            if row["kind"] != "image":
                continue
            version = AssetReferenceVersion.objects.select_related("generated_from__asset").get(
                generated_artifact=row["artifact"])
            if version.generated_from_id is None:
                self.assertEqual(row["uploads"], {})
                continue
            variants.append(version)
            self.assertEqual(len(row["uploads"]), 1)
            self.assertEqual(sha256(next(iter(row["uploads"].values()))).hexdigest(),
                version.generated_from.asset.checksum)
        self.assertEqual(len(variants), 2)
        self.assertEqual(len({row.generated_from_id for row in variants}), 1)
        self.assertEqual(len({row.setting_revision_id for row in variants}), 2)
        for row in self.submissions:
            if row["kind"] != "video":
                continue
            shot_id = row["attempt"].unit_key.split(":")[1]
            use = ShotReferenceUse.objects.get(shot_id=shot_id)
            self.assertEqual(len(row["uploads"]), 1)
            self.assertEqual(sha256(next(iter(row["uploads"].values()))).hexdigest(), use.asset_sha256)
            self.assertIn(row["graph"]["cr_images_0"]["inputs"]["image"], row["uploads"])
            self.assertIn("等一下！", json.dumps(row["graph"], ensure_ascii=False))
        assembly = AssemblyJob.objects.get(consumer_stage__production_run=run)
        movie = Path(assembly.output_path)
        self.assertTrue(movie.resolve().is_relative_to(self.int_root))
        probe = json.loads(subprocess.run([settings.FFPROBE_BINARY, "-v", "error", "-show_format",
            "-show_streams", "-of", "json", str(movie)], check=True, capture_output=True, timeout=20).stdout)
        self.assertAlmostEqual(float(probe["format"]["duration"]), 10, delta=.2)
        self.assertEqual([source.shot.number for source in assembly.source_items.order_by("ordinal")], [1, 2])
        master = GeneratedArtifact.objects.get(production_run=run, role="episode_master")
        detail = self.api("get", f"/api/v1/generated-artifacts/{master.pk}/")
        response = self.client.get(detail["preview_url"], HTTP_RANGE="bytes=5-127")
        try:
            self.assertEqual(response.status_code, 206)
            self.assertEqual(b"".join(response.streaming_content), movie.read_bytes()[5:128])
        finally:
            response.close()
        self.assertEqual(master.sha256, sha256(movie.read_bytes()).hexdigest())

    def test_empty_library_automatic_chain_submits_selected_references_and_seals_new_delivery(self):
        self.sample = self.int_sample()
        for model in (Asset, CharacterBible, SceneBible, PropBible, AssetReferenceVersion):
            self.assertFalse(model.objects.exists(), model.__name__)
        self.initial_plan = deepcopy(self.sample.run.execution_plan.definition)
        for _ in range(30):
            run = self.advance()
            if run.status == "succeeded":
                break
            self.assertNotIn(run.status, ("failed", "cancelled", "awaiting_approval"), self.state())
            task = claim_next_task()
            if task is not None:
                self.collect(task)
                continue
            assembly = claim_next_assembly(worker_id="int-isolated")
            if assembly is not None:
                with self.captureOnCommitCallbacks(execute=True):
                    finished = process_assembly_job(assembly)
                self.assertEqual(finished.status, "succeeded", finished.error_message)
                continue
            self.fail(f"No runnable work remains: {self.state()}")
        else:
            self.fail(f"New integration chain did not finish: {self.state()}")
        self.assertEqual((CharacterBible.objects.filter(project=self.sample.project).count(),
            SceneBible.objects.filter(project=self.sample.project).count(), PropBible.objects.filter(project=self.sample.project).count()), (1, 1, 1))
        self.assertEqual(len(self.submissions), 7)  # Three primary, two appearances, two reference videos.
        self.assertEqual([row["kind"] for row in self.submissions].count("image"), 5)
        self.assertEqual([row["kind"] for row in self.submissions].count("video"), 2)
        self.assertFalse(ApprovalDecision.objects.filter(stage_run__production_run=run).exclude(actor="recipe_policy").exists())
        self.assertFalse(ApprovalDecision.objects.filter(stage_run__production_run=run, decided_by__isnull=False).exists())
        self.assertFalse(ExecutionPreparation.objects.filter(stage_run__production_run=run, field_values__isnull=False).exists())
        self.assertEqual(self.sample.run.execution_plan.definition, self.initial_plan)
        uses = list(ShotReferenceUse.objects.filter(shot__episode__project=self.sample.project).select_related("reference_version", "shot").order_by("shot__number"))
        self.assertEqual(len(uses), 2)
        videos = [row for row in self.submissions if row["kind"] == "video"]
        for row in videos:
            shot_id = row["attempt"].unit_key.split(":")[1]
            use = next(use for use in uses if str(use.shot_id) == shot_id)
            self.assertEqual(use.source_mode, "generated")
            self.assertEqual(len(row["uploads"]), 1)
            self.assertEqual(sha256(next(iter(row["uploads"].values()))).hexdigest(), use.asset_sha256)
            self.assertIn(row["graph"]["cr_images_0"]["inputs"]["image"], row["uploads"])
            preparation = row["attempt"].preparation
            frozen = preparation.contract.resolved_inputs["inputs"]["logical_values"]["references"][0]
            self.assertEqual(frozen["shot_reference"]["source"]["reference_version_id"], str(use.reference_version_id))
        bundle = ReleaseBundle.objects.get(production_run=run)
        verified = self.api("get", f"/api/v1/release-bundles/{bundle.pk}/verify/")
        self.assertEqual(verified["verification"]["status"], "verified")
        self.assertEqual(len(verified["assemblies"]), 1)
        self.assertEqual({row["generated_artifact_id"] for row in verified["artifacts"]}, {str(row["artifact"].pk) for row in videos})
        self.assert_media_lineage_and_delivery(run)
        attempts = list(GenerationAttempt.objects.filter(stage_run__production_run=run).values())
        self.advance()
        self.assertEqual(list(GenerationAttempt.objects.filter(stage_run__production_run=run).values()), attempts)
