"""Original Stage scheduler integration, with isolated provider replies only."""
from decimal import Decimal
from django.core.exceptions import ValidationError
from django.db import connection
from django.test import TestCase, override_settings
from django.test.utils import CaptureQueriesContext
from rest_framework.test import APIClient

from studio.models import AssetGenerationJob, ExecutionPreparation, GeneratedArtifact, GenerationAttempt, ScriptRevision
from studio.services.production_project_text_stages import project_text_candidate_manifest
from studio.services.production_runs import advance_production_run
from studio.tests import test_execution_production_dispatch as fixtures


@override_settings(AI_PROVIDER_ALLOWED_HOSTS=("api.example.com",))
class ProjectTextStageTests(TestCase):
    setUpTestData = classmethod(fixtures.ProductionDispatchTests.setUpTestData.__func__)
    profile_instance = fixtures.ProductionDispatchTests.profile_instance
    root = fixtures.ProductionDispatchTests.root
    prepare = fixtures.ProductionDispatchTests.prepare
    structure_instance = fixtures.ProductionDispatchTests.structure_instance
    pass_smoke = fixtures.ProductionDispatchTests.pass_smoke
    advance = fixtures.ProductionDispatchTests.advance
    response = fixtures.ProductionDispatchTests.response
    process = fixtures.ProductionDispatchTests.process
    visual_root = fixtures.ProductionDispatchTests.visual_root
    setUp = fixtures.ProductionDispatchTests.setUp

    def test_recipe_resource_pause_resumes_same_preparations_only_on_explicit_continue(self):
        self.pass_smoke()
        fixtures.ProductionDispatchTests.set_system_budget(self, "0")
        run = advance_production_run(self.run)
        self.assertEqual(run.status, "awaiting_input")
        self.assertEqual(run.blocked_code, "execution_preparation_review_required")
        ids = set(ExecutionPreparation.objects.filter(stage_run=self.stage).values_list("pk", flat=True))
        self.assertEqual(len(ids), 2)
        self.assertFalse(GenerationAttempt.objects.exists())
        fixtures.ProductionDispatchTests.set_system_budget(self, "1")
        self.assertEqual(advance_production_run(run, automatic=True).status, "awaiting_input")
        self.assertFalse(GenerationAttempt.objects.exists())
        client = APIClient()
        client.force_authenticate(self.actor)
        response = client.post(f"/api/v1/production-runs/{run.pk}/continue/")
        self.assertEqual(response.status_code, 200, response.data)
        run.refresh_from_db()
        self.assertEqual(run.status, "running", run.blocked_reason)
        self.assertEqual(set(ExecutionPreparation.objects.filter(stage_run=self.stage).values_list("pk", flat=True)), ids)
        self.assertEqual(GenerationAttempt.objects.count(), 2)

    def test_original_scheduler_creates_all_candidates_once_from_plan(self):
        self.pass_smoke()
        self.stage.status = "pending"
        self.stage.save(update_fields=("status",))
        result = advance_production_run(self.run)
        self.assertEqual(result.status, "running", result.blocked_reason)
        self.assertEqual(ExecutionPreparation.objects.filter(stage_run=self.stage).count(), 2)
        attempts = list(GenerationAttempt.objects.filter(stage_run=self.stage))
        self.assertEqual(len(attempts), 2)
        self.assertEqual({a.route_index for a in attempts}, {0})
        self.assertEqual({a.execution_instance_version_id for a in attempts}, {self.version.pk})
        self.assertEqual(len({a.request_sha256 for a in attempts}), 2)
        self.assertEqual(result.committed_cost_total, sum(a.estimated_cost for a in attempts))
        self.stage.refresh_from_db()
        self.assertEqual(self.stage.attempt_count, 2)
        self.assertEqual(self.stage.input_snapshot["run_plan_sha256"], self.plan.sha256)
        self.assertNotIn("routes", self.stage.config_snapshot)
        advance_production_run(self.run)
        self.assertEqual(GenerationAttempt.objects.filter(stage_run=self.stage).count(), 2)
        self.assertFalse(AssetGenerationJob.objects.exists())

    def test_completion_is_candidate_manifest_not_automatic_adoption(self):
        self.pass_smoke()
        advance_production_run(self.run)
        for _ in range(2):
            result, _ = self.process()
            self.assertEqual(result.status, "succeeded", result.error_code)
        result = advance_production_run(self.run)
        self.stage.refresh_from_db()
        self.assertEqual(self.stage.status, "succeeded", self.stage.reason)
        self.assertEqual(self.stage.output_reference, f"stage-candidates:{self.stage.pk}")
        snapshot = self.stage.output_snapshot
        self.assertEqual(snapshot["formal_object_type"], "")
        self.assertEqual(snapshot["canonical"], {})
        self.assertEqual(snapshot["review_payload"]["candidate_count"], 2)
        self.assertNotIn("members", snapshot["review_payload"])
        self.assertIsNone(result.script_revision_id)
        self.assertFalse(GeneratedArtifact.objects.filter(status="selected").exists())
        # An isolated single-generation recipe is not a completed production.
        self.assertNotEqual(result.status, "succeeded")

    def test_missing_smoke_is_visible_preparation_wait_and_can_resume(self):
        result = advance_production_run(self.run)
        self.assertEqual(result.status, "running", result.blocked_reason)
        self.assertEqual(result.blocked_code, "waiting_execution_preparation")
        self.assertFalse(GenerationAttempt.objects.exists())
        original_ids = set(ExecutionPreparation.objects.filter(stage_run=self.stage).values_list("id", flat=True))
        self.pass_smoke()
        result = advance_production_run(self.run)
        self.assertEqual(result.status, "running", result.blocked_reason)
        self.assertEqual(GenerationAttempt.objects.count(), 2)
        self.assertEqual(
            set(ExecutionPreparation.objects.filter(stage_run=self.stage).values_list("id", flat=True)),
            original_ids,
        )

    def test_incomplete_output_pauses_recipe_scan_until_explicit_continuation(self):
        import tempfile
        from studio.services.provider_registry import ProviderDriverError
        from studio.services.production_run_auto_advance import active_recipe_runs
        with tempfile.TemporaryDirectory() as folder, override_settings(MEDIA_ROOT=folder):
            self.pass_smoke()
            advance_production_run(self.run)
            error = ProviderDriverError("length", code="provider_response_invalid")
            error.metadata = {"contract_error_code": "response_output_budget_exhausted", "finish_reason": "length"}
            error.partial_text = '{"episodes": ['
            self.process(effect=error)
            result = advance_production_run(self.run)
            self.assertEqual(result.status, "awaiting_input")
            self.assertEqual(result.blocked_code, "execution_output_review_required")
            self.assertFalse(active_recipe_runs().filter(pk=result.pk).exists())
            self.assertEqual(advance_production_run(result, automatic=True).status, "awaiting_input")
            self.assertEqual(GenerationAttempt.objects.filter(stage_run=self.stage).count(), 2)
            self.assertFalse(GeneratedArtifact.objects.filter(status="selected").exists())

    def test_continue_does_not_reset_blocked_stage_from_deployment_smoke(self):
        self.structure_instance()
        self.profile_instance()
        self.run = self.root(stage_key="script_structure", source=self.fixture["inputs"])
        self.stage.status = "blocked"
        self.stage.reason_code = "script_structure_smoke_missing_or_stale"
        self.stage.reason = "Blocked original stage; no execution preparation exists."
        self.stage.save(update_fields=("status", "reason_code", "reason"))
        with CaptureQueriesContext(connection) as queries:
            result = advance_production_run(self.run)
        sql = " ".join(row["sql"].lower() for row in queries.captured_queries)
        self.assertNotIn("studio_modeldeploymentcapabilitysnapshot", sql)
        self.assertNotIn("studio_promptprofilebinding", sql)
        self.stage.refresh_from_db()
        self.assertEqual(result.status, "blocked")
        self.assertEqual(result.blocked_code, self.stage.reason_code)
        self.assertEqual(self.stage.status, "blocked")
        self.assertFalse(ExecutionPreparation.objects.filter(stage_run=self.stage).exists())
        self.assertFalse(GenerationAttempt.objects.exists())

    def test_disabled_owner_is_explicitly_blocked_without_preparation_or_call(self):
        self.actor.is_active = False
        self.actor.save(update_fields=("is_active",))
        result = advance_production_run(self.run)
        self.assertEqual(result.status, "blocked")
        self.assertEqual(result.blocked_code, "execution_owner_unavailable")
        self.assertFalse(ExecutionPreparation.objects.filter(stage_run=self.stage).exists())
        self.assertFalse(GenerationAttempt.objects.exists())

    def test_partial_failure_without_frozen_retry_policy_requires_action_and_keeps_success(self):
        self.pass_smoke()
        advance_production_run(self.run)
        self.process()
        failed, _ = self.process(response={"request_id": "missing-output"})
        self.assertEqual(failed.status, "failed")
        result = advance_production_run(self.run)
        self.assertEqual(result.status, "awaiting_input", result.blocked_reason)
        self.assertEqual(result.blocked_code, "execution_retry_requires_action")
        self.assertIn("最大尝试次数", result.blocked_reason)
        self.assertEqual(GenerationAttempt.objects.count(), 2)
        self.assertEqual(GeneratedArtifact.objects.count(), 1)
        self.assertFalse(GeneratedArtifact.objects.filter(status="selected").exists())

    def test_missing_candidate_never_becomes_stage_success(self):
        self.pass_smoke()
        advance_production_run(self.run)
        self.process()
        self.process()
        artifact = GeneratedArtifact.objects.first()
        artifact.status = "invalid"
        artifact.save(update_fields=("status",))
        ids = list(ExecutionPreparation.objects.filter(stage_run=self.stage).values_list("id", flat=True))
        with self.assertRaisesMessage(ValidationError, "唯一的有效候选"):
            project_text_candidate_manifest(self.stage, ids)
        result = advance_production_run(self.run)
        self.assertEqual(result.status, "blocked")
        self.stage.refresh_from_db()
        self.assertNotEqual(self.stage.status, "succeeded")

    def test_structure_stage_uses_same_scheduler_and_unadopted_candidate_details(self):
        from studio.services.execution_production_candidates import read_production_text_candidate

        self.structure_instance()
        self.profile_instance()
        self.run = self.root(stage_key="script_structure", source=self.fixture["inputs"])
        self.run.allow_external_calls = True
        self.run.save(update_fields=("allow_external_calls",))
        self.pass_smoke()
        result = advance_production_run(self.run)
        self.assertEqual(result.status, "running", result.blocked_reason)
        for _ in range(2):
            result, _ = self.process()
            self.assertEqual(result.status, "succeeded", result.error_code)
        advance_production_run(self.run)
        self.stage.refresh_from_db()
        self.assertEqual(self.stage.status, "succeeded", self.stage.reason)
        candidate = read_production_text_candidate(GeneratedArtifact.objects.filter(stage_run=self.stage).first())
        self.assertEqual(candidate["operation"], "script.structure")
        self.assertTrue(candidate["text"])
        self.run.refresh_from_db()
        self.assertIsNone(self.run.script_revision_id)

    def test_visual_stage_uses_same_scheduler_without_overwriting_project(self):
        from studio.services.execution_production_candidates import read_production_text_candidate

        self.visual_root()
        self.pass_smoke()
        result = advance_production_run(self.run)
        self.assertEqual(result.status, "running", result.blocked_reason)
        for _ in range(2):
            result, _ = self.process()
            self.assertEqual(result.status, "succeeded", result.error_code)
        advance_production_run(self.run)
        self.stage.refresh_from_db()
        self.assertEqual(self.stage.status, "succeeded", self.stage.reason)
        candidate = read_production_text_candidate(GeneratedArtifact.objects.filter(stage_run=self.stage).first())
        self.assertEqual(candidate["operation"], "visual_rules.generate")
        self.assertIn("color_script", candidate["candidate"])
        self.assertIsNone(candidate["text"])
        self.project.refresh_from_db()
        self.assertEqual(self.project.continuity_summary, "")
        self.assertFalse(GeneratedArtifact.objects.filter(status="selected").exists())

    def test_formal_artifact_api_has_narrow_list_and_verified_candidate_detail(self):
        from types import SimpleNamespace
        from studio.platform_views import GeneratedArtifactViewSet

        self.pass_smoke()
        advance_production_run(self.run)
        self.process()
        self.process()
        client = APIClient()
        client.force_authenticate(self.actor)
        with CaptureQueriesContext(connection) as queries:
            response = client.get("/api/v1/generated-artifacts/", {"stage_run": str(self.stage.pk)})
        self.assertEqual(response.status_code, 200, response.data)
        self.assertEqual(len(response.data["results"]), 2)
        self.assertTrue(all("manifest_metadata" not in row and "text_candidate" not in row for row in response.data["results"]))
        # Pagination also reads the shared configuration catalog. Count the
        # actual artifact reads: one count and one narrow page, no per-row I/O.
        artifact_reads = [q for q in queries if 'FROM "studio_generatedartifact"' in q["sql"]]
        self.assertEqual(len(artifact_reads), 2, [q["sql"] for q in artifact_reads])
        for query in queries:
            self.assertNotIn('"studio_generatedartifact"."manifest_metadata"', query["sql"])
            self.assertNotIn('"studio_generationattempt"."request_snapshot"', query["sql"])
            self.assertNotIn('"studio_productionrun"."input_snapshot"', query["sql"])
        view = GeneratedArtifactViewSet()
        view.action, view.request = "list", SimpleNamespace(user=self.actor)
        if connection.vendor == "sqlite":
            self.assertIn("SEARCH studio_generatedartifact USING INDEX", view.get_queryset().filter(stage_run=self.stage).explain())
        artifact = GeneratedArtifact.objects.first()
        response = client.get(f"/api/v1/generated-artifacts/{artifact.pk}/")
        self.assertEqual(response.status_code, 200, response.data)
        candidate = response.data["text_candidate"]
        self.assertEqual(candidate["operation"], "script.generate")
        # The current structured-script protocol preserves the whole returned
        # document; it no longer carries a parallel master_script string.
        self.assertEqual(candidate["text"], self.response()["text"])
        self.assertIn("episodes", candidate["candidate"])
        self.assertEqual(candidate["result_sha256"], artifact.generation_attempt.result_sha256)
        self.assertFalse(GeneratedArtifact.objects.filter(status="selected").exists())
        # A successful historical result can subsequently be excluded by the
        # original cancellation/late-result policy; details must not revive it.
        GenerationAttempt.objects.filter(pk=artifact.generation_attempt_id).update(ignore_late_result=True)
        self.assertEqual(client.get(f"/api/v1/generated-artifacts/{artifact.pk}/").status_code,400)
        self.assertTrue(GenerationAttempt.objects.get(pk=artifact.generation_attempt_id).ignore_late_result)
        ids=list(ExecutionPreparation.objects.filter(stage_run=self.stage).values_list("id",flat=True))
        with self.assertRaisesMessage(ValidationError,"尚未全部成功"):
            project_text_candidate_manifest(self.stage,ids)
        GenerationAttempt.objects.filter(pk=artifact.generation_attempt_id).update(ignore_late_result=False)
        GeneratedArtifact.objects.filter(pk=artifact.pk).update(status="invalid")
        self.assertEqual(client.get(f"/api/v1/generated-artifacts/{artifact.pk}/").status_code,400)
        self.assertEqual(GeneratedArtifact.objects.get(pk=artifact.pk).status,"invalid")

    def test_corrupt_script_candidate_detail_is_rejected_without_repair(self):
        self.pass_smoke()
        advance_production_run(self.run)
        self.process()
        artifact = GeneratedArtifact.objects.first()
        revision_id = artifact.manifest_metadata["script_revision_id"]
        ScriptRevision.objects.filter(pk=revision_id).update(content="tampered candidate")
        client = APIClient()
        client.force_authenticate(self.actor)
        response = client.get(f"/api/v1/generated-artifacts/{artifact.pk}/")
        self.assertEqual(response.status_code, 400, response.data)
        self.assertEqual(ScriptRevision.objects.get(pk=revision_id).content, "tampered candidate")
        self.assertFalse(GeneratedArtifact.objects.filter(status="selected").exists())
