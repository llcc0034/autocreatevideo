import json
import re
from unittest.mock import Mock, patch

from django.test import SimpleTestCase, TestCase, override_settings

from studio.models import Shot
from studio.services.h3_prompt_optimizer import (
    H3PromptOptimizationBlocked,
    optimize_h3_video_prompt,
)
from studio.services.h3_prompts import (
    PROMPT_COMPILER_VERSION,
    H3PromptOptions,
    build_h3_video_prompt,
)
from studio.tests.factories import build_story


LOCAL_CONFIG = {
    "enabled": True,
    "base_url": "http://127.0.0.1:11434/v1",
    "api_key": "",
    "model": "local-minimax-m2",
    "allow_non_loopback": False,
    "timeout_seconds": 10,
    "failure_mode": "warn",
}

SAMPLE_PROMPT = """integrated_multimodal_description:
[Shot 1] 她推开门。The on-screen character (S1) says: <d>[Chinese] 别动。</d>
overall_soundscape: 雨声贴近窗户。
non_diegetic_music: N/A"""

REFERENCE_SAMPLE_PROMPT = """subject_definitions:
- <Subject 1> is defined by <Picture 1>.
- <Subject 2> is defined by <Picture 2>.
summary: [reference generation] <Subject 1> meets <Subject 2>.
retention_analysis:
- <Subject 1>: fully_preserved.
- <Subject 2>: fully_preserved.
detailed_description:
[Shot 1] <Subject 1> faces <Subject 2>.
overall_soundscape: Quiet room tone.
non_diegetic_music: N/A"""


def _response_for_request(request_kwargs, *, mutate=None):
    user_payload = json.loads(request_kwargs["json"]["messages"][-1]["content"])
    candidate = re.sub(r"[\u3400-\u4dbf\u4e00-\u9fff]+", "translated prose", user_payload["prompt"])
    if mutate:
        candidate = mutate(candidate)
    response = Mock()
    response.raise_for_status.return_value = None
    response.json.return_value = {
        "choices": [{"message": {"content": json.dumps({"prompt": candidate})}}]
    }
    return response


