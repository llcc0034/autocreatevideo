from unittest.mock import patch

from django.contrib.auth import get_user_model
from django.test import TestCase
from rest_framework.test import APIClient

from studio.services.visual_rule_drafts import (
    _negative_prompt,
    render_color_script,
    visual_rule_context_snapshot,
)
from studio.services.prompt_translation import PromptTranslationError
from studio.tests.factories import build_story


User = get_user_model()


class ProjectVisualRuleDraftAPITests(TestCase):
    def setUp(self):
        self.user, self.project, *_ = build_story(username="visual-rules")
        self.other = User.objects.create_user(username="visual-rules-other", password="StrongPass123!")
        self.project.master_script = "全剧保持冷峻雨夜氛围，线索逐步显现。"
        self.project.production_settings = {
            "translation_glossary": {"林默": "Lin Mo", "旧档案室": "Old Archive Room"},
            "custom_setting": "preserve-me",
        }
        self.project.save(update_fields=("master_script", "production_settings", "updated_at"))
        self.client = APIClient()
        self.client.force_authenticate(self.user)
        self.generate_url = f"/api/v1/projects/{self.project.id}/visual-rules-drafts/generate/"
        self.apply_url = f"/api/v1/projects/{self.project.id}/visual-rules-drafts/apply/"

    @staticmethod
    def config():
        return {
            "enabled": True,
            "model_id": "Qwen/Qwen3-4B-GGUF",
            "model_dir": "E:/models/qwen",
            "runtime_script": "E:/runtime/qwen_translate_once.py",
            "runtime_python": "E:/runtime/python.exe",
            "device": "cpu",
            "timeout_seconds": 300,
            "max_new_tokens": 1024,
        }

    @staticmethod
    def color_design(**overrides):
        value = {
            "base_colors": ["teal", "copper"],
            "accent_color": "amber",
            "palette_relation": "cinematic",
            "lighting": "soft_diffuse",
            "contrast": "restrained",
            "saturation": "muted",
            "exposure": "balanced",
            "progression": "cool_to_warm",
            "preserve_natural_skin_exposure": True,
        }
        value.update(overrides)
        return value

    @classmethod
    def runtime_response(cls, request):
        return {
            "ok": True,
            "model": "Qwen/Qwen3-4B-GGUF",
            "elapsed_ms": 1_234,
            "result": {"color_design": cls.color_design()},
            "metadata": {"repair_attempted": False, "repair_succeeded": False},
        }

    def generate(self, **payload):
        captured = {}

        def runtime(request, _config):
            captured.update(request)
            return self.runtime_response(request)

        with patch("studio.services.visual_rule_drafts.local_qwen_config", return_value=self.config()), patch(
            "studio.services.visual_rule_drafts.require_local_qwen_runtime",
        ), patch("studio.services.visual_rule_drafts.run_local_qwen_runtime", side_effect=runtime):
            response = self.client.post(self.generate_url, payload, format="json")
        return response, captured

    def test_generate_returns_isolated_candidate_and_exact_source_summary(self):
        before_settings = dict(self.project.production_settings)
        before_continuity = self.project.continuity_summary

        response, captured = self.generate(
            attempt=1,
            direction_note="Prefer a restrained cyan-gray palette with selective warm accents.",
        )

        self.assertEqual(response.status_code, 200, response.data)
        self.assertFalse(response.data["persisted"])
        self.assertTrue(response.data["review_required"])
        self.assertEqual(response.data["candidate_language"], "en")
        self.assertEqual(response.data["field_modes"], {
            "negative_prompt": "deterministic_guard",
            "color_script": "structured_design",
            "continuity_summary": "deterministic_guard",
        })
        self.assertEqual(response.data["color_design"], self.color_design())
        self.assertEqual(
            response.data["draft"]["color_script"],
            render_color_script(response.data["color_design"]),
        )
        self.assertEqual(response.data["source_summary"]["episode_summary_count"], 1)
        self.assertFalse(response.data["source_summary"]["episode_scripts_included"])
        self.assertFalse(response.data["source_summary"]["shots_included"])
        self.assertIn("episodes", captured["context"])
        self.assertNotIn("script", captured["context"]["episodes"][0])
        self.assertNotIn("negative_prompt", captured)
        self.assertNotIn("continuity_summary", captured)
        self.assertIn("Character Bible", response.data["draft"]["continuity_summary"])
        self.assertIn("Scene Bible", response.data["draft"]["continuity_summary"])
        self.assertNotIn("林默", response.data["draft"]["continuity_summary"])
        self.project.refresh_from_db()
        self.assertEqual(self.project.production_settings, before_settings)
        self.assertEqual(self.project.continuity_summary, before_continuity)

    def test_regenerate_rejects_stale_expected_fingerprint_without_loading_model(self):
        with patch("studio.services.visual_rule_drafts.run_local_qwen_runtime") as runtime:
            response = self.client.post(self.generate_url, {
                "attempt": 2,
                "variation_note": "Try a more restrained treatment.",
                "expected_context_fingerprint": "f" * 64,
            }, format="json")
        self.assertEqual(response.status_code, 409, response.data)
        self.assertEqual(response.data["error"]["code"], "visual_rules_source_conflict")
        runtime.assert_not_called()

    @patch("studio.services.visual_rule_drafts.GenerationTask.objects.filter")
    def test_generate_is_busy_while_video_task_is_active(self, task_filter):
        task_filter.return_value.exists.return_value = True
        with patch("studio.services.visual_rule_drafts.run_local_qwen_runtime") as runtime:
            response = self.client.post(self.generate_url, {}, format="json")
        self.assertEqual(response.status_code, 409, response.data)
        self.assertEqual(response.data["error"]["code"], "visual_rules_resource_busy")
        runtime.assert_not_called()

    def test_runtime_failure_returns_safe_diagnostic_code(self):
        with patch("studio.services.visual_rule_drafts.local_qwen_config", return_value=self.config()), patch(
            "studio.services.visual_rule_drafts.require_local_qwen_runtime",
        ), patch(
            "studio.services.visual_rule_drafts.run_local_qwen_runtime",
            side_effect=PromptTranslationError(
                "安全的全剧视觉规则失败说明", code="visual_rules_validation_failed",
            ),
        ):
            response = self.client.post(self.generate_url, {}, format="json")
        self.assertEqual(response.status_code, 503, response.data)
        self.assertEqual(response.data["error"]["code"], "visual_rules_runtime_unavailable")
        self.assertNotIn("traceback", str(response.data).casefold())

    def test_generate_detects_source_change_during_inference(self):
        def runtime(request, _config):
            self.project.logline = "推理过程中变化的权威源"
            self.project.save(update_fields=("logline", "updated_at"))
            return self.runtime_response(request)

        with patch("studio.services.visual_rule_drafts.local_qwen_config", return_value=self.config()), patch(
            "studio.services.visual_rule_drafts.require_local_qwen_runtime",
        ), patch("studio.services.visual_rule_drafts.run_local_qwen_runtime", side_effect=runtime):
            response = self.client.post(self.generate_url, {}, format="json")
        self.assertEqual(response.status_code, 409, response.data)

    def test_apply_is_atomic_preserves_other_settings_and_keeps_locked_fields_exact(self):
        generated, _captured = self.generate(
            attempt=2,
            direction_note="Use a cool investigative palette.",
            variation_note="Reserve warm gold for the climax.",
        )
        self.assertEqual(generated.status_code, 200, generated.data)
        payload = {
            "context_fingerprint": generated.data["context_fingerprint"],
            "generation_fingerprint": generated.data["generation"]["generation_fingerprint"],
            "attempt": 2,
            "direction_note": "Use a cool investigative palette.",
            "variation_note": "Reserve warm gold for the climax.",
            "color_design": generated.data["color_design"],
        }

        applied = self.client.post(self.apply_url, payload, format="json")

        self.assertEqual(applied.status_code, 200, applied.data)
        self.assertNotEqual(
            applied.data["applied_context_fingerprint"],
            applied.data["current_context_fingerprint"],
        )
        self.project.refresh_from_db()
        self.assertEqual(
            self.project.production_settings["negative_prompt"],
            generated.data["draft"]["negative_prompt"],
        )
        self.assertEqual(
            self.project.production_settings["color_script"],
            generated.data["draft"]["color_script"],
        )
        self.assertEqual(
            self.project.continuity_summary,
            generated.data["draft"]["continuity_summary"],
        )
        self.assertEqual(self.project.production_settings["custom_setting"], "preserve-me")

    def test_apply_rejects_stale_source_generation_mismatch_and_adversarial_fields(self):
        generated, _captured = self.generate(attempt=1)
        self.assertEqual(generated.status_code, 200, generated.data)
        base = {
            "context_fingerprint": generated.data["context_fingerprint"],
            "generation_fingerprint": generated.data["generation"]["generation_fingerprint"],
            "attempt": 1,
            "direction_note": "",
            "variation_note": "",
            "color_design": generated.data["color_design"],
        }
        mismatch = self.client.post(
            self.apply_url, {**base, "generation_fingerprint": "0" * 64}, format="json",
        )
        self.assertEqual(mismatch.status_code, 409, mismatch.data)

        invalid_drafts = []
        for field, value in (
            ("color_script", "Use amber lighting without watermark."),
            ("color_script", "Use янтарный color and lighting."),
            ("color_script", "Use amber color with a 35mm camera pan."),
            ("color_script", "Use Lin Mo red lighting and stable color."),
            ("color_script", "Use a silver pistol as a visual highlight."),
            ("color_script", "The heroine betrays her brother during a red color transition."),
            ("color_script", "Use imperial palace architecture with a gold palette."),
            ("color_script", "Use a red coat as the dominant color accent."),
            ("continuity_summary", "Maintain a fixed red coat and silver pistol across episodes."),
            ("negative_prompt", generated.data["draft"]["negative_prompt"] + ", natural skin tones"),
        ):
            candidate = dict(generated.data["draft"])
            candidate[field] = value
            invalid_drafts.append(candidate)
        for candidate in invalid_drafts:
            with self.subTest(candidate=candidate):
                response = self.client.post(
                    self.apply_url, {**base, "draft": candidate}, format="json",
                )
                self.assertEqual(response.status_code, 400, response.data)

        self.project.logline = "应用前源已变化"
        self.project.save(update_fields=("logline", "updated_at"))
        stale = self.client.post(self.apply_url, base, format="json")
        self.assertEqual(stale.status_code, 409, stale.data)

    def test_apply_rejects_unknown_duplicate_empty_and_fact_injection_designs(self):
        generated, _captured = self.generate(attempt=1)
        self.assertEqual(generated.status_code, 200, generated.data)
        base = {
            "context_fingerprint": generated.data["context_fingerprint"],
            "generation_fingerprint": generated.data["generation"]["generation_fingerprint"],
            "attempt": 1,
            "direction_note": "",
            "variation_note": "",
        }
        invalid_designs = []
        for bad_base in (
            [], ["teal", "teal"], ["silver"], ["silver key"], ["red skin"],
            ["detective becomes darker"], ["detective romance"], ["golden stage"],
            ["teal\u200b"], ["teal\u202eromance"], ["🎨"], ["teal "], ["TEAL"],
            ["ｔｅａｌ"], [1], [None],
        ):
            invalid_designs.append(self.color_design(base_colors=bad_base))
        complete_design = self.color_design()
        invalid_designs.extend(
            {key: value for key, value in complete_design.items() if key != missing}
            for missing in complete_design
        )
        invalid_designs.extend((
            {**self.color_design(), "extra": "heroine betrays brother"},
            self.color_design(accent_color="silver pistol"),
            self.color_design(lighting="imperial_palace"),
            self.color_design(preserve_natural_skin_exposure="true"),
        ))
        for design in invalid_designs:
            with self.subTest(design=design):
                response = self.client.post(
                    self.apply_url, {**base, "color_design": design}, format="json",
                )
                self.assertEqual(response.status_code, 400, response.data)

    def test_apply_accepts_reviewed_enum_change_and_renders_server_side(self):
        generated, _captured = self.generate(attempt=1)
        self.assertEqual(generated.status_code, 200, generated.data)
        design = self.color_design(
            base_colors=["navy", "gold", "ivory"], accent_color="none",
            lighting="low_key", progression="stable",
            preserve_natural_skin_exposure=False,
        )
        response = self.client.post(self.apply_url, {
            "context_fingerprint": generated.data["context_fingerprint"],
            "generation_fingerprint": generated.data["generation"]["generation_fingerprint"],
            "attempt": 1,
            "direction_note": "",
            "variation_note": "",
            "color_design": design,
        }, format="json")
        self.assertEqual(response.status_code, 200, response.data)
        self.assertEqual(response.data["applied"]["color_script"], render_color_script(design))
        self.assertTrue(response.data["applied"]["color_script"].isascii())

    def test_negative_merge_preserves_safe_explicit_phrases_and_rejects_positive_pollution(self):
        self.project.style.negative_prompt = (
            "no graphic gore, no modern objects, natural skin tones, no warm gold, Lin Mo blur"
        )
        self.project.style.save(update_fields=("negative_prompt", "updated_at"))
        snapshot = visual_rule_context_snapshot(self.project)

        negative, allowed, warnings = _negative_prompt(snapshot)

        self.assertIn("no graphic gore", negative)
        self.assertIn("no modern objects", negative)
        self.assertNotIn("natural skin tones", negative)
        self.assertNotIn("no warm gold", negative)
        self.assertNotIn("Lin Mo blur", negative)
        self.assertIn("no graphic gore", allowed)
        self.assertTrue(warnings)

    def test_cross_tenant_project_is_not_disclosed(self):
        self.client.force_authenticate(self.other)
        response = self.client.post(self.generate_url, {}, format="json")
        self.assertEqual(response.status_code, 404, response.data)
