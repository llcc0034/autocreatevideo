"""Real isolated archive/seal chain, with an explicit non-playable encoder fixture."""
from pathlib import Path
import hashlib
import subprocess
from unittest.mock import patch
from uuid import uuid4

from django.conf import settings
from django.contrib.auth import get_user_model
from django.db import connection
from django.test import TestCase, override_settings
from django.test.utils import CaptureQueriesContext

from studio.models import ApprovalDecision, AssemblyJob, GeneratedArtifact, GenerationAttempt, ProductionManualCommand, ReleaseBundle
from studio.services.assembly_queue import claim_next_assembly, process_assembly_job
from studio.services.distribution import prepare_task_distribution, distribution_artifact_path
from studio.services.release_bundles import live_verified_release_bundle
from studio.services.generated_artifacts import verified_local_media_facts
from studio.tests import test_production_manual_assembly as assembly_fixtures
from studio.tests import test_execution_task_distribution as distribution_fixtures


@override_settings(AI_PROVIDER_ALLOWED_HOSTS=("127.0.0.1",))
class ManualDeliveryTests(TestCase):
    setUp = assembly_fixtures.ManualAssemblyTests.setUp
    mark_submitted = assembly_fixtures.ManualAssemblyTests.mark_submitted
    archive = assembly_fixtures.ManualAssemblyTests.archive
    resolve = assembly_fixtures.ManualAssemblyTests.resolve
    approve = assembly_fixtures.ManualAssemblyTests.approve
    detail = assembly_fixtures.ManualAssemblyTests.detail
    request = assembly_fixtures.ManualAssemblyTests.request
    post = assembly_fixtures.ManualAssemblyTests.post
    require_font = distribution_fixtures.PreparedTaskDistributionTests.require_font

    def delivery(self):
        result = self.client.get(f"/api/v1/production-runs/{self.run.pk}/manual-delivery/")
        self.assertEqual(result.status_code, 200, result.data)
        self.assertEqual(result["Cache-Control"], "private, no-store")
        return result.data

    def delivery_request(self, detail=None):
        detail = detail or self.delivery()
        return {"action": "deliver", "stage_key": "delivery", "scope": {"kind": "project", "id": str(self.project.pk)},
            "unit_key": f"project:{self.project.pk}", "expected_run_plan_sha256": self.plan.sha256,
            "expected_selection_revision": detail["contract"]["selection_revision"], "idempotency_key": str(uuid4()),
            "payload": {"expected_contract_sha256": detail["contract_sha256"], "comment": "Explicit fixture master review"}}

    def complete_assembly(self, *, distribute=True):
        self.approve()
        self.assertEqual(self.post(self.request()).status_code, 202)
        def encoder(command, **kwargs):
            Path(command[-1]).write_bytes(b"isolated encoder fixture; not real playable media acceptance")
            return subprocess.CompletedProcess(command, 0, stdout="", stderr="")
        with self.captureOnCommitCallbacks(execute=True), patch("studio.services.assembly_queue._run_ffmpeg", side_effect=encoder):
            job = process_assembly_job(claim_next_assembly(worker_id="manual-delivery-test"))
        self.assertEqual(job.status, "succeeded")
        # The non-playable encoder fixture needs an explicit matching probe.
        # File/hash/signature/source checks remain real, including tampering.
        def media_facts(path, **kwargs):
            if Path(path) == Path(job.output_path):
                data = Path(path).read_bytes()
                return {"sha256": hashlib.sha256(data).hexdigest(), "size_bytes": len(data), "mime_type": "video/mp4"}
            return verified_local_media_facts(path, **kwargs)
        self.enterContext(patch("studio.services.release_bundles.verified_local_media_facts", side_effect=media_facts))
        if distribute:
            self.require_font()
            self.task.refresh_from_db()
            prepare_task_distribution(self.task, 0)
            self.task.refresh_from_db()

    def test_incomplete_inventory_permissions_and_registered_action_never_create_delivery(self):
        detail = self.delivery()
        self.assertTrue(detail["blockers"])
        self.assertIsNone(detail["contract"]["episodes"][0]["artifact_id"])
        self.assertEqual(self.post(self.delivery_request(detail)).status_code, 400)
        url = f"/api/v1/production-runs/{self.run.pk}/manual-delivery/"
        self.assertEqual(self.client.get(url, {"page": 1}).status_code, 400)
        # This partial fixture omits unrelated content stages; registration is
        # checked directly rather than fabricating their approval state.
        from studio.services.production_manual_actions import _ADAPTERS
        self.assertIn(("delivery", "deliver"), _ADAPTERS)
        self.client.force_authenticate(get_user_model().objects.create_user(username="delivery-other"))
        self.assertEqual(self.client.get(url).status_code, 404)
        self.assertEqual(self.post(self.delivery_request(detail)).status_code, 404)
        self.assertFalse(ReleaseBundle.objects.exists())

    def test_explicit_seal_replay_and_file_reverification_without_encoding(self):
        self.complete_assembly()
        with CaptureQueriesContext(connection) as queries:
            detail = self.delivery()
        self.assertEqual(detail["blockers"], [])
        master_reads = [row["sql"] for row in queries if 'FROM "studio_generatedartifact"' in row["sql"]
            and '"manifest_key" IN' in row["sql"]]
        self.assertEqual(len(master_reads), 1)
        self.assertNotIn('"manifest_metadata"', master_reads[0])
        query_plan = GeneratedArtifact.objects.filter(production_run=self.run, kind="assembly", role="episode_master",
            manifest_key=f"assembly:{detail['contract']['episodes'][0]['assembly_job_id']}").explain()
        if connection.vendor == "sqlite":
            self.assertIn("INDEX", query_plan.upper())
        self.run.refresh_from_db(); self.assertEqual(self.run.status, "awaiting_input")
        body = self.delivery_request(detail)
        with patch("studio.services.distribution.prepare_task_distribution", side_effect=AssertionError("No request encoding")), \
                patch("studio.services.assembly_queue._run_ffmpeg", side_effect=AssertionError("No request assembly")):
            first = self.post(body)
            self.assertEqual(first.status_code, 201, first.data)
            self.assertEqual(first.data["result_kind"], "release_bundle")
            replay = self.post(body)
            self.assertEqual(replay.status_code, 200, replay.data)
            self.assertEqual(replay.data, first.data)
        bundle = ReleaseBundle.objects.get(pk=first.data["target_id"])
        self.assertEqual(bundle.production_run_id, self.run.pk)
        self.assertEqual(live_verified_release_bundle(bundle)["verification"]["status"], "verified")
        self.run.refresh_from_db(); self.assertEqual(self.run.status, "succeeded")
        self.assertIsNotNone(self.run.finished_at)
        self.assertEqual(self.run.stages.get(stage_key="delivery").status, "succeeded")
        self.assertEqual(ApprovalDecision.objects.filter(stage_run__stage_key="delivery").count(), 1)
        self.assertEqual(AssemblyJob.objects.count(), 1)
        self.assertEqual(self.post({**body, "idempotency_key": str(uuid4())}).status_code, 400)
        self.assertEqual(self.delivery()["bundle"]["id"], str(bundle.pk))
        public = distribution_artifact_path(self.task, 0)
        public.write_bytes(public.read_bytes() + b"changed")
        self.assertEqual(self.post(body).status_code, 400)
        self.assertEqual(ReleaseBundle.objects.count(), 1)

    def test_missing_distribution_does_not_encode_or_seal_and_failed_receipt_rolls_back(self):
        self.complete_assembly(distribute=False)
        body = self.delivery_request()
        with patch("studio.services.distribution.prepare_task_distribution", side_effect=AssertionError("Must not repair")):
            response = self.post(body)
        self.assertEqual(response.status_code, 400, response.data)
        self.assertFalse(ReleaseBundle.objects.exists())
        self.assertFalse(ProductionManualCommand.objects.filter(action="deliver").exists())
        self.require_font(); self.task.refresh_from_db(); prepare_task_distribution(self.task, 0)
        with patch("studio.services.production_manual_commands.ProductionManualCommand.save", side_effect=RuntimeError("Receipt unavailable")):
            with self.assertRaises(RuntimeError):
                self.post(body)
        self.assertFalse(ReleaseBundle.objects.exists())
        self.assertFalse(ApprovalDecision.objects.filter(stage_run__stage_key="delivery").exists())
        self.run.refresh_from_db(); self.assertEqual(self.run.status, "awaiting_input")
        self.assertEqual(self.post(body).status_code, 201)

    def test_stale_scope_unresolved_and_corrupt_master_fail_but_old_budget_does_not_block_delivery(self):
        self.complete_assembly()
        body = self.delivery_request()
        self.assertEqual(self.post({**body, "expected_selection_revision": 999}).status_code, 409)
        self.assertEqual(self.post({**body, "payload": {**body["payload"], "expected_contract_sha256": "f" * 64}}).status_code, 409)
        GenerationAttempt.objects.filter(pk=self.attempt.pk).update(error_code="uncertain_remote_response")
        self.assertTrue(self.delivery()["blockers"])
        self.assertEqual(self.post(body).status_code, 400)
        GenerationAttempt.objects.filter(pk=self.attempt.pk).update(error_code="")
        self.run.actual_cost_total = 1
        self.run.save(update_fields=("actual_cost_total",))
        self.assertEqual(self.delivery()["blockers"], [])
        self.run.actual_cost_total = 0; self.run.save(update_fields=("actual_cost_total",))
        master = GeneratedArtifact.objects.get(production_run=self.run, kind="assembly")
        (Path(settings.MEDIA_ROOT) / master.object_key).write_bytes(b"corrupt")
        self.assertEqual(self.post(body).status_code, 400)
        self.assertFalse(ReleaseBundle.objects.exists())
