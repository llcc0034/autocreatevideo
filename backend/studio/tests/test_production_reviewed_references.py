"""Original DB review -> mapping -> shared file/semantic validation, no calls."""
from copy import deepcopy
from hashlib import sha256
from io import BytesIO
from pathlib import Path
from tempfile import TemporaryDirectory
from unittest.mock import patch
from uuid import uuid4

from PIL import Image
from django.contrib.auth import get_user_model
from django.core.exceptions import ValidationError
from django.core.files.base import ContentFile
from django.test import TestCase
from rest_framework.test import APIClient

from studio.models import (ApprovalDecision, Asset, Episode, ExecutionPreparation, GenerationAttempt,
    ProductionRecipeVersion, ProductionRun, SeriesProject, Shot, ShotReferenceUse, StageRun)
from studio.services.execution_input_resolution import resolve_execution_input_sources
from studio.services.json_contracts import assign_json_contract, json_contract
from studio.services.production_run_plans import freeze_run_execution_plan, get_run_execution_plan
from studio.services.production_runs import _formal_snapshot, _save_stage_snapshot
from studio.services.production_reviewed_references import read_reviewed_shot_references
from studio.services.production_recipe_media_preparations import resolve_recipe_media_sources
from studio.services.production_stage_catalog import production_stage_catalog
from studio.tests.test_execution_context_inputs import PROJECT_CONFIG
from studio.tests.test_production_reference_mapping import mapping, spec


