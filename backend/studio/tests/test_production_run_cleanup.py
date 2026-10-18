"""Scoped offline maintenance on an isolated DB; never invokes providers."""
from contextlib import closing
from datetime import timedelta
from io import StringIO
import hashlib
import json
from pathlib import Path
import sqlite3
from tempfile import TemporaryDirectory
from unittest.mock import patch
from uuid import uuid4

from django.contrib.auth import get_user_model
from django.core.management import call_command
from django.core.management.base import CommandError
from django.db import connection
from django.test import TestCase, TransactionTestCase
from django.test.utils import CaptureQueriesContext
from django.utils import timezone

from studio import models as m
from studio.management import production_run_cleanup as cleanup
from studio.services.json_contracts import assign_json_contract
from studio.services.script_sources import create_pasted_script_source, confirm_script_revision
from studio.tests.test_execution_context_inputs import PROJECT_CONFIG


class CleanupFixture:
    def setUp(self):
        self.actor = get_user_model().objects.create_superuser(username="cleanup-operator", password="test-only")
        self.project = m.SeriesProject.objects.create(owner=self.actor, title="Preserved project", **PROJECT_CONFIG)
        self.recipe = m.ProductionRecipeVersion(recipe_key="cleanup-fixture", name="Retired fixture", version=1, status="retired")
        assign_json_contract(self.recipe, "definition", {"schema_version": 3})
        self.recipe.save()
        self.run = m.ProductionRun.objects.create(owner=self.actor, project=self.project, plan_source="recipe",
            recipe_version=self.recipe, status="cancelled", idempotency_key=str(uuid4()), request_sha256="a" * 64)
        self.stage = m.StageRun.objects.create(production_run=self.run, stage_key="media_generation",
            mode="existing_pipeline", ordinal=1, status="cancelled", idempotency_key=str(uuid4()))
        self.asset = m.Asset.objects.create(owner=self.actor, kind="reference", name="Preserved asset",
            metadata={"production_run_id": str(self.run.pk)})

    def plan(self, **kwargs):
        return cleanup.build_plan(run_id=self.run.pk, recipe_id=self.recipe.pk, **kwargs)

    def execute(self, plan=None, **kwargs):
        plan = plan or self.plan()
        args = dict(run_id=self.run.pk, recipe_id=self.recipe.pk, expected_sha256=plan.report["plan_sha256"],
            confirmation=cleanup.CONFIRMATION, backup_manifest="fixture/manifest.json", operator=self.actor.username,
            writers_stopped=True, detach_script_provenance=plan.report["detach_script_provenance"])
        args.update(kwargs)
        return cleanup.execute(**args)

    def attempt(self):
        provider = m.ProviderProfile.objects.create(key=str(uuid4()), name="Shared", driver_type="comfyui")
        profile = m.ModelProfileVersion.objects.create(profile_key=str(uuid4()), version=1,
            provider=provider, model_identifier="fixture", modality="image")
        deployment = m.ModelDeployment.objects.create(key=str(uuid4()), name="Shared", profile_version=profile)
        instance = m.ModelExecutionInstance.objects.create(key=str(uuid4()), name="Shared")
        version = m.ModelExecutionInstanceVersion.objects.create(instance=instance, version=1,
            deployment=deployment, operation="image.generate", prompt_mode="manual")
        prep = m.ExecutionPreparation.objects.create(stage_run=self.stage, execution_instance_version=version,
            purpose="production", unit_key="fixture", request_key="fixture", intent_sha256="a" * 64,
            created_by=self.actor, status="cancelled", cancelled_at=timezone.now(), cancelled_by=self.actor)
        task = m.GenerationTask.objects.create(owner=self.actor, project=self.project, kind="image", status="cancelled")
        attempt = m.GenerationAttempt.objects.create(stage_run=self.stage, preparation=prep,
            execution_instance_version=version, role="primary", unit_key="fixture", attempt_number=1,
            generation_task=task, status="cancelled", request_sha256="a" * 64)
        return attempt, task, prep


