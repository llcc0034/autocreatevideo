from django.contrib.auth import get_user_model
from django.test import TestCase
from rest_framework.test import APIClient
from studio.models import ShotCharacterRelation

from studio.services.dialogue import (
    DialogueContractError,
    normalize_dialogue,
    parse_delivery_annotation,
)
from studio.services.configuration import create_config_revision, publish_config_revision
from dataclasses import replace
from studio.services.reference_context import compile_reference_context, reference_context_advisories
from studio.services.reference_context_facts import ReferenceSubjectFacts
from studio.services.prompt_compiler_contract import PromptBuildError
from studio.tests.reference_rendering_fixtures import reference_context_fixture, reference_unit_fixture, rendering_config
from studio.tests.factories import build_story, conditioning_contract


class DialogueContractTests(TestCase):
    def setUp(self):
        (
            self.user, self.project, self.character, self.scene, self.episode,
            self.shot1, self.shot2,
        ) = build_story(username="dialogue-contract")
        ShotCharacterRelation.objects.bulk_create([ShotCharacterRelation(shot=shot, character=self.character,
            role=role, position=0) for shot in (self.shot1, self.shot2) for role in ("visible", "speaker")])
        self.project.production_settings = {"default_conditioning_mode": "T2VA"}
        self.project.save(update_fields=("production_settings", "updated_at"))
        self.client = APIClient()
        self.client.force_authenticate(self.user)

    def test_contract_accepts_optional_delivery_and_voiceover(self):
        value = [{
            "speaker_id": "S1",
            "text": "谁在等我！？",
            "delivery": "in a low, restrained voice",
            "voiceover": True,
            "start_ms": 1000,
            "end_ms": 3500,
        }]
        self.assertEqual(normalize_dialogue(value), value)
        self.assertEqual(
            parse_delivery_annotation("voiceover; in a low, restrained voice"),
            ("in a low, restrained voice", True),
        )
        for annotation in (
            "voice-over, lips closed",
            "off-screen voice, calm",
            "VO; restrained",
            "画外音；克制",
            "旁白, low voice",
        ):
            with self.subTest(annotation=annotation):
                delivery, voiceover = parse_delivery_annotation(annotation)
                self.assertTrue(voiceover)
                self.assertNotRegex(
                    delivery,
                    r"(?i)voice[\s_-]?over|off[\s_-]?screen|\bvo\b|画外音|旁白",
                )

    def test_contract_rejects_unknown_keys_types_and_lengths(self):
        invalid_values = (
            [{"speaker_id": "S1", "text": "line", "cutoff": True}],
            [{"speaker_id": "S1", "text": "line", "delivery": 1}],
            [{"speaker_id": "S1", "text": "line", "voiceover": 1}],
            [{"speaker_id": "S1", "text": "line", "start_ms": 0}],
            [{"speaker_id": "S1", "text": "line", "start_ms": 2000, "end_ms": 1000}],
            [{"speaker_id": "S1", "text": "x" * 2_001}],
            [{"speaker_id": "speaker-one", "text": "line"}],
        )
        for value in invalid_values:
            with self.subTest(value=value), self.assertRaises(DialogueContractError):
                normalize_dialogue(value)

    def test_contract_limits_follow_live_superadmin_security_boundaries(self):
        admin = get_user_model().objects.create_superuser(
            username="dialogue-security-admin",
            password="StrongPass123!",
            email="admin@example.test",
        )
        lowered = create_config_revision(
            scope_type="system",
            values={"security.dialogue.text_max_characters": 4},
            created_by=admin,
        )
        publish_config_revision(lowered, published_by=admin)
        with self.assertRaisesRegex(DialogueContractError, "1–4"):
            normalize_dialogue([{"speaker_id": "S1", "text": "12345"}])

        raised = create_config_revision(
            scope_type="system",
            values={"security.dialogue.text_max_characters": 5},
            created_by=admin,
        )
        publish_config_revision(raised, published_by=admin)
        self.assertEqual(
            normalize_dialogue([{"speaker_id": "S1", "text": "12345"}]),
            [{"speaker_id": "S1", "text": "12345"}],
        )

    def test_shot_api_persists_strict_dialogue_contract(self):
        self.shot1.duration_ms = 8_000
        self.shot1.save(update_fields=("duration_ms", "updated_at"))
        dialogue = [{
            "speaker_id": "S1", "text": "原文。",
            "language": "Chinese",
            "delivery": "softly", "voiceover": True,
            "start_ms": 500, "end_ms": 6500,
        }]
        response = self.client.patch(
            f"/api/v1/shots/{self.shot1.id}/",
            {"duration_ms": 8_000, "dialogue": dialogue, "character_relations": [
                {"role":"speaker", "position":0, "character_id":str(self.character.pk)}]},
            format="json",
        )
        self.assertEqual(response.status_code, 200, response.data)
        self.shot1.refresh_from_db()
        self.assertEqual(self.shot1.dialogue, dialogue)

        timing_rejected = self.client.patch(
            f"/api/v1/shots/{self.shot1.id}/",
            {"duration_ms": 5000},
            format="json",
        )
        self.assertEqual(timing_rejected.status_code, 400, timing_rejected.data)
        self.assertIn("不能超过镜头时长", str(timing_rejected.data))

        rejected = self.client.patch(
            f"/api/v1/shots/{self.shot1.id}/",
            {"duration_ms": 8_000, "dialogue": [{
                "speaker_id": "S1", "text": "原文。", "scenetrans": True,
            }]},
            format="json",
        )
        self.assertEqual(rejected.status_code, 400, rejected.data)
        self.assertIn("未知字段", str(rejected.data))

    def render(self, context):
        return compile_reference_context(context, rendering_config()["primitive_options"]["reference_context_ir_v1"]["rendering"])

    def test_renderer_keeps_delivery_outside_verbatim_dialogue_and_closes_lips_for_vo(self):
        entry = {"speaker_id": "S1", "text": "别动！？“原样”", "language": "Chinese", "delivery": "in a low, restrained voice",
            "voiceover": True, "start_ms": 500, "end_ms": 2500, "subject": "", "voice_profile": "low voice"}
        unit = reference_unit_fixture(dialogue=(entry,), requires_first_frame_continuity=True,
            requires_last_frame_continuity=True)
        text = str(self.render(reference_context_fixture(ordered_units=(unit,)))["fields"])
        for fact in ("500–2500 ms", "(S1)", "in a low, restrained voice",
                     "<d>[Chinese] 别动！？“原样”</d>", "Off-screen voiceover",
                     "No on-screen lip movement", "First-frame continuity required: True",
                     "last-frame continuity required: True"):
            self.assertIn(fact, text)
        self.assertNotIn("in a low, restrained voice</d>", text)
        self.assertEqual(entry["text"], "别动！？“原样”")

    def test_ref2va_voiceover_keeps_explicit_subject_and_speaker_binding(self):
        unit = reference_unit_fixture(subject_keys=(("character", "subject-1"),), dialogue=({
            "speaker_id": "S1", "text": "原文。", "language": "Chinese", "voiceover": True, "delivery": "", "voice_profile": "",
            "subject": "<Subject 1>", "start_ms": 123, "end_ms": 3123,
        },))
        context = reference_context_fixture(ordered_units=(unit,), mode="Ref2VA",
            conditioning_mode_contract=conditioning_contract("Ref2VA", input_kind="classified_references")["modes"][0],
            subject_facts=(ReferenceSubjectFacts("character", "subject-1", ()),),
            reference_bindings=({"category": "character", "entity_id": "subject-1",
                "input_label": "<Picture 1>", "entity_label": "<Subject 1>", "name": "canonical portrait"},))
        text = str(self.render(context)["fields"])
        for fact in ("123–3123 ms", "Off-screen voiceover <Subject 1> (S1)",
                     "<d>[Chinese] 原文。</d>", "No on-screen lip movement"):
            self.assertIn(fact, text)
        self.assertNotIn("<Subject 1> (S1) says", text)

    def test_authored_dialogue_cannot_be_silently_discarded_when_audio_is_disabled(self):
        unit = reference_unit_fixture(dialogue=({"speaker_id": "S1", "text": "中文对白。", "language": "Chinese",
            "delivery": "压低声音", "voiceover": False, "start_ms": 100, "end_ms": 2100,
            "subject": "", "voice_profile": ""},))
        context = reference_context_fixture(ordered_units=(unit,))
        text = str(self.render(context)["fields"])
        self.assertIn("压低声音", text)
        self.assertIn("<d>[Chinese] 中文对白。</d>", text)
        with self.assertRaisesMessage(PromptBuildError, "不能静默丢弃"):
            self.render(replace(context, include_audio=False))
        explicit_silent = replace(context, include_audio=False, ordered_units=(replace(unit, dialogue=()),))
        silent_text = str(self.render(explicit_silent)["fields"])
        self.assertNotIn("压低声音", silent_text)
        self.assertNotIn("中文对白。", silent_text)

    def test_video_editing_uses_its_rule_not_motion_camera_guidance(self):
        binding = {"category": "video", "input_label": "<Video 1>", "entity_label": "",
            "purpose": "video_editing", "prompt_hint": "Edit."}
        context = reference_context_fixture(ordered_units=(reference_unit_fixture(),), mode="Ref2VA",
            conditioning_mode_contract=conditioning_contract("Ref2VA", input_kind="classified_references")["modes"][0],
            reference_bindings=(binding,))
        fields = self.render(context)["fields"]
        self.assertIn("video editing", fields["summary"])
        self.assertIn("partially_preserved", str(fields["retention_analysis"]))
        config = rendering_config()["quality_advisories"]
        self.assertEqual(reference_context_advisories(context, {}, config), [])
        motion = replace(context, reference_bindings=({**binding, "purpose": "motion_camera_reference"},))
        self.assertTrue(any("运镜参考说明未达到本策略建议值" in item
            for item in reference_context_advisories(motion, {}, config)))
