"""Real isolated database deletion and media reuse; never the current workspace DB."""
from datetime import timedelta
from hashlib import sha256
from io import StringIO
from pathlib import Path
from tempfile import TemporaryDirectory
from unittest.mock import patch
from uuid import uuid4

from django.contrib.auth import get_user_model
from django.core.files.base import ContentFile
from django.core.management import call_command
from django.core.management.base import CommandError
from django.db import connection
from django.test import TestCase, override_settings
from django.utils import timezone
from rest_framework.test import APIClient

from studio import models as m
from studio.services.history_cleanup import HISTORY_MODELS
from studio.services.execution_preparation_queue import (
    advance_next_execution_preparation, claim_next_execution_preparation, process_claimed_execution_preparation,
)
from studio.tests import test_execution_asset_admission as preparation_fixtures
from studio.tests.factories import build_story
from studio.tests.test_asset_management_api import image_upload


URL = "/api/v1/maintenance/history/"
PAUSE_URL = URL + "background-pause/"


class HistoryCleanupTests(TestCase):
    def setUp(self):
        root = TemporaryDirectory()
        self.addCleanup(root.cleanup)
        override = override_settings(MEDIA_ROOT=root.name)
        override.enable(); self.addCleanup(override.disable)
        self.user, self.project, self.character, self.scene, self.episode, self.shot, _ = build_story("history-cleanup")
        self.user.is_staff = self.user.is_superuser = True
        self.user.save()
        self.client = APIClient(); self.client.force_authenticate(self.user)
        raw = image_upload().read()
        self.asset = m.Asset(owner=self.user, name="原生成主图", kind="character", mime_type="image/png",
            checksum=sha256(raw).hexdigest(), source_info="原模型生成来源说明",
            metadata={"generation_purpose": "bible_reference_image", "production_run_id": str(uuid4())})
        self.asset.file.save("kept.png", ContentFile(raw))
        self.path = Path(self.asset.file.path)
        self.original_bytes = raw
        self.character.anchor_asset = self.asset; self.character.save()
        self.character.reference_assets.add(self.asset)

    def preview(self):
        response = self.client.get(URL)
        self.assertEqual(response.status_code, 200, response.data)
        return response.data

    def execute(self, plan):
        if not hasattr(self, "pause_token"):
            self.pause_token = str(uuid4())
        self.client.post(PAUSE_URL, {"token": self.pause_token}, format="json")
        return self.client.post(URL, {"plan_sha256": plan["plan_sha256"], "confirmation": plan["confirmation"],
            "pause_token": self.pause_token}, format="json")

    def test_clear_history_preserves_assets_users_config_and_new_project_reuse(self):
        config_before = list(m.ConfigRevision.objects.filter(scope_type="system").values())
        plan = self.preview()
        self.assertTrue(plan["can_execute"], plan["blockers"])
        response = self.execute(plan)
        self.assertEqual(response.status_code, 200, response.data)
        for name in HISTORY_MODELS:
            self.assertFalse(getattr(m, name)._base_manager.exists(), name)
        self.assertEqual(list(m.ConfigRevision.objects.filter(scope_type="system").values()), config_before)
        self.assertTrue(get_user_model().objects.filter(pk=self.user.pk).exists())
        self.asset.refresh_from_db()
        self.assertEqual(self.path.read_bytes(), self.original_bytes)
        self.assertEqual(self.asset.source_info, "原模型生成来源说明")
        self.assertNotIn("generation_purpose", self.asset.metadata)
        self.assertEqual(self.asset.metadata["cleared_history_origin"]["original"]["generation_purpose"], "bible_reference_image")
        response = self.client.post('/api/v1/projects/', {"title": "清理后的新项目", "language": "Chinese",
            "aspect_ratio": "16:9", "width": 1280, "height": 720, "fps": 24,
            "default_shot_duration_ms": 5000, "continuity_seed": 42}, format="json")
        self.assertEqual(response.status_code, 201, response.data)
        project = m.SeriesProject.objects.get(pk=response.data["id"])
        entity = m.CharacterBible.objects.create(project=project, slug="new", name="新人物", stable_speaker_id="S1", anchor_asset=self.asset)
        from studio.services.production_runs import _bible_binding_asset_fact
        fact = _bible_binding_asset_fact(self.asset, project=project, entity_type="character", entity=entity)
        self.assertEqual(fact["checksum"], self.asset.checksum)
        self.assertIsNone(fact["generated_artifact_id"])
        connection.check_constraints()

    def test_active_task_and_unknown_submission_block_without_changes(self):
        task = m.GenerationTask.objects.create(owner=self.user, project=self.project, kind="image", status="running")
        plan = self.preview()
        self.assertFalse(plan["can_execute"])
        blocker = next(item for item in plan["blockers"] if item["id"] == str(task.pk))
        self.assertEqual(blocker["label"], "生成任务")
        self.assertEqual(blocker["detail_url"], f"/workflows?kind=generation_task&id={task.pk}")
        self.assertEqual(self.execute(plan).status_code, 409)
        task.status = "failed"; task.submission_token = "unknown-submission"; task.save()
        self.assertFalse(self.preview()["can_execute"])
        self.assertTrue(m.SeriesProject.objects.filter(pk=self.project.pk).exists())
        self.assertEqual(self.path.read_bytes(), self.original_bytes)

    def test_finished_assembly_token_is_history_but_unfinished_claim_still_blocks(self):
        job = m.AssemblyJob.objects.create(owner=self.user, project=self.project, episode=self.episode,
            scope="episode", status="succeeded", max_attempts=1, claim_token=uuid4(), finished_at=timezone.now())
        plan = self.preview()
        self.assertTrue(plan["can_execute"], plan["blockers"])
        job.finished_at = None; job.save()
        self.assertFalse(self.preview()["can_execute"])
        job.finished_at = timezone.now(); job.save()
        response = self.execute(self.preview())
        self.assertEqual(response.status_code, 200, response.data)
        self.assertFalse(m.AssemblyJob.objects.exists())

    def test_stale_preview_and_permission_and_confirmation(self):
        plan = self.preview()
        self.project.title = "预览之后变更"; self.project.save()
        self.assertEqual(self.execute(plan).status_code, 409)
        self.assertEqual(self.client.post(URL, {"plan_sha256": plan["plan_sha256"]}, format="json").status_code, 400)
        self.user.is_superuser = False; self.user.save()
        self.assertEqual(self.client.get(URL).status_code, 403)
        self.assertEqual(self.execute(plan).status_code, 403)

    def test_polling_timestamp_only_does_not_invalidate_reviewed_scope(self):
        plan = self.preview()
        m.SeriesProject.objects.filter(pk=self.project.pk).update(updated_at=timezone.now())
        self.assertEqual(self.preview()["plan_sha256"], plan["plan_sha256"])
        self.assertEqual(self.execute(plan).status_code, 200)

    def test_new_history_member_invalidates_preview_without_deleting(self):
        plan = self.preview()
        task = m.GenerationTask.objects.create(owner=self.user, project=self.project, kind="image", status="cancelled")
        self.assertNotEqual(self.preview()["plan_sha256"], plan["plan_sha256"])
        self.assertEqual(self.execute(plan).status_code, 409)
        self.assertTrue(m.GenerationTask.objects.filter(pk=task.pk).exists())
        self.assertEqual(self.path.read_bytes(), self.original_bytes)

    def test_retained_configuration_change_invalidates_preview(self):
        recipe = m.ProductionRecipeVersion.objects.create(recipe_key="cleanup-config", version=1,
            definition={"description": "reviewed"}, definition_sha256="a" * 64)
        plan = self.preview()
        m.ProductionRecipeVersion.objects.filter(pk=recipe.pk).update(
            definition={"description": "changed"}, definition_sha256="b" * 64)
        self.assertNotEqual(self.preview()["plan_sha256"], plan["plan_sha256"])
        self.assertEqual(self.execute(plan).status_code, 409)
        self.assertTrue(m.SeriesProject.objects.filter(pk=self.project.pk).exists())

    def test_transaction_rollback_keeps_assets_and_history(self):
        plan = self.preview()
        with patch("studio.services.history_cleanup.connection.check_constraints", side_effect=RuntimeError("injected failure")):
            with self.assertRaises(RuntimeError):
                self.execute(plan)
        self.assertTrue(m.SeriesProject.objects.filter(pk=self.project.pk).exists())
        self.asset.refresh_from_db()
        self.assertIn("generation_purpose", self.asset.metadata)

    def test_project_cover_becomes_independent_without_losing_bytes(self):
        self.asset.kind = "reference"
        self.asset.metadata = {"usage": "project_cover", "project_id": str(self.project.pk)}
        self.asset.save()
        self.project.cover_asset = self.asset; self.project.save()
        response = self.execute(self.preview())
        self.assertEqual(response.status_code, 200, response.data)
        self.asset.refresh_from_db()
        from studio.views import _is_dedicated_project_cover
        self.assertFalse(_is_dedicated_project_cover(self.asset))
        self.assertEqual(self.path.read_bytes(), self.original_bytes)

    def test_live_configuration_input_reference_blocks(self):
        m.ProductionRecipeVersion.objects.create(recipe_key="history-bound", version=1,
            definition={"source_id": str(self.project.pk)}, definition_sha256="a" * 64)
        plan = self.preview()
        self.assertFalse(plan["can_execute"])
        self.assertIn("直接引用历史对象", str(plan["blockers"]))
        self.assertEqual(self.execute(plan).status_code, 409)

    def test_fresh_install_check_refuses_existing_history_without_deleting(self):
        with self.assertRaises(CommandError):
            call_command("assert_clean_install", stdout=StringIO())
        self.assertTrue(m.SeriesProject.objects.filter(pk=self.project.pk).exists())

    def test_cleared_origin_is_server_owned_but_other_metadata_can_be_edited(self):
        # Metadata PATCH merges omissions; clients cannot forge or remove the
        # explicit historical provenance marker after cleanup.
        self.asset.kind = "reference"
        self.asset.metadata = {"cleared_history_origin": {"original": {"production_run_id": str(uuid4())}}}
        self.asset.save()
        original = self.asset.metadata["cleared_history_origin"]
        response = self.client.patch(f"/api/v1/assets/{self.asset.pk}/", {"metadata": {"tags": ["保留"]}}, format="json")
        self.assertEqual(response.status_code, 200, response.data)
        self.asset.refresh_from_db()
        self.assertEqual(self.asset.metadata["cleared_history_origin"], original)
        response = self.client.patch(f"/api/v1/assets/{self.asset.pk}/", {"metadata": {"cleared_history_origin": None}}, format="json")
        self.assertEqual(response.status_code, 400, response.data)


