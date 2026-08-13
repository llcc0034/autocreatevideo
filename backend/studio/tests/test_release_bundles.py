from pathlib import Path
import tempfile
from uuid import uuid4
from unittest.mock import patch

from django.contrib.auth import get_user_model
from django.test import TestCase, override_settings
from django.utils import timezone
from rest_framework.test import APIClient
from PIL import Image

from studio.models import (
    GeneratedWorkflow,
    GenerationTask,
    PromptArtifact,
    ReleaseBundle,
    Shot,
    WorkflowTemplate,
)
from studio.services.h3_prompts import build_consistent_image_prompt
from studio.services.provenance import persist_task_provenance
from studio.services.distribution import distribution_artifact_path, prepare_task_distribution
from studio.services.release_bundles import (
    ReleaseBundleError,
    _ordered_assembly_source_pairs,
    asset_is_release_evidence,
    mark_release_bundle_released,
)
from studio.tests.factories import build_story


class ReleaseBundleAPITests(TestCase):
    def setUp(self):
        self.temp = tempfile.TemporaryDirectory()
        self.addCleanup(self.temp.cleanup)
        root = Path(self.temp.name)
        self.comfy_output = root / "comfy-output"
        self.media_root = root / "media"
        self.comfy_output.mkdir()
        self.override = override_settings(
            COMFYUI_OUTPUT_DIR=str(self.comfy_output), MEDIA_ROOT=self.media_root,
        )
        self.override.enable()
        self.addCleanup(self.override.disable)

        self.user, self.project, character, _, self.episode, self.shot, shot2 = build_story(
            "release-owner",
        )
        shot2.delete()
        self.shot.media_type = Shot.MediaType.IMAGE
        self.shot.save(update_fields=("media_type", "updated_at"))
        self.source_asset = character.anchor_asset
        self.client = APIClient()
        self.client.force_authenticate(self.user)
        self.template = WorkflowTemplate.objects.create(
            name="release-image", slug="release-image", output_kind="image",
            model="SDXL 1.0", input_schema={},
            definition={"1": {"class_type": "MockImage", "inputs": {}}},
        )
        self.task = self._successful_image_task()

    def _successful_image_task(self):
        result = build_consistent_image_prompt(self.project, self.shot)
        prompt = PromptArtifact.objects.create(
            project=self.project, episode=self.episode, shot=self.shot,
            created_by=self.user, prompt_type="image", h3_mode="T2VA",
            prompt_text=result.text, total_duration_ms=0,
            structured_payload=result.payload,
            consistency_snapshot=result.consistency_snapshot,
            content_hash=result.content_hash,
            request_options={"shot_ids": [str(self.shot.id)]},
        )
        workflow = GeneratedWorkflow.objects.create(
            project=self.project, episode=self.episode, prompt_artifact=prompt,
            template=self.template, created_by=self.user,
            workflow_json=self.template.definition, content_hash="a" * 64,
        )
        output = self.comfy_output / "episode-1" / "release-shot.png"
        output.parent.mkdir(parents=True)
        Image.new("RGB", (640, 360), (32, 54, 76)).save(output, "PNG")
        task = GenerationTask.objects.create(
            owner=self.user, project=self.project, episode=self.episode,
            generated_workflow=workflow, kind=GenerationTask.Kind.IMAGE,
            status=GenerationTask.Status.SUCCEEDED, progress=100,
            finished_at=timezone.now(),
            staged_inputs=[{
                "asset_id": str(self.source_asset.id),
                "media_role": "reference_image",
                "source_sha256": "1" * 64,
                "submitted_sha256": "2" * 64,
            }],
            outputs=[{
                "kind": "images", "filename": output.name,
                "subfolder": "episode-1", "type": "output",
            }],
        )
        persist_task_provenance(task, list(task.outputs), generated_at=task.finished_at)
        task.refresh_from_db()
        prepare_task_distribution(task, 0)
        task.refresh_from_db()
        return task

    def _create(self):
        response = self.client.post("/api/v1/release-bundles/", {
            "project_id": str(self.project.id),
        }, format="json")
        self.assertEqual(response.status_code, 201, response.data)
        return response

    def test_draft_proposes_every_shot_and_seals_an_immutable_signed_manifest(self):
        created = self._create()
        self.assertEqual(created.data["status"], "draft")
        self.assertEqual(created.data["version"], 1)
        self.assertEqual(created.data["selection"]["shots"], [{
            "shot_id": str(self.shot.id),
            "task_id": str(self.task.id),
            "output_index": 0,
        }])

        sealed = self.client.post(
            f"/api/v1/release-bundles/{created.data['id']}/seal/", {}, format="json",
        )
        self.assertEqual(sealed.status_code, 200, sealed.data)
        self.assertEqual(sealed.data["status"], "sealed")
        self.assertEqual(sealed.data["verification_status"], "verified")
        self.assertRegex(sealed.data["manifest_sha256"], r"^[0-9a-f]{64}$")
        artifact = sealed.data["manifest"]["artifacts"][0]
        self.assertEqual(artifact["task_id"], str(self.task.id))
        self.assertEqual(artifact["distribution_label"], "AI-generated · SDXL 1.0")
        self.assertEqual(artifact["distribution_model_key"], "sdxl-1.0")
        self.assertEqual(artifact["distribution_model"], "SDXL 1.0")
        self.assertEqual(artifact["distribution_version"], "shot-public-v1")
        self.assertNotEqual(
            artifact["artifact_sha256"], artifact["source_artifact_sha256"],
        )
        self.assertEqual(
            artifact["source_media"][0]["asset_id"], str(self.source_asset.id),
        )

        verified = self.client.get(
            f"/api/v1/release-bundles/{created.data['id']}/verify/",
        )
        self.assertEqual(verified.status_code, 200, verified.data)
        self.assertEqual(verified.data["verification"]["status"], "verified")
        self.assertIs(verified.data["verification"]["signature_verified"], True)
        self.assertIs(verified.data["verification"]["live_verified"], True)
        self.assertEqual(
            self.client.patch(
                f"/api/v1/release-bundles/{created.data['id']}/",
                {"title": "should fail"}, format="json",
            ).status_code,
            400,
        )
        self.assertEqual(
            self.client.delete(f"/api/v1/release-bundles/{created.data['id']}/").status_code,
            400,
        )

    def test_seal_rejects_missing_shot_and_tenant_cannot_select_other_project(self):
        created = self._create()
        patched = self.client.patch(
            f"/api/v1/release-bundles/{created.data['id']}/",
            {"selection": {"shots": [], "episodes": [], "series_assembly_id": None}},
            format="json",
        )
        self.assertEqual(patched.status_code, 200, patched.data)
        sealed = self.client.post(
            f"/api/v1/release-bundles/{created.data['id']}/seal/", {}, format="json",
        )
        self.assertEqual(sealed.status_code, 400, sealed.data)
        self.assertIn(
            str(self.shot.id),
            sealed.data["error"]["details"]["details"]["missing_shot_ids"],
        )

        other, other_project, *_ = build_story("release-other")
        forbidden = self.client.post("/api/v1/release-bundles/", {
            "project_id": str(other_project.id),
        }, format="json")
        self.assertEqual(forbidden.status_code, 404, forbidden.data)

    def test_seal_rejects_a_shot_without_verified_managed_distribution_copy(self):
        outputs = list(self.task.outputs)
        outputs[0] = dict(outputs[0])
        outputs[0].pop("distribution", None)
        self.task.outputs = outputs
        self.task.save(update_fields=("outputs", "updated_at"))
        created = self._create()

        sealed = self.client.post(
            f"/api/v1/release-bundles/{created.data['id']}/seal/", {}, format="json",
        )

        self.assertEqual(sealed.status_code, 400, sealed.data)
        self.assertIn("managed_distribution_invalid", str(sealed.data))
        self.assertIn("/workflows?task=", str(sealed.data))
        self.assertIn("publish-output", str(sealed.data))

    def test_distribution_tamper_after_seal_blocks_staff_release(self):
        created = self._create()
        bundle_id = created.data["id"]
        self.assertEqual(
            self.client.post(
                f"/api/v1/release-bundles/{bundle_id}/seal/", {}, format="json",
            ).status_code,
            200,
        )
        self.task.refresh_from_db()
        public = distribution_artifact_path(self.task, 0)
        public.write_bytes(public.read_bytes() + b"tamper")
        live_verification = self.client.get(
            f"/api/v1/release-bundles/{bundle_id}/verify/",
        )
        self.assertEqual(live_verification.status_code, 400, live_verification.data)
        self.assertIn("managed_distribution_invalid", str(live_verification.data))
        staff = get_user_model().objects.create_user(username="release-tamper-staff", is_staff=True)
        self.client.force_authenticate(staff)

        released = self.client.post(
            f"/api/v1/release-bundles/{bundle_id}/release/", {}, format="json",
        )

        self.assertEqual(released.status_code, 400, released.data)
        self.assertIn("managed_distribution_invalid", str(released.data))

    def test_tamper_is_detected_and_only_staff_can_mark_released(self):
        created = self._create()
        bundle_id = created.data["id"]
        self.assertEqual(
            self.client.post(f"/api/v1/release-bundles/{bundle_id}/seal/", {}, format="json").status_code,
            200,
        )
        self.assertEqual(
            self.client.post(f"/api/v1/release-bundles/{bundle_id}/release/", {}, format="json").status_code,
            403,
        )
        staff = get_user_model().objects.create_user(username="release-staff", is_staff=True)
        self.client.force_authenticate(staff)
        released = self.client.post(
            f"/api/v1/release-bundles/{bundle_id}/release/", {}, format="json",
        )
        self.assertEqual(released.status_code, 200, released.data)
        self.assertEqual(released.data["status"], "released")

        bundle = ReleaseBundle.objects.get(pk=bundle_id)
        bundle.manifest["title"] = "tampered"
        ReleaseBundle.objects.filter(pk=bundle.pk).update(manifest=bundle.manifest)
        invalid = self.client.get(f"/api/v1/release-bundles/{bundle_id}/verify/")
        self.assertEqual(invalid.status_code, 400, invalid.data)

    def test_release_evidence_blocks_task_and_source_asset_deletion(self):
        created = self._create()
        self.assertEqual(
            self.client.post(
                f"/api/v1/release-bundles/{created.data['id']}/seal/", {}, format="json",
            ).status_code,
            200,
        )
        task_delete = self.client.delete(f"/api/v1/tasks/{self.task.id}/")
        self.assertEqual(task_delete.status_code, 400, task_delete.data)
        self.assertIn("发行清单", str(task_delete.data))
        asset_delete = self.client.delete(f"/api/v1/assets/{self.source_asset.id}/")
        self.assertEqual(asset_delete.status_code, 400, asset_delete.data)
        self.assertIn("审计证据", str(asset_delete.data))

    def test_foreign_task_uuid_is_reported_as_unavailable_without_scope_oracle(self):
        other_user, other_project, _, _, other_episode, *_ = build_story("release-foreign-task")
        foreign_task = GenerationTask.objects.create(
            owner=other_user,
            project=other_project,
            episode=other_episode,
            generated_workflow=self.task.generated_workflow,
            kind=GenerationTask.Kind.IMAGE,
            status=GenerationTask.Status.SUCCEEDED,
            progress=100,
            outputs=[],
        )
        created = self._create()
        selection = dict(created.data["selection"])
        selection["shots"] = [{
            "shot_id": str(self.shot.id),
            "task_id": str(foreign_task.id),
            "output_index": 0,
        }]
        patched = self.client.patch(
            f"/api/v1/release-bundles/{created.data['id']}/",
            {"selection": selection}, format="json",
        )
        self.assertEqual(patched.status_code, 200, patched.data)

        sealed = self.client.post(
            f"/api/v1/release-bundles/{created.data['id']}/seal/", {}, format="json",
        )

        self.assertEqual(sealed.status_code, 400, sealed.data)
        self.assertIn("task_unavailable", str(sealed.data))
        self.assertNotIn("task_scope", str(sealed.data))
        self.assertNotIn("task_missing", str(sealed.data))

    def test_signed_task_provenance_not_mutable_prompt_controls_shot_binding(self):
        second_shot = Shot.objects.create(
            episode=self.episode,
            number=2,
            title="second image",
            narrative="A second release still",
            media_type=Shot.MediaType.IMAGE,
            include_audio=False,
        )
        prompt = self.task.generated_workflow.prompt_artifact
        prompt.request_options = {"shot_ids": [str(self.shot.id), str(second_shot.id)]}
        prompt.save(update_fields=("request_options", "updated_at"))
        created = self._create()
        selection = dict(created.data["selection"])
        selection["shots"] = [
            {
                "shot_id": str(self.shot.id),
                "task_id": str(self.task.id),
                "output_index": 0,
            },
            {
                "shot_id": str(second_shot.id),
                "task_id": str(self.task.id),
                "output_index": 0,
            },
        ]
        patched = self.client.patch(
            f"/api/v1/release-bundles/{created.data['id']}/",
            {"selection": selection}, format="json",
        )
        self.assertEqual(patched.status_code, 200, patched.data)

        sealed = self.client.post(
            f"/api/v1/release-bundles/{created.data['id']}/seal/", {}, format="json",
        )

        self.assertEqual(sealed.status_code, 400, sealed.data)
        self.assertIn("task_provenance_binding", str(sealed.data))

    def test_older_sealed_version_cannot_supersede_a_newer_release(self):
        first = self._create()
        self.assertEqual(self.client.post(
            f"/api/v1/release-bundles/{first.data['id']}/seal/", {}, format="json",
        ).status_code, 200)
        second = self._create()
        self.assertEqual(self.client.post(
            f"/api/v1/release-bundles/{second.data['id']}/seal/", {}, format="json",
        ).status_code, 200)
        staff = get_user_model().objects.create_user(
            username="release-version-staff", is_staff=True,
        )
        self.client.force_authenticate(staff)
        newer = self.client.post(
            f"/api/v1/release-bundles/{second.data['id']}/release/", {}, format="json",
        )
        self.assertEqual(newer.status_code, 200, newer.data)

        older = self.client.post(
            f"/api/v1/release-bundles/{first.data['id']}/release/", {}, format="json",
        )

        self.assertEqual(older.status_code, 400, older.data)
        self.assertIn("更高版本", str(older.data))
        self.assertEqual(
            ReleaseBundle.objects.get(pk=second.data["id"]).status,
            ReleaseBundle.Status.RELEASED,
        )

    def test_release_unique_constraint_race_has_stable_service_error(self):
        from django.db import IntegrityError

        bundle = ReleaseBundle.objects.get(pk=self._create().data["id"])
        with patch(
            "studio.services.release_bundles._mark_release_bundle_released_transaction",
            side_effect=IntegrityError("one_released_bundle_per_project"),
        ):
            with self.assertRaises(ReleaseBundleError) as raised:
                mark_release_bundle_released(bundle)
        self.assertEqual(raised.exception.details["code"], "release_transition_conflict")

    def test_global_asset_referenced_by_sealed_manifest_remains_release_evidence(self):
        created = self._create()
        self.assertEqual(self.client.post(
            f"/api/v1/release-bundles/{created.data['id']}/seal/", {}, format="json",
        ).status_code, 200)
        self.source_asset.owner = None
        self.source_asset.save(update_fields=("owner", "updated_at"))

        self.assertIs(asset_is_release_evidence(self.source_asset), True)

    def test_assembly_source_rows_bind_exact_shot_order_even_for_same_task_output(self):
        first_shot_id = str(uuid4())
        second_shot_id = str(uuid4())
        task_id = str(uuid4())
        normal, error = _ordered_assembly_source_pairs({"source_outputs": [
            {"clip_index": 0, "shot_id": first_shot_id, "task_id": task_id, "output_index": 0},
            {"clip_index": 1, "shot_id": second_shot_id, "task_id": task_id, "output_index": 0},
        ]})
        self.assertIsNone(error)
        self.assertEqual(normal, [
            (first_shot_id, task_id, 0),
            (second_shot_id, task_id, 0),
        ])

        swapped, error = _ordered_assembly_source_pairs({"source_outputs": [
            {"clip_index": 0, "shot_id": second_shot_id, "task_id": task_id, "output_index": 0},
            {"clip_index": 1, "shot_id": first_shot_id, "task_id": task_id, "output_index": 0},
        ]})
        self.assertIsNone(error)
        self.assertNotEqual(swapped, normal)

        historical, error = _ordered_assembly_source_pairs({"source_outputs": [
            {"clip_index": 0, "task_id": task_id, "output_index": 0},
        ]})
        self.assertIsNone(historical)
        self.assertEqual(error, "assembly_source_shot_ids_missing_reassemble")
