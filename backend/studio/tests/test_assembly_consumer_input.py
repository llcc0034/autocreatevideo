"""M8-04 creation-time consumer facts; isolated formal manual/API path."""
from copy import deepcopy

from django.db import IntegrityError, transaction
from django.test import TestCase

from studio.models import AssemblyJob, GenerationAttempt, GenerationTask, ProductionAssemblyLink
from studio.services.json_contracts import assign_json_contract
from studio.tests import test_production_manual_assembly as fixtures


class AssemblyConsumerInputTests(TestCase):
    setUp = fixtures.ManualAssemblyTests.setUp
    mark_submitted = fixtures.ManualAssemblyTests.mark_submitted
    archive = fixtures.ManualAssemblyTests.archive
    resolve = fixtures.ManualAssemblyTests.resolve
    approve = fixtures.ManualAssemblyTests.approve
    detail = fixtures.ManualAssemblyTests.detail
    request = fixtures.ManualAssemblyTests.request
    post = fixtures.ManualAssemblyTests.post

    def test_failed_retry_preserves_both_consumers_and_filters_old_job_without_current_link(self):
        self.approve()
        counts = (GenerationAttempt.objects.count(), GenerationTask.objects.count())
        body = self.request()
        self.assertEqual(self.post(body).status_code, 202)
        old = AssemblyJob.objects.get()
        stage = self.run.stages.get(stage_key="assembly")
        self.assertEqual(old.consumer_stage_id, stage.pk)
        self.assertNotEqual(old.consumer_stage_id, self.artifact.stage_run_id)
        self.assertEqual(old.consumer_input_sha256, stage.input_snapshot_sha256)
        original_sources = list(old.source_items.values())
        old.status = "failed"
        old.error_message = "isolated encoder failure"
        old.save(update_fields=("status", "error_message"))
        retry = self.request()
        self.assertEqual(self.post(retry).status_code, 202)
        new = ProductionAssemblyLink.objects.get().assembly_job
        self.assertNotEqual(new.pk, old.pk)
        self.assertEqual(new.consumer_stage_id, old.consumer_stage_id)
        self.assertEqual(new.consumer_input_sha256, old.consumer_input_sha256)
        self.assertEqual(self.post(retry).status_code, 200)
        self.assertEqual(AssemblyJob.objects.count(), 2)
        self.assertEqual(list(old.source_items.values()), original_sources)
        self.assertEqual((GenerationAttempt.objects.count(), GenerationTask.objects.count()), counts)
        for filters in ({"production_run_id": str(self.run.pk)}, {"stage_run_id": str(stage.pk)}):
            page = self.client.get("/api/v1/task-center/", {**filters, "operation": "assembly"})
            self.assertEqual(page.status_code, 200, page.data)
            self.assertEqual({row["id"] for row in page.data["results"]}, {str(old.pk), str(new.pk)})
            self.assertTrue(all(row["production_run_id"] == str(self.run.pk) for row in page.data["results"]))
        detail = self.client.get(f"/api/v1/task-center/assembly/{old.pk}/").data
        self.assertEqual(detail["status"], "failed")
        self.assertEqual(detail["input"]["submitted"]["consumer_input_sha256"], old.consumer_input_sha256)
        self.assertEqual(detail["input"]["submitted"]["consumer_input_status"], "current")
        print("M8 consumer-stage query plan:", AssemblyJob.objects.filter(consumer_stage=stage).explain())

    def test_unknown_historical_consumer_is_not_guessed_from_current_link_or_project(self):
        self.approve()
        self.assertEqual(self.post(self.request()).status_code, 202)
        job = AssemblyJob.objects.get()
        AssemblyJob.objects.filter(pk=job.pk).update(consumer_stage=None, consumer_input_sha256="")
        detail = self.client.get(f"/api/v1/task-center/assembly/{job.pk}/").data
        self.assertIsNone(detail["production_run_id"])
        self.assertIsNone(detail["input"]["submitted"]["consumer_stage_id"])
        self.assertEqual(detail["input"]["submitted"]["consumer_input_status"], "unrecorded")
        page = self.client.get("/api/v1/task-center/", {"production_run_id": str(self.run.pk), "operation": "assembly"})
        self.assertEqual(page.data["count"], 0)
        other = type(self.user).objects.create_user(username="not-assembly-owner")
        self.client.force_authenticate(other)
        self.assertEqual(self.client.get(f"/api/v1/task-center/assembly/{job.pk}/").status_code, 404)

    def test_changed_stage_input_never_relabels_old_job_digest_and_pair_is_constrained(self):
        self.approve()
        self.assertEqual(self.post(self.request()).status_code, 202)
        job = AssemblyJob.objects.get()
        stage = job.consumer_stage
        changed = deepcopy(stage.input_snapshot)
        changed["isolated_change"] = True
        assign_json_contract(stage, "input_snapshot", changed)
        stage.save(update_fields=("input_snapshot", "input_snapshot_schema_version", "input_snapshot_size_bytes", "input_snapshot_sha256"))
        detail = self.client.get(f"/api/v1/task-center/assembly/{job.pk}/").data
        self.assertEqual(detail["input"]["submitted"]["consumer_input_status"], "changed")
        self.assertEqual(detail["input"]["submitted"]["consumer_input_sha256"], job.consumer_input_sha256)
        with self.assertRaises(IntegrityError), transaction.atomic():
            AssemblyJob.objects.filter(pk=job.pk).update(consumer_input_sha256="")
