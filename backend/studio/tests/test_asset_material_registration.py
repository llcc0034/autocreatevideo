"""Original independent image queue -> shared versions, using isolated PNG peers."""
from copy import deepcopy
from pathlib import Path, PurePosixPath
from unittest.mock import Mock, patch

from django.core.exceptions import ValidationError
from django.test import TestCase, override_settings
from django.utils import timezone
from PIL import Image
from rest_framework.exceptions import ValidationError as APIValidationError

from studio.models import Asset, AssetGenerationJob, AssetReferenceSeries, AssetReferenceVersion, GenerationAttempt, ProductionRun
from studio.services.asset_generation import claim_next_comfy_asset_job, process_comfy_asset_job
from studio.services.asset_references import attach_asset, create_series, read_generated_material, register_generated_material
from studio.services.execution_asset_dispatch import dispatch_asset_execution
from studio.services.media_generation_targets import read_media_fulfillment_version
from studio.tests.test_execution_asset_requests import freeze, isolated_observations
from studio.tests.test_execution_workflow_staging import RecordingUploadClient
from studio.tests import test_media_generation_appearances as fixtures


@override_settings(AI_PROVIDER_ALLOWED_HOSTS=("127.0.0.1",))
class AssetMaterialRegistrationTests(TestCase):
    setUp = fixtures.AppearanceAssetPreparationTests.setUp
    create_job = fixtures.AppearanceAssetPreparationTests.create_job
    primary_version = fixtures.AppearanceAssetPreparationTests.primary_version
    http_payload = fixtures.AppearanceAssetPreparationTests.http_payload
    post_target = fixtures.AppearanceAssetPreparationTests.post_target

    def tearDown(self):
        self.external.assert_not_called()
        self.assertFalse(GenerationAttempt.objects.exists())
        self.assertFalse(ProductionRun.objects.exists())

    def submit(self, target, *, key="registered-image", retry_source=None):
        response = (self.post_target(target, key=key) if retry_source is None else
            self.client.post(f"/api/v1/asset-generation-jobs/{retry_source.pk}/retry/",
                format="json", HTTP_IDEMPOTENCY_KEY=key))
        self.assertEqual(response.status_code, 202, response.data)
        self.job = AssetGenerationJob.objects.get(pk=response.data["id"])
        self.prep = freeze(self.job, self.user)
        self.peer = Mock()
        self.enterContext(patch("studio.services.asset_generation.driver_for", return_value=self.peer))
        self.enterContext(patch("studio.services.execution_workflow_staging.ExecutionWorkflowUploadClient",
            return_value=RecordingUploadClient()))
        self.enterContext(patch("studio.services.execution_asset_dispatch.collect_asset_execution_admission", side_effect=isolated_observations))
        dispatch_asset_execution(preparation_id=self.prep.pk, expected_preparation_revision=self.prep.revision, actor=self.user)
        self.peer.invoke.return_value = {"request_id": str(self.prep.pk)}
        submitted = self.cycle()
        self.assertEqual(submitted.status, "submitted", submitted.error_message)
        self.peer.invoke.return_value = {"status": "succeeded"}
        self.assertEqual(self.cycle().status, "running")
        compiled = self.prep.contract.prepared_request["transport"]["payload"]["compiled_workflow"]
        prefix = PurePosixPath(compiled["resolved_semantics"]["output.prefix"])
        self.output = self.root / "output" / Path(*prefix.parent.parts) / f"{prefix.name}_00001.png"
        self.output.parent.mkdir(parents=True, exist_ok=True)
        Image.new("RGB", (16, 24), "blue").save(self.output)
        self.receipt = {"request_id": str(self.prep.pk), "outputs": [{"node_id": "7", "filename": self.output.name,
            "subfolder": str(prefix.parent), "type": "output"}]}
        self.peer.invoke.return_value = self.receipt

    def cycle(self):
        AssetGenerationJob.objects.filter(pk=self.job.pk).update(next_poll_at=timezone.now())
        claim = claim_next_comfy_asset_job("isolated-material-registration")
        self.assertIsNotNone(claim)
        return process_comfy_asset_job(claim)

    def complete(self):
        result = self.cycle()
        self.assertEqual(result.status, "succeeded", result.error_message)
        self.job = result
        return AssetReferenceVersion.objects.get(generation_job=result)

    def files(self):
        # Retained collect receipts are independent execution facts, not output
        # materialization files; failed registration must preserve the receipt.
        return {str(path.relative_to(self.root)) for path in (self.root / "media").rglob("*")
                if path.is_file() and "execution-responses" not in path.parts}

    def test_formal_primary_completion_registers_exact_origin_and_replay_without_copy(self):
        setting = fixtures.create_setting(self.user, self.project, self.character)
        self.submit(fixtures.declaration(setting=setting))
        original = deepcopy(self.prep.contract.resolved_inputs)
        version = self.complete()
        self.assertEqual((version.source, version.asset_id, version.generation_job_id, version.generated_artifact_id),
            ("generated", self.job.output_asset_id, self.job.pk, None))
        self.assertEqual((version.setting_revision_id, version.series.bible_variant_id, version.generated_from_id),
            (setting.pk, setting.variant_id, None))
        self.assertEqual(Path(version.asset.file.path).read_bytes(), self.output.read_bytes())
        self.assertEqual(version.asset.metadata["generation_job_id"], str(self.job.pk))
        before = self.files()
        with patch("django.db.models.fields.files.FieldFile.save", side_effect=AssertionError("No replay copy")):
            self.assertEqual(register_generated_material(job=self.job), (version, False))
            self.assertEqual(read_generated_material(job=self.job).pk, version.pk)
            resolved = read_media_fulfillment_version(version, expected_media_type="image")
        self.assertEqual(resolved.generation_origin["generation_job_id"], str(self.job.pk))
        self.assertIsNone(resolved.generation_origin["generated_artifact_id"])
        self.assertEqual((Asset.objects.count(), AssetReferenceVersion.objects.count()), (4, 1))
        self.assertEqual(self.files(), before)
        self.assertIsNone(claim_next_comfy_asset_job("terminal-no-retry"))
        self.assertEqual([call.args[1] for call in self.peer.invoke.call_args_list], ["submit", "poll", "collect"])
        self.prep.contract.refresh_from_db()
        self.assertEqual(self.prep.contract.resolved_inputs, original)
        response = self.client.get("/api/v1/asset-reference-versions/", {"series_id": str(version.series_id)})
        self.assertEqual(response.status_code, 200, response.data)
        self.assertIn(str(self.job.pk), str(response.data))

    def test_variant_keeps_exact_old_primary_not_newest_library_version(self):
        primary = self.primary_version()
        self.submit(fixtures.declaration("variant", primary=primary))
        self.assets[1].mime_type = "image/png"
        self.assets[1].save(update_fields=("mime_type",))
        latest = attach_asset(actor=self.user, series_id=primary.series_id, asset_id=self.assets[1].pk, source="existing")[0]
        self.assertGreater(latest.version, primary.version)
        version = self.complete()
        self.assertEqual((version.generated_from_id, version.series.primary_series_id, version.series.role),
            (primary.pk, primary.series_id, "variant"))
        self.assertIsNone(version.setting_revision_id)
        resolved = read_media_fulfillment_version(version, expected_media_type="image")
        self.assertEqual(resolved.generation_origin["generated_from_id"], str(primary.pk))
        self.assertEqual(AssetReferenceVersion.objects.count(), 3)

    def test_successful_retry_registration_cannot_be_duplicated_from_original_job(self):
        from studio.services.asset_generation import request_asset_generation_cancel
        from studio.models import ExecutionPreparation

        response = self.post_target(fixtures.declaration(), key="cancelled-original")
        self.assertEqual(response.status_code, 202, response.data)
        source = AssetGenerationJob.objects.get(pk=response.data["id"])
        request_asset_generation_cancel(source)
        self.submit(None, key="registered-successor", retry_source=source)
        version = self.complete()
        before = (AssetGenerationJob.objects.count(), ExecutionPreparation.objects.count(),
            Asset.objects.count(), AssetReferenceVersion.objects.count(), self.files(), self.peer.invoke.call_count)
        path = f"/api/v1/asset-generation-jobs/{source.pk}/retry/"
        replay = self.client.post(path, format="json", HTTP_IDEMPOTENCY_KEY="registered-successor")
        self.assertEqual(replay.status_code, 200, replay.data)
        self.assertEqual((replay.data["id"], replay.data["status"]), (str(self.job.pk), "succeeded"))
        branch = self.client.post(path, format="json", HTTP_IDEMPOTENCY_KEY="duplicate-success")
        self.assertEqual(branch.status_code, 409, branch.data)
        self.assertEqual(branch.data["code"], "execution_preparation_conflict")
        from django.db import connection
        from django.test.utils import CaptureQueriesContext
        with CaptureQueriesContext(connection) as queries:
            for _ in range(2):
                original = self.client.get(f"/api/v1/asset-generation-jobs/{source.pk}/")
                successor = self.client.get(f"/api/v1/asset-generation-jobs/{self.job.pk}/")
                assets = self.client.get("/api/v1/assets/")
                self.assertEqual((original.status_code, successor.status_code, assets.status_code), (200, 200, 200))
                self.assertEqual(original.data["retries"], [{"id": str(self.job.pk), "status": "succeeded"}])
                self.assertEqual(str(successor.data["retry_of"]), str(source.pk))
                self.assertEqual(successor.data["retries"], [])
        self.assertFalse(any(query["sql"].lstrip().upper().startswith(("INSERT", "UPDATE", "DELETE")) for query in queries))
        self.assertEqual((AssetGenerationJob.objects.count(), ExecutionPreparation.objects.count(),
            Asset.objects.count(), AssetReferenceVersion.objects.count(), self.files(), self.peer.invoke.call_count), before)
        self.assertEqual(read_generated_material(job=self.job).pk, version.pk)
        self.assertIsNone(claim_next_comfy_asset_job("no-duplicate-successor"))

    def test_unbound_primaries_do_not_merge_by_name_or_null_setting(self):
        self.submit(fixtures.declaration(), key="unbound-first")
        first = self.complete()
        self.submit(fixtures.declaration(), key="unbound-second")
        second = self.complete()
        self.assertEqual(first.series.name, second.series.name)
        self.assertNotEqual(first.series_id, second.series_id)
        self.assertIsNone(second.setting_revision_id)
        self.assertEqual(AssetReferenceSeries.objects.count(), 2)

    def test_absent_appearance_remains_ordinary_asset_without_invented_version(self):
        self.submit(None)
        result = self.cycle()
        self.assertEqual(result.status, "succeeded", result.error_message)
        self.assertIsNotNone(result.output_asset_id)
        self.assertEqual(register_generated_material(job=result), (None, False))
        self.assertIsNone(read_generated_material(job=result))
        self.assertFalse(AssetReferenceVersion.objects.exists())
        self.assertFalse(AssetReferenceSeries.objects.exists())

    def test_registration_failure_rolls_back_output_pointer_asset_and_new_file(self):
        self.submit(fixtures.declaration())
        before = self.files()
        with patch("studio.services.asset_references._save", side_effect=APIValidationError("isolated version registration failure")):
            result = self.cycle()
        self.assertEqual((result.status, result.remote_status), ("failed", "succeeded"))
        self.assertIn("isolated version registration failure", result.error_message)
        self.assertIsNone(result.output_asset_id)
        self.assertIsNone(result.actual_cost)
        self.assertFalse(AssetReferenceVersion.objects.exists())
        self.assertFalse(AssetReferenceSeries.objects.exists())
        self.assertEqual(Asset.objects.count(), 3)
        self.assertEqual(self.files(), before)
        self.assertTrue(self.output.is_file())
        self.assertIsNone(claim_next_comfy_asset_job("registration-failed-no-resubmit"))

    def test_ambiguous_bound_series_is_not_guessed_or_rebound(self):
        setting = fixtures.create_setting(self.user, self.project, self.character)
        for name in ("first", "second"):
            create_series(actor=self.user, values={"role": "primary", "name": name, "bible_variant_id": str(setting.variant_id)})
        self.submit(fixtures.declaration(setting=setting))
        before = self.files()
        result = self.cycle()
        self.assertEqual(result.status, "failed", result.error_message)
        self.assertIn("多个素材系列", result.error_message)
        self.assertIsNone(result.output_asset_id)
        self.assertEqual(self.files(), before)
        self.assertEqual(AssetReferenceSeries.objects.count(), 2)
        self.assertFalse(AssetReferenceVersion.objects.exists())

    def test_registered_origin_corruption_is_not_repaired_on_read_or_replay(self):
        self.submit(fixtures.declaration())
        version = self.complete()
        broken = {**version.asset.metadata, "generation_job_id": "not-the-original-job"}
        Asset.objects.filter(pk=version.asset_id).update(metadata=broken)
        for reader in (read_generated_material, register_generated_material):
            with self.assertRaisesMessage(ValidationError, "原独立生成来源不一致"):
                reader(job=self.job)
        self.assertEqual(Asset.objects.get(pk=version.asset_id).metadata, broken)
        self.assertEqual(AssetReferenceVersion.objects.count(), 1)

    def test_job_origin_lookup_uses_existing_fk_index(self):
        rows = AssetReferenceVersion.objects.filter(generation_job_id="00000000-0000-0000-0000-000000000001").order_by().values("id")
        plan = rows.explain()
        self.assertIn("SEARCH", plan)
        self.assertIn("INDEX", plan)
        self.assertNotIn("TEMP B-TREE", plan)

    def test_cancel_during_collect_does_not_register_a_late_result(self):
        self.submit(fixtures.declaration())
        before = self.files()

        def cancel_then_receipt(*_args, **_kwargs):
            AssetGenerationJob.objects.filter(pk=self.job.pk).update(status="cancel_requested")
            return self.receipt

        self.peer.invoke.side_effect = cancel_then_receipt
        with patch("studio.services.asset_references.register_generated_material", side_effect=AssertionError("No cancelled version")):
            result = self.cycle()
        self.assertEqual(result.status, "cancelled", result.error_message)
        self.assertIsNone(result.output_asset_id)
        self.assertFalse(AssetReferenceVersion.objects.exists())
        self.assertEqual(self.files(), before)
