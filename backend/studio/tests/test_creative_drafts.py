from copy import deepcopy
from pathlib import Path
import subprocess
import tempfile
from types import SimpleNamespace
from unittest.mock import patch
from uuid import uuid4

from django.contrib.auth import get_user_model
from django.core.exceptions import ImproperlyConfigured
from django.test import TestCase, override_settings
from rest_framework.test import APIClient

from studio.models import (
    CharacterBible,
    Episode,
    EpisodeCreativeDraft,
    PromptArtifact,
    PromptFieldTranslation,
    PropBible,
    SceneBible,
    SeriesProject,
    Shot,
    Style,
)
from studio.services.creative_drafts import (
    CreativeDraftError,
    CreativeResourceBusy,
    CreativeValidationError,
    _canonical_storyboard_narrative,
    _recommended_shot_durations_ms,
    _script_action_coverage_issue,
    _run_runtime,
    _runtime_request,
    creative_context_hash,
    creative_context_snapshot,
    creative_config,
    creative_provider_config,
    ensure_creative_resources_available,
    required_dialogue_manifest,
    source_timed_visual_segments,
    validate_creative_draft_data,
)
from studio.services.h3_prompts import H3PromptOptions, build_h3_video_prompt
from studio.services.local_qwen_lock import local_qwen_runtime_lock


User = get_user_model()


