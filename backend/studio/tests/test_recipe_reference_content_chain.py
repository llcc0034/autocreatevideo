"""Published recipe: imported structure -> Bible -> reused images -> shot review.

All original admission, closure, file readers and decisions are exercised. No
provider, Smoke, generated candidate or downstream video/assembly is run.
"""
from copy import deepcopy
import json
from unittest.mock import patch

from django.test import TestCase, override_settings
from django.urls import reverse
from rest_framework.test import APIClient

from studio.models import (ApprovalDecision, ArtifactSelectionDecision, AssetGenerationJob,
    BibleVariantRevision, BibleVariantReview, ExecutionPreparation, GeneratedArtifact,
    GenerationAttempt, ModelDeploymentSmokeAttempt, ProductionMaterialFulfillment,
    ProductionRecipeVersion, ProductionRun, Shot, ShotReferenceUse)
from studio.services.asset_references import create_series
from studio.services.business_outputs import output_reference
from studio.services.production_recipe_material_review import material_review_context
from studio.services.production_runs import advance_production_run
from studio.services.script_revision_structures import create_structured_script_source
from studio.services.script_sources import confirm_script_revision
from studio.tests.test_asset_management_api import image_upload
from studio.tests import test_production_recipe_delivery_chain as fixtures


@override_settings(AI_PROVIDER_ALLOWED_HOSTS=("api.example.com", "cdn.example.com"))
class RecipeReferenceContentChainTests(TestCase):
    Fixture = fixtures.RecipeDeliveryChainTests
    setUpTestData = classmethod(Fixture.setUpTestData.__func__)
    profile_instance = Fixture.profile_instance
    root = Fixture.root
    receipt = Fixture.receipt
    media_instance = Fixture.media_instance

    def setUp(self):
        self.Fixture.setUp(self)
        self.external = self.enterContext(patch("requests.sessions.Session.request",
            side_effect=AssertionError("No external transport in the imported/reused chain")))
        self.actor.is_staff = True
        self.actor.save(update_fields=("is_staff",))
        self.client = APIClient()
        self.client.force_authenticate(self.actor)
        self.candidate["episodes"] = self.candidate["episodes"][:1]
        self.raw = " \r\n" + json.dumps(self.candidate, ensure_ascii=False, indent=2) + "\r\n "
        _, preview, _ = create_structured_script_source(project=self.project, user=self.actor,
            content=self.raw, output=output_reference(self.output_version), source_type="paste", max_bytes=1_000_000)
        self.revision = confirm_script_revision(preview, user=self.actor)
        media = self.media_instance()
        self.definition_value = {"schema_version": 3, "interaction_mode": "assisted", "stages": [
            {"key": "script_input", "mode": "confirmed_revision", "approval": "auto"},
            {"key": "bible_materialize", "mode": "deterministic", "approval": "auto"},
            {"key": "bible_review", "mode": "review", "approval": "required"},
            {"key": "storyboard", "mode": "deterministic", "approval": "auto"},
            {"key": "bible_image_generation", "mode": "existing_pipeline", "approval": "auto",
                "material_sources": {"primary": "existing", "variant": "existing"}},
            {"key": "bible_image_review", "mode": "review", "approval": "required"},
            {"key": "storyboard_review", "mode": "review", "approval": "required",
                "shot_reference_selection": {"source": "upstream", "source_stage_key": "bible_image_generation",
                    "selection": "manual", "allow_override": False}},
            media, {"key": "artifact_selection", "mode": "review", "approval": "required"},
            {"key": "assembly", "mode": "existing_pipeline", "approval": "auto"},
            {"key": "delivery", "mode": "existing_pipeline", "approval": "auto"}]}
        # Keep original unrelated facts; script_input legitimately registers the
        # confirmed import, but must never pretend that it was model-generated.
        self.original_artifact_ids = set(GeneratedArtifact.objects.values_list("pk", flat=True))

    def start(self, *, unattended=False):
        if unattended:
            from studio.services.production_stage_catalog import UNATTENDED_DECISION_POLICY_REGISTRY
            self.definition_value["interaction_mode"] = "unattended"
            for stage in self.definition_value["stages"]:
                stage["approval"] = "policy" if stage["mode"] == "review" else "auto"
                if stage["mode"] == "review":
                    stage["decision_policy"] = UNATTENDED_DECISION_POLICY_REGISTRY[stage["key"]]
                if stage["key"] == "storyboard_review":
                    stage["shot_reference_selection"]["selection"] = "unique_setting"
                if stage["key"] == "bible_image_generation":
                    stage["material_selection"] = {"primary": "unique_version", "variant": "unique_version"}
        response = self.client.post(reverse("production-recipe-list"), {"recipe_key": "reference-content-chain",
            "name": "Isolated imported/reused chain", "definition": self.definition_value}, format="json")
        self.assertEqual(response.status_code, 201, response.data)
        self.recipe = ProductionRecipeVersion.objects.get(pk=response.data["id"])
        response = self.client.post(reverse("production-recipe-publish", args=[self.recipe.pk]), {}, format="json")
        self.assertEqual(response.status_code, 200, response.data)
        self.recipe.refresh_from_db()
        self.assertEqual(self.recipe.status, "published")
        self.assertIsNotNone(self.recipe.published_at)
        response = self.client.post("/api/v1/production-runs/", {"project_id": str(self.project.pk),
            "recipe_version_id": str(self.recipe.pk), "plan_source": "recipe",
            "script_revision_id": str(self.revision.pk), "idempotency_key": "isolated-reference-chain",
            "allow_external_calls": False, "unattended_confirmed": unattended}, format="json")
        self.assertEqual(response.status_code, 201, response.data)
        self.run = ProductionRun.objects.get(pk=response.data["id"])
        self.url = f"/api/v1/production-runs/{self.run.pk}/"
        self.run = advance_production_run(self.run)
        if unattended:
            self.assert_stage("bible_image_generation", "running")
            self.assertEqual(self.run.blocked_code, "waiting_material_source")
            self.read_storyboard()
        else:
            self.assert_stage("bible_review", "awaiting_approval")

    def test_unrelated_invalid_video_does_not_block_recipe_creation_but_selected_corruption_does(self):
        from studio.models import ExecutionVersionContract
        from studio.tests.test_video_conditioning_catalog import VideoConditioningCatalogTests
        VideoConditioningCatalogTests.unrelated_broken_version(self, "video", "video.text_to_video")
        with patch("studio.capability_views.configured_video_conditioning_modes", side_effect=AssertionError("production configuration must not scan video")):
            response = self.client.get("/api/v1/capabilities/production/", {"project_id": str(self.project.pk)})
            self.assertEqual(response.status_code, 200, response.data)
            self.start()
        self.assertEqual(self.client.get("/api/v1/capabilities/video-conditioning/").status_code, 409)
        media = next(row for row in self.definition_value["stages"] if row["key"] == "media_generation")
        selected_id = media["routing"]["routes"][0]["execution_instance_version_id"]
        ExecutionVersionContract.objects.filter(version_id=selected_id).update(sha256="0" * 64)
        before = ProductionRun.objects.count()
        rejected = self.client.post("/api/v1/production-runs/", {"project_id": str(self.project.pk),
            "recipe_version_id": str(self.recipe.pk), "plan_source": "recipe", "script_revision_id": str(self.revision.pk),
            "idempotency_key": "corrupt-selected-route", "allow_external_calls": False}, format="json")
        self.assertEqual(rejected.status_code, 400, rejected.data)
        self.assertIn("route.expected_definition_sha256", rejected.data["error"]["details"])
        self.assertEqual(ProductionRun.objects.count(), before)

    def tearDown(self):
        self.external.assert_not_called()
        for model in (GenerationAttempt,
                AssetGenerationJob, ArtifactSelectionDecision, ModelDeploymentSmokeAttempt):
            self.assertFalse(model.objects.exists(), model.__name__)
        # The original scheduler may prepare the two downstream video units;
        # they must remain undispatched, never becoming image-generation work.
        preparations = ExecutionPreparation.objects.all()
        self.assertFalse(preparations.exclude(stage_run__production_run=self.run,
            stage_run__stage_key="media_generation").exists())
        self.assertFalse(preparations.exclude(status__in=("blocked", "awaiting_input", "awaiting_review")).exists())
        self.assertFalse(preparations.exclude(request_frozen_at=None).exists())
        imported = GeneratedArtifact.objects.get(production_run=self.run)
        self.assertEqual((imported.kind, imported.role, imported.sha256),
            ("script", "confirmed_script", self.revision.content_sha256))
        self.assertIsNone(imported.generation_attempt_id)
        self.assertIsNone(imported.generation_task_id)
        self.assertEqual(imported.manifest_metadata["script_revision_id"], str(self.revision.pk))
        self.assertEqual(set(GeneratedArtifact.objects.values_list("pk", flat=True)),
            self.original_artifact_ids | {imported.pk})

    def assert_stage(self, key, status):
        self.run.refresh_from_db()
        self.assertEqual((self.run.current_stage_key, self.run.status), (key, status), self.run.blocked_reason)

    def assert_downstream_not_dispatched(self):
        self.assert_stage("media_generation", "running")
        self.assertEqual(self.run.blocked_code, "waiting_execution_preparation")
        self.assertEqual(ExecutionPreparation.objects.filter(stage_run__production_run=self.run).count(), 2)
        self.assertFalse(GenerationAttempt.objects.exists())

    def get(self, action, params=None):
        response = self.client.get(self.url + action + "/", params or {})
        self.assertEqual(response.status_code, 200, response.data)
        return response.data

    def post(self, action, data, status=200):
        response = self.client.post(self.url + action + "/", data, format="json")
        self.assertEqual(response.status_code, status, response.data)
        return response.data

    def approve_bible(self):
        detail = self.get("bible-review")
        confirmation = {"expected_source_snapshot_sha256": detail["source_snapshot_sha256"],
            "expected_project_bible_sha256": detail["project_bible_sha256"], "reviewed_project_bible": True,
            "reviewed_candidate_ids": [row["id"] for row in detail["candidates"]],
            "reviewed_setting_keys": [row["review_key"] for row in detail["settings"]]}
        self.post("approve", {"stage_key": "bible_review", "decision": "approve", "bible_review": confirmation})
        self.assertTrue(BibleVariantReview.objects.exists())
        self.assert_stage("bible_image_generation", "running")
        self.assertEqual(self.run.blocked_code, "waiting_material_source")
        self.read_storyboard()

    def read_storyboard(self):
        self.image_stage = self.run.stages.get(stage_key="bible_image_generation")
        self.shots = list(Shot.objects.filter(episode__project=self.project).order_by("number"))
        self.assertEqual(len(self.shots), 2)
        self.assertFalse(ShotReferenceUse.objects.exclude(source_mode="").exists())

    def fulfill_images(self, *, advance=True):
        settings = list(BibleVariantRevision.objects.filter(variant__project=self.project).select_related("variant"))
        settings.sort(key=lambda row: (row.variant.appearance_role != "primary", str(row.pk)))
        series = {}
        self.fulfillments = []
        for index, setting in enumerate(settings):
            variant = setting.variant
            values = {"name": f"Isolated {index}", "role": variant.appearance_role, "bible_variant_id": str(variant.pk)}
            if setting.primary_setting_revision_id:
                values["primary_series_id"] = str(series[setting.primary_setting_revision_id].pk)
            series[setting.pk] = create_series(actor=self.actor, values=values)
            uploaded = self.client.post("/api/v1/assets/", {"name": f"Existing isolated image {index}",
                "kind": "reference", "file": image_upload()}, format="multipart")
            self.assertEqual(uploaded.status_code, 201, uploaded.data)
            context = self.get("material-fulfillment-context", {"stage_run_id": str(self.image_stage.pk),
                "unit_key": f"appearance:{variant.pk}"})
            self.assertEqual(context["setting_revision_id"], str(setting.pk))
            body = {"stage_run_id": str(self.image_stage.pk), "unit_key": f"appearance:{variant.pk}",
                "requirement_key": f"setting:{setting.pk}", "expected_run_plan_sha256": context["run_plan_sha256"],
                "series_id": str(series[setting.pk].pk), "asset_id": uploaded.data["id"], "source": "existing"}
            chosen = self.post("fulfill-material", body, 201)
            self.assertEqual(self.post("fulfill-material", body), chosen)
            self.fulfillments.append(ProductionMaterialFulfillment.objects.get(pk=chosen["id"]))
            self.assert_stage("bible_image_generation", "running")
            self.assertEqual(self.run.blocked_code, "waiting_material_source")
        if advance:
            self.post("continue", {})
            self.assert_stage("bible_image_review", "awaiting_approval")
            self.image_stage.refresh_from_db()

    def image_bindings(self):
        context = material_review_context(self.run, self.image_stage.output_snapshot["canonical"])
        return [{"entity_type": kind, "entity_id": identity, "selected_artifact_ids": [],
            "selected_library_assets": [{"asset_id": asset_id, "expected_sha256": fact["checksum"]}
                for asset_id, fact in sorted(row["assets"].items())],
            "anchor_artifact_id": None, "anchor_library_asset_id": row["primary_asset_id"]}
            for (kind, identity), row in context.items()]

    def approve_images(self):
        self.post("approve", {"stage_key": "bible_image_review", "decision": "approve",
            "expected_source_snapshot_sha256": self.image_stage.output_snapshot_sha256,
            "bible_image_bindings": self.image_bindings()})
        self.assert_stage("storyboard_review", "awaiting_approval")

    def test_required_review_continue_rejects_bad_file_without_approval_or_partial_selection(self):
        from pathlib import Path
        review_definition = next(row for row in self.definition_value["stages"] if row["key"] == "storyboard_review")
        review_definition["shot_reference_selection"]["selection"] = "unique_setting"
        self.start()
        self.approve_bible()
        self.fulfill_images()
        self.approve_images()
        use = ShotReferenceUse.objects.select_related("reference_version__asset").order_by("shot__number").last()
        path = Path(use.reference_version.asset.file.path)
        original = path.read_bytes()
        ShotReferenceUse.objects.update(source_mode="", reference_version=None, asset_id=None, asset_sha256="")
        try:
            path.write_bytes(b"isolated corrupt reference")
            self.post("continue", {}, 400)
        finally:
            path.write_bytes(original)
        self.assert_stage("storyboard_review", "awaiting_approval")
        self.assertFalse(ShotReferenceUse.objects.exclude(source_mode="").exists())
        self.assertFalse(ApprovalDecision.objects.filter(stage_run__production_run=self.run,
            stage_run__stage_key="storyboard_review").exists())
        self.assertFalse(ExecutionPreparation.objects.exists())
        self.post("continue", {})
        self.assertEqual(self.get("storyboard-reference-selection")["shot_count"], 2)
        self.assert_stage("storyboard_review", "awaiting_approval")

    def arrange_retained_source_conflict(self, *, allow_override=True, approve=True):
        review_definition = next(row for row in self.definition_value["stages"] if row["key"] == "storyboard_review")
        review_definition["shot_reference_selection"].update(selection="unique_setting", allow_override=allow_override)
        self.start()
        self.approve_bible()
        self.fulfill_images()
        later = ShotReferenceUse.objects.order_by("shot__number").last()
        original = next(f.reference_version for f in self.fulfillments
            if f.reference_version.setting_revision_id == later.setting_revision_id)
        # Isolated prior-use fixture: the file/version is intact but its saved
        # usage mode is not this round's existing-material fulfillment mode.
        ShotReferenceUse.objects.filter(pk=later.pk).update(source_mode="upload",
            reference_version=original, asset_sha256=original.asset.checksum)
        before = list(ShotReferenceUse.objects.order_by("pk").values())
        if approve:
            self.approve_images()
        return later, original, before

    def assert_image_review_blocks_conflict(self, before):
        self.post("approve", {"stage_key": "bible_image_review", "decision": "approve",
            "expected_source_snapshot_sha256": self.image_stage.output_snapshot_sha256,
            "bible_image_bindings": self.image_bindings()})
        self.assert_stage("storyboard_review", "blocked")
        self.assertEqual(self.run.blocked_code, "stage_validation_failed")
        self.assertEqual(before, list(ShotReferenceUse.objects.order_by("pk").values()))
        self.assertFalse(ApprovalDecision.objects.filter(stage_run__production_run=self.run,
            stage_run__stage_key="storyboard_review").exists())

    def test_retained_source_conflict_does_not_grant_forbidden_replacement(self):
        later, _, before = self.arrange_retained_source_conflict(allow_override=False, approve=False)
        self.assert_image_review_blocks_conflict(before)
        self.assertIn("不允许替换", self.run.blocked_reason)
        self.assertFalse(self.get("recipe-shot-references", {"shot_id": str(later.shot_id)})["editability"]["allowed"])
        self.assertFalse(ExecutionPreparation.objects.exists())

    def test_retained_source_conflict_cannot_reopen_a_prepared_shot(self):
        later, _, before = self.arrange_retained_source_conflict(approve=False)
        media = next(row for row in self.definition_value["stages"] if row["key"] == "media_generation")
        ExecutionPreparation.objects.create(stage_run=self.run.stages.get(stage_key="media_generation"),
            execution_instance_version_id=media["routing"]["routes"][0]["execution_instance_version_id"],
            purpose="production", unit_key=f"shot:{later.shot_id}", request_key="isolated-prepared-shot",
            status="awaiting_input", created_by=self.actor, intent_sha256="1" * 64)
        self.assert_image_review_blocks_conflict(before)
        self.assertIn("已有执行准备", self.run.blocked_reason)
        self.assertEqual(ExecutionPreparation.objects.count(), 1)

    def test_retained_source_conflict_does_not_hide_corrupt_selected_file(self):
        from pathlib import Path
        _, original, before = self.arrange_retained_source_conflict()
        path = Path(original.asset.file.path)
        content = path.read_bytes()
        try:
            path.write_bytes(b"isolated corrupt selected reference")
            self.post("continue", {}, 400)
        finally:
            path.write_bytes(content)
        self.assertEqual(before, list(ShotReferenceUse.objects.order_by("pk").values()))
        self.assert_stage("storyboard_review", "awaiting_approval")
        self.assertFalse(ExecutionPreparation.objects.exists())

    def test_retained_round_source_conflict_waits_for_explicit_choice_without_partial_defaults(self):
        later, original, before = self.arrange_retained_source_conflict()
        review = self.run.stages.get(stage_key="storyboard_review")
        frozen = (review.input_snapshot_sha256, self.image_stage.output_snapshot_sha256)
        self.assertEqual(self.run.blocked_code, "waiting_shot_reference_selection")
        self.assertIn("明确改选", self.run.blocked_reason)
        self.assertEqual(before, list(ShotReferenceUse.objects.order_by("pk").values()))
        detail = self.get("recipe-shot-references", {"shot_id": str(later.shot_id)})
        self.assertTrue(detail["editability"]["allowed"])
        self.assertEqual(self.client.get(self.url + "storyboard-reference-selection/").status_code, 400)
        advance_production_run(self.run, automatic=True)
        self.post("continue", {})
        self.assertEqual(before, list(ShotReferenceUse.objects.order_by("pk").values()))
        self.select_shot(later.shot)
        self.post("continue", {})
        self.assertEqual(self.get("storyboard-reference-selection")["shot_count"], 2)
        self.assert_stage("storyboard_review", "awaiting_approval")
        later.refresh_from_db(); review.refresh_from_db(); self.image_stage.refresh_from_db()
        self.assertEqual((later.reference_version_id, later.source_mode), (original.pk, "existing"))
        self.assertEqual(frozen, (review.input_snapshot_sha256, self.image_stage.output_snapshot_sha256))
        self.assertFalse(ApprovalDecision.objects.filter(stage_run=review).exists())
        self.assertFalse(ExecutionPreparation.objects.exists())

    def test_prior_blocked_source_conflict_uses_exact_original_retry_without_model_calls(self):
        later, original, before = self.arrange_retained_source_conflict()
        review = self.run.stages.get(stage_key="storyboard_review")
        frozen = (review.config_snapshot_sha256, review.input_snapshot_sha256,
            self.image_stage.output_snapshot_sha256)
        # Model the old persisted stage_validation_failed stop in this isolated
        # database. Recovery must use the original retry endpoint, not a backfill.
        review.status, review.reason_code = "blocked", "stage_validation_failed"
        review.save(update_fields=("status", "reason_code"))
        self.run.status, self.run.blocked_code = "blocked", "stage_validation_failed"
        self.run.save(update_fields=("status", "blocked_code"))
        self.post("continue", {})
        self.assert_stage("storyboard_review", "blocked")
        preview = self.get("retry-preview", {"expected_stage_id": str(review.pk)})
        self.assertEqual(preview["results"], [])
        self.post("retry", {"expected_stage_id": str(review.pk), "expected_attempt_ids": []})
        self.assert_stage("storyboard_review", "awaiting_approval")
        self.assertEqual(self.run.blocked_code, "waiting_shot_reference_selection")
        review.refresh_from_db(); self.image_stage.refresh_from_db()
        self.assertEqual(frozen, (review.config_snapshot_sha256, review.input_snapshot_sha256,
            self.image_stage.output_snapshot_sha256))
        self.assertEqual(before, list(ShotReferenceUse.objects.order_by("pk").values()))
        self.assertTrue(self.get("recipe-shot-references", {"shot_id": str(later.shot_id)})["editability"]["allowed"])
        self.assertFalse(ApprovalDecision.objects.filter(stage_run=review).exists())
        self.assertFalse(ExecutionPreparation.objects.exists())

    def select_shot(self, shot):
        detail = self.get("recipe-shot-references", {"shot_id": str(shot.pk)})
        use = ShotReferenceUse.objects.get(shot=shot)
        candidates = [row for row in detail["upstream_candidates"] if row["setting_revision_id"] == str(use.setting_revision_id)]
        self.assertEqual(len(candidates), 1)
        candidate = candidates[0]
        choice = {"use_id": str(use.pk), "source_mode": candidate["source_mode"],
            "reference_version_id": candidate["reference_version_id"], "asset_id": None,
            "asset_sha256": candidate["asset_sha256"]}
        body = {"action": "select", "stage_key": "storyboard_review", "unit_key": f"shot:{shot.pk}",
            "scope": {"kind": "shot", "id": str(shot.pk)}, "expected_run_plan_sha256": detail["run_plan_sha256"],
            "expected_selection_revision": None, "idempotency_key": f"select-{shot.pk}",
            "payload": {"expected_source_snapshot_sha256": detail["source_snapshot_sha256"],
                "expected_uses_sha256": detail["uses_sha256"], "choices": [choice]}}
        result = self.post("manual-actions", body, 201)
        self.assertEqual(self.post("manual-actions", body), result)
        self.assert_stage("storyboard_review", "awaiting_approval")
        use.refresh_from_db()
        self.assertEqual(str(use.reference_version_id), candidate["reference_version_id"])
        self.assertEqual(use.reference_version.setting_revision_id, use.setting_revision_id)
        self.assertEqual(use.source_mode, "existing")

    def test_published_import_reuse_chain_freezes_exact_uses_without_any_calls(self):
        self.start()
        self.approve_bible()
        self.fulfill_images()
        self.approve_images()
        source = self.run.stages.get(stage_key="storyboard")
        frozen_source = deepcopy(source.output_snapshot)
        review = self.run.stages.get(stage_key="storyboard_review")
        response = self.client.get(self.url + "storyboard-reference-selection/")
        self.assertIn(response.status_code, (400, 409), response.data)
        self.select_shot(self.shots[0])
        response = self.client.get(self.url + "storyboard-reference-selection/")
        self.assertIn(response.status_code, (400, 409), response.data)
        self.assertFalse(ApprovalDecision.objects.filter(stage_run=review).exists())
        self.select_shot(self.shots[1])
        observed = self.get("storyboard-reference-selection")
        self.assertEqual(observed["source_snapshot_sha256"], source.output_snapshot_sha256)
        self.assertEqual(observed["shot_count"], 2)
        self.assertEqual(ShotReferenceUse.objects.values("reference_version_id").distinct().count(), 2)
        self.post("approve", {"stage_key": "storyboard_review", "decision": "approve",
            "expected_source_snapshot_sha256": observed["source_snapshot_sha256"],
            "expected_selection_sha256": "0" * 64}, 409)
        self.assertFalse(ApprovalDecision.objects.filter(stage_run=review).exists())
        self.post("approve", {"stage_key": "storyboard_review", "decision": "approve",
            "expected_source_snapshot_sha256": observed["source_snapshot_sha256"],
            "expected_selection_sha256": observed["selection_sha256"]})
        review.refresh_from_db()
        self.assertEqual(review.status, "succeeded")
        decision = ApprovalDecision.objects.get(stage_run=review)
        self.assertEqual((decision.actor, decision.decided_by_id), ("human", self.actor.pk))
        source.refresh_from_db()
        self.assertEqual(source.output_snapshot, frozen_source)
        self.revision.refresh_from_db()
        self.assertEqual(self.revision.content, self.raw)
        self.assert_downstream_not_dispatched()
        self.assertNotEqual(self.run.status, "succeeded")
        for item in self.fulfillments:
            self.assertEqual((item.selection_mode, item.selected_by_id), ("manual", self.actor.pk))
            self.assertIsNone(item.reference_version.generated_artifact_id)
            self.assertIsNone(item.reference_version.generation_job_id)

    def test_required_review_applies_automatic_choices_without_approving_or_dispatching(self):
        from django.db import connection
        from django.test.utils import CaptureQueriesContext
        review_definition = next(row for row in self.definition_value["stages"] if row["key"] == "storyboard_review")
        review_definition["shot_reference_selection"]["selection"] = "unique_setting"
        self.start()
        self.approve_bible()
        self.fulfill_images()
        self.approve_images()
        review = self.run.stages.get(stage_key="storyboard_review")
        uses = list(ShotReferenceUse.objects.select_related("reference_version").order_by("shot__number"))
        self.assertEqual(len(uses), 2)
        self.assertTrue(all(use.reference_version_id for use in uses))
        self.assertEqual(len({use.reference_version_id for use in uses}), 2)
        for use in uses:
            self.assertEqual(use.reference_version.setting_revision_id, use.setting_revision_id)
            self.assertEqual(use.source_mode, "existing")
        with CaptureQueriesContext(connection) as queries:
            observed = self.get("storyboard-reference-selection")
        self.assertFalse([q for q in queries if q["sql"].startswith(("INSERT", "UPDATE", "DELETE"))])
        self.assertEqual(observed["shot_count"], 2)
        # Model an already-waiting review created before defaults were applied.
        # Neither GET nor a background wake may repair it as a read side effect.
        ShotReferenceUse.objects.update(source_mode="", reference_version=None, asset_id=None, asset_sha256="")
        response = self.client.get(self.url + "storyboard-reference-selection/")
        self.assertIn(response.status_code, (400, 409), response.data)
        advance_production_run(self.run, automatic=True)
        self.assertFalse(ShotReferenceUse.objects.exclude(source_mode="").exists())
        self.post("continue", {})
        self.assert_stage("storyboard_review", "awaiting_approval")
        self.assertEqual(self.get("storyboard-reference-selection")["selection_sha256"], observed["selection_sha256"])
        self.assertFalse(ApprovalDecision.objects.filter(stage_run=review).exists())
        self.assertFalse(ExecutionPreparation.objects.exists())
        with CaptureQueriesContext(connection) as queries:
            self.post("continue", {})
        self.assertFalse([q for q in queries if q["sql"].startswith("UPDATE \"studio_shotreferenceuse\"")])
        self.assert_stage("storyboard_review", "awaiting_approval")

    def test_changed_bible_setting_cannot_be_approved_as_old_upstream_material(self):
        self.start()
        self.approve_bible()
        self.fulfill_images()
        from studio.services.bible_variants import revise_variant, setting_payload
        setting = self.fulfillments[-1].reference_version.setting_revision
        before = deepcopy(self.image_stage.output_snapshot)
        bindings = self.image_bindings()
        revise_variant(actor=self.actor, variant=setting.variant, expected_revision=setting.revision,
            value={**setting_payload(setting), "appearance_anchor": "Changed and not reviewed"})
        response = self.client.post(self.url + "approve/", {"stage_key": "bible_image_review",
            "decision": "approve", "expected_source_snapshot_sha256": self.image_stage.output_snapshot_sha256,
            "bible_image_bindings": bindings}, format="json")
        self.assertIn(response.status_code, (400, 409), response.data)
        self.assertIn("变", str(response.data))
        self.assertFalse(ApprovalDecision.objects.filter(stage_run__production_run=self.run,
            stage_run__stage_key__in=("bible_image_review", "storyboard_review")).exists())
        self.image_stage.refresh_from_db()
        self.assertEqual(self.image_stage.output_snapshot, before)
        self.assertFalse(ShotReferenceUse.objects.exclude(source_mode="").exists())
        self.assertFalse(ExecutionPreparation.objects.exists())

    def test_automatic_exact_setting_selection_uses_policy_not_human_review(self):
        self.start(unattended=True)
        self.assertFalse(BibleVariantReview.objects.exists())
        original_input = deepcopy(self.image_stage.input_snapshot)
        self.assertFalse(ProductionMaterialFulfillment.objects.exists())
        self.fulfill_images(advance=False)
        # Fulfillment does not itself write shot choices or an approval.
        self.assertFalse(ShotReferenceUse.objects.exclude(source_mode="").exists())
        self.post("continue", {})
        self.assert_downstream_not_dispatched()
        self.image_stage.refresh_from_db()
        self.assertEqual(self.image_stage.input_snapshot, original_input)
        review = self.run.stages.get(stage_key="storyboard_review")
        self.assertEqual(review.status, "succeeded")
        decision = ApprovalDecision.objects.get(stage_run=review)
        self.assertEqual((decision.actor, decision.decided_by_id), ("recipe_policy", None))
        self.assertFalse(ApprovalDecision.objects.filter(stage_run__production_run=self.run, actor="human").exists())
        self.assertFalse(BibleVariantReview.objects.exists())
        uses = list(ShotReferenceUse.objects.select_related("reference_version").order_by("shot__number"))
        self.assertEqual(len(uses), 2)
        self.assertEqual(len({row.reference_version_id for row in uses}), 2)
        for use in uses:
            self.assertEqual(use.reference_version.setting_revision_id, use.setting_revision_id)
            self.assertEqual(use.source_mode, "existing")
        self.assertEqual(ProductionMaterialFulfillment.objects.count(), 3)

