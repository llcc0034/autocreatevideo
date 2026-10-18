"""Original isolated queue/selection/registration with a failed candidate peer.

Only admission observations and Comfy transport are substituted by the shared
fixture. No real model, current database, or business adoption is exercised.
"""
from django.core.exceptions import ValidationError
from django.test import override_settings
from django.db import connection
from django.test.utils import CaptureQueriesContext
from unittest.mock import patch

from studio.models import ArtifactSelectionDecision, AssetReferenceVersion, ExecutionPreparation, GenerationAttempt, StageRun
from studio.services.asset_references import read_generated_material
from studio.services.production_runs import StageBlocked, _formal_snapshot, _save_stage_snapshot
from studio.tests import test_recipe_mixed_material_stage as queue
from studio.tests.test_data_structures import StructureFixture


@override_settings(AI_PROVIDER_ALLOWED_HOSTS=("127.0.0.1", "api.example.com"))
class RecipeMaterialPartialFailureTests(StructureFixture):
    Fixture = queue.RecipeMixedMaterialStageTests
    tearDown = Fixture.tearDown
    create_job = Fixture.create_job
    primary_version = Fixture.primary_version
    write = Fixture.write
    input = staticmethod(Fixture.input)
    configure_variant_sources = Fixture.configure_variant_sources
    variant_anchors = Fixture.variant_anchors
    select = Fixture.select
    allow_calls = Fixture.allow_calls
    fill_fields = Fixture.fill_fields
    advance = Fixture.advance
    collect = Fixture.collect

    def setUp(self):
        self.Fixture.setUp(self)
        from studio.tests.recipe_request_fixtures import freeze_recipe_request_fixture
        # Complete the isolated request/stage fixture before exercising the
        # public scheduler; do not bypass its receipt or stage-row validation.
        freeze_recipe_request_fixture(self.run, authorized=False)
        from studio.tests.test_production_automatic_retry import freeze_retry_policy
        freeze_retry_policy(self.run, 1)
        StageRun.objects.create(production_run=self.run, stage_key="bible_image_review", mode="review",
            status="pending", ordinal=3, approval_required=True, idempotency_key=f"review-{self.run.pk}")
        def observations(preparation, definition, request, *, retry_of=None):
            if retry_of is not None:
                self.assertEqual(retry_of.preparation_id, preparation.pk)
            from dataclasses import replace
            from copy import deepcopy
            from studio.services.json_contracts import json_contract
            result = queue.production_observations(preparation, definition, request)
            body = deepcopy(result.root_contract.value)
            body["budget"]["is_external"] = False
            return replace(result, root_contract=json_contract(body))
        # Same controlled admission facts as the shared fixture, with the
        # original retry context accepted; dispatch/retry writers remain real.
        admission = patch("studio.services.execution_production_dispatch.collect_production_execution_admission",
            side_effect=observations)
        admission.start()
        self.addCleanup(admission.stop)

    def configure_declaration(self, declaration):
        self.Fixture.configure_declaration(self, declaration)
        declaration["routing"]["variant_count"] = 2
        if self._testMethodName == "test_manual_policy_waits_for_original_public_selection_without_retry":
            declaration["routing"]["evaluator"] = None
        if self._testMethodName == "test_recovery_preview_skips_failed_peers_of_successful_units":
            declaration["variant_routing"]["variant_count"] = 2
            declaration["variant_routing"]["evaluator"] = None

    def start(self):
        self.assertIsNone(self.advance(supply_fields=True))
        self.assertEqual(GenerationAttempt.objects.count(), 2)
        self.assertEqual(ExecutionPreparation.objects.filter(stage_run=self.stage).count(), 2)

    def failed(self):
        return GenerationAttempt.objects.get(status="failed")

    def test_partial_failure_automatic_registers_one_primary_and_both_variants(self):
        self.start()
        self.collect(fail=True)
        artifact = self.collect()
        failed = self.failed()
        original = GenerationAttempt.objects.filter(pk=failed.pk).values().get()
        self.assertIsNone(self.advance())
        self.assertEqual(self.stage.reason_code, "waiting_material_registration")
        decision = ArtifactSelectionDecision.objects.get()
        self.assertEqual((decision.artifact_id, decision.selection_mode, decision.selected_by_id),
            (artifact.pk, "automatic", None))
        artifact.refresh_from_db()
        primary = read_generated_material(artifact=artifact)
        self.assertIsNotNone(primary)
        self.assertIsNone(self.advance(supply_fields=True))
        siblings = [self.collect(), self.collect()]
        self.assertIsNone(self.advance())
        self.assertEqual(self.advance(), "generated-artifacts:3")
        self.assertEqual(self.advance(), "generated-artifacts:3")
        self.assertEqual(GenerationAttempt.objects.count(), 4)
        self.assertEqual(ArtifactSelectionDecision.objects.filter(selection_mode="automatic", selected_by=None).count(), 3)
        for sibling in siblings:
            sibling.refresh_from_db()
            self.assertEqual(read_generated_material(artifact=sibling).generated_from_id, primary.pk)
        self.assertEqual(GenerationAttempt.objects.filter(pk=failed.pk).values().get(), original)
        self.stage.status = "succeeded"
        self.stage.save(update_fields=["status"])
        _save_stage_snapshot(self.stage, "output_snapshot", _formal_snapshot(self.run, self.stage, "generated-artifacts:3"))
        self.assertEqual(len(self.stage.output_snapshot["canonical"]["material_generations"]), 3)

    def test_manual_policy_waits_for_original_public_selection_without_retry(self):
        self.start()
        self.collect(fail=True)
        artifact = self.collect()
        for _ in range(2):
            self.assertIsNone(self.advance())
            self.assertEqual(self.stage.reason_code, "waiting_material_selection")
        self.assertFalse(ArtifactSelectionDecision.objects.exists())
        self.assertEqual(GenerationAttempt.objects.count(), 2)
        self.client.force_authenticate(self.user)
        url = f"/api/v1/production-runs/{self.run.pk}/"
        context = self.client.get(url + "material-selection-context/", {"artifact_id": str(artifact.pk)})
        self.assertEqual(context.status_code, 200, context.data)
        self.assertTrue(context.data["can_select"])
        response = self.client.post(url + "select-artifact/", {"artifact_id": str(artifact.pk),
            "material_selection": {"expected_decision_id": context.data["expected_decision_id"]}}, format="json", HTTP_IDEMPOTENCY_KEY="select-partial")
        self.assertEqual(response.status_code, 200, response.data)
        decision = ArtifactSelectionDecision.objects.get()
        self.assertEqual((decision.selection_mode, decision.selected_by_id), ("manual", self.user.pk))
        self.assertIsNone(self.advance())
        artifact.refresh_from_db()
        self.assertIsNotNone(read_generated_material(artifact=artifact))
        self.assertEqual(GenerationAttempt.objects.count(), 2)
        self.assertEqual(self.failed().status, "failed")

    def test_all_failed_retains_explicit_recovery_and_never_dispatches_variants(self):
        self.start()
        self.collect(fail=True)
        self.collect(fail=True)
        for _ in range(2):
            with self.assertRaises(StageBlocked) as caught:
                self.advance()
            self.assertEqual(caught.exception.code, "execution_retry_exhausted")
        self.assertFalse(ArtifactSelectionDecision.objects.exists())
        self.assertEqual(GenerationAttempt.objects.count(), 2)
        self.assertEqual(ExecutionPreparation.objects.filter(stage_run=self.stage).count(), 2)
        self.assertEqual(AssetReferenceVersion.objects.count(), self.initial_versions)

    def public_recovery(self):
        from studio.services.production_runs import advance_production_run
        # The shared stage fixture omits unrelated final delivery stages only.
        # Run/Stage waiting state, source checks and media execution remain real.
        with patch("studio.services.production_runs._assert_frozen_bible_run_closure"):
            self.run = advance_production_run(self.run)
        self.stage.refresh_from_db()
        self.assertEqual((self.run.status, self.stage.status), ("awaiting_input", "blocked"), self.run.blocked_reason)
        self.assertEqual(self.run.blocked_code, "execution_retry_exhausted")
        self.client.force_authenticate(self.user)
        return f"/api/v1/production-runs/{self.run.pk}/"

    def test_all_failed_public_takeover_preview_and_explicit_retry_keep_original_requests(self):
        from studio.services.production_runs import advance_production_run
        self.start()
        self.collect(fail=True)
        self.collect(fail=True)
        path = self.public_recovery()
        original = list(GenerationAttempt.objects.order_by("id"))
        self.assertEqual(advance_production_run(self.run, automatic=True).status, "awaiting_input")
        self.assertEqual(GenerationAttempt.objects.count(), 2)
        with CaptureQueriesContext(connection) as queries:
            preview = self.client.get(path + "retry-preview/", {"expected_stage_id": str(self.stage.pk)})
        self.assertEqual(preview.status_code, 200, preview.data)
        self.assertFalse(any(q["sql"].lstrip().upper().startswith(("INSERT", "UPDATE", "DELETE")) for q in queries))
        self.assertTrue(preview.data["can_submit"], preview.data)  # Local terminal failure needs no invented bill.
        body = preview.data["retry_context"]
        self.assertEqual(set(body["expected_attempt_ids"]), {str(row.pk) for row in original})
        self.assertTrue(all(row.actual_cost is None and row.committed_cost == 0 for row in original))
        before = list(GenerationAttempt.objects.order_by("id").values())
        with patch("studio.services.production_runs._assert_frozen_bible_run_closure"):
            response = self.client.post(path + "retry/", body, format="json")
        self.assertEqual(response.status_code, 200, response.data)
        self.assertEqual(response.data["status"], "running")
        for row in original:
            successor = GenerationAttempt.objects.get(retry_of=row)
            self.assertEqual(successor.preparation_id, row.preparation_id)
            self.assertEqual(successor.request_snapshot, row.request_snapshot)
            self.assertEqual(successor.request_sha256, row.request_sha256)
            self.assertEqual(successor.retry_requested_by_id, self.user.pk)
        self.assertEqual(before, list(GenerationAttempt.objects.filter(pk__in=[row.pk for row in original]).order_by("id").values()))
        self.assertEqual(self.client.post(path + "retry/", body, format="json").status_code, 200)
        self.assertEqual(GenerationAttempt.objects.count(), 4)
        self.assertEqual(ExecutionPreparation.objects.filter(stage_run=self.stage).count(), 2)
        self.collect(fail=True)
        artifact = self.collect()
        self.assertIsNone(self.advance())
        self.assertEqual(ArtifactSelectionDecision.objects.get().artifact_id, artifact.pk)
        artifact.refresh_from_db()
        self.assertIsNotNone(read_generated_material(artifact=artifact))
        self.assertEqual(GenerationAttempt.objects.count(), 4)

    def test_recovery_preview_skips_failed_peers_of_successful_units(self):
        from studio.services.production_execution_retry import stage_retry_context, stage_retry_attempts
        self.start()
        self.collect()
        self.collect()
        self.assertIsNone(self.advance())  # Exact primary registration.
        self.assertIsNone(self.advance(supply_fields=True))
        for fail in (True, True, True, False):
            self.collect(fail=fail)
        path = self.public_recovery()
        GenerationAttempt.objects.filter(status="failed").update(actual_cost=0, committed_cost=0)
        preview = self.client.get(path + "retry-preview/", {"expected_stage_id": str(self.stage.pk)})
        self.assertEqual(preview.status_code, 200, preview.data)
        self.assertTrue(preview.data["can_submit"], preview.data)
        self.assertEqual(len(preview.data["retry_context"]["expected_attempt_ids"]), 2)
        rows = preview.data["results"]
        self.assertEqual(sum(row["disposition"] == "retry" for row in rows), 2)
        self.assertEqual(sum(row["status"] == "failed" and row["disposition"] == "skip" for row in rows), 1)
        self.assertIn("成功任务与产物保留", self.stage.reason)
        query = stage_retry_attempts(self.stage).order_by("id").values("id")
        self.assertIn("INDEX", query.explain())
        self.assertNotIn("request_snapshot", str(query.query))
        # Later resource-review gates must keep the same reduced target set.
        self.stage.reason_code = "execution_resource_confirmation_required"
        self.assertEqual(stage_retry_context(self.stage), preview.data["retry_context"])
        self.assertEqual(GenerationAttempt.objects.count(), 6)
        self.assertEqual(ArtifactSelectionDecision.objects.count(), 1)
        # A successful peer is not permission to hide an uncertain failed call.
        peer_id = next(row["attempt_id"] for row in rows if row["status"] == "failed" and row["disposition"] == "skip")
        peer = GenerationAttempt.objects.get(pk=peer_id)
        peer.generation_task.submission_state = "submitted"
        peer.generation_task.save(update_fields=["submission_state"])
        changed = self.client.get(path + "retry-preview/", {"expected_stage_id": str(self.stage.pk)})
        self.assertFalse(changed.data["can_submit"], changed.data)
        self.assertEqual(next(row["disposition"] for row in changed.data["results"]
            if str(row["attempt_id"]) == str(peer_id)), "attention")
        response = self.client.post(path + "retry/", preview.data["retry_context"], format="json")
        self.assertEqual(response.status_code, 400, response.data)
        self.assertFalse(GenerationAttempt.objects.filter(retry_of__isnull=False).exists())

    def test_waiting_recovery_rechecks_unknown_result_before_retry(self):
        self.start()
        self.collect(fail=True)
        self.collect(fail=True)
        path = self.public_recovery()
        GenerationAttempt.objects.filter(status="failed").update(actual_cost=0, committed_cost=0)
        preview = self.client.get(path + "retry-preview/", {"expected_stage_id": str(self.stage.pk)})
        self.assertTrue(preview.data["can_submit"], preview.data)
        attempt = GenerationAttempt.objects.order_by("id").first()
        task = attempt.generation_task
        task.submission_state = "submitted"
        task.save(update_fields=["submission_state"])
        response = self.client.post(path + "retry/", preview.data["retry_context"], format="json")
        self.assertEqual(response.status_code, 400, response.data)
        self.assertEqual(GenerationAttempt.objects.count(), 2)
        self.assertFalse(GenerationAttempt.objects.filter(retry_of__isnull=False).exists())
        self.assertFalse(ArtifactSelectionDecision.objects.exists())

    def recovery_choice(self):
        self.start()
        self.collect(fail=True)
        self.collect(fail=True)
        path = self.public_recovery()
        body = {"stage_run_id": str(self.stage.pk), "unit_key": f"appearance:{self.primary.setting_revision.variant_id}",
            "requirement_key": f"setting:{self.primary.setting_revision_id}",
            "expected_run_plan_sha256": self.plan.sha256, "reference_version_id": str(self.primary.pk)}
        return path, body

    def test_all_failed_exact_version_takeover_preserves_requests_and_supplies_variants(self):
        from studio.models import ProductionMaterialFulfillment
        from studio.services.execution_production_attempts import require_retryable_attempt
        from studio.services.execution_production_dispatch import dispatch_production_execution, current_production_gate
        from studio.services.production_execution_retry import stage_retry_context
        from studio.services.media_generation_targets import read_media_generation_target
        from studio.services.production_recipe_material_fulfillment import read_recipe_material_fulfillment
        path, body = self.recovery_choice()
        originals = list(GenerationAttempt.objects.order_by("id").values())
        with CaptureQueriesContext(connection) as queries:
            context = self.client.get(path + "material-fulfillment-context/",
                {key: body[key] for key in ("stage_run_id", "unit_key")})
        self.assertEqual(context.status_code, 200, context.data)
        self.assertTrue(context.data["can_select"], context.data)
        self.assertFalse(any(q["sql"].lstrip().upper().startswith(("INSERT", "UPDATE", "DELETE")) for q in queries))
        result = self.client.post(path + "fulfill-material/", body, format="json")
        self.assertEqual(result.status_code, 201, result.data)
        self.assertEqual(self.client.post(path + "fulfill-material/", body, format="json").status_code, 200)
        self.run.refresh_from_db()
        self.assertEqual((self.run.status, self.run.blocked_code), ("running", ""))
        self.assertEqual(list(GenerationAttempt.objects.order_by("id").values()), originals)
        self.assertEqual(AssetReferenceVersion.objects.count(), self.initial_versions)
        self.assertFalse(ArtifactSelectionDecision.objects.exists())
        choice = read_recipe_material_fulfillment(stage=self.stage, unit_key=body["unit_key"])
        self.assertEqual(choice["version"].pk, self.primary.pk)
        self.assertEqual(stage_retry_context(self.stage)["expected_attempt_ids"], [])
        preview = self.client.get(path + "retry-preview/", {"expected_stage_id": str(self.stage.pk)})
        self.assertEqual(preview.status_code, 200, preview.data)
        self.assertFalse(preview.data["can_submit"])
        self.assertEqual({row["disposition"] for row in preview.data["results"]}, {"skip"})
        self.assertTrue(all("已明确选用成图" in row["message"] for row in preview.data["results"]))
        failed = GenerationAttempt.objects.order_by("id").first()
        for action in (lambda: require_retryable_attempt(failed), lambda: current_production_gate(failed.preparation),
                lambda: dispatch_production_execution(preparation_id=failed.preparation_id,
                    expected_preparation_revision=failed.preparation.revision, actor=self.user, retry_of_id=failed.pk)):
            with self.assertRaisesMessage(ValidationError, "已明确选用成图"):
                action()
        self.assertEqual(ProductionMaterialFulfillment.objects.get().selected_by_id, self.user.pk)
        self.assertIsNone(self.advance(supply_fields=True))
        variants = GenerationAttempt.objects.exclude(pk__in=[row["id"] for row in originals])
        self.assertEqual(variants.count(), 2)
        for attempt in variants:
            target = read_media_generation_target(attempt.preparation).value
            self.assertEqual(target["appearance"]["primary"]["version_id"], str(self.primary.pk))
        self.collect()
        self.collect()
        self.assertIsNone(self.advance())
        self.assertEqual(self.advance(), "generated-artifacts:2;material-fulfillments:1")
        self.assertEqual(self.advance(), "generated-artifacts:2;material-fulfillments:1")
        self.assertEqual(GenerationAttempt.objects.count(), 4)
        self.assertEqual(list(GenerationAttempt.objects.filter(pk__in=[row["id"] for row in originals]).order_by("id").values()), originals)
        self.stage.status = "succeeded"
        self.stage.save(update_fields=["status"])
        snapshot = _formal_snapshot(self.run, self.stage, "generated-artifacts:2;material-fulfillments:1")
        self.assertEqual(len(snapshot["canonical"]["material_fulfillments"]), 1)
        self.assertEqual(len(snapshot["canonical"]["material_generations"]), 2)
        from studio.services.production_recipe_material_review import material_review_context
        _save_stage_snapshot(self.stage, "output_snapshot", snapshot)
        context = material_review_context(self.run, snapshot["canonical"])
        self.assertEqual(len(context), 1)
        self.assertEqual(next(iter(context.values()))["primary_asset_id"], str(self.primary.asset_id))

    def test_all_failed_upload_takeover_is_atomic_and_uses_original_asset_writer(self):
        from studio.models import ProductionMaterialFulfillment
        from studio.tests.test_asset_management_api import image_upload
        path, body = self.recovery_choice()
        originals = list(GenerationAttempt.objects.order_by("id").values())
        uploaded = self.client.post("/api/v1/assets/", {"name": "isolated failed-unit replacement",
            "kind": "reference", "file": image_upload()}, format="multipart")
        self.assertEqual(uploaded.status_code, 201, uploaded.data)
        body.pop("reference_version_id")
        body.update(series_id=str(self.primary.series_id), asset_id=uploaded.data["id"], source="upload")
        save = ProductionMaterialFulfillment.save
        def fail(row, *args, **kwargs):
            save(row, *args, **kwargs)
            raise RuntimeError("isolated recovery rollback")
        with patch.object(ProductionMaterialFulfillment, "save", fail), self.assertRaisesMessage(RuntimeError, "rollback"):
            self.client.post(path + "fulfill-material/", body, format="json")
        self.run.refresh_from_db()
        self.assertEqual(self.run.status, "awaiting_input")
        self.assertFalse(ProductionMaterialFulfillment.objects.exists())
        self.assertEqual(AssetReferenceVersion.objects.count(), self.initial_versions)
        result = self.client.post(path + "fulfill-material/", body, format="json")
        self.assertEqual(result.status_code, 201, result.data)
        self.assertEqual(result.data["source"], "upload")
        self.assertEqual(self.client.post(path + "fulfill-material/", body, format="json").status_code, 200)
        version = AssetReferenceVersion.objects.get(pk=result.data["reference_version_id"])
        self.assertIsNone(version.generated_from_id)
        self.assertEqual(version.setting_revision_id, self.primary.setting_revision_id)
        self.assertEqual(AssetReferenceVersion.objects.count(), self.initial_versions + 1)
        self.assertEqual(list(GenerationAttempt.objects.order_by("id").values()), originals)
        self.assertFalse(ArtifactSelectionDecision.objects.exists())

    def test_failure_takeover_rechecks_inflight_unknown_cancelled_success_and_other_pause(self):
        from django.db import transaction
        from studio.models import ProductionMaterialFulfillment, ProductionRun, GenerationTask
        path, body = self.recovery_choice()
        attempt = GenerationAttempt.objects.order_by("id").first()
        cases = [(GenerationAttempt, attempt.pk, {"status": status}) for status in ("queued", "running", "submitting", "succeeded", "cancelled")]
        cases += [(GenerationAttempt, attempt.pk, {"ignore_late_result": True}),
            (ExecutionPreparation, attempt.preparation_id, {"status": "cancelled"}),
            (GenerationTask, attempt.generation_task_id, {"submission_state": "submitted"}),
            (GenerationTask, attempt.generation_task_id, {"outputs": [{"filename": "unarchived.png"}]}),
            (ProductionRun, self.run.pk, {"blocked_code": "execution_preparation_review_required"})]
        for model, pk, changes in cases:
            with self.subTest(changes=changes), transaction.atomic():
                model.objects.filter(pk=pk).update(**changes)
                context = self.client.get(path + "material-fulfillment-context/",
                    {key: body[key] for key in ("stage_run_id", "unit_key")})
                self.assertEqual(context.status_code, 200, context.data)
                self.assertFalse(context.data["can_select"], context.data)
                self.assertEqual(self.client.post(path + "fulfill-material/", body, format="json").status_code, 400)
                self.assertFalse(ProductionMaterialFulfillment.objects.exists())
                self.assertEqual(GenerationAttempt.objects.count(), 2)
                transaction.set_rollback(True)

    def test_unresolved_peer_cannot_be_hidden_by_success_or_resent(self):
        self.start()
        self.collect(fail=True)
        self.collect()
        task = self.failed().generation_task
        task.submission_state = "submitted"
        task.save(update_fields=["submission_state"])
        with self.assertRaises(StageBlocked) as error:
            self.advance()
        self.assertEqual(error.exception.code, "execution_candidates_incomplete")
        self.assertIn("结果未知", error.exception.reason)
        self.assertFalse(ArtifactSelectionDecision.objects.exists())
        self.assertEqual(GenerationAttempt.objects.count(), 2)
        self.assertEqual(AssetReferenceVersion.objects.count(), self.initial_versions)

    def test_cancelled_or_ignored_peer_does_not_authorize_selection(self):
        self.start()
        self.collect(fail=True)
        self.collect()
        failed = self.failed()
        from studio.services.execution_production_attempts import current_primary_attempts, retry_check_queryset
        query = retry_check_queryset(current_primary_attempts(stage=self.stage).filter(pk=failed.pk))
        self.assertIn("INDEX", query.explain())
        for status, ignored in (("cancelled", False), ("failed", True)):
            with self.subTest(status=status, ignored=ignored):
                GenerationAttempt.objects.filter(pk=failed.pk).update(status=status, ignore_late_result=ignored)
                with self.assertRaises(StageBlocked) as error:
                    self.advance()
                self.assertEqual(error.exception.code, "execution_candidates_incomplete")
                self.assertFalse(ArtifactSelectionDecision.objects.exists())
                self.assertEqual(GenerationAttempt.objects.count(), 2)
                self.assertEqual(AssetReferenceVersion.objects.count(), self.initial_versions)

    def test_pending_peer_waits_until_terminal_failure_before_selecting(self):
        self.start()
        artifact = self.collect()
        self.assertIsNone(self.advance())
        self.assertFalse(ArtifactSelectionDecision.objects.exists())
        self.collect(fail=True)
        self.assertIsNone(self.advance())
        self.assertEqual(ArtifactSelectionDecision.objects.get().artifact_id, artifact.pk)
        self.assertEqual(GenerationAttempt.objects.count(), 2)

    def test_partial_failure_does_not_mask_invalid_successful_artifact(self):
        self.start()
        self.collect(fail=True)
        artifact = self.collect()
        artifact.sha256 = "f" * 64
        artifact.save(update_fields=["sha256"])
        with self.assertRaises(ValidationError):
            self.advance()
        self.assertFalse(ArtifactSelectionDecision.objects.exists())
        self.assertEqual(GenerationAttempt.objects.count(), 2)
        self.assertEqual(AssetReferenceVersion.objects.count(), self.initial_versions)