@override_settings(AI_PROVIDER_ALLOWED_HOSTS=("api.example.com",))
class HistoryCleanupPreparationQueueTests(TestCase):
    setUpTestData = preparation_fixtures.ExecutionAssetAdmissionTests.__dict__["setUpTestData"]
    version = preparation_fixtures.ExecutionAssetAdmissionTests.version
    attempt = preparation_fixtures.ExecutionAssetAdmissionTests.attempt
    finish = preparation_fixtures.ExecutionAssetAdmissionTests.finish
    preview = HistoryCleanupTests.preview
    execute = HistoryCleanupTests.execute

    def setUp(self):
        network = self.enterContext(patch("requests.sessions.Session.request", side_effect=AssertionError("No remote calls in cleanup tests")))
        self.addCleanup(network.assert_not_called)
        preparation_fixtures.ExecutionAssetAdmissionTests.setUp(self)
        self.actor.is_staff = self.actor.is_superuser = True
        self.actor.save()
        self.client = APIClient()
        self.client.force_authenticate(self.actor)
        self.prep = advance_next_execution_preparation()
        self.assertEqual(self.prep.reason_code, "execution_smoke_missing")

    def due(self):
        m.ExecutionPreparation.objects.filter(pk=self.prep.pk).update(
            next_check_at=timezone.now() - timedelta(seconds=1))

    def test_blocker_links_and_task_cancel_action_follow_exact_asset_preparation(self):
        m.ExecutionPreparation.objects.filter(pk=self.prep.pk).update(claim_token=uuid4(), claim_until=timezone.now() + timedelta(minutes=1))
        blocker = next(item for item in self.preview()["blockers"] if item["id"] == str(self.prep.pk))
        self.assertEqual(blocker["detail_url"], f"/workflows?kind=preparation&id={self.prep.pk}")
        self.assertIn(self.prep.asset_job.name, blocker["label"])
        url = f"/api/v1/task-center/preparation/{self.prep.pk}/"
        for state in ("preparing", "queued", "running", "cancel_requested", "cancelled", "succeeded", "uncertain"):
            m.AssetGenerationJob.objects.filter(pk=self.prep.asset_job_id).update(status=state)
            response = self.client.get(url)
            self.assertEqual(response.status_code, 200, response.data)
            self.assertEqual(response.data["actions"], {"cancel_smoke": False,
                "cancel_asset": state in {"preparing", "queued", "running"}})
        other = get_user_model().objects.create_user(username="unrelated-cleanup-viewer")
        self.client.force_authenticate(other)
        self.assertEqual(self.client.get(url).status_code, 404)

    def test_original_preview_survives_real_blocked_worker_rechecks(self):
        plan = self.preview()
        self.assertTrue(plan["can_execute"], plan["blockers"])
        original_revision = self.prep.revision
        for _ in range(3):
            self.due()
            current = advance_next_execution_preparation()
            self.assertEqual(current.revision, original_revision)
            self.assertIsNone(current.claim_token)
            self.assertIsNone(current.request_frozen_at)
            self.assertEqual(self.preview()["plan_sha256"], plan["plan_sha256"])
        response = self.execute(plan)
        self.assertEqual(response.status_code, 200, response.data)
        self.assertFalse(m.AssetGenerationJob.objects.exists())
        self.assertFalse(m.ExecutionPreparation.objects.exists())
        connection.check_constraints()

    def test_claim_is_checked_at_submit_not_misreported_as_scope_change(self):
        plan = self.preview()
        self.due()
        claim = claim_next_execution_preparation()
        claimed = self.preview()
        self.assertEqual(claimed["plan_sha256"], plan["plan_sha256"])
        self.assertFalse(claimed["can_execute"])
        rejected = self.execute(plan)
        self.assertEqual(rejected.status_code, 409, rejected.data)
        self.assertIn(str(claim.pk), str(rejected.data))
        self.assertNotIn("清理范围已变化", str(rejected.data))
        self.assertTrue(m.AssetGenerationJob.objects.filter(pk=self.job.pk).exists())
        process_claimed_execution_preparation(claim)
        self.assertEqual(self.execute(plan).status_code, 200)

    def test_real_preparation_fact_change_still_rejects_original_preview(self):
        plan = self.preview()
        m.ExecutionPreparation.objects.filter(pk=self.prep.pk).update(
            reason_code="execution_resource_inactive", revision=self.prep.revision + 1)
        self.assertNotEqual(self.preview()["plan_sha256"], plan["plan_sha256"])
        self.assertEqual(self.execute(plan).status_code, 409)
        self.assertTrue(m.AssetGenerationJob.objects.filter(pk=self.job.pk).exists())

    def test_pause_drains_existing_claim_without_reclaim_then_cleanup_resumes(self):
        from studio.services.history_cleanup_pause import PAUSE_KEY
        self.due()
        claim = claim_next_execution_preparation()
        self.pause_token = str(uuid4())
        response = self.client.post(PAUSE_URL, {"token": self.pause_token}, format="json")
        self.assertEqual(response.status_code, 200, response.data)
        self.assertFalse(self.preview()["can_execute"])
        # Work claimed before the pause can settle truthfully, not be cancelled.
        process_claimed_execution_preparation(claim)
        self.due()
        self.assertIsNone(claim_next_execution_preparation())
        self.prep.refresh_from_db()
        self.assertEqual(self.prep.status, "blocked")
        self.assertIsNone(self.prep.claim_token)
        plan = self.preview()
        self.assertTrue(plan["can_execute"], plan["blockers"])
        self.assertEqual(self.execute(plan).status_code, 200)
        self.assertFalse(m.WorkerLease.objects.get(pk=PAUSE_KEY).holder_id)

    def test_expired_pause_resumes_original_claim_path(self):
        from studio.services.history_cleanup_pause import PAUSE_KEY
        token = str(uuid4())
        self.assertEqual(self.client.post(PAUSE_URL, {"token": token}, format="json").status_code, 200)
        self.due()
        self.assertIsNone(claim_next_execution_preparation())
        m.WorkerLease.objects.filter(pk=PAUSE_KEY).update(expires_at=timezone.now() - timedelta(seconds=1))
        self.assertIsNotNone(claim_next_execution_preparation())


