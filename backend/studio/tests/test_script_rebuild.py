from uuid import uuid4

from django.test import TestCase
from rest_framework.test import APIClient

from studio.models import (
    ConsistencyLock,
    GeneratedWorkflow,
    GenerationTask,
    PromptArtifact,
    PromptFieldTranslation,
    ReleaseBundle,
    Shot,
    WorkflowTemplate,
)
from studio.services.h3_prompts import sync_consistency_locks
from studio.tests.factories import build_story


class ScriptRebuildAPITests(TestCase):
    def setUp(self):
        (
            self.user,
            self.project,
            self.character,
            self.scene,
            self.episode,
            self.shot,
            self.shot2,
        ) = build_story("script-rebuild-owner")
        self.project.master_script = (
            "第1集：当前版本\n"
            "镜头1：旧档案室内，林默展开证物。\n"
            "林默：这封信不是昨晚烧的。\n"
            "镜头2：林默望向门口。\n"
            "林默：别动。"
        )
        self.project.save(update_fields=("master_script", "updated_at"))
        self.client = APIClient()
        self.client.force_authenticate(self.user)
        self.template = WorkflowTemplate.objects.create(
            name="script-rebuild-template",
            slug="script-rebuild-template",
            output_kind=PromptArtifact.PromptType.VIDEO,
            is_system=True,
            definition={"1": {"class_type": "SaveVideo", "inputs": {}}},
        )

    def _create_terminal_task(self, status=GenerationTask.Status.SUCCEEDED):
        prompt = PromptArtifact.objects.create(
            project=self.project,
            episode=self.episode,
            shot=self.shot,
            created_by=self.user,
            prompt_type=PromptArtifact.PromptType.VIDEO,
            prompt_text="old prompt",
            total_duration_ms=self.shot.duration_ms,
            content_hash="a" * 64,
        )
        workflow = GeneratedWorkflow.objects.create(
            project=self.project,
            episode=self.episode,
            prompt_artifact=prompt,
            template=self.template,
            created_by=self.user,
            workflow_json=self.template.definition,
            content_hash="b" * 64,
        )
        task = GenerationTask.objects.create(
            owner=self.user,
            project=self.project,
            episode=self.episode,
            generated_workflow=workflow,
            kind=GenerationTask.Kind.VIDEO,
            status=status,
            progress=100 if status == GenerationTask.Status.SUCCEEDED else 0,
            remote_state=(
                GenerationTask.RemoteState.SUCCEEDED
                if status == GenerationTask.Status.SUCCEEDED else ""
            ),
            outputs=[{"filename": "retained-output.mp4"}],
        )
        return prompt, workflow, task

    def _create_project_level_task(
        self,
        *,
        request_shot_ids=None,
        status=GenerationTask.Status.RUNNING,
    ):
        prompt = PromptArtifact.objects.create(
            project=self.project,
            created_by=self.user,
            prompt_type=PromptArtifact.PromptType.VIDEO,
            prompt_text="project-level prompt",
            total_duration_ms=self.shot.duration_ms,
            request_options={
                "shot_ids": [str(value) for value in (request_shot_ids or [])],
            },
            content_hash="e" * 64,
        )
        workflow = GeneratedWorkflow.objects.create(
            project=self.project,
            prompt_artifact=prompt,
            template=self.template,
            created_by=self.user,
            workflow_json=self.template.definition,
            content_hash="f" * 64,
        )
        task = GenerationTask.objects.create(
            owner=self.user,
            project=self.project,
            generated_workflow=workflow,
            kind=GenerationTask.Kind.VIDEO,
            status=status,
            progress=100 if status == GenerationTask.Status.SUCCEEDED else 0,
            remote_state=(
                GenerationTask.RemoteState.SUCCEEDED
                if status == GenerationTask.Status.SUCCEEDED else ""
            ),
        )
        return prompt, workflow, task

    def _translation(self, scope_type, scope_id, field_key):
        return PromptFieldTranslation.objects.create(
            project=self.project,
            scope_type=scope_type,
            scope_id=scope_id,
            field_key=field_key,
            source_text="source",
            source_hash="c" * 64,
            translated_text="translation",
            status=PromptFieldTranslation.Status.CONFIRMED,
        )

    def _payload(self, *, discard):
        return {
            "script": self.project.master_script,
            "episode_count_hint": 1,
            "default_shot_duration_ms": 5000,
            "multi_shot": True,
            "include_audio": True,
            "replace_existing": True,
            "discard_existing_production": discard,
        }

    def test_confirmed_rebuild_discards_terminal_lineage_and_orphan_translations(self):
        prompt, workflow, task = self._create_terminal_task()
        shot_translation = self._translation(
            PromptFieldTranslation.ScopeType.SHOT,
            self.shot.id,
            "shot.narrative",
        )
        orphan_character_translation = self._translation(
            PromptFieldTranslation.ScopeType.CHARACTER,
            uuid4(),
            "character.visual_identity",
        )
        project_translation = self._translation(
            PromptFieldTranslation.ScopeType.PROJECT,
            self.project.id,
            "project.continuity_summary",
        )
        draft_release = ReleaseBundle.objects.create(
            owner=self.user,
            project=self.project,
            created_by=self.user,
            version=1,
            title="discardable draft",
            status=ReleaseBundle.Status.DRAFT,
        )
        old_episode_id = self.episode.id

        confirmation = self.client.post(
            f"/api/v1/projects/{self.project.id}/breakdown-script/",
            self._payload(discard=False),
            format="json",
        )
        self.assertEqual(confirmation.status_code, 409, confirmation.data)
        self.assertEqual(
            confirmation.data["error"]["code"],
            "script_rebuild_confirmation_required",
        )
        self.assertTrue(GenerationTask.objects.filter(pk=task.id).exists())

        response = self.client.post(
            f"/api/v1/projects/{self.project.id}/breakdown-script/",
            self._payload(discard=True),
            format="json",
        )

        self.assertEqual(response.status_code, 201, response.data)
        self.assertEqual(response.data["counts"], {"episodes": 1, "shots": 2})
        self.assertEqual(response.data["cleared"]["episodes"], 1)
        self.assertEqual(response.data["cleared"]["shots"], 2)
        self.assertEqual(response.data["cleared"]["generation_tasks"], 1)
        self.assertEqual(response.data["cleared"]["prompt_field_translations"], 2)
        self.assertTrue(response.data["cleared"]["physical_outputs_retained"])
        self.assertFalse(self.project.episodes.filter(pk=old_episode_id).exists())
        self.assertFalse(GenerationTask.objects.filter(pk=task.id).exists())
        self.assertFalse(GeneratedWorkflow.objects.filter(pk=workflow.id).exists())
        self.assertFalse(PromptArtifact.objects.filter(pk=prompt.id).exists())
        self.assertFalse(PromptFieldTranslation.objects.filter(pk=shot_translation.id).exists())
        self.assertFalse(
            PromptFieldTranslation.objects.filter(pk=orphan_character_translation.id).exists()
        )
        self.assertTrue(PromptFieldTranslation.objects.filter(pk=project_translation.id).exists())
        self.assertFalse(ReleaseBundle.objects.filter(pk=draft_release.id).exists())
        rebuilt_shot = self.project.episodes.get(number=1).shots.get(number=1)
        self.assertEqual(
            list(rebuilt_shot.characters.values_list("id", flat=True)),
            [self.character.id],
        )

    def test_active_task_blocks_rebuild_without_partial_cleanup(self):
        prompt, workflow, task = self._create_terminal_task(
            GenerationTask.Status.RUNNING,
        )
        old_episode_id = self.episode.id

        response = self.client.post(
            f"/api/v1/projects/{self.project.id}/breakdown-script/",
            self._payload(discard=True),
            format="json",
        )

        self.assertEqual(response.status_code, 409, response.data)
        self.assertEqual(response.data["error"]["code"], "script_rebuild_blocked")
        self.assertEqual(response.data["error"]["details"]["active_generation_tasks"], 1)
        self.assertTrue(self.project.episodes.filter(pk=old_episode_id).exists())
        self.assertTrue(GenerationTask.objects.filter(pk=task.id).exists())
        self.assertTrue(GeneratedWorkflow.objects.filter(pk=workflow.id).exists())
        self.assertTrue(PromptArtifact.objects.filter(pk=prompt.id).exists())

    def test_unrelated_project_level_task_is_preserved_and_does_not_block(self):
        prompt, workflow, task = self._create_project_level_task()

        response = self.client.post(
            f"/api/v1/projects/{self.project.id}/breakdown-script/",
            self._payload(discard=True),
            format="json",
        )

        self.assertEqual(response.status_code, 201, response.data)
        self.assertEqual(response.data["cleared"]["generation_tasks"], 0)
        self.assertTrue(GenerationTask.objects.filter(pk=task.id).exists())
        self.assertTrue(GeneratedWorkflow.objects.filter(pk=workflow.id).exists())
        self.assertTrue(PromptArtifact.objects.filter(pk=prompt.id).exists())

    def test_legacy_project_level_prompt_with_old_shot_ids_is_discarded(self):
        prompt, workflow, task = self._create_project_level_task(
            request_shot_ids=[self.shot.id, self.shot2.id],
            status=GenerationTask.Status.SUCCEEDED,
        )

        confirmation = self.client.post(
            f"/api/v1/projects/{self.project.id}/breakdown-script/",
            self._payload(discard=False),
            format="json",
        )
        self.assertEqual(confirmation.status_code, 409, confirmation.data)
        self.assertEqual(
            confirmation.data["error"]["details"]["clearable"]["generation_tasks"],
            1,
        )

        response = self.client.post(
            f"/api/v1/projects/{self.project.id}/breakdown-script/",
            self._payload(discard=True),
            format="json",
        )

        self.assertEqual(response.status_code, 201, response.data)
        self.assertFalse(GenerationTask.objects.filter(pk=task.id).exists())
        self.assertFalse(GeneratedWorkflow.objects.filter(pk=workflow.id).exists())
        self.assertFalse(PromptArtifact.objects.filter(pk=prompt.id).exists())

    def test_sealed_release_blocks_rebuild(self):
        release = ReleaseBundle.objects.create(
            owner=self.user,
            project=self.project,
            created_by=self.user,
            version=1,
            title="sealed evidence",
            status=ReleaseBundle.Status.SEALED,
            manifest={"version": 1},
            manifest_sha256="d" * 64,
        )

        response = self.client.post(
            f"/api/v1/projects/{self.project.id}/breakdown-script/",
            self._payload(discard=True),
            format="json",
        )

        self.assertEqual(response.status_code, 409, response.data)
        self.assertEqual(
            response.data["error"]["details"]["protected_release_bundles"],
            1,
        )
        self.assertTrue(ReleaseBundle.objects.filter(pk=release.id).exists())
        self.assertTrue(self.project.episodes.filter(pk=self.episode.id).exists())

    def test_bible_delete_prunes_live_json_translation_and_identity_lock(self):
        self.shot.reference_bindings = {
            "characters": [{
                "entity_id": str(self.character.id),
                "asset_id": str(self.character.anchor_asset_id),
            }],
        }
        self.shot.metadata = {"reference_bindings": self.shot.reference_bindings}
        self.shot.save(update_fields=("reference_bindings", "metadata", "updated_at"))
        translation = self._translation(
            PromptFieldTranslation.ScopeType.CHARACTER,
            self.character.id,
            "character.visual_identity",
        )
        sync_consistency_locks(self.project)
        lock = ConsistencyLock.objects.get(
            project=self.project,
            scope=ConsistencyLock.Scope.CHARACTER,
            target_id=self.character.id,
            lock_key="identity",
        )

        response = self.client.post(
            f"/api/v1/projects/{self.project.id}/sync-bibles/",
            {"characters": []},
            format="json",
        )

        self.assertEqual(response.status_code, 200, response.data)
        self.shot.refresh_from_db()
        lock.refresh_from_db()
        self.assertEqual(self.shot.reference_bindings, {})
        self.assertNotIn("reference_bindings", self.shot.metadata)
        self.assertFalse(PromptFieldTranslation.objects.filter(pk=translation.id).exists())
        self.assertFalse(lock.is_active)
