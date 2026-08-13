from types import SimpleNamespace

from django.core.management import call_command
from django.test import TestCase, override_settings
from rest_framework.test import APIClient

from studio.models import (
    GeneratedWorkflow,
    GenerationTask,
    PromptArtifact,
    PromptFieldTranslation,
    PropBible,
    WorkflowTemplate,
)
from studio.services.prompt_translation import (
    TRANSLATION_CONTEXT_VERSION,
    build_translation_context,
    confirmed_translation,
    source_text_hash,
    translation_context_hash,
)
from studio.services.prompt_quality_gate import evaluate_prompt_language_gate
from studio.services.h3_prompts import PROMPT_COMPILER_VERSION
from studio.tests.factories import build_story


DISABLED_OPTIMIZER = {
    "enabled": False,
    "base_url": "",
    "api_key": "",
    "model": "MiniMax-M2.7",
    "allow_non_loopback": False,
    "timeout_seconds": 60,
    "failure_mode": "warn",
}


@override_settings(H3_PROMPT_OPTIMIZER=DISABLED_OPTIMIZER)
class H3PromptLanguageGateTests(TestCase):
    def setUp(self):
        call_command("seed_data", verbosity=0)
        (
            self.user,
            self.project,
            _,
            _,
            self.episode,
            self.shot,
            _,
        ) = build_story(username="language-gate")
        self.shot.duration_ms = 5000
        self.shot.save(update_fields=("duration_ms", "updated_at"))
        self.client = APIClient()
        self.client.force_authenticate(self.user)

    def _production_payload(self, *, enqueue=True):
        return {
            "episode_ids": [str(self.episode.id)],
            "shot_ids": [str(self.shot.id)],
            "multi_shot": False,
            "enqueue": enqueue,
        }

    def _preview_video(self):
        response = self.client.post("/api/v1/prompts/generate/", {
            "project_id": str(self.project.id),
            "episode_id": str(self.episode.id),
            "shot_ids": [str(self.shot.id)],
            "prompt_type": "video",
            "h3_mode": "T2VA",
            "multi_shot": False,
            "include_audio": True,
            "shot_durations": [{
                "shot_id": str(self.shot.id),
                "duration_ms": self.shot.duration_ms,
            }],
            # Mirror the editor transport: saved shot audio is submitted again
            # as a one-off override. It must canonicalize to the same input as
            # production defaults, not force a semantically identical revision.
            "overall_soundscape": self.shot.sound_effects,
            "non_diegetic_music": self.shot.music,
        }, format="json")
        self.assertEqual(response.status_code, 201, response.data)
        return response

    def _preview_image(self):
        self.shot.media_type = "image"
        self.shot.save(update_fields=("media_type", "updated_at"))
        response = self.client.post("/api/v1/prompts/generate/", {
            "project_id": str(self.project.id),
            "episode_id": str(self.episode.id),
            "shot_ids": [str(self.shot.id)],
            "prompt_type": "image",
            "h3_mode": "T2VA",
            "multi_shot": False,
            "include_audio": False,
            "shot_durations": [],
        }, format="json")
        self.assertEqual(response.status_code, 201, response.data)
        return response

    def _mark_artifact_as_legacy_v6(self, artifact_id):
        artifact = PromptArtifact.objects.get(pk=artifact_id)
        self.assertEqual(
            artifact.structured_payload["prompt_compiler_version"], PROMPT_COMPILER_VERSION,
        )
        self.assertTrue(any("\u4e00" <= char <= "\u9fff" for char in artifact.prompt_text))
        artifact.structured_payload = {
            **artifact.structured_payload,
            "prompt_compiler_version": 6,
        }
        artifact.save(update_fields=("structured_payload", "updated_at"))
        return artifact

    def _confirm_action_translation(self):
        source = "\u5979\u62d4\u51fa\u7384\u7eb9\u5251\u6321\u5728\u95e8\u524d\u3002"
        translated = "She draws the Xuanwen Sword and blocks the doorway."
        production_settings = dict(self.project.production_settings or {})
        glossary = dict(production_settings.get("translation_glossary") or {})
        glossary["\u7384\u7eb9\u5251"] = "Xuanwen Sword"
        production_settings["translation_glossary"] = glossary
        self.project.production_settings = production_settings
        self.project.save(update_fields=("production_settings", "updated_at"))
        self.shot.action = source
        self.shot.save(update_fields=("action", "updated_at"))
        context = build_translation_context(
            self.project,
            scope_type="shot",
            scope_id=self.shot.id,
            field_key="shot.action",
            source_text=source,
        )
        PromptFieldTranslation.objects.create(
            project=self.project,
            scope_type="shot",
            scope_id=self.shot.id,
            field_key="shot.action",
            source_text=source,
            source_hash=source_text_hash(source),
            translated_text=translated,
            status=PromptFieldTranslation.Status.CONFIRMED,
            runtime_metadata={
                "translation_context_hash": translation_context_hash(context),
                "translation_context_version": TRANSLATION_CONTEXT_VERSION,
            },
        )
        return source, translated

    def _add_unrelated_glossary_entry(self):
        production_settings = dict(self.project.production_settings or {})
        glossary = dict(production_settings.get("translation_glossary") or {})
        glossary["\u672a\u51fa\u73b0\u5728\u6e90\u6587\u7684\u7389\u4f69"] = "Jade Pendant"
        production_settings["translation_glossary"] = glossary
        self.project.production_settings = production_settings
        self.project.save(update_fields=("production_settings", "updated_at"))

    def _change_relevant_glossary_entry(self):
        production_settings = dict(self.project.production_settings or {})
        glossary = dict(production_settings.get("translation_glossary") or {})
        glossary["\u7384\u7eb9\u5251"] = "Mystic Pattern Sword"
        production_settings["translation_glossary"] = glossary
        self.project.production_settings = production_settings
        self.project.save(update_fields=("production_settings", "updated_at"))

    @override_settings(H3_PROMPT_LANGUAGE_GATE="block")
    def test_preview_still_persists_needs_attention_but_enqueue_is_atomic_blocked(self):
        preview = self.client.post("/api/v1/prompts/generate/", {
            "project_id": str(self.project.id),
            "episode_id": str(self.episode.id),
            "shot_ids": [str(self.shot.id)],
            "prompt_type": "video",
            "h3_mode": "T2VA",
            "multi_shot": False,
            "include_audio": True,
            "shot_durations": [{"shot_id": str(self.shot.id), "duration_ms": 5000}],
        }, format="json")

        self.assertEqual(preview.status_code, 201, preview.data)
        quality = preview.data[0]["structured_payload"]["quality"]
        self.assertEqual(quality["status"], "needs_attention")
        self.assertEqual(quality["production_language_gate"]["mode"], "block")
        self.assertTrue(quality["production_language_gate"]["would_block_enqueue"])
        baseline_prompts = PromptArtifact.objects.count()

        response = self.client.post(
            f"/api/v1/projects/{self.project.id}/build-production/",
            self._production_payload(enqueue=True),
            format="json",
        )

        self.assertEqual(response.status_code, 400, response.data)
        details = response.data["error"]["details"]["prompt_language_gate"]
        self.assertEqual(details["mode"], "block")
        self.assertEqual(details["status"], "blocked")
        self.assertTrue(details["fields_requiring_english_rewrite"])
        self.assertFalse(any(
            "对白" in str(field) for field in details["fields_requiring_english_rewrite"]
        ))
        self.assertEqual(PromptArtifact.objects.count(), baseline_prompts)
        self.assertEqual(GeneratedWorkflow.objects.count(), 0)
        self.assertEqual(GenerationTask.objects.count(), 0)

    @override_settings(H3_PROMPT_LANGUAGE_GATE="block")
    def test_plan_without_enqueue_remains_available_and_reports_would_block(self):
        response = self.client.post(
            f"/api/v1/projects/{self.project.id}/build-production/",
            self._production_payload(enqueue=False),
            format="json",
        )

        self.assertEqual(response.status_code, 201, response.data)
        self.assertEqual(response.data["counts"]["tasks"], 0)
        report = response.data["prompt_language_gate"][0]
        self.assertEqual(report["status"], "blocked")
        self.assertTrue(report["would_block_enqueue"])

    @override_settings(H3_PROMPT_LANGUAGE_GATE="warn")
    def test_warn_mode_preserves_legacy_enqueue_with_explicit_warning_report(self):
        response = self.client.post(
            f"/api/v1/projects/{self.project.id}/build-production/",
            self._production_payload(enqueue=True),
            format="json",
        )

        self.assertEqual(response.status_code, 201, response.data)
        self.assertEqual(response.data["counts"]["tasks"], 1)
        report = response.data["prompt_language_gate"][0]
        self.assertEqual(report["mode"], "warn")
        self.assertEqual(report["status"], "warning")
        self.assertFalse(report["would_block_enqueue"])

    @override_settings(H3_PROMPT_LANGUAGE_GATE="warn")
    def test_canonical_target_collision_blocks_enqueue_even_in_warn_mode(self):
        character = self.project.characters.first()
        prop = PropBible.objects.create(
            project=self.project, slug="collision-prop", name="玄纹剑",
            visual_identity="a silver-gray sword with dark patterns",
        )
        self.project.production_settings = {
            **(self.project.production_settings or {}),
            "translation_glossary": {
                character.name: "Ming Shao",
                prop.name: "ming   shao",
            },
        }
        self.project.save(update_fields=("production_settings", "updated_at"))

        response = self.client.post(
            f"/api/v1/projects/{self.project.id}/build-production/",
            self._production_payload(enqueue=True), format="json",
        )

        self.assertEqual(response.status_code, 400, response.data)
        details = response.data["error"]["details"]["prompt_language_gate"]
        self.assertEqual(details["mode"], "warn")
        self.assertEqual(details["status"], "blocked")
        self.assertIn(
            "canonical_target_collision",
            details["fields_requiring_english_rewrite"],
        )
        self.assertEqual(GenerationTask.objects.count(), 0)

    @override_settings(H3_PROMPT_LANGUAGE_GATE="warn")
    def test_build_production_reuses_latest_unchanged_preview_artifact(self):
        preview = self._preview_video()
        preview_id = preview.data[0]["id"]
        baseline_prompts = PromptArtifact.objects.count()

        response = self.client.post(
            f"/api/v1/projects/{self.project.id}/build-production/",
            self._production_payload(enqueue=True),
            format="json",
        )

        self.assertEqual(response.status_code, 201, response.data)
        unit = response.data["production_units"][0]
        self.assertEqual(unit["prompt_artifact_id"], preview_id)
        self.assertTrue(unit["prompt_artifact_reused"])
        self.assertEqual(PromptArtifact.objects.count(), baseline_prompts)
        task = GenerationTask.objects.get(pk=unit["task_id"])
        self.assertEqual(str(task.generated_workflow.prompt_artifact_id), preview_id)

    @override_settings(H3_PROMPT_LANGUAGE_GATE="warn")
    def test_build_production_does_not_reuse_legacy_v6_chinese_video_artifact(self):
        preview = self._preview_video()
        preview_id = preview.data[0]["id"]
        self._mark_artifact_as_legacy_v6(preview_id)
        baseline_prompts = PromptArtifact.objects.count()

        response = self.client.post(
            f"/api/v1/projects/{self.project.id}/build-production/",
            self._production_payload(enqueue=True),
            format="json",
        )

        self.assertEqual(response.status_code, 201, response.data)
        unit = response.data["production_units"][0]
        self.assertNotEqual(unit["prompt_artifact_id"], preview_id)
        self.assertFalse(unit["prompt_artifact_reused"])
        self.assertEqual(PromptArtifact.objects.count(), baseline_prompts + 1)
        current = PromptArtifact.objects.get(pk=unit["prompt_artifact_id"])
        self.assertEqual(
            current.structured_payload["prompt_compiler_version"], PROMPT_COMPILER_VERSION,
        )

    @override_settings(H3_PROMPT_LANGUAGE_GATE="warn")
    def test_build_production_does_not_reuse_pre_high_detail_v7_video_artifact(self):
        preview = self._preview_video()
        preview_id = preview.data[0]["id"]
        artifact = PromptArtifact.objects.get(pk=preview_id)
        self.assertEqual(
            artifact.structured_payload["prompt_compiler_version"], PROMPT_COMPILER_VERSION,
        )
        artifact.structured_payload = {
            **artifact.structured_payload,
            "prompt_compiler_version": 7,
            "prompt_input_fingerprint_version": 3,
        }
        artifact.save(update_fields=("structured_payload", "updated_at"))
        baseline_prompts = PromptArtifact.objects.count()

        response = self.client.post(
            f"/api/v1/projects/{self.project.id}/build-production/",
            self._production_payload(enqueue=True),
            format="json",
        )

        self.assertEqual(response.status_code, 201, response.data)
        unit = response.data["production_units"][0]
        self.assertNotEqual(unit["prompt_artifact_id"], preview_id)
        self.assertFalse(unit["prompt_artifact_reused"])
        self.assertEqual(PromptArtifact.objects.count(), baseline_prompts + 1)
        current = PromptArtifact.objects.get(pk=unit["prompt_artifact_id"])
        self.assertEqual(
            current.structured_payload["prompt_compiler_version"], PROMPT_COMPILER_VERSION,
        )

    @override_settings(H3_PROMPT_LANGUAGE_GATE="warn")
    def test_unrelated_glossary_keeps_video_overlay_but_forces_new_artifact(self):
        source, translated = self._confirm_action_translation()
        preview = self._preview_video()
        preview_id = preview.data[0]["id"]
        self.assertIn(translated.rstrip("."), preview.data[0]["prompt_text"])
        self.assertNotIn(source.rstrip("。"), preview.data[0]["prompt_text"])
        baseline_prompts = PromptArtifact.objects.count()

        self._add_unrelated_glossary_entry()
        self.assertEqual(
            confirmed_translation(
                project_id=self.project.id,
                scope_type="shot",
                scope_id=self.shot.id,
                field_key="shot.action",
                source_text=source,
            ),
            translated,
        )
        response = self.client.post(
            f"/api/v1/projects/{self.project.id}/build-production/",
            self._production_payload(enqueue=True),
            format="json",
        )

        self.assertEqual(response.status_code, 201, response.data)
        unit = response.data["production_units"][0]
        self.assertNotEqual(unit["prompt_artifact_id"], preview_id)
        self.assertFalse(unit["prompt_artifact_reused"])
        self.assertEqual(PromptArtifact.objects.count(), baseline_prompts + 1)
        current = PromptArtifact.objects.get(pk=unit["prompt_artifact_id"])
        self.assertIn(translated.rstrip("."), current.prompt_text)
        self.assertNotIn(source.rstrip("。"), current.prompt_text)

    @override_settings(H3_PROMPT_LANGUAGE_GATE="block")
    def test_relevant_glossary_change_falls_back_to_chinese_and_blocks_video(self):
        source, translated = self._confirm_action_translation()
        self.assertEqual(
            confirmed_translation(
                project_id=self.project.id,
                scope_type="shot",
                scope_id=self.shot.id,
                field_key="shot.action",
                source_text=source,
            ),
            translated,
        )
        self._change_relevant_glossary_entry()
        self.assertEqual(
            confirmed_translation(
                project_id=self.project.id,
                scope_type="shot",
                scope_id=self.shot.id,
                field_key="shot.action",
                source_text=source,
            ),
            source,
        )

        preview = self._preview_video()
        self.assertIn(source.rstrip("。"), preview.data[0]["prompt_text"])
        self.assertNotIn(translated.rstrip("."), preview.data[0]["prompt_text"])
        quality = preview.data[0]["structured_payload"]["quality"]
        self.assertTrue(quality["production_language_gate"]["would_block_enqueue"])
        baseline_prompts = PromptArtifact.objects.count()

        response = self.client.post(
            f"/api/v1/projects/{self.project.id}/build-production/",
            self._production_payload(enqueue=True),
            format="json",
        )

        self.assertEqual(response.status_code, 400, response.data)
        self.assertEqual(PromptArtifact.objects.count(), baseline_prompts)
        self.assertEqual(GenerationTask.objects.count(), 0)

    @override_settings(H3_PROMPT_LANGUAGE_GATE="warn")
    def test_build_production_creates_new_artifact_after_prompt_input_changes(self):
        preview = self._preview_video()
        preview_id = preview.data[0]["id"]
        baseline_prompts = PromptArtifact.objects.count()
        self.shot.camera = "A slow dolly-in ends on the detective's eyes."
        self.shot.save(update_fields=("camera", "updated_at"))

        response = self.client.post(
            f"/api/v1/projects/{self.project.id}/build-production/",
            self._production_payload(enqueue=True),
            format="json",
        )

        self.assertEqual(response.status_code, 201, response.data)
        unit = response.data["production_units"][0]
        self.assertNotEqual(unit["prompt_artifact_id"], preview_id)
        self.assertFalse(unit["prompt_artifact_reused"])
        self.assertEqual(PromptArtifact.objects.count(), baseline_prompts + 1)

    @override_settings(H3_PROMPT_LANGUAGE_GATE="warn")
    def test_build_production_does_not_reuse_video_preview_after_glossary_changes(self):
        preview = self._preview_video()
        preview_id = preview.data[0]["id"]
        baseline_prompts = PromptArtifact.objects.count()
        self.project.production_settings = {
            **(self.project.production_settings or {}),
            "translation_glossary": {"林默": "Lin Mo"},
        }
        self.project.save(update_fields=("production_settings", "updated_at"))

        response = self.client.post(
            f"/api/v1/projects/{self.project.id}/build-production/",
            self._production_payload(enqueue=True),
            format="json",
        )

        self.assertEqual(response.status_code, 201, response.data)
        unit = response.data["production_units"][0]
        self.assertNotEqual(unit["prompt_artifact_id"], preview_id)
        self.assertFalse(unit["prompt_artifact_reused"])
        self.assertEqual(PromptArtifact.objects.count(), baseline_prompts + 1)

    @override_settings(H3_PROMPT_LANGUAGE_GATE="warn")
    def test_build_production_does_not_reuse_video_preview_after_visual_rules_change(self):
        preview = self._preview_video()
        preview_id = preview.data[0]["id"]
        baseline_prompts = PromptArtifact.objects.count()
        self.project.production_settings = {
            **(self.project.production_settings or {}),
            "negative_prompt": "identity drift, extra fingers",
            "color_script": "a restrained teal palette with soft amber lighting",
        }
        self.project.continuity_summary = (
            "Maintain stable project-wide identity and spatial continuity."
        )
        self.project.save(update_fields=(
            "production_settings", "continuity_summary", "updated_at",
        ))

        response = self.client.post(
            f"/api/v1/projects/{self.project.id}/build-production/",
            self._production_payload(enqueue=True),
            format="json",
        )

        self.assertEqual(response.status_code, 201, response.data)
        unit = response.data["production_units"][0]
        self.assertNotEqual(unit["prompt_artifact_id"], preview_id)
        self.assertFalse(unit["prompt_artifact_reused"])
        self.assertEqual(PromptArtifact.objects.count(), baseline_prompts + 1)

    @override_settings(H3_PROMPT_LANGUAGE_GATE="warn")
    def test_build_production_does_not_reuse_tampered_preview_prompt(self):
        preview = self._preview_video()
        preview_id = preview.data[0]["id"]
        baseline_prompts = PromptArtifact.objects.count()
        PromptArtifact.objects.filter(pk=preview_id).update(
            prompt_text="tampered prompt text",
        )

        response = self.client.post(
            f"/api/v1/projects/{self.project.id}/build-production/",
            self._production_payload(enqueue=True),
            format="json",
        )

        self.assertEqual(response.status_code, 201, response.data)
        unit = response.data["production_units"][0]
        self.assertNotEqual(unit["prompt_artifact_id"], preview_id)
        self.assertFalse(unit["prompt_artifact_reused"])
        self.assertEqual(PromptArtifact.objects.count(), baseline_prompts + 1)

    @override_settings(H3_PROMPT_LANGUAGE_GATE="warn")
    def test_build_production_never_falls_back_to_older_matching_preview(self):
        original = self._preview_video()
        original_id = original.data[0]["id"]
        original_camera = self.shot.camera
        self.shot.camera = "A handheld camera circles behind the detective."
        self.shot.save(update_fields=("camera", "updated_at"))
        latest = self._preview_video()
        self.assertNotEqual(latest.data[0]["id"], original_id)
        self.shot.camera = original_camera
        self.shot.save(update_fields=("camera", "updated_at"))
        baseline_prompts = PromptArtifact.objects.count()

        response = self.client.post(
            f"/api/v1/projects/{self.project.id}/build-production/",
            self._production_payload(enqueue=True),
            format="json",
        )

        self.assertEqual(response.status_code, 201, response.data)
        unit = response.data["production_units"][0]
        self.assertNotEqual(unit["prompt_artifact_id"], original_id)
        self.assertFalse(unit["prompt_artifact_reused"])
        self.assertEqual(PromptArtifact.objects.count(), baseline_prompts + 1)

    @override_settings(H3_PROMPT_LANGUAGE_GATE="warn")
    def test_build_production_reuses_latest_unchanged_image_preview(self):
        self.shot.media_type = "image"
        self.shot.save(update_fields=("media_type", "updated_at"))
        preview = self.client.post("/api/v1/prompts/generate/", {
            "project_id": str(self.project.id),
            "episode_id": str(self.episode.id),
            "shot_ids": [str(self.shot.id)],
            "prompt_type": "image",
            "h3_mode": "T2VA",
            "multi_shot": False,
            "include_audio": False,
            "shot_durations": [],
        }, format="json")
        self.assertEqual(preview.status_code, 201, preview.data)
        preview_id = preview.data[0]["id"]
        self.assertEqual(
            preview.data[0]["structured_payload"]["prompt_compiler_version"],
            PROMPT_COMPILER_VERSION,
        )
        self.assertTrue(preview.data[0]["compiler_current"])
        baseline_prompts = PromptArtifact.objects.count()

        response = self.client.post(
            f"/api/v1/projects/{self.project.id}/build-production/",
            self._production_payload(enqueue=True),
            format="json",
        )

        self.assertEqual(response.status_code, 201, response.data)
        unit = response.data["production_units"][0]
        self.assertEqual(unit["prompt_artifact_id"], preview_id)
        self.assertTrue(unit["prompt_artifact_reused"])
        self.assertEqual(PromptArtifact.objects.count(), baseline_prompts)

    @override_settings(H3_PROMPT_LANGUAGE_GATE="warn")
    def test_build_production_does_not_reuse_legacy_v6_chinese_image_artifact(self):
        preview = self._preview_image()
        preview_id = preview.data[0]["id"]
        self._mark_artifact_as_legacy_v6(preview_id)
        baseline_prompts = PromptArtifact.objects.count()

        response = self.client.post(
            f"/api/v1/projects/{self.project.id}/build-production/",
            self._production_payload(enqueue=True),
            format="json",
        )

        self.assertEqual(response.status_code, 201, response.data)
        unit = response.data["production_units"][0]
        self.assertNotEqual(unit["prompt_artifact_id"], preview_id)
        self.assertFalse(unit["prompt_artifact_reused"])
        self.assertEqual(PromptArtifact.objects.count(), baseline_prompts + 1)
        current = PromptArtifact.objects.get(pk=unit["prompt_artifact_id"])
        self.assertEqual(
            current.structured_payload["prompt_compiler_version"], PROMPT_COMPILER_VERSION,
        )

    @override_settings(H3_PROMPT_LANGUAGE_GATE="warn")
    def test_unrelated_glossary_keeps_image_overlay_but_forces_new_artifact(self):
        source, translated = self._confirm_action_translation()
        preview = self._preview_image()
        preview_id = preview.data[0]["id"]
        self.assertIn(translated.rstrip("."), preview.data[0]["prompt_text"])
        self.assertNotIn(source.rstrip("。"), preview.data[0]["prompt_text"])
        baseline_prompts = PromptArtifact.objects.count()

        self._add_unrelated_glossary_entry()
        self.assertEqual(
            confirmed_translation(
                project_id=self.project.id,
                scope_type="shot",
                scope_id=self.shot.id,
                field_key="shot.action",
                source_text=source,
            ),
            translated,
        )
        response = self.client.post(
            f"/api/v1/projects/{self.project.id}/build-production/",
            self._production_payload(enqueue=True),
            format="json",
        )

        self.assertEqual(response.status_code, 201, response.data)
        unit = response.data["production_units"][0]
        self.assertNotEqual(unit["prompt_artifact_id"], preview_id)
        self.assertFalse(unit["prompt_artifact_reused"])
        self.assertEqual(PromptArtifact.objects.count(), baseline_prompts + 1)
        current = PromptArtifact.objects.get(pk=unit["prompt_artifact_id"])
        self.assertIn(translated.rstrip("."), current.prompt_text)
        self.assertNotIn(source.rstrip("。"), current.prompt_text)

    @override_settings(H3_PROMPT_LANGUAGE_GATE="block")
    def test_relevant_glossary_change_falls_back_to_chinese_and_blocks_image(self):
        source, translated = self._confirm_action_translation()
        self._change_relevant_glossary_entry()
        self.assertEqual(
            confirmed_translation(
                project_id=self.project.id,
                scope_type="shot",
                scope_id=self.shot.id,
                field_key="shot.action",
                source_text=source,
            ),
            source,
        )

        preview = self._preview_image()
        self.assertIn(source.rstrip("。"), preview.data[0]["prompt_text"])
        self.assertNotIn(translated.rstrip("."), preview.data[0]["prompt_text"])
        quality = preview.data[0]["structured_payload"]["quality"]
        self.assertTrue(quality["production_language_gate"]["would_block_enqueue"])
        baseline_prompts = PromptArtifact.objects.count()

        response = self.client.post(
            f"/api/v1/projects/{self.project.id}/build-production/",
            self._production_payload(enqueue=True),
            format="json",
        )

        self.assertEqual(response.status_code, 400, response.data)
        self.assertEqual(PromptArtifact.objects.count(), baseline_prompts)
        self.assertEqual(GenerationTask.objects.count(), 0)

    @override_settings(H3_PROMPT_LANGUAGE_GATE="warn")
    def test_build_production_does_not_reuse_image_preview_after_glossary_changes(self):
        self.shot.media_type = "image"
        self.shot.save(update_fields=("media_type", "updated_at"))
        preview = self.client.post("/api/v1/prompts/generate/", {
            "project_id": str(self.project.id),
            "episode_id": str(self.episode.id),
            "shot_ids": [str(self.shot.id)],
            "prompt_type": "image",
            "h3_mode": "T2VA",
            "multi_shot": False,
            "include_audio": False,
            "shot_durations": [],
        }, format="json")
        self.assertEqual(preview.status_code, 201, preview.data)
        preview_id = preview.data[0]["id"]
        baseline_prompts = PromptArtifact.objects.count()
        self.project.refresh_from_db()
        self.project.production_settings = {
            **(self.project.production_settings or {}),
            "translation_glossary": {"林默": "Lin Mo"},
        }
        self.project.save(update_fields=("production_settings", "updated_at"))

        response = self.client.post(
            f"/api/v1/projects/{self.project.id}/build-production/",
            self._production_payload(enqueue=True),
            format="json",
        )

        self.assertEqual(response.status_code, 201, response.data)
        unit = response.data["production_units"][0]
        self.assertNotEqual(unit["prompt_artifact_id"], preview_id)
        self.assertFalse(unit["prompt_artifact_reused"])
        self.assertEqual(PromptArtifact.objects.count(), baseline_prompts + 1)

    @override_settings(H3_PROMPT_LANGUAGE_GATE="warn")
    def test_build_production_does_not_reuse_image_preview_after_visual_rules_change(self):
        self.shot.media_type = "image"
        self.shot.save(update_fields=("media_type", "updated_at"))
        preview = self.client.post("/api/v1/prompts/generate/", {
            "project_id": str(self.project.id),
            "episode_id": str(self.episode.id),
            "shot_ids": [str(self.shot.id)],
            "prompt_type": "image",
            "h3_mode": "T2VA",
            "multi_shot": False,
            "include_audio": False,
            "shot_durations": [],
        }, format="json")
        self.assertEqual(preview.status_code, 201, preview.data)
        preview_id = preview.data[0]["id"]
        baseline_prompts = PromptArtifact.objects.count()
        self.project.refresh_from_db()
        self.project.production_settings = {
            **(self.project.production_settings or {}),
            "negative_prompt": "identity drift, extra fingers",
            "color_script": "a restrained teal palette with soft amber lighting",
        }
        self.project.continuity_summary = (
            "Maintain stable project-wide identity and spatial continuity."
        )
        self.project.save(update_fields=(
            "production_settings", "continuity_summary", "updated_at",
        ))

        response = self.client.post(
            f"/api/v1/projects/{self.project.id}/build-production/",
            self._production_payload(enqueue=True),
            format="json",
        )

        self.assertEqual(response.status_code, 201, response.data)
        unit = response.data["production_units"][0]
        self.assertNotEqual(unit["prompt_artifact_id"], preview_id)
        self.assertFalse(unit["prompt_artifact_reused"])
        self.assertEqual(PromptArtifact.objects.count(), baseline_prompts + 1)

    @override_settings(H3_PROMPT_LANGUAGE_GATE="block")
    def test_image_with_untranslated_director_fields_is_blocked_but_english_image_passes(self):
        quality = {
            "fields_requiring_english_rewrite": ["镜头 1 主体动作"],
            "optimizer": {"status": "not_configured"},
        }
        image_report = evaluate_prompt_language_gate(SimpleNamespace(
            prompt_type=PromptArtifact.PromptType.IMAGE,
            structured_payload={"quality": quality},
        ))
        english_image_report = evaluate_prompt_language_gate(SimpleNamespace(
            prompt_type=PromptArtifact.PromptType.IMAGE,
            structured_payload={"quality": {
                "fields_requiring_english_rewrite": [],
                "optimizer": {"status": "not_required"},
            }},
        ))
        optimized_video_report = evaluate_prompt_language_gate(SimpleNamespace(
            prompt_type=PromptArtifact.PromptType.VIDEO,
            structured_payload={"quality": {
                **quality,
                "optimizer": {"status": "optimized"},
            }},
        ))

        self.assertTrue(image_report.applies)
        self.assertTrue(image_report.blocks_enqueue)
        self.assertTrue(image_report.requires_english_rewrite)
        self.assertTrue(english_image_report.applies)
        self.assertFalse(english_image_report.blocks_enqueue)
        self.assertFalse(optimized_video_report.requires_english_rewrite)
        self.assertFalse(optimized_video_report.blocks_enqueue)

    @override_settings(H3_PROMPT_LANGUAGE_GATE="block")
    def test_image_preview_is_persisted_with_honest_quality_report_but_enqueue_rolls_back(self):
        self.shot.media_type = "image"
        self.shot.save(update_fields=("media_type", "updated_at"))
        preview = self.client.post("/api/v1/prompts/generate/", {
            "project_id": str(self.project.id),
            "episode_id": str(self.episode.id),
            "shot_ids": [str(self.shot.id)],
            "prompt_type": "image",
            "h3_mode": "T2VA",
            "multi_shot": False,
            "include_audio": False,
            "shot_durations": [],
        }, format="json")

        self.assertEqual(preview.status_code, 201, preview.data)
        image = preview.data[0]
        self.assertEqual(image["model"], "stabilityai/stable-diffusion-xl-base-1.0")
        quality = image["structured_payload"]["quality"]
        self.assertEqual(quality["status"], "needs_attention")
        self.assertEqual(quality["optimizer"]["status"], "not_configured")
        self.assertTrue(quality["fields_requiring_english_rewrite"])
        self.assertTrue(quality["production_language_gate"]["would_block_enqueue"])
        self.assertEqual(image["structured_payload"]["reference_bindings"], [])
        self.assertFalse(image["structured_payload"]["pixel_reference_inputs"])
        baseline_prompts = PromptArtifact.objects.count()

        response = self.client.post(
            f"/api/v1/projects/{self.project.id}/build-production/",
            self._production_payload(enqueue=True),
            format="json",
        )

        self.assertEqual(response.status_code, 400, response.data)
        details = response.data["error"]["details"]["prompt_language_gate"]
        self.assertEqual(details["status"], "blocked")
        self.assertTrue(details["fields_requiring_english_rewrite"])
        self.assertEqual(PromptArtifact.objects.count(), baseline_prompts)
        self.assertEqual(GeneratedWorkflow.objects.count(), 0)
        self.assertEqual(GenerationTask.objects.count(), 0)

    @override_settings(H3_PROMPT_LANGUAGE_GATE="block")
    def test_manual_workflow_preview_reports_gate_but_direct_task_enqueue_is_blocked(self):
        preview = self.client.post("/api/v1/prompts/generate/", {
            "project_id": str(self.project.id),
            "episode_id": str(self.episode.id),
            "shot_ids": [str(self.shot.id)],
            "prompt_type": "video",
            "h3_mode": "T2VA",
            "multi_shot": False,
            "include_audio": True,
            "shot_durations": [{"shot_id": str(self.shot.id), "duration_ms": 5000}],
        }, format="json")
        self.assertEqual(preview.status_code, 201, preview.data)
        template = WorkflowTemplate.objects.get(owner=None, slug="minimax-h3-base-video")

        workflow = self.client.post("/api/v1/workflows/generate/", {
            "prompt_artifact_id": preview.data[0]["id"],
            "template_id": str(template.id),
        }, format="json")
        self.assertEqual(workflow.status_code, 201, workflow.data)
        self.assertTrue(workflow.data["prompt_language_gate"]["would_block_enqueue"])
        self.assertEqual(GenerationTask.objects.count(), 0)

        queued = self.client.post("/api/v1/tasks/", {
            "generated_workflow_id": workflow.data["id"],
        }, format="json")
        self.assertEqual(queued.status_code, 400, queued.data)
        details = queued.data["error"]["details"]["prompt_language_gate"]
        self.assertEqual(details["status"], "blocked")
        self.assertEqual(details["workflow_id"], workflow.data["id"])
        self.assertEqual(GenerationTask.objects.count(), 0)

    @override_settings(H3_PROMPT_LANGUAGE_GATE="block")
    def test_retry_rechecks_language_gate_before_cloning_failed_task(self):
        preview = self.client.post("/api/v1/prompts/generate/", {
            "project_id": str(self.project.id),
            "episode_id": str(self.episode.id),
            "shot_ids": [str(self.shot.id)],
            "prompt_type": "video",
            "h3_mode": "T2VA",
            "multi_shot": False,
            "include_audio": True,
            "shot_durations": [{"shot_id": str(self.shot.id), "duration_ms": 5000}],
        }, format="json")
        self.assertEqual(preview.status_code, 201, preview.data)
        template = WorkflowTemplate.objects.get(owner=None, slug="minimax-h3-base-video")
        workflow_response = self.client.post("/api/v1/workflows/generate/", {
            "prompt_artifact_id": preview.data[0]["id"],
            "template_id": str(template.id),
        }, format="json")
        self.assertEqual(workflow_response.status_code, 201, workflow_response.data)
        workflow = GeneratedWorkflow.objects.get(pk=workflow_response.data["id"])
        failed = GenerationTask.objects.create(
            owner=self.user,
            project=self.project,
            episode=self.episode,
            generated_workflow=workflow,
            kind=GenerationTask.Kind.VIDEO,
            status=GenerationTask.Status.FAILED,
            submission_state=GenerationTask.SubmissionState.TERMINAL_FAILED,
        )

        retried = self.client.post(f"/api/v1/tasks/{failed.id}/retry/", format="json")
        self.assertEqual(retried.status_code, 400, retried.data)
        details = retried.data["error"]["details"]["prompt_language_gate"]
        self.assertEqual(details["status"], "blocked")
        self.assertEqual(details["retry_of"], str(failed.id))
        self.assertEqual(GenerationTask.objects.count(), 1)