class HistoryCleanupPauseTests(TestCase):
    def setUp(self):
        self.actor = get_user_model().objects.create_superuser(username="pause-admin", password="test-only-pass")
        self.client = APIClient(); self.client.force_authenticate(self.actor)
        self.token = str(uuid4())

    def pause(self, token=None):
        return self.client.post(PAUSE_URL, {"token": token or self.token}, format="json")

    def test_pause_status_is_read_only_explicit_and_does_not_disclose_owner(self):
        from studio.services.history_cleanup_pause import PAUSE_KEY
        response = self.client.get(PAUSE_URL)
        self.assertEqual(response.status_code, 200)
        self.assertEqual(set(response.data), {"active", "expires_at", "server_time", "include_maintenance"})
        self.assertFalse(response.data["active"])
        self.assertIsNone(response.data["expires_at"])
        self.assertFalse(m.WorkerLease.objects.filter(pk=PAUSE_KEY).exists())
        paused = self.pause().data
        before = list(m.WorkerLease.objects.filter(pk=PAUSE_KEY).values())
        response = self.client.get(PAUSE_URL)
        self.assertTrue(response.data["active"])
        self.assertEqual(response.data["expires_at"], paused["expires_at"])
        self.assertNotIn(self.token, str(response.data))
        self.assertEqual(list(m.WorkerLease.objects.filter(pk=PAUSE_KEY).values()), before)
        m.WorkerLease.objects.filter(pk=PAUSE_KEY).update(expires_at=timezone.now() - timedelta(seconds=1))
        response = self.client.get(PAUSE_URL)
        self.assertFalse(response.data["active"])
        self.assertIsNone(response.data["expires_at"])
        self.assertTrue(m.WorkerLease.objects.get(pk=PAUSE_KEY).holder_id)  # GET does not release/write.
        self.actor.is_superuser = False; self.actor.save()
        self.assertEqual(self.client.get(PAUSE_URL).status_code, 403)

    def test_read_is_read_only_pause_owned_idempotent_and_other_window_cannot_release(self):
        from studio.services.history_cleanup_pause import PAUSE_KEY
        self.assertEqual(self.client.get(URL).status_code, 200)
        self.assertFalse(m.WorkerLease.objects.filter(pk=PAUSE_KEY).exists())
        first = self.pause()
        self.assertEqual(first.status_code, 200, first.data)
        self.assertEqual(first.data, self.pause().data)
        other = str(uuid4())
        self.assertEqual(self.pause(other).status_code, 409)
        self.assertEqual(self.client.delete(PAUSE_URL, {"token": other}, format="json").status_code, 409)
        self.assertEqual(self.client.delete(PAUSE_URL, {"token": self.token}, format="json").status_code, 204)
        self.assertEqual(self.client.delete(PAUSE_URL, {"token": self.token}, format="json").status_code, 204)
        self.assertEqual(self.pause(other).status_code, 200)

    def test_permissions_and_expired_session_cannot_clear_even_matching_preview(self):
        from studio.services.history_cleanup_pause import PAUSE_KEY
        self.pause()
        plan = self.client.get(URL).data
        m.WorkerLease.objects.filter(pk=PAUSE_KEY).update(expires_at=timezone.now() - timedelta(seconds=1))
        response = self.client.post(URL, {"pause_token": self.token, "plan_sha256": plan["plan_sha256"],
            "confirmation": plan["confirmation"]}, format="json")
        self.assertEqual(response.status_code, 409, response.data)
        self.assertIn("暂停已到期", str(response.data))
        self.actor.is_superuser = False; self.actor.save()
        self.assertEqual(self.pause().status_code, 403)
        self.assertEqual(self.client.delete(PAUSE_URL, {"token": self.token}, format="json").status_code, 403)

    def test_pause_stops_scheduled_probe_creation_but_existing_probe_drains(self):
        from studio.services.runtime_host_probe_queue import (enqueue_due_runtime_host_probes,
            enqueue_runtime_host_probe, claim_next_runtime_host_probe)
        host = m.RuntimeHost.objects.create(key="pause-host", name="Pause test host", status="unavailable")
        attempt, _ = enqueue_runtime_host_probe(host, trigger="scheduled")
        self.assertEqual(self.client.post(PAUSE_URL, {"token": self.token, "include_maintenance": True}, format="json").status_code, 200)
        self.assertEqual(enqueue_due_runtime_host_probes(), 0)
        self.assertEqual(m.RuntimeHostProbeAttempt.objects.count(), 1)
        claimed = claim_next_runtime_host_probe("isolated-worker")
        self.assertEqual(claimed.pk, attempt.pk)
        self.assertEqual(claimed.status, "running")
        report = self.client.get(URL, {"include_maintenance": True}).data
        self.assertFalse(report["can_execute"])
        self.assertIn(str(attempt.pk), str(report["blockers"]))
        blocker = next(item for item in report["blockers"] if item["id"] == str(attempt.pk))
        self.assertEqual(blocker["label"], "主机探测 · Pause test host")
        self.assertEqual(blocker["detail_url"], f"/settings?section=runtime&probe_attempt={attempt.pk}")
        self.assertEqual(blocker["status"], "running")
        self.assertIsNotNone(blocker["started_at"])
        self.assertIsNotNone(blocker["heartbeat_at"])
        # Manual probes remain explicit user operations; no scheduler bypass.
        another = m.RuntimeHost.objects.create(key="manual-pause-host", name="Manual host", status="unavailable")
        manual, created = enqueue_runtime_host_probe(another, created_by=self.actor)
        self.assertTrue(created); self.assertEqual(manual.trigger, "manual")

    def test_business_scope_keeps_running_probe_and_its_finish_does_not_change_digest(self):
        from studio.services.runtime_host_probe_queue import enqueue_runtime_host_probe, claim_next_runtime_host_probe
        host = m.RuntimeHost.objects.create(key="business-probe", name="Running probe", status="unavailable")
        probe, _ = enqueue_runtime_host_probe(host, trigger="scheduled")
        claimed = claim_next_runtime_host_probe("isolated-worker")
        project = m.SeriesProject.objects.create(owner=self.actor, title="Delete only business", language="Chinese",
            aspect_ratio="16:9", width=1280, height=720, fps=24, default_shot_duration_ms=5000, continuity_seed=1)
        first = self.client.get(URL).data
        self.assertFalse(first["include_maintenance"])
        self.assertTrue(first["can_execute"], first["blockers"])
        self.assertFalse(self.client.get(URL, {"include_maintenance": True}).data["can_execute"])
        self.pause()
        another = m.RuntimeHost.objects.create(key="business-scheduled", name="Scheduled while business paused", status="unavailable")
        scheduled, created = enqueue_runtime_host_probe(another, trigger="scheduled")
        self.assertTrue(created)  # Business pause does not stop scheduled probes.
        # Scope changed because we created configuration; take a new business preview.
        first = self.client.get(URL).data
        snapshot = m.HostCapabilitySnapshot.objects.create(host=host, probe_attempt=probe, revision=1,
            probe_status="succeeded", details_sha256="a" * 64)
        m.RuntimeHostProbeAttempt.objects.filter(pk=probe.pk).update(status="succeeded", claim_token=None,
            finished_at=timezone.now(), heartbeat_at=timezone.now(), progress_marker="phase:succeeded")
        m.RuntimeHost.objects.filter(pk=host.pk).update(status="active", name="Refreshed host name", last_seen_at=timezone.now(), fingerprint_sha256="b" * 64)
        second = self.client.get(URL).data
        self.assertEqual(first["plan_sha256"], second["plan_sha256"])
        response = self.client.post(URL, {"pause_token": self.token, "plan_sha256": first["plan_sha256"], "confirmation": first["confirmation"]}, format="json")
        self.assertEqual(response.status_code, 200, response.data)
        self.assertFalse(m.SeriesProject.objects.filter(pk=project.pk).exists())
        self.assertTrue(m.RuntimeHostProbeAttempt.objects.filter(pk=scheduled.pk, status="queued").exists())
        self.assertTrue(m.HostCapabilitySnapshot.objects.filter(pk=snapshot.pk).exists())
        connection.check_constraints()

    def test_maintenance_scope_requires_matching_pause_and_preview_and_deletes_only_when_terminal(self):
        host = m.RuntimeHost.objects.create(key="scope-host", name="Scope host", status="active")
        probe = m.RuntimeHostProbeAttempt.objects.create(host=host, status="succeeded", trigger="scheduled", finished_at=timezone.now())
        snapshot = m.HostCapabilitySnapshot.objects.create(host=host, probe_attempt=probe, revision=1,
            probe_status="succeeded", details_sha256="a" * 64)
        self.pause()
        full = self.client.get(URL, {"include_maintenance": True}).data
        body = {"pause_token": self.token, "include_maintenance": True, "plan_sha256": full["plan_sha256"], "confirmation": full["confirmation"]}
        self.assertEqual(self.client.post(URL, body, format="json").status_code, 409)
        self.client.delete(PAUSE_URL, {"token": self.token}, format="json")
        self.client.post(PAUSE_URL, {"token": self.token, "include_maintenance": True}, format="json")
        business = self.client.get(URL).data
        self.assertNotEqual(full["plan_sha256"], business["plan_sha256"])
        wrong = {**body, "plan_sha256": business["plan_sha256"]}
        self.assertEqual(self.client.post(URL, wrong, format="json").status_code, 409)
        self.assertTrue(m.HostCapabilitySnapshot.objects.filter(pk=snapshot.pk).exists())
        self.assertEqual(self.client.post(URL, body, format="json").status_code, 200)
        self.assertFalse(m.HostCapabilitySnapshot.objects.exists())
        self.assertFalse(m.RuntimeHostProbeAttempt.objects.exists())
        self.assertTrue(m.RuntimeHost.objects.filter(pk=host.pk).exists())
        connection.check_constraints()


