from __future__ import annotations

import importlib.util
from pathlib import Path
import sys
from types import SimpleNamespace
from unittest.mock import patch

from django.test import TestCase, override_settings
from django.urls import reverse
from rest_framework.test import APIClient

from studio.models import PromptFieldTranslation, Shot, ShotDetailDraft
from studio.serializers import ShotDetailDraftDataSerializer
from studio.services.creative_drafts import CreativeDraftError, CreativeResourceBusy
from studio.services.direction_semantics import direction_semantic_mismatches
from studio.services.h3_prompts import (
    H3PromptOptions,
    build_h3_video_prompt,
    build_negative_prompt,
)
from studio.services.prompt_translation import (
    TRANSLATION_CONTEXT_VERSION,
    build_translation_context,
    source_text_hash,
    translation_context_hash,
)
from studio.services.shot_detail_drafts import shot_detail_context_hash
from studio.tests.factories import build_story


LOCAL_CREATIVE_CONFIG = {
    "enabled": True,
    "model_id": "Qwen/Qwen3-4B-GGUF",
    "model_dir": "../.runtime/models/Qwen/Qwen3-4B-GGUF",
    "runtime_script": "../scripts/qwen_translate_once.py",
    "runtime_python": "../.runtime/qwen-python/Scripts/python.exe",
    "device": "cpu",
    "timeout_seconds": 900,
    "max_new_tokens": 3072,
    "context_size": 8192,
    "min_shots": 1,
    "max_shots": 4,
    "max_source_chars": 16000,
}


