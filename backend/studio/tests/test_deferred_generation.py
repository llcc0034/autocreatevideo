from __future__ import annotations

import hashlib
import tempfile
from pathlib import Path
from unittest.mock import patch
from uuid import uuid4

from django.core.management import call_command
from django.test import TestCase
from django.utils import timezone
from rest_framework.test import APIClient

from studio.models import (
    DeferredGenerationRequest,
    GeneratedWorkflow,
    GenerationTask,
    PromptArtifact,
    Shot,
    WorkflowTemplate,
)
from studio.services.production_pipeline import materialize_generation_task
from studio.services.deferred_generation import (
    active_first_frame_deferred_for,
    claim_next_deferred_request,
    create_deferred_generation_request,
    ensure_next_first_frame_deferred_for_task,
    previous_video_shot_for,
    process_deferred_request,
)
from studio.services.system_workflow_contracts import video_output_contract
from studio.services.workflow import WorkflowBuildError
from studio.tests.factories import build_story


def _video_definition(node_id="14"):
    return {str(node_id): {"class_type": "SaveVideo", "inputs": {"video": ["1", 0]}}}


class DeferredGenerationRequestAPITests(TestCase):
    def setUp(self):
        call_command("seed_data", verbosity=0)
        (
            self.user,
            self.project,
            _character,
            _scene,
            self.episode,
            self.source_shot,
            self.target_shot,
        ) = build_story("deferred-owner")
        self.target_shot.h3_mode = Shot.H3Mode.FL2VA
        self.target_shot.duration_ms = 5000
        self.target_shot.save(update_fields=("h3_mode", "duration_ms", "updated_at"))
        self.client = APIClient()
        self.client.force_authenticate(self.user)
        self.source_task = self._source_task()
        self.url = "/api/v1/deferred-generation-requests/"

    def test_previous_video_shot_never_crosses_storyboard_versions(self):
        version_key = "creative-draft:44444444-4444-4444-8444-444444444444"
        first = Shot.objects.create(
            episode=self.episode,
            number=3,
            title="V2 镜头 1",
            narrative="新版本第一镜。",
            camera="中景",
            duration_ms=5000,
            h3_mode=Shot.H3Mode.REF2VA,
            metadata={
                "creative_version_key": version_key,
                "creative_version_label": "V2",
                "creative_version_shot_number": 1,
                "creative_version_active": True,
            },
        )
        second = Shot.objects.create(
            episode=self.episode,
            number=4,
            title="V2 镜头 2",
            narrative="新版本第二镜。",
            camera="中景",
            duration_ms=5000,
            h3_mode=Shot.H3Mode.REF2VA,
            metadata={
                "creative_version_key": version_key,
                "creative_version_label": "V2",
                "creative_version_shot_number": 2,
                "creative_version_active": True,
            },
        )

        self.assertIsNone(previous_video_shot_for(first))
        self.assertEqual(previous_video_shot_for(second).id, first.id)

    def _template(self):
        return WorkflowTemplate.objects.filter(
            output_kind=WorkflowTemplate.OutputKind.VIDEO,
            is_active=True,
        ).first()

    def _source_task(self):
        template = self._template()
        prompt = PromptArtifact.objects.create(
            project=self.project,
            episode=self.episode,
            shot=self.source_shot,
            created_by=self.user,
            prompt_type=PromptArtifact.PromptType.VIDEO,
            model="minimax-h3",
            h3_mode=Shot.H3Mode.T2VA,
            revision=1,
            is_multi_shot=False,
            include_audio=True,
            total_duration_ms=self.source_shot.duration_ms,
            prompt_text="source prompt",
            negative_prompt="",
            structured_payload={"model": "minimax-h3"},
            consistency_snapshot={},
            request_options={"shot_ids": [str(self.source_shot.id)]},
            content_hash=hashlib.sha256(b"source prompt").hexdigest(),
        )
        workflow_json = _video_definition("14")
        workflow = GeneratedWorkflow.objects.create(
            project=self.project,
            episode=self.episode,
            prompt_artifact=prompt,
            template=template,
            created_by=self.user,
            workflow_json=workflow_json,
            injected_values={},
            content_hash=hashlib.sha256(b"workflow").hexdigest(),
        )
        return GenerationTask.objects.create(
            owner=self.user,
            project=self.project,
            episode=self.episode,
            generated_workflow=workflow,
            kind=GenerationTask.Kind.VIDEO,
            status=GenerationTask.Status.QUEUED,
            capability_snapshot={
                "output_contract": video_output_contract(workflow_json),
            },
        )

    def _payload(self, **overrides):
        payload = {
            "project_id": str(self.project.id),
            "source_task_id": str(self.source_task.id),
            "target_shot_id": str(self.target_shot.id),
            "frame_slot": "first_frame",
            "idempotency_key": "deferred-key-1",
        }
        payload.update(overrides)
        return payload

    def test_create_persists_only_deferred_request_and_provisional_prompt(self):
        before_prompts = PromptArtifact.objects.count()
        before_workflows = GeneratedWorkflow.objects.count()
        before_tasks = GenerationTask.objects.count()

        response = self.client.post(self.url, self._payload(), format="json")

        self.assertEqual(response.status_code, 201, response.data)
        self.assertEqual(response.data["status"], "waiting_upstream")
        self.assertEqual(response.data["source_output_role"], "primary_video")
        self.assertEqual(response.data["source_task_id"], str(self.source_task.id))
        self.assertEqual(response.data["target_shot_id"], str(self.target_shot.id))
        self.assertTrue(response.data["cancel_allowed"])
        self.assertFalse(response.data["retry_allowed"])
        self.assertIn("provisional_prompt_text", response.data)
        self.assertEqual(PromptArtifact.objects.count(), before_prompts)
        self.assertEqual(GeneratedWorkflow.objects.count(), before_workflows)
        self.assertEqual(GenerationTask.objects.count(), before_tasks)

    def test_same_key_replays_and_different_request_conflicts(self):
        first = self.client.post(self.url, self._payload(), format="json")
        self.assertEqual(first.status_code, 201, first.data)

        replay = self.client.post(self.url, self._payload(), format="json")
        self.assertEqual(replay.status_code, 200, replay.data)
        self.assertEqual(replay.data["id"], first.data["id"])

        conflict = self.client.post(
            self.url,
            self._payload(frame_slot="last_frame"),
            format="json",
        )
        self.assertEqual(conflict.status_code, 409, conflict.data)
        self.assertEqual(
            conflict.data["error"]["code"],
            "deferred_generation_idempotency_conflict",
        )

    def test_cancel_only_waiting_request(self):
        created = self.client.post(self.url, self._payload(), format="json")
        self.assertEqual(created.status_code, 201, created.data)

        cancelled = self.client.post(
            f"{self.url}{created.data['id']}/cancel/",
            {},
            format="json",
        )

        self.assertEqual(cancelled.status_code, 200, cancelled.data)
        self.assertEqual(cancelled.data["status"], "cancelled")
        self.assertFalse(cancelled.data["cancel_allowed"])

    def test_cancel_source_task_cancels_downstream_and_retry_restores_chain(self):
        self.source_shot.requires_last_frame_continuity = True
        self.source_shot.save(update_fields=("requires_last_frame_continuity", "updated_at"))
        self.target_shot.requires_first_frame_continuity = True
        self.target_shot.save(update_fields=("requires_first_frame_continuity", "updated_at"))
        created = self.client.post(self.url, self._payload(), format="json")
        self.assertEqual(created.status_code, 201, created.data)

        cancelled = self.client.post(
            f"/api/v1/tasks/{self.source_task.id}/cancel/",
            {},
            format="json",
        )

        self.assertEqual(cancelled.status_code, 200, cancelled.data)
        self.assertEqual(cancelled.data["status"], GenerationTask.Status.CANCELLED)
        self.assertEqual(
            cancelled.data["downstream_cancellations"]["deferred_requests_cancelled"],
            1,
        )
        old_deferred = DeferredGenerationRequest.objects.get(pk=created.data["id"])
        self.assertEqual(old_deferred.status, DeferredGenerationRequest.Status.CANCELLED)
        self.assertEqual(old_deferred.error_code, "deferred_source_cancelled_by_user")

        retried = self.client.post(
            f"/api/v1/tasks/{self.source_task.id}/retry/",
            {"extended_generation_confirmed": False},
            format="json",
        )
        self.assertEqual(retried.status_code, 201, retried.data)
        retry_task = GenerationTask.objects.get(pk=retried.data["id"])
        retry_task.status = GenerationTask.Status.SUCCEEDED
        retry_task.finished_at = timezone.now()
        retry_task.save(update_fields=("status", "finished_at", "updated_at"))

        restored = ensure_next_first_frame_deferred_for_task(retry_task)

        self.assertIsNotNone(restored)
        new_deferred, is_new = restored
        self.assertTrue(is_new)
        self.assertNotEqual(new_deferred.id, old_deferred.id)
        self.assertEqual(new_deferred.source_task_id, retry_task.id)
        self.assertEqual(new_deferred.target_shot_id, self.target_shot.id)
        self.assertEqual(new_deferred.status, DeferredGenerationRequest.Status.WAITING_UPSTREAM)

    def test_target_mode_must_support_requested_frame_slot(self):
        self.target_shot.h3_mode = Shot.H3Mode.T2VA
        self.target_shot.save(update_fields=("h3_mode", "updated_at"))

        response = self.client.post(self.url, self._payload(), format="json")

        self.assertEqual(response.status_code, 409, response.data)
        self.assertEqual(
            response.data["error"]["code"],
            "deferred_generation_target_incompatible",
        )

    def test_ref2va_first_frame_reserves_one_of_nine_picture_slots(self):
        self.target_shot.h3_mode = Shot.H3Mode.REF2VA
        self.target_shot.requires_first_frame_continuity = True
        self.target_shot.reference_bindings = {
            "characters": [
                {"entity_id": str(uuid4()), "asset_id": str(uuid4())}
                for _index in range(9)
            ],
        }
        self.target_shot.save(update_fields=(
            "h3_mode", "requires_first_frame_continuity",
            "reference_bindings", "updated_at",
        ))

        response = self.client.post(self.url, self._payload(), format="json")

        self.assertEqual(response.status_code, 409, response.data)
        self.assertEqual(
            response.data["error"]["code"],
            "deferred_generation_target_incompatible",
        )

    def test_task_materialization_uses_persisted_workflow_output_contract(self):
        source = self.source_task
        workflow = source.generated_workflow
        template = workflow.template
        template.definition = _video_definition("99")
        schema = dict(template.input_schema or {})
        schema["output_contract"] = video_output_contract(template.definition)
        template.input_schema = schema
        template.save(update_fields=("definition", "input_schema", "updated_at"))

        with self.assertRaises(WorkflowBuildError):
            materialize_generation_task(
                user=self.user,
                workflow=workflow,
                priority=0,
                max_attempts=3,
            )

    def test_deferred_claim_waits_until_source_task_is_terminal(self):
        response = self.client.post(self.url, self._payload(), format="json")
        self.assertEqual(response.status_code, 201, response.data)

        self.assertIsNone(claim_next_deferred_request("worker-1"))

    def test_successful_deferred_request_materializes_downstream_task_without_mutating_shot(self):
        response = self.client.post(self.url, self._payload(), format="json")
        self.assertEqual(response.status_code, 201, response.data)
        self.source_task.status = GenerationTask.Status.SUCCEEDED
        self.source_task.finished_at = timezone.now()
        self.source_task.save(update_fields=("status", "finished_at", "updated_at"))
        with tempfile.TemporaryDirectory() as temp_dir:
            frame = Path(temp_dir) / "frame.png"
            frame.write_bytes(b"\x89PNG\r\n\x1a\nfake-frame")
            source_output = {
                "output_index": 0,
                "path": Path(temp_dir) / "source.mp4",
                "artifact_sha256": "a" * 64,
                "manifest_sha256": "b" * 64,
                "manifest": {},
                "item": {},
            }
            claimed = claim_next_deferred_request("worker-1")
            self.assertIsNotNone(claimed)
            with patch(
                "studio.services.deferred_generation.resolve_declared_task_output",
                return_value=source_output,
            ), patch(
                "studio.services.deferred_generation.extract_last_decodable_frame",
                return_value=frame,
            ):
                result = process_deferred_request(claimed)

        result.refresh_from_db()
        self.assertEqual(
            result.status,
            DeferredGenerationRequest.Status.MATERIALIZED,
            result.error_message,
        )
        self.assertIsNotNone(result.derived_asset_id)
        self.assertIsNotNone(result.materialized_task_id)
        self.assertEqual(result.resolved_source_output_index, 0)
        self.assertEqual(result.source_artifact_sha256, "a" * 64)
        self.assertEqual(result.source_manifest_sha256, "b" * 64)
        self.target_shot.refresh_from_db()
        self.assertEqual(self.target_shot.reference_bindings, {})
        self.assertFalse(self.target_shot.reference_assets.exists())
        task = result.materialized_task
        self.assertEqual(task.status, GenerationTask.Status.QUEUED)
        deferred_snapshot = task.capability_snapshot["deferred_generation"]
        self.assertEqual(deferred_snapshot["request_id"], str(result.id))
        self.assertEqual(deferred_snapshot["derived_asset_id"], str(result.derived_asset_id))
        self.source_task.outputs = {}
        self.source_task.save(update_fields=("outputs", "updated_at"))

        detail = self.client.get(f"/api/v1/tasks/{self.source_task.id}/")
        self.assertEqual(detail.status_code, 200, detail.data)
        derived_media = [
            item for item in detail.data["output_media"]
            if item["kind"] == "image" and item["promoted_asset_id"] == str(result.derived_asset_id)
        ]
        self.assertEqual(len(derived_media), 1, detail.data["output_media"])
        self.assertTrue(derived_media[0]["filename"].endswith(".png"))
        self.assertTrue(derived_media[0]["preview_url"])
        self.assertEqual(derived_media[0]["public_label"], "尾帧抽取图")
        listing = self.client.get("/api/v1/tasks/")
        self.assertEqual(listing.status_code, 200, listing.data)
        rows = listing.data.get("results", listing.data)
        source_row = next(row for row in rows if row["id"] == str(self.source_task.id))
        self.assertTrue(any(
            item["kind"] == "image" and item["promoted_asset_id"] == str(result.derived_asset_id)
            for item in source_row["output_media"]
        ))

    def test_active_first_frame_deferred_is_scoped_by_request_key(self):
        snapshot = dict(self.source_task.capability_snapshot)
        snapshot.update({
            "batch_id": "batch-a",
            "batch_kind": "episode",
            "batch_index": 1,
            "batch_size": 2,
            "idempotency_key": "episode-request-a",
        })
        self.source_task.capability_snapshot = snapshot
        self.source_task.save(update_fields=("capability_snapshot", "updated_at"))
        response = self.client.post(self.url, self._payload(idempotency_key="auto-a"), format="json")
        self.assertEqual(response.status_code, 201, response.data)

        self.assertIsNotNone(
            active_first_frame_deferred_for(
                self.target_shot,
                request_key="episode-request-a",
            ),
        )
        self.assertIsNone(
            active_first_frame_deferred_for(
                self.target_shot,
                request_key="episode-request-b",
            ),
        )

    def test_deferred_materialization_preserves_batch_metadata_from_retry_source(self):
        snapshot = dict(self.source_task.capability_snapshot)
        snapshot.update({
            "batch_id": "retry-source-batch",
            "batch_kind": "episode",
            "batch_role": "retry",
            "batch_index": 1,
            "batch_size": 2,
            "episode_id": str(self.episode.id),
            "episode_number": self.episode.number,
            "episode_title": self.episode.title,
            "shot_ids": [str(self.source_shot.id)],
            "shot_id": str(self.source_shot.id),
            "shot_number": self.source_shot.number,
            "shot_title": self.source_shot.title,
            "idempotency_key": "retry-source-batch-key",
            "request_fingerprint": "a" * 64,
        })
        self.source_task.capability_snapshot = snapshot
        self.source_task.save(update_fields=("capability_snapshot", "updated_at"))
        response = self.client.post(self.url, self._payload(), format="json")
        self.assertEqual(response.status_code, 201, response.data)
        self.source_task.status = GenerationTask.Status.SUCCEEDED
        self.source_task.finished_at = timezone.now()
        self.source_task.save(update_fields=("status", "finished_at", "updated_at"))
        with tempfile.TemporaryDirectory() as temp_dir:
            frame = Path(temp_dir) / "frame.png"
            frame.write_bytes(b"\x89PNG\r\n\x1a\nfake-frame")
            source_output = {
                "output_index": 0,
                "path": Path(temp_dir) / "source.mp4",
                "artifact_sha256": "a" * 64,
                "manifest_sha256": "b" * 64,
                "manifest": {},
                "item": {},
            }
            claimed = claim_next_deferred_request("worker-1")
            self.assertIsNotNone(claimed)
            with patch(
                "studio.services.deferred_generation.resolve_declared_task_output",
                return_value=source_output,
            ), patch(
                "studio.services.deferred_generation.extract_last_decodable_frame",
                return_value=frame,
            ):
                result = process_deferred_request(claimed)

        result.refresh_from_db()
        task_snapshot = result.materialized_task.capability_snapshot
        self.assertEqual(task_snapshot["batch_id"], "retry-source-batch")
        self.assertEqual(task_snapshot["batch_role"], "child")
        self.assertEqual(task_snapshot["batch_index"], 2)
        self.assertEqual(task_snapshot["batch_size"], 2)
        self.assertEqual(task_snapshot["shot_id"], str(self.target_shot.id))

    def test_ref2va_first_frame_request_extracts_source_tail_into_soft_reference_slot(self):
        self.target_shot.h3_mode = Shot.H3Mode.REF2VA
        self.target_shot.requires_first_frame_continuity = True
        self.target_shot.reference_bindings = {}
        self.target_shot.save(update_fields=(
            "h3_mode", "requires_first_frame_continuity",
            "reference_bindings", "updated_at",
        ))

        response = self.client.post(self.url, self._payload(), format="json")
        self.assertEqual(response.status_code, 201, response.data)
        self.source_task.status = GenerationTask.Status.SUCCEEDED
        self.source_task.finished_at = timezone.now()
        self.source_task.save(update_fields=("status", "finished_at", "updated_at"))
        with tempfile.TemporaryDirectory() as temp_dir:
            frame = Path(temp_dir) / "frame.png"
            frame.write_bytes(b"\x89PNG\r\n\x1a\nfake-frame")
            source_output = {
                "output_index": 0,
                "path": Path(temp_dir) / "source.mp4",
                "artifact_sha256": "a" * 64,
                "manifest_sha256": "b" * 64,
                "manifest": {},
                "item": {},
            }
            claimed = claim_next_deferred_request("worker-1")
            self.assertIsNotNone(claimed)
            with patch(
                "studio.services.deferred_generation.resolve_declared_task_output",
                return_value=source_output,
            ), patch(
                "studio.services.deferred_generation.extract_last_decodable_frame",
                return_value=frame,
            ) as extract_frame:
                result = process_deferred_request(claimed)

        result.refresh_from_db()
        self.assertEqual(
            result.status,
            DeferredGenerationRequest.Status.MATERIALIZED,
            result.error_message,
        )
        extract_frame.assert_called_once_with(
            source_output["path"],
            DeferredGenerationRequest.FrameSlot.LAST_FRAME,
        )
        self.target_shot.refresh_from_db()
        self.assertEqual(self.target_shot.reference_bindings, {})
        self.assertFalse(self.target_shot.reference_assets.exists())
        prompt = result.materialized_task.generated_workflow.prompt_artifact
        records = prompt.structured_payload["reference_bindings"]
        self.assertEqual(records[0]["category"], "continuity_frame")
        self.assertEqual(records[0]["input_label"], "<Picture 1>")
        self.assertEqual(records[0]["asset_id"], str(result.derived_asset_id))
        self.assertIn("previous shot", prompt.prompt_text)

    def test_deferred_materialization_preserves_target_template_resolution_and_parameters(self):
        self.target_shot.h3_mode = Shot.H3Mode.REF2VA
        self.target_shot.requires_first_frame_continuity = True
        self.target_shot.reference_bindings = {}
        self.target_shot.save(update_fields=(
            "h3_mode", "requires_first_frame_continuity",
            "reference_bindings", "updated_at",
        ))
        template = WorkflowTemplate.objects.get(
            owner=None,
            slug="minimax-h3-r2v-turbo-lora",
        )
        overrides = {
            "RESOLUTION_PROFILE": "0.2",
            "RESOLUTION_MULTIPLE": 32,
            "WIDTH": 352,
            "HEIGHT": 608,
            "STEPS": 8,
            "LORA_STRENGTH": 0.65,
        }
        deferred, created = create_deferred_generation_request(
            user=self.user,
            project=self.project,
            source_task=self.source_task,
            target_shot=self.target_shot,
            frame_slot=DeferredGenerationRequest.FrameSlot.FIRST_FRAME,
            idempotency_key="deferred-target-overrides",
            preferred_template=template,
            target_workflow_overrides=overrides,
        )
        self.assertTrue(created)
        self.assertEqual(deferred.target_template_id, template.id)
        self.assertEqual(deferred.target_workflow_overrides, overrides)
        self.source_task.status = GenerationTask.Status.SUCCEEDED
        self.source_task.finished_at = timezone.now()
        self.source_task.save(update_fields=("status", "finished_at", "updated_at"))
        with tempfile.TemporaryDirectory() as temp_dir:
            frame = Path(temp_dir) / "frame.png"
            frame.write_bytes(b"\x89PNG\r\n\x1a\nfake-frame")
            source_output = {
                "output_index": 0,
                "path": Path(temp_dir) / "source.mp4",
                "artifact_sha256": "a" * 64,
                "manifest_sha256": "b" * 64,
                "manifest": {},
                "item": {},
            }
            claimed = claim_next_deferred_request("worker-1")
            self.assertIsNotNone(claimed)
            with patch(
                "studio.services.deferred_generation.resolve_declared_task_output",
                return_value=source_output,
            ), patch(
                "studio.services.deferred_generation.extract_last_decodable_frame",
                return_value=frame,
            ):
                result = process_deferred_request(claimed)

        result.refresh_from_db()
        self.assertEqual(
            result.status,
            DeferredGenerationRequest.Status.MATERIALIZED,
            result.error_message,
        )
        workflow = result.materialized_task.generated_workflow
        self.assertEqual(workflow.template_id, template.id)
        self.assertEqual(workflow.injected_values["RESOLUTION_PROFILE"], "0.2")
        self.assertEqual(workflow.injected_values["RESOLUTION_MULTIPLE"], 32)
        self.assertEqual((workflow.injected_values["WIDTH"], workflow.injected_values["HEIGHT"]), (352, 608))
        self.assertEqual(workflow.injected_values["STEPS"], 8)
        self.assertEqual(workflow.injected_values["LORA_STRENGTH"], 0.65)
        snapshot = result.materialized_task.capability_snapshot
        self.assertEqual(snapshot["resolution_profile"], "0.2")
        self.assertEqual(snapshot["resolution_multiple"], 32)
        self.assertEqual((snapshot["width"], snapshot["height"]), (352, 608))
        self.assertEqual(snapshot["workflow_parameters"], {"STEPS": 8, "LORA_STRENGTH": 0.65})

    def test_failed_source_terminalizes_deferred_request(self):
        response = self.client.post(self.url, self._payload(), format="json")
        self.assertEqual(response.status_code, 201, response.data)
        self.source_task.status = GenerationTask.Status.FAILED
        self.source_task.finished_at = timezone.now()
        self.source_task.save(update_fields=("status", "finished_at", "updated_at"))

        claimed = claim_next_deferred_request("worker-1")
        self.assertIsNotNone(claimed)
        result = process_deferred_request(claimed)

        self.assertEqual(result.status, DeferredGenerationRequest.Status.FAILED)
        self.assertEqual(result.error_code, "deferred_source_failed")


