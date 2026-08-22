from copy import deepcopy
from datetime import timedelta
from pathlib import Path
import tempfile
from unittest.mock import patch
import uuid

from django.core.management import call_command
from django.db import transaction
from django.test import TestCase, override_settings
from django.utils import timezone
from rest_framework.test import APIClient

from studio.models import (
    Asset,
    GeneratedWorkflow,
    GenerationTask,
    PromptArtifact,
    PromptFieldTranslation,
    PropBible,
    WorkflowTemplate,
)
from studio.services.h3_prompts import SDXL_BASE_MODEL_ID
from studio.services.production_pipeline import materialize_generation_task
from studio.services.prompt_translation import (
    TRANSLATION_CONTEXT_VERSION,
    build_translation_context,
    source_text_hash,
    translation_context_hash,
)
from studio.services.provenance import persist_task_provenance, verified_task_manifest
from studio.services.workflow import WorkflowBuildError
from studio.tests.factories import build_story


class BibleImageCandidateAPITests(TestCase):
    def setUp(self):
        self.temp = tempfile.TemporaryDirectory()
        self.addCleanup(self.temp.cleanup)
        self.output_root = Path(self.temp.name) / "comfy-output"
        self.output_root.mkdir()
        self.media_root = Path(self.temp.name) / "media"
        self.override = override_settings(
            COMFYUI_OUTPUT_DIR=str(self.output_root),
            MEDIA_ROOT=self.media_root,
        )
        self.override.enable()
        self.addCleanup(self.override.disable)

        call_command("seed_data", verbosity=0)
        (
            self.user,
            self.project,
            self.character,
            self.scene,
            self.episode,
            self.shot,
            _,
        ) = build_story()
        self.prop = PropBible.objects.create(
            project=self.project,
            slug="burned-letter",
            name="烧焦信纸",
            description="关键证物",
            visual_identity="边缘焦黑、右下角有蓝色蜡封",
            material="泛黄棉纸",
            distinctive_marks="蓝色蜡封",
            continuity_state="折叠但未撕裂",
        )
        self.client = APIClient()
        self.client.force_authenticate(self.user)
        self.url = f"/api/v1/projects/{self.project.id}/bible-image-candidates/"

    def _post(self, entity_type, entity, key, **extra):
        return self.client.post(self.url, {
            "entity_type": entity_type,
            "entity_id": str(entity.id),
            "idempotency_key": key,
            **extra,
        }, format="json")

    def _create(self, entity_type="character", entity=None, key="candidate-1", **extra):
        entity = entity or self.character
        response = self._post(entity_type, entity, key, **extra)
        self.assertEqual(response.status_code, 201, response.data)
        return response, GenerationTask.objects.get(
            pk=response.data["generation_task_id"],
        )

    def _assert_candidate_task_projection(self, payload, task):
        self.assertNotIn("capability_snapshot", payload)
        self.assertEqual(payload["generation_purpose"], "bible_image_candidate")
        self.assertEqual(payload["bible_target"], task.capability_snapshot["bible_target"])

    def _succeed_with_sealed_image(self, task, *, filename="candidate.png"):
        path = self.output_root / filename
        path.write_bytes(b"reviewed-bible-image")
        task.status = GenerationTask.Status.SUCCEEDED
        task.progress = 100
        task.finished_at = timezone.now()
        task.outputs = [{
            "kind": "images",
            "filename": path.name,
            "subfolder": "",
            "type": "output",
        }]
        task.save(update_fields=(
            "status", "progress", "finished_at", "outputs", "updated_at",
        ))
        persist_task_provenance(task, list(task.outputs), generated_at=task.finished_at)
        task.refresh_from_db()
        return path

    def _confirmed_character_translation(self, translated_text):
        field_key = "character.visual_identity"
        source = self.character.visual_identity
        return PromptFieldTranslation.objects.create(
            project=self.project,
            scope_type=PromptFieldTranslation.ScopeType.CHARACTER,
            scope_id=self.character.id,
            field_key=field_key,
            source_text=source,
            source_hash=source_text_hash(source),
            translated_text=translated_text,
            status=PromptFieldTranslation.Status.CONFIRMED,
            revision=1,
            runtime_metadata={
                "translation_context_hash": translation_context_hash(
                    build_translation_context(
                        self.project,
                        scope_type=PromptFieldTranslation.ScopeType.CHARACTER,
                        scope_id=self.character.id,
                        field_key=field_key,
                        source_text=source,
                    )
                ),
                "translation_context_version": TRANSLATION_CONTEXT_VERSION,
            },
        )

    def test_three_entity_types_use_the_fixed_text_only_system_template(self):
        for index, (entity_type, entity) in enumerate((
            ("character", self.character),
            ("scene", self.scene),
            ("prop", self.prop),
        ), start=1):
            with self.subTest(entity_type=entity_type):
                response, task = self._create(
                    entity_type, entity, f"three-types-{index}",
                )
                prompt = PromptArtifact.objects.get(
                    pk=response.data["prompt_artifact_id"],
                )
                workflow = GeneratedWorkflow.objects.get(
                    pk=response.data["generated_workflow_id"],
                )
                self.assertEqual(workflow.template.slug, "minimax-reference-image")
                self.assertTrue(workflow.template.is_system)
                self.assertIsNone(workflow.template.owner_id)
                self.assertEqual(prompt.structured_payload["model"], SDXL_BASE_MODEL_ID)
                self.assertEqual(
                    prompt.structured_payload["prompt_template_key"],
                    "sdxl_txt2img_bible_v1",
                )
                self.assertFalse(prompt.structured_payload["pixel_reference_inputs"])
                self.assertEqual(prompt.structured_payload["reference_bindings"], [])
                self.assertNotIn("LoadImage", str(workflow.workflow_json))
                self.assertEqual(
                    task.capability_snapshot["bible_target"]["reference_category"],
                    entity_type,
                )
                self.assertTrue(response.data["review_required"])
                self.assertFalse(response.data["persisted_as_asset"])

    def test_project_creation_fills_default_bible_image_prompt_config(self):
        response = self.client.post("/api/v1/projects/", {
            "title": "默认参考图配置",
            "style": str(self.project.style_id),
            "target_model": "minimax-h3",
            "language": "Chinese",
            "aspect_ratio": "9:16",
            "width": 768,
            "height": 1344,
            "fps": 24,
            "production_settings": {},
        }, format="json")

        self.assertEqual(response.status_code, 201, response.data)
        settings = response.data["production_settings"]
        config = settings["bible_image_prompt_config"]
        self.assertEqual(config["schema_version"], 1)
        self.assertEqual(
            config["style_prompt"],
            self.project.style.visual_prompt,
        )
        self.assertIn("character", config["composition_rules"])
        self.assertIn("prop", config["negative_rules"])

    def test_task_list_and_detail_project_queued_and_succeeded_candidate_safely(self):
        _, task = self._create(key="task-projection")

        detail = self.client.get(f"/api/v1/tasks/{task.id}/")
        self.assertEqual(detail.status_code, 200, detail.data)
        self._assert_candidate_task_projection(detail.data, task)

        listed = self.client.get("/api/v1/tasks/")
        self.assertEqual(listed.status_code, 200, listed.data)
        rows = listed.data.get("results", listed.data)
        row = next(item for item in rows if str(item["id"]) == str(task.id))
        self._assert_candidate_task_projection(row, task)

        task.status = GenerationTask.Status.SUCCEEDED
        task.progress = 100
        task.finished_at = timezone.now()
        task.save(update_fields=(
            "status", "progress", "finished_at", "updated_at",
        ))
        succeeded = self.client.get(f"/api/v1/tasks/{task.id}/")
        self.assertEqual(succeeded.status_code, 200, succeeded.data)
        self._assert_candidate_task_projection(succeeded.data, task)

    def test_task_projection_keeps_candidate_identity_when_frozen_target_is_invalid(self):
        _, task = self._create(key="tampered-task-target")
        snapshot = deepcopy(task.capability_snapshot)
        snapshot["bible_target"] = {
            **snapshot["bible_target"],
            "source_fingerprint": "not-a-sha256",
            "private_extra": "must-not-leak",
        }
        task.capability_snapshot = snapshot
        task.save(update_fields=("capability_snapshot", "updated_at"))

        tampered = self.client.get(f"/api/v1/tasks/{task.id}/")
        self.assertEqual(tampered.status_code, 200, tampered.data)
        self.assertEqual(tampered.data["generation_purpose"], "bible_image_candidate")
        self.assertIsNone(tampered.data["bible_target"])
        self.assertNotIn("capability_snapshot", tampered.data)
        self.assertNotIn("private_extra", str(tampered.data))

        # The projection is derived only from the task's frozen snapshot.  A
        # live candidate Prompt must not make an ordinary snapshot look special.
        task.capability_snapshot = {}
        task.save(update_fields=("capability_snapshot", "updated_at"))
        ordinary = self.client.get(f"/api/v1/tasks/{task.id}/")
        self.assertEqual(ordinary.status_code, 200, ordinary.data)
        self.assertIsNone(ordinary.data["generation_purpose"])
        self.assertIsNone(ordinary.data["bible_target"])
        self.assertNotIn("capability_snapshot", ordinary.data)

    def test_request_rejects_client_selected_production_fields(self):
        for field in ("template", "model", "workflow"):
            with self.subTest(field=field):
                response = self._post(
                    "character", self.character, f"unsafe-{field}", **{field: "x"},
                )
                self.assertEqual(response.status_code, 400, response.data)
                self.assertEqual(response.data["error"]["code"], "invalid")
        self.assertEqual(PromptArtifact.objects.count(), 0)
        self.assertEqual(GeneratedWorkflow.objects.count(), 0)
        self.assertEqual(GenerationTask.objects.count(), 0)

    def test_latest_misconfigured_system_template_returns_503_without_fallback(self):
        current = WorkflowTemplate.objects.get(
            owner=None, slug="minimax-reference-image", is_active=True,
        )
        WorkflowTemplate.objects.create(
            owner=None,
            name="broken newer image system template",
            slug=current.slug,
            version=current.version + 1,
            provider=current.provider,
            output_kind=current.output_kind,
            model="unreviewed-other-model.safetensors",
            definition=current.definition,
            input_schema=current.input_schema,
            is_system=True,
            is_active=True,
        )

        response = self._post("character", self.character, "bad-template")

        self.assertEqual(response.status_code, 503, response.data)
        self.assertEqual(
            response.data["error"]["code"],
            "bible_image_candidate_template_unavailable",
        )
        self.assertEqual(PromptArtifact.objects.count(), 0)
        self.assertEqual(GeneratedWorkflow.objects.count(), 0)
        self.assertEqual(GenerationTask.objects.count(), 0)

    def test_newer_system_template_with_pixel_input_returns_503_without_writes(self):
        current = WorkflowTemplate.objects.get(
            owner=None, slug="minimax-reference-image", is_active=True,
        )
        definition = deepcopy(current.definition)
        definition["99"] = {
            "class_type": "LoadImage",
            "inputs": {"image": "untrusted.png"},
        }
        WorkflowTemplate.objects.create(
            owner=None,
            name="unsafe newer image system template",
            slug=current.slug,
            version=current.version + 1,
            provider=current.provider,
            output_kind=current.output_kind,
            model=current.model,
            definition=definition,
            input_schema=current.input_schema,
            is_system=True,
            is_active=True,
        )

        response = self._post("character", self.character, "pixel-template")

        self.assertEqual(response.status_code, 503, response.data)
        self.assertEqual(
            response.data["error"]["code"],
            "bible_image_candidate_template_unavailable",
        )
        self.assertEqual(PromptArtifact.objects.count(), 0)
        self.assertEqual(GeneratedWorkflow.objects.count(), 0)
        self.assertEqual(GenerationTask.objects.count(), 0)

    def test_reviewed_definition_rejects_changed_text_sampler_or_link_without_writes(self):
        current = WorkflowTemplate.objects.get(
            owner=None, slug="minimax-reference-image", is_active=True,
        )
        cases = (
            ("clip-text", "2", "text", "${NEGATIVE_PROMPT}"),
            ("sampler-steps", "5", "steps", 31),
            ("vae-link", "6", "samples", ["4", 0]),
        )
        for index, (label, node_id, input_key, value) in enumerate(cases, start=1):
            with self.subTest(label=label):
                definition = deepcopy(current.definition)
                definition[node_id]["inputs"][input_key] = value
                unsafe = WorkflowTemplate.objects.create(
                    owner=None,
                    name=f"unsafe reviewed graph {label}",
                    slug=current.slug,
                    version=current.version + index,
                    provider=current.provider,
                    output_kind=current.output_kind,
                    model=current.model,
                    definition=definition,
                    input_schema=current.input_schema,
                    is_system=True,
                    is_active=True,
                )

                response = self._post(
                    "character", self.character, f"unsafe-definition-{label}",
                )

                self.assertEqual(response.status_code, 503, response.data)
                self.assertEqual(
                    response.data["error"]["code"],
                    "bible_image_candidate_template_unavailable",
                )
                self.assertEqual(PromptArtifact.objects.count(), 0)
                self.assertEqual(GeneratedWorkflow.objects.count(), 0)
                self.assertEqual(GenerationTask.objects.count(), 0)
                unsafe.delete()

    def test_advisory_quality_never_blocks_candidate_creation(self):
        self.character.visual_identity = ""
        self.character.description = ""
        self.character.age_appearance = ""
        self.character.face_identity = ""
        self.character.wardrobe = ""
        self.character.save(update_fields=(
            "visual_identity", "description", "age_appearance", "face_identity",
            "wardrobe", "updated_at",
        ))

        response, task = self._create(
            key="advisory", extra_direction="请保持人物眼下小痣",
        )

        quality = task.capability_snapshot["quality"]
        self.assertTrue(quality["advisory_only"])
        self.assertFalse(quality["would_block_enqueue"])
        self.assertEqual(quality["status"], "needs_attention")
        codes = {item["code"] for item in quality["issues"]}
        self.assertIn("bible_image_visual_identity_fallback", codes)
        self.assertIn("bible_image_director_text_contains_cjk", codes)
        self.assertEqual(response.data["candidate"]["latest_candidate"]["quality"], quality)

    def test_prompt_config_inputs_and_manual_overrides_are_explicit(self):
        current = self.client.get(self.url)
        self.assertEqual(current.status_code, 200, current.data)
        self.assertIn("prompt_config", current.data)
        self.assertEqual(
            current.data["prompt_config"]["composition_rules"]["character"],
            "neutral full-body or three-quarter standing pose; unobstructed face; identity reference image; plain unobtrusive studio background; no other people",
        )
        character_row = next(
            item for item in current.data["candidates"]
            if item["entity_type"] == "character"
        )
        positive_keys = {
            item["key"]: item
            for item in character_row["current_prompt_inputs"]["positive"]
        }
        self.assertIn("project.style_prompt", positive_keys)
        self.assertIn("system.composition.character", positive_keys)
        self.assertIn("entity.visual_anchor", positive_keys)

        response, task = self._create(
            key="manual-prompt-overrides",
            extra_direction="冷雨逆光",
            prompt_overrides={
                "project.style_prompt": "manual cold ink project style",
                "system.composition.character": "half-body identity portrait",
            },
        )
        prompt = PromptArtifact.objects.get(pk=response.data["prompt_artifact_id"])
        snapshot = prompt.request_options["source_snapshot"]
        self.assertEqual(
            snapshot["prompt_overrides"]["project.style_prompt"],
            "manual cold ink project style",
        )
        self.assertRegex(
            task.capability_snapshot["bible_target"]["source_fingerprint"],
            r"^[0-9a-f]{64}$",
        )
        latest_inputs = response.data["candidate"]["latest_candidate"]["prompt_inputs"]
        latest_positive = {
            item["key"]: item
            for item in latest_inputs["positive"]
        }
        self.assertEqual(
            latest_positive["project.style_prompt"]["value"],
            "manual cold ink project style",
        )
        self.assertEqual(
            latest_positive["project.style_prompt"]["source"],
            "manual_override",
        )
        self.assertIn(
            "manual cold ink project style",
            latest_inputs["final_prompt_text"],
        )

        changed = self._post(
            "character",
            self.character,
            "manual-prompt-overrides",
            prompt_overrides={
                "project.style_prompt": "different manual style",
            },
        )
        self.assertEqual(changed.status_code, 409, changed.data)
        self.assertEqual(
            changed.data["error"]["code"],
            "bible_image_candidate_idempotency_conflict",
        )

    def test_three_record_creation_is_atomic_when_compilation_fails(self):
        with patch(
            "studio.services.bible_image_candidates.materialize_generated_workflow",
            side_effect=WorkflowBuildError("broken graph"),
        ):
            response = self._post("character", self.character, "atomic-failure")

        self.assertEqual(response.status_code, 503, response.data)
        self.assertEqual(PromptArtifact.objects.count(), 0)
        self.assertEqual(GeneratedWorkflow.objects.count(), 0)
        self.assertEqual(GenerationTask.objects.count(), 0)

    def test_idempotent_replay_conflict_and_residual_chain_are_explicit(self):
        created, task = self._create(key="same-key")
        counts = (
            PromptArtifact.objects.count(),
            GeneratedWorkflow.objects.count(),
            GenerationTask.objects.count(),
        )

        replay = self._post("character", self.character, "same-key")
        self.assertEqual(replay.status_code, 200, replay.data)
        self.assertTrue(replay.data["idempotent_replay"])
        self.assertEqual(replay.data["prompt_artifact_id"], created.data["prompt_artifact_id"])
        self.assertEqual(replay.data["generated_workflow_id"], created.data["generated_workflow_id"])
        self.assertEqual(replay.data["generation_task_id"], created.data["generation_task_id"])
        self.assertEqual(counts, (
            PromptArtifact.objects.count(),
            GeneratedWorkflow.objects.count(),
            GenerationTask.objects.count(),
        ))

        changed = self._post(
            "character", self.character, "same-key", extra_direction="different",
        )
        self.assertEqual(changed.status_code, 409, changed.data)
        self.assertEqual(
            changed.data["error"]["code"],
            "bible_image_candidate_idempotency_conflict",
        )

        task.delete()
        residual = self._post("character", self.character, "same-key")
        self.assertEqual(residual.status_code, 409, residual.data)
        self.assertEqual(
            residual.data["error"]["code"],
            "bible_image_candidate_incomplete_chain",
        )

    def test_generic_workflow_and_task_creation_reject_reserved_candidate_chain(self):
        created, task = self._create(key="reserved-generic")
        workflow_count = GeneratedWorkflow.objects.count()
        task_count = GenerationTask.objects.count()

        workflow_response = self.client.post("/api/v1/workflows/generate/", {
            "prompt_artifact_id": created.data["prompt_artifact_id"],
            "template_id": str(task.generated_workflow.template_id),
            "overrides": {},
        }, format="json")
        self.assertEqual(workflow_response.status_code, 409, workflow_response.data)
        self.assertEqual(
            workflow_response.data["error"]["code"],
            "bible_image_candidate_reserved_pipeline",
        )
        self.assertEqual(GeneratedWorkflow.objects.count(), workflow_count)

        task_response = self.client.post("/api/v1/tasks/", {
            "generated_workflow_id": created.data["generated_workflow_id"],
        }, format="json")
        self.assertEqual(task_response.status_code, 409, task_response.data)
        self.assertEqual(
            task_response.data["error"]["code"],
            "bible_image_candidate_reserved_pipeline",
        )
        self.assertEqual(GenerationTask.objects.count(), task_count)

    def test_pipeline_rejects_candidate_reserved_snapshot_overrides(self):
        _, task = self._create(key="reserved-override")
        with transaction.atomic(), self.assertRaisesRegex(
            WorkflowBuildError, "candidate capability fields",
        ):
            materialize_generation_task(
                user=self.user,
                workflow=task.generated_workflow,
                priority=0,
                max_attempts=3,
                capability_snapshot_overrides={"request_fingerprint": "0" * 64},
            )

    def test_get_is_read_only_and_marks_changed_source_stale(self):
        self._create(key="projection")
        before = (
            PromptArtifact.objects.count(),
            GeneratedWorkflow.objects.count(),
            GenerationTask.objects.count(),
            Asset.objects.count(),
        )

        current = self.client.get(self.url)

        self.assertEqual(current.status_code, 200, current.data)
        character_row = next(
            item for item in current.data["candidates"]
            if item["entity_type"] == "character"
        )
        self.assertTrue(character_row["latest_candidate"]["source_current"])
        self.assertEqual(before, (
            PromptArtifact.objects.count(),
            GeneratedWorkflow.objects.count(),
            GenerationTask.objects.count(),
            Asset.objects.count(),
        ))

        self.character.visual_identity += "，银色耳钉"
        self.character.save(update_fields=("visual_identity", "updated_at"))
        stale = self.client.get(self.url)
        character_row = next(
            item for item in stale.data["candidates"]
            if item["entity_type"] == "character"
        )
        self.assertFalse(character_row["latest_candidate"]["source_current"])

    def test_get_preserves_every_reference_image_candidate_history(self):
        first, first_task = self._create(key="history-1")
        self._succeed_with_sealed_image(first_task, filename="candidate-1.png")
        first_prompt = PromptArtifact.objects.get(pk=first.data["prompt_artifact_id"])
        first_prompt.created_at = timezone.now() - timedelta(minutes=5)
        first_prompt.save(update_fields=("created_at", "updated_at"))

        second, second_task = self._create(
            key="history-2", extra_direction="雨夜逆光版本",
        )
        self._succeed_with_sealed_image(second_task, filename="candidate-2.png")

        response = self.client.get(self.url)

        self.assertEqual(response.status_code, 200, response.data)
        row = next(
            item for item in response.data["candidates"]
            if item["entity_type"] == "character"
        )
        self.assertEqual(len(row["candidate_history"]), 2)
        self.assertEqual(
            row["latest_candidate"]["prompt_artifact_id"],
            second.data["prompt_artifact_id"],
        )
        self.assertEqual(
            row["candidate_history"][0]["generation_task_id"],
            second.data["generation_task_id"],
        )
        self.assertEqual(
            row["candidate_history"][1]["generation_task_id"],
            first.data["generation_task_id"],
        )
        filenames = [
            candidate["output_media"][0]["filename"]
            for candidate in row["candidate_history"]
        ]
        self.assertEqual(filenames, ["candidate-2.png", "candidate-1.png"])

    def test_confirmed_translation_and_revision_are_exact_fingerprint_inputs(self):
        self._create(key="translation-before")
        entry = self._confirmed_character_translation(
            "A short-haired detective with a silver earring.",
        )

        stale = self.client.get(self.url)
        row = next(
            item for item in stale.data["candidates"]
            if item["entity_type"] == "character"
        )
        self.assertFalse(row["latest_candidate"]["source_current"])
        conflict = self._post("character", self.character, "translation-before")
        self.assertEqual(conflict.status_code, 409, conflict.data)

        created, _task = self._create(key="translation-current")
        prompt = PromptArtifact.objects.get(pk=created.data["prompt_artifact_id"])
        evidence = prompt.request_options["source_snapshot"]["translations"][
            "character.visual_identity"
        ]["translation"]
        self.assertEqual(evidence["revision"], 1)
        self.assertEqual(evidence["translated_text_sha256"], source_text_hash(
            entry.translated_text,
        ))

        entry.revision = 2
        entry.save(update_fields=("revision", "updated_at"))
        stale_after_revision = self.client.get(self.url)
        row = next(
            item for item in stale_after_revision.data["candidates"]
            if item["entity_type"] == "character"
        )
        self.assertFalse(row["latest_candidate"]["source_current"])
        conflict = self._post("character", self.character, "translation-current")
        self.assertEqual(conflict.status_code, 409, conflict.data)

    def test_canonical_collision_inputs_stale_candidate_but_unrelated_field_does_not(self):
        self._create(key="collision-inputs")
        self.prop.description += " unrelated edit"
        self.prop.save(update_fields=("description", "updated_at"))

        unchanged = self.client.get(self.url)
        row = next(
            item for item in unchanged.data["candidates"]
            if item["entity_type"] == "character"
        )
        self.assertTrue(row["latest_candidate"]["source_current"])

        self.project.production_settings = {
            **(self.project.production_settings or {}),
            "translation_glossary": {
                self.character.name: "Shared Subject",
                self.prop.name: "  shared   subject  ",
            },
        }
        self.project.save(update_fields=("production_settings", "updated_at"))
        stale = self.client.get(self.url)
        row = next(
            item for item in stale.data["candidates"]
            if item["entity_type"] == "character"
        )
        self.assertFalse(row["latest_candidate"]["source_current"])
        conflict = self._post("character", self.character, "collision-inputs")
        self.assertEqual(conflict.status_code, 409, conflict.data)

    def test_advisory_candidate_retry_preserves_frozen_purpose_and_target(self):
        _, task = self._create(key="retry")
        task.status = GenerationTask.Status.FAILED
        task.finished_at = timezone.now()
        task.save(update_fields=("status", "finished_at", "updated_at"))

        response = self.client.post(
            f"/api/v1/tasks/{task.id}/retry/",
            {"extended_generation_confirmed": False},
            format="json",
        )

        self.assertEqual(response.status_code, 201, response.data)
        retry = GenerationTask.objects.get(pk=response.data["id"])
        self._assert_candidate_task_projection(response.data, retry)
        self.assertEqual(retry.capability_snapshot["retry_of"], str(task.id))
        self.assertEqual(
            retry.capability_snapshot["generation_purpose"],
            "bible_image_candidate",
        )
        self.assertEqual(
            retry.capability_snapshot["bible_target"],
            task.capability_snapshot["bible_target"],
        )
        replay = self._post("character", self.character, "retry")
        self.assertEqual(replay.status_code, 200, replay.data)
        self.assertEqual(str(replay.data["generation_task_id"]), str(retry.id))
        projection = self.client.get(self.url)
        row = next(
            item for item in projection.data["candidates"]
            if item["entity_type"] == "character"
        )
        self.assertEqual(
            str(row["latest_candidate"]["generation_task_id"]), str(retry.id),
        )

    def test_promotion_uses_frozen_target_records_provenance_and_is_idempotent(self):
        _, task = self._create(key="promote")
        self._succeed_with_sealed_image(task)
        manifest = verified_task_manifest(task, 0)
        generation = manifest["generation"]
        self.assertEqual(generation["generation_purpose"], "bible_image_candidate")
        self.assertEqual(
            generation["request_fingerprint"],
            task.capability_snapshot["request_fingerprint"],
        )
        self.assertEqual(
            generation["bible_target"], task.capability_snapshot["bible_target"],
        )
        anchor_id = self.character.anchor_asset_id
        url = f"/api/v1/tasks/{task.id}/promote-output-to-asset/"

        wrong_target = self.client.post(url, {
            "output_index": 0,
            "reference_category": "scene",
            "entity_id": str(self.scene.id),
        }, format="json")
        self.assertEqual(wrong_target.status_code, 409, wrong_target.data)
        self.assertEqual(
            wrong_target.data["error"]["code"],
            "bible_image_candidate_target_conflict",
        )

        created = self.client.post(url, {"output_index": 0}, format="json")
        self.assertEqual(created.status_code, 201, created.data)
        asset = Asset.objects.get(pk=created.data["id"])
        self.character.refresh_from_db()
        task.refresh_from_db()
        self.assertEqual(self.character.anchor_asset_id, anchor_id)
        self.assertTrue(self.character.reference_assets.filter(pk=asset.id).exists())
        evidence = asset.metadata["bible_image_candidate"]
        self.assertEqual(evidence["generation_purpose"], "bible_image_candidate")
        self.assertEqual(evidence["entity_id"], str(self.character.id))
        self.assertEqual(
            evidence["source_fingerprint"],
            task.capability_snapshot["bible_target"]["source_fingerprint"],
        )
        self.assertEqual(
            evidence["request_fingerprint"],
            task.capability_snapshot["request_fingerprint"],
        )
        self.assertEqual(
            evidence["manifest_sha256"],
            task.outputs[0]["provenance"]["manifest_sha256"],
        )
        self.assertEqual(evidence["artifact_sha256"], asset.checksum)
        self.assertEqual(evidence["template_hash"], generation["template_hash"])
        self.assertEqual(task.outputs[0]["promoted_asset_id"], str(asset.id))

        # Once this exact output has been reviewed and copied, replay remains
        # idempotent even if the live Bible later changes.  Staleness protects
        # the first write; it must not erase or duplicate accepted audit facts.
        self.character.visual_identity += "，新增银色耳钉"
        self.character.save(update_fields=("visual_identity", "updated_at"))
        replay = self.client.post(url, {"output_index": 0}, format="json")
        self.assertEqual(replay.status_code, 200, replay.data)
        self.assertEqual(replay.data["id"], str(asset.id))
        self.assertEqual(
            Asset.objects.filter(
                metadata__generation_purpose="bible_image_candidate",
            ).count(),
            1,
        )

    def test_first_promotion_requires_explicit_confirmation_for_stale_bible_source(self):
        _, task = self._create(key="stale-promotion-bible")
        self._succeed_with_sealed_image(task, filename="stale-bible.png")
        asset_count = Asset.objects.count()
        reference_ids = set(self.character.reference_assets.values_list("id", flat=True))

        self.character.visual_identity += "，新增银色耳钉"
        self.character.save(update_fields=("visual_identity", "updated_at"))
        response = self.client.post(
            f"/api/v1/tasks/{task.id}/promote-output-to-asset/",
            {"output_index": 0},
            format="json",
        )

        self.assertEqual(response.status_code, 409, response.data)
        self.assertEqual(
            response.data["error"]["code"],
            "bible_image_candidate_source_stale",
        )
        self.assertIs(response.data["error"]["details"]["can_accept_stale"], True)
        self.assertEqual(Asset.objects.count(), asset_count)
        self.character.refresh_from_db()
        self.assertEqual(
            set(self.character.reference_assets.values_list("id", flat=True)),
            reference_ids,
        )
        task.refresh_from_db()
        self.assertNotIn("promoted_asset_id", task.outputs[0])

        accepted = self.client.post(
            f"/api/v1/tasks/{task.id}/promote-output-to-asset/",
            {
                "output_index": 0,
                "accept_stale_bible_image_candidate": True,
            },
            format="json",
        )

        self.assertEqual(accepted.status_code, 201, accepted.data)
        asset = Asset.objects.get(pk=accepted.data["id"])
        self.character.refresh_from_db()
        self.assertTrue(self.character.reference_assets.filter(pk=asset.id).exists())
        evidence = asset.metadata["bible_image_candidate"]
        self.assertFalse(evidence["source_current"])
        self.assertTrue(evidence["stale_source_accepted"])
        self.assertEqual(
            evidence["source_fingerprint"],
            task.capability_snapshot["bible_target"]["source_fingerprint"],
        )
        self.assertRegex(evidence["current_source_fingerprint"], r"^[0-9a-f]{64}$")
        self.assertNotEqual(
            evidence["current_source_fingerprint"],
            evidence["source_fingerprint"],
        )
        self.assertFalse(
            asset.metadata["generated_from"]["source_current"],
        )
        self.assertTrue(
            asset.metadata["generated_from"]["stale_source_accepted"],
        )

    def test_first_promotion_rejects_changed_confirmed_translation(self):
        translation = self._confirmed_character_translation(
            "a young detective with a dark wool coat",
        )
        _, task = self._create(key="stale-promotion-translation")
        self._succeed_with_sealed_image(task, filename="stale-translation.png")

        translation.translated_text = "a young detective with a white linen coat"
        translation.revision = 2
        translation.save(update_fields=("translated_text", "revision", "updated_at"))
        response = self.client.post(
            f"/api/v1/tasks/{task.id}/promote-output-to-asset/",
            {"output_index": 0},
            format="json",
        )

        self.assertEqual(response.status_code, 409, response.data)
        self.assertEqual(
            response.data["error"]["code"],
            "bible_image_candidate_source_stale",
        )

    def test_first_promotion_rejects_changed_canonical_collision_input(self):
        _, task = self._create(key="stale-promotion-canonical")
        self._succeed_with_sealed_image(task, filename="stale-canonical.png")
        self.project.production_settings = {
            **(self.project.production_settings or {}),
            "translation_glossary": {
                self.character.name: "Shared Subject",
                self.prop.name: "shared subject",
            },
        }
        self.project.save(update_fields=("production_settings", "updated_at"))

        response = self.client.post(
            f"/api/v1/tasks/{task.id}/promote-output-to-asset/",
            {"output_index": 0},
            format="json",
        )

        self.assertEqual(response.status_code, 409, response.data)
        self.assertEqual(
            response.data["error"]["code"],
            "bible_image_candidate_source_stale",
        )

    def test_candidate_promotion_requires_worker_sealed_provenance(self):
        _, task = self._create(key="missing-provenance")
        path = self.output_root / "unsealed.png"
        path.write_bytes(b"unsealed")
        task.status = GenerationTask.Status.SUCCEEDED
        task.outputs = [{
            "kind": "images", "filename": path.name, "subfolder": "", "type": "output",
        }]
        task.save(update_fields=("status", "outputs", "updated_at"))

        response = self.client.post(
            f"/api/v1/tasks/{task.id}/promote-output-to-asset/",
            {"output_index": 0},
            format="json",
        )

        self.assertEqual(response.status_code, 409, response.data)
        self.assertEqual(
            response.data["error"]["code"],
            "bible_image_candidate_provenance_missing",
        )
        self.assertFalse(
            Asset.objects.filter(
                metadata__generation_purpose="bible_image_candidate",
            ).exists(),
        )

    def test_promotion_rejects_signed_manifest_prompt_task_mismatch(self):
        _, task = self._create(key="signed-mismatch")
        self._succeed_with_sealed_image(task, filename="signed-mismatch.png")
        prompt = task.generated_workflow.prompt_artifact
        options = deepcopy(prompt.request_options)
        snapshot = deepcopy(task.capability_snapshot)
        options["bible_target"]["entity_name"] = "tampered reviewed target"
        snapshot["bible_target"]["entity_name"] = "tampered reviewed target"
        prompt.request_options = options
        task.capability_snapshot = snapshot
        prompt.save(update_fields=("request_options", "updated_at"))
        task.save(update_fields=("capability_snapshot", "updated_at"))

        response = self.client.post(
            f"/api/v1/tasks/{task.id}/promote-output-to-asset/",
            {"output_index": 0},
            format="json",
        )

        self.assertEqual(response.status_code, 409, response.data)
        self.assertEqual(
            response.data["error"]["code"],
            "bible_image_candidate_provenance_mismatch",
        )
        self.assertFalse(Asset.objects.filter(
            metadata__generation_purpose="bible_image_candidate",
        ).exists())

    def test_promotion_rejects_unknown_fields_and_uses_unified_not_found_envelope(self):
        _, task = self._create(key="promotion-envelope")
        invalid = self.client.post(
            f"/api/v1/tasks/{task.id}/promote-output-to-asset/",
            {"output_index": 0, "template": "client-selected"},
            format="json",
        )
        self.assertEqual(invalid.status_code, 400, invalid.data)
        self.assertEqual(invalid.data["error"]["code"], "invalid")
        self.assertIn("template", invalid.data["error"]["details"])

        missing = self.client.post(
            f"/api/v1/tasks/{uuid.uuid4()}/promote-output-to-asset/",
            {"output_index": 0},
            format="json",
        )
        self.assertEqual(missing.status_code, 404, missing.data)
        self.assertEqual(missing.data["error"]["code"], "not_found")

    def test_promotion_removes_copied_file_when_later_database_write_fails(self):
        _, task = self._create(key="rollback-file")
        self._succeed_with_sealed_image(task, filename="rollback-file.png")
        url = f"/api/v1/tasks/{task.id}/promote-output-to-asset/"
        asset_count = Asset.objects.count()

        with patch.object(
            GenerationTask,
            "save",
            autospec=True,
            side_effect=RuntimeError("simulated task update failure"),
        ):
            with self.assertRaisesRegex(RuntimeError, "simulated task update failure"):
                self.client.post(url, {"output_index": 0}, format="json")

        self.assertEqual(Asset.objects.count(), asset_count)
        copied_files = list(self.media_root.rglob(f"generated-{task.id}-0*"))
        self.assertEqual(copied_files, [])
