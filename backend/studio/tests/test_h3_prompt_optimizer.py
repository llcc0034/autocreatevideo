from unittest.mock import patch

from django.test import SimpleTestCase, TestCase

from studio.models import Shot
from studio.services.h3_prompt_optimizer import (
    H3PromptOptimizationBlocked,
    h3_prompt_optimizer_input_fingerprint,
    optimize_h3_video_prompt,
)
from studio.services.h3_prompts import H3PromptOptions, PromptBuildError, build_h3_video_prompt
from studio.tests.factories import build_story


SAMPLE_PROMPT = """integrated_multimodal_description:
[Shot 1] 她推开门。The on-screen character says: <d>[Chinese] 别动。</d>
overall_soundscape: rain against the window.
non_diegetic_music: N/A"""


class H3PromptOptimizerRetirementTests(SimpleTestCase):
    @patch("studio.services.provider_registry.requests.post")
    def test_automatic_optimizer_never_sends_prompt(self, post):
        result = optimize_h3_video_prompt(
            SAMPLE_PROMPT, mode=Shot.H3Mode.T2VA, failure_mode="warn",
        )

        post.assert_not_called()
        self.assertEqual(result.text, SAMPLE_PROMPT)
        self.assertEqual(result.report["status"], "disabled")
        self.assertEqual(result.report["transport"], "none")

    def test_block_policy_refuses_unreviewed_director_text(self):
        with self.assertRaisesRegex(H3PromptOptimizationBlocked, "prompt.normalize"):
            optimize_h3_video_prompt(
                SAMPLE_PROMPT, mode=Shot.H3Mode.T2VA, failure_mode="block",
            )

    def test_optimizer_fingerprint_is_explicit_and_stable(self):
        first = h3_prompt_optimizer_input_fingerprint(language_gate="warn")
        second = h3_prompt_optimizer_input_fingerprint(language_gate="warn")
        blocked = h3_prompt_optimizer_input_fingerprint(language_gate="block")
        self.assertEqual(first, second)
        self.assertNotEqual(first, blocked)


class H3PromptCompilerPolicyTests(TestCase):
    def setUp(self):
        self.user, self.project, _, _, _, self.shot, _ = build_story(
            username="optimizer-retired",
        )

    def test_warn_policy_records_disabled_optimizer_without_network(self):
        result = build_h3_video_prompt(
            self.project,
            [self.shot],
            H3PromptOptions(mode=Shot.H3Mode.T2VA, language_gate_mode="warn"),
        )
        quality = result.payload["quality"]
        self.assertEqual(quality["optimizer"]["status"], "disabled")
        self.assertTrue(quality["fields_requiring_english_rewrite"])

    def test_block_policy_stops_compilation_before_task_materialization(self):
        with self.assertRaisesRegex(PromptBuildError, "prompt.normalize"):
            build_h3_video_prompt(
                self.project,
                [self.shot],
                H3PromptOptions(mode=Shot.H3Mode.T2VA, language_gate_mode="block"),
            )