class ReviewedReferenceTests(TestCase):
    def setUp(self):
        self.actor = get_user_model().objects.create_user(username="review-owner")
        self.other = get_user_model().objects.create_user(username="review-other")
        self.client = APIClient(); self.client.force_authenticate(self.actor)
        self.project = SeriesProject.objects.create(owner=self.actor, title="Mapping project", **PROJECT_CONFIG)
        self.episode = Episode.objects.create(project=self.project, number=1, title="Reviewed episode")
        self.outside = Episode.objects.create(project=self.project, number=2, title="Not selected")
        self.shot = Shot.objects.create(episode=self.episode, number=1, title="Review target", duration_ms=1000,
            media_type="image", include_audio=False)
        temp = TemporaryDirectory(); self.addCleanup(temp.cleanup)
        self.enterContext(self.settings(MEDIA_ROOT=temp.name, ASSET_LIBRARY_ROOT=temp.name))
        image = BytesIO(); Image.new("RGB", (12, 18), "red").save(image, format="PNG")
        data = image.getvalue()
        self.asset = Asset(owner=self.actor, kind="reference", name="Reviewed frame", mime_type="image/png", checksum=sha256(data).hexdigest())
        self.asset.file.save("review-frame.png", ContentFile(data))
        self.use = ShotReferenceUse.objects.create(shot=self.shot, use_key="appearance", position=0,
            media_kind="image", purpose="configured_style", source_mode="existing", asset=self.asset,
            asset_sha256=self.asset.checksum)
        recipe = ProductionRecipeVersion(recipe_key="mapping-fixture", version=1, name="Storage fixture", status="published")
        assign_json_contract(recipe, "definition", {"schema_version": 3, "interaction_mode": "assisted", "stages": [
            {"key": "storyboard_review", "mode": "review", "approval": "required"},
            {"key": "media_generation", "mode": "existing_pipeline", "approval": "required"}]}, schema_version=3)
        recipe.save()
        self.run = ProductionRun(owner=self.actor, project=self.project, recipe_version=recipe, plan_source="recipe",
            status="running", idempotency_key=str(uuid4()), request_sha256="a" * 64)
        for field in ("input_snapshot", "config_snapshot"):
            assign_json_contract(self.run, field, {"schema_version": 1})
        self.run.save()
        catalog = production_stage_catalog()
        freeze_run_execution_plan(run=self.run, scope={"kind": "episodes", "episode_ids": [str(self.episode.pk)]},
            delivery={"kind": "episode_masters"}, initial_content=None, stage_catalog_key=catalog.value["key"],
            expected_stage_catalog_sha256=catalog.sha256, actor=self.actor)
        self.review = StageRun.objects.create(production_run=self.run, stage_key="storyboard_review", ordinal=1,
            mode="review", status="succeeded", idempotency_key=str(uuid4()))
        self.stage = StageRun.objects.create(production_run=self.run, stage_key="media_generation", ordinal=2,
            mode="existing_pipeline", status="running", idempotency_key=str(uuid4()))
        for target in ("studio.services.provider_registry._request_json", "studio.services.comfyui.ComfyUIClient",
                "studio.services.execution_parameter_preparation.randbelow"):
            self.enterContext(patch(target, side_effect=AssertionError("Mapping may not invoke model or allocate seed")))
        self.approve()

    def approve(self):
        _save_stage_snapshot(self.review, "output_snapshot", _formal_snapshot(self.run, self.review, "storyboard:" + str(self.episode.pk)))
        self.decision = ApprovalDecision.objects.create(stage_run=self.review, revision=self.review.approval_decisions.count() + 1, decision="approve",
            actor="human", decided_by=self.actor, output_snapshot_sha256=self.review.output_snapshot_sha256)

    def read(self):
        return read_reviewed_shot_references(stage=self.stage, shot_id=str(self.shot.pk))

    def test_exact_review_and_actual_asset_reach_shared_normalizer(self):
        source = self.read()
        self.assertEqual(source["records"][0]["source"], {"kind": "asset", "asset_id": str(self.asset.pk)})
        self.assertEqual(source["review"]["decision_id"], str(self.decision.pk))
        input_spec = spec(); input_spec["semantic_contract"].update(schema_version=1, allow_repeated_asset=False)
        input_spec["targets"] = [{"target_kind": "workflow_slot", "target_key": "image", "is_primary": True}]
        definition = json_contract({"schema_version": 1, "configuration": {"inputs": [input_spec]},
            "resources": {"conditioning_mode": None}, "interface": {"input_targets": {"image": {
                "target_kind": "workflow_slot", "target_key": "image", "kind": "image", "min_items": 0, "max_items": 4,
                "wiring": {"kind": "semantic_group", "semantic_keys": ["ref.one", "ref.two", "ref.three", "ref.four"]}}}}})
        bound = resolve_recipe_media_sources(definition=definition, route={"input_sources": {"images": mapping("images")}},
            stage=self.stage, plan=get_run_execution_plan(self.run), scope={"kind": "shot", "id": str(self.shot.pk)})
        resolved = resolve_execution_input_sources(definition, bound["inputs"], owner=self.actor, subject_facts=[],
            max_media_bytes=1024*1024, project_id=self.project.pk, production_run_id=str(self.run.pk),
            eligible_stage_ids=bound["eligible_stage_ids"])
        self.assertEqual(resolved.value["logical_values"]["images"][0]["asset_id"], str(self.asset.pk))
        self.assertIsNone(resolved.value["logical_values"]["images"][0]["purpose"])
        self.assertEqual(source["records"][0]["reference_key"], "shot-use:appearance")
        self.assertEqual(source["records"][0]["purpose"], "configured_style")
        self.assertEqual(bound["review"], source["review"])
        self.assertFalse(ExecutionPreparation.objects.exists()); self.assertFalse(GenerationAttempt.objects.exists())

    def test_changed_references_or_creative_content_reject_whole_review(self):
        for target, field, value in ((self.shot, "narrative", "Changed content"), (self.use, "purpose", "New authored purpose")):
            old = deepcopy(getattr(target, field)); setattr(target, field, value); target.save(update_fields=(field,))
            with self.subTest(field=field), self.assertRaisesMessage(ValidationError, "已变更"):
                self.read()
            setattr(target, field, old); target.save(update_fields=(field,))

    def test_content_free_legacy_metadata_does_not_change_reviewed_uses(self):
        original = self.read()
        self.shot.metadata = {"reference_bindings": {"first_frame_asset_id": str(uuid4())}}
        self.shot.save(update_fields=("metadata",))
        self.assertEqual(self.read(), original)

    def test_exact_use_changes_and_missing_selection_never_fall_back_to_old_pool(self):
        ShotReferenceUse.objects.filter(pk=self.use.pk).update(purpose="new-purpose")
        with self.assertRaisesMessage(ValidationError, "已变更"):
            self.read()
        self.approve()
        self.assertEqual(self.read()["records"][0]["purpose"], "new-purpose")
        ShotReferenceUse.objects.filter(pk=self.use.pk).update(asset=None, source_mode="", asset_sha256="")
        self.approve()
        with self.assertRaisesMessage(ValidationError, "尚未选择确切素材"):
            self.read()
        self.use.delete(); self.approve()
        self.assertEqual(self.read()["records"], [])

    def test_original_review_is_not_rewritten_when_selected_bytes_change(self):
        snapshot = deepcopy(self.review.output_snapshot)
        Path(self.asset.file.path).write_bytes(b"changed isolated fixture")
        with self.assertRaises(ValidationError):
            self.read()
        self.review.refresh_from_db()
        self.assertEqual(self.review.output_snapshot, snapshot)
        self.assertFalse(GenerationAttempt.objects.exists())

    def test_exact_version_stays_selected_when_series_gains_a_newer_version(self):
        from studio.models import AssetReferenceSeries
        from studio.services.asset_references import attach_asset
        series = AssetReferenceSeries.objects.create(owner=self.actor, name="Isolated source series", role="primary")
        version = attach_asset(actor=self.actor, series_id=series.pk, asset_id=self.asset.pk, source="existing")[0]
        ShotReferenceUse.objects.filter(pk=self.use.pk).update(asset=None, reference_version=version)
        self.approve()
        before = self.read()
        attach_asset(actor=self.actor, series_id=series.pk, asset_id=self.asset.pk, source="existing")
        self.assertEqual(self.read(), before)
        canonical = self.review.output_snapshot["canonical"]["episodes"][0]["shots"][0]
        self.assertEqual(canonical["reference_uses"][0]["reference_version_id"], str(version.pk))
        self.assertEqual(canonical["reference_uses"][0]["asset_sha256"], self.asset.checksum)

    def test_image_shot_edit_keeps_reference_facts_without_a_video_mode(self):
        from studio.serializers import ShotSerializer
        from rest_framework.request import Request
        from rest_framework.test import APIRequestFactory, force_authenticate
        request = APIRequestFactory().patch("/shots/", {"title": "Renamed"}, format="json")
        force_authenticate(request, user=self.actor)
        serializer = ShotSerializer(self.shot, data={"title": "Renamed"}, partial=True, context={"request": Request(request)})
        self.assertTrue(serializer.is_valid(), serializer.errors)
        serializer.save(); self.shot.refresh_from_db()
        self.assertEqual(self.shot.conditioning_mode, "")
        self.use.refresh_from_db()
        self.assertEqual(self.use.asset_id, self.asset.pk)

    def test_missing_rejected_and_wrong_hash_decisions_reject(self):
        for change in ({"decision": "reject"}, {"output_snapshot_sha256": "0" * 64}):
            ApprovalDecision.objects.filter(pk=self.decision.pk).update(**change)
            with self.assertRaisesMessage(ValidationError, "不一致"):
                self.read()
            ApprovalDecision.objects.filter(pk=self.decision.pk).update(decision="approve", output_snapshot_sha256=self.review.output_snapshot_sha256)
        self.decision.delete()
        with self.assertRaises(ValidationError): self.read()

    def test_review_must_precede_current_unit_and_be_in_frozen_scope(self):
        StageRun.objects.filter(pk=self.review.pk).update(ordinal=3)
        with self.assertRaisesMessage(ValidationError, "冻结运行计划"):
            self.read()
        StageRun.objects.filter(pk=self.review.pk).update(ordinal=1)
        self.shot.episode = self.outside; self.shot.save(update_fields=("episode",))
        with self.assertRaisesMessage(ValidationError, "冻结剧集范围"):
            self.read()

    def test_targets_api_is_paginated_scoped_and_owner_only_for_recipe(self):
        url = f"/api/v1/production-runs/{self.run.pk}/targets/"
        response = self.client.get(url, {"kind": "shot", "page_size": 1})
        self.assertEqual(response.status_code, 200, response.data)
        self.assertEqual(response.data["count"], 1)
        self.assertEqual(response.data["results"][0]["scope"]["id"], str(self.shot.pk))
        self.assertEqual(response["Cache-Control"], "private, no-store")
        self.client.force_authenticate(self.other)
        self.assertEqual(self.client.get(url, {"kind": "shot"}).status_code, 404)

    def test_preview_rejects_missing_route_forged_fields_and_cross_run_stage_without_writes(self):
        url = f"/api/v1/production-runs/{self.run.pk}/reference-mapping-preview/"
        request = {"stage_run_id": str(self.stage.pk), "shot_id": str(self.shot.pk),
            "expected_run_plan_sha256": get_run_execution_plan(self.run).sha256}
        for change in ({}, {"records": []}, {"stage_run_id": str(uuid4())}, {"expected_run_plan_sha256": "0" * 64}):
            response = self.client.post(url, {**request, **change}, format="json")
            self.assertEqual(response.status_code, 400, response.data)
        self.client.force_authenticate(self.other)
        self.assertEqual(self.client.post(url, request, format="json").status_code, 404)
        self.assertEqual(ApprovalDecision.objects.count(), 1)
        self.assertFalse(ExecutionPreparation.objects.exists()); self.assertFalse(GenerationAttempt.objects.exists())
