"""Actual frozen preparation -> PNG archive -> original signed distribution.

Only temporary files and isolated database rows; no model or remote service.
"""
import json
from pathlib import Path
from unittest.mock import patch

from django.test import TestCase, override_settings
from PIL import Image
from rest_framework.test import APIClient

from studio.models import GenerationAttempt, GenerationTask, ModelProfileVersion
from studio.services.distribution import (
    DistributionClaimLost, DistributionError, distribution_artifact_path, distribution_manifest_path,
    prepare_task_distribution, verified_distribution_manifest,
)
from studio.services.provenance import (
    ProvenanceError, _seal, resolve_declared_task_output, sha256_file, task_manifest_path, verified_task_manifest,
)
from studio.tests import test_execution_production_workflow_artifacts as fixtures


@override_settings(AI_PROVIDER_ALLOWED_HOSTS=("127.0.0.1",), DEPLOYMENT_PROFILE="production")
class PreparedTaskDistributionTests(TestCase):
    resolve = fixtures.ProductionWorkflowArtifactTests.resolve
    mark_submitted = fixtures.ProductionWorkflowArtifactTests.mark_submitted
    archive = fixtures.ProductionWorkflowArtifactTests.archive

    def setUp(self):
        fixtures.ProductionWorkflowArtifactTests.setUp(self)
        self.archive()
        self.task.refresh_from_db()
        self.client = APIClient()
        self.client.force_authenticate(self.user)

    def require_font(self):
        candidates = (Path(r"C:\Windows\Fonts\arial.ttf"),
            Path("/usr/share/fonts/truetype/dejavu/DejaVuSans.ttf"))
        font = next((path for path in candidates if path.is_file()), None)
        if font is None:
            self.skipTest("A real installed test font is required for image rendering")
        setting = override_settings(TASK_DISTRIBUTION_FONTFILE=str(font))
        setting.enable()
        self.addCleanup(setting.disable)

    def test_frozen_identity_real_render_idempotence_and_signed_download(self):
        self.require_font()
        master_hash = sha256_file(self.output)
        original_model = self.profile.model_identifier
        # Rendering a past result must not pick up a renamed current model.
        ModelProfileVersion.objects.filter(pk=self.profile.pk).update(model_identifier="Not this invocation")
        first = prepare_task_distribution(self.task, 0)
        self.task.refresh_from_db()
        public = distribution_artifact_path(self.task, 0)
        self.assertEqual(first["release"]["model_key"], str(self.profile.pk))
        self.assertEqual(first["release"]["model"], original_model)
        self.assertEqual(first["source_artifact"]["sha256"], master_hash)
        self.assertEqual(first["verification"]["status"], "verified")
        self.assertEqual(sha256_file(self.output), master_hash)
        self.assertNotEqual(public.read_bytes(), self.output.read_bytes())
        with Image.open(public) as rendered:
            self.assertEqual(rendered.info["Comment"], first["disclosure"]["label"])
            self.assertEqual(rendered.info["SourceArtifactSHA256"], master_hash)
        with patch("studio.services.distribution._render_image") as render:
            second = prepare_task_distribution(self.task, 0)
        render.assert_not_called()
        self.assertEqual(second["release"]["release_id"], first["release"]["release_id"])
        detail = self.client.get(f"/api/v1/tasks/{self.task.pk}/")
        self.assertEqual(detail.status_code, 200, detail.data)
        media, = detail.data["output_media"]
        anonymous = APIClient()
        preview = anonymous.get(media["preview_url"])
        self.assertEqual(preview.status_code, 200)
        self.assertEqual(b"".join(preview.streaming_content), self.output.read_bytes())
        preview.close()
        download = anonymous.get(media["download_url"])
        self.assertEqual(download.status_code, 200)
        self.assertEqual(b"".join(download.streaming_content), public.read_bytes())
        self.assertEqual(download["X-Managed-Distribution"], "true")
        download.close()

    def test_corrupt_request_cannot_use_old_trace_or_offer_new_public_copy(self):
        GenerationAttempt.objects.filter(pk=self.attempt.pk).update(request_sha256="0" * 64)
        with self.assertRaisesMessage(ProvenanceError, "source preparation"):
            verified_task_manifest(self.task, 0)
        with patch("studio.services.distribution._render_image") as render:
            with self.assertRaisesMessage(DistributionError, "Source master provenance"):
                prepare_task_distribution(self.task, 0)
        render.assert_not_called()
        self.task.refresh_from_db()
        self.assertEqual(self.task.status, "succeeded")
        self.assertEqual(self.task.outputs[0]["distribution"]["status"], "failed")
        detail = self.client.get(f"/api/v1/tasks/{self.task.pk}/")
        self.assertEqual(detail.status_code, 400, detail.data)
        self.assertNotIn("output_media", detail.data)

    def test_valid_signature_alone_cannot_replace_actual_preparation_identity(self):
        sidecar = task_manifest_path(self.task, 0)
        document = json.loads(sidecar.read_text(encoding="utf-8"))
        document.pop("integrity")
        document["generation"]["preparation_id"] = str(self.stage.pk)
        changed = _seal(document)
        sidecar.write_text(json.dumps(changed), encoding="utf-8")
        outputs = list(self.task.outputs)
        outputs[0]["provenance"]["manifest_sha256"] = changed["integrity"]["payload_sha256"]
        GenerationTask.objects.filter(pk=self.task.pk).update(outputs=outputs)
        self.task.refresh_from_db()
        with self.assertRaisesMessage(ProvenanceError, "actual frozen preparation"):
            verified_task_manifest(self.task, 0)
        for index in (-1, True, 1):
            with self.subTest(index=index), self.assertRaisesMessage(ProvenanceError, "index"):
                verified_task_manifest(self.task, index)

    def test_tampered_public_bytes_are_not_repaired_and_master_stays_verifiable(self):
        self.require_font()
        prepare_task_distribution(self.task, 0)
        self.task.refresh_from_db()
        public = distribution_artifact_path(self.task, 0)
        tampered = public.read_bytes() + b"tamper"
        public.write_bytes(tampered)
        with self.assertRaises(DistributionError):
            verified_distribution_manifest(self.task, 0)
        with patch("studio.services.distribution._render_image") as render:
            with self.assertRaisesMessage(DistributionError, "failed verification"):
                prepare_task_distribution(self.task, 0)
        render.assert_not_called()
        self.assertEqual(public.read_bytes(), tampered)
        self.assertEqual(verified_task_manifest(self.task, 0)["verification"]["status"], "verified")
        self.assertTrue(distribution_manifest_path(self.task, 0).is_file())

    def test_primary_resolution_uses_frozen_node_and_real_media_without_legacy_role_alias(self):
        result = resolve_declared_task_output(self.task)
        self.assertEqual(result["path"], self.output)
        self.assertEqual(result["artifact_sha256"], sha256_file(self.output))
        self.assertEqual(result["manifest"]["artifact"]["media_kind"], "image")
        with self.assertRaisesMessage(ProvenanceError, "primary output role"):
            resolve_declared_task_output(self.task, "primary_video")
        self.task.outputs[0]["node_id"] = "wrong-node"
        with self.assertRaisesMessage(ProvenanceError, "primary output contract"):
            resolve_declared_task_output(self.task)

    def test_invalid_distribution_index_never_changes_outputs(self):
        original = self.task.outputs
        for index in (-1, True, False, "0", 1, None):
            with self.subTest(index=index), self.assertRaises(DistributionError):
                prepare_task_distribution(self.task, index)
        self.task.refresh_from_db()
        self.assertEqual(self.task.outputs, original)

    def test_late_preflight_failure_does_not_replace_another_completed_release(self):
        from studio.services import distribution
        self.require_font()
        real_font = distribution._resolve_font

        def competing_publisher():
            with patch.object(distribution, "_resolve_font", real_font):
                prepare_task_distribution(self.task, 0)
            raise DistributionError("This older publisher could not resolve its font")

        with patch.object(distribution, "_resolve_font", side_effect=competing_publisher):
            with self.assertRaisesMessage(DistributionError, "older publisher"):
                prepare_task_distribution(self.task, 0)
        self.task.refresh_from_db()
        self.assertEqual(self.task.outputs[0]["distribution"]["status"], "ready")
        self.assertEqual(verified_distribution_manifest(self.task, 0)["verification"]["status"], "verified")

    def test_reclaimed_processing_lock_rejects_late_render_without_failure_write(self):
        from studio.services import distribution
        self.require_font()
        renderer = distribution._render_image
        parent = distribution._distribution_parent(self.task, 0)
        replacement = []

        def render_and_lose_claim(*args, **kwargs):
            result = renderer(*args, **kwargs)
            lock, = parent.glob(".*.lock")
            document = json.loads(lock.read_text(encoding="utf-8"))
            document["owner_token"] = "new-publisher"
            lock.write_text(json.dumps(document), encoding="utf-8")
            replacement.append(lock)
            return result

        with patch.object(distribution, "_render_image", side_effect=render_and_lose_claim):
            with self.assertRaisesMessage(DistributionClaimLost, "processing lock"):
                prepare_task_distribution(self.task, 0)
        self.task.refresh_from_db()
        self.assertNotIn("distribution", self.task.outputs[0])
        self.assertEqual(self.task.status, "succeeded")
        self.assertTrue(replacement[0].is_file())
        self.assertFalse(list(parent.glob("*/public.png")))
        self.assertEqual(verified_task_manifest(self.task, 0)["verification"]["status"], "verified")

    def test_output_change_rejects_publication_and_does_not_record_a_new_source_as_failed(self):
        from studio.services import distribution
        self.require_font()
        renderer = distribution._render_image

        def render_and_change_source(*args, **kwargs):
            result = renderer(*args, **kwargs)
            changed = json.loads(json.dumps(self.task.outputs))
            changed[0]["node_id"] = "a-different-source"
            GenerationTask.objects.filter(pk=self.task.pk).update(outputs=changed)
            return result

        with patch.object(distribution, "_render_image", side_effect=render_and_change_source):
            with self.assertRaises(DistributionClaimLost):
                prepare_task_distribution(self.task, 0)
        self.task.refresh_from_db()
        self.assertEqual(self.task.outputs[0]["node_id"], "a-different-source")
        self.assertNotIn("distribution", self.task.outputs[0])