@override_settings(LOCAL_QWEN_CREATIVE=LOCAL_CREATIVE_CONFIG)
class ShotDetailDraftApiTests(TestCase):
    def setUp(self):
        (
            self.user, self.project, self.character, self.scene, self.episode,
            self.shot, self.next_shot,
        ) = build_story()
        self.client = APIClient()
        self.client.force_authenticate(self.user)
        settings = dict(self.project.production_settings or {})
        settings["translation_glossary"] = {self.character.name: "Lin Mo"}
        self.project.production_settings = settings
        self.project.save(update_fields=("production_settings", "updated_at"))

    @staticmethod
    def candidate() -> dict[str, object]:
        return {
            "schema_version": 1,
            "detail_zh": (
                "林默站在左侧档案架前，灰色风衣贴着肩线，右手把烧焦信纸按在桌面；"
                "S1 的视线先停在纸角，再移向门口阴影。镜头缓慢推近，雨声穿过玻璃，"
                "她在结尾收紧握纸的手指并稳住呼吸。"
            ),
            "detail_en": (
                "Lin Mo stands at the left archive shelf, the gray coat following the shoulder line, "
                "and presses a scorched letter to the desk with the right hand; S1 first fixes the gaze "
                "on its corner, then turns toward the shadow at the doorway. The camera slowly pushes in "
                "as rain taps the glass, ending with tightened fingers and a controlled breath."
            ),
        }

    def _runtime_side_effect(self, captured, candidate=None):
        candidate = candidate or self.candidate()

        def invoke(request, _config):
            captured.update(request)
            return {
                "result": candidate,
                "source_fingerprint": request["source_fingerprint"],
                "runtime": {"model": "Qwen/Qwen3-4B-GGUF", "elapsed_ms": 1},
            }
        return invoke

    def _generate(self, candidate=None):
        captured = {}
        with patch("studio.services.shot_detail_drafts.ensure_creative_resources_available"), patch(
            "studio.services.shot_detail_drafts._require_runtime",
        ), patch(
            "studio.services.shot_detail_drafts._run_runtime",
            side_effect=self._runtime_side_effect(captured, candidate),
        ):
            response = self.client.post(
                f"/api/v1/projects/{self.project.id}/shot-detail-drafts/generate/",
                {"shot_id": str(self.shot.id), "variation_note": "加强雨夜压迫感"},
                format="json",
            )
        return response, captured

    def _confirm(self, draft, *, review_acknowledged=False):
        return self.client.post(
            f"/api/v1/shot-detail-drafts/{draft['id']}/confirm/",
            {
                "expected_revision": draft["revision"],
                "review_acknowledged": review_acknowledged,
            }, format="json",
        )

    def _apply(
        self, draft, *, language_mode="bilingual_confirmed", acknowledged=True,
        review_acknowledged=False,
    ):
        return self.client.post(
            f"/api/v1/shot-detail-drafts/{draft['id']}/apply/",
            {
                "expected_revision": draft["revision"],
                "language_mode": language_mode,
                "english_review_acknowledged": acknowledged,
                "review_acknowledged": review_acknowledged,
            }, format="json",
        )

    def test_generate_isolated_then_bilingual_apply_updates_formal_shot_once(self):
        generated, captured = self._generate()

        self.assertEqual(generated.status_code, 201, generated.data)
        self.assertEqual(generated.data["status"], ShotDetailDraft.Status.DRAFT)
        self.assertEqual(generated.data["validation"]["status"], "ready")
        self.assertEqual(captured["operation"], "shot_high_detail")
        self.assertEqual(captured["target_shot"]["id"], str(self.shot.id))
        self.assertEqual(captured["target_shot"]["media_type"], "video")
        self.assertEqual(
            captured["target_shot"]["expansion_focus"],
            {
                "camera_motion": self.shot.camera,
                "action_and_visual_description": self.shot.action,
                "narrative_visual_context": self.shot.narrative,
            },
        )
        self.assertEqual(
            captured["expansion_scope"]["primary_fields"],
            captured["target_shot"]["expansion_focus"],
        )
        self.assertNotIn("script", captured)
        self.shot.refresh_from_db()
        self.assertEqual(self.shot.detailed_description, "")

        confirmed = self._confirm(generated.data)
        self.assertEqual(confirmed.status_code, 200, confirmed.data)
        denied = self._apply(confirmed.data, acknowledged=False)
        self.assertEqual(denied.status_code, 400, denied.data)

        applied = self._apply(confirmed.data)
        self.assertEqual(applied.status_code, 200, applied.data)
        self.assertTrue(applied.data["translation_written"])
        self.assertEqual(applied.data["draft"]["status"], ShotDetailDraft.Status.APPLIED)
        self.assertTrue(applied.data["draft"]["context_current"])
        self.assertEqual(
            applied.data["applied_shot"]["detailed_description"],
            self.candidate()["detail_en"],
        )

        self.shot.refresh_from_db()
        draft = ShotDetailDraft.objects.get(pk=generated.data["id"])
        self.assertEqual(self.shot.detailed_description, self.candidate()["detail_en"])
        self.assertEqual(draft.context_hash, shot_detail_context_hash(self.project, self.episode, self.shot))
        self.assertEqual(
            draft.input_settings["generation_context_hash"],
            draft.revision_history[-1]["context_hash"],
        )
        self.assertEqual(
            self.shot.metadata["shot_detail_provenance"]["applied_context_hash"],
            draft.context_hash,
        )
        self.assertTrue(PromptFieldTranslation.objects.filter(
            scope_id=self.shot.id,
            field_key="shot.detailed_description",
            status=PromptFieldTranslation.Status.CONFIRMED,
        ).exists())

        compiled = build_h3_video_prompt(
            self.project, [self.shot], H3PromptOptions(mode=Shot.H3Mode.T2VA),
        )
        self.assertEqual(compiled.text.count(self.candidate()["detail_en"]), 1)
        self.assertNotIn(self.candidate()["detail_zh"], compiled.text)
        self.assertNotIn(self.candidate()["detail_en"], build_negative_prompt(self.project, [self.shot]))

    def test_post_apply_boundary_and_bible_changes_make_draft_stale(self):
        generated, _ = self._generate()
        confirmed = self._confirm(generated.data)
        applied = self._apply(confirmed.data)
        self.assertTrue(applied.data["draft"]["context_current"])

        self.next_shot.camera = "A new boundary camera direction."
        self.next_shot.save(update_fields=("camera", "updated_at"))
        listed = self.client.get(
            f"/api/v1/projects/{self.project.id}/shot-detail-drafts/?shot={self.shot.id}",
        )
        self.assertEqual(listed.status_code, 200, listed.data)
        self.assertFalse(listed.data[0]["context_current"])

        # Create a fresh applied baseline, then prove that a bound Bible edit
        # also invalidates it without relying on unrelated project records.
        generated, _ = self._generate()
        confirmed = self._confirm(generated.data)
        applied = self._apply(confirmed.data)
        self.assertTrue(applied.data["draft"]["context_current"])
        self.character.visual_identity = "短发侦探，左眼下的小痣清晰可见。"
        self.character.save(update_fields=("visual_identity", "updated_at"))
        listed = self.client.get(
            f"/api/v1/projects/{self.project.id}/shot-detail-drafts/?shot={self.shot.id}",
        )
        self.assertFalse(listed.data[0]["context_current"])

    def test_chinese_only_apply_can_be_promoted_through_existing_translation_review(self):
        generated, _ = self._generate()
        confirmed = self._confirm(generated.data)
        applied = self._apply(confirmed.data, language_mode="chinese_only")

        self.assertEqual(applied.status_code, 200, applied.data)
        self.assertFalse(applied.data["translation_written"])
        self.shot.refresh_from_db()
        source = self.candidate()["detail_zh"]
        translated = self.candidate()["detail_en"]
        self.assertEqual(self.shot.detailed_description, source)
        self.assertFalse(PromptFieldTranslation.objects.filter(
            scope_id=self.shot.id, field_key="shot.detailed_description",
        ).exists())

        context = build_translation_context(
            self.project,
            scope_type=PromptFieldTranslation.ScopeType.SHOT,
            scope_id=self.shot.id,
            field_key="shot.detailed_description",
        )
        entry = PromptFieldTranslation.objects.create(
            project=self.project,
            scope_type=PromptFieldTranslation.ScopeType.SHOT,
            scope_id=self.shot.id,
            field_key="shot.detailed_description",
            source_text=source,
            source_hash=source_text_hash(source),
            translated_text=translated,
            runtime_metadata={
                "translation_context_hash": translation_context_hash(context),
                "translation_context_version": TRANSLATION_CONTEXT_VERSION,
            },
        )
        confirmed_translation = self.client.post(
            reverse("translation-confirm", args=[entry.id]),
            {"expected_revision": entry.revision}, format="json",
        )
        self.assertEqual(confirmed_translation.status_code, 200, confirmed_translation.data)
        self.assertEqual(confirmed_translation.data["promoted_fields"], [{
            "field_key": "shot.detailed_description",
            "value": translated,
            "dialogue_index": None,
        }])
        self.shot.refresh_from_db()
        self.assertEqual(self.shot.detailed_description, translated)
        self.assertIn("shot_detail_provenance", self.shot.metadata)

    def test_rejects_image_shot_and_busy_runtime_without_writing_a_draft(self):
        self.shot.media_type = Shot.MediaType.IMAGE
        self.shot.save(update_fields=("media_type", "updated_at"))
        with patch("studio.services.shot_detail_drafts.ensure_creative_resources_available"), patch(
            "studio.services.shot_detail_drafts._require_runtime",
        ):
            image_response = self.client.post(
                f"/api/v1/projects/{self.project.id}/shot-detail-drafts/generate/",
                {"shot_id": str(self.shot.id)}, format="json",
            )
        self.assertEqual(image_response.status_code, 400, image_response.data)
        self.assertFalse(ShotDetailDraft.objects.exists())

        self.shot.media_type = Shot.MediaType.VIDEO
        self.shot.save(update_fields=("media_type", "updated_at"))
        with patch(
            "studio.services.shot_detail_drafts.ensure_creative_resources_available",
            side_effect=CreativeResourceBusy("shared local Qwen runtime is busy"),
        ):
            busy_response = self.client.post(
                f"/api/v1/projects/{self.project.id}/shot-detail-drafts/generate/",
                {"shot_id": str(self.shot.id)}, format="json",
            )
        self.assertEqual(busy_response.status_code, 409, busy_response.data)
        self.assertFalse(ShotDetailDraft.objects.exists())

        generated, _ = self._generate()
        self.assertEqual(generated.status_code, 201, generated.data)
        self.shot.media_type = Shot.MediaType.IMAGE
        self.shot.save(update_fields=("media_type", "updated_at"))
        stale_media = self._confirm(generated.data)
        self.assertEqual(stale_media.status_code, 400, stale_media.data)
        self.assertEqual(
            ShotDetailDraft.objects.get(pk=generated.data["id"]).status,
            ShotDetailDraft.Status.DRAFT,
        )

    def test_manual_edit_revalidates_canonical_terms_and_english_scripts(self):
        generated, _ = self._generate()
        invalid = {
            **generated.data["draft_data"],
            "detail_en": "A detective stands at the left shelf with the right hand visible.",
        }
        response = self.client.patch(
            f"/api/v1/shot-detail-drafts/{generated.data['id']}/",
            {"expected_revision": generated.data["revision"], "draft_data": invalid},
            format="json",
        )
        self.assertEqual(response.status_code, 200, response.data)
        self.assertEqual(response.data["status"], ShotDetailDraft.Status.DRAFT)
        self.assertEqual(response.data["validation"]["status"], "needs_review")
        self.assertTrue(any(
            issue["code"] == "canonical_glossary_missing"
            for issue in response.data["validation"]["review_issues"]
        ))

        serializer = ShotDetailDraftDataSerializer(data={
            "schema_version": 1,
            "detail_zh": "中文细节",
            "detail_en": "safe director prose\u202e hidden",
        })
        self.assertTrue(serializer.is_valid(), serializer.errors)
        serializer = ShotDetailDraftDataSerializer(data={
            "schema_version": 1,
            "detail_zh": "中文细节",
            "detail_en": "Greek α director prose",
        })
        self.assertTrue(serializer.is_valid(), serializer.errors)

    def test_high_detail_review_accepts_bilateral_equivalence_and_rejects_subject_side_swaps(self):
        bilateral = self.candidate()
        bilateral["detail_zh"] = (
            "两名守卫从两侧逼近，另外两人分左右包抄。雨水沿着地面漫开，"
            "镜头保持中景并让门口与窗边同时可读。"
        )
        bilateral["detail_en"] = (
            "Two guards close in from opposite sides while two others encircle from both sides. "
            "Rain spreads across the ground as the medium framing keeps the doorway and window readable."
        )
        accepted, _ = self._generate(bilateral)
        self.assertEqual(accepted.status_code, 201, accepted.data)
        self.assertEqual(accepted.data["validation"]["status"], "ready")

        swapped = self.candidate()
        swapped["detail_zh"] = "S1站在左侧，S2站在右侧，雨声持续压住空旷走廊。"
        swapped["detail_en"] = "S1 stands on the right and S2 stands on the left as rain fills the empty corridor."
        rejected, _ = self._generate(swapped)
        self.assertEqual(rejected.status_code, 201, rejected.data)
        self.assertEqual(rejected.data["status"], ShotDetailDraft.Status.DRAFT)
        self.assertEqual(rejected.data["validation"]["status"], "needs_review")
        self.assertTrue(any(
            issue["code"] == "direction_semantics_mismatch"
            for issue in rejected.data["validation"]["review_issues"]
        ))

    def test_parseable_review_candidate_is_editable_and_requires_acknowledgement(self):
        candidate = self.candidate()
        candidate["detail_en"] = "Lin Mo turns right while 中文 remains here."
        generated, _ = self._generate(candidate)
        self.assertEqual(generated.status_code, 201, generated.data)
        self.assertEqual(generated.data["status"], ShotDetailDraft.Status.DRAFT)
        self.assertEqual(generated.data["validation"]["status"], "needs_review")
        self.assertEqual(generated.data["draft_data"]["detail_en"], candidate["detail_en"])
        self.assertTrue(generated.data["can_edit"])
        self.assertFalse(generated.data["can_apply"])
        denied = self._confirm(generated.data)
        self.assertEqual(denied.status_code, 400, denied.data)
        confirmed = self._confirm(generated.data, review_acknowledged=True)
        self.assertEqual(confirmed.status_code, 200, confirmed.data)
        # The English CJK issue is explicitly marked requires_edit, so a human
        # must replace it before application even after acknowledging it.
        denied_apply = self._apply(confirmed.data, review_acknowledged=True)
        self.assertEqual(denied_apply.status_code, 400, denied_apply.data)

    def test_risk_only_candidate_can_apply_after_explicit_human_acknowledgement(self):
        candidate = self.candidate()
        candidate["detail_en"] = candidate["detail_en"].replace(
            "left archive shelf", "right archive shelf",
        )
        generated, _ = self._generate(candidate)
        self.assertEqual(generated.status_code, 201, generated.data)
        self.assertEqual(generated.data["validation"]["status"], "needs_review")
        self.assertFalse(any(
            issue["severity"] == "requires_edit"
            for issue in generated.data["validation"]["review_issues"]
        ))
        denied_confirm = self._confirm(generated.data)
        self.assertEqual(denied_confirm.status_code, 400, denied_confirm.data)
        confirmed = self._confirm(generated.data, review_acknowledged=True)
        self.assertEqual(confirmed.status_code, 200, confirmed.data)
        denied_apply = self._apply(confirmed.data)
        self.assertEqual(denied_apply.status_code, 400, denied_apply.data)
        applied = self._apply(confirmed.data, review_acknowledged=True)
        self.assertEqual(applied.status_code, 200, applied.data)
        self.assertEqual(applied.data["draft"]["status"], ShotDetailDraft.Status.APPLIED)
        self.assertTrue(applied.data["draft"]["runtime_metadata"]["review_acknowledgements"])

    def test_structurally_empty_candidate_becomes_source_based_editable_fallback(self):
        generated, _ = self._generate({
            "schema_version": 1, "detail_zh": "", "detail_en": "",
        })
        self.assertEqual(generated.status_code, 201, generated.data)
        self.assertEqual(generated.data["status"], ShotDetailDraft.Status.DRAFT)
        self.assertEqual(generated.data["validation"]["status"], "needs_review")
        self.assertIn("镜头运动：", generated.data["draft_data"]["detail_zh"])
        self.assertEqual(generated.data["draft_data"]["detail_en"], "")
        self.assertTrue(generated.data["runtime_metadata"]["fallback_generated"])
        self.assertTrue(any(
            issue["code"] == "model_output_unusable_fallback"
            for issue in generated.data["validation"]["review_issues"]
        ))

    def test_runtime_validation_error_becomes_editable_source_fallback_without_second_model_call(self):
        runtime_error = CreativeDraftError(
            "raw parser details must not reach the API",
            code="shot_detail_validation_failed",
            runtime_metadata={"phase": "primary_validation", "primary_completion": {
                "finish_reason": "stop", "output_chars": 6211, "output_tokens": 1793,
            }},
        )
        with patch("studio.services.shot_detail_drafts.ensure_creative_resources_available"), patch(
            "studio.services.shot_detail_drafts._require_runtime",
        ), patch(
            "studio.services.shot_detail_drafts._run_runtime", side_effect=runtime_error,
        ) as run_runtime:
            response = self.client.post(
                f"/api/v1/projects/{self.project.id}/shot-detail-drafts/generate/",
                {"shot_id": str(self.shot.id)}, format="json",
            )

        self.assertEqual(response.status_code, 201, response.data)
        self.assertEqual(response.data["status"], ShotDetailDraft.Status.DRAFT)
        self.assertEqual(response.data["draft_data"]["detail_en"], "")
        self.assertIn(self.shot.camera, response.data["draft_data"]["detail_zh"])
        self.assertNotIn("raw parser details", response.content.decode("utf-8"))
        self.assertEqual(run_runtime.call_count, 1)
        self.assertTrue(response.data["runtime_metadata"]["fallback_generated"])


