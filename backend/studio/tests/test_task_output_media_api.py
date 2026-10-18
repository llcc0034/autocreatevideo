from pathlib import Path
import json
import tempfile

from django.contrib.auth import get_user_model
from django.core.cache import cache
from django.test import TestCase, override_settings
from django.urls import reverse
from rest_framework.test import APIClient

from studio.media_signing import signed_media_token
from studio.models import ConfigRevision, GenerationTask
from studio.services.configuration import create_config_revision, publish_config_revision
from studio.services.provenance import task_manifest_path
from studio.tests import test_execution_production_workflow_requests as video_fixtures
from studio.tests import test_execution_production_workflow_artifacts as image_fixtures
from studio.tests.prepared_production_media_fixtures import PreparedProductionMediaFixture


@override_settings(AI_PROVIDER_ALLOWED_HOSTS=("127.0.0.1",), DEPLOYMENT_PROFILE="development")
class GenerationTaskOutputMediaAPITests(PreparedProductionMediaFixture, TestCase):
    def setUp(self):
        self.temp = tempfile.TemporaryDirectory()
        self.addCleanup(self.temp.cleanup)
        self.output_root = Path(self.temp.name) / "comfy-output"
        self.output_root.mkdir()
        self.superadmin = get_user_model().objects.create_superuser(
            username="task-output-superadmin", password="x")
        self.client = APIClient()

    def prepare_task(self, kind):
        self.media_kind = kind
        fixture = (image_fixtures.ProductionWorkflowArtifactTests
                   if kind == "image" else video_fixtures.ProductionWorkflowRequestTests)
        fixture.setUp(self)
        self.client.force_authenticate(self.user)
        self.task = self.attempt.generation_task
        return self.task

    def _task(self, outputs, *, kind="video"):
        # Transport/API tests deliberately supply persisted receipts (including
        # invalid paths). They do not fabricate execution contracts or Smoke.
        output_root = self.output_root
        task = self.prepare_task(kind)
        self.output_root = output_root
        setting = override_settings(COMFYUI_OUTPUT_DIR=str(output_root))
        setting.enable()
        self.addCleanup(setting.disable)
        GenerationTask.objects.filter(pk=task.pk).update(status="succeeded", progress=100, outputs=outputs)
        task.refresh_from_db()
        return task

    def sealed_task(self, kind="image", **kwargs):
        # Hash/disclosure tests use real decoded PNG bytes and the normal
        # preparation-backed archival writer, not a forged media observation.
        self.archive_isolated_media(kind, **kwargs)
        self.client.force_authenticate(self.user)
        return self.task

    def test_persisted_mp4_is_previewable_and_downloadable_even_when_kind_is_images(self):
        task = self.sealed_task("video", collection="images")

        detail = self.client.get(f"/api/v1/tasks/{task.id}/")

        self.assertEqual(detail.status_code, 200, detail.data)
        self.assertEqual(len(detail.data["output_media"]), 1)
        media = detail.data["output_media"][0]
        self.assertEqual(media["kind"], "video")
        self.assertEqual(media["filename"], self.output.name)
        anonymous = APIClient()
        preview = anonymous.get(media["preview_url"])
        self.assertEqual(preview.status_code, 200)
        self.assertEqual(preview["Content-Type"], "video/mp4")
        self.assertTrue(preview["Content-Disposition"].startswith("inline;"))
        preview.close()
        self.assertEqual(media["distribution_status"], "development_master")
        self.assertTrue(media["download_url"].endswith("?download=1"))
        download = anonymous.get(media["download_url"])
        self.assertEqual(download.status_code, 200)
        self.assertTrue(download["Content-Disposition"].startswith("attachment;"))
        download.close()

    def test_persisted_png_image_is_previewable_and_downloadable(self):
        task = self.sealed_task()

        detail = self.client.get(f"/api/v1/tasks/{task.id}/")

        self.assertEqual(detail.status_code, 200, detail.data)
        media = detail.data["output_media"][0]
        self.assertEqual(media["kind"], "image")
        anonymous = APIClient()
        preview = anonymous.get(media["preview_url"])
        self.assertEqual(preview.status_code, 200)
        self.assertEqual(preview["Content-Type"], "image/png")
        self.assertTrue(preview["Content-Disposition"].startswith("inline;"))
        preview.close()
        self.assertEqual(media["distribution_status"], "development_master")
        download = anonymous.get(media["download_url"])
        self.assertEqual(download.status_code, 200)
        self.assertTrue(download["Content-Disposition"].startswith("attachment;"))
        download.close()

    def test_signed_media_uses_its_own_database_rate_without_consuming_anonymous_api_quota(self):
        revision = create_config_revision(
            scope_type=ConfigRevision.ScopeType.SYSTEM,
            values={
                "access.throttle_anon_rate": "1/hour",
                "access.throttle_signed_media_rate": "2/hour",
            },
            created_by=self.superadmin,
            clone_published=True,
        )
        publish_config_revision(revision, published_by=self.superadmin)
        cache.clear()
        task = self.sealed_task()
        preview_url = self.client.get(f"/api/v1/tasks/{task.id}/").data[
            "output_media"
        ][0]["preview_url"]
        anonymous = APIClient()

        first = anonymous.get(preview_url)
        second = anonymous.get(preview_url)
        throttled = anonymous.get(preview_url)

        self.assertEqual(first.status_code, 200)
        self.assertEqual(second.status_code, 200)
        self.assertEqual(throttled.status_code, 429)
        first.close()
        second.close()
        self.assertEqual(anonymous.get("/api/v1/auth/register/").status_code, 200)

    def test_production_master_remains_preview_only_until_distribution_is_ready(self):
        task = self.sealed_task("video")
        with override_settings(DEPLOYMENT_PROFILE="production"):
            media = self.client.get(f"/api/v1/tasks/{task.id}/").data["output_media"][0]
            self.assertEqual(media["download_url"], "")
            self.assertEqual(APIClient().get(f'{media["preview_url"]}?download=1').status_code, 404)

    def test_task_output_signed_url_rejects_path_escape_unpersisted_index_and_wrong_owner(self):
        outside = Path(self.temp.name) / "outside.mp4"
        outside.write_bytes(b"secret")
        task = self._task([{
            "kind": "videos", "filename": outside.name, "subfolder": "..", "type": "output",
        }])
        detail = self.client.get(f"/api/v1/tasks/{task.id}/")
        escaped_url = detail.data["output_media"][0]["preview_url"]
        anonymous = APIClient()
        self.assertEqual(anonymous.get(escaped_url).status_code, 404)

        missing_token = signed_media_token(
            purpose="task_output", object_id=task.id, owner_id=self.user.id,
            claims={"output_index": 1},
        )
        self.assertEqual(anonymous.get(reverse("signed-media", kwargs={"token": missing_token})).status_code, 404)

        other = get_user_model().objects.create_user(username="media-bob", password="StrongPass123!")
        wrong_owner_token = signed_media_token(
            purpose="task_output", object_id=task.id, owner_id=other.id,
            claims={"output_index": 0},
        )
        self.assertEqual(
            anonymous.get(reverse("signed-media", kwargs={"token": wrong_owner_token})).status_code,
            404,
        )
        self.client.force_authenticate(other)
        self.assertEqual(self.client.get(f"/api/v1/tasks/{task.id}/").status_code, 404)

    def test_preview_supports_single_byte_ranges_for_video_seeking(self):
        task = self.sealed_task("video")
        payload = self.output.read_bytes()
        detail = self.client.get(f"/api/v1/tasks/{task.id}/")
        preview_url = detail.data["output_media"][0]["preview_url"]
        anonymous = APIClient()

        partial = anonymous.get(preview_url, HTTP_RANGE="bytes=2-5")

        self.assertEqual(partial.status_code, 206)
        self.assertEqual(partial["Accept-Ranges"], "bytes")
        self.assertEqual(partial["Content-Range"], f"bytes 2-5/{len(payload)}")
        self.assertEqual(partial["Content-Length"], "4")
        self.assertEqual(b"".join(partial.streaming_content), payload[2:6])
        partial.close()

        invalid = anonymous.get(preview_url, HTTP_RANGE=f"bytes={len(payload)}-{len(payload)+1}")
        self.assertEqual(invalid.status_code, 416)
        self.assertEqual(invalid["Content-Range"], f"bytes */{len(payload)}")

    def test_missing_seal_is_not_repaired_by_preview(self):
        task = self.sealed_task()
        sidecar = task_manifest_path(task, 0)
        sidecar.unlink()
        unsealed = [{key: value for key, value in task.outputs[0].items() if key != "provenance"}]
        GenerationTask.objects.filter(pk=task.pk).update(outputs=unsealed)
        response = self.client.get(f"/api/v1/tasks/{task.id}/")
        self.assertEqual(response.status_code, 200, response.data)
        preview = APIClient().get(response.data["output_media"][0]["preview_url"])
        self.assertEqual(preview.status_code, 404)
        task.refresh_from_db()
        self.assertEqual(task.outputs, unsealed)
        self.assertFalse(sidecar.exists())

    def test_non_media_outputs_are_not_signed(self):
        task = self._task([{
            "kind": "videos", "filename": "execution.json", "subfolder": "", "type": "output",
        }])
        detail = self.client.get(f"/api/v1/tasks/{task.id}/")
        self.assertEqual(detail.status_code, 200, detail.data)
        self.assertEqual(detail.data["output_media"], [])

    def test_sealed_task_provenance_is_tenant_bound_hash_only_and_disclosed_on_download(self):
        task = self.sealed_task()
        payload = self.output.read_bytes()

        detail = self.client.get(f"/api/v1/tasks/{task.id}/")
        self.assertEqual(detail.status_code, 200, detail.data)
        self.assertNotIn("staged_inputs", detail.data)
        media = detail.data["output_media"][0]
        self.assertTrue(media["ai_generated"])
        self.assertRegex(media["artifact_sha256"], r"^[0-9a-f]{64}$")
        self.assertRegex(media["manifest_sha256"], r"^[0-9a-f]{64}$")
        self.assertTrue(media["provenance_url"].startswith("/api/v1/media/"))

        anonymous = APIClient()
        manifest_response = anonymous.get(media["provenance_url"])
        self.assertEqual(manifest_response.status_code, 200)
        self.assertEqual(manifest_response["X-AI-Generated"], "true")
        document = manifest_response.json()
        self.assertTrue(document["disclosure"]["ai_generated"])
        self.assertEqual(document["verification"]["status"], "verified")
        self.assertEqual(document["generation"]["task_id"], str(task.id))
        self.assertEqual(document["generation"]["request_sha256"], self.attempt.request_sha256)
        self.assertEqual(document["generation"]["preparation_id"], str(self.preparation.pk))
        self.assertEqual(document["source_media"][0]["source_sha256"], self.asset.checksum)
        serialized = json.dumps(document, ensure_ascii=False)
        self.assertNotIn("PRIVATE PROMPT", serialized)
        self.assertNotIn(str(Path(self.temp.name)), serialized)

        # Development mode intentionally allows downloading the exact signed
        # master for iteration. Production keeps this URL preview-only and
        # requires an independently verified distribution copy.
        self.assertEqual(media["distribution_status"], "development_master")
        download = anonymous.get(media["download_url"])
        self.assertEqual(download.status_code, 200)
        self.assertEqual(b"".join(download.streaming_content), payload)
        download.close()

        other = get_user_model().objects.create_user(
            username="provenance-bob", password="StrongPass123!",
        )
        wrong_owner_token = signed_media_token(
            purpose="task_provenance", object_id=task.id, owner_id=other.id,
            claims={"output_index": 0},
        )
        self.assertEqual(
            anonymous.get(reverse("signed-media", kwargs={"token": wrong_owner_token})).status_code,
            404,
        )
        tampered_token = media["provenance_url"].rsplit("/", 2)[-2] + "x"
        self.assertEqual(
            anonymous.get(reverse("signed-media", kwargs={"token": tampered_token})).status_code,
            404,
        )

    def test_task_provenance_rejects_media_and_sidecar_tampering(self):
        task = self.sealed_task()
        path = self.output
        original = path.read_bytes()
        detail = self.client.get(f"/api/v1/tasks/{task.id}/")
        media = detail.data["output_media"][0]
        anonymous = APIClient()

        path.write_bytes(b"tampered-video")
        self.assertEqual(anonymous.get(media["provenance_url"]).status_code, 404)
        self.assertEqual(anonymous.get(media["preview_url"]).status_code, 404)
        self.assertEqual(anonymous.get(media["download_url"]).status_code, 404)

        path.write_bytes(original)
        sidecar = task_manifest_path(task, 0)
        document = json.loads(sidecar.read_text(encoding="utf-8"))
        document["generation"]["model"] = "tampered-model"
        sidecar.write_text(json.dumps(document), encoding="utf-8")
        self.assertEqual(anonymous.get(media["provenance_url"]).status_code, 404)
        self.assertEqual(anonymous.get(media["preview_url"]).status_code, 404)
        self.assertEqual(anonymous.get(media["download_url"]).status_code, 404)