@override_settings(
    LOCAL_QWEN_CREATIVE={
        "enabled": True,
        "model_id": "Qwen/Qwen3-4B-GGUF",
        "model_dir": "../.runtime/models/Qwen/Qwen3-4B-GGUF",
        "runtime_script": "../scripts/qwen_translate_once.py",
        "runtime_python": "../.runtime/qwen-python/Scripts/python.exe",
        "device": "cpu",
        "timeout_seconds": 900,
        "process_cleanup_grace_seconds": 60,
        "max_new_tokens": 3072,
        "context_size": 8192,
        "min_context_size": 8192,
        "max_context_size": 32768,
        "reserved_input_tokens": 2048,
        "min_shots": 1,
        "recommended_max_shots": 4,
        "batch_shots": 4,
        "max_dialogue_per_shot": 2,
        "max_episode_title_chars": 80,
        "max_dialogue_text_chars": 240,
        "max_dialogue_delivery_chars": 100,
        "max_dialogue_delivery_en_chars": 150,
        "min_shot_duration_ms": 5000,
        "max_shot_duration_ms": 15000,
        "output_min_tokens": 1024,
        "output_base_tokens": 768,
        "output_tokens_per_shot": 384,
        "output_tokens_per_dialogue": 128,
        "output_dialogue_char_threshold": 32,
        "max_source_chars": 16000,
        "max_regeneration_context_chars": 16000,
        "max_input_chars": 24000,
        "output_headroom_per_shot": 0,
    },
    DEEPSEEK_API={
        "enabled": True,
        "base_url": "https://api.deepseek.com",
        "api_key": "test-secret-key",
        "model": "deepseek-v4-flash",
        "timeout_seconds": 180,
        "max_retries": 0,
        "max_tokens": 16384,
    },
    DEEPSEEK_CREATIVE={
        "min_shots": 1,
        "recommended_max_shots": 4,
        "batch_shots": 16,
        "max_dialogue_per_shot": 2,
        "max_episode_title_chars": 80,
        "max_dialogue_text_chars": 240,
        "max_dialogue_delivery_chars": 100,
        "max_dialogue_delivery_en_chars": 150,
        "min_shot_duration_ms": 5000,
        "max_shot_duration_ms": 15000,
        "output_min_tokens": 1024,
        "output_base_tokens": 768,
        "output_tokens_per_shot": 384,
        "output_tokens_per_dialogue": 128,
        "output_dialogue_char_threshold": 32,
        "max_source_chars": 24000,
        "max_regeneration_context_chars": 96000,
        "max_input_chars": 120000,
        "output_headroom_per_shot": 384,
    },
    SERIES_CONCEPT_DRAFT_PREFERRED_PROVIDER="deepseek_api",
    SERIES_CONCEPT_DRAFT_PROVIDER_SUITABILITY={
        "local_qwen": "experimental",
        "deepseek_api": "recommended",
    },
)
class CreativeDraftAPITests(TestCase):
    def setUp(self):
        self.user = User.objects.create_user(username="creator", password="password123")
        self.other = User.objects.create_user(username="other", password="password123")
        self.client = APIClient()
        self.client.force_authenticate(self.user)
        self.style = Style.objects.create(
            owner=self.user, slug="wuxia-creative", name="武侠创作", category="wuxia",
            visual_prompt="雨夜写实武侠电影，克制冷色调。",
        )
        self.project = SeriesProject.objects.create(
            owner=self.user, title="听雨不归人", logline="雨夜追凶。",
            master_script="凌霜在雨夜客栈拔剑。凌霜：别回头。",
            style=self.style, continuity_summary="凌霜始终穿灰色窄袖劲装。",
            production_settings={
                "translation_glossary": {"凌霜": "Ling Shuang", "雨夜客栈": "Rainy Night Inn"},
                "default_h3_mode": Shot.H3Mode.T2VA,
            },
        )
        self.episode = Episode.objects.create(
            project=self.project, number=1, title="雨夜试剑",
            synopsis="凌霜在客栈察觉杀机。",
            script="凌霜在雨夜客栈拔剑。\n凌霜：别回头。",
        )
        self.character = CharacterBible.objects.create(
            project=self.project, slug="ling-shuang", name="凌霜",
            stable_speaker_id="S1", visual_identity="灰衣剑客",
            wardrobe="灰色窄袖劲装", face_identity="右眉浅疤",
        )
        self.scene = SceneBible.objects.create(
            project=self.project, slug="rain-inn", name="雨夜客栈",
            visual_identity="木构客栈，雨幕", lighting="暖灯与冷雨",
        )

    def candidate(self, *, count=2, h3_mode=Shot.H3Mode.T2VA):
        shots = []
        for index in range(1, count + 1):
            shots.append({
                "client_key": f"shot-{index:03d}", "order": index,
                "title_zh": f"凌霜察觉杀机{index}",
                "title_en": f"Ling Shuang Senses Danger {index}",
                "narrative_zh": "凌霜立在雨夜客栈门内，灰衣与右眉浅疤保持不变。",
                "narrative_en": "Ling Shuang stands inside the Rainy Night Inn; the gray outfit and shallow right-eyebrow scar remain unchanged.",
                "camera_zh": "中景平视，镜头缓慢推近，停在人物上半身。",
                "camera_en": "Eye-level medium shot, slowly push in and settle on the upper body.",
                "action_zh": "凌霜右手按住剑鞘，最终静止。",
                "action_en": "Ling Shuang presses the right hand against the scabbard, then holds still.",
                "continuity_zh": "灰色窄袖劲装与右眉浅疤不变。",
                "continuity_en": "Keep the gray narrow-sleeved outfit and shallow right-eyebrow scar unchanged.",
                "soundscape_zh": "近处雨声与木门轻响。",
                "soundscape_en": "Nearby rain and a soft wooden-door creak.",
                "music_zh": "低沉古琴单音。",
                "music_en": "Sparse low guqin notes.",
                "duration_ms": 5000, "media_type": "video", "h3_mode": h3_mode,
                "include_audio": True,
                "requires_first_frame_continuity": index > 1,
                "requires_last_frame_continuity": index < count,
                "scene_id": str(self.scene.id),
                "character_ids": [str(self.character.id)], "prop_ids": [],
                "dialogue": ([{
                    "speaker_id": "S1", "text_zh": "别回头。",
                    "delivery_zh": "低声", "delivery_en": "in a low voice",
                    "voiceover": False,
                }] if index == 1 else []),
            })
        return {
            "schema_version": 1,
            "shots": shots,
        }

    def runtime_side_effect(self, captured, candidate=None):
        candidate = candidate or self.candidate()

        def invoke(request, config):
            captured.update(request)
            return {
                "ok": True,
                "result": {**deepcopy(candidate), "source_fingerprint": request["context_hash"]},
                "runtime": {"model": "Qwen/Qwen3-4B-GGUF", "peak_working_set_bytes": 5_000_000_000},
            }

        return invoke

    def generate(self, *, count=2, candidate=None, creativity="bold", note="加强悬念"):
        captured = {}
        with patch("studio.services.creative_drafts._require_runtime"), patch(
            "studio.services.creative_drafts._run_runtime",
            side_effect=self.runtime_side_effect(captured, candidate or self.candidate(count=count)),
        ):
            response = self.client.post(
                f"/api/v1/projects/{self.project.id}/creative-drafts/generate/",
                {
                    "episode_id": str(self.episode.id), "target_shot_count": count,
                    "creativity": creativity, "variation_note": note,
                }, format="json",
            )
        return response, captured

    def test_generate_persists_isolated_bilingual_draft_and_runtime_controls(self):
        response, captured = self.generate()
        self.assertEqual(response.status_code, 201, response.data)
        self.assertEqual(response.data["status"], EpisodeCreativeDraft.Status.DRAFT)
        self.assertEqual(response.data["validation"]["status"], "ready")
        self.assertEqual(Shot.objects.count(), 0)
        self.assertEqual(captured["operation"], "creative_storyboard")
        self.assertEqual(captured["feedback"], "加强悬念")
        self.assertEqual(captured["options"]["creativity"], "bold")
        self.assertEqual(captured["options"]["shot_count"], 2)
        self.assertEqual(captured["glossary"]["凌霜"], "Ling Shuang")
        self.assertEqual(captured["max_new_tokens"], 1664)
        self.assertEqual(len(captured["required_dialogue_manifest"]), 1)
        self.assertEqual(captured["required_dialogue_manifest"][0]["text_zh"], "别回头。")
        self.assertEqual(captured["timeout_seconds"], 900)
        self.assertEqual(captured["context_size"], 8192)
        self.assertEqual(captured["seed"], self.project.continuity_seed + 1)
        self.assertEqual(captured["episode"]["script"], self.episode.script)
        self.assertEqual(
            response.data["draft_data"]["episode"]["title_zh"], self.episode.title,
        )
        self.assertEqual(
            response.data["draft_data"]["episode"]["synopsis_zh"], self.episode.synopsis,
        )
        self.assertEqual(response.data["draft_data"]["episode"]["title_en"], "")
        self.assertEqual(response.data["draft_data"]["episode"]["synopsis_en"], "")

    def test_inline_timed_script_dialogue_is_required_and_applied(self):
        self.episode.script = (
            "【1 [首帧连续:否] [尾帧连续:是]: 凌霜在雨夜客栈拔剑；"
            "[1-3.5秒] 凌霜（低声）: 别回头。】"
        )
        self.episode.save(update_fields=("script", "updated_at"))
        snapshot = creative_context_snapshot(self.project, self.episode)
        self.assertEqual(
            required_dialogue_manifest(snapshot, include_audio=True),
            [{
                "speaker_id": "S1",
                "text_zh": "别回头。",
                "source_order": 1,
                "source_delivery_zh": "低声",
                "start_ms": 1000,
                "end_ms": 3500,
            }],
        )

        candidate = self.candidate(count=1)
        candidate["shots"][0]["dialogue"] = []
        generated, _captured = self.generate(count=1, candidate=candidate)
        self.assertEqual(generated.status_code, 201, generated.data)
        self.assertEqual(
            generated.data["draft_data"]["shots"][0]["dialogue"][0]["start_ms"],
            1000,
        )
        self.assertEqual(
            generated.data["draft_data"]["shots"][0]["dialogue"][0]["end_ms"],
            3500,
        )
        confirmed = self.client.post(
            f"/api/v1/creative-drafts/{generated.data['id']}/confirm/",
            {
                "expected_revision": generated.data["revision"],
                "review_acknowledged": True,
            },
            format="json",
        )
        self.assertEqual(confirmed.status_code, 200, confirmed.data)
        applied = self.client.post(
            f"/api/v1/creative-drafts/{generated.data['id']}/apply/",
            {
                "expected_revision": confirmed.data["revision"],
                "language_mode": "chinese_only",
                "placement": "append",
                "review_acknowledged": True,
            },
            format="json",
        )
        self.assertEqual(applied.status_code, 200, applied.data)
        shot = Shot.objects.get()
        self.assertEqual(shot.dialogue, [{
            "speaker_id": "S1",
            "text": "别回头。",
            "delivery": "低声",
            "start_ms": 1000,
            "end_ms": 3500,
        }])

    def test_non_dialogue_source_time_ranges_are_sent_and_reviewed(self):
        self.episode.script = (
            "[0-2.5秒] 动作：凌霜绕过桌案，右手压住剑柄。\n"
            "[2.5-5秒] 凌霜：别回头。"
        )
        self.episode.save(update_fields=("script", "updated_at"))
        snapshot = creative_context_snapshot(self.project, self.episode)
        self.assertEqual(
            source_timed_visual_segments(snapshot),
            [{
                "source_order": 1,
                "start_ms": 0,
                "end_ms": 2500,
                "kind": "action",
                "text_zh": "凌霜绕过桌案，右手压住剑柄",
            }],
        )

        candidate = self.candidate(count=1)
        candidate["shots"][0]["narrative_zh"] = "凌霜绕过桌案，右手压住剑柄。"
        candidate["shots"][0]["action_zh"] = "凌霜绕过桌案，最终停步。"
        generated, captured = self.generate(count=1, candidate=candidate)
        self.assertEqual(generated.status_code, 201, generated.data)
        self.assertEqual(
            captured["source_timed_visual_segments"][0]["start_ms"], 0,
        )
        self.assertEqual(
            captured["source_timed_visual_segments"][0]["end_ms"], 2500,
        )
        self.assertIn(
            ("source_timing_range_missing", "shots"),
            {
                (issue["code"], issue["path"])
                for issue in generated.data["validation"]["review_issues"]
            },
        )

    def test_worker_resource_lease_maps_creative_generate_to_409(self):
        with tempfile.TemporaryDirectory() as temp, override_settings(
            LOCAL_QWEN_LOCK_PATH=str(Path(temp) / "shared-local-ai.lock"),
        ), patch("studio.services.creative_drafts._require_runtime"):
            with local_qwen_runtime_lock():
                response = self.client.post(
                    f"/api/v1/projects/{self.project.id}/creative-drafts/generate/",
                    {
                        "episode_id": str(self.episode.id),
                        "target_shot_count": 1,
                        "creativity": "balanced",
                    },
                    format="json",
                )

        self.assertEqual(response.status_code, 409, response.data)
        self.assertEqual(Shot.objects.count(), 0)

    def test_confirm_does_not_write_shots_then_chinese_apply_does(self):
        response, _ = self.generate()
        draft_id = response.data["id"]
        confirmed = self.client.post(
            f"/api/v1/creative-drafts/{draft_id}/confirm/",
            {"expected_revision": response.data["revision"]}, format="json",
        )
        self.assertEqual(confirmed.status_code, 200, confirmed.data)
        self.assertEqual(confirmed.data["status"], EpisodeCreativeDraft.Status.CONFIRMED)
        self.assertEqual(Shot.objects.count(), 0)
        applied = self.client.post(
            f"/api/v1/creative-drafts/{draft_id}/apply/",
            {
                "expected_revision": confirmed.data["revision"],
                "language_mode": "chinese_only", "placement": "append",
            }, format="json",
        )
        self.assertEqual(applied.status_code, 200, applied.data)
        self.assertEqual(len(applied.data["applied_shot_ids"]), 2)
        self.assertEqual(applied.data["translations_written"], 0)
        self.assertEqual(Shot.objects.count(), 2)
        self.assertEqual(PromptFieldTranslation.objects.count(), 0)
        first_shot, second_shot = Shot.objects.order_by("number")
        self.assertFalse(first_shot.requires_first_frame_continuity)
        self.assertTrue(first_shot.requires_last_frame_continuity)
        self.assertTrue(second_shot.requires_first_frame_continuity)
        self.assertFalse(second_shot.requires_last_frame_continuity)

    def test_applying_new_draft_demotes_previous_applied_draft(self):
        first, _ = self.generate(count=1)
        first_confirmed = self.client.post(
            f"/api/v1/creative-drafts/{first.data['id']}/confirm/",
            {"expected_revision": first.data["revision"]}, format="json",
        )
        self.assertEqual(first_confirmed.status_code, 200, first_confirmed.data)
        first_applied = self.client.post(
            f"/api/v1/creative-drafts/{first.data['id']}/apply/",
            {
                "expected_revision": first_confirmed.data["revision"],
                "language_mode": "chinese_only",
                "placement": "append",
            },
            format="json",
        )
        self.assertEqual(first_applied.status_code, 200, first_applied.data)

        second, _ = self.generate(count=1)
        second_confirmed = self.client.post(
            f"/api/v1/creative-drafts/{second.data['id']}/confirm/",
            {"expected_revision": second.data["revision"]}, format="json",
        )
        self.assertEqual(second_confirmed.status_code, 200, second_confirmed.data)
        second_applied = self.client.post(
            f"/api/v1/creative-drafts/{second.data['id']}/apply/",
            {
                "expected_revision": second_confirmed.data["revision"],
                "language_mode": "chinese_only",
                "placement": "append",
            },
            format="json",
        )
        self.assertEqual(second_applied.status_code, 200, second_applied.data)

        first_draft = EpisodeCreativeDraft.objects.get(pk=first.data["id"])
        second_draft = EpisodeCreativeDraft.objects.get(pk=second.data["id"])
        self.assertEqual(first_draft.status, EpisodeCreativeDraft.Status.CONFIRMED)
        self.assertIsNone(first_draft.applied_at)
        self.assertEqual(first_draft.applied_shot_ids, [])
        self.assertEqual(second_draft.status, EpisodeCreativeDraft.Status.APPLIED)
        self.assertEqual(
            EpisodeCreativeDraft.objects.filter(
                episode=self.episode,
                status=EpisodeCreativeDraft.Status.APPLIED,
            ).count(),
            1,
        )
        listed = self.client.get(
            f"/api/v1/projects/{self.project.id}/creative-drafts/",
            {"episode": str(self.episode.id)},
        )
        self.assertEqual(listed.status_code, 200, listed.data)
        statuses = {item["generation_no"]: item["status"] for item in listed.data}
        self.assertEqual(statuses[1], EpisodeCreativeDraft.Status.CONFIRMED)
        self.assertEqual(statuses[2], EpisodeCreativeDraft.Status.APPLIED)

    def test_replace_with_existing_production_records_creates_new_episode_shot_version(self):
        first, _ = self.generate(count=1)
        first_confirmed = self.client.post(
            f"/api/v1/creative-drafts/{first.data['id']}/confirm/",
            {"expected_revision": first.data["revision"]}, format="json",
        )
        first_applied = self.client.post(
            f"/api/v1/creative-drafts/{first.data['id']}/apply/",
            {
                "expected_revision": first_confirmed.data["revision"],
                "language_mode": "chinese_only",
                "placement": "replace",
            },
            format="json",
        )
        self.assertEqual(first_applied.status_code, 200, first_applied.data)
        original_shot = Shot.objects.get()
        PromptArtifact.objects.create(
            project=self.project,
            episode=self.episode,
            shot=original_shot,
            created_by=self.user,
            prompt_type=PromptArtifact.PromptType.VIDEO,
            prompt_text="verified prompt",
            content_hash="a" * 64,
        )

        second, _ = self.generate(count=1)
        second_confirmed = self.client.post(
            f"/api/v1/creative-drafts/{second.data['id']}/confirm/",
            {"expected_revision": second.data["revision"]}, format="json",
        )
        second_applied = self.client.post(
            f"/api/v1/creative-drafts/{second.data['id']}/apply/",
            {
                "expected_revision": second_confirmed.data["revision"],
                "language_mode": "chinese_only",
                "placement": "replace",
            },
            format="json",
        )

        self.assertEqual(second_applied.status_code, 200, second_applied.data)
        shots = list(Shot.objects.order_by("number"))
        self.assertEqual(len(shots), 2)
        self.assertEqual(shots[0].id, original_shot.id)
        self.assertFalse(shots[0].metadata["creative_version_active"])
        self.assertEqual(shots[0].metadata["creative_version_mode"], "direct_replace")
        self.assertEqual(shots[1].metadata["creative_version_mode"], "versioned_replace")
        self.assertTrue(shots[1].metadata["creative_version_active"])
        self.assertEqual(shots[1].metadata["creative_version_shot_number"], 1)
        self.assertEqual(shots[1].number, 2)
        self.assertEqual(Shot.objects.filter(prompt_artifacts__isnull=False).count(), 1)

    def test_sync_shots_preserves_hidden_creative_versions(self):
        first, _ = self.generate(count=1)
        first_confirmed = self.client.post(
            f"/api/v1/creative-drafts/{first.data['id']}/confirm/",
            {"expected_revision": first.data["revision"]}, format="json",
        )
        first_applied = self.client.post(
            f"/api/v1/creative-drafts/{first.data['id']}/apply/",
            {
                "expected_revision": first_confirmed.data["revision"],
                "language_mode": "chinese_only",
                "placement": "replace",
            },
            format="json",
        )
        self.assertEqual(first_applied.status_code, 200, first_applied.data)
        original_shot = Shot.objects.get()
        PromptArtifact.objects.create(
            project=self.project,
            episode=self.episode,
            shot=original_shot,
            created_by=self.user,
            prompt_type=PromptArtifact.PromptType.VIDEO,
            prompt_text="verified prompt",
            content_hash="b" * 64,
        )
        second, _ = self.generate(count=1)
        second_confirmed = self.client.post(
            f"/api/v1/creative-drafts/{second.data['id']}/confirm/",
            {"expected_revision": second.data["revision"]}, format="json",
        )
        second_applied = self.client.post(
            f"/api/v1/creative-drafts/{second.data['id']}/apply/",
            {
                "expected_revision": second_confirmed.data["revision"],
                "language_mode": "chinese_only",
                "placement": "replace",
            },
            format="json",
        )
        self.assertEqual(second_applied.status_code, 200, second_applied.data)
        active_shot = Shot.objects.order_by("-number").first()

        synced = self.client.post(
            f"/api/v1/episodes/{self.episode.id}/sync-shots/",
            {
                "shot_version_key": active_shot.metadata["creative_version_key"],
                "shots": [{
                    "id": str(active_shot.id),
                    "number": 1,
                    "title": "当前版本已编辑",
                    "narrative": active_shot.narrative,
                    "camera": active_shot.camera,
                    "dialogue": active_shot.dialogue,
                    "duration_ms": active_shot.duration_ms,
                    "media_type": active_shot.media_type,
                    "h3_mode": active_shot.h3_mode,
                    "include_audio": active_shot.include_audio,
                    "scene": str(self.scene.id),
                    "characters": [str(self.character.id)],
                    "props": [],
                    "reference_assets": [],
                    "reference_bindings": {},
                    "metadata": active_shot.metadata,
                }],
            },
            format="json",
        )

        self.assertEqual(synced.status_code, 200, synced.data)
        self.assertEqual(Shot.objects.count(), 2)
        original_shot.refresh_from_db()
        active_shot.refresh_from_db()
        self.assertFalse(original_shot.metadata["creative_version_active"])
        self.assertTrue(active_shot.metadata["creative_version_active"])
        self.assertEqual(active_shot.title, "当前版本已编辑")

    def test_apply_never_overwrites_authoritative_episode_source(self):
        original_title = self.episode.title
        original_synopsis = self.episode.synopsis
        generated, _ = self.generate(count=1)
        confirmed = self.client.post(
            f"/api/v1/creative-drafts/{generated.data['id']}/confirm/",
            {"expected_revision": generated.data["revision"]}, format="json",
        )
        self.assertEqual(confirmed.status_code, 200, confirmed.data)
        applied = self.client.post(
            f"/api/v1/creative-drafts/{generated.data['id']}/apply/",
            {
                "expected_revision": confirmed.data["revision"],
                "language_mode": "chinese_only", "placement": "append",
            }, format="json",
        )
        self.assertEqual(applied.status_code, 200, applied.data)
        self.episode.refresh_from_db()
        self.assertEqual(self.episode.title, original_title)
        self.assertEqual(self.episode.synopsis, original_synopsis)

    def test_long_authoritative_synopsis_survives_generate_and_confirm(self):
        source_synopsis = "雨夜客栈的剑客保持警觉。" * 40
        self.assertGreater(len(source_synopsis), 240)
        self.episode.synopsis = source_synopsis
        self.episode.save(update_fields=("synopsis", "updated_at"))

        generated, _ = self.generate(count=1)
        self.assertEqual(generated.status_code, 201, generated.data)
        self.assertEqual(
            generated.data["draft_data"]["episode"]["synopsis_zh"], source_synopsis,
        )
        self.assertEqual(generated.data["draft_data"]["episode"]["synopsis_en"], "")
        confirmed = self.client.post(
            f"/api/v1/creative-drafts/{generated.data['id']}/confirm/",
            {"expected_revision": generated.data["revision"]}, format="json",
        )
        self.assertEqual(confirmed.status_code, 200, confirmed.data)
        self.assertEqual(
            confirmed.data["draft_data"]["episode"]["synopsis_zh"], source_synopsis,
        )

    def test_manual_nonblank_episode_synopsis_mismatch_is_reviewable_and_source_is_reprojected(self):
        source_synopsis = "凌霜右手按住玄纹剑。"
        self.episode.synopsis = source_synopsis
        self.episode.save(update_fields=("synopsis", "updated_at"))
        generated, _ = self.generate(count=1)
        self.assertEqual(generated.status_code, 201, generated.data)
        edited = deepcopy(generated.data["draft_data"])
        edited["episode"]["synopsis_zh"] = "人工尝试改写正式梗概。"
        edited["episode"]["synopsis_en"] = (
            "Ling Shuang presses the Xuanwen Sword with the left hand."
        )

        patched = self.client.patch(
            f"/api/v1/creative-drafts/{generated.data['id']}/",
            {"expected_revision": generated.data["revision"], "draft_data": edited},
            format="json",
        )

        self.assertEqual(patched.status_code, 200, patched.data)
        self.assertEqual(patched.data["status"], EpisodeCreativeDraft.Status.DRAFT)
        self.assertEqual(patched.data["validation"]["status"], "needs_review")
        self.assertEqual(
            patched.data["draft_data"]["episode"]["synopsis_zh"], source_synopsis,
        )
        self.assertTrue(any(
            issue["code"] == "direction_semantics_mismatch"
            and issue["path"] == "episode.synopsis_en"
            and issue["severity"] == "risk"
            for issue in patched.data["validation"]["review_issues"]
        ))

    def test_creative_review_uses_structured_direction_semantics(self):
        bilateral = self.candidate(count=1)
        bilateral["shots"][0]["narrative_zh"] = "两名杀手分左右包抄，另外两人从两侧逼近。"
        bilateral["shots"][0]["narrative_en"] = (
            "Two killers encircle from both sides while two others close in from opposite sides."
        )
        accepted, _ = self.generate(count=1, candidate=bilateral)
        self.assertEqual(accepted.status_code, 201, accepted.data)
        self.assertEqual(accepted.data["validation"]["status"], "ready")

        swapped = self.candidate(count=1)
        swapped["shots"][0]["narrative_zh"] = "S1站在左侧，S2站在右侧。"
        swapped["shots"][0]["narrative_en"] = "S1 stands on the right and S2 stands on the left."
        rejected, _ = self.generate(count=1, candidate=swapped)
        self.assertEqual(rejected.status_code, 201, rejected.data)
        self.assertEqual(rejected.data["status"], EpisodeCreativeDraft.Status.DRAFT)
        self.assertEqual(rejected.data["validation"]["status"], "needs_review")
        self.assertTrue(any(
            issue["code"] == "direction_semantics_mismatch"
            and issue["path"] == "shots[0].narrative_en"
            and issue["severity"] == "risk"
            for issue in rejected.data["validation"]["review_issues"]
        ))

    def test_cjk_english_patch_is_reviewable_and_repair_clears_issues(self):
        generated, _ = self.generate(count=1)
        edited = deepcopy(generated.data["draft_data"])
        edited["episode"]["title_en"] = "中文标题"

        patched = self.client.patch(
            f"/api/v1/creative-drafts/{generated.data['id']}/",
            {"expected_revision": generated.data["revision"], "draft_data": edited},
            format="json",
        )

        self.assertEqual(patched.status_code, 200, patched.data)
        self.assertEqual(patched.data["status"], EpisodeCreativeDraft.Status.DRAFT)
        self.assertEqual(patched.data["validation"]["status"], "needs_review")
        self.assertTrue(any(
            issue["code"] == "english_contains_cjk"
            and issue["path"] == "episode.title_en"
            and issue["severity"] == "risk"
            for issue in patched.data["validation"]["review_issues"]
        ))

        repaired = deepcopy(patched.data["draft_data"])
        repaired["episode"]["title_en"] = "Rainy Night Sword Trial"
        repaired_response = self.client.patch(
            f"/api/v1/creative-drafts/{generated.data['id']}/",
            {"expected_revision": patched.data["revision"], "draft_data": repaired},
            format="json",
        )
        self.assertEqual(repaired_response.status_code, 200, repaired_response.data)
        self.assertEqual(repaired_response.data["status"], EpisodeCreativeDraft.Status.DRAFT)
        self.assertEqual(repaired_response.data["validation"]["status"], "ready")
        self.assertEqual(repaired_response.data["validation"]["review_issues"], [])

    def test_bilingual_apply_requires_ack_and_writes_confirmed_overlays(self):
        generated, _ = self.generate(count=1, candidate=self.candidate(count=1))
        confirmed = self.client.post(
            f"/api/v1/creative-drafts/{generated.data['id']}/confirm/",
            {"expected_revision": generated.data["revision"]}, format="json",
        )
        denied = self.client.post(
            f"/api/v1/creative-drafts/{generated.data['id']}/apply/",
            {
                "expected_revision": confirmed.data["revision"],
                "language_mode": "bilingual_confirmed", "placement": "append",
            }, format="json",
        )
        self.assertEqual(denied.status_code, 400)
        applied = self.client.post(
            f"/api/v1/creative-drafts/{generated.data['id']}/apply/",
            {
                "expected_revision": confirmed.data["revision"],
                "language_mode": "bilingual_confirmed", "placement": "append",
                "english_review_acknowledged": True,
            }, format="json",
        )
        self.assertEqual(applied.status_code, 200, applied.data)
        self.assertGreater(applied.data["translations_written"], 0)
        self.assertTrue(PromptFieldTranslation.objects.filter(
            status=PromptFieldTranslation.Status.CONFIRMED,
            provider="local_qwen_creative",
        ).exists())
        shot = Shot.objects.get()
        candidate = generated.data["draft_data"]["shots"][0]
        self.assertEqual(candidate["narrative_en"], shot.narrative)
        self.assertNotIn(candidate["action_en"], shot.narrative)
        self.assertEqual(shot.action, "")
        self.assertEqual(shot.camera, candidate["camera_en"])
        self.assertEqual(shot.continuity_notes, candidate["continuity_en"])
        self.assertEqual(shot.sound_effects, candidate["soundscape_en"])
        self.assertEqual(shot.music, candidate["music_en"])
        self.assertEqual(shot.dialogue[0]["text"], candidate["dialogue"][0]["text_zh"])
        self.assertEqual(shot.dialogue[0]["delivery"], candidate["dialogue"][0]["delivery_en"])
        self.assertEqual(shot.metadata["prompt_field_version"], 2)
        self.assertEqual(shot.metadata["negative_prompt"], "")
        narrative_entry = PromptFieldTranslation.objects.get(
            scope_id=shot.id, field_key="shot.narrative",
        )
        self.assertEqual(narrative_entry.source_text, shot.narrative)
        self.assertEqual(
            narrative_entry.revision_history[0]["source_text"],
            candidate["action_zh"],
        )
        promotion = narrative_entry.runtime_metadata["promotion"]
        self.assertEqual(promotion["event"], "creative_translation_promoted")
        self.assertEqual(promotion["promoted_by"], str(self.user.id))
        self.assertEqual(
            promotion["promoted_value_sha256"], narrative_entry.source_hash,
        )

    def test_bilingual_apply_allows_acknowledged_canonical_target_collision(self):
        prop = PropBible.objects.create(
            project=self.project, slug="collision-sword", name="玄纹剑",
            visual_identity="a silver-gray sword with subtle dark patterns",
        )
        self.project.production_settings = {
            **(self.project.production_settings or {}),
            "translation_glossary": {
                self.character.name: "Ling Shuang",
                prop.name: "ling   shuang",
            },
        }
        self.project.save(update_fields=("production_settings", "updated_at"))
        generated, _ = self.generate(count=1, candidate=self.candidate(count=1))
        self.assertEqual(generated.status_code, 201, generated.data)
        confirmed = self.client.post(
            f"/api/v1/creative-drafts/{generated.data['id']}/confirm/",
            {
                "expected_revision": generated.data["revision"],
                "review_acknowledged": True,
            },
            format="json",
        )
        self.assertEqual(confirmed.status_code, 200, confirmed.data)

        response = self.client.post(
            f"/api/v1/creative-drafts/{generated.data['id']}/apply/",
            {
                "expected_revision": confirmed.data["revision"],
                "language_mode": "bilingual_confirmed",
                "placement": "append",
                "english_review_acknowledged": True,
                "review_acknowledged": True,
            }, format="json",
        )

        self.assertEqual(response.status_code, 200, response.data)
        self.assertEqual(Shot.objects.count(), 1)
        self.assertGreater(response.data["translations_written"], 0)
        draft = EpisodeCreativeDraft.objects.get(pk=generated.data["id"])
        self.assertEqual(draft.status, EpisodeCreativeDraft.Status.APPLIED)
        self.assertTrue(any(
            item["action"] == "confirm"
            and "canonical_target_collision" in item["issue_codes"]
            for item in draft.runtime_metadata["review_acknowledgements"]
        ))
        self.assertTrue(any(
            item["action"] == "apply"
            and "canonical_target_collision" in item["issue_codes"]
            for item in draft.runtime_metadata["review_acknowledgements"]
        ))

    def test_canonical_storyboard_narrative_selects_rich_action_when_narrative_is_summary(self):
        item = {
            "narrative_en": "Ling Shuang crosses the inn and stops beside the sealed door.",
            "action_en": "Ling Shuang crosses the inn and",
            "narrative_zh": "凌霜穿过客栈，停在封闭的门旁。",
            "action_zh": "凌霜穿过客栈，",
        }

        self.assertEqual(
            _canonical_storyboard_narrative(item, "en"), item["narrative_en"],
        )
        self.assertEqual(
            _canonical_storyboard_narrative(item, "zh"), item["narrative_zh"],
        )
        self.assertEqual(
            _canonical_storyboard_narrative({"narrative_en": "", "action_en": "Fallback."}, "en"),
            "Fallback.",
        )

        rich = {
            "narrative_zh": "两名杀手从左右包抄。",
            "action_zh": "凌霜侧身闪避，右腕翻转，斜撩荡刀，左脚踢膝，后撤半步后剑尖点地。",
        }
        self.assertEqual(
            _canonical_storyboard_narrative(rich, "zh"), rich["action_zh"],
        )

    def test_canonical_storyboard_narrative_merges_only_with_proven_script_order(self):
        item = {"narrative_zh": "凌霜拔剑出鞘。", "action_zh": "凌霜侧身闪避。"}
        script = "凌霜拔剑出鞘。凌霜侧身闪避。"
        self.assertEqual(
            _canonical_storyboard_narrative(item, "zh", source_script=script),
            script,
        )
        self.assertEqual(
            _canonical_storyboard_narrative(item, "zh", source_script=""),
            item["narrative_zh"],
        )

    def test_canonical_storyboard_narrative_preserves_unique_bilateral_context(self):
        item = {
            "narrative_zh": "另两名杀手从左右包抄。",
            "action_zh": "凌霜侧身闪避，右腕翻转，斜撩荡刀，左脚踢膝。",
        }
        script = (
            "凌霜侧身闪避，右腕翻转，斜撩荡刀，左脚踢膝。"
            "另两名杀手从左右包抄。"
        )
        merged = _canonical_storyboard_narrative(item, "zh", source_script=script)
        self.assertLess(merged.index("闪避"), merged.index("左右包抄"))
        self.assertIn("斜撩荡刀", merged)

        legacy = {
            "narrative_zh": "两名杀手从左右包抄。",
            "action_zh": "凌霜拔剑出鞘，侧身闪避，右腕翻转，斜撩荡刀。",
        }
        legacy_script = (
            "凌霜拔剑出鞘，侧身闪避，右腕翻转，斜撩荡刀。"
            "两名杀手从左右包抄。"
        )
        legacy_merged = _canonical_storyboard_narrative(
            legacy, "zh", source_script=legacy_script,
        )
        self.assertTrue(legacy_merged.startswith("凌霜拔剑"))
        self.assertTrue(legacy_merged.endswith("左右包抄。"))

        covered = {
            "narrative_zh": "两名攻击者从左右包抄。",
            "action_zh": "凌霜后撤半步，剑尖点地，逼退左右包抄二人，纵身跃入黑暗。",
        }
        covered_result = _canonical_storyboard_narrative(
            covered, "zh", source_script=legacy_script,
        )
        self.assertEqual(covered_result, covered["action_zh"])
        self.assertEqual(covered_result.count("左右包抄"), 1)

    def test_dense_script_gets_bounded_adaptive_durations_and_coverage_advisory(self):
        script = (
            "凌霜拔剑出鞘，跃入雨幕。杀手扑出挥刀直劈。"
            "凌霜侧身闪避，右腕翻转，斜撩荡刀，左脚踢膝。"
            "凌霜后撤半步，剑尖点地，纵身跃入黑暗，消失。"
        )
        durations = _recommended_shot_durations_ms(
            script, 2, 5_000, minimum_ms=5_000, maximum_ms=15_000,
        )
        self.assertEqual(len(durations), 2)
        self.assertTrue(all(5_000 <= value <= 15_000 for value in durations))
        self.assertGreater(sum(durations), 10_000)
        issue = _script_action_coverage_issue(script, [{
            "narrative_zh": "凌霜拔剑出鞘，跃入雨幕。",
            "action_zh": "杀手扑出挥刀直劈。",
        }])
        self.assertEqual(issue["code"], "script_action_coverage_low")
        self.assertNotIn("/", issue["suggestion_zh"])
        self.assertIsNone(_script_action_coverage_issue(
            script,
            [{
                "narrative_zh": "凌霜拔剑出鞘，跃入雨幕。",
                "action_zh": "杀手扑出挥刀直劈。",
            }],
            minimum_coverage_percent=1,
        ))

    @override_settings(H3_PROMPT_LANGUAGE_GATE="warn")
    def test_bilingual_apply_feeds_confirmed_english_into_h3_compiler(self):
        generated, _ = self.generate(count=1, candidate=self.candidate(count=1))
        confirmed = self.client.post(
            f"/api/v1/creative-drafts/{generated.data['id']}/confirm/",
            {"expected_revision": generated.data["revision"]}, format="json",
        )
        applied = self.client.post(
            f"/api/v1/creative-drafts/{generated.data['id']}/apply/",
            {
                "expected_revision": confirmed.data["revision"],
                "language_mode": "bilingual_confirmed", "placement": "append",
                "english_review_acknowledged": True,
            }, format="json",
        )
        self.assertEqual(applied.status_code, 200, applied.data)
        shot = Shot.objects.get()
        compiled = build_h3_video_prompt(
            self.project, [shot],
            H3PromptOptions(duration_overrides={str(shot.id): 5000}),
        )
        self.assertIn(
            "Ling Shuang stands inside the Rainy Night Inn", compiled.text,
        )
        self.assertNotIn(
            "Ling Shuang presses the right hand against the scabbard", compiled.text,
        )
        self.assertIn(
            "Maintain this visible continuity: Keep the gray narrow-sleeved outfit and shallow right-eyebrow scar unchanged",
            compiled.text,
        )
        self.assertNotIn("凌霜右手按住剑鞘", compiled.text)

    def test_needs_review_candidate_stays_editable_and_confirm_requires_acknowledgement(self):
        candidate = self.candidate(count=1)
        candidate["shots"][0]["narrative_en"] = "Ling Shuang 身在客栈"
        response, _ = self.generate(count=1, candidate=candidate)
        self.assertEqual(response.status_code, 201, response.data)
        self.assertEqual(response.data["status"], EpisodeCreativeDraft.Status.DRAFT)
        self.assertEqual(response.data["validation"]["status"], "needs_review")
        self.assertTrue(response.data["can_edit"])
        self.assertTrue(response.data["can_confirm"])
        self.assertTrue(any(
            issue["code"] == "english_contains_cjk"
            and issue["path"] == "shots[0].narrative_en"
            and issue["severity"] == "risk"
            for issue in response.data["validation"]["review_issues"]
        ))
        self.assertEqual(Shot.objects.count(), 0)
        denied = self.client.post(
            f"/api/v1/creative-drafts/{response.data['id']}/confirm/",
            {"expected_revision": response.data["revision"]}, format="json",
        )
        self.assertEqual(denied.status_code, 400, denied.data)
        confirmed = self.client.post(
            f"/api/v1/creative-drafts/{response.data['id']}/confirm/",
            {
                "expected_revision": response.data["revision"],
                "review_acknowledged": True,
            },
            format="json",
        )
        self.assertEqual(confirmed.status_code, 200, confirmed.data)
        self.assertEqual(confirmed.data["status"], EpisodeCreativeDraft.Status.CONFIRMED)
        acknowledgement = confirmed.data["runtime_metadata"]["review_acknowledgements"][-1]
        self.assertEqual(acknowledgement["action"], "confirm")
        self.assertEqual(acknowledgement["acknowledged_by"], str(self.user.id))
        self.assertIn("english_contains_cjk", acknowledgement["issue_codes"])

    def test_runtime_dialogue_fact_issue_is_dropped_after_persisted_candidate_revalidates(self):
        """Pre-normalization runtime errors must not masquerade as missing bindings."""
        captured = {}

        def invoke(request, config):
            captured.update(request)
            return {
                "ok": True,
                "result": {
                    **deepcopy(self.candidate(count=1)),
                    "source_fingerprint": request["context_hash"],
                },
                "review_issues": [{
                    "code": "dialogue_manifest_mismatch",
                    "severity": "requires_edit",
                    "path": "shots[].dialogue",
                }],
                "runtime": {"model": "Qwen/Qwen3-4B-GGUF"},
            }

        with patch("studio.services.creative_drafts._require_runtime"), patch(
            "studio.services.creative_drafts._run_runtime", side_effect=invoke,
        ):
            response = self.client.post(
                f"/api/v1/projects/{self.project.id}/creative-drafts/generate/",
                {
                    "episode_id": str(self.episode.id),
                    "target_shot_count": 1,
                    "creativity": "balanced",
                },
                format="json",
            )

        self.assertEqual(response.status_code, 201, response.data)
        self.assertEqual(response.data["validation"]["status"], "ready")
        self.assertFalse(any(
            issue["code"] == "dialogue_manifest_mismatch"
            for issue in response.data["validation"]["review_issues"]
        ))

    def test_changed_authored_delivery_is_addressable_advisory_not_missing_reference(self):
        self.episode.script = "凌霜（语气冰冷）：别回头。"
        self.episode.save(update_fields=("script", "updated_at"))
        candidate = self.candidate(count=1)
        candidate["shots"][0]["dialogue"][0]["delivery_zh"] = "cold tone"

        response, _ = self.generate(count=1, candidate=candidate)

        self.assertEqual(response.status_code, 201, response.data)
        issue = next(
            item for item in response.data["validation"]["review_issues"]
            if item["code"] == "dialogue_delivery_differs_from_script"
        )
        self.assertEqual(issue["severity"], "risk")
        self.assertEqual(issue["path"], "shots[0].dialogue[0].delivery_zh")
        self.assertFalse(any(
            item["severity"] == "requires_edit"
            for item in response.data["validation"]["review_issues"]
        ))

    def test_needs_review_apply_requires_acknowledgement_then_applies(self):
        candidate = self.candidate(count=1)
        candidate["shots"][0]["narrative_en"] = "Ling Shuang 身在客栈"
        generated, _ = self.generate(count=1, candidate=candidate)
        self.assertEqual(generated.status_code, 201, generated.data)
        self.assertEqual(generated.data["validation"]["status"], "needs_review")

        confirmed = self.client.post(
            f"/api/v1/creative-drafts/{generated.data['id']}/confirm/",
            {
                "expected_revision": generated.data["revision"],
                "review_acknowledged": True,
            },
            format="json",
        )
        self.assertEqual(confirmed.status_code, 200, confirmed.data)

        denied = self.client.post(
            f"/api/v1/creative-drafts/{generated.data['id']}/apply/",
            {
                "expected_revision": confirmed.data["revision"],
                "language_mode": "chinese_only",
                "placement": "append",
            },
            format="json",
        )
        self.assertEqual(denied.status_code, 400, denied.data)
        self.assertEqual(Shot.objects.count(), 0)

        applied = self.client.post(
            f"/api/v1/creative-drafts/{generated.data['id']}/apply/",
            {
                "expected_revision": confirmed.data["revision"],
                "language_mode": "chinese_only",
                "placement": "append",
                "review_acknowledged": True,
            },
            format="json",
        )
        self.assertEqual(applied.status_code, 200, applied.data)
        self.assertEqual(Shot.objects.count(), 1)
        acknowledgements = applied.data["draft"]["runtime_metadata"]["review_acknowledgements"]
        self.assertEqual([item["action"] for item in acknowledgements[-2:]], ["confirm", "apply"])

    def test_requires_edit_unknown_reference_confirms_but_cannot_apply(self):
        candidate = self.candidate(count=1)
        candidate["shots"][0]["scene_id"] = str(uuid4())
        generated, _ = self.generate(count=1, candidate=candidate)
        self.assertEqual(generated.status_code, 201, generated.data)
        self.assertEqual(generated.data["status"], EpisodeCreativeDraft.Status.DRAFT)
        self.assertEqual(generated.data["validation"]["status"], "needs_review")
        self.assertTrue(any(
            issue["code"] == "unknown_scene_reference"
            and issue["severity"] == "requires_edit"
            and issue["path"] == "shots[0].scene_id"
            for issue in generated.data["validation"]["review_issues"]
        ))

        confirmed = self.client.post(
            f"/api/v1/creative-drafts/{generated.data['id']}/confirm/",
            {
                "expected_revision": generated.data["revision"],
                "review_acknowledged": True,
            },
            format="json",
        )
        self.assertEqual(confirmed.status_code, 200, confirmed.data)
        self.assertFalse(confirmed.data["can_apply"])

        denied = self.client.post(
            f"/api/v1/creative-drafts/{generated.data['id']}/apply/",
            {
                "expected_revision": confirmed.data["revision"],
                "language_mode": "chinese_only",
                "placement": "append",
                "review_acknowledged": True,
            },
            format="json",
        )
        self.assertEqual(denied.status_code, 400, denied.data)
        self.assertEqual(Shot.objects.count(), 0)
        draft = EpisodeCreativeDraft.objects.get(pk=generated.data["id"])
        self.assertEqual(draft.status, EpisodeCreativeDraft.Status.CONFIRMED)

    def test_failed_candidate_can_be_manually_repaired(self):
        invalid = self.candidate(count=1)
        invalid["shots"][0].pop("title_en")
        response, _ = self.generate(count=1, candidate=invalid)
        self.assertEqual(response.status_code, 201, response.data)
        self.assertEqual(response.data["status"], EpisodeCreativeDraft.Status.FAILED)
        self.assertEqual(response.data["validation"]["status"], "failed")
        repaired = deepcopy(response.data["draft_data"])
        repaired["shots"][0]["title_en"] = "Ling Shuang Senses Danger 1"
        patched = self.client.patch(
            f"/api/v1/creative-drafts/{response.data['id']}/",
            {"expected_revision": response.data["revision"], "draft_data": repaired},
            format="json",
        )
        self.assertEqual(patched.status_code, 200, patched.data)
        self.assertEqual(patched.data["status"], EpisodeCreativeDraft.Status.DRAFT)
        self.assertEqual(patched.data["validation"]["status"], "ready")

    def test_review_envelope_preserves_recommendations_and_hard_transport_limits(self):
        """Review risks are editable; only broad transport limits reject a PATCH."""
        generated, _ = self.generate(count=1)
        self.assertEqual(generated.status_code, 201, generated.data)

        edited = deepcopy(generated.data["draft_data"])
        shot = edited["shots"][0]
        shot["narrative_en"] += "x" * (1000 - len(shot["narrative_en"]))
        shot["action_en"] += "x" * (1000 - len(shot["action_en"]))
        shot["continuity_en"] += "x" * (240 - len(shot["continuity_en"]))
        self.assertEqual(len(shot["narrative_en"]), 1000)
        self.assertEqual(len(shot["action_en"]), 1000)
        self.assertEqual(len(shot["continuity_en"]), 240)

        patched = self.client.patch(
            f"/api/v1/creative-drafts/{generated.data['id']}/",
            {"expected_revision": generated.data["revision"], "draft_data": edited},
            format="json",
        )
        self.assertEqual(patched.status_code, 200, patched.data)
        self.assertEqual(patched.data["validation"]["status"], "ready")
        self.assertEqual(len(patched.data["draft_data"]["shots"][0]["narrative_en"]), 1000)
        self.assertEqual(len(patched.data["draft_data"]["shots"][0]["action_en"]), 1000)
        self.assertEqual(len(patched.data["draft_data"]["shots"][0]["continuity_en"]), 240)

        over_recommendation = deepcopy(patched.data["draft_data"])
        over_recommendation["shots"][0]["narrative_en"] += "x"
        reviewed = self.client.patch(
            f"/api/v1/creative-drafts/{generated.data['id']}/",
            {"expected_revision": patched.data["revision"], "draft_data": over_recommendation},
            format="json",
        )
        self.assertEqual(reviewed.status_code, 200, reviewed.data)
        self.assertEqual(reviewed.data["validation"]["status"], "needs_review")
        self.assertTrue(any(
            issue["code"] == "field_length_above_recommendation"
            and issue["path"] == "shots[0].narrative_en"
            for issue in reviewed.data["validation"]["review_issues"]
        ))

        over_narrative_transport = deepcopy(reviewed.data["draft_data"])
        over_narrative_transport["shots"][0]["narrative_en"] = "x" * 6001
        rejected_narrative = self.client.patch(
            f"/api/v1/creative-drafts/{generated.data['id']}/",
            {"expected_revision": reviewed.data["revision"], "draft_data": over_narrative_transport},
            format="json",
        )
        self.assertEqual(rejected_narrative.status_code, 400, rejected_narrative.data)

        over_action_transport = deepcopy(reviewed.data["draft_data"])
        over_action_transport["shots"][0]["action_en"] = "x" * 3001
        rejected_action = self.client.patch(
            f"/api/v1/creative-drafts/{generated.data['id']}/",
            {"expected_revision": reviewed.data["revision"], "draft_data": over_action_transport},
            format="json",
        )
        self.assertEqual(rejected_action.status_code, 400, rejected_action.data)

        over_continuity_transport = deepcopy(reviewed.data["draft_data"])
        over_continuity_transport["shots"][0]["continuity_en"] = "x" * 3001
        rejected_continuity = self.client.patch(
            f"/api/v1/creative-drafts/{generated.data['id']}/",
            {"expected_revision": reviewed.data["revision"], "draft_data": over_continuity_transport},
            format="json",
        )
        self.assertEqual(rejected_continuity.status_code, 400, rejected_continuity.data)

        confirmed = self.client.post(
            f"/api/v1/creative-drafts/{generated.data['id']}/confirm/",
            {
                "expected_revision": reviewed.data["revision"],
                "review_acknowledged": True,
            },
            format="json",
        )
        self.assertEqual(confirmed.status_code, 200, confirmed.data)
        applied = self.client.post(
            f"/api/v1/creative-drafts/{generated.data['id']}/apply/",
            {
                "expected_revision": confirmed.data["revision"],
                "language_mode": "chinese_only", "placement": "append",
                "review_acknowledged": True,
            }, format="json",
        )
        self.assertEqual(applied.status_code, 200, applied.data)

    def test_context_and_revision_conflicts_fail_closed(self):
        generated, _ = self.generate(count=1, candidate=self.candidate(count=1))
        stale_revision = self.client.post(
            f"/api/v1/creative-drafts/{generated.data['id']}/confirm/",
            {"expected_revision": generated.data["revision"] + 1}, format="json",
        )
        self.assertEqual(stale_revision.status_code, 409)
        self.episode.synopsis = "正式梗概已被修改。"
        self.episode.save(update_fields=("synopsis", "updated_at"))
        stale_context = self.client.post(
            f"/api/v1/creative-drafts/{generated.data['id']}/confirm/",
            {"expected_revision": generated.data["revision"]}, format="json",
        )
        self.assertEqual(stale_context.status_code, 409)

    def test_partial_regenerate_preserves_unselected_and_changes_attempt_seed(self):
        generated, first_request = self.generate()
        old_first = generated.data["draft_data"]["shots"][0]
        new_candidate = self.candidate()
        new_candidate["shots"][1]["action_zh"] = "凌霜收剑转身，最终停步。"
        new_candidate["shots"][1]["action_en"] = "Ling Shuang sheathes the sword, turns, and stops."
        captured = {}
        with patch("studio.services.creative_drafts._require_runtime"), patch(
            "studio.services.creative_drafts._run_runtime",
            side_effect=self.runtime_side_effect(captured, new_candidate),
        ):
            regenerated = self.client.post(
                f"/api/v1/creative-drafts/{generated.data['id']}/regenerate/",
                {
                    "expected_revision": generated.data["revision"],
                    "shot_keys": [generated.data["draft_data"]["shots"][1]["client_key"]],
                    "preserve_locked_content": True,
                    "variation_note": "第二镜动作更利落",
                }, format="json",
            )
        self.assertEqual(regenerated.status_code, 201, regenerated.data)
        self.assertEqual(regenerated.data["draft_data"]["shots"][0], old_first)
        self.assertEqual(regenerated.data["draft_data"]["shots"][1]["action_zh"], "凌霜收剑转身，最终停步。")
        self.assertEqual(captured["attempt"], 2)
        self.assertEqual(captured["seed"], first_request["seed"] + 1)
        self.assertEqual(
            captured["regeneration_context"]["selected_shot_keys"],
            [generated.data["draft_data"]["shots"][1]["client_key"]],
        )
        self.assertEqual(
            len(captured["regeneration_context"]["previous_draft"]["shots"]), 2,
        )
        self.assertEqual(
            EpisodeCreativeDraft.objects.get(pk=generated.data["id"]).status,
            EpisodeCreativeDraft.Status.SUPERSEDED,
        )

    def test_partial_regenerate_cannot_change_total_count(self):
        generated, _ = self.generate()
        response = self.client.post(
            f"/api/v1/creative-drafts/{generated.data['id']}/regenerate/",
            {
                "expected_revision": generated.data["revision"],
                "shot_keys": [generated.data["draft_data"]["shots"][0]["client_key"]],
                "target_shot_count": 1, "preserve_locked_content": True,
            }, format="json",
        )
        self.assertEqual(response.status_code, 400)

    def test_regeneration_runtime_failure_is_persisted_without_superseding_parent(self):
        generated, _ = self.generate(count=1, candidate=self.candidate(count=1))
        with patch("studio.services.creative_drafts._require_runtime"), patch(
            "studio.services.creative_drafts._run_runtime",
            side_effect=CreativeDraftError(
                "critic repair failed",
                code="creative_validation_failed",
                runtime_metadata={"phase": "repair_validation", "elapsed_ms": 1234},
            ),
        ):
            response = self.client.post(
                f"/api/v1/creative-drafts/{generated.data['id']}/regenerate/",
                {
                    "expected_revision": generated.data["revision"],
                    "variation_note": "换一种动作",
                }, format="json",
            )
        self.assertEqual(response.status_code, 502, response.data)
        self.assertEqual(response.data["draft"]["status"], EpisodeCreativeDraft.Status.FAILED)
        self.assertEqual(response.data["code"], "creative_validation_failed")
        self.assertEqual(response.data["draft"]["runtime_metadata"]["phase"], "repair_validation")
        self.assertEqual(response.data["draft"]["runtime_metadata"]["elapsed_ms"], 1234)
        self.assertEqual(str(response.data["draft"]["parent_draft"]), generated.data["id"])
        parent = EpisodeCreativeDraft.objects.get(pk=generated.data["id"])
        self.assertEqual(parent.status, EpisodeCreativeDraft.Status.DRAFT)

    def test_invalid_json_failure_uses_safe_code_and_public_audit_only(self):
        safe_message = "本地 Qwen 返回的结构化草稿无法安全解析，已拒绝采用；请重试。"
        with patch("studio.services.creative_drafts._require_runtime"), patch(
            "studio.services.creative_drafts._run_runtime",
            side_effect=CreativeDraftError(
                safe_message,
                code="creative_output_invalid_json",
                runtime_metadata={
                    "phase": "primary_decode",
                    "json_decode_error": {
                        "phase": "primary_decode", "category": "invalid_json",
                    },
                    "primary_json_control_recovery": {},
                },
            ),
        ):
            response = self.client.post(
                f"/api/v1/projects/{self.project.id}/creative-drafts/generate/",
                {
                    "episode_id": str(self.episode.id),
                    "target_shot_count": 1,
                    "creativity": "balanced",
                },
                format="json",
            )

        self.assertEqual(response.status_code, 502, response.data)
        self.assertEqual(response.data["code"], "creative_output_invalid_json")
        self.assertEqual(response.data["detail"], safe_message)
        self.assertNotIn("Invalid control character", response.data["detail"])
        self.assertNotIn("line 1", response.data["detail"])
        failed = EpisodeCreativeDraft.objects.get(pk=response.data["draft"]["id"])
        self.assertEqual(failed.error_message, safe_message)
        self.assertEqual(
            response.data["draft"]["runtime_metadata"]["json_decode_error"],
            {"phase": "primary_decode", "category": "invalid_json"},
        )

    def test_replace_partial_apply_is_forbidden(self):
        generated, _ = self.generate()
        confirmed = self.client.post(
            f"/api/v1/creative-drafts/{generated.data['id']}/confirm/",
            {"expected_revision": generated.data["revision"]}, format="json",
        )
        response = self.client.post(
            f"/api/v1/creative-drafts/{generated.data['id']}/apply/",
            {
                "expected_revision": confirmed.data["revision"],
                "language_mode": "chinese_only", "placement": "replace",
                "shot_keys": [generated.data["draft_data"]["shots"][0]["client_key"]],
            }, format="json",
        )
        self.assertEqual(response.status_code, 400)
        self.assertEqual(Shot.objects.count(), 0)

    def test_image_conditioned_mode_without_bindings_downgrades_to_t2va(self):
        candidate = self.candidate(count=1, h3_mode=Shot.H3Mode.I2VA)
        generated, _ = self.generate(count=1, candidate=candidate)
        confirmed = self.client.post(
            f"/api/v1/creative-drafts/{generated.data['id']}/confirm/",
            {"expected_revision": generated.data["revision"]}, format="json",
        )
        applied = self.client.post(
            f"/api/v1/creative-drafts/{generated.data['id']}/apply/",
            {
                "expected_revision": confirmed.data["revision"],
                "language_mode": "chinese_only", "placement": "append",
            }, format="json",
        )
        self.assertEqual(applied.status_code, 200, applied.data)
        shot = Shot.objects.get()
        self.assertEqual(shot.h3_mode, Shot.H3Mode.T2VA)
        self.assertEqual(shot.metadata["requested_h3_mode"], Shot.H3Mode.I2VA)

    def test_tenant_isolation_and_project_episode_binding(self):
        generated, _ = self.generate(count=1, candidate=self.candidate(count=1))
        self.client.force_authenticate(self.other)
        self.assertEqual(
            self.client.get(f"/api/v1/creative-drafts/{generated.data['id']}/").status_code,
            404,
        )
        self.assertEqual(
            self.client.get(f"/api/v1/projects/{self.project.id}/creative-drafts/").status_code,
            404,
        )

    def test_capability_and_single_shot_contract(self):
        response = self.client.get("/api/v1/capabilities/creative-drafts/")
        self.assertEqual(response.status_code, 200)
        self.assertTrue(response.data["creative_generation_supported"])
        self.assertTrue(response.data["review_required"])
        self.assertEqual(
            response.data["series_concept_draft_preferred_provider"],
            "deepseek_api",
        )
        self.assertTrue(response.data["series_concept_draft_supported"])
        self.assertEqual(response.data["series_concept_draft_schema_version"], 2)
        self.assertEqual(response.data["series_concept_draft_limits"], {
            "max_idea_chars": 6000,
            "max_style_hint_chars": 2000,
            "default_episode_duration_seconds": 60,
            "minimum_shot_duration_ms": 5000,
            "maximum_shot_duration_ms": 15000,
        })
        self.assertTrue(response.data["series_concept_draft_review_required"])
        self.assertFalse(response.data["series_concept_draft_persisted"])
        self.assertNotIn("min_shots", response.data)
        providers = {item["id"]: item for item in response.data["providers"]}

        local = providers["local_qwen"]
        self.assertEqual(
            local["series_concept_draft_suitability"], "experimental",
        )
        self.assertEqual(
            local["series_concept_draft_suitability_reason_code"],
            "series_concept_strict_structure_experimental",
        )
        self.assertEqual(local["series_concept_draft_timeout_seconds"], 900)
        self.assertEqual(local["series_concept_draft_max_output_tokens"], 3072)
        self.assertIn("基础/测试级", local["series_concept_draft_suitability_notice"])
        self.assertEqual(local["min_shots"], 1)
        self.assertEqual(local["recommended_max_shots"], 4)
        self.assertEqual(local["batch_shots"], 4)
        self.assertEqual(local["max_episode_title_chars"], 80)
        self.assertEqual(local["max_dialogue_text_chars"], 240)
        self.assertEqual(local["min_shot_duration_ms"], 5000)
        self.assertEqual(local["max_shot_duration_ms"], 15000)
        self.assertEqual(local["output_tokens_per_shot"], 384)
        self.assertEqual(local["output_tokens_per_dialogue"], 128)
        self.assertEqual(local["output_dialogue_char_threshold"], 32)
        self.assertEqual(local["output_headroom_per_shot"], 0)
        self.assertEqual(local["baseline_max_new_tokens"], 2304)
        self.assertEqual(local["max_new_tokens"], 3072)
        self.assertEqual(local["runtime_timeout_seconds"], 900)
        self.assertEqual(local["process_timeout_seconds"], 960)
        self.assertEqual(local["min_context_size"], 8192)
        self.assertEqual(local["max_context_size"], 32768)
        self.assertEqual(local["reserved_input_tokens"], 2048)
        self.assertEqual(local["recommended_narrative_en_chars"], 1000)
        self.assertEqual(local["recommended_action_en_chars"], 1000)
        self.assertEqual(local["max_camera_movements"], 1)
        self.assertEqual(local["max_action_sequence_markers"], 4)
        self.assertEqual(local["min_script_action_coverage_percent"], 80)

        deepseek = providers["deepseek_api"]
        self.assertEqual(
            deepseek["series_concept_draft_suitability"], "recommended",
        )
        self.assertEqual(
            deepseek["series_concept_draft_suitability_reason_code"],
            "series_concept_strict_structure_recommended",
        )
        self.assertEqual(deepseek["series_concept_draft_timeout_seconds"], 900)
        self.assertEqual(
            deepseek["series_concept_draft_max_output_tokens"], 384000,
        )
        self.assertIn("仍需人工确认", deepseek["series_concept_draft_suitability_notice"])
        self.assertEqual(deepseek["min_shots"], 1)
        self.assertEqual(deepseek["recommended_max_shots"], 4)
        self.assertEqual(deepseek["batch_shots"], 16)
        self.assertEqual(deepseek["max_dialogue_per_shot"], 2)
        self.assertEqual(deepseek["max_output_tokens"], 16384)

    @override_settings(
        SERIES_CONCEPT_DRAFT_PREFERRED_PROVIDER="local_qwen",
        SERIES_CONCEPT_DRAFT_PROVIDER_SUITABILITY={
            "local_qwen": "recommended",
            "deepseek_api": "experimental",
        },
    )
    def test_series_concept_provider_guidance_is_configuration_driven(self):
        response = self.client.get("/api/v1/capabilities/creative-drafts/")

        self.assertEqual(response.status_code, 200, response.data)
        self.assertEqual(
            response.data["series_concept_draft_preferred_provider"],
            "local_qwen",
        )
        providers = {item["id"]: item for item in response.data["providers"]}
        self.assertEqual(
            providers["local_qwen"]["series_concept_draft_suitability"],
            "recommended",
        )
        self.assertEqual(
            providers["deepseek_api"]["series_concept_draft_suitability"],
            "experimental",
        )

    def test_capability_maps_custom_deepseek_policy_without_root_level_duplicates(self):
        policy = {
            "min_shots": 2,
            "recommended_max_shots": 6,
            "batch_shots": 12,
            "max_dialogue_per_shot": 3,
            "max_episode_title_chars": 96,
            "max_dialogue_text_chars": 300,
            "max_dialogue_delivery_chars": 120,
            "max_dialogue_delivery_en_chars": 180,
            "min_shot_duration_ms": 6000,
            "max_shot_duration_ms": 14000,
            "output_min_tokens": 1200,
            "output_base_tokens": 900,
            "output_tokens_per_shot": 420,
            "output_tokens_per_dialogue": 140,
            "output_dialogue_char_threshold": 40,
            "max_source_chars": 26000,
            "max_regeneration_context_chars": 88000,
            "max_input_chars": 130000,
            "output_headroom_per_shot": 512,
        }
        with override_settings(DEEPSEEK_CREATIVE=policy):
            response = self.client.get("/api/v1/capabilities/creative-drafts/")

        self.assertEqual(response.status_code, 200, response.data)
        self.assertNotIn("recommended_max_shots", response.data)
        deepseek = next(
            item for item in response.data["providers"] if item["id"] == "deepseek_api"
        )
        for key, value in policy.items():
            self.assertEqual(deepseek[key], value)

    def test_deepseek_policy_fallback_uses_one_shot_per_model_call(self):
        with override_settings(DEEPSEEK_CREATIVE={}):
            config = creative_provider_config("deepseek_api")

        self.assertEqual(config["batch_shots"], 1)

    def test_creative_provider_policy_rejects_invalid_environment_relationships(self):
        from autoprompt_backend.settings import (
            CREATIVE_DRAFT_REVIEW,
            _validate_creative_provider_policy,
            _validate_creative_review_policy,
            _validate_series_concept_draft_suitability,
        )

        valid = {
            "min_shots": 1,
            "recommended_max_shots": 4,
            "batch_shots": 16,
            "max_dialogue_per_shot": 2,
            "max_episode_title_chars": 80,
            "max_dialogue_text_chars": 240,
            "max_dialogue_delivery_chars": 100,
            "max_dialogue_delivery_en_chars": 150,
            "min_shot_duration_ms": 5000,
            "max_shot_duration_ms": 15000,
            "output_min_tokens": 1024,
            "output_base_tokens": 768,
            "output_tokens_per_shot": 384,
            "output_tokens_per_dialogue": 128,
            "output_dialogue_char_threshold": 32,
            "max_source_chars": 24000,
            "max_regeneration_context_chars": 96000,
            "max_input_chars": 120000,
            "output_headroom_per_shot": 384,
        }
        _validate_creative_provider_policy(
            {**valid, "recommended_max_shots": 11, "batch_shots": 4},
            "DEEPSEEK_CREATIVE",
        )
        invalid_cases = (
            ({**valid, "min_shots": 2, "recommended_max_shots": 1}, "MIN_SHOTS"),
            ({**valid, "min_shots": 2, "batch_shots": 1}, "MIN_SHOTS"),
            ({**valid, "max_source_chars": 120001}, "MAX_SOURCE_CHARS"),
            ({**valid, "max_regeneration_context_chars": 120001}, "MAX_REGENERATION"),
            ({**valid, "max_dialogue_text_chars": 0}, "MAX_DIALOGUE_TEXT_CHARS"),
            ({**valid, "min_shot_duration_ms": 15001}, "MIN_SHOT_DURATION_MS"),
            ({**valid, "max_shot_duration_ms": 18000}, "MAX_SHOT_DURATION_MS"),
            ({**valid, "output_dialogue_char_threshold": -1}, "OUTPUT_DIALOGUE_CHAR_THRESHOLD"),
            ({**valid, "output_headroom_per_shot": -1}, "OUTPUT_HEADROOM"),
        )
        for policy, message in invalid_cases:
            with self.subTest(message=message), self.assertRaisesRegex(
                ImproperlyConfigured, message,
            ):
                _validate_creative_provider_policy(policy, "DEEPSEEK_CREATIVE")

        with self.assertRaisesRegex(ImproperlyConfigured, "NARRATIVE_EN"):
            _validate_creative_review_policy({
                **CREATIVE_DRAFT_REVIEW,
                "recommended_narrative_en_chars": 0,
            })
        with self.assertRaisesRegex(ImproperlyConfigured, "between 1 and 100"):
            _validate_creative_review_policy({
                **CREATIVE_DRAFT_REVIEW,
                "min_script_action_coverage_percent": 101,
            })
        with self.assertRaisesRegex(ImproperlyConfigured, "experimental, recommended"):
            _validate_series_concept_draft_suitability(
                "auto", "QWEN_SERIES_CONCEPT_DRAFT_SUITABILITY",
            )

    def test_review_recommendations_are_configurable_advisories(self):
        from django.conf import settings

        candidate = self.candidate(count=1)
        candidate["shots"][0]["camera_en"] = "Push in, then pan right."
        candidate["shots"][0]["action_en"] = "Step forward, then turn, then stop."
        review_policy = {
            **settings.CREATIVE_DRAFT_REVIEW,
            "recommended_narrative_en_chars": 10,
            "max_camera_movements": 1,
            "max_action_sequence_markers": 1,
        }
        _data, validation = validate_creative_draft_data(
            self.project,
            self.episode,
            candidate,
            target_shot_count=1,
            review_policy=review_policy,
        )
        issue_codes = {
            (item["code"], item["path"])
            for item in validation["review_issues"]
        }
        self.assertIn(
            ("field_length_above_recommendation", "shots[0].narrative_en"),
            issue_codes,
        )
        self.assertIn(("camera_movement_dense", "shots[0].camera_en"), issue_codes)
        self.assertIn(("action_arc_dense", "shots[0].action_en"), issue_codes)
        self.assertEqual(validation["status"], "needs_review")

    def test_busy_guard_queries_active_generation_before_runtime(self):
        mocked = type("Exists", (), {"exists": lambda self: True})()
        with patch("studio.services.creative_drafts.GenerationTask.objects.filter", return_value=mocked):
            with self.assertRaises(CreativeResourceBusy):
                ensure_creative_resources_available()

    def test_source_batch_recommendation_requires_confirmation_then_batches(self):
        source_lines = [
            f"凌霜穿过第{index}段长廊" + ("雨" * 1000) + "。"
            for index in range(1, 5)
        ]
        self.episode.script = "\n".join(source_lines)
        self.episode.save(update_fields=("script", "updated_at"))
        tiny = {**creative_config(), "max_source_chars": 2500}
        url = f"/api/v1/projects/{self.project.id}/creative-drafts/generate/"
        with patch(
            "studio.services.creative_drafts.creative_provider_config",
            return_value=tiny,
        ), patch(
            "studio.services.creative_drafts._run_provider_runtime",
        ) as runtime:
            unconfirmed = self.client.post(
                url,
                {
                    "episode_id": str(self.episode.id), "target_shot_count": 4,
                    "creativity": "balanced",
                }, format="json",
            )
        self.assertEqual(unconfirmed.status_code, 400, unconfirmed.data)
        details = unconfirmed.data["error"]["details"]
        self.assertEqual(details["code"], "creative_generation_confirmation_required")
        self.assertEqual(details["source_char_count"], len(self.episode.script))
        self.assertEqual(details["max_source_chars"], 2500)
        runtime.assert_not_called()

        captured = []

        def complete_batch(request, _config, _provider):
            captured.append(deepcopy(request))
            candidate = self.candidate(count=request["options"]["shot_count"])
            for shot in candidate["shots"]:
                shot["dialogue"] = []
            manifest = request["required_dialogue_manifest"]
            if manifest:
                source = manifest[0]
                candidate["shots"][0]["dialogue"] = [{
                    "speaker_id": source["speaker_id"],
                    "text_zh": source["text_zh"],
                    "delivery_zh": source.get("source_delivery_zh", ""),
                    "delivery_en": "",
                    "voiceover": bool(source.get("voiceover", False)),
                }]
            return {
                "ok": True,
                "result": {
                    **candidate,
                    "source_fingerprint": request["context_hash"],
                },
                "runtime": {"model": "Qwen/Qwen3-4B-GGUF"},
            }

        with patch(
            "studio.services.creative_drafts.creative_provider_config",
            return_value=tiny,
        ), patch(
            "studio.services.creative_drafts._run_provider_runtime",
            side_effect=complete_batch,
        ) as runtime:
            confirmed = self.client.post(
                url,
                {
                    "episode_id": str(self.episode.id),
                    "target_shot_count": 4,
                    "creativity": "balanced",
                    "extended_generation_confirmed": True,
                },
                format="json",
            )

        self.assertEqual(confirmed.status_code, 201, confirmed.data)
        self.assertEqual(runtime.call_count, 4)
        self.assertEqual(
            [request["batch_context"]["source_mode"] for request in captured],
            ["contiguous_excerpt"] * 4,
        )
        self.assertEqual(
            [request["episode"]["script"] for request in captured],
            self.episode.script.splitlines(),
        )
        self.assertEqual(len(confirmed.data["draft_data"]["shots"]), 4)

        with patch(
            "studio.services.creative_drafts.creative_provider_config",
            return_value=tiny,
        ), patch(
            "studio.services.creative_drafts._run_provider_runtime",
        ) as runtime:
            selected = self.client.post(
                f"/api/v1/creative-drafts/{confirmed.data['id']}/regenerate/",
                {
                    "expected_revision": confirmed.data["revision"],
                    "target_shot_count": 4,
                    "shot_keys": [
                        confirmed.data["draft_data"]["shots"][0]["client_key"]
                    ],
                    "preserve_locked_content": True,
                },
                format="json",
            )

        self.assertEqual(selected.status_code, 400, selected.data)
        selected_details = selected.data["error"]["details"]
        self.assertNotEqual(
            selected_details["code"],
            "creative_generation_confirmation_required",
        )
        self.assertNotIn("source_char_count", selected_details)
        runtime.assert_not_called()

    def test_runtime_request_keeps_hash_and_attempt_authoritative(self):
        snapshot = creative_context_snapshot(self.project, self.episode)
        fingerprint = creative_context_hash(self.project, self.episode)
        request = _runtime_request(
            project=self.project, episode=self.episode, snapshot=snapshot,
            context_hash=fingerprint, target_shot_count=1,
            creativity="conservative", variation_note="避免重复旧构图", attempt=3,
        )
        self.assertEqual(request["source_fingerprint"], fingerprint)
        self.assertEqual(request["attempt"], 3)
        self.assertEqual(request["feedback"], "避免重复旧构图")
        self.assertEqual(request["seed"], self.project.continuity_seed + 3)
        self.assertEqual(len(request["options"]["recommended_shot_durations_ms"]), 1)
        self.assertTrue(
            5_000 <= request["options"]["recommended_shot_durations_ms"][0] <= 15_000
        )

    def test_authored_dialogue_drives_exact_output_budget_and_capacity(self):
        def request_for(line_count, shot_count=4, *, include_audio=True):
            lines = ["凌霜在雨夜客栈拔剑。"] + [
                f"凌霜：第{i}句。" for i in range(1, line_count + 1)
            ]
            self.episode.script = "\n".join(lines)
            self.episode.prompt_settings = {"include_audio": include_audio}
            self.episode.save(update_fields=("script", "prompt_settings", "updated_at"))
            snapshot = creative_context_snapshot(self.project, self.episode)
            return _runtime_request(
                project=self.project, episode=self.episode, snapshot=snapshot,
                context_hash=creative_context_hash(self.project, self.episode),
                target_shot_count=shot_count, creativity="balanced",
                variation_note="", attempt=1,
            )

        expected = {0: 2304, 2: 2560, 6: 3072}
        for count, tokens in expected.items():
            with self.subTest(dialogue_count=count):
                request = request_for(count)
                self.assertEqual(request["max_new_tokens"], tokens)
                self.assertEqual(len(request["required_dialogue_manifest"]), count)

        self.episode.script = "凌霜：别回头。\n凌霜：别回头。"
        self.episode.prompt_settings = {"include_audio": True}
        self.episode.save(update_fields=("script", "prompt_settings", "updated_at"))
        repeated_snapshot = creative_context_snapshot(self.project, self.episode)
        repeated = _runtime_request(
            project=self.project, episode=self.episode, snapshot=repeated_snapshot,
            context_hash=creative_context_hash(self.project, self.episode),
            target_shot_count=4, creativity="balanced", variation_note="", attempt=1,
        )
        self.assertEqual(len(repeated["required_dialogue_manifest"]), 2)
        self.assertEqual(repeated["max_new_tokens"], 2560)

        for dialogue_count in (7, 8):
            with self.subTest(dialogue_count=dialogue_count), self.assertRaisesRegex(
                CreativeValidationError, "3072",
            ):
                request_for(dialogue_count)

        with self.assertRaisesRegex(CreativeValidationError, "只能容纳 2 句"):
            request_for(3, shot_count=1)
        with self.assertRaisesRegex(CreativeValidationError, "只能容纳 8 句") as caught:
            request_for(9, shot_count=4)
        self.assertEqual(caught.exception.code, "creative_dialogue_capacity_exceeded")
        self.assertEqual(caught.exception.public_context, {
            "required_dialogue_count": 9,
            "target_shot_count": 4,
            "dialogue_capacity": 8,
            "max_dialogue_per_shot": 2,
        })

        audio_disabled = request_for(9, shot_count=1, include_audio=False)
        self.assertEqual(audio_disabled["required_dialogue_manifest"], [])
        self.assertEqual(audio_disabled["max_new_tokens"], 1152)

    def test_failed_attempt_persists_start_snapshot_when_source_drifts(self):
        original_script = self.project.master_script

        def fail_after_drift(_request, _config):
            SeriesProject.objects.filter(pk=self.project.pk).update(
                master_script="并发修改后的母稿",
            )
            raise CreativeDraftError(
                "candidate validation failed",
                code="creative_validation_failed",
                runtime_metadata={"phase": "primary_validation", "elapsed_ms": 5000},
            )

        with patch("studio.services.creative_drafts._require_runtime"), patch(
            "studio.services.creative_drafts._run_runtime", side_effect=fail_after_drift,
        ):
            response = self.client.post(
                f"/api/v1/projects/{self.project.id}/creative-drafts/generate/",
                {
                    "episode_id": str(self.episode.id),
                    "target_shot_count": 1,
                    "creativity": "balanced",
                },
                format="json",
            )

        self.assertEqual(response.status_code, 502, response.data)
        failed = EpisodeCreativeDraft.objects.get(pk=response.data["draft"]["id"])
        self.assertEqual(failed.context_snapshot["project"]["master_script"], original_script)
        self.assertFalse(response.data["draft"]["context_current"])
        self.assertTrue(failed.runtime_metadata["source_drift_during_attempt"])
        self.assertEqual(failed.runtime_metadata["attempt_context_hash"], failed.context_hash)
        self.assertEqual(failed.input_settings["min_shots"], 1)
        self.assertEqual(failed.input_settings["max_source_chars"], 16000)
        self.assertEqual(failed.input_settings["max_input_chars"], 24000)
        self.assertEqual(failed.input_settings["max_output_tokens"], 3072)
        self.assertEqual(failed.input_settings["context_size"], 8192)
        self.assertEqual(failed.input_settings["timeout_seconds"], 900)

    def test_deepseek_eleven_shots_requires_confirmation_then_preserves_count_on_regenerate(self):
        dialogue_texts = [f"第{index}句。" for index in range(1, 14)]
        self.episode.script = "\n".join(
            ["凌霜在雨夜客栈拔剑。"]
            + [f"凌霜：{text}" for text in dialogue_texts]
        )
        self.episode.save(update_fields=("script", "updated_at"))
        candidate = self.candidate(count=11)
        for shot in candidate["shots"]:
            shot["dialogue"] = []
        for index, text in enumerate(dialogue_texts):
            candidate["shots"][index // 2]["dialogue"].append({
                "speaker_id": "S1",
                "text_zh": text,
                "delivery_zh": "",
                "delivery_en": "",
                "voiceover": False,
            })

        generate_url = f"/api/v1/projects/{self.project.id}/creative-drafts/generate/"
        base_request = {
            "episode_id": str(self.episode.id),
            "provider": "deepseek_api",
            "target_shot_count": 11,
            "creativity": "balanced",
        }
        with patch("studio.services.creative_drafts._run_provider_runtime") as runtime:
            unconfirmed = self.client.post(generate_url, base_request, format="json")

        self.assertEqual(unconfirmed.status_code, 400, unconfirmed.data)
        details = unconfirmed.data["error"]["details"]
        self.assertEqual(details["code"], "creative_generation_confirmation_required")
        self.assertEqual(details["target_shot_count"], 11)
        self.assertEqual(details["recommended_max_shots"], 4)
        self.assertEqual(details["batch_shots"], 16)
        runtime.assert_not_called()
        self.assertFalse(EpisodeCreativeDraft.objects.exists())

        captured = {}

        def complete(request, _config, provider):
            captured.update(deepcopy(request))
            self.assertEqual(provider, "deepseek_api")
            return {
                "ok": True,
                "result": {
                    **deepcopy(candidate),
                    "source_fingerprint": request["context_hash"],
                },
                "runtime": {"model": "deepseek-v4-flash", "phase": "complete"},
            }

        with patch(
            "studio.services.creative_drafts._run_provider_runtime",
            side_effect=complete,
        ):
            generated = self.client.post(
                generate_url,
                {**base_request, "extended_generation_confirmed": True},
                format="json",
            )

        self.assertEqual(generated.status_code, 201, generated.data)
        self.assertEqual(len(generated.data["draft_data"]["shots"]), 11)
        self.assertEqual(captured["options"]["shot_count"], 11)
        self.assertEqual(captured["options"]["batch_shots"], 16)
        self.assertEqual(captured["max_new_tokens"], 6656)
        self.assertEqual(captured["provider_output_token_budget"], 10880)
        self.assertEqual(len(captured["required_dialogue_manifest"]), 13)
        self.assertEqual(generated.data["input_settings"]["target_shot_count"], 11)
        self.assertTrue(generated.data["input_settings"]["extended_generation_confirmed"])
        self.assertEqual(generated.data["input_settings"]["recommended_max_shots"], 4)
        self.assertEqual(generated.data["input_settings"]["batch_shots"], 16)
        self.assertEqual(generated.data["input_settings"]["max_dialogue_per_shot"], 2)
        self.assertEqual(generated.data["input_settings"]["min_shots"], 1)
        self.assertEqual(generated.data["input_settings"]["max_source_chars"], 24000)
        self.assertEqual(generated.data["input_settings"]["max_input_chars"], 120000)
        self.assertEqual(generated.data["input_settings"]["max_output_tokens"], 16384)
        self.assertFalse(Shot.objects.exists())

        regenerated_capture = {}

        def regenerate(request, _config, provider):
            regenerated_capture.update(deepcopy(request))
            self.assertEqual(provider, "deepseek_api")
            return {
                "ok": True,
                "result": {
                    **deepcopy(candidate),
                    "source_fingerprint": request["context_hash"],
                },
                "runtime": {"model": "deepseek-v4-flash", "phase": "complete"},
            }

        with patch(
            "studio.services.creative_drafts._run_provider_runtime",
            side_effect=regenerate,
        ):
            regenerated = self.client.post(
                f"/api/v1/creative-drafts/{generated.data['id']}/regenerate/",
                {
                    "expected_revision": generated.data["revision"],
                    "provider": "deepseek_api",
                    "target_shot_count": 11,
                    "extended_generation_confirmed": True,
                    "shot_keys": [generated.data["draft_data"]["shots"][0]["client_key"]],
                    "preserve_locked_content": True,
                },
                format="json",
            )

        self.assertEqual(regenerated.status_code, 201, regenerated.data)
        self.assertEqual(len(regenerated.data["draft_data"]["shots"]), 11)
        self.assertEqual(regenerated_capture["options"]["shot_count"], 11)
        self.assertEqual(
            len(regenerated_capture["regeneration_context"]["previous_draft"]["shots"]),
            11,
        )
        self.assertTrue(regenerated.data["input_settings"]["extended_generation_confirmed"])

    def test_initial_generate_batches_contiguous_source_and_carries_global_boundary(self):
        source_lines = [
            "片段一动作。",
            "片段二动作。",
            "片段三动作。",
            "片段四动作。",
        ]
        self.episode.script = "\n".join(source_lines)
        self.episode.save(update_fields=("script", "updated_at"))
        config = {
            **creative_config(),
            "recommended_max_shots": 4,
            "batch_shots": 2,
            "max_new_tokens": 2000,
            "max_output_tokens": 2000,
        }
        captured_requests = []

        def complete_batch(request, _config, provider):
            self.assertEqual(provider, "local_qwen")
            captured_requests.append(deepcopy(request))
            candidate = self.candidate(count=2)
            for shot in candidate["shots"]:
                shot["dialogue"] = []
            return {
                "ok": True,
                "result": {
                    **candidate,
                    "source_fingerprint": request["context_hash"],
                },
                "runtime": {
                    "model": "Qwen/Qwen3-4B-GGUF",
                    "elapsed_ms": 100,
                },
            }

        with patch(
            "studio.services.creative_drafts.creative_provider_config",
            return_value=config,
        ), patch(
            "studio.services.creative_drafts._run_provider_runtime",
            side_effect=complete_batch,
        ) as runtime:
            response = self.client.post(
                f"/api/v1/projects/{self.project.id}/creative-drafts/generate/",
                {
                    "episode_id": str(self.episode.id),
                    "target_shot_count": 4,
                    "creativity": "balanced",
                },
                format="json",
            )

        self.assertEqual(response.status_code, 201, response.data)
        self.assertEqual(runtime.call_count, 2)
        self.assertEqual(
            [request["episode"]["script"] for request in captured_requests],
            ["\n".join(source_lines[:2]), "\n".join(source_lines[2:])],
        )
        self.assertEqual(
            [request["batch_context"]["global_shot_start"] for request in captured_requests],
            [1, 3],
        )
        self.assertIsNone(captured_requests[0]["batch_context"]["previous_shot"])
        previous = captured_requests[1]["batch_context"]["previous_shot"]
        self.assertEqual(previous["client_key"], "shot-02")
        self.assertEqual(previous["order"], 2)
        self.assertEqual(previous["scene_id"], str(self.scene.id))
        self.assertEqual(
            [shot["client_key"] for shot in response.data["draft_data"]["shots"]],
            ["shot-01", "shot-02", "shot-03", "shot-04"],
        )
        self.assertEqual(
            [shot["order"] for shot in response.data["draft_data"]["shots"]],
            [1, 2, 3, 4],
        )
        self.assertEqual(response.data["runtime_metadata"]["batch_count"], 2)
        self.assertEqual(response.data["runtime_metadata"]["batch_shot_counts"], [2, 2])
        self.assertEqual(
            [request["batch_context"]["source_mode"] for request in captured_requests],
            ["contiguous_excerpt", "contiguous_excerpt"],
        )

    def test_batch_count_and_order_variance_is_preserved_for_review(self):
        self.episode.script = "\n".join(f"片段{index}动作。" for index in range(1, 5))
        self.episode.save(update_fields=("script", "updated_at"))
        config = {
            **creative_config(),
            "recommended_max_shots": 4,
            "batch_shots": 2,
            "max_new_tokens": 2000,
            "max_output_tokens": 2000,
        }
        call_count = 0

        def complete_batch(request, _config, _provider):
            nonlocal call_count
            call_count += 1
            returned_count = request["options"]["shot_count"] if call_count == 1 else 1
            candidate = self.candidate(count=returned_count)
            for shot in candidate["shots"]:
                shot["dialogue"] = []
            if call_count == 2:
                candidate["shots"][0]["order"] = 99
            return {
                "ok": True,
                "result": {
                    **candidate,
                    "source_fingerprint": request["context_hash"],
                },
                "runtime": {"model": "Qwen/Qwen3-4B-GGUF"},
            }

        with patch(
            "studio.services.creative_drafts.creative_provider_config",
            return_value=config,
        ), patch(
            "studio.services.creative_drafts._run_provider_runtime",
            side_effect=complete_batch,
        ):
            response = self.client.post(
                f"/api/v1/projects/{self.project.id}/creative-drafts/generate/",
                {
                    "episode_id": str(self.episode.id),
                    "target_shot_count": 4,
                    "creativity": "balanced",
                },
                format="json",
            )

        self.assertEqual(response.status_code, 201, response.data)
        self.assertEqual(len(response.data["draft_data"]["shots"]), 3)
        self.assertEqual(
            [shot["order"] for shot in response.data["draft_data"]["shots"]],
            [1, 2, 3],
        )
        codes = {issue["code"] for issue in response.data["validation"]["review_issues"]}
        self.assertTrue({
            "batch_shot_count_mismatch",
            "batch_shot_order_repaired",
            "shot_count_mismatch",
        } <= codes)
        self.assertEqual(response.data["status"], EpisodeCreativeDraft.Status.DRAFT)

    def test_later_batch_failure_preserves_completed_model_data(self):
        self.episode.script = "\n".join(f"片段{index}动作。" for index in range(1, 5))
        self.episode.save(update_fields=("script", "updated_at"))
        config = {
            **creative_config(),
            "recommended_max_shots": 4,
            "batch_shots": 2,
            "max_new_tokens": 2000,
            "max_output_tokens": 2000,
        }
        call_count = 0

        def complete_then_fail(request, _config, _provider):
            nonlocal call_count
            call_count += 1
            if call_count == 2:
                raise CreativeDraftError(
                    "remote request failed",
                    code="deepseek_api_network_error",
                    runtime_metadata={"transport": "remote"},
                )
            candidate = self.candidate(count=request["options"]["shot_count"])
            for shot in candidate["shots"]:
                shot["dialogue"] = []
            return {
                "ok": True,
                "result": {
                    **candidate,
                    "source_fingerprint": request["context_hash"],
                },
                "runtime": {"model": "deepseek-v4-flash", "transport": "remote"},
            }

        with patch(
            "studio.services.creative_drafts.creative_provider_config",
            return_value=config,
        ), patch(
            "studio.services.creative_drafts._run_provider_runtime",
            side_effect=complete_then_fail,
        ):
            response = self.client.post(
                f"/api/v1/projects/{self.project.id}/creative-drafts/generate/",
                {
                    "episode_id": str(self.episode.id),
                    "target_shot_count": 4,
                    "creativity": "balanced",
                },
                format="json",
            )

        self.assertEqual(response.status_code, 201, response.data)
        self.assertEqual(len(response.data["draft_data"]["shots"]), 2)
        codes = {issue["code"] for issue in response.data["validation"]["review_issues"]}
        self.assertTrue({"generation_incomplete", "shot_count_mismatch"} <= codes)
        self.assertTrue(response.data["runtime_metadata"]["generation_incomplete"])
        self.assertEqual(
            response.data["runtime_metadata"]["partial_failure"]["error_code"],
            "deepseek_api_network_error",
        )
        self.assertEqual(response.data["status"], EpisodeCreativeDraft.Status.DRAFT)

    def test_initial_and_whole_regenerate_respect_configured_batch_size(self):
        source_lines = [f"片段{index}动作。" for index in range(1, 5)]
        self.episode.script = "\n".join(source_lines)
        self.episode.save(update_fields=("script", "updated_at"))
        full_fit_config = {
            **creative_config(),
            "recommended_max_shots": 4,
            "batch_shots": 2,
        }

        captured_initial = []

        def complete_for_request(request, _config, _provider):
            captured_initial.append(deepcopy(request))
            candidate = self.candidate(count=request["options"]["shot_count"])
            for shot in candidate["shots"]:
                shot["dialogue"] = []
            return {
                "ok": True,
                "result": {
                    **candidate,
                    "source_fingerprint": request["context_hash"],
                },
                "runtime": {"model": "Qwen/Qwen3-4B-GGUF"},
            }

        with patch(
            "studio.services.creative_drafts.creative_provider_config",
            return_value=full_fit_config,
        ), patch(
            "studio.services.creative_drafts._run_provider_runtime",
            side_effect=complete_for_request,
        ) as runtime:
            initial = self.client.post(
                f"/api/v1/projects/{self.project.id}/creative-drafts/generate/",
                {
                    "episode_id": str(self.episode.id),
                    "target_shot_count": 4,
                    "creativity": "balanced",
                },
                format="json",
            )

        self.assertEqual(initial.status_code, 201, initial.data)
        self.assertEqual(runtime.call_count, 2)
        self.assertEqual(
            [request["options"]["shot_count"] for request in captured_initial],
            [2, 2],
        )
        self.assertEqual(
            [request["batch_context"]["source_mode"] for request in captured_initial],
            ["contiguous_excerpt", "contiguous_excerpt"],
        )

        batched_config = {
            **full_fit_config,
            "max_new_tokens": 2000,
            "max_output_tokens": 2000,
        }
        captured_whole = []

        def complete_whole(request, _config, _provider):
            captured_whole.append(deepcopy(request))
            candidate = self.candidate(count=request["options"]["shot_count"])
            for shot in candidate["shots"]:
                shot["dialogue"] = []
            return {
                "ok": True,
                "result": {
                    **candidate,
                    "source_fingerprint": request["context_hash"],
                },
                "runtime": {"model": "Qwen/Qwen3-4B-GGUF"},
            }

        with patch(
            "studio.services.creative_drafts.creative_provider_config",
            return_value=batched_config,
        ), patch(
            "studio.services.creative_drafts._run_provider_runtime",
            side_effect=complete_whole,
        ) as runtime:
            whole = self.client.post(
                f"/api/v1/creative-drafts/{initial.data['id']}/regenerate/",
                {
                    "expected_revision": initial.data["revision"],
                    "target_shot_count": 4,
                    "preserve_locked_content": True,
                },
                format="json",
            )

        self.assertEqual(whole.status_code, 201, whole.data)
        self.assertEqual(runtime.call_count, 2)
        self.assertEqual(whole.data["runtime_metadata"]["batch_count"], 2)
        self.assertTrue(all(
            "regeneration_context" not in request for request in captured_whole
        ))

        captured_selected = []

        def complete_selected(request, _config, _provider):
            captured_selected.append(deepcopy(request))
            candidate = self.candidate(count=4)
            for shot in candidate["shots"]:
                shot["dialogue"] = []
            return {
                "ok": True,
                "result": {
                    **candidate,
                    "source_fingerprint": request["context_hash"],
                },
                "runtime": {"model": "Qwen/Qwen3-4B-GGUF"},
            }

        with patch(
            "studio.services.creative_drafts.creative_provider_config",
            return_value=full_fit_config,
        ), patch(
            "studio.services.creative_drafts._run_provider_runtime",
            side_effect=complete_selected,
        ) as runtime:
            selected = self.client.post(
                f"/api/v1/creative-drafts/{whole.data['id']}/regenerate/",
                {
                    "expected_revision": whole.data["revision"],
                    "target_shot_count": 4,
                    "shot_keys": [whole.data["draft_data"]["shots"][0]["client_key"]],
                    "preserve_locked_content": True,
                },
                format="json",
            )

        self.assertEqual(selected.status_code, 201, selected.data)
        self.assertEqual(runtime.call_count, 1)
        selected_context = captured_selected[0]["regeneration_context"]
        self.assertEqual(selected_context["selected_shot_keys"], ["shot-01"])
        self.assertEqual(len(selected_context["previous_draft"]["shots"]), 4)

    def test_short_non_dialogue_source_uses_shared_read_only_batches(self):
        self.episode.script = "凌霜在雨夜客栈拔剑。"
        self.episode.save(update_fields=("script", "updated_at"))
        config = {
            **creative_config(),
            "recommended_max_shots": 4,
            "batch_shots": 4,
        }
        captured_requests = []

        def complete_batch(request, _config, _provider):
            captured_requests.append(deepcopy(request))
            candidate = self.candidate(count=request["options"]["shot_count"])
            for shot in candidate["shots"]:
                shot["dialogue"] = []
            return {
                "ok": True,
                "result": {
                    **candidate,
                    "source_fingerprint": request["context_hash"],
                },
                "runtime": {"model": "Qwen/Qwen3-4B-GGUF"},
            }

        with patch(
            "studio.services.creative_drafts.creative_provider_config",
            return_value=config,
        ), patch(
            "studio.services.creative_drafts._run_provider_runtime",
            side_effect=complete_batch,
        ) as runtime:
            response = self.client.post(
                f"/api/v1/projects/{self.project.id}/creative-drafts/generate/",
                {
                    "episode_id": str(self.episode.id),
                    "target_shot_count": 11,
                    "creativity": "balanced",
                    "extended_generation_confirmed": True,
                },
                format="json",
            )

        self.assertEqual(response.status_code, 201, response.data)
        self.assertEqual(runtime.call_count, 3)
        self.assertEqual(
            [request["options"]["shot_count"] for request in captured_requests],
            [4, 4, 3],
        )
        self.assertEqual(
            [request["batch_context"]["source_mode"] for request in captured_requests],
            ["shared_read_only", "shared_read_only", "shared_read_only"],
        )
        self.assertEqual(
            [request["episode"]["script"] for request in captured_requests],
            [self.episode.script, self.episode.script, self.episode.script],
        )
        self.assertEqual(len(response.data["draft_data"]["shots"]), 11)
        self.assertEqual(response.data["runtime_metadata"]["batch_count"], 3)

    def test_sparse_dialogue_source_assigns_dialogue_once_and_shares_no_manifest(self):
        self.episode.script = "凌霜：别回头。"
        self.episode.save(update_fields=("script", "updated_at"))
        config = {
            **creative_config(),
            "recommended_max_shots": 4,
            "batch_shots": 4,
        }
        captured_requests = []

        def complete_batch(request, _config, _provider):
            captured_requests.append(deepcopy(request))
            candidate = self.candidate(count=request["options"]["shot_count"])
            for shot in candidate["shots"]:
                shot["dialogue"] = []
            manifest = request["required_dialogue_manifest"]
            if manifest:
                source = manifest[0]
                candidate["shots"][0]["dialogue"] = [{
                    "speaker_id": source["speaker_id"],
                    "text_zh": source["text_zh"],
                    "delivery_zh": source.get("source_delivery_zh", ""),
                    "delivery_en": "",
                    "voiceover": bool(source.get("voiceover", False)),
                }]
            return {
                "ok": True,
                "result": {
                    **candidate,
                    "source_fingerprint": request["context_hash"],
                },
                "runtime": {"model": "Qwen/Qwen3-4B-GGUF"},
            }

        with patch(
            "studio.services.creative_drafts.creative_provider_config",
            return_value=config,
        ), patch(
            "studio.services.creative_drafts._run_provider_runtime",
            side_effect=complete_batch,
        ) as runtime:
            response = self.client.post(
                f"/api/v1/projects/{self.project.id}/creative-drafts/generate/",
                {
                    "episode_id": str(self.episode.id),
                    "target_shot_count": 11,
                    "creativity": "balanced",
                    "extended_generation_confirmed": True,
                },
                format="json",
            )

        self.assertEqual(response.status_code, 201, response.data)
        self.assertEqual(runtime.call_count, 3)
        self.assertEqual(
            [request["options"]["shot_count"] for request in captured_requests],
            [4, 4, 3],
        )
        self.assertEqual(
            [request["batch_context"]["source_mode"] for request in captured_requests],
            ["contiguous_excerpt", "shared_read_only", "shared_read_only"],
        )
        manifests = [
            request["required_dialogue_manifest"] for request in captured_requests
        ]
        self.assertEqual([len(manifest) for manifest in manifests], [1, 0, 0])
        self.assertEqual(manifests[0][0]["text_zh"], "别回头。")
        materialized = [
            line["text_zh"]
            for shot in response.data["draft_data"]["shots"]
            for line in shot["dialogue"]
        ]
        self.assertEqual(materialized, ["别回头。"])
        self.assertEqual(response.data["runtime_metadata"]["batch_count"], 3)

    def test_source_over_single_batch_budget_requires_confirmation_then_batches(self):
        source_lines = [
            f"动作{index}" + ("风" * 500) + "。"
            for index in range(1, 21)
        ]
        self.episode.script = "\n".join(source_lines)
        self.episode.save(update_fields=("script", "updated_at"))
        config = {
            **creative_config(),
            "max_source_chars": 6500,
            "recommended_max_shots": 4,
            "batch_shots": 4,
        }
        url = f"/api/v1/projects/{self.project.id}/creative-drafts/generate/"
        base_request = {
            "episode_id": str(self.episode.id),
            "target_shot_count": 4,
            "creativity": "balanced",
        }
        with patch(
            "studio.services.creative_drafts.creative_provider_config",
            return_value=config,
        ), patch(
            "studio.services.creative_drafts._run_provider_runtime",
        ) as runtime:
            unconfirmed = self.client.post(url, base_request, format="json")

        self.assertEqual(unconfirmed.status_code, 400, unconfirmed.data)
        details = unconfirmed.data["error"]["details"]
        self.assertEqual(details["code"], "creative_generation_confirmation_required")
        self.assertIn("source_char_count", details, details)
        self.assertEqual(details["source_char_count"], len(self.episode.script))
        self.assertEqual(details["max_source_chars"], 6500)
        runtime.assert_not_called()

        captured_requests = []

        def complete_batch(request, _config, _provider):
            captured_requests.append(deepcopy(request))
            candidate = self.candidate(count=request["options"]["shot_count"])
            for shot in candidate["shots"]:
                shot["dialogue"] = []
            return {
                "ok": True,
                "result": {
                    **candidate,
                    "source_fingerprint": request["context_hash"],
                },
                "runtime": {"model": "Qwen/Qwen3-4B-GGUF"},
            }

        with patch(
            "studio.services.creative_drafts.creative_provider_config",
            return_value=config,
        ), patch(
            "studio.services.creative_drafts._run_provider_runtime",
            side_effect=complete_batch,
        ):
            confirmed = self.client.post(
                url,
                {**base_request, "extended_generation_confirmed": True},
                format="json",
            )

        self.assertEqual(confirmed.status_code, 201, confirmed.data)
        self.assertGreater(len(captured_requests), 1)
        self.assertEqual(
            "\n".join(request["episode"]["script"] for request in captured_requests),
            self.episode.script,
        )
        self.assertTrue(confirmed.data["input_settings"]["extended_generation_confirmed"])
        self.assertEqual(
            confirmed.data["runtime_metadata"]["batch_count"],
            len(captured_requests),
        )

    def test_initial_batch_repairs_second_batch_dialogue_tampering(self):
        self.episode.script = "\n".join((
            "片段一动作。",
            "凌霜：台词一。",
            "片段三动作。",
            "凌霜：台词二。",
        ))
        self.episode.save(update_fields=("script", "updated_at"))
        config = {
            **creative_config(),
            "recommended_max_shots": 4,
            "batch_shots": 2,
            "max_new_tokens": 2000,
            "max_output_tokens": 2000,
        }
        call_index = 0

        def complete_batch(request, _config, _provider):
            nonlocal call_index
            call_index += 1
            candidate = self.candidate(count=2)
            for shot in candidate["shots"]:
                shot["dialogue"] = []
            candidate["shots"][0]["dialogue"] = [{
                "speaker_id": "S1",
                "text_zh": "台词一。" if call_index == 1 else "被篡改的台词。",
                "delivery_zh": "",
                "delivery_en": "",
                "voiceover": False,
            }]
            return {
                "ok": True,
                "result": {
                    **candidate,
                    "source_fingerprint": request["context_hash"],
                },
                "runtime": {"model": "Qwen/Qwen3-4B-GGUF"},
            }

        with patch(
            "studio.services.creative_drafts.creative_provider_config",
            return_value=config,
        ), patch(
            "studio.services.creative_drafts._run_provider_runtime",
            side_effect=complete_batch,
        ) as runtime:
            response = self.client.post(
                f"/api/v1/projects/{self.project.id}/creative-drafts/generate/",
                {
                    "episode_id": str(self.episode.id),
                    "target_shot_count": 4,
                    "creativity": "balanced",
                },
                format="json",
            )

        self.assertEqual(response.status_code, 201, response.data)
        self.assertEqual(runtime.call_count, 2)
        self.assertEqual(response.data["status"], EpisodeCreativeDraft.Status.DRAFT)
        self.assertEqual(response.data["validation"]["status"], "needs_review")
        self.assertTrue(any(
            issue["code"] == "dialogue_manifest_repaired"
            and issue["severity"] == "risk"
            for issue in response.data["validation"]["review_issues"]
        ))
        self.assertEqual([
            line["text_zh"]
            for shot in response.data["draft_data"]["shots"]
            for line in shot["dialogue"]
        ], ["台词一。", "台词二。"])
        self.assertEqual(EpisodeCreativeDraft.objects.count(), 1)

    def test_deepseek_single_shot_calls_preserve_authored_dialogue_ownership(self):
        sword_spirit = CharacterBible.objects.create(
            project=self.project,
            slug="sword-spirit",
            name="剑灵",
            stable_speaker_id="S2",
            visual_identity="剑中灵体",
        )
        elder = CharacterBible.objects.create(
            project=self.project,
            slug="elder-mo",
            name="墨渊",
            stable_speaker_id="S3",
            visual_identity="宗门长老",
        )
        mask = CharacterBible.objects.create(
            project=self.project,
            slug="bone-mask",
            name="白骨面具",
            stable_speaker_id="S4",
            visual_identity="白骨面具人",
        )
        source_dialogue = [
            [("S1", "第一句。")],
            [("S1", "第二句。")],
            [("S2", "第三句。")],
            [("S3", "第四句。")],
            [("S1", "第五句。")],
            [("S2", "第六句。")],
            [("S4", "第七句。")],
            [("S1", "第八句。")],
            [("S2", "第九句。")],
            [("S2", "第十句甲。"), ("S1", "第十句乙。")],
            [("S2", "第十一句甲。"), ("S1", "第十一句乙。")],
        ]
        speaker_names = {
            "S1": self.character.name,
            "S2": sword_spirit.name,
            "S3": elder.name,
            "S4": mask.name,
        }
        blocks = []
        for shot_index, lines in enumerate(source_dialogue, start=1):
            block_lines = [f"第{shot_index}镜动作。"]
            block_lines.extend(
                f"{speaker_names[speaker_id]}：{text}"
                for speaker_id, text in lines
            )
            blocks.append("\n".join(block_lines))
        self.episode.script = "\n\n".join(blocks)
        self.episode.save(update_fields=("script", "updated_at"))
        config = {
            **creative_provider_config("deepseek_api"),
            "recommended_max_shots": 4,
            "batch_shots": 1,
        }
        captured_requests = []

        def complete_single_shot(request, _config, provider):
            self.assertEqual(provider, "deepseek_api")
            captured_requests.append(deepcopy(request))
            candidate = self.candidate(count=1)
            candidate["shots"][0]["dialogue"] = []
            candidate["shots"][0]["character_ids"] = [
                str(self.character.id),
                str(sword_spirit.id),
                str(elder.id),
                str(mask.id),
            ]
            return {
                "ok": True,
                "result": {
                    **candidate,
                    "source_fingerprint": request["context_hash"],
                },
                "runtime": {
                    "model": "deepseek-v4-flash",
                    "transport": "remote",
                    "elapsed_ms": 10,
                },
            }

        with patch(
            "studio.services.creative_drafts.creative_provider_config",
            return_value=config,
        ), patch(
            "studio.services.creative_drafts._run_provider_runtime",
            side_effect=complete_single_shot,
        ) as runtime:
            generated = self.client.post(
                f"/api/v1/projects/{self.project.id}/creative-drafts/generate/",
                {
                    "episode_id": str(self.episode.id),
                    "provider": "deepseek_api",
                    "target_shot_count": 11,
                    "creativity": "balanced",
                    "extended_generation_confirmed": True,
                },
                format="json",
            )

        self.assertEqual(generated.status_code, 201, generated.data)
        self.assertEqual(runtime.call_count, 11)
        self.assertEqual(
            [request["options"]["shot_count"] for request in captured_requests],
            [1] * 11,
        )
        self.assertEqual(
            [request["episode"]["script"] for request in captured_requests],
            blocks,
        )
        self.assertEqual(
            [
                [
                    (line["speaker_id"], line["text_zh"])
                    for line in request["required_dialogue_manifest"]
                ]
                for request in captured_requests
            ],
            source_dialogue,
        )
        self.assertEqual(
            [
                [
                    (line["speaker_id"], line["text_zh"])
                    for line in shot["dialogue"]
                ]
                for shot in generated.data["draft_data"]["shots"]
            ],
            source_dialogue,
        )
        self.assertEqual(
            generated.data["runtime_metadata"]["batch_shot_counts"],
            [1] * 11,
        )

    def test_deepseek_multi_batch_repairs_thirteen_dialogues_and_requires_review(self):
        self.episode.script = "\n".join(
            f"凌霜（低声{index}）：第{index}句。" for index in range(1, 14)
        )
        self.episode.save(update_fields=("script", "updated_at"))
        config = {
            **creative_provider_config("deepseek_api"),
            "recommended_max_shots": 4,
            "batch_shots": 4,
            # The complete request does not fit, while each planned provider
            # batch does. This exercises the real multi-batch merge path.
            "max_output_tokens": 5000,
        }
        captured_requests = []

        def complete_batch(request, _config, provider):
            self.assertEqual(provider, "deepseek_api")
            captured_requests.append(deepcopy(request))
            candidate = self.candidate(count=request["options"]["shot_count"])
            for shot in candidate["shots"]:
                shot["dialogue"] = []
            manifest = request["required_dialogue_manifest"]
            if manifest:
                source = manifest[-1]
                candidate["shots"][-1]["dialogue"] = [{
                    "speaker_id": source["speaker_id"],
                    "text_zh": source["text_zh"],
                    "delivery_zh": source["source_delivery_zh"],
                    "delivery_en": f"preserved {source['text_zh']}",
                    "voiceover": False,
                }, {
                    "speaker_id": "S1",
                    "text_zh": "模型多写的对白。",
                    "delivery_zh": "",
                    "delivery_en": "invented line",
                    "voiceover": False,
                }]
            return {
                "ok": True,
                "result": {
                    **candidate,
                    "source_fingerprint": request["context_hash"],
                },
                "runtime": {"model": "deepseek-v4-flash"},
            }

        with patch(
            "studio.services.creative_drafts.creative_provider_config",
            return_value=config,
        ), patch(
            "studio.services.creative_drafts._run_provider_runtime",
            side_effect=complete_batch,
        ) as runtime:
            generated = self.client.post(
                f"/api/v1/projects/{self.project.id}/creative-drafts/generate/",
                {
                    "episode_id": str(self.episode.id),
                    "provider": "deepseek_api",
                    "target_shot_count": 11,
                    "creativity": "balanced",
                    "extended_generation_confirmed": True,
                },
                format="json",
            )

        self.assertEqual(generated.status_code, 201, generated.data)
        self.assertEqual(runtime.call_count, 3)
        self.assertEqual(
            [request["options"]["shot_count"] for request in captured_requests],
            [4, 4, 3],
        )
        self.assertEqual(generated.data["status"], EpisodeCreativeDraft.Status.DRAFT)
        self.assertEqual(generated.data["validation"]["status"], "needs_review")
        self.assertTrue(any(
            issue["code"] == "dialogue_manifest_repaired"
            and issue["severity"] == "risk"
            for issue in generated.data["validation"]["review_issues"]
        ))
        shots = generated.data["draft_data"]["shots"]
        self.assertTrue(all(len(shot["dialogue"]) <= 2 for shot in shots))
        flattened = [line for shot in shots for line in shot["dialogue"]]
        self.assertEqual(len(flattened), 13)
        self.assertEqual(
            [(line["speaker_id"], line["text_zh"], line["delivery_zh"])
             for line in flattened],
            [("S1", f"第{index}句。", f"低声{index}") for index in range(1, 14)],
        )
        self.assertEqual(
            [line["delivery_en"] for line in flattened if line["delivery_en"]],
            [
                f"preserved {request['required_dialogue_manifest'][-1]['text_zh']}"
                for request in captured_requests
            ],
        )

        denied = self.client.post(
            f"/api/v1/creative-drafts/{generated.data['id']}/confirm/",
            {"expected_revision": generated.data["revision"]},
            format="json",
        )
        self.assertEqual(denied.status_code, 400, denied.data)
        confirmed = self.client.post(
            f"/api/v1/creative-drafts/{generated.data['id']}/confirm/",
            {
                "expected_revision": generated.data["revision"],
                "review_acknowledged": True,
            },
            format="json",
        )
        self.assertEqual(confirmed.status_code, 200, confirmed.data)
        self.assertEqual(confirmed.data["status"], EpisodeCreativeDraft.Status.CONFIRMED)
        denied_apply = self.client.post(
            f"/api/v1/creative-drafts/{generated.data['id']}/apply/",
            {
                "expected_revision": confirmed.data["revision"],
                "language_mode": "chinese_only",
                "placement": "replace",
            },
            format="json",
        )
        self.assertEqual(denied_apply.status_code, 400, denied_apply.data)
        applied = self.client.post(
            f"/api/v1/creative-drafts/{generated.data['id']}/apply/",
            {
                "expected_revision": confirmed.data["revision"],
                "language_mode": "chinese_only",
                "placement": "replace",
                "review_acknowledged": True,
            },
            format="json",
        )
        self.assertEqual(applied.status_code, 200, applied.data)
        self.assertEqual(len(applied.data["applied_shot_ids"]), 11)

    def test_dialogue_density_above_recommendation_requires_confirmation_then_continues(self):
        self.episode.script = "\n".join(
            ["凌霜在雨夜客栈拔剑。"]
            + [f"凌霜：第{i}句。" for i in range(1, 10)]
        )
        self.episode.save(update_fields=("script", "updated_at"))

        with patch("studio.services.creative_drafts._run_provider_runtime") as runtime:
            response = self.client.post(
                f"/api/v1/projects/{self.project.id}/creative-drafts/generate/",
                {
                    "episode_id": str(self.episode.id),
                    "provider": "deepseek_api",
                    "target_shot_count": 4,
                    "creativity": "balanced",
                },
                format="json",
            )

        self.assertEqual(response.status_code, 400, response.data)
        details = response.data["error"]["details"]
        self.assertEqual(details["code"], "creative_generation_confirmation_required")
        self.assertEqual(details["required_dialogue_count"], 9)
        self.assertEqual(details["target_shot_count"], 4)
        self.assertEqual(details["dialogue_capacity"], 8)
        self.assertEqual(details["effective_max_dialogue_per_shot"], 3)
        runtime.assert_not_called()
        self.assertFalse(EpisodeCreativeDraft.objects.exists())
        self.assertFalse(Shot.objects.exists())

        captured = {}

        def complete(request, _config, _provider):
            captured.update(deepcopy(request))
            candidate = self.candidate(count=4)
            manifest = list(request["required_dialogue_manifest"])
            for index, shot in enumerate(candidate["shots"]):
                shot["dialogue"] = [{
                    "speaker_id": item["speaker_id"],
                    "text_zh": item["text_zh"],
                    "delivery_zh": item["source_delivery_zh"],
                    "delivery_en": "spoken as scripted",
                    "voiceover": False,
                } for item in manifest[index * 3:(index + 1) * 3]]
            return {
                "ok": True,
                "result": {
                    **candidate,
                    "source_fingerprint": request["context_hash"],
                },
                "runtime": {"model": "deepseek-v4-flash"},
            }

        with patch(
            "studio.services.creative_drafts._run_provider_runtime",
            side_effect=complete,
        ):
            generated = self.client.post(
                f"/api/v1/projects/{self.project.id}/creative-drafts/generate/",
                {
                    "episode_id": str(self.episode.id),
                    "provider": "deepseek_api",
                    "target_shot_count": 4,
                    "creativity": "balanced",
                    "extended_generation_confirmed": True,
                },
                format="json",
            )

        self.assertEqual(generated.status_code, 201, generated.data)
        self.assertEqual(captured["options"]["max_dialogue_per_shot"], 3)
        self.assertEqual(
            generated.data["input_settings"]["effective_max_dialogue_per_shot"], 3,
        )
        self.assertTrue(any(
            issue["code"] == "dialogue_density_high"
            for issue in generated.data["validation"]["review_issues"]
        ))
        self.assertEqual(sum(
            len(shot["dialogue"]) for shot in generated.data["draft_data"]["shots"]
        ), 9)
        self.assertFalse(Shot.objects.exists())

    def test_runtime_uses_utf8_and_parent_cleanup_grace(self):
        config = creative_config()
        captured = {}

        def invoke(*args, **kwargs):
            captured.update(kwargs)
            return SimpleNamespace(
                returncode=0,
                stdout='{"ok":true,"result":{},"metadata":{"phase":"complete"}}',
                stderr="",
            )

        # This is a subprocess-contract unit test, not a shared-resource lease
        # test. Keep it independent of an actual CUDA generation running on the
        # developer machine; lock contention itself is covered above.
        with tempfile.TemporaryDirectory() as directory, override_settings(
            LOCAL_QWEN_LOCK_PATH=str(Path(directory) / "local-qwen.lock"),
        ), patch("studio.services.creative_drafts.subprocess.run", side_effect=invoke):
            payload = _run_runtime({
                "operation": "creative_storyboard",
                "options": {"shot_count": 4},
                "max_new_tokens": 2176,
                "context_size": 8192,
                "timeout_seconds": 900,
            }, config)

        self.assertTrue(payload["ok"])
        self.assertEqual(captured["timeout"], 960)
        self.assertEqual(captured["env"]["PYTHONUTF8"], "1")
        self.assertEqual(captured["env"]["PYTHONIOENCODING"], "utf-8")

    def test_runtime_timeout_has_safe_auditable_metadata(self):
        config = creative_config()
        with tempfile.TemporaryDirectory() as directory, override_settings(
            LOCAL_QWEN_LOCK_PATH=str(Path(directory) / "local-qwen.lock"),
        ), patch(
            "studio.services.creative_drafts.subprocess.run",
            side_effect=subprocess.TimeoutExpired("qwen", 960, output="", stderr="partial"),
        ):
            with self.assertRaises(CreativeDraftError) as raised:
                _run_runtime({
                    "operation": "creative_storyboard",
                    "options": {"shot_count": 4},
                    "max_new_tokens": 2176,
                    "context_size": 8192,
                    "timeout_seconds": 900,
                }, config)

        self.assertEqual(raised.exception.code, "creative_parent_timeout")
        self.assertEqual(raised.exception.runtime_metadata["timeout_stage"], "parent_process")
        self.assertEqual(raised.exception.runtime_metadata["process_timeout_seconds"], 960)
        self.assertTrue(raised.exception.runtime_metadata["partial_stderr_present"])
