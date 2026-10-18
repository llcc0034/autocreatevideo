import hashlib
import shutil
import tempfile
import wave
from pathlib import Path

from django.contrib.auth import get_user_model
from django.test import TestCase
from PIL import Image
from rest_framework.test import APIClient

from studio.media_signing import signed_media_token
from studio.models import GeneratedArtifact, ProductionRun, SeriesProject
from studio.services.json_contracts import json_contract
from studio.services.production_run_plans import freeze_run_execution_plan
from studio.services.production_stage_catalog import production_stage_catalog


class GeneratedArtifactMediaTests(TestCase):
    def setUp(self):
        self.user = get_user_model().objects.create_user(
            username="artifact-media-owner", password="StrongPass123!",
        )
        self.other = get_user_model().objects.create_user(
            username="artifact-media-other", password="StrongPass123!",
        )
        self.project = SeriesProject.objects.create(language="Chinese", aspect_ratio="9:16", owner=self.user, title="正式产物读取", width=768, height=1344, fps=24, default_shot_duration_ms=5000, continuity_seed=1)
        config = json_contract({"schema_version": 1, "values": {}})
        request = json_contract({"schema_version": 1, "episode_ids": []})
        self.run = ProductionRun.objects.create(
            owner=self.user,
            project=self.project,
            plan_source="manual",
            idempotency_key="artifact-media-run",
            request_sha256="a" * 64,
            config_snapshot=config.value,
            config_snapshot_schema_version=config.schema_version,
            config_snapshot_size_bytes=config.size_bytes,
            config_snapshot_sha256=config.sha256,
            input_snapshot=request.value,
            input_snapshot_schema_version=request.schema_version,
            input_snapshot_size_bytes=request.size_bytes,
            input_snapshot_sha256=request.sha256,
        )
        catalog = production_stage_catalog()
        freeze_run_execution_plan(run=self.run, scope={"kind": "project"}, delivery={"kind": "episode_masters"},
            initial_content=None, stage_catalog_key=catalog.value["key"], expected_stage_catalog_sha256=catalog.sha256,
            actor=self.user)
        self.client = APIClient(); self.client.force_authenticate(self.user)

    def test_audio_list_preview_and_download_verify_real_audio_bytes(self):
        with tempfile.TemporaryDirectory() as workspace:
            media_root = Path(workspace)
            relative = Path("generated-artifacts", str(self.user.pk), str(self.project.pk), "audio", "reference.wav")
            path = media_root / relative
            path.parent.mkdir(parents=True)
            with wave.open(str(path), "wb") as stream:
                stream.setnchannels(1); stream.setsampwidth(2); stream.setframerate(8000)
                stream.writeframes(b"\0\0" * 8000)
            metadata = json_contract({"schema_version": 1, "source": "restricted_async_rest", "tenant_owner_id": str(self.user.pk)})
            from studio.services.generated_artifacts import verified_local_media_facts
            facts = verified_local_media_facts(path, expected_kind="audio", max_bytes=path.stat().st_size)
            artifact = GeneratedArtifact.objects.create(manifest_key="audio-preview", production_run=self.run, kind="audio", role="primary",
                variant_group="audio-reference", variant_index=1, status="available", storage_backend="managed_local",
                object_key=relative.as_posix(), mime_type=facts["mime_type"], size_bytes=facts["size_bytes"], sha256=facts["sha256"],
                manifest_metadata=metadata.value, manifest_schema_version=metadata.schema_version,
                manifest_metadata_size_bytes=metadata.size_bytes, manifest_metadata_sha256=metadata.sha256)
            with self.settings(MEDIA_ROOT=media_root):
                page = self.client.get("/api/v1/generated-artifacts/", {"production_run": str(self.run.pk), "kind": "audio"})
                self.assertEqual(page.status_code, 200, page.data)
                row, = page.data["results"]
                self.assertEqual(row["id"], str(artifact.pk))
                self.assertTrue(row["preview_url"])
                anonymous = APIClient()
                for url in (row["preview_url"], row["download_url"]):
                    response = anonymous.get(url)
                    self.assertEqual(response.status_code, 200)
                    self.assertEqual(b"".join(response.streaming_content), path.read_bytes())
                path.write_bytes(b"corrupted audio")
                self.assertEqual(anonymous.get(row["preview_url"]).status_code, 404)

    def test_async_storage_never_falls_back_to_old_run_actor_or_missing_tenant_metadata(self):
        from studio.services.generated_artifacts import generated_artifact_storage_owner_id
        from studio.services.provenance import ProvenanceError
        staff = get_user_model().objects.create_user(username="old-storage-actor", is_staff=True)
        self.run.owner = staff
        self.run.save(update_fields=("owner",))
        artifact = GeneratedArtifact(production_run=self.run, storage_backend="managed_local")
        for owner_id, metadata in ((staff.pk, {"source": "restricted_async_rest"}),
                (self.user.pk, {"source": "restricted_async_rest"}),
                (staff.pk, {"source": "restricted_async_rest", "tenant_owner_id": str(staff.pk)})):
            artifact.object_key = f"generated-artifacts/{owner_id}/{self.project.pk}/async/isolated.media"
            artifact.manifest_metadata = metadata
            with self.assertRaises(ProvenanceError):
                generated_artifact_storage_owner_id(artifact)

    def test_managed_artifact_remains_readable_after_temporary_source_is_deleted(self):
        with tempfile.TemporaryDirectory() as workspace:
            root = Path(workspace)
            media_root = root / "media"
            source = root / "temporary-source.png"
            Image.new("RGB", (4, 3), color=(11, 22, 33)).save(source, format="PNG")
            relative = Path(
                "generated-artifacts", str(self.user.id), str(self.project.id),
                "fixture", "0", "result.png",
            )
            managed = media_root / relative
            managed.parent.mkdir(parents=True)
            shutil.copyfile(source, managed)
            source.unlink()
            payload = managed.read_bytes()
            metadata = json_contract({"schema_version": 1, "source": "test"})
            artifact = GeneratedArtifact.objects.create(
                manifest_key="managed-artifact-media-test",
                production_run=self.run,
                kind=GeneratedArtifact.Kind.IMAGE,
                role="primary_generation_output",
                variant_group="image:fixture",
                variant_index=1,
                status=GeneratedArtifact.Status.SELECTED,
                storage_backend="managed_local",
                object_key=relative.as_posix(),
                mime_type="image/png",
                size_bytes=len(payload),
                sha256=hashlib.sha256(payload).hexdigest(),
                manifest_metadata=metadata.value,
                comparison_parameters={
                    "schema_version": 1,
                    "model": "image-model-v1",
                    "generation": {"seed": 17, "width": 768, "height": 1344},
                },
                manifest_schema_version=metadata.schema_version,
                manifest_metadata_size_bytes=metadata.size_bytes,
                manifest_metadata_sha256=metadata.sha256,
            )
            with self.settings(MEDIA_ROOT=media_root):
                detail = self.client.get(f"/api/v1/generated-artifacts/{artifact.id}/")
                self.assertEqual(detail.status_code, 200, detail.data)
                self.assertEqual(detail.data["comparison_parameters"], {
                    "schema_version": 1,
                    "model": "image-model-v1",
                    "generation": {"seed": 17, "width": 768, "height": 1344},
                })
                self.assertTrue(detail.data["preview_url"].startswith("/api/v1/media/"))
                anonymous = APIClient()
                preview = anonymous.get(detail.data["preview_url"])
                self.assertEqual(preview.status_code, 200)
                self.assertEqual(b"".join(preview.streaming_content), payload)

                wrong_owner = signed_media_token(
                    purpose="generated_artifact",
                    object_id=artifact.id,
                    owner_id=self.other.id,
                )
                self.assertEqual(
                    anonymous.get(f"/api/v1/media/{wrong_owner}/").status_code,
                    404,
                )
                artifact.status = GeneratedArtifact.Status.INVALID
                artifact.save(update_fields=("status", "updated_at"))
                self.assertEqual(anonymous.get(detail.data["preview_url"]).status_code, 404)

    def test_staff_run_uses_project_tenant_for_async_artifact_url_and_access(self):
        staff = get_user_model().objects.create_user(
            username="artifact-media-staff", password="StrongPass123!", is_staff=True,
        )
        self.run.owner = staff
        self.run.save(update_fields=("owner", "updated_at"))
        with tempfile.TemporaryDirectory() as workspace:
            media_root = Path(workspace) / "media"
            relative = Path(
                "generated-artifacts", str(self.user.id), str(self.project.id),
                "async", "fixture", "artifact-1.media",
            )
            managed = media_root / relative
            managed.parent.mkdir(parents=True)
            Image.new("RGB", (4, 3), color=(31, 41, 59)).save(managed, format="PNG")
            payload = managed.read_bytes()
            metadata = json_contract({
                "schema_version": 1,
                "source": "restricted_async_rest",
                "tenant_owner_id": str(self.user.id),
            })
            artifact = GeneratedArtifact.objects.create(
                manifest_key="managed-cross-owner-async-media-test",
                production_run=self.run,
                kind=GeneratedArtifact.Kind.IMAGE,
                role="primary_generation_output",
                variant_group="image:cross-owner",
                variant_index=1,
                status=GeneratedArtifact.Status.SELECTED,
                storage_backend="managed_local",
                object_key=relative.as_posix(),
                mime_type="image/png",
                size_bytes=len(payload),
                sha256=hashlib.sha256(payload).hexdigest(),
                manifest_metadata=metadata.value,
                manifest_schema_version=metadata.schema_version,
                manifest_metadata_size_bytes=metadata.size_bytes,
                manifest_metadata_sha256=metadata.sha256,
            )
            with self.settings(MEDIA_ROOT=media_root):
                # The project owner can read a staff-created run's tenant media.
                detail = self.client.get(f"/api/v1/generated-artifacts/{artifact.id}/")
                self.assertEqual(detail.status_code, 200, detail.data)
                anonymous = APIClient()
                preview = anonymous.get(detail.data["preview_url"])
                self.assertEqual(preview.status_code, 200)
                self.assertEqual(b"".join(preview.streaming_content), payload)

                actor_token = signed_media_token(
                    purpose="generated_artifact",
                    object_id=artifact.id,
                    owner_id=staff.id,
                )
                self.assertEqual(
                    anonymous.get(f"/api/v1/media/{actor_token}/").status_code,
                    404,
                )