class DeferredGenerationDeletionGuardTests(TestCase):
    def setUp(self):
        call_command("seed_data", verbosity=0)
        (
            self.user,
            self.project,
            _character,
            _scene,
            self.episode,
            self.source_shot,
            self.target_shot,
        ) = build_story("deferred-delete")
        self.target_shot.h3_mode = Shot.H3Mode.FL2VA
        self.target_shot.duration_ms = 5000
        self.target_shot.save(update_fields=("h3_mode", "duration_ms", "updated_at"))
        self.client = APIClient()
        self.client.force_authenticate(self.user)
        self.source_task = DeferredGenerationRequestAPITests._source_task(self)
        response = self.client.post("/api/v1/deferred-generation-requests/", {
            "project_id": str(self.project.id),
            "source_task_id": str(self.source_task.id),
            "target_shot_id": str(self.target_shot.id),
            "frame_slot": "first_frame",
            "idempotency_key": "delete-guard",
        }, format="json")
        self.assertEqual(response.status_code, 201, response.data)

    def _template(self):
        return WorkflowTemplate.objects.filter(
            output_kind=WorkflowTemplate.OutputKind.VIDEO,
            is_active=True,
        ).first()

    def test_source_task_destroy_is_rejected_with_explicit_error(self):
        self.source_task.status = GenerationTask.Status.SUCCEEDED
        self.source_task.finished_at = timezone.now()
        self.source_task.save(update_fields=("status", "finished_at", "updated_at"))

        response = self.client.delete(f"/api/v1/tasks/{self.source_task.id}/")

        self.assertEqual(response.status_code, 400, response.data)
        self.assertIn("延迟首尾帧", str(response.data))

    def test_project_delete_is_rejected_while_deferred_history_exists(self):
        self.source_task.status = GenerationTask.Status.SUCCEEDED
        self.source_task.finished_at = timezone.now()
        self.source_task.save(update_fields=("status", "finished_at", "updated_at"))

        response = self.client.delete(f"/api/v1/projects/{self.project.id}/")

        self.assertEqual(response.status_code, 409, response.data)
        self.assertEqual(response.data["error"]["code"], "project_has_active_work")
