"""M3 -> mixed material stage -> original Comfy queue/archive/Asset versions.

Real isolated PNGs and original writers; admission and Comfy transport are
controlled peers, not live Smoke, model quality, or business adoption evidence.
"""
from copy import deepcopy
from hashlib import sha256
from pathlib import Path
from types import SimpleNamespace
from unittest.mock import Mock, patch

from PIL import Image
from django.conf import settings
from django.db import connection, transaction
from django.test import override_settings
from django.test.utils import CaptureQueriesContext
from django.utils import timezone
from rest_framework.test import APIClient

from studio.models import (Asset, AssetGenerationJob, AssetReferenceSeries, AssetReferenceVersion, ArtifactSelectionDecision,
    ExecutionPreparation, GeneratedArtifact, GenerationAttempt)
from studio.services.asset_references import read_generated_material
from studio.services.execution_production_media import read_prepared_production_media
from studio.services.media_generation_targets import read_media_generation_target
from studio.services.production_recipe_material_registration import register_recipe_material_selection
from studio.services.production_recipe_media_stages import advance_recipe_media_stage
from studio.services.production_recipe_media_preparations import prepare_recipe_media_unit
from studio.services.production_runs import _StoredAssetFileGuard, _canonical_bible_image_candidates
from studio.services.task_queue import claim_next_task, process_task
from studio.tests import test_recipe_material_inputs as fixtures
from studio.tests.test_data_structures import StructureFixture
from studio.tests.test_execution_production_auxiliaries import production_observations
from studio.tests import test_execution_reference_preparation as field_fixtures
from studio.tests.test_execution_workflow_staging import RecordingUploadClient


