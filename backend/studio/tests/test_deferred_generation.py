from pathlib import Path
import tempfile
from types import SimpleNamespace
from uuid import uuid4

from django.test import TestCase
from rest_framework.test import APIClient

from studio.services.deferred_generation import _create_derived_frame_asset
from studio.tests.factories import build_story


class DeferredGenerationDiagnosticAPITests(TestCase):
    """Historical deferred chains and tasks expose diagnostics, never mutations."""

    def setUp(self):
        self.user, *_rest = build_story("deferred-read-only")
        self.client = APIClient()
        self.client.force_authenticate(self.user)

    def test_deferred_collection_is_read_only(self):
        response = self.client.get("/api/v1/deferred-generation-requests/")
        self.assertEqual(response.status_code, 200, response.data)
        self.assertEqual(response.data["count"], 0)
        posted = self.client.post(
            "/api/v1/deferred-generation-requests/",
            {"idempotency_key": "public-write-must-not-exist"},
            format="json",
        )
        self.assertEqual(posted.status_code, 405, posted.data)

    def test_deferred_and_task_mutation_actions_are_unroutable(self):
        deferred_id = "11111111-1111-4111-8111-111111111111"
        task_id = "22222222-2222-4222-8222-222222222222"
        for path in (
            f"/api/v1/deferred-generation-requests/{deferred_id}/cancel/",
            f"/api/v1/tasks/{task_id}/cancel/",
            f"/api/v1/tasks/{task_id}/retry/",
        ):
            with self.subTest(path=path):
                self.assertEqual(self.client.post(path, {}, format="json").status_code, 404)

    def test_derived_frame_asset_records_request_task_and_frame_source(self):
        user, project, _character, _scene, _episode, shot, _task = build_story(
            "deferred-source-info",
        )
        request_id = uuid4()
        source_task_id = uuid4()
        request = SimpleNamespace(
            id=request_id,
            owner=user,
            project=project,
            frame_slot="first_frame",
            target_shot=shot,
            target_shot_id=shot.id,
            source_task_id=source_task_id,
            source_output_role="primary_video",
            request_fingerprint="a" * 64,
            target_input_fingerprint="b" * 64,
        )
        tracked = []
        guard = SimpleNamespace(track=tracked.append)
        source_output = {
            "output_index": 0,
            "artifact_sha256": "c" * 64,
            "manifest_sha256": "d" * 64,
        }

        with tempfile.TemporaryDirectory() as workspace, self.settings(
            MEDIA_ROOT=Path(workspace) / "media",
        ):
            frame_path = Path(workspace) / "frame.png"
            frame_path.write_bytes(b"verified-frame-fixture")
            asset = _create_derived_frame_asset(
                request=request,
                frame_path=frame_path,
                source_output=source_output,
                file_guard=guard,
            )

        self.assertEqual(
            asset.source_info,
            (
                f"延迟连续生成请求 {request_id}"
                f" · 上游生成任务 {source_task_id} · 自动首帧"
            ),
        )
        self.assertEqual(asset.metadata["deferred_generation"]["frame_slot"], "first_frame")
        self.assertEqual(len(tracked), 1)
