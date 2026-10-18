"""Recipe retry API/queue receipts with isolated media, no encoder or model."""
from unittest.mock import patch
from uuid import uuid4

from django.contrib.auth import get_user_model
from django.core.exceptions import ValidationError
from django.test import TestCase, TransactionTestCase

from studio.models import AssemblyJob, Episode, GenerationAttempt, GenerationTask, ProductionAssemblyLink, Shot
from studio.services.assembly import AssemblyError, create_assembly
from studio.services.production_execution_retry import stage_retry_context
from studio.services.production_runs import _execute_assembly, retry_production_run
from studio.tests.test_recipe_assembly_configuration import RecipeAssemblyConfigurationTests as Fixture


class RecipeAssemblyRetryTests(TestCase):
    setUp = Fixture.setUp
    _selected_artifact = Fixture._selected_artifact
    _read_fixture_candidate = Fixture._read_fixture_candidate
    recipe = Fixture.recipe
    freeze = Fixture.freeze

    def failed(self, *, two=False):
        if two:
            episode = Episode.objects.create(project=self.project, number=2, title="Isolated second episode")
            shot = Shot.objects.create(episode=episode, number=1, title="Third", duration_ms=3000, media_type="video", include_audio=True)
            self._selected_artifact(shot, "third.mp4")
        self.freeze()
        _execute_assembly(self.run, self.assembly_stage)
        links = list(ProductionAssemblyLink.objects.filter(production_run=self.run).order_by("episode__number"))
        AssemblyJob.objects.filter(pk__in=[row.assembly_job_id for row in links]).update(status="failed")
        self.run.status = self.assembly_stage.status = "blocked"
        self.run.save(update_fields=("status",)); self.assembly_stage.save(update_fields=("status",))
        return links

    def preview(self, **params):
        return self.client.get(f"/api/v1/production-runs/{self.run.pk}/retry-preview/", {
            "expected_stage_id": str(self.assembly_stage.pk), **params})

    def post(self, payload):
        return self.client.post(f"/api/v1/production-runs/{self.run.pk}/retry/", payload, format="json")

    def test_preview_is_read_only_paginated_exact_and_has_no_lock_or_large_job_body(self):
        from django.db import connection
        from django.test.utils import CaptureQueriesContext
        from django.db.models.query import QuerySet
        links = self.failed(two=True)
        AssemblyJob.objects.filter(pk=links[1].assembly_job_id).update(status="succeeded")
        with CaptureQueriesContext(connection) as queries, patch.object(QuerySet, "select_for_update", side_effect=AssertionError("Read only")):
            result = self.preview(page_size=1)
        self.assertEqual(result.status_code, 200, result.data)
        self.assertEqual(result.data["mode"], "assembly_jobs")
        self.assertEqual(result.data["assembly_count"], 2)
        self.assertTrue(result.data["can_submit"], result.data)
        self.assertEqual(result.data["retry_context"], stage_retry_context(self.assembly_stage))
        self.assertEqual(result.data["results"][0]["id"], str(links[0].assembly_job_id))
        self.assertEqual(result.data["results"][0]["consumer_input_sha256"], self.assembly_stage.input_snapshot_sha256)
        self.assertEqual(self.preview(page_size=1, page=2).data["results"][0]["disposition"], "skip")
        self.assertEqual(self.preview(page_size=1, page=3).status_code, 404)
        job_sql = "\n".join(q["sql"] for q in queries if '"studio_assemblyjob"' in q["sql"])
        self.assertNotIn('"ffmpeg_command"', job_sql)
        self.assertFalse(any(q["sql"].lstrip().upper().startswith(("INSERT", "UPDATE", "DELETE")) for q in queries))
        self.assertEqual(AssemblyJob.objects.count(), 2)

    def test_post_requires_exact_set_digest_and_stage_and_keeps_selection(self):
        links = self.failed()
        context = stage_retry_context(self.assembly_stage)
        before = list(self.stage.artifact_selection_decisions.values())
        bad = [{"expected_assembly_job_ids": []}, {"expected_assembly_job_ids": [str(uuid4())]},
            {"expected_input_sha256": "0" * 64}, {"expected_stage_id": str(uuid4())},
            {"expected_assembly_job_ids": context["expected_assembly_job_ids"] * 2}, {"expected_attempt_ids": [str(uuid4())]}]
        for change in bad:
            with self.subTest(change=change):
                result = self.post({**context, **change})
                self.assertEqual(result.status_code, 400, result.data)
        self.assertEqual(self.post({"expected_stage_id": str(self.assembly_stage.pk), "expected_attempt_ids": []}).status_code, 400)
        self.assertEqual(AssemblyJob.objects.count(), 1)
        self.assertEqual(list(self.stage.artifact_selection_decisions.values()), before)
        links[0].refresh_from_db()
        self.assertEqual(str(links[0].assembly_job_id), context["expected_assembly_job_ids"][0])

    def test_successful_retry_and_old_replay_after_successor_fails_never_retries_new_target(self):
        links = self.failed()
        context = stage_retry_context(self.assembly_stage)
        before = list(self.stage.artifact_selection_decisions.values())
        with patch("studio.services.production_runs.advance_production_run", side_effect=lambda run: run):
            result = self.post(context)
        self.assertEqual(result.status_code, 200, result.data)
        links[0].refresh_from_db()
        self.assertNotEqual(str(links[0].assembly_job_id), context["expected_assembly_job_ids"][0])
        AssemblyJob.objects.filter(pk=links[0].assembly_job_id).update(status="failed")
        self.run.status = self.assembly_stage.status = "blocked"
        self.run.save(update_fields=("status",)); self.assembly_stage.save(update_fields=("status",))
        self.assertEqual(self.post(context).status_code, 400)
        self.assertEqual(AssemblyJob.objects.count(), 2)
        self.assertEqual(list(self.stage.artifact_selection_decisions.values()), before)
        self.assertFalse(GenerationAttempt.objects.exists()); self.assertFalse(GenerationTask.objects.exists())

    def test_running_unknown_and_unrecorded_or_changed_consumption_do_not_enable_retry(self):
        links = self.failed()
        context = stage_retry_context(self.assembly_stage)
        for status in ("running", "queued", "planned", "succeeded"):
            AssemblyJob.objects.filter(pk=links[0].assembly_job_id).update(status=status)
            response = self.preview()
            self.assertFalse(response.data["can_submit"], response.data)
            self.assertEqual(response.data["results"][0]["disposition"], "skip" if status == "succeeded" else "attention")
            self.assertEqual(self.post(context).status_code, 400)
        for fields in ({"consumer_stage": None, "consumer_input_sha256": ""},
                {"consumer_stage": self.assembly_stage, "consumer_input_sha256": "0" * 64}):
            AssemblyJob.objects.filter(pk=links[0].assembly_job_id).update(status="failed", **fields)
            response = self.preview()
            self.assertFalse(response.data["can_submit"])
            self.assertEqual(response.data["results"][0]["disposition"], "attention")
            self.assertEqual(self.post(context).status_code, 400)

    def test_second_episode_failure_rolls_back_first_job_links_sources_and_new_files(self):
        links = self.failed(two=True)
        context = stage_retry_context(self.assembly_stage)
        before = set(self.media_root.rglob("*"))
        calls = []
        def create(**kwargs):
            calls.append(kwargs["episode"].pk)
            if len(calls) == 2:
                raise AssemblyError("Isolated second episode failure")
            return create_assembly(**kwargs)
        with patch("studio.services.production_runs.create_assembly", side_effect=create):
            response = self.post(context)
        self.assertEqual(response.status_code, 400, response.data)
        self.assertEqual(len(calls), 2)
        self.assertEqual(AssemblyJob.objects.count(), 2)
        self.assertEqual(set(self.media_root.rglob("*")), before)
        for link in links:
            expected = link.assembly_job_id; link.refresh_from_db()
            self.assertEqual(link.assembly_job_id, expected)
        self.run.refresh_from_db(); self.assertEqual(self.run.status, "blocked")

    def test_source_drift_blocks_preview_and_post_and_other_user_is_denied(self):
        self.failed()
        context = stage_retry_context(self.assembly_stage)
        self.shot1.duration_ms += 1000; self.shot1.save(update_fields=("duration_ms",))
        self.assertFalse(self.preview().data["can_submit"])
        self.assertEqual(self.post(context).status_code, 400)
        other = get_user_model().objects.create_user(username="other-retry-assembly")
        self.client.force_authenticate(other)
        self.assertEqual(self.preview().status_code, 404)
        self.assertEqual(self.post(context).status_code, 404)
        self.assertEqual(AssemblyJob.objects.count(), 1)

    def test_later_stage_failure_rolls_back_new_manifest_and_link(self):
        links = self.failed()
        context = stage_retry_context(self.assembly_stage)
        before = set(self.media_root.rglob("*"))
        with patch("studio.services.production_runs.advance_production_run", side_effect=ValidationError("Isolated advance failure")):
            self.assertEqual(self.post(context).status_code, 400)
        links[0].refresh_from_db()
        self.assertEqual(str(links[0].assembly_job_id), context["expected_assembly_job_ids"][0])
        self.assertEqual(AssemblyJob.objects.count(), 1)
        self.assertEqual(set(self.media_root.rglob("*")), before)

    def test_two_episode_job_swap_and_missing_link_reject_before_enqueue(self):
        links = self.failed(two=True)
        context = stage_retry_context(self.assembly_stage)
        original = [row.assembly_job_id for row in links]
        # Explicit isolated corruption, not modification of production history.
        for link, job in zip(links, reversed(original)):
            ProductionAssemblyLink.objects.filter(pk=link.pk).update(assembly_job_id=job)
        with patch("studio.services.production_runs.create_assembly", side_effect=AssertionError("No enqueue")):
            self.assertFalse(self.preview().data["can_submit"])
            self.assertEqual(self.post(context).status_code, 400)
        for link, job in zip(links, original):
            ProductionAssemblyLink.objects.filter(pk=link.pk).update(assembly_job_id=job)
        links[1].delete()
        before = set(self.media_root.rglob("*"))
        with patch("studio.services.production_runs.create_assembly", side_effect=AssertionError("No unconfirmed episode")):
            self.assertFalse(self.preview().data["can_submit"])
            self.assertEqual(self.post(stage_retry_context(self.assembly_stage)).status_code, 400)
        self.assertEqual(AssemblyJob.objects.count(), 2)
        self.assertEqual(set(self.media_root.rglob("*")), before)

    def test_mixed_running_job_is_not_retried_and_project_lock_precedes_jobs(self):
        from django.db.models.query import QuerySet
        links = self.failed(two=True)
        AssemblyJob.objects.filter(pk=links[1].assembly_job_id).update(status="running")
        context = stage_retry_context(self.assembly_stage)
        locks = []; original = QuerySet.select_for_update
        def lock(query, *args, **kwargs):
            locks.append(query.model.__name__)
            return original(query, *args, **kwargs)
        with patch.object(QuerySet, "select_for_update", lock), patch("studio.services.production_runs.advance_production_run", side_effect=lambda run: run):
            self.assertEqual(self.post(context).status_code, 200)
        self.assertLess(locks.index("SeriesProject"), locks.index("ProductionAssemblyLink"))
        links[1].refresh_from_db(); self.assertEqual(links[1].assembly_job.status, "running")
        self.assertEqual(AssemblyJob.objects.count(), 3)


class AssemblyRetryCommitFailureTests(TransactionTestCase):
    setUp = Fixture.setUp
    _selected_artifact = Fixture._selected_artifact
    _read_fixture_candidate = Fixture._read_fixture_candidate
    recipe = Fixture.recipe
    freeze = Fixture.freeze
    failed = RecipeAssemblyRetryTests.failed

    def test_outer_commit_failure_rolls_back_all_new_jobs_and_manifests(self):
        from django.db import connection, DatabaseError
        links = self.failed(two=True)
        context = stage_retry_context(self.assembly_stage)
        before = set(self.media_root.rglob("*"))
        with patch("studio.services.production_runs.advance_production_run", side_effect=lambda run: run), patch.object(
                connection, "commit", side_effect=DatabaseError("Isolated commit failure")):
            with self.assertRaisesMessage(DatabaseError, "Isolated commit failure"):
                retry_production_run(self.run, user=self.user, **context)
        self.assertEqual(AssemblyJob.objects.count(), 2)
        self.assertEqual(set(self.media_root.rglob("*")), before)
        for link in links:
            original = link.assembly_job_id; link.refresh_from_db()
            self.assertEqual(link.assembly_job_id, original)