class H3PromptOptimizerSafetyTests(SimpleTestCase):
    @override_settings(H3_PROMPT_OPTIMIZER={**LOCAL_CONFIG, "enabled": False})
    @patch("studio.services.h3_prompt_optimizer.requests.post")
    def test_disabled_optimizer_never_sends_prompt(self, post):
        result = optimize_h3_video_prompt(SAMPLE_PROMPT, mode=Shot.H3Mode.T2VA)

        post.assert_not_called()
        self.assertEqual(result.text, SAMPLE_PROMPT)
        self.assertEqual(result.report["status"], "disabled")
        self.assertIn("未向任何外部服务发送", result.report["message"])

    @override_settings(H3_PROMPT_OPTIMIZER=LOCAL_CONFIG)
    @patch("studio.services.h3_prompt_optimizer.requests.post")
    def test_local_optimizer_preserves_authored_dialogue_and_structure(self, post):
        post.side_effect = lambda *args, **kwargs: _response_for_request(kwargs)

        result = optimize_h3_video_prompt(SAMPLE_PROMPT, mode=Shot.H3Mode.T2VA)

        self.assertEqual(result.report["status"], "optimized")
        self.assertIn("<d>[Chinese] 别动。</d>", result.text)
        self.assertNotRegex(
            re.sub(r"<d>.*?</d>", "", result.text, flags=re.DOTALL),
            r"[\u3400-\u4dbf\u4e00-\u9fff]",
        )
        self.assertEqual(
            [
                line.split(":", 1)[0]
                for line in result.text.splitlines()
                if line.startswith(("integrated_", "overall_", "non_diegetic_"))
            ],
            ["integrated_multimodal_description", "overall_soundscape", "non_diegetic_music"],
        )
        self.assertEqual(post.call_args.args[0], "http://127.0.0.1:11434/v1/chat/completions")
        self.assertNotIn("Authorization", post.call_args.kwargs["headers"])

    @override_settings(H3_PROMPT_OPTIMIZER=LOCAL_CONFIG)
    @patch("studio.services.h3_prompt_optimizer.requests.post")
    def test_invalid_structure_fails_closed_to_original_in_warn_mode(self, post):
        post.side_effect = lambda *args, **kwargs: _response_for_request(
            kwargs, mutate=lambda value: value.replace("[Shot 1]", "[Shot 2]")
        )

        result = optimize_h3_video_prompt(SAMPLE_PROMPT, mode=Shot.H3Mode.T2VA)

        self.assertEqual(result.text, SAMPLE_PROMPT)
        self.assertEqual(result.report["status"], "failed_validation")
        self.assertTrue(result.report["validation_errors"])

    @override_settings(H3_PROMPT_OPTIMIZER=LOCAL_CONFIG)
    @patch("studio.services.h3_prompt_optimizer.requests.post")
    def test_swapped_reference_labels_fail_closed_even_when_counts_match(self, post):
        def swap_subjects(value):
            return (
                value.replace("<Subject 1>", "__SUBJECT_SWAP__")
                .replace("<Subject 2>", "<Subject 1>")
                .replace("__SUBJECT_SWAP__", "<Subject 2>")
            )

        post.side_effect = lambda *args, **kwargs: _response_for_request(
            kwargs, mutate=swap_subjects,
        )

        result = optimize_h3_video_prompt(
            REFERENCE_SAMPLE_PROMPT,
            mode=Shot.H3Mode.REF2VA,
        )

        self.assertEqual(result.text, REFERENCE_SAMPLE_PROMPT)
        self.assertEqual(result.report["status"], "failed_validation")
        self.assertTrue(any(
            "Picture/Subject/Video/Audio" in error
            for error in result.report["validation_errors"]
        ))

    @override_settings(H3_PROMPT_OPTIMIZER=LOCAL_CONFIG)
    @patch("studio.services.h3_prompt_optimizer.requests.post")
    def test_swapped_numbers_on_the_same_line_fail_closed(self, post):
        prompt = SAMPLE_PROMPT.replace("她推开门。", "She moves 10 meters in 2 seconds.")
        post.side_effect = lambda *args, **kwargs: _response_for_request(
            kwargs,
            mutate=lambda value: value.replace(
                "10 meters in 2 seconds", "2 meters in 10 seconds",
            ),
        )

        result = optimize_h3_video_prompt(prompt, mode=Shot.H3Mode.T2VA)

        self.assertEqual(result.text, prompt)
        self.assertEqual(result.report["status"], "failed_validation")
        self.assertTrue(any(
            "数字" in error for error in result.report["validation_errors"]
        ))

    @override_settings(
        H3_PROMPT_OPTIMIZER={
            **LOCAL_CONFIG,
            "base_url": "https://api.example.com/v1",
            "api_key": "secret",
            "allow_non_loopback": False,
        }
    )
    @patch("studio.services.h3_prompt_optimizer.requests.post")
    def test_remote_endpoint_requires_explicit_consent(self, post):
        result = optimize_h3_video_prompt(SAMPLE_PROMPT, mode=Shot.H3Mode.T2VA)

        post.assert_not_called()
        self.assertEqual(result.report["status"], "misconfigured")
        self.assertIn("未获授权", result.report["message"])

    @override_settings(
        H3_PROMPT_OPTIMIZER={
            **LOCAL_CONFIG,
            "base_url": "https://api.example.com/v1",
            "api_key": "",
            "allow_non_loopback": True,
        }
    )
    @patch("studio.services.h3_prompt_optimizer.requests.post")
    def test_remote_endpoint_without_api_key_is_reported_without_a_request(self, post):
        result = optimize_h3_video_prompt(SAMPLE_PROMPT, mode=Shot.H3Mode.T2VA)

        post.assert_not_called()
        self.assertEqual(result.report["status"], "misconfigured")
        self.assertIn("缺少 API_KEY", result.report["message"])

    @override_settings(H3_PROMPT_OPTIMIZER=LOCAL_CONFIG)
    @patch("studio.services.h3_prompt_optimizer.requests.post")
    def test_lyrics_and_screen_text_blocks_are_preserved_verbatim(self, post):
        prompt = SAMPLE_PROMPT.replace(
            "她推开门。",
            "她推开门，电视显示 <screen_text>今日停电</screen_text>，"
            "远处歌声唱 <lyrics>长夜未央</lyrics>。",
        )
        post.side_effect = lambda *args, **kwargs: _response_for_request(kwargs)

        result = optimize_h3_video_prompt(prompt, mode=Shot.H3Mode.T2VA)

        self.assertEqual(result.report["status"], "optimized")
        self.assertIn("<screen_text>今日停电</screen_text>", result.text)
        self.assertIn("<lyrics>长夜未央</lyrics>", result.text)

    @override_settings(
        H3_PROMPT_OPTIMIZER={**LOCAL_CONFIG, "enabled": False, "failure_mode": "block"}
    )
    def test_block_mode_refuses_unoptimized_chinese_director_prose(self):
        with self.assertRaisesRegex(H3PromptOptimizationBlocked, "未启用"):
            optimize_h3_video_prompt(SAMPLE_PROMPT, mode=Shot.H3Mode.T2VA)


class H3PromptOptimizerIntegrationTests(TestCase):
    def setUp(self):
        self.user, self.project, _, _, _, self.shot, _ = build_story(username="optimizer")

    @override_settings(H3_PROMPT_OPTIMIZER=LOCAL_CONFIG)
    @patch("studio.services.h3_prompt_optimizer.requests.post")
    def test_compiler_records_successful_optimizer_state_and_clears_rewrite_queue(self, post):
        post.side_effect = lambda *args, **kwargs: _response_for_request(kwargs)

        result = build_h3_video_prompt(
            self.project,
            [self.shot],
            H3PromptOptions(mode=Shot.H3Mode.T2VA),
        )

        quality = result.payload["quality"]
        self.assertEqual(result.payload["prompt_compiler_version"], PROMPT_COMPILER_VERSION)
        self.assertEqual(quality["optimizer"]["status"], "optimized")
        self.assertEqual(quality["fields_requiring_english_rewrite"], [])
        self.assertTrue(quality["source_fields_optimized"])
        self.assertIn("<d>[Chinese] 这封信不是昨晚烧的。</d>", result.text)

    @override_settings(H3_PROMPT_OPTIMIZER={**LOCAL_CONFIG, "enabled": False})
    @patch("studio.services.h3_prompt_optimizer.requests.post")
    def test_compiler_truthfully_reports_disabled_optimizer(self, post):
        result = build_h3_video_prompt(
            self.project,
            [self.shot],
            H3PromptOptions(mode=Shot.H3Mode.T2VA),
        )

        post.assert_not_called()
        quality = result.payload["quality"]
        self.assertEqual(quality["optimizer"]["status"], "disabled")
        self.assertTrue(quality["fields_requiring_english_rewrite"])
        self.assertTrue(any("未向任何外部服务发送" in item for item in quality["warnings"]))