class ProductionRunCleanupTests(CleanupFixture, TestCase):
    def test_preview_read_only_exact_scope_and_stable_hash(self):
        with CaptureQueriesContext(connection) as queries:
            first = self.plan()
        self.assertFalse(any(row["sql"].lstrip().upper().startswith(("UPDATE", "DELETE", "INSERT")) for row in queries))
        self.assertEqual(first.report["blockers"], [])
        self.assertEqual(first.report["counts"], {"studio.ProductionRun": 1, "studio.ProductionRecipeVersion": 1, "studio.StageRun": 1})
        self.assertEqual(first.report["plan_sha256"], self.plan().report["plan_sha256"])
        self.assertTrue(first.report["preserved_provenance"])
        self.assertFalse(first.report["physical_files_deleted"])

    def test_unknown_or_mismatched_targets_do_not_expand_scope(self):
        for run, recipe in (("", self.recipe.pk), (uuid4(), self.recipe.pk), (self.run.pk, uuid4())):
            with self.subTest(run=run), self.assertRaises(CommandError):
                cleanup.build_plan(run_id=run, recipe_id=recipe)
        self.assertTrue(m.ProductionRun.objects.filter(pk=self.run.pk).exists())

    def test_shared_recipe_and_external_json_only_block(self):
        other = m.ProductionRun.objects.create(owner=self.actor, project=self.project, plan_source="recipe",
            recipe_version=self.recipe, status="cancelled", idempotency_key=str(uuid4()), request_sha256="b" * 64)
        plan = self.plan()
        self.assertTrue(any(row["id"] == str(other.pk) for row in plan.report["blockers"]))
        self.assertEqual(plan.report["counts"]["studio.ProductionRun"], 1)
        self.asset.metadata["current_dependency"] = str(self.run.pk)
        self.asset.save()
        self.assertTrue(any(row["reason"] == "outside_scope_json_reference" for row in self.plan().report["blockers"]))

    def test_live_status_remains_blocked_independent_of_cost(self):
        attempt, _, _ = self.attempt()
        m.ProductionRun.objects.filter(pk=self.run.pk).update(status="running")
        m.GenerationAttempt.objects.filter(pk=attempt.pk).update(committed_cost=1, actual_cost=None)
        reasons = {row["reason"] for row in self.plan().report["blockers"]}
        self.assertIn("run_must_be_cancelled", reasons)

    def test_cancelled_development_scope_does_not_require_cost_settlement(self):
        attempt, _, _ = self.attempt()
        m.GenerationAttempt.objects.filter(pk=attempt.pk).update(committed_cost=1, actual_cost=None)
        with CaptureQueriesContext(connection) as queries:
            plan = self.plan()
        self.assertEqual(plan.report["blockers"], [])
        self.assertFalse(any(row["sql"].lstrip().upper().startswith(("UPDATE", "DELETE", "INSERT")) for row in queries))
        attempt.refresh_from_db()
        self.assertEqual(attempt.committed_cost, 1)
        self.assertIsNone(attempt.actual_cost)
        self.assertTrue(m.ProductionRun.objects.filter(pk=self.run.pk).exists())

    def test_attempt_active_remote_uncertain_and_distribution_are_not_terminal(self):
        attempt, task, prep = self.attempt()
        m.GenerationAttempt.objects.filter(pk=attempt.pk).update(status="failed", error_code="uncertain_remote_response")
        m.GenerationTask.objects.filter(pk=task.pk).update(status="succeeded", submission_token="submitted-fixture", submission_state="submitted",
            distribution_state="queued", distribution_revision=1, distribution_requested_at=timezone.now())
        m.ExecutionPreparation._base_manager.filter(pk=prep.pk).update(claim_token=uuid4(), claim_until=timezone.now() + timedelta(minutes=1))
        reasons = {row["reason"] for row in self.plan().report["blockers"]}
        self.assertTrue({"remote_response_uncertain", "remote_submission_not_terminal", "distribution_not_terminal", "preparation_claim_active"} <= reasons)

    def test_provenance_detachment_is_explicit_and_preserves_script_and_audit(self):
        attempt, _, _ = self.attempt()
        _, revision = create_pasted_script_source(project=self.project, user=self.actor, content="保留原剧本正文")
        revision = confirm_script_revision(revision, user=self.actor)
        m.ScriptRevision._base_manager.filter(pk=revision.pk).update(origin_generation_attempt=attempt)
        self.assertTrue(any(row["field"] == "origin_generation_attempt" for row in self.plan().report["blockers"]))
        plan = self.plan(detach_script_provenance=True)
        self.assertFalse(plan.report["blockers"])
        with patch.object(cleanup, "_backup_matches_plan", return_value={"database_sha256": "c" * 64}):
            self.execute(plan)
        revision.refresh_from_db()
        self.assertEqual(revision.content, "保留原剧本正文")
        self.assertEqual(revision.status, "confirmed")
        self.assertIsNone(revision.origin_generation_attempt_id)
        self.assertTrue(m.PlatformLifecycleAuditEvent.objects.filter(object_id=revision.pk,
            after_summary__status="provenance_detached").exists())
        self.assertTrue(m.ModelExecutionInstanceVersion.objects.exists())
        self.assertFalse(m.GenerationTask.objects.exists())

    def test_retained_run_using_revision_blocks_source_detachment(self):
        attempt, _, _ = self.attempt()
        _, revision = create_pasted_script_source(project=self.project, user=self.actor, content="Shared script")
        m.ScriptRevision._base_manager.filter(pk=revision.pk).update(origin_generation_attempt=attempt)
        m.ProductionRun.objects.create(owner=self.actor, project=self.project, plan_source="manual", script_revision=revision,
            status="cancelled", idempotency_key=str(uuid4()), request_sha256="b" * 64)
        self.assertTrue(any(row["reason"] == "retained_run_uses_script_provenance" for row in self.plan(detach_script_provenance=True).report["blockers"]))

    def test_delete_preserves_project_asset_files_and_existing_audit(self):
        prior = m.PlatformLifecycleAuditEvent.objects.create(actor=self.actor, action="retire",
            object_type="production_recipe_version", object_id=self.recipe.pk, reason_code="test.retire")
        with TemporaryDirectory() as directory:
            media = Path(directory) / "reference.txt"
            media.write_bytes(b"unchanged fixture")
            self.asset.local_path = str(media)
            self.asset.save()
            plan = self.plan()
            with patch.object(cleanup, "_backup_matches_plan", return_value={"database_sha256": "c" * 64}):
                result = self.execute(plan)
            self.assertEqual(media.read_bytes(), b"unchanged fixture")
        self.assertEqual(result["mode"], "production_run_cleanup_completed")
        self.assertFalse(m.ProductionRun.objects.filter(pk=self.run.pk).exists())
        self.assertFalse(m.ProductionRecipeVersion.objects.filter(pk=self.recipe.pk).exists())
        self.assertTrue(m.SeriesProject.objects.filter(pk=self.project.pk).exists())
        self.asset.refresh_from_db()
        self.assertEqual(self.asset.metadata, {"production_run_id": str(self.run.pk)})
        self.assertTrue(m.PlatformLifecycleAuditEvent.objects.filter(pk=prior.pk).exists())
        with self.assertRaises(CommandError):
            self.plan()

    def test_audit_failure_rolls_back_deletion(self):
        with patch.object(cleanup, "_backup_matches_plan", return_value={"database_sha256": "c" * 64}), \
                patch.object(m.PlatformLifecycleAuditEvent, "save", side_effect=RuntimeError("audit unavailable")):
            with self.assertRaises(RuntimeError):
                self.execute()
        self.assertTrue(m.ProductionRun.objects.filter(pk=self.run.pk).exists())
        self.assertTrue(m.StageRun.objects.filter(pk=self.stage.pk).exists())

    def test_stale_content_digest_and_missing_authority_never_delete(self):
        plan = self.plan()
        m.StageRun.objects.filter(pk=self.stage.pk).update(reason="changed, same IDs")
        cases = ({}, {"writers_stopped": False}, {"confirmation": ""}, {"backup_manifest": ""}, {"operator": "unknown"})
        with patch.object(cleanup, "_hard_delete_plan") as delete:
            for changes in cases:
                with self.subTest(changes=changes), self.assertRaises(CommandError):
                    self.execute(plan, **changes)
            delete.assert_not_called()

    def test_valid_worker_lease_blocks_execution(self):
        m.WorkerLease.objects.create(key="fixture-lease", holder_id="worker", expires_at=timezone.now() + timedelta(minutes=1))
        with self.assertRaisesMessage(CommandError, "worker"):
            self.execute()

    def test_command_modes_cannot_mix_or_use_no_backup_escape(self):
        for changes in ({"recipe_version": None}, {"resource_policy_cutover": True}, {"irreversible_without_backup": True},
                {"writers_stopped": True}, {"workflow_version": [uuid4()]}):
            args = dict(production_run=self.run.pk, recipe_version=self.recipe.pk, stdout=StringIO())
            args.update(changes)
            with self.subTest(changes=changes), self.assertRaises(CommandError):
                call_command("purge_incompatible_workflows", **args)
        output = StringIO()
        call_command("purge_incompatible_workflows", production_run=self.run.pk, recipe_version=self.recipe.pk, stdout=output)
        self.assertEqual(json.loads(output.getvalue())["mode"], "production_run_cleanup_preview")


