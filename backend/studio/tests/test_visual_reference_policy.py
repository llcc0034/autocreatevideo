from django.test import TestCase

from studio.models import Asset, CharacterBible
from studio.tests.media_prompt_fixtures import (
    MediaPromptOptions,
    build_test_image_prompt,
    build_test_video_prompt,
)
from studio.services.production_runs import _bible_entities
from studio.services.reference_bindings import (
    ReferenceBindingError,
    normalize_shot_reference_bindings,
)
from studio.services.visual_reference_policy import (
    VISUAL_REFERENCE_POLICY_METADATA_KEY,
    character_visual_reference_decision,
)
from studio.tests.factories import build_story


class CharacterVisualReferencePolicyTests(TestCase):
    def setUp(self):
        (
            self.user,
            self.project,
            self.visible_character,
            self.scene,
            self.episode,
            self.shot,
            _shot2,
        ) = build_story(username="voice-only-reference")
        self.voice_asset = Asset.objects.create(
            owner=self.user,
            style=self.project.style,
            kind=Asset.Kind.CHARACTER,
            name="画外说话人错误参考图",
            local_path="C:/fixtures/offscreen-speaker.png",
            checksum="fixture-offscreen-speaker",
            mime_type="image/png",
        )
        self.voice_only = CharacterBible.objects.create(
            project=self.project,
            slug="offscreen-companion",
            name="同伴",
            stable_speaker_id="S2",
            description="林纪的同伴，只通过声音出现，从不出镜。",
            visual_identity="Unseen offscreen companion, voice only",
            voice_profile="calm restrained voice",
            anchor_asset=self.voice_asset,
        )
        self.shot.characters.add(self.voice_only)
        self.shot.dialogue = [{
            "speaker_id": "S2",
            "text": "别碰它，它在呼吸。",
            "start_ms": 500,
            "end_ms": 2500,
            "delivery": "with a warning",
        }]
        self.shot.save(update_fields=("dialogue", "updated_at"))

    @staticmethod
    def _structured_mode(mode: str) -> dict:
        return {
            VISUAL_REFERENCE_POLICY_METADATA_KEY: {
                "schema_version": 1,
                "mode": mode,
            },
        }

    def test_structured_policy_wins_and_auto_uses_high_confidence_semantics(self):
        inferred = character_visual_reference_decision(self.voice_only)
        self.assertFalse(inferred.eligible)
        self.assertEqual(inferred.source, "semantic_fallback")

        self.voice_only.metadata = self._structured_mode("include")
        included = character_visual_reference_decision(self.voice_only)
        self.assertTrue(included.eligible)
        self.assertEqual(included.source, "structured_metadata")

        self.visible_character.metadata = self._structured_mode("exclude")
        excluded = character_visual_reference_decision(self.visible_character)
        self.assertFalse(excluded.eligible)
        self.assertEqual(excluded.reason, "explicit_exclude")

    def test_voice_only_speaker_is_not_a_bible_image_target(self):
        targets = {
            (entity_type, str(entity.id))
            for entity_type, entity in _bible_entities(self.project)
        }

        self.assertIn(("character", str(self.visible_character.id)), targets)
        self.assertNotIn(("character", str(self.voice_only.id)), targets)
        self.assertIn(("scene", str(self.scene.id)), targets)

    def test_visual_binding_rejects_voice_only_but_keeps_shot_speaker_relation(self):
        with self.assertRaisesRegex(
            ReferenceBindingError,
            "说话人事实可保留，但不能绑定图片",
        ):
            normalize_shot_reference_bindings(
                mode="Ref2VA",
                bindings={
                    "characters": [{
                        "entity_id": str(self.voice_only.id),
                        "asset_id": str(self.voice_asset.id),
                    }],
                },
                project=self.project,
                characters=list(self.shot.characters.all()),
                scene=self.shot.scene,
                props=list(self.shot.props.all()),
                shot_reference_assets=list(self.shot.reference_assets.all()),
                user=self.user,
            )

        self.assertTrue(self.shot.characters.filter(pk=self.voice_only.id).exists())
        self.assertEqual(self.shot.dialogue[0]["speaker_id"], "S2")

    def test_h3_keeps_voice_only_dialogue_without_visual_subject_definition(self):
        options = MediaPromptOptions(
            model_identifier="minimax-h3-test",
            mode="Ref2VA",
            language_gate_mode="warn",
        )
        result = build_test_video_prompt(self.project, [self.shot], options)

        for fact in ("Off-screen voiceover", "(S2)", "calm restrained voice",
                     "with a warning", "<d>[Chinese] 别碰它，它在呼吸。</d>"):
            self.assertIn(fact, result.text)
        self.assertNotIn("Unseen offscreen companion, voice only", result.text)
        self.assertNotIn("[Character S2]", result.text)
        self.assertNotIn("corresponding visible on-screen character", result.text)
        self.assertFalse(any(
            item.get("category") == "character"
            and item.get("entity_id") == str(self.voice_only.id)
            for item in result.payload["reference_bindings"]
        ))

        image = build_test_image_prompt(
            self.project,
            self.shot,
            language_gate_mode="warn",
        )
        self.assertNotIn(
            "Unseen offscreen companion, voice only",
            image.text,
        )
        self.assertFalse(any(
            item.get("asset_id") == str(self.voice_asset.id)
            for item in image.payload["reference_bindings"]
        ))