class ShotDetailRuntimeValidationTests(TestCase):
    def test_server_direction_semantics_excludes_ordinary_left_right_words(self):
        self.assertEqual(
            direction_semantic_mismatches("她离开房间。", "She left the room."), [],
        )
        self.assertEqual(
            direction_semantic_mismatches("现在就行动。", "Act right now."), [],
        )
        self.assertEqual(
            direction_semantic_mismatches("两侧都有守卫。", "Guards stand on either side."), [],
        )
        self.assertEqual(
            direction_semantic_mismatches("她靠近门口。", "She approaches from either side."), [],
        )

    def test_runtime_keeps_parseable_language_issues_for_human_review(self):
        runtime_path = Path(__file__).resolve().parents[3] / "scripts" / "qwen_translate_once.py"
        spec = importlib.util.spec_from_file_location("qwen_detail_runtime_for_test", runtime_path)
        runtime = importlib.util.module_from_spec(spec)
        assert spec and spec.loader
        sys.modules[spec.name] = runtime
        spec.loader.exec_module(runtime)
        request = SimpleNamespace(glossary={})
        for invalid_english in (
            "日本語 director prose", "한국어 director prose", "Русский prose",
            "Greek α director prose", "Arabic العربية prose",
        ):
            issues = []
            data = runtime._validate_shot_high_detail({
                "schema_version": 1,
                "detail_zh": "中文细节说明。",
                "detail_en": invalid_english,
            }, request, review_issues=issues)
            self.assertEqual(data["detail_en"], invalid_english)
            self.assertTrue(issues)
        with self.assertRaises(RuntimeError):
            runtime._validate_shot_high_detail({
                "schema_version": 1,
                "detail_zh": "",
                "detail_en": "",
            }, request)

    def test_runtime_materialises_omitted_constant_schema_version_for_json_object_mode(self):
        runtime_path = Path(__file__).resolve().parents[3] / "scripts" / "qwen_translate_once.py"
        spec = importlib.util.spec_from_file_location("qwen_detail_json_object_for_test", runtime_path)
        runtime = importlib.util.module_from_spec(spec)
        assert spec and spec.loader
        sys.modules[spec.name] = runtime
        spec.loader.exec_module(runtime)
        candidate = runtime._validate_shot_high_detail({
            "detail_zh": "镜头缓慢向右移动，人物停在门口。",
            "detail_en": "The camera slowly moves right as the subject stops at the doorway.",
        }, SimpleNamespace(glossary={}), review_issues=[])
        self.assertEqual(candidate["schema_version"], 1)
        self.assertIn("detail_en", candidate)

    def test_runtime_normalises_one_known_wrapper_aliases_and_extra_keys_for_review(self):
        runtime_path = Path(__file__).resolve().parents[3] / "scripts" / "qwen_translate_once.py"
        spec = importlib.util.spec_from_file_location("qwen_detail_shape_for_test", runtime_path)
        runtime = importlib.util.module_from_spec(spec)
        assert spec and spec.loader
        sys.modules[spec.name] = runtime
        spec.loader.exec_module(runtime)
        issues = []
        candidate = runtime._validate_shot_high_detail({
            "schema_version": 1,
            "result": {
                "chinese_detail": "镜头缓慢向右横移，人物停在门口。",
                "english": "The camera slowly tracks right as the subject stops at the doorway.",
                "explanation": "ignored operator note",
            },
        }, SimpleNamespace(glossary={}), review_issues=issues)
        self.assertEqual(candidate["schema_version"], 1)
        self.assertEqual(candidate["detail_zh"], "镜头缓慢向右横移，人物停在门口。")
        self.assertTrue(any(
            issue["code"] == "model_output_shape_normalized" for issue in issues
        ))

    def test_runtime_keeps_single_known_language_field_as_editable_review_candidate(self):
        runtime_path = Path(__file__).resolve().parents[3] / "scripts" / "qwen_translate_once.py"
        spec = importlib.util.spec_from_file_location("qwen_detail_partial_for_test", runtime_path)
        runtime = importlib.util.module_from_spec(spec)
        assert spec and spec.loader
        sys.modules[spec.name] = runtime
        spec.loader.exec_module(runtime)
        issues = []
        candidate = runtime._validate_shot_high_detail({
            "detail_cn": "镜头缓慢向右横移，人物停在门口。",
        }, SimpleNamespace(glossary={}), review_issues=issues)
        self.assertEqual(candidate["detail_en"], "")
        self.assertTrue(any(issue["code"] == "detail_en_missing" for issue in issues))
