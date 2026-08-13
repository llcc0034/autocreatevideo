from pathlib import Path
import json
import tempfile

from django.contrib.auth import get_user_model
from django.test import TestCase, override_settings
from django.urls import reverse
from django.utils import timezone
from rest_framework.test import APIClient

from studio.media_signing import signed_media_token
from studio.models import (
    Asset,
    CharacterBible,
    GeneratedWorkflow,
    GenerationTask,
    PromptArtifact,
    SeriesProject,
    WorkflowTemplate,
)
from studio.services.h3_prompts import (
    H3PromptOptions,
    build_consistent_image_prompt,
    build_h3_video_prompt,
)
from studio.services.provenance import persist_task_provenance, task_manifest_path
from studio.tests.factories import build_story


class GenerationTaskOutputMediaAPITests(TestCase):
    def setUp(self):
        self.temp = tempfile.TemporaryDirectory()
        self.addCleanup(self.temp.cleanup)
        self.output_root = Path(self.temp.name) / "comfy-output"
        self.output_root.mkdir()
        self.media_root = Path(self.temp.name) / "media"
        self.override = override_settings(
            COMFYUI_OUTPUT_DIR=str(self.output_root), MEDIA_ROOT=self.media_root,
        )
        self.override.enable()
        self.addCleanup(self.override.disable)

        self.user, self.project, _, _, self.episode, self.shot, _ = build_story()
        self.shot.duration_ms = 5000
        self.shot.save(update_fields=("duration_ms", "updated_at"))
        self.client = APIClient()
        self.client.force_authenticate(self.user)
        self.template = WorkflowTemplate.objects.create(
            name="task-output-media", slug="task-output-media", output_kind="video",
            definition={"1": {"class_type": "Mock", "inputs": {}}},
        )

    def _task(self, outputs, *, kind="video"):
        result = (
            build_consistent_image_prompt(self.project, self.shot)
            if kind == "image"
            else build_h3_video_prompt(self.project, [self.shot], H3PromptOptions())
        )
        prompt = PromptArtifact.objects.create(
            project=self.project, episode=self.episode, shot=self.shot, created_by=self.user,
            prompt_type=kind, h3_mode="T2VA", prompt_text=result.text,
            total_duration_ms=result.total_duration_ms, structured_payload=result.payload,
            consistency_snapshot=result.consistency_snapshot, content_hash=result.content_hash,
            request_options={"shot_ids": [str(self.shot.id)]},
        )
        template = self.template
        if kind == "image":
            template = WorkflowTemplate.objects.create(
                name="task-output-media-image", slug="task-output-media-image",
                output_kind="image",
                definition={"1": {"class_type": "MockImage", "inputs": {}}},
            )
        workflow = GeneratedWorkflow.objects.create(
            project=self.project, episode=self.episode, prompt_artifact=prompt,
            template=template, created_by=self.user,
            workflow_json=template.definition, content_hash="a" * 64,
        )
        return GenerationTask.objects.create(
            owner=self.user, project=self.project, episode=self.episode,
            generated_workflow=workflow, kind=kind, status=GenerationTask.Status.SUCCEEDED,
            progress=100, outputs=outputs,
        )

    def test_persisted_mp4_is_previewable_and_downloadable_even_when_kind_is_images(self):
        path = self.output_root / "episode-1" / "shot-01.mp4"
        path.parent.mkdir(parents=True)
        path.write_bytes(b"fake-mp4")
        task = self._task([{
            "kind": "images", "filename": path.name, "subfolder": "episode-1", "type": "output",
        }])

        detail = self.client.get(f"/api/v1/tasks/{task.id}/")

        self.assertEqual(detail.status_code, 200, detail.data)
        self.assertEqual(len(detail.data["output_media"]), 1)
        media = detail.data["output_media"][0]
        self.assertEqual(media["kind"], "video")
        self.assertEqual(media["filename"], "shot-01.mp4")
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
        path = self.output_root / "episode-1" / "shot-02.png"
        path.parent.mkdir(parents=True)
        path.write_bytes(b"fake-png")
        task = self._task([{
            "kind": "images", "filename": path.name,
            "subfolder": "episode-1", "type": "output",
        }], kind="image")

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

    def test_generated_image_can_be_idempotently_saved_to_reusable_asset_library(self):
        path = self.output_root / "episode-1" / "continuity-frame.png"
        path.parent.mkdir(parents=True)
        path.write_bytes(b"generated-image-bytes")
        task = self._task([{
            "kind": "images", "filename": path.name,
            "subfolder": "episode-1", "type": "output",
        }], kind="image")
        url = f"/api/v1/tasks/{task.id}/promote-output-to-asset/"
        asset_count_before = Asset.objects.filter(owner=self.user).count()

        created = self.client.post(url, {"output_index": 0, "name": "凌霜连续性定帧"}, format="json")

        self.assertEqual(created.status_code, 201, created.data)
        self.assertEqual(created.data["kind"], Asset.Kind.OUTPUT_IMAGE)
        self.assertEqual(created.data["name"], "凌霜连续性定帧")
        asset = Asset.objects.get(pk=created.data["id"])
        self.assertEqual(asset.owner, self.user)
        self.assertTrue(Path(asset.file.path).is_file())
        self.assertEqual(Path(asset.file.path).read_bytes(), path.read_bytes())
        task.refresh_from_db()
        self.assertEqual(task.outputs[0]["promoted_asset_id"], str(asset.id))

        repeated = self.client.post(url, {"output_index": 0}, format="json")
        self.assertEqual(repeated.status_code, 200, repeated.data)
        self.assertEqual(repeated.data["id"], str(asset.id))
        self.assertEqual(
            Asset.objects.filter(owner=self.user).count(), asset_count_before + 1,
        )

    def test_generated_image_can_be_saved_as_classified_ref2va_entity_reference(self):
        path = self.output_root / "episode-1" / "character-reference.png"
        path.parent.mkdir(parents=True)
        path.write_bytes(b"generated-character-reference")
        task = self._task([{
            "kind": "images", "filename": path.name,
            "subfolder": "episode-1", "type": "output",
        }], kind="image")
        character = self.project.characters.get()
        original_anchor_id = character.anchor_asset_id
        url = f"/api/v1/tasks/{task.id}/promote-output-to-asset/"
        payload = {
            "output_index": 0,
            "name": "林默新定妆",
            "reference_category": Asset.Kind.CHARACTER,
            "entity_id": str(character.id),
        }

        created = self.client.post(url, payload, format="json")

        self.assertEqual(created.status_code, 201, created.data)
        self.assertEqual(created.data["kind"], Asset.Kind.CHARACTER)
        asset = Asset.objects.get(pk=created.data["id"])
        character.refresh_from_db()
        self.assertEqual(character.anchor_asset_id, original_anchor_id)
        self.assertTrue(character.reference_assets.filter(pk=asset.id).exists())
        self.assertEqual(asset.metadata["reference_category"], Asset.Kind.CHARACTER)
        self.assertEqual(asset.metadata["reference_entity_id"], str(character.id))
        self.assertEqual(
            asset.metadata["generated_from"]["reference_category"],
            Asset.Kind.CHARACTER,
        )
        self.assertEqual(
            asset.metadata["generated_from"]["entity_id"], str(character.id),
        )
        task.refresh_from_db()
        self.assertEqual(task.outputs[0]["promoted_asset_target"], {
            "reference_category": Asset.Kind.CHARACTER,
            "entity_id": str(character.id),
        })

        repeated = self.client.post(url, payload, format="json")
        self.assertEqual(repeated.status_code, 200, repeated.data)
        self.assertEqual(repeated.data["id"], str(asset.id))

        changed_target = self.client.post(url, {
            "output_index": 0,
            "reference_category": Asset.Kind.SCENE,
            "entity_id": str(self.project.scenes.get().id),
        }, format="json")
        self.assertEqual(changed_target.status_code, 400, changed_target.data)
        self.assertIn("不能把同一次保存", str(changed_target.data))

    def test_classified_output_promotion_rejects_incomplete_or_foreign_entity_target(self):
        path = self.output_root / "classified-invalid.png"
        path.write_bytes(b"classified-invalid")
        task = self._task([{
            "kind": "images", "filename": path.name, "subfolder": "", "type": "output",
        }], kind="image")
        url = f"/api/v1/tasks/{task.id}/promote-output-to-asset/"

        incomplete = self.client.post(url, {
            "output_index": 0,
            "reference_category": Asset.Kind.CHARACTER,
        }, format="json")
        self.assertEqual(incomplete.status_code, 400, incomplete.data)
        self.assertIn("必须同时选择", str(incomplete.data))

        other_project = SeriesProject.objects.create(
            owner=self.user,
            title="另一个项目",
            style=self.project.style,
        )
        foreign_character = CharacterBible.objects.create(
            project=other_project,
            slug="foreign-character",
            name="外部人物",
            stable_speaker_id="S1",
        )
        foreign = self.client.post(url, {
            "output_index": 0,
            "reference_category": Asset.Kind.CHARACTER,
            "entity_id": str(foreign_character.id),
        }, format="json")
        self.assertEqual(foreign.status_code, 400, foreign.data)
        self.assertIn("不属于当前任务项目", str(foreign.data))
        task.refresh_from_db()
        self.assertNotIn("promoted_asset_id", task.outputs[0])

    def test_video_output_cannot_be_saved_as_image_asset(self):
        path = self.output_root / "not-an-image.mp4"
        path.write_bytes(b"video")
        task = self._task([{
            "kind": "videos", "filename": path.name, "subfolder": "", "type": "output",
        }])
        response = self.client.post(
            f"/api/v1/tasks/{task.id}/promote-output-to-asset/",
            {"output_index": 0}, format="json",
        )
        self.assertEqual(response.status_code, 400, response.data)
        self.assertIn("不是图片", str(response.data))

    @override_settings(DEPLOYMENT_PROFILE="production")
    def test_production_master_remains_preview_only_until_distribution_is_ready(self):
        path = self.output_root / "production-master.mp4"
        path.write_bytes(b"production-master")
        task = self._task([{
            "kind": "videos", "filename": path.name, "subfolder": "", "type": "output",
        }])

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
        payload = b"0123456789"
        path = self.output_root / "seekable.mp4"
        path.write_bytes(payload)
        task = self._task([{
            "kind": "videos", "filename": path.name, "subfolder": "", "type": "output",
        }])
        detail = self.client.get(f"/api/v1/tasks/{task.id}/")
        preview_url = detail.data["output_media"][0]["preview_url"]
        anonymous = APIClient()

        partial = anonymous.get(preview_url, HTTP_RANGE="bytes=2-5")

        self.assertEqual(partial.status_code, 206)
        self.assertEqual(partial["Accept-Ranges"], "bytes")
        self.assertEqual(partial["Content-Range"], "bytes 2-5/10")
        self.assertEqual(partial["Content-Length"], "4")
        self.assertEqual(b"".join(partial.streaming_content), b"2345")
        partial.close()

        invalid = anonymous.get(preview_url, HTTP_RANGE="bytes=99-100")
        self.assertEqual(invalid.status_code, 416)
        self.assertEqual(invalid["Content-Range"], "bytes */10")

    def test_non_media_outputs_are_not_signed(self):
        task = self._task([{
            "kind": "videos", "filename": "execution.json", "subfolder": "", "type": "output",
        }])
        detail = self.client.get(f"/api/v1/tasks/{task.id}/")
        self.assertEqual(detail.status_code, 200, detail.data)
        self.assertEqual(detail.data["output_media"], [])

    def test_sealed_task_provenance_is_tenant_bound_hash_only_and_disclosed_on_download(self):
        path = self.output_root / "episode-1" / "sealed-shot.mp4"
        path.parent.mkdir(parents=True)
        path.write_bytes(b"sealed-video")
        task = self._task([{
            "kind": "videos", "filename": path.name,
            "subfolder": "episode-1", "type": "output",
        }])
        task.staged_inputs = [{
            "asset_id": "reference-asset",
            "media_role": "reference_image",
            "source_sha256": "1" * 64,
            "submitted_sha256": "2" * 64,
            "source_path": str(Path(self.temp.name) / "private" / "reference.png"),
        }]
        task.save(update_fields=("staged_inputs", "updated_at"))
        persist_task_provenance(task, list(task.outputs), generated_at=timezone.now())

        detail = self.client.get(f"/api/v1/tasks/{task.id}/")
        self.assertEqual(detail.status_code, 200, detail.data)
        self.assertEqual(detail.data["staged_inputs"], [])
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
        self.assertRegex(document["generation"]["prompt_sha256"], r"^[0-9a-f]{64}$")
        self.assertEqual(document["source_media"][0]["source_sha256"], "1" * 64)
        serialized = json.dumps(document, ensure_ascii=False)
        prompt_text = task.generated_workflow.prompt_artifact.prompt_text
        self.assertNotIn(prompt_text, serialized)
        self.assertNotIn(str(Path(self.temp.name)), serialized)

        # Development mode intentionally allows downloading the exact signed
        # master for iteration. Production keeps this URL preview-only and
        # requires an independently verified distribution copy.
        self.assertEqual(media["distribution_status"], "development_master")
        download = anonymous.get(media["download_url"])
        self.assertEqual(download.status_code, 200)
        self.assertEqual(b"".join(download.streaming_content), b"sealed-video")
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
        path = self.output_root / "tamper.mp4"
        path.write_bytes(b"original-video")
        task = self._task([{
            "kind": "videos", "filename": path.name, "subfolder": "", "type": "output",
        }])
        persist_task_provenance(task, list(task.outputs), generated_at=timezone.now())
        detail = self.client.get(f"/api/v1/tasks/{task.id}/")
        media = detail.data["output_media"][0]
        anonymous = APIClient()

        path.write_bytes(b"tampered-video")
        self.assertEqual(anonymous.get(media["provenance_url"]).status_code, 404)
        self.assertEqual(anonymous.get(media["preview_url"]).status_code, 404)
        self.assertEqual(anonymous.get(media["download_url"]).status_code, 404)

        path.write_bytes(b"original-video")
        sidecar = task_manifest_path(task, 0)
        document = json.loads(sidecar.read_text(encoding="utf-8"))
        document["generation"]["model"] = "tampered-model"
        sidecar.write_text(json.dumps(document), encoding="utf-8")
        self.assertEqual(anonymous.get(media["provenance_url"]).status_code, 404)
        self.assertEqual(anonymous.get(media["preview_url"]).status_code, 404)
        self.assertEqual(anonymous.get(media["download_url"]).status_code, 404)