@override_settings(AI_PROVIDER_ALLOWED_HOSTS=("127.0.0.1", "api.example.com"))
class RecipeMixedMaterialStageTests(StructureFixture):
    create_job = fixtures.RecipeMaterialInputTests.create_job
    primary_version = fixtures.RecipeMaterialInputTests.primary_version
    write = fixtures.RecipeMaterialInputTests.write
    input = staticmethod(fixtures.RecipeMaterialInputTests.input)
    configure_variant_sources = fixtures.RecipeMaterialInputTests.configure_variant_sources
    # This published fixture Profile requires English direct text. Use authored
    # English settings, not a translation or a weakened compiler validator.
    variant_anchors = ("Wear a blue coat.", "Wear a red coat while preserving the same character.")
    select = field_fixtures.ExecutionReferencePreparationTests.select

    def configure_declaration(self, declaration):
        declaration["routing"] = deepcopy(declaration["routing"])
        declaration["routing"]["evaluator"] = "first_succeeded"
        if self._testMethodName == "test_public_primary_change_rechecks_dependencies_created_after_preview":
            declaration["routing"]["variant_count"] = 2
        if self._testMethodName == "test_explicit_append_keep_primary_preserves_completed_variant_basis":
            # Author an appendable primary route before RunPlan freeze; the
            # usual fixture deliberately locks its seed and forbids new draws.
            for rule in declaration["routing"]["routes"][0]["parameter_policies"]:
                if rule["key"] == "sampling.seed":
                    rule["policy"] = "default"
        declaration["variant_routing"]["variant_count"] = 1
        # All possible routes use authored markers, including fallback/choice.
        declaration["variant_routing"]["routes"][0]["input_sources"] = deepcopy(
            declaration["variant_routing"]["routes"][1]["input_sources"])

    def setUp(self):
        if self._testMethodName == "test_explicit_append_keep_primary_preserves_completed_variant_basis":
            # Imported before publication: automatic draws need an authored,
            # finite domain as well as an unlocked route policy.
            self.workflow_parameter_constraints = {"sampling.seed": {"min": 0, "max": 99}}
        fixtures.RecipeMaterialInputTests.setUp(self)
        self.client = APIClient()
        self.user.is_staff = True
        self.user.save(update_fields=("is_staff",))
        self.output_root = Path(settings.MEDIA_ROOT) / "isolated-comfy-output"
        override = override_settings(COMFYUI_OUTPUT_DIR=str(self.output_root))
        override.enable()
        self.addCleanup(override.disable)
        for path in ("execution_production_admission", "execution_production_dispatch"):
            mocked = patch(f"studio.services.{path}.collect_production_execution_admission", side_effect=production_observations)
            mocked.start()
            self.addCleanup(mocked.stop)
        self.initial_versions = AssetReferenceVersion.objects.count()
        self.initial_anchor = self.character.anchor_asset_id
        self.initial_references = list(self.character.reference_assets.values_list("pk", flat=True))
        self.filled = set()
        # The isolated Run authorizes calls from creation of these requests;
        # transport remains blocked by the test's external-HTTP guard.
        self.allow_calls(True)

    def tearDown(self):
        self.external.assert_not_called()
        self.assertFalse(AssetGenerationJob.objects.exists())
        self.character.refresh_from_db()
        self.assertEqual(self.character.anchor_asset_id, self.initial_anchor)
        self.assertEqual(list(self.character.reference_assets.values_list("pk", flat=True)), self.initial_references)

    def allow_calls(self, allowed):
        self.run.allow_external_calls = allowed
        self.run.save(update_fields=("allow_external_calls",))

    def advance(self, *, callbacks=True, supply_fields=False):
        self.run.refresh_from_db()
        self.stage.refresh_from_db()
        def prepare(**kwargs):
            preparation = prepare_recipe_media_unit(**kwargs)
            if supply_fields and preparation.pk not in self.filled:
                # Original manual-field actions supply this fixture's Profile
                # text; no auxiliary model response or human audit is invented.
                self.fill_fields(preparation)
                preparation.refresh_from_db()
            return preparation
        with patch("studio.services.production_run_auto_advance.schedule_production_run_advance"), patch(
                "studio.services.production_recipe_media_stages.prepare_recipe_media_unit", side_effect=prepare):
            with self.captureOnCommitCallbacks(execute=callbacks):
                result = advance_recipe_media_stage(self.run, self.stage)
        return result

    def fill_fields(self, preparation):
        self.prep = preparation
        self.select("first", "Keep the parcel beside the blue box.")
        self.select("second", "Open the parcel while preserving the supplied shape.")
        self.filled.add(preparation.pk)

    def collect(self, *, fail=False, color="green"):
        task = claim_next_task()
        self.assertIsNotNone(task)
        attempt = GenerationAttempt.objects.get(generation_task=task)
        media = read_prepared_production_media(attempt)
        filename = f"{attempt.preparation_id}_00001_.png"
        output = self.output_root / "autoprompt" / "execution" / filename
        output.parent.mkdir(parents=True, exist_ok=True)
        parameters = media["parameters"]
        Image.new("RGB", (parameters["image.width"], parameters["image.height"]), color).save(output)
        receipt = {"node_id": media["output_contract"]["primary"]["node_id"], "filename": filename,
            "subfolder": "autoprompt/execution", "type": "output"}
        peer = RecordingUploadClient()
        now = timezone.now().isoformat()
        peer.release_resources_before_submit = Mock(return_value={"schema_version": 1, "status": "succeeded",
            "policy_sha256": "d" * 64, "unload_models": True, "free_memory": True, "attempted_at": now,
            "completed_at": now, "error_code": "", "http_status": 200})
        peer.queue_prompt = Mock(side_effect=lambda graph, client_id, *, prompt_id: SimpleNamespace(prompt_id=prompt_id, node_errors={}))
        peer.extract_outputs = lambda _: [receipt]
        with patch("studio.services.task_queue.preflight_comfyui_workflow", return_value=SimpleNamespace(
                as_dict=lambda: {"node_count": 8, "link_count": 7, "class_types": ["IsolatedPeer"], "schema_sha256": "c" * 64})), patch(
                "studio.services.task_queue._wait_for_task_completion", return_value={"status": {
                    "status_str": "error" if fail else "success", "completed": not fail}}), patch(
                "studio.services.task_distribution_recovery.prepare_all_task_distributions", return_value=[]), patch(
                "studio.services.task_queue.schedule_generation_task_run_advance"):
            result = process_task(task, peer)
        self.assertEqual(result.status, "failed" if fail else "succeeded", result.error_message)
        peer.queue_prompt.assert_called_once()
        attempt.refresh_from_db()
        self.assertIsNone(attempt.actual_cost)
        if fail:
            self.assertFalse(GeneratedArtifact.objects.filter(generation_attempt=attempt).exists())
            return None
        artifact = GeneratedArtifact.objects.get(generation_attempt=attempt)
        self.assertEqual(artifact.sha256, sha256(output.read_bytes()).hexdigest())
        target = read_media_generation_target(attempt.preparation).value
        if target["appearance"]["role"] == "variant":
            self.assertNotIn("bible_target", media)
            graph = peer.queue_prompt.call_args.args[0]
            self.assertEqual(sha256(peer.files[graph["11"]["inputs"]["image"]]).hexdigest(), target["appearance"]["primary"]["sha256"])
            setting = next(row for row in self.settings if str(row.pk) == target["setting"]["setting_revision_id"])
            self.assertIn(setting.appearance_anchor, graph["2"]["inputs"]["text"])
        return artifact

    def start_primary(self):
        self.assertIsNone(self.advance(supply_fields=True))
        preparations = list(ExecutionPreparation.objects.filter(stage_run=self.stage))
        self.assertEqual(len(preparations), 1)
        self.assertEqual(GenerationAttempt.objects.count(), 1, list(ExecutionPreparation.objects.filter(
            stage_run=self.stage).values("status", "reason_code", "reason", "contract__evaluation")))

    def test_public_primary_change_rechecks_dependencies_created_after_preview(self):
        self.client.force_authenticate(self.user)
        self.advance(supply_fields=True)
        first, second = self.collect(), self.collect()
        self.advance()
        decision = ArtifactSelectionDecision.objects.get()
        alternative = second if decision.artifact_id == first.pk else first
        url = f"/api/v1/production-runs/{self.run.pk}/"
        context = self.client.get(url + "material-selection-context/", {"artifact_id": str(alternative.pk)})
        self.assertEqual(context.status_code, 200, context.data)
        self.assertTrue(context.data["can_select"])
        self.advance()  # Original preparation may start configured auxiliaries, not image main calls.
        preparations = list(ExecutionPreparation.objects.filter(stage_run=self.stage).values(
            "id", "intent_sha256", "request_sha256", "status"))
        attempts = list(GenerationAttempt.objects.values("id", "preparation_id", "role", "status"))
        result = self.client.post(url + "select-artifact/", {"artifact_id": str(alternative.pk),
            "material_selection": {"expected_decision_id": str(decision.pk)}}, format="json", HTTP_IDEMPOTENCY_KEY="change-primary")
        self.assertEqual(result.status_code, 409, result.data)
        self.assertIn("material_dependency_change_required", str(result.data))
        context = self.client.get(url + "material-selection-context/", {"artifact_id": str(alternative.pk)})
        self.assertEqual(context.status_code, 200, context.data)
        self.assertTrue(context.data["can_select"], context.data)
        self.assertEqual({row["action"] for row in context.data["dependent_preparations"]}, {"reprepare"})
        self.assertEqual(len(context.data["dependent_preparations"]), 2)
        self.assertEqual({row["reference_name"] for row in context.data["dependent_preparations"]},
            {row.reference_name for row in self.settings})
        self.assertEqual(list(ExecutionPreparation.objects.filter(stage_run=self.stage).values(
            "id", "intent_sha256", "request_sha256", "status")), preparations)
        self.assertEqual(list(GenerationAttempt.objects.values("id", "preparation_id", "role", "status")), attempts)
        self.assertEqual(ArtifactSelectionDecision.objects.get().pk, decision.pk)

    def primary_ready(self):
        self.start_primary()
        artifact = self.collect()
        self.assertIsNone(self.advance())
        self.assertEqual(self.stage.reason_code, "waiting_material_registration")
        self.assertEqual(ExecutionPreparation.objects.filter(stage_run=self.stage).count(), 1)
        artifact.refresh_from_db()
        return read_generated_material(artifact=artifact)

    def start_variants(self):
        primary = self.primary_ready()
        self.assertIsNone(self.advance(supply_fields=True))
        variants = list(ExecutionPreparation.objects.filter(stage_run=self.stage).exclude(pk=primary.generated_artifact.generation_attempt.preparation_id))
        self.assertEqual(len(variants), 2)
        for preparation in variants:
            self.assertEqual(read_media_generation_target(preparation).value["appearance"]["primary"]["version_id"], str(primary.pk))
        self.allow_calls(True)
        self.assertIsNone(self.advance())
        self.assertEqual(GenerationAttempt.objects.count(), 3)
        return primary, variants

    def test_primary_then_sibling_variants_share_original_queue_and_register_exact_basis(self):
        primary, variants = self.start_variants()
        first, second = self.collect(), self.collect()
        self.assertIsNone(self.advance())
        self.assertEqual(self.stage.reason_code, "waiting_material_registration")
        for artifact in (first, second):
            artifact.refresh_from_db()
            version = read_generated_material(artifact=artifact)
            self.assertEqual(version.generated_from_id, primary.pk)
            self.assertEqual(version.series.primary_series_id, primary.series_id)
            self.assertEqual(version.series.role, "variant")
            self.assertIsNone(version.generation_job_id)
        self.assertEqual(AssetReferenceVersion.objects.count(), self.initial_versions + 3)
        self.assertEqual(ArtifactSelectionDecision.objects.filter(selection_mode="automatic", selected_by__isnull=True).count(), 3)
        versions = [primary, read_generated_material(artifact=first), read_generated_material(artifact=second)]
        self.assertEqual(len({version.asset_id for version in versions}), 3)
        assets_before = list(Asset.objects.order_by("pk").values())
        series_before = list(AssetReferenceSeries.objects.order_by("pk").values())
        versions_before = list(AssetReferenceVersion.objects.order_by("pk").values())
        files_before = {str(path.relative_to(settings.MEDIA_ROOT)): sha256(path.read_bytes()).hexdigest()
            for path in Path(settings.MEDIA_ROOT).rglob("*") if path.is_file()}
        with patch("django.db.models.fields.files.FieldFile.save", side_effect=AssertionError("No new copy")):
            # First archive -> Asset materialization above may copy once. Replay
            # must reuse that Asset and the same version, not a shadow relation.
            for _ in range(2):
                for version in versions:
                    artifact = version.generated_artifact
                    decision = ArtifactSelectionDecision.objects.get(artifact=artifact)
                    with _StoredAssetFileGuard() as guard, transaction.atomic():
                        repeated = register_recipe_material_selection(stage_run_id=self.stage.pk,
                            unit_key=artifact.generation_attempt.unit_key, decision_id=decision.pk,
                            actor=self.user, asset_guard=guard)
                    self.assertEqual((repeated.pk, repeated.asset_id), (version.pk, version.asset_id))
            self.assertEqual(self.advance(), "generated-artifacts:3")
            self.assertEqual(self.advance(), "generated-artifacts:3")
            self.client.force_authenticate(self.user)
            with CaptureQueriesContext(connection) as queries:
                for version in versions:
                    artifact = version.generated_artifact
                    detail = self.client.get(f"/api/v1/asset-reference-versions/{version.pk}/")
                    self.assertEqual(detail.status_code, 200, detail.data)
                    self.assertEqual(detail.data["id"], str(version.pk))
                    self.assertEqual(detail.data["asset"]["id"], str(artifact.asset_id))
                    self.assertEqual(detail.data["source"], "generated")
                    self.assertEqual(detail.data["setting_revision_id"], str(version.setting_revision_id))
                    self.assertEqual(detail.data["generated_from_id"],
                        None if version == primary else str(primary.pk))
                    self.assertIsNone(detail.data["generation_job"])
                    self.assertEqual(detail.data["generated_artifact"], {
                        "id": str(artifact.pk), "status": artifact.status,
                        "production_run_id": str(self.run.pk),
                        "generation_attempt_id": str(artifact.generation_attempt_id)})
                    self.assertIn(detail.data["generated_artifact"]["status"], ("available", "selected"))
                    series = self.client.get(f"/api/v1/asset-reference-series/{version.series_id}/")
                    self.assertEqual(series.status_code, 200, series.data)
                    expected_versions = sorted((row for row in versions_before if row["series_id"] == version.series_id),
                        key=lambda row: -row["version"])
                    # The primary fixture already has an explicitly uploaded
                    # version. Keep it alongside the generated one, not a
                    # fabricated single-version series or duplicate origin.
                    self.assertEqual(series.data["version_count"], len(expected_versions))
                    self.assertEqual(series.data["primary_series_id"],
                        None if version == primary else str(primary.series_id))
                    self.assertEqual(series.data["role"], "primary" if version == primary else "variant")
                    listing = self.client.get("/api/v1/asset-reference-versions/", {"series_id": str(version.series_id)})
                    self.assertEqual(listing.status_code, 200, listing.data)
                    self.assertEqual([row["id"] for row in listing.data["results"]],
                        [str(row["id"]) for row in expected_versions])
                    self.assertEqual([row for row in listing.data["results"] if row["id"] == str(version.pk)], [detail.data])
            self.assertFalse(any(q["sql"].lstrip().upper().startswith(("INSERT", "UPDATE", "DELETE")) for q in queries))
        self.assertEqual(list(Asset.objects.order_by("pk").values()), assets_before)
        self.assertEqual(list(AssetReferenceSeries.objects.order_by("pk").values()), series_before)
        self.assertEqual(list(AssetReferenceVersion.objects.order_by("pk").values()), versions_before)
        self.assertEqual({str(path.relative_to(settings.MEDIA_ROOT)): sha256(path.read_bytes()).hexdigest()
            for path in Path(settings.MEDIA_ROOT).rglob("*") if path.is_file()}, files_before)
        self.assertEqual(GenerationAttempt.objects.count(), 3)
        catalog = _canonical_bible_image_candidates(self.run, self.stage)
        self.assertEqual([candidate["artifact_id"] for row in catalog["entities"] for candidate in row["candidates"]], [str(primary.generated_artifact_id)])
        self.client.force_authenticate(self.user)
        history = self.client.get(f"/api/v1/projects/{self.project.pk}/bible-image-candidates/")
        self.assertEqual(history.status_code, 200, history.data)
        self.assertEqual([candidate["artifact_id"] for row in history.json()["candidates"]
            for candidate in row["candidate_history"]], [str(primary.generated_artifact_id)])
        with patch("studio.services.production_recipe_material_registration.read_registered_recipe_primary_dependency",
                side_effect=AssertionError("Historical basis is not today's selection")):
            self.assertEqual(read_generated_material(artifact=first).generated_from_id, primary.pk)
            self.assertNotIn("bible_target", read_prepared_production_media(first.generation_attempt))

    def test_failed_primary_never_allocates_or_dispatches_variant(self):
        self.start_primary()
        self.collect(fail=True)
        self.assertIsNone(self.advance())
        self.assertEqual(self.stage.reason_code, "waiting_material_recovery")
        self.assertEqual(ExecutionPreparation.objects.filter(stage_run=self.stage).count(), 1)
        self.assertEqual(AssetReferenceVersion.objects.count(), self.initial_versions)

    def test_explicit_append_keep_primary_preserves_completed_variant_basis(self):
        from uuid import uuid4
        from studio.models import StageRun
        from studio.services.production_recipe_material_selection import recipe_material_selection_context
        from studio.services.production_runs import (
            _formal_snapshot, _save_stage_snapshot, regenerate_bible_image_candidates, select_run_artifact,
        )
        seed_rule = self.definition.value["interface"]["parameters"]["sampling.seed"]["rule"]
        self.assertEqual((seed_rule["minimum"], seed_rule["maximum"]), (0, 99))
        primary, variants = self.start_variants()
        self.collect()
        self.collect()
        self.assertIsNone(self.advance())
        result = self.advance()
        self.assertEqual(result, "generated-artifacts:3")
        old_contracts = {row.pk: deepcopy(row.contract.resolved_inputs) for row in variants}
        old_versions = list(AssetReferenceVersion.objects.order_by("pk").values())
        _save_stage_snapshot(self.stage, "output_snapshot", _formal_snapshot(self.run, self.stage, result))
        self.stage.status = "succeeded"
        self.stage.save(update_fields=["status"])
        StageRun.objects.create(production_run=self.run, stage_key="bible_image_review", ordinal=3,
            mode="review", status="awaiting_approval", idempotency_key=str(uuid4()))
        self.run.status, self.run.current_stage_key = "awaiting_approval", "bible_image_review"
        self.run.save(update_fields=["status", "current_stage_key"])
        with patch("studio.services.production_runs.advance_production_run", side_effect=lambda run: run):
            regenerate_bible_image_candidates(self.run, request_id=uuid4(),
                expected_source_snapshot_sha256=self.stage.output_snapshot_sha256,
                entities=[{"entity_type": "character", "entity_id": str(self.character.pk)}], user=self.user)
        self.assertIsNone(self.advance(supply_fields=True))
        self.collect()
        self.assertIsNone(self.advance())
        self.assertEqual(self.stage.reason_code, "waiting_material_selection")
        artifact = primary.generated_artifact
        context = recipe_material_selection_context(artifact=artifact, actor=self.user)
        self.assertTrue(context["can_select"], context)
        self.assertEqual([row["action"] for row in context["dependent_preparations"]], ["retain", "retain"])
        select_run_artifact(self.run, artifact, user=self.user, material_selection={
            "expected_decision_id": context["expected_decision_id"],
            "expected_dependencies_sha256": context["dependencies_sha256"]})
        self.assertEqual(self.advance(), "generated-artifacts:4")
        self.assertEqual(self.advance(), "generated-artifacts:4")
        _formal_snapshot(self.run, self.stage, "generated-artifacts:4")
        self.assertEqual(list(AssetReferenceVersion.objects.order_by("pk").values()), old_versions)
        for row in variants:
            row.contract.refresh_from_db()
            self.assertEqual(row.contract.resolved_inputs, old_contracts[row.pk])
        self.assertEqual(ExecutionPreparation.objects.filter(stage_run=self.stage).count(), 4)
        self.assertEqual(GenerationAttempt.objects.count(), 4)
        self.assertEqual(ArtifactSelectionDecision.objects.filter(selection_mode="manual").count(), 1)

    def test_lost_registration_callback_recovers_before_any_variant_preparation(self):
        self.start_primary()
        self.collect()
        self.assertIsNone(self.advance(callbacks=False))
        self.assertIsNone(self.advance(callbacks=False))
        self.assertEqual(AssetReferenceVersion.objects.count(), self.initial_versions)
        self.assertEqual(ExecutionPreparation.objects.filter(stage_run=self.stage).count(), 1)
        self.assertIsNone(self.advance())
        self.assertEqual(AssetReferenceVersion.objects.count(), self.initial_versions + 1)
        self.assertEqual(GenerationAttempt.objects.count(), 1)

    def test_variant_registration_rollback_preserves_archive_and_primary(self):
        primary, _ = self.start_variants()
        artifact = self.collect()
        self.client.force_authenticate(self.user)
        url = f"/api/v1/production-runs/{self.run.pk}/"
        context = self.client.get(url + "material-selection-context/", {"artifact_id": str(artifact.pk)})
        self.assertEqual(context.status_code, 200, context.data)
        self.assertEqual(context.data["appearance_role"], "variant")
        self.assertTrue(context.data["can_select"])
        result = self.client.post(url + "select-artifact/", {"artifact_id": str(artifact.pk),
            "material_selection": {"expected_decision_id": context.data["expected_decision_id"],
                "expected_dependencies_sha256": context.data["dependencies_sha256"]}}, format="json", HTTP_IDEMPOTENCY_KEY="select-variant")
        self.assertEqual(result.status_code, 200, result.data)
        decision = ArtifactSelectionDecision.objects.get(artifact=artifact)
        self.assertEqual((decision.selection_mode, decision.selected_by_id), ("manual", self.user.pk))
        files = {path for path in Path(settings.MEDIA_ROOT).rglob("*") if path.is_file()}
        with patch("studio.services.asset_references._save", side_effect=RuntimeError("isolated version failure")):
            with self.assertRaisesMessage(RuntimeError, "isolated version failure"):
                with _StoredAssetFileGuard() as guard, transaction.atomic():
                    register_recipe_material_selection(stage_run_id=self.stage.pk, unit_key=artifact.generation_attempt.unit_key,
                        decision_id=decision.pk, actor=self.user, asset_guard=guard)
        artifact.refresh_from_db()
        self.assertIsNone(artifact.asset_id)
        self.assertEqual(files, {path for path in Path(settings.MEDIA_ROOT).rglob("*") if path.is_file()})
        self.assertEqual(read_generated_material(artifact=primary.generated_artifact).pk, primary.pk)