class CleanBootstrapTests(TestCase):
    def test_default_bootstrap_has_no_business_or_smoke_history_and_no_asset_import(self):
        call_command("seed_data", stdout=StringIO(), verbosity=0)
        for name in HISTORY_MODELS - {"AccessControlAuditEvent", "SecretAuditEvent", "PlatformLifecycleAuditEvent"}:
            self.assertFalse(getattr(m, name)._base_manager.exists(), name)
        self.assertFalse(m.Asset.objects.exists())
        self.assertTrue(m.Style.objects.exists())
        self.assertTrue(m.ConfigDefinition.objects.exists())
        call_command("assert_clean_install", stdout=StringIO())


from studio.tests import test_execution_workflow_request_media as fixtures


@override_settings(AI_PROVIDER_ALLOWED_HOSTS=("127.0.0.1",))
class RetainedGeneratedLibraryTests(TestCase):
    _create_version = fixtures.ExecutionWorkflowMediaRequestTests._create_version
    setUp = fixtures.ExecutionWorkflowMediaRequestTests.setUp

    def test_optional_maintenance_preserves_global_recommendations_and_file_proofs_until_selected(self):
        self.user.is_staff = self.user.is_superuser = True; self.user.save()
        client = APIClient(); client.force_authenticate(self.user)
        host = m.RuntimeHost.objects.create(key="maintenance-split", name="Isolated host", status="active")
        snapshot = m.HostCapabilitySnapshot.objects.create(host=host, revision=1, probe_status="succeeded", details_sha256="a" * 64)
        artifact = m.ModelArtifact.objects.create(profile_version=self.profile, role="model", logical_name="isolated",
            filename="not-a-real-file.safetensors", status="available", verified_snapshot=snapshot,
            verified_at=timezone.now(), verified_by=self.user)
        observation = m.RuntimeModelAvailability.objects.create(snapshot=snapshot, deployment=self.deployment,
            artifact=artifact, availability_key="isolated", status="available", observed_size_bytes=123,
            observed_sha256="b" * 64, observation_source="local_filesystem")
        project = m.SeriesProject.objects.create(owner=self.user, title="Business recommendation", language="Chinese",
            aspect_ratio="16:9", width=1280, height=720, fps=24, default_shot_duration_ms=5000, continuity_seed=1)
        def recommendation(related_project, key):
            row = m.RecommendationSnapshot.objects.create(host_snapshot=snapshot, project=related_project,
                input_sha256=key * 64, status="ready", recommendation_sha256="c" * 64)
            candidate = m.RecommendationCandidate.objects.create(recommendation_snapshot=row, deployment=self.deployment,
                capability_key="video.reference_to_video", status="recommended", reason_code="isolated", suggested_values_sha256="d" * 64)
            decision = m.RecommendationDecision.objects.create(recommendation_snapshot=row, candidate=candidate,
                project=related_project, scope_type="project" if related_project else "system", decision="reject",
                decided_by=self.user, override_values_sha256="e" * 64)
            return row, candidate, decision
        global_rows = recommendation(None, "1")
        project_rows = recommendation(project, "2")
        # Project-specific use of a global recommendation is business history.
        project_decision = m.RecommendationDecision.objects.create(recommendation_snapshot=global_rows[0],
            candidate=global_rows[1], project=project, scope_type="project", decision="reject", decided_by=self.user,
            override_values_sha256="f" * 64)
        preserved_models = (m.HostCapabilitySnapshot, m.RuntimeModelAvailability, m.ModelArtifact)
        preserved_before = {model: list(model.objects.order_by("pk").values()) for model in preserved_models}
        global_before = [list(type(row).objects.filter(pk=row.pk).values()) for row in global_rows]
        plan = client.get(URL).data
        self.assertTrue(plan["can_execute"], plan["blockers"])
        self.assertEqual(plan["delete_counts"]["studio.RecommendationSnapshot"], 1)
        self.assertEqual(plan["delete_counts"]["studio.RecommendationDecision"], 2)
        token = str(uuid4()); client.post(PAUSE_URL, {"token": token}, format="json")
        response = client.post(URL, {"pause_token": token, "plan_sha256": plan["plan_sha256"], "confirmation": plan["confirmation"]}, format="json")
        self.assertEqual(response.status_code, 200, response.data)
        for row in (*project_rows, project_decision):
            self.assertFalse(type(row).objects.filter(pk=row.pk).exists())
        for model, before in preserved_before.items():
            self.assertEqual(list(model.objects.order_by("pk").values()), before)
        self.assertEqual([list(type(row).objects.filter(pk=row.pk).values()) for row in global_rows], global_before)
        # Full scope explicitly removes the retained maintenance facts and clears
        # their verification pointers, not the model/deployment definitions.
        plan = client.get(URL, {"include_maintenance": True}).data
        self.assertTrue(plan["can_execute"], plan["blockers"])
        token = str(uuid4()); client.post(PAUSE_URL, {"token": token, "include_maintenance": True}, format="json")
        response = client.post(URL, {"pause_token": token, "include_maintenance": True,
            "plan_sha256": plan["plan_sha256"], "confirmation": plan["confirmation"]}, format="json")
        self.assertEqual(response.status_code, 200, response.data)
        for row in (*global_rows, snapshot, observation):
            self.assertFalse(type(row).objects.filter(pk=row.pk).exists())
        artifact.refresh_from_db()
        self.assertEqual(artifact.status, "declared"); self.assertIsNone(artifact.verified_snapshot_id)
        self.assertIsNone(artifact.verified_at); self.assertIsNone(artifact.verified_by_id)
        self.assertEqual(artifact.filename, "not-a-real-file.safetensors")
        self.assertTrue(m.ModelDeployment.objects.filter(pk=self.deployment.pk).exists())
        connection.check_constraints()

    def test_workflow_runtime_proofs_clear_without_rewriting_import_definition(self):
        self.user.is_staff = self.user.is_superuser = True; self.user.save()
        from studio.services.json_contracts import assign_json_contract
        project = m.SeriesProject.objects.create(owner=self.user, title="Proof source", language="Chinese",
            aspect_ratio="16:9", width=1280, height=720, fps=24, default_shot_duration_ms=5000, continuity_seed=1)
        history_id = str(project.pk)
        m.WorkflowTemplateVersion._base_manager.filter(pk=self.workflow.pk).update(
            validation_state="passed", validation_evidence={"snapshot_id": history_id},
            smoke_test_state="passed", smoke_test_evidence={"smoke_attempt_id": history_id})
        source = m.ComfyWorkflowSource.objects.create(source_deployment=self.deployment,
            import_kind="candidate", source_path="candidate.json", source_path_key="candidate.json",
            source_size_bytes=2, source_sha256="a" * 64, source_path_sha256="b" * 64,
            source_scope_sha256="c" * 64, import_contract_sha256="d" * 64, imported_by=self.user)
        projection = {"model_dependencies": [{"artifact_matches": [{"verified_snapshot": history_id}]}],
            "input_contract": {"parameters": {}}}
        draft = m.ComfyWorkflowContractDraft(candidate_source=source, revision=1,
            primary_output_node_id="1", admission_tier="candidate", actionability="candidate_only", created_by=self.user)
        assign_json_contract(draft, "projection", projection); draft.save()
        receipt = m.ComfyWorkflowPreviewReceipt(owner=self.user, deployment=self.deployment,
            contract_draft=draft, request_key="cleanup-proof", request_sha256="e" * 64)
        assign_json_contract(receipt, "response", projection); receipt.save()
        receipt_before = list(m.ComfyWorkflowPreviewReceipt.objects.filter(pk=receipt.pk).values())
        before = list(m.ComfyWorkflowContractDraft._base_manager.filter(pk=draft.pk).values())
        graph_before = (self.workflow.graph, self.workflow.graph_sha256, self.workflow.input_contract)
        client = APIClient(); client.force_authenticate(self.user)
        plan = client.get(URL).data
        self.assertTrue(plan["can_execute"], plan["blockers"])
        # Live input references elsewhere in the same immutable projection are
        # still protected; only inventory proof annotations are non-inputs.
        bad = {**projection, "input_contract": {"source_id": history_id}}
        m.ComfyWorkflowContractDraft._base_manager.filter(pk=draft.pk).update(projection=bad)
        self.assertFalse(client.get(URL).data["can_execute"])
        m.ComfyWorkflowContractDraft._base_manager.filter(pk=draft.pk).update(projection=projection)
        m.ComfyWorkflowPreviewReceipt.objects.filter(pk=receipt.pk).update(response=bad)
        self.assertFalse(client.get(URL).data["can_execute"])
        m.ComfyWorkflowPreviewReceipt.objects.filter(pk=receipt.pk).update(response=projection)
        token = str(uuid4())
        self.assertEqual(client.post(PAUSE_URL, {"token": token}, format="json").status_code, 200)
        response = client.post(URL, {"plan_sha256": plan["plan_sha256"], "confirmation": plan["confirmation"], "pause_token": token}, format="json")
        self.assertEqual(response.status_code, 200, response.data)
        self.workflow.refresh_from_db()
        self.assertEqual(self.workflow.status, "published")
        self.assertEqual((self.workflow.graph, self.workflow.graph_sha256, self.workflow.input_contract), graph_before)
        self.assertEqual(self.workflow.validation_state, "not_run")
        self.assertEqual(self.workflow.smoke_test_state, "not_run")
        self.assertEqual(self.workflow.smoke_test_evidence, {})
        self.assertEqual(self.workflow.validation_evidence, {})
        self.assertIsNone(self.workflow.validated_deployment_id)
        self.assertEqual(list(m.ComfyWorkflowContractDraft._base_manager.filter(pk=draft.pk).values()), before)
        self.assertEqual(list(m.ComfyWorkflowPreviewReceipt.objects.filter(pk=receipt.pk).values()), receipt_before)

    def test_generated_origin_and_primary_variant_relationship_survive_history_cleanup(self):
        self.user.is_staff = self.user.is_superuser = True; self.user.save()
        job = m.AssetGenerationJob.objects.create(owner=self.user, model_deployment=self.deployment,
            execution_instance_version_id=self.definition.value["version_id"], workflow_template_version=self.workflow,
            execution_kind="comfy_workflow", job_type="asset", media_type="image", status="succeeded",
            operation="image.generate", idempotency_key=str(uuid4()), request_snapshot_sha256="a" * 64,
            output_asset=self.asset)
        primary = m.AssetReferenceSeries.objects.create(owner=self.user, name="主图", role="primary")
        variant = m.AssetReferenceSeries.objects.create(owner=self.user, name="变体", role="variant", primary_series=primary)
        basis = m.AssetReferenceVersion.objects.create(series=primary, version=1, asset=self.asset, source="generated", generation_job=job)
        version = m.AssetReferenceVersion.objects.create(series=variant, version=1, asset=self.asset, source="generated", generation_job=job, generated_from=basis)
        original = Path(self.asset.file.path).read_bytes()
        client = APIClient(); client.force_authenticate(self.user)
        plan = client.get(URL).data
        self.assertTrue(plan["can_execute"], plan["blockers"])
        token = str(uuid4())
        self.assertEqual(client.post(PAUSE_URL, {"token": token}, format="json").status_code, 200)
        response = client.post(URL, {"plan_sha256": plan["plan_sha256"], "confirmation": plan["confirmation"], "pause_token": token}, format="json")
        self.assertEqual(response.status_code, 200, response.data)
        version.refresh_from_db(); basis.refresh_from_db()
        for row in (basis, version):
            self.assertEqual(row.source, "generated")
            self.assertTrue(row.origin_history_cleared)
            self.assertIsNone(row.generation_job_id)
            row.full_clean()
        self.assertEqual(version.generated_from_id, basis.pk)
        self.assertEqual(Path(self.asset.file.path).read_bytes(), original)
        detail = client.get(f"/api/v1/asset-reference-versions/{version.pk}/")
        self.assertEqual(detail.status_code, 200, detail.data)
        self.assertTrue(detail.data["origin_history_cleared"])
