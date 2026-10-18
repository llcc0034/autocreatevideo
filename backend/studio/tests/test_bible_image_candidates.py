from django.test import TestCase, override_settings
from rest_framework.test import APIClient

from studio.models import ExecutionPreparation, GenerationTask, GeneratedArtifact
from studio.tests.factories import build_story


@override_settings(AI_PROVIDER_ALLOWED_HOSTS=("127.0.0.1",))
class BibleExecutionCandidateTests(TestCase):
    """Original workflow/archive services with isolated bytes, no real model."""
    from studio.tests.test_execution_production_workflow_artifacts import ProductionWorkflowArtifactTests as _Fixture
    mark_submitted = _Fixture.mark_submitted
    archive = _Fixture.archive

    def setUp(self):
        self._Fixture.setUp(self)
        self.client = APIClient()
        self.client.force_authenticate(self.user)
        self.url = f"/api/v1/projects/{self.project.pk}/bible-image-candidates/"

    def resolve(self, definition, stage):
        from studio.models import PropBible
        from studio.services.execution_request_preparation import resolve_execution_request_facts
        from studio.services.json_contracts import json_contract
        from studio.services.production_stage_catalog import production_stage_catalog

        self.entity = PropBible.objects.create(project=self.project, slug="actual-key", name="铜钥匙",
            description=getattr(self, "target_description", "旧铜钥匙"))
        from studio.services.bible_variants import create_variant
        self.primary = create_variant(actor=self.user, project_id=self.project.pk, entity_kind="prop", entity_id=self.entity.pk,
            appearance_role="primary", primary_setting_revision_id=None, primary_setting_sha256="",
            value={"name": "铜钥匙主图", "reference_name": "key_primary", "identity_anchor": "铜制钥匙",
                "appearance_anchor": "方形握柄", "basis_asset_id": None, "basis_asset_sha256": ""})
        stage.stage_key = "bible_image_generation"
        stage.ordinal = next(row["ordinal"] for row in production_stage_catalog().value["stages"] if row["key"] == stage.stage_key)
        stage.save(update_fields=("stage_key", "ordinal"))
        return resolve_execution_request_facts(definition, self.intent.value, owner=self.user, unit_key="image-unit",
            max_media_bytes=65536, parameter_policies=[], resolve_parameter_source=lambda _: self.fail("No implicit source"),
            review_contract=json_contract({"schema_version": 1, "mode": "manual"}), project_id=self.project.pk,
            production_run_id=str(self.run.pk), production_scope={"kind": "prop", "id": str(self.entity.pk)})

    def test_real_candidate_uses_preparation_identity_and_reports_changed_source(self):
        artifact, = self.archive()
        before = (ExecutionPreparation.objects.count(), GenerationTask.objects.count(), GeneratedArtifact.objects.count())
        response = self.client.get(self.url)
        self.assertEqual(response.status_code, 200, response.data)
        self.assertEqual(response["Cache-Control"], "private, no-store")
        entity, = response.data["candidates"]
        candidate, = entity["candidate_history"]
        self.assertEqual(str(entity["entity_id"]), str(self.entity.pk))
        self.assertEqual(str(candidate["artifact_id"]), str(artifact.pk))
        self.assertEqual(str(candidate["preparation_id"]), str(self.preparation.pk))
        self.assertEqual(str(candidate["execution_instance_version_id"]), str(self.version.pk))
        self.assertEqual(candidate["request_sha256"], self.preparation.request_sha256)
        self.assertEqual(candidate["integrity_status"], "valid")
        self.assertTrue(candidate["source_current"])
        self.assertTrue(candidate["preview_url"])
        for old in ("prompt_artifact_id", "prompt_profile_binding_key", "source_snapshot_sha256"):
            self.assertNotIn(old, candidate)
        from unittest.mock import patch
        from studio.models import PropBible
        from studio.services.execution_production_candidates import read_production_media_candidate
        for number in range(2):
            PropBible.objects.create(project=self.project, slug=f"zz-other-{number}", name=f"另一道具{number}")
        with patch("studio.services.bible_image_candidates.read_production_media_candidate", wraps=read_production_media_candidate) as verify:
            expanded = self.client.get(self.url)
        self.assertEqual(expanded.status_code, 200, expanded.data)
        self.assertEqual(expanded.data["counts"]["total"], 3)
        self.assertEqual(expanded.data["counts"]["with_candidate"], 1)
        self.assertEqual(verify.call_count, 1)
        self.entity.description = "人工后来修改的外观"
        self.entity.save(update_fields=("description",))
        changed = self.client.get(self.url)
        self.assertEqual(changed.status_code, 200, changed.data)
        candidate = changed.data["candidates"][0]["latest_candidate"]
        self.assertFalse(candidate["source_current"])
        self.assertEqual(candidate["integrity_status"], "valid")
        self.assertEqual(before, (ExecutionPreparation.objects.count(), GenerationTask.objects.count(), GeneratedArtifact.objects.count()))

    def test_late_or_corrupt_candidate_cannot_offer_a_verified_preview(self):
        artifact, = self.archive()
        self.attempt.ignore_late_result = True
        self.attempt.save(update_fields=("ignore_late_result",))
        response = self.client.get(self.url)
        self.assertEqual(response.status_code, 200, response.data)
        candidate = response.data["candidates"][0]["latest_candidate"]
        self.assertEqual(candidate["integrity_status"], "invalid")
        self.assertEqual(candidate["preview_url"], "")
        self.assertEqual(candidate["download_url"], "")
        self.assertTrue(candidate["integrity_error_code"])
        from pathlib import Path
        from django.conf import settings
        self.attempt.ignore_late_result = False
        self.attempt.save(update_fields=("ignore_late_result",))
        (Path(settings.MEDIA_ROOT) / artifact.object_key).write_bytes(b"corrupted isolated image")
        response = self.client.get(self.url)
        self.assertEqual(response.status_code, 200, response.data)
        candidate = response.data["candidates"][0]["latest_candidate"]
        self.assertEqual(candidate["integrity_status"], "invalid")
        self.assertEqual(candidate["preview_url"], "")
        # Primary settings protect the entity. A broken current pointer is
        # still an explicit conflict, never absence or an old-content fallback.
        from django.db.models.deletion import ProtectedError
        from studio.models import BibleVariant
        with self.assertRaises(ProtectedError):
            self.entity.delete()
        BibleVariant.objects.filter(pk=self.primary.pk).update(revision=99)
        response = self.client.get(self.url)
        self.assertEqual(response.status_code, 409, response.data)

    def test_candidate_history_does_not_disclose_another_owners_asset(self):
        from django.contrib.auth import get_user_model
        from studio.models import Asset

        other = get_user_model().objects.create_user(username="other-bible-owner", password="isolated-only")
        foreign = Asset.objects.create(owner=other, kind="reference", name="Other private asset")
        self.entity.anchor_asset = foreign
        self.entity.save(update_fields=("anchor_asset",))
        response = self.client.get(self.url)
        self.assertEqual(response.status_code, 409, response.data)
        self.assertNotIn(foreign.name, str(response.data))
        self.client.force_authenticate(other)
        self.assertEqual(self.client.get(self.url).status_code, 404)


class BibleImageCandidateReadOnlySurfaceTests(TestCase):
    def setUp(self):
        user, self.project, _, _, _, _, _ = build_story()
        self.client = APIClient()
        self.client.force_authenticate(user)
        self.url = f"/api/v1/projects/{self.project.id}/bible-image-candidates/"

    def test_candidate_projection_is_read_only(self):
        response = self.client.get(self.url)

        self.assertEqual(response.status_code, 200, response.data)
        self.assertIn("candidates", response.data)
        before = (
            ExecutionPreparation.objects.count(),
            GeneratedArtifact.objects.count(),
            GenerationTask.objects.count(),
        )
        rejected = self.client.post(self.url, {}, format="json")
        self.assertEqual(rejected.status_code, 405, rejected.data)
        self.assertEqual(before, (
            ExecutionPreparation.objects.count(),
            GeneratedArtifact.objects.count(),
            GenerationTask.objects.count(),
        ))

    def test_low_level_workflow_and_task_creation_are_closed(self):
        self.assertEqual(
            self.client.post("/api/v1/workflows/generate/", {}, format="json").status_code,
            404,
        )
        self.assertEqual(
            self.client.post("/api/v1/tasks/", {}, format="json").status_code,
            405,
        )
