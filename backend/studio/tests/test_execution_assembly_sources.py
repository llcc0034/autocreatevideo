"""Preparation/adoption-backed assembly source checks with real isolated PNGs."""
from pathlib import Path
import json
from uuid import uuid4

from django.conf import settings
from django.test import TestCase, override_settings

from studio.models import AssemblyJob, AssemblySourceItem, Episode, GeneratedArtifact, Shot
from studio.services.assembly import AssemblyError, _assembly_artifact_evidence, _ordered_frozen_sources_for_episode, _resolve_output, verify_assembly_sources
from studio.services.generated_artifacts import select_artifact
from studio.services.production_manual_actions import perform_manual_action
from studio.services.production_runs import _selected_assembly_sources
from studio.services.production_run_plans import get_run_execution_plan
from studio.services.provenance import persist_assembly_provenance, verified_assembly_manifest
from studio.tests import test_execution_production_workflow_artifacts as fixtures
from studio.tests.test_assembly import frozen_sources_for_test, selection_for_test


@override_settings(AI_PROVIDER_ALLOWED_HOSTS=("127.0.0.1",))
class ExecutionAssemblySourceTests(TestCase):
    mark_submitted = fixtures.ProductionWorkflowArtifactTests.mark_submitted
    archive = fixtures.ProductionWorkflowArtifactTests.archive

    def resolve(self, definition, stage):
        from studio.services.execution_request_preparation import resolve_execution_request_facts
        from studio.services.json_contracts import json_contract

        self.episode = Episode.objects.create(project=self.project, number=1, title="Explicit episode")
        self.shot = Shot.objects.create(episode=self.episode, number=1, title="Actual image target", duration_ms=2500,
            media_type="image", include_audio=False)
        return resolve_execution_request_facts(definition, self.intent.value, owner=self.user, unit_key="image-unit",
            max_media_bytes=65536, parameter_policies=[], resolve_parameter_source=lambda _: self.fail("No inferred input"),
            review_contract=json_contract({"schema_version": 1, "mode": "manual"}), project_id=self.project.pk,
            production_run_id=str(self.run.pk), production_scope={"kind": "shot", "id": str(self.shot.pk)})

    def setUp(self):
        fixtures.ProductionWorkflowArtifactTests.setUp(self)
        self.artifact, = self.archive()

    def frozen_item(self, artifact):
        job = AssemblyJob.objects.create(owner=self.user, project=self.project, episode=self.episode,
            scope="episode", status="planned", dry_run=True, source_contract_version=5, max_attempts=1)
        AssemblySourceItem.objects.create(assembly_job=job, ordinal=1, shot=self.shot, generated_artifact=artifact,
            storage_backend=artifact.storage_backend, object_key=artifact.object_key, mime_type=artifact.mime_type,
            size_bytes=artifact.size_bytes, sha256=artifact.sha256, media_kind="image", start_ms=0, duration_ms=2500)
        return job

    def seal_source(self, artifact):
        """Test sidecar identity, not an FFmpeg/model quality result."""
        job = self.frozen_item(artifact)
        source, _ = _assembly_artifact_evidence(artifact)
        output = Path(settings.MEDIA_ROOT) / "assemblies" / str(self.user.pk) / str(self.project.pk) / f"assembly-{job.pk}.mp4"
        output.parent.mkdir(parents=True, exist_ok=True)
        output.write_bytes(b"isolated sidecar integrity fixture")
        job.output_path = str(output)
        job.ffmpeg_command = ["ffmpeg", "-i", str(source["path"]), str(output)]
        job.save(update_fields=("output_path", "ffmpeg_command"))
        persist_assembly_provenance(job)
        document = verified_assembly_manifest(job)
        self.assertNotIn("PRIVATE PROMPT", json.dumps(document))
        self.assertNotIn(str(output.parent), json.dumps(document))
        return document["source_outputs"][0]

    def test_generated_comfy_image_uses_real_target_and_formal_selection_not_old_prompt(self):
        with self.assertRaises(AssemblyError):
            _selected_assembly_sources(self.run)
        self.artifact.status = "selected"
        with self.assertRaises(AssemblyError):
            _assembly_artifact_evidence(self.artifact)
        self.artifact.status = "available"
        select_artifact(self.artifact, user=self.user)
        self.artifact.refresh_from_db()
        mapping = _selected_assembly_sources(self.run)
        self.assertEqual(mapping, {str(self.shot.pk): {"generated_artifact_id": str(self.artifact.pk)}})
        selections = _ordered_frozen_sources_for_episode(self.episode, frozen_sources_for_test(self.episode, mapping)["episodes"][0])
        self.assertEqual(selections[0].artifact.pk, self.artifact.pk)
        clip = _resolve_output(selections[0])
        self.assertEqual(clip.duration_seconds, 2.5)
        self.assertEqual(clip.path.read_bytes(), self.output.read_bytes())
        verify_assembly_sources(self.frozen_item(self.artifact))
        source = self.seal_source(self.artifact)
        self.assertEqual(source["source_kind"], "execution")
        self.assertEqual(source["preparation_id"], str(self.preparation.pk))
        self.assertEqual(source["request_sha256"], self.attempt.request_sha256)
        self.assertEqual(source["execution_instance_version_id"], str(self.version.pk))
        self.assertIsNone(source["asset_id"])
        self.assertNotIn("prompt_artifact_id", source)
        self.assertNotIn("generated_workflow", {field.name for field in self.task._meta.fields})

    def test_manually_adopted_asset_has_no_attempt_but_is_a_valid_frozen_source(self):
        scope = {"kind": "shot", "id": str(self.shot.pk)}
        receipt, created = perform_manual_action(run_id=self.run.pk, actor=self.user, request={"action": "adopt",
            "stage_key": "media_generation", "scope": scope, "unit_key": f"shot:{self.shot.pk}",
            "expected_run_plan_sha256": get_run_execution_plan(self.run).sha256, "expected_selection_revision": 0,
            "idempotency_key": str(uuid4()), "payload": {"source": {"kind": "asset", "asset_id": str(self.asset.pk),
                "expected_sha256": self.asset.checksum, "media_kind": "image"}}})
        self.assertTrue(created)
        artifact = GeneratedArtifact.objects.get(production_run=self.run, status="selected")
        self.assertIsNone(artifact.generation_attempt_id)
        self.assertIsNone(artifact.generation_task_id)
        mapping = _selected_assembly_sources(self.run)
        self.assertEqual(mapping, {str(self.shot.pk): {"generated_artifact_id": str(artifact.pk)}})
        clip = _resolve_output(selection_for_test(self.shot, artifact))
        self.assertEqual(clip.duration_seconds, 2.5)
        verify_assembly_sources(self.frozen_item(artifact))
        source = self.seal_source(artifact)
        self.assertEqual(source["source_kind"], "asset")
        self.assertEqual(source["asset_id"], str(self.asset.pk))
        self.assertIsNone(source["generation_attempt_id"])
        self.assertIsNone(source["preparation_id"])
        self.assertIsNone(source["request_sha256"])
        self.assertTrue(Path(clip.path).is_file())

    def test_missing_shot_late_candidate_and_corrupt_bytes_do_not_fallback(self):
        select_artifact(self.artifact, user=self.user)
        self.artifact.refresh_from_db()
        other = Shot.objects.create(episode=self.episode, number=2, title="Unfilled", duration_ms=1000,
            media_type="image", include_audio=False)
        with self.assertRaises(AssemblyError):
            _selected_assembly_sources(self.run)
        with self.assertRaises(AssemblyError):
            _resolve_output(selection_for_test(other, self.artifact))
        other.delete()
        self.attempt.ignore_late_result = True
        self.attempt.save(update_fields=("ignore_late_result",))
        with self.assertRaises(AssemblyError):
            _selected_assembly_sources(self.run)
        self.attempt.ignore_late_result = False
        self.attempt.save(update_fields=("ignore_late_result",))
        detail, _ = _assembly_artifact_evidence(self.artifact)
        Path(detail["path"]).write_bytes(b"corrupt isolated archive")
        with self.assertRaises(AssemblyError):
            _selected_assembly_sources(self.run)
