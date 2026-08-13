from unittest.mock import patch

from django.test import TestCase
from rest_framework.test import APIClient

from studio.models import Shot
from studio.services.dialogue import (
    DialogueContractError,
    normalize_dialogue,
    parse_delivery_annotation,
)
from studio.services.h3_prompts import H3PromptOptions, build_h3_video_prompt
from studio.services.script_breakdown import breakdown_script
from studio.tests.factories import build_story


class DialogueContractTests(TestCase):
    def setUp(self):
        (
            self.user, self.project, self.character, self.scene, self.episode,
            self.shot1, self.shot2,
        ) = build_story(username="dialogue-contract")
        self.client = APIClient()
        self.client.force_authenticate(self.user)

    def test_contract_accepts_optional_delivery_and_voiceover(self):
        value = [{
            "speaker_id": "S1",
            "text": "谁在等我！？",
            "delivery": "in a low, restrained voice",
            "voiceover": True,
        }]
        self.assertEqual(normalize_dialogue(value), value)
        self.assertEqual(
            parse_delivery_annotation("voiceover; in a low, restrained voice"),
            ("in a low, restrained voice", True),
        )

    def test_contract_rejects_unknown_keys_types_and_lengths(self):
        invalid_values = (
            [{"speaker_id": "S1", "text": "line", "cutoff": True}],
            [{"speaker_id": "S1", "text": "line", "delivery": 1}],
            [{"speaker_id": "S1", "text": "line", "voiceover": 1}],
            [{"speaker_id": "S1", "text": "x" * 2_001}],
            [{"speaker_id": "speaker-one", "text": "line"}],
        )
        for value in invalid_values:
            with self.subTest(value=value), self.assertRaises(DialogueContractError):
                normalize_dialogue(value)

    def test_shot_api_persists_strict_dialogue_contract(self):
        self.shot1.duration_ms = 5_000
        self.shot1.save(update_fields=("duration_ms", "updated_at"))
        dialogue = [{
            "speaker_id": "S1", "text": "原文。",
            "delivery": "softly", "voiceover": True,
        }]
        response = self.client.patch(
            f"/api/v1/shots/{self.shot1.id}/",
            {"duration_ms": 5_000, "dialogue": dialogue},
            format="json",
        )
        self.assertEqual(response.status_code, 200, response.data)
        self.shot1.refresh_from_db()
        self.assertEqual(self.shot1.dialogue, dialogue)

        rejected = self.client.patch(
            f"/api/v1/shots/{self.shot1.id}/",
            {"duration_ms": 5_000, "dialogue": [{
                "speaker_id": "S1", "text": "原文。", "scenetrans": True,
            }]},
            format="json",
        )
        self.assertEqual(rejected.status_code, 400, rejected.data)
        self.assertIn("未知字段", str(rejected.data))

    def test_script_breakdown_preserves_parenthetical_delivery_and_voiceover(self):
        self.episode.delete()
        result = breakdown_script(
            self.project,
            "第1集：归来\n镜头1：林默站在窗边。\n"
            "林默（voiceover; softly, with controlled breath）：我又回到了这里。\n"
            "镜头2：林默转身。\nS1（in a low, restrained voice）：别动。",
        )
        shots = list(result.episodes[0].shots.order_by("number"))
        self.assertEqual(shots[0].dialogue, [{
            "speaker_id": "S1",
            "text": "我又回到了这里。",
            "delivery": "softly, with controlled breath",
            "voiceover": True,
        }])
        self.assertEqual(shots[1].dialogue, [{
            "speaker_id": "S1", "text": "别动。",
            "delivery": "in a low, restrained voice",
        }])

    def test_h3_prompt_keeps_delivery_outside_verbatim_dialogue_and_closes_lips_for_vo(self):
        self.shot1.dialogue = [{
            "speaker_id": "S1",
            "text": "别动！？“原样”",
            "delivery": "in a low, restrained voice",
            "voiceover": True,
        }]
        result = build_h3_video_prompt(
            self.project, [self.shot1], H3PromptOptions(mode=Shot.H3Mode.T2VA),
        )
        self.assertIn(
            "(S1) says in an off-screen voiceover, in a low, restrained voice: "
            "<d>[Chinese] 别动！？“原样”</d>",
            result.text,
        )
        self.assertIn("keeps their lips closed and does not speak on camera", result.text)
        self.assertNotIn("in a low, restrained voice</d>", result.text)

    def test_ref2va_voiceover_keeps_subject_and_speaker_binding(self):
        self.shot1.dialogue = [{
            "speaker_id": "S1", "text": "原文。", "voiceover": True,
        }]
        binding = {
            "category": "character",
            "entity_id": str(self.character.id),
            "entity_name": self.character.name,
            "input_label": "<Picture 1>",
            "entity_label": "<Subject 1>",
            "name": "canonical portrait",
        }
        with patch(
            "studio.services.h3_prompts.resolve_group_reference_bindings",
            return_value=[binding],
        ):
            result = build_h3_video_prompt(
                self.project, [self.shot1], H3PromptOptions(mode=Shot.H3Mode.REF2VA),
            )
        self.assertIn(
            "<Subject 1> (S1) says in an off-screen voiceover: <d>[Chinese] 原文。</d>",
            result.text,
        )
        self.assertIn("<Subject 1> (S1) keeps their lips closed", result.text)

    def test_chinese_delivery_but_not_chinese_dialogue_enters_language_gate(self):
        self.shot1.dialogue = [{
            "speaker_id": "S1", "text": "中文对白。", "delivery": "压低声音",
        }]
        result = build_h3_video_prompt(
            self.project, [self.shot1], H3PromptOptions(mode=Shot.H3Mode.T2VA),
        )
        fields = result.payload["quality"]["fields_requiring_english_rewrite"]
        self.assertIn("镜头 1 对白 S1 演绎方式", fields)
        self.assertFalse(any(value.endswith("对白原文") for value in fields))

        audio_disabled = build_h3_video_prompt(
            self.project,
            [self.shot1],
            H3PromptOptions(mode=Shot.H3Mode.T2VA, include_audio=False),
        )
        disabled_fields = audio_disabled.payload["quality"]["fields_requiring_english_rewrite"]
        self.assertNotIn("镜头 1 对白 S1 演绎方式", disabled_fields)

    def test_ref2va_video_editing_skips_generation_word_count_guidance(self):
        binding = {
            "category": "video",
            "entity_id": None,
            "input_label": "<Video 1>",
            "entity_label": "",
            "purpose": "video_editing",
            "prompt_hint": "Replace only the final hand movement.",
            "audio_input_label": None,
        }
        with patch(
            "studio.services.h3_prompts.resolve_group_reference_bindings",
            return_value=[binding],
        ):
            result = build_h3_video_prompt(
                self.project, [self.shot1], H3PromptOptions(mode=Shot.H3Mode.REF2VA),
            )
        warnings = result.payload["quality"]["warnings"]
        self.assertFalse(any("350–500" in warning for warning in warnings))
