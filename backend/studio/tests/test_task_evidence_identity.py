"""M7 diagnostic reads over isolated rows and actual temporary PNG bytes.

No provider, network, dispatch, adoption or persistent database is permitted.
The existing preparation builder supplies a labelled admission fixture only;
task-center permissions, output projection and signed-file readers are real.
"""
from hashlib import sha256
import os
from pathlib import Path
import sys
from unittest.mock import patch
from uuid import uuid4

from django.contrib.auth import get_user_model
from django.db import connection
from django.test import TestCase, override_settings
from django.test.utils import CaptureQueriesContext
from PIL import Image
from rest_framework.test import APIClient

from studio.media_signing import signed_media_token
from studio.models import GeneratedArtifact, GenerationAttempt, GenerationTask, ProductionRun, SeriesProject, StageRun
from studio.services.json_contracts import json_contract
from studio.tests import test_execution_production_media as fixtures


@override_settings(AI_PROVIDER_ALLOWED_HOSTS=("api.example.com", "cdn.example.com"))
class TaskEvidenceIdentityTests(TestCase):
    setUpTestData = classmethod(fixtures.ProductionMediaTests.setUpTestData.__func__)
    write_contract = staticmethod(fixtures.ProductionMediaTests.write_contract)
    job_row = fixtures.ProductionMediaTests.job_row
    resolve = fixtures.ProductionMediaTests.resolve

    @classmethod
    def setUpClass(cls):
        if (not sys.dont_write_bytecode or os.environ.get("PYTHON_DOTENV_DISABLED") != "1"
                or os.environ.get("DATABASE_URL") != "sqlite:///:memory:"
                or connection.vendor != "sqlite" or "memory" not in str(connection.settings_dict["NAME"])):
            raise AssertionError("Use Python -B, disabled dotenv and an explicit in-memory SQLite database")
        for target in ("socket.socket.connect", "socket.socket.connect_ex", "socket.create_connection",
                "socket.getaddrinfo", "requests.sessions.Session.request", "studio.services.provider_registry.resolve_credential"):
            guard = cls.enterClassContext(patch(target, side_effect=AssertionError("M7 forbids external IO")))
            cls.addClassCleanup(guard.assert_not_called)
        super().setUpClass()

    def setUp(self):
        fixtures.ProductionMediaTests.setUp(self)
        self.client = APIClient()
        self.client.force_authenticate(self.actor)
        self.url = f"/api/v1/task-center/preparation/{self.preparation.pk}/"

    def artifact(self, index=1):
        relative = Path("generated-artifacts", str(self.actor.pk), str(self.project.pk), "m7-fixture", f"{index}.png")
        path = Path(self.media_root) / relative
        path.parent.mkdir(parents=True, exist_ok=True)
        Image.new("RGB", (4, 3), (index, 20, 30)).save(path, format="PNG")
        raw = path.read_bytes()
        metadata = json_contract({"schema_version": 1, "source": "restricted_async_rest", "tenant_owner_id": str(self.actor.pk),
            "endpoint": "https://api.example.com/private", "token": "isolated-secret"})
        artifact = GeneratedArtifact.objects.create(manifest_key=f"m7-evidence:{index}", production_run=self.run,
            stage_run=self.stage, generation_attempt=self.attempt, kind="image", role="primary_generation_output",
            variant_group=f"image:{index}", variant_index=1, status="available", storage_backend="managed_local",
            object_key=relative.as_posix(), mime_type="image/png", size_bytes=len(raw), sha256=sha256(raw).hexdigest(),
            manifest_metadata=metadata.value, manifest_schema_version=metadata.schema_version,
            manifest_metadata_size_bytes=metadata.size_bytes, manifest_metadata_sha256=metadata.sha256)
        return artifact, path, raw

    def detail(self, **query):
        with CaptureQueriesContext(connection) as queries:
            response = self.client.get(self.url, query)
        self.assertEqual(response.status_code, 200, response.data)
        self.assertFalse(any(q["sql"].lstrip().upper().startswith(("INSERT", "UPDATE", "DELETE")) for q in queries))
        return response.data

    def test_foreign_run_stage_or_task_does_not_become_this_calls_output(self):
        artifact, _, _ = self.artifact()
        other = get_user_model().objects.create_user(username="m7-evidence-other")
        project = SeriesProject.objects.create(owner=other, title="Other tenant", language="Chinese", aspect_ratio="9:16",
            width=768, height=1344, fps=24, default_shot_duration_ms=5000, continuity_seed=1)
        run = ProductionRun.objects.create(owner=other, project=project, plan_source="manual", idempotency_key=str(uuid4()), request_sha256="b" * 64)
        stage = StageRun.objects.create(production_run=run, stage_key="media_generation", ordinal=1,
            mode="existing_pipeline", status="running", idempotency_key=str(uuid4()))
        task = GenerationTask.objects.create(owner=other, project=project, kind="image", status="succeeded")
        original = {"production_run_id": artifact.production_run_id, "stage_run_id": artifact.stage_run_id,
            "generation_task_id": artifact.generation_task_id}
        for changed in ({"production_run_id": run.pk}, {"stage_run_id": stage.pk}, {"generation_task_id": task.pk}):
            with self.subTest(changed=changed):
                GeneratedArtifact.objects.filter(pk=artifact.pk).update(**original)
                GeneratedArtifact.objects.filter(pk=artifact.pk).update(**changed)
                result = self.detail(attempt_id=str(self.attempt.pk))
                self.assertEqual(result["output"]["state"], "error")
                self.assertEqual(result["validation"]["error_code"], "task_center_output_invalid")
                self.assertIsNone(result["output"]["result"])
                self.assertEqual(result["output"]["media"], [])
                artifact.refresh_from_db()
                for field, value in changed.items():
                    self.assertEqual(getattr(artifact, field), value, "Read must not silently repair original rows")

    def test_exact_output_page_and_signed_file_identity_are_read_only(self):
        first, path, raw = self.artifact(1)
        second, _, _ = self.artifact(2)
        result = self.detail(attempt_id=str(self.attempt.pk), output_page_size=1)
        self.assertEqual(result["source_id"], str(self.attempt.pk))
        self.assertEqual(result["output_count"], 2)
        self.assertEqual(result["output"]["media"][0]["id"], str(first.pk))
        self.assertEqual(self.detail(output_page=2, output_page_size=1)["output"]["media"][0]["id"], str(second.pk))
        self.assertNotIn("isolated-secret", str(result))
        self.assertNotIn("https://api.example.com/private", str(result))
        url = result["output"]["media"][0]["preview_url"]
        self.assertTrue(url.startswith("/api/v1/media/"))
        response = APIClient().get(url)
        self.assertEqual(response.status_code, 200)
        self.assertEqual(b"".join(response.streaming_content), raw)
        response.close()
        self.assertEqual(path.read_bytes(), raw)
        wrong = signed_media_token(purpose="generated_artifact", object_id=first.pk, owner_id=uuid4())
        self.assertEqual(APIClient().get(f"/api/v1/media/{wrong}/").status_code, 404)

    def test_missing_empty_changed_bytes_or_invalid_status_never_stream_success(self):
        artifact, path, raw = self.artifact()
        url = self.detail()["output"]["media"][0]["preview_url"]
        self.assertTrue(path.resolve().is_relative_to(Path(self.media_root).resolve()))
        for mode in ("missing", "empty", "changed", "invalid"):
            with self.subTest(mode=mode):
                path.write_bytes(raw)
                GeneratedArtifact.objects.filter(pk=artifact.pk).update(status="available")
                if mode == "missing":
                    path.unlink()  # This explicitly bounded TemporaryDirectory fixture only.
                elif mode == "empty":
                    path.write_bytes(b"")
                elif mode == "changed":
                    Image.new("RGB", (4, 3), (99, 20, 30)).save(path, format="PNG")
                else:
                    GeneratedArtifact.objects.filter(pk=artifact.pk).update(status="invalid")
                self.assertEqual(APIClient().get(url).status_code, 404)
                artifact.refresh_from_db()
                self.assertEqual(artifact.sha256, sha256(raw).hexdigest())

    def test_manifest_damage_is_explicit_and_no_record_is_not_success(self):
        result = self.detail()
        self.assertEqual(result["output"]["state"], "unavailable")
        self.assertEqual(result["output_count"], 0)
        artifact, _, _ = self.artifact()
        GeneratedArtifact.objects.filter(pk=artifact.pk).update(manifest_metadata_sha256="0" * 64)
        result = self.detail()
        self.assertEqual(result["output"]["state"], "error")
        self.assertEqual(result["output"]["media"], [])
        artifact.refresh_from_db()
        self.assertEqual(artifact.manifest_metadata_sha256, "0" * 64)

    def test_historical_attempt_never_uses_successors_output_and_foreign_attempt_is_404(self):
        first, _, _ = self.artifact()
        latest = GenerationAttempt.objects.create(stage_run=self.stage, preparation=self.preparation,
            execution_instance_version=self.version, role="primary", unit_key=self.attempt.unit_key,
            attempt_number=2, operation=self.attempt.operation, status="failed", request_sha256=self.attempt.request_sha256)
        self.assertEqual(self.detail()["source_id"], str(latest.pk))
        self.assertEqual(self.detail()["output_count"], 0)
        historical = self.detail(attempt_id=str(self.attempt.pk))
        self.assertEqual(historical["source_id"], str(self.attempt.pk))
        self.assertEqual(historical["output"]["media"][0]["id"], str(first.pk))
        self.assertEqual(self.client.get(self.url, {"attempt_id": str(uuid4())}).status_code, 404)
        self.client.force_authenticate(get_user_model().objects.create_user(username="m7-no-permission"))
        self.assertEqual(self.client.get(self.url, {"attempt_id": str(self.attempt.pk)}).status_code, 404)
