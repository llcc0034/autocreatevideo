from copy import deepcopy
from pathlib import Path
import subprocess
import tempfile
from types import SimpleNamespace
from unittest.mock import patch
from uuid import uuid4

from django.contrib.auth import get_user_model
from django.test import TestCase, override_settings
from rest_framework.test import APIClient

from studio.models import (
    CharacterBible,
    Episode,
    EpisodeCreativeDraft,
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
    ensure_creative_resources_available,
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
        "max_new_tokens": 3072,
        "context_size": 8192,
        "min_shots": 1,
        "max_shots": 4,
        "max_source_chars": 16000,
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
                "include_audio": True, "scene_id": str(self.scene.id),
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
        durations = _recommended_shot_durations_ms(script, 2, 5_000)
        self.assertEqual(len(durations), 2)
        self.assertTrue(all(5_000 <= value <= 15_000 for value in durations))
        self.assertGreater(sum(durations), 10_000)
        issue = _script_action_coverage_issue(script, [{
            "narrative_zh": "凌霜拔剑出鞘，跃入雨幕。",
            "action_zh": "杀手扑出挥刀直劈。",
        }])
        self.assertEqual(issue["code"], "script_action_coverage_low")
        self.assertNotIn("/", issue["suggestion_zh"])

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
        self.assertEqual(response.data["min_shots"], 1)
        self.assertEqual(response.data["max_shots"], 4)
        self.assertEqual(response.data["baseline_max_new_tokens"], 2304)
        self.assertEqual(response.data["max_new_tokens"], 3072)
        self.assertEqual(response.data["runtime_timeout_seconds"], 900)
        self.assertEqual(response.data["process_timeout_seconds"], 960)
        self.assertTrue(response.data["review_required"])

    def test_busy_guard_queries_active_generation_before_runtime(self):
        mocked = type("Exists", (), {"exists": lambda self: True})()
        with patch("studio.services.creative_drafts.GenerationTask.objects.filter", return_value=mocked):
            with self.assertRaises(CreativeResourceBusy):
                ensure_creative_resources_available()

    def test_source_budget_fails_explicitly_without_truncation(self):
        snapshot = creative_context_snapshot(self.project, self.episode)
        tiny = {**creative_config(), "max_source_chars": 20}
        with patch("studio.services.creative_drafts.creative_config", return_value=tiny):
            response = self.client.post(
                f"/api/v1/projects/{self.project.id}/creative-drafts/generate/",
                {
                    "episode_id": str(self.episode.id), "target_shot_count": 1,
                    "creativity": "balanced",
                }, format="json",
            )
        self.assertEqual(response.status_code, 400)
        self.assertIn("超过本地模型单次", str(response.data))

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
        with self.assertRaisesRegex(CreativeValidationError, "只能容纳 8 句"):
            request_for(9, shot_count=4)

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

    def test_generation_rejects_unverified_five_shot_cpu_request(self):
        response = self.client.post(
            f"/api/v1/projects/{self.project.id}/creative-drafts/generate/",
            {
                "episode_id": str(self.episode.id),
                "target_shot_count": 5,
                "creativity": "balanced",
            },
            format="json",
        )
        self.assertEqual(response.status_code, 400, response.data)

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