class ProductionRunCleanupBackupTests(CleanupFixture, TransactionTestCase):
    def backup(self, directory):
        path = Path(directory) / "database.sqlite3"
        with closing(sqlite3.connect(path)) as saved:
            connection.connection.backup(saved)
        manifest = {"database": {"kind": "sqlite", "included": True, "backup_relative_path": path.name},
            "files": [{"relative_path": path.name, "sha256": hashlib.sha256(path.read_bytes()).hexdigest(), "bytes": path.stat().st_size}]}
        manifest_path = Path(directory) / "manifest.json"
        manifest_path.write_text(json.dumps(manifest), encoding="utf-8")
        (Path(directory) / "manifest.sha256").write_text(hashlib.sha256(manifest_path.read_bytes()).hexdigest(), encoding="ascii")
        return manifest_path

    def test_real_backup_matches_then_detects_changed_row(self):
        with TemporaryDirectory() as directory:
            manifest = self.backup(directory)
            plan = self.plan()
            self.assertTrue(cleanup._backup_matches_plan(plan, manifest)["database_sha256"])
            m.SeriesProject.objects.filter(pk=self.project.pk).update(title="Changed content")
            with self.assertRaisesMessage(CommandError, "精确状态"):
                cleanup._backup_matches_plan(self.plan(), manifest)

    def test_real_backup_integrity_required_for_execute_and_restore(self):
        with TemporaryDirectory() as directory:
            manifest = self.backup(directory)
            plan = self.plan()
            self.execute(plan, backup_manifest=str(manifest))
            with closing(sqlite3.connect(Path(directory) / "database.sqlite3")) as saved:
                self.assertEqual(saved.execute("SELECT COUNT(*) FROM studio_productionrun WHERE id=?", [self.run.pk.hex]).fetchone()[0], 1)
            self.assertFalse(m.ProductionRun.objects.filter(pk=self.run.pk).exists())
            self.assertTrue(m.SeriesProject.objects.filter(pk=self.project.pk).exists())

    def test_corrupt_manifest_and_wrong_size_are_rejected(self):
        with TemporaryDirectory() as directory:
            manifest = self.backup(directory)
            data = json.loads(manifest.read_text())
            data["files"][0]["bytes"] += 1
            manifest.write_text(json.dumps(data), encoding="utf-8")
            with self.assertRaises(CommandError):
                cleanup._backup_matches_plan(self.plan(), manifest)
            (Path(directory) / "manifest.sha256").write_text(hashlib.sha256(manifest.read_bytes()).hexdigest(), encoding="ascii")
            with self.assertRaisesMessage(CommandError, "大小"):
                cleanup._backup_matches_plan(self.plan(), manifest)
