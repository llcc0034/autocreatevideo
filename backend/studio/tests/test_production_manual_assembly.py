"""Real isolated PNG -> formal selection/review -> original assembly queue.

Encoder completion is injected explicitly; this is not real-media acceptance.
"""
from copy import deepcopy
from pathlib import Path
import subprocess
from unittest.mock import patch
from uuid import uuid4

from django.conf import settings
from django.contrib.auth import get_user_model
from django.db import connection
from django.test import TestCase, override_settings
from django.test.utils import CaptureQueriesContext
from rest_framework.test import APIClient

from studio.models import ApprovalDecision, AssemblyJob, Episode, GeneratedArtifact, GenerationAttempt, ProductionAssemblyLink, ProductionManualCommand, Shot, StageRun
from studio.services.generated_artifacts import select_artifact
from studio.services.json_contracts import assign_json_contract
from studio.services.production_manual_actions import perform_manual_action
from studio.services.production_manual_media_review import manual_media_review_detail
from studio.services.production_run_plans import get_run_execution_plan
from studio.services.production_runs import advance_production_run
from studio.services.assembly_queue import claim_next_assembly, process_assembly_job
from studio.tests import test_execution_assembly_sources as fixtures


@override_settings(AI_PROVIDER_ALLOWED_HOSTS=("127.0.0.1",))
class ManualAssemblyTests(TestCase):
    mark_submitted = fixtures.ExecutionAssemblySourceTests.mark_submitted
    archive = fixtures.ExecutionAssemblySourceTests.archive
    resolve = fixtures.ExecutionAssemblySourceTests.resolve

    def setUp(self):
        fixtures.ExecutionAssemblySourceTests.setUp(self)
        self.plan = get_run_execution_plan(self.run)
        for key in ("artifact_selection", "assembly", "delivery"):
            ordinal = next(row["ordinal"] for row in self.plan.value["stage_catalog"]["value"]["stages"] if row["key"] == key)
            row = StageRun(production_run=self.run, stage_key=key, ordinal=ordinal, mode="manual", status="pending", idempotency_key=str(uuid4()))
            assign_json_contract(row, "output_snapshot", {"schema_version": 2, "run_plan_sha256": self.plan.sha256}, schema_version=2)
            row.save()
        self.client = APIClient(); self.client.force_authenticate(self.user)
        self.url = f"/api/v1/production-runs/{self.run.pk}/manual-actions/"
        self.read_url = f"/api/v1/production-runs/{self.run.pk}/manual-assembly/"
        font = Path(settings.MEDIA_ROOT) / "fixture-font.ttf"
        font.write_bytes(b"font used only in command planning")
        self.enterContext(override_settings(ASSEMBLY_AI_LABEL_FONTFILE=str(font)))

    def approve(self):
        select_artifact(self.artifact, user=self.user)
        detail = manual_media_review_detail(self.run)
        perform_manual_action(run_id=self.run.pk, actor=self.user, request={"action": "approve", "stage_key": "artifact_selection",
            "scope": {"kind": "project", "id": str(self.project.pk)}, "unit_key": f"project:{self.project.pk}",
            "expected_run_plan_sha256": self.plan.sha256, "expected_selection_revision": detail["approval"]["revision"],
            "idempotency_key": str(uuid4()), "payload": {"selection": detail["selection"], "expected_selection_sha256": detail["selection_sha256"], "comment": "Reviewed exact test image"}})

    def detail(self):
        result = self.client.get(self.read_url)
        self.assertEqual(result.status_code, 200, result.data)
        self.assertEqual(result["Cache-Control"], "private, no-store")
        return result.data

    def request(self, detail=None):
        detail = detail or self.detail()
        episode = detail["episodes"][0]
        return {"action": "assemble", "stage_key": "assembly", "scope": {"kind": "episode", "id": episode["episode_id"]},
            "unit_key": f"episode:{episode['episode_id']}", "expected_run_plan_sha256": self.plan.sha256,
            "expected_selection_revision": detail["approval"]["revision"], "idempotency_key": str(uuid4()),
            "payload": {"expected_contract_sha256": detail["contract_sha256"], "expected_assembly_job_id": episode["job"]["id"] if episode["job"] else None}}

    def post(self, request):
        return self.client.post(self.url, request, format="json")

    def test_metadata_inventory_empty_unreviewed_and_permissions_never_enqueue(self):
        with patch("studio.services.execution_production_candidates.read_production_media_candidate", side_effect=AssertionError("No GET file probe")):
            detail = self.detail()
            self.assertTrue(detail["blockers"])
            self.assertIsNone(detail["episodes"][0]["shots"][0]["source"])
            self.assertNotIn("fontfile", detail["output"])
        self.assertEqual(self.post(self.request(detail)).status_code, 400)
        self.assertEqual(self.client.get(self.read_url, {"page": 1}).status_code, 400)
        self.client.force_authenticate(get_user_model().objects.create_user(username="assembly-other"))
        self.assertEqual(self.client.get(self.read_url).status_code, 404)
        self.assertEqual(self.post(self.request(detail)).status_code, 404)
        self.assertFalse(AssemblyJob.objects.exists())

    def test_inventory_batches_shots_without_hiding_empty_episodes_or_loading_job_payloads(self):
        episode = Episode.objects.create(project=self.project, number=2, title="第二集")
        empty = Episode.objects.create(project=self.project, number=3, title="尚未分镜")
        for number in (2, 1):
            Shot.objects.create(episode=episode, number=number, title=f"镜头{number}", duration_ms=2000,
                media_type="image", include_audio=False)
        with CaptureQueriesContext(connection) as queries:
            detail = self.detail()
        self.assertEqual([row["episode_id"] for row in detail["episodes"]],
            [str(self.episode.pk), str(episode.pk), str(empty.pk)])
        self.assertEqual([row["number"] for row in detail["episodes"][1]["shots"]], [1, 2])
        self.assertEqual(detail["episodes"][2]["shots"], [])
        self.assertIn("第 3 集尚无正式镜头。", detail["blockers"])
        shot_queries = [row["sql"] for row in queries if 'FROM "studio_shot"' in row["sql"]]
        self.assertEqual(len(shot_queries), 1)
        # M5 retired the old Shot column. Reuse review reads each original
        # relation/revision in one batch, not per shot or through legacy fields.
        self.assertNotIn('"reference_bindings"', shot_queries[0])
        for table in ("studio_shotfieldrevision", "studio_shotscriptsource",
                "studio_shotcharacterrelation", "studio_shotreferenceuse"):
            related_queries = [row["sql"] for row in queries if f'FROM "{table}"' in row["sql"]]
            self.assertEqual(len(related_queries), 1, table)
        self.assertNotIn('"studio_shot"."updated_at"', shot_queries[0])
        link_queries = [row["sql"] for row in queries if 'FROM "studio_productionassemblylink"' in row["sql"] and 'JOIN' in row["sql"]]
        self.assertEqual(len(link_queries), 1)
        for field in ("timeline_payload", "concat_manifest_path", "provenance_manifest"):
            self.assertNotIn(f'"{field}"', link_queries[0])
        query_plan = Shot.objects.filter(episode_id__in=[self.episode.pk, episode.pk, empty.pk]).order_by(
            "episode_id", "number", "id").only("id", "episode_id", "number", "title", "duration_ms", "media_type").explain()
        if connection.vendor == "sqlite":
            self.assertIn("USING INDEX", query_plan)

    def test_enqueue_replay_freeze_and_no_implicit_completion_or_delivery(self):
        # This fixture intentionally starts with only media/assembly stages.
        # The reader must not invent the missing source stage. Supply its exact
        # frozen catalog entry before checking the real submitted-job boundary.
        impact_url = f"/api/v1/production-runs/{self.run.pk}/manual-change-impact/"
        self.assertEqual(self.client.get(impact_url, {"stage_key": "storyboard"}).status_code, 400)
        source = next(row for row in self.plan.value["stage_catalog"]["value"]["stages"] if row["key"] == "storyboard")
        source_stage = StageRun(production_run=self.run, stage_key=source["key"], ordinal=source["ordinal"],
            mode="manual", status="pending", idempotency_key=str(uuid4()))
        assign_json_contract(source_stage, "output_snapshot", {"schema_version": 2, "run_plan_sha256": self.plan.sha256}, schema_version=2)
        source_stage.save()
        self.approve()
        detail = self.detail(); self.assertEqual(detail["blockers"], [])
        body = self.request(detail)
        first = self.post(body)
        self.assertEqual(first.status_code, 202, first.data)
        self.assertEqual(first.data["result_kind"], "assembly_link")
        link = ProductionAssemblyLink.objects.get(pk=first.data["target_id"])
        job = link.assembly_job
        impact = self.client.get(f"/api/v1/production-runs/{self.run.pk}/manual-change-impact/", {"stage_key": "storyboard"})
        self.assertEqual(impact.status_code, 200, impact.data)
        self.assertTrue(impact.data["assembly_locked"])
        self.assertEqual(job.status, "queued")
        self.assertEqual(job.source_items.get().generated_artifact_id, self.artifact.pk)
        self.assertTrue(Path(job.concat_manifest_path).is_file())
        self.assertEqual(self.post(body).data["target_id"], str(link.pk))
        self.assertEqual(AssemblyJob.objects.count(), 1)
        self.assertEqual(self.post({**body, "idempotency_key": str(uuid4())}).status_code, 409)
        with patch("studio.services.production_manual_assembly.resolve_assembly_configuration", side_effect=AssertionError("Frozen, not latest config")):
            updated = self.detail()
        self.assertTrue(updated["frozen"])
        self.assertEqual(updated["contract_sha256"], detail["contract_sha256"])
        self.assertEqual(updated["episodes"][0]["job"]["status"], "queued")
        self.assertEqual(self.post(self.request(updated)).status_code, 400)
        advance_production_run(self.run)
        self.assertEqual(self.run.stages.get(stage_key="assembly").status, "running")
        self.assertEqual(self.run.stages.get(stage_key="delivery").status, "pending")
        self.run.refresh_from_db(); self.assertEqual(self.run.status, "awaiting_input")

    def test_stale_parameters_members_or_revision_and_corrupt_source_do_not_write(self):
        self.approve(); body = self.request()
        changed = deepcopy(body); changed["expected_selection_revision"] += 1
        self.assertEqual(self.post(changed).status_code, 409)
        self.project.width += 2; self.project.save(update_fields=("width",))
        self.assertEqual(self.post(body).status_code, 409)
        current = self.request()
        (Path(settings.MEDIA_ROOT) / self.artifact.object_key).write_bytes(b"corrupted image")
        self.assertEqual(self.post(current).status_code, 400)
        self.assertFalse(AssemblyJob.objects.exists())
        self.assertFalse(ProductionManualCommand.objects.filter(action="assemble").exists())

    def test_timeline_change_requires_explicit_review_before_assembly_and_keeps_generation(self):
        self.approve()
        original = manual_media_review_detail(self.run)
        old_assembly = self.request()
        review_stage = self.run.stages.get(stage_key="artifact_selection")
        original_snapshot = deepcopy(review_stage.output_snapshot)
        old_decision = review_stage.approval_decisions.get()
        attempts = list(GenerationAttempt.objects.values("id", "request_sha256", "result_sha256"))
        artifact_facts = (self.artifact.sha256, self.artifact.manifest_metadata_sha256)
        self.shot.duration_ms += 1000
        self.shot.save(update_fields=("duration_ms",))
        changed = manual_media_review_detail(self.run)
        self.assertEqual(changed["approval"]["status"], "changed")
        self.assertEqual(original["selection"]["items"], changed["selection"]["items"])
        self.assertNotEqual(original["selection_sha256"], changed["selection_sha256"])
        self.assertEqual(changed["selection"]["delivery_timeline"][0]["shots"][0]["duration_ms"], self.shot.duration_ms)
        self.assertTrue(self.detail()["blockers"])
        self.assertIn(self.post(old_assembly).status_code, (400, 409))
        self.assertFalse(AssemblyJob.objects.exists())
        review_stage.refresh_from_db()
        self.assertEqual(review_stage.output_snapshot, original_snapshot)
        old_decision.refresh_from_db()
        self.assertEqual(old_decision.output_snapshot_sha256, review_stage.output_snapshot_sha256)
        stale = {"action": "approve", "stage_key": "artifact_selection",
            "scope": {"kind": "project", "id": str(self.project.pk)}, "unit_key": f"project:{self.project.pk}",
            "expected_run_plan_sha256": self.plan.sha256, "expected_selection_revision": original["approval"]["revision"],
            "idempotency_key": str(uuid4()), "payload": {"selection": original["selection"],
                "expected_selection_sha256": original["selection_sha256"], "comment": "stale review"}}
        self.assertEqual(self.post(stale).status_code, 409)
        self.approve()
        refreshed = manual_media_review_detail(self.run)
        self.assertEqual(refreshed["approval"]["status"], "current")
        self.assertEqual(refreshed["approval"]["revision"], original["approval"]["revision"] + 1)
        self.assertEqual(self.post(self.request()).status_code, 202)
        self.assertEqual(ApprovalDecision.objects.filter(stage_run=review_stage).count(), 2)
        self.artifact.refresh_from_db()
        self.assertEqual((self.artifact.sha256, self.artifact.manifest_metadata_sha256), artifact_facts)
        self.assertEqual(list(GenerationAttempt.objects.values("id", "request_sha256", "result_sha256")), attempts)

    def test_all_delivery_timeline_facts_participate_in_review_digest(self):
        self.approve()
        original = manual_media_review_detail(self.run)
        for obj, key, value in ((self.shot, "number", 9), (self.shot, "title", "新镜头标题"),
                (self.shot, "media_type", "video"), (self.episode, "number", 7), (self.episode, "title", "新集标题")):
            with self.subTest(field=key, model=type(obj).__name__):
                saved = getattr(obj, key)
                setattr(obj, key, value); obj.save(update_fields=(key,))
                current = manual_media_review_detail(self.run)
                self.assertEqual(current["approval"]["status"], "changed")
                self.assertEqual(current["selection"]["items"], original["selection"]["items"])
                setattr(obj, key, saved); obj.save(update_fields=(key,))
        extra = Shot.objects.create(episode=self.episode, number=2, title="新增未选片镜头", duration_ms=1000, media_type="image", include_audio=False)
        self.assertEqual(manual_media_review_detail(self.run)["approval"]["status"], "changed")
        self.assertTrue(self.detail()["blockers"])
        extra.delete()  # Only the isolated test fixture, never operational data.
        self.assertEqual(manual_media_review_detail(self.run)["approval"]["status"], "current")

    def test_episode_clock_change_requires_rereview_and_gap_is_not_silently_closed(self):
        self.approve()
        old = manual_media_review_detail(self.run)
        self.shot.episode_start_ms = 45000
        self.shot.episode_end_ms = 45000 + self.shot.duration_ms
        self.shot.save(update_fields=("episode_start_ms", "episode_end_ms"))
        current = manual_media_review_detail(self.run)
        self.assertEqual(current["approval"]["status"], "changed")
        self.assertEqual(current["selection"]["items"], old["selection"]["items"])
        self.assertIn("45000", " ".join(self.detail()["blockers"]))
        self.assertFalse(AssemblyJob.objects.exists())
        self.shot.episode_start_ms = 0
        self.shot.episode_end_ms = self.shot.duration_ms
        self.shot.save(update_fields=("episode_start_ms", "episode_end_ms"))
        self.approve()
        self.assertEqual(self.detail()["blockers"], [])

    def test_receipt_failure_rolls_back_job_link_frozen_contract_and_new_file(self):
        self.approve(); body = self.request()
        with patch("studio.services.production_manual_commands.ProductionManualCommand.save", side_effect=RuntimeError("receipt commit failed")):
            with self.assertRaises(RuntimeError):
                self.post(body)
        self.assertFalse(AssemblyJob.objects.exists()); self.assertFalse(ProductionAssemblyLink.objects.exists())
        self.assertEqual(list((Path(settings.MEDIA_ROOT) / "assemblies").rglob("*.ffconcat")), [])
        self.assertFalse(self.detail()["frozen"])
        self.assertEqual(self.post(body).status_code, 202)

    def test_failed_job_requires_explicit_retry_preserves_configuration_and_no_auto_retry(self):
        self.approve(); first = self.post(self.request())
        self.assertEqual(first.status_code, 202, first.data)
        link = ProductionAssemblyLink.objects.get(pk=first.data["target_id"])
        old_job = link.assembly_job
        old_job.status = "failed"; old_job.error_message = "Encoder fixture failure"
        old_job.save(update_fields=("status", "error_message"))
        advance_production_run(self.run)
        self.assertEqual(AssemblyJob.objects.count(), 1)
        self.assertEqual(self.run.stages.get(stage_key="assembly").status, "blocked")
        retry = self.request()
        wrong = deepcopy(retry); wrong["payload"]["expected_assembly_job_id"] = str(uuid4())
        self.assertEqual(self.post(wrong).status_code, 409)
        with patch("studio.services.assembly.resolve_assembly_configuration", side_effect=AssertionError("No new settings")):
            result = self.post(retry)
        self.assertEqual(result.status_code, 202, result.data)
        link.refresh_from_db(); self.assertNotEqual(link.assembly_job_id, old_job.pk)
        self.assertEqual(link.assembly_job.source_signature, old_job.source_signature)
        self.assertEqual(link.assembly_job.ffmpeg_command[1:-1], old_job.ffmpeg_command[1:-1])
        self.assertEqual(self.post(retry).status_code, 200)
        self.assertEqual(AssemblyJob.objects.count(), 2)
        self.assertEqual(self.run.stages.get(stage_key="delivery").status, "pending")

    def test_worker_completion_archives_original_master_without_auto_delivery(self):
        self.approve()
        self.assertEqual(self.post(self.request()).status_code, 202)
        def encoder(command, **kwargs):
            Path(command[-1]).write_bytes(b"isolated encoder-result fixture, not a playable MP4")
            return subprocess.CompletedProcess(command, 0, stdout="", stderr="")
        with self.captureOnCommitCallbacks(execute=True), patch("studio.services.assembly_queue._run_ffmpeg", side_effect=encoder):
            job = claim_next_assembly(worker_id="manual-assembly-test")
            result = process_assembly_job(job)
        self.assertEqual(result.status, "succeeded")
        master = GeneratedArtifact.objects.get(production_run=self.run, kind="assembly")
        self.assertEqual(master.manifest_key, f"assembly:{job.pk}")
        self.assertEqual(self.run.stages.get(stage_key="assembly").status, "succeeded")
        self.assertEqual(self.run.stages.get(stage_key="delivery").status, "pending")
        self.run.refresh_from_db(); self.assertEqual(self.run.status, "awaiting_input")
        self.assertEqual(self.detail()["episodes"][0]["job"]["status"], "succeeded")
        self.assertEqual(self.post(self.request()).status_code, 400)

    def test_incomplete_delivery_and_post_freeze_content_drift_are_explicit(self):
        self.approve()
        another = Episode.objects.create(project=self.project, number=2, title="Not prepared")
        detail = self.detail()
        self.assertTrue(detail["blockers"])
        self.assertEqual(self.post(self.request(detail)).status_code, 400)
        another.delete()
        self.assertEqual(self.post(self.request()).status_code, 202)
        self.shot.duration_ms += 500
        self.shot.save(update_fields=("duration_ms",))
        changed = self.detail()
        self.assertTrue(changed["frozen"]); self.assertTrue(changed["blockers"])
        job = AssemblyJob.objects.get(); job.status = "failed"; job.save(update_fields=("status",))
        self.assertEqual(self.post(self.request()).status_code, 400)
        self.assertEqual(AssemblyJob.objects.count(), 1)
