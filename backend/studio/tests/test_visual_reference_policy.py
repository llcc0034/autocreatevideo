from copy import deepcopy
from unittest.mock import patch

from django.core.exceptions import ValidationError
from django.contrib.auth import get_user_model
from django.db import connection
from django.test import TestCase
from django.test.utils import CaptureQueriesContext

from studio.models import Asset, BibleVariant, CharacterBible, ExecutionPreparation, GenerationAttempt, ShotCharacterRelation
from studio.services.bible_variants import create_variant, revise_variant, review_variant, setting_payload
from studio.services.consistency import capture_consistency_snapshot
from studio.services.production_storyboard_content import storyboard_content
from dataclasses import replace
from studio.services.reference_context import compile_reference_context
from studio.tests.reference_rendering_fixtures import reference_context_fixture, reference_unit_fixture, rendering_config
from studio.services.production_runs import _bible_entities
from studio.services.visual_reference_policy import (
    VISUAL_REFERENCE_POLICY_METADATA_KEY,
    VisualReferencePolicyError,
    character_visual_reference_decision,
    character_visual_reference_decisions,
    visual_reference_characters,
)
from studio.tests.factories import build_story, conditioning_contract


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
            description="林纪的同伴。",
            voice_profile="calm restrained voice",
            anchor_asset=self.voice_asset,
        )
        self.primary = self.create_primary(self.voice_only, "Unseen offscreen companion, voice only")
        self.shot.dialogue = [{
            "speaker_id": "S2",
            "text": "别碰它，它在呼吸。",
            "start_ms": 500,
            "end_ms": 2500,
            "delivery": "with a warning",
        }]
        self.shot.save(update_fields=("dialogue", "updated_at"))

    def create_primary(self, character, identity):
        return create_variant(actor=self.user, project_id=character.project_id,
            entity_kind="character", entity_id=character.pk, appearance_role="primary",
            primary_setting_revision_id=None, primary_setting_sha256="", value={
                "name": "Primary", "reference_name": f"{character.slug} primary",
                "identity_anchor": identity, "appearance_anchor": "Authored appearance",
                "basis_asset_id": None, "basis_asset_sha256": ""})

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

    def test_auto_batches_unique_character_primaries(self):
        self.create_primary(self.visible_character, "Visible person")
        rows = list(self.project.characters.order_by("id"))
        with CaptureQueriesContext(connection) as queries:
            decisions = character_visual_reference_decisions([*rows, *rows])
        self.assertEqual(len(queries), 2)
        self.assertTrue(decisions[str(self.visible_character.pk)].eligible)
        self.assertFalse(decisions[str(self.voice_only.pk)].eligible)
        for query in queries:
            self.assertNotIn('"visual_identity"', query["sql"])
            self.assertNotIn('"studio_scriptrevision"."content"', query["sql"])
            self.assertTrue(query["sql"].startswith("SELECT"))
            if connection.vendor == "sqlite":
                with connection.cursor() as cursor:
                    cursor.execute("EXPLAIN QUERY PLAN " + query["sql"])
                    plan = " ".join(str(row[-1]) for row in cursor.fetchall()).upper()
                self.assertIn("SEARCH", plan)
                self.assertNotIn("SCAN STUDIO_", plan)
        self.assertEqual(visual_reference_characters(rows), [self.visible_character])

    def test_absent_and_blank_primary_do_not_read_old_text_or_create_facts(self):
        character = self.visible_character
        character.description = ""
        counts = (Asset.objects.count(), ExecutionPreparation.objects.count(), GenerationAttempt.objects.count())
        with self.assertNumQueries(1):
            self.assertTrue(character_visual_reference_decision(character).eligible)
        self.create_primary(character, "")
        with self.assertNumQueries(2):
            self.assertTrue(character_visual_reference_decision(character).eligible)
        self.assertEqual(counts, (Asset.objects.count(), ExecutionPreparation.objects.count(), GenerationAttempt.objects.count()))
        with self.assertNumQueries(0):
            self.assertEqual(character_visual_reference_decisions([]), {})

    def test_explicit_modes_skip_primary_read_but_invalid_modes_fail(self):
        BibleVariant.objects.filter(pk=self.primary.pk).update(revision=999)
        for mode, expected in (("include", True), ("exclude", False)):
            self.voice_only.metadata = self._structured_mode(mode)
            with self.assertNumQueries(0):
                self.assertEqual(character_visual_reference_decision(self.voice_only).eligible, expected)
        self.voice_only.metadata = self._structured_mode("auto")
        with self.assertRaisesRegex(VisualReferencePolicyError, "主图设定无效"):
            character_visual_reference_decision(self.voice_only)
        self.voice_only.metadata = self._structured_mode("unknown")
        with self.assertNumQueries(0), self.assertRaises(VisualReferencePolicyError):
            character_visual_reference_decision(self.voice_only)

    def test_description_semantics_remain_and_appearance_does_not_redefine_identity(self):
        self.create_primary(self.visible_character, "Visible identity")
        for description in ("仅声音出场", "从不出镜", "voice-only caller", "never visible", "unseen narrator"):
            self.visible_character.description = description
            self.assertFalse(character_visual_reference_decision(self.visible_character).eligible)
        self.visible_character.description = ""
        setting = self.primary.revisions.get(revision=1)
        revise_variant(actor=self.user, variant=self.primary, expected_revision=1,
            value={**setting_payload(setting), "identity_anchor": "Visible identity", "appearance_anchor": "voice only"})
        self.assertTrue(character_visual_reference_decision(self.voice_only).eligible)

    def test_current_revision_changes_decision_without_mutating_frozen_snapshot(self):
        before = capture_consistency_snapshot(self.project)
        frozen = deepcopy(before)
        setting = self.primary.revisions.get(revision=1)
        review_variant(actor=self.user, variant=self.primary, expected_revision=1,
            expected_sha256=setting.content_sha256, decision="approve", comment="Isolated test")
        self.assertEqual(capture_consistency_snapshot(self.project), before)
        revise_variant(actor=self.user, variant=self.primary, expected_revision=1,
            value={**setting_payload(setting), "identity_anchor": "Visible companion"})
        self.assertTrue(character_visual_reference_decision(self.voice_only).eligible)
        self.assertEqual(before, frozen)
        self.assertEqual(setting.identity_anchor, "Unseen offscreen companion, voice only")

    def test_malformed_primary_never_becomes_absence_or_guessed_eligibility(self):
        for changes in ({"revision": 999}, {"revision": 1, "name": "Mismatched name"}):
            BibleVariant.objects.filter(pk=self.primary.pk).update(**changes)
            with self.assertRaises(VisualReferencePolicyError):
                character_visual_reference_decision(self.voice_only)
        BibleVariant.objects.filter(pk=self.primary.pk).update(revision=1, name="Primary")
        # Corruption fixture only, in the isolated test DB; production writes stay immutable.
        with connection.cursor() as cursor:
            cursor.execute("UPDATE studio_biblevariantrevision SET identity_anchor = %s WHERE variant_id = %s",
                ["Corrupted body", self.primary.pk.hex])
        with self.assertRaises(VisualReferencePolicyError):
            visual_reference_characters([self.visible_character, self.voice_only])

    def test_unrelated_broken_primary_is_not_read_and_projects_are_separately_scoped(self):
        (_user, other_project, other_character, *_rest) = build_story(username="other-visual-project")
        # Each project is read separately even when an internal caller supplies mixed rows.
        from studio.services.bible_setting_evidence import read_entity_primary_settings
        with patch("studio.services.bible_setting_evidence.read_entity_primary_settings",
                wraps=read_entity_primary_settings) as reader:
            decisions = character_visual_reference_decisions([self.voice_only, other_character])
        self.assertEqual({call.kwargs["project_id"] for call in reader.call_args_list}, {self.project.pk, other_project.pk})
        self.assertFalse(decisions[str(self.voice_only.pk)].eligible)
        self.assertTrue(decisions[str(other_character.pk)].eligible)
        BibleVariant.objects.filter(pk=self.primary.pk).update(revision=999)
        self.assertTrue(character_visual_reference_decision(other_character).eligible)

    def test_storyboard_reads_authored_roles_without_legacy_pool_or_global_eligibility(self):
        for shot in self.episode.shots.all():
            ShotCharacterRelation.objects.create(shot=shot, character=self.visible_character, role="visible", position=0)
            ShotCharacterRelation.objects.create(shot=shot,
                character=self.voice_only if shot.pk == self.shot.pk else self.visible_character, role="speaker", position=0)
        self.shot.dialogue[0]["voiceover"] = True
        self.shot.save(update_fields=["dialogue"])
        with CaptureQueriesContext(connection) as queries:
            content = storyboard_content(project_id=self.project.pk, episodes=[self.episode], include_references=False)
        for shot in content["episodes"][0]["shots"]:
            self.assertNotIn("character_ids", shot)
            self.assertNotIn("visual_reference_character_ids", shot)
            self.assertEqual(shot["character_relations"], [
                {"role": "speaker", "position": 0, "character_id": str(self.voice_only.pk if shot["id"] == str(self.shot.pk) else self.visible_character.pk)},
                {"role": "visible", "position": 0, "character_id": str(self.visible_character.pk)},
            ])
        self.assertEqual(sum('FROM "studio_biblevariant"' in q["sql"] for q in queries), 0)
        self.assertEqual(sum('FROM "studio_biblevariantrevision"' in q["sql"] for q in queries), 0)
        for query in queries:
            self.assertNotIn('"visual_identity"', query["sql"])
            self.assertNotIn('"studio_shot_characters"', query["sql"])
        # Source-setting validation belongs to material preparation, not content reads.
        BibleVariant.objects.filter(pk=self.primary.pk).update(revision=999)
        self.assertEqual(storyboard_content(project_id=self.project.pk, episodes=[self.episode], include_references=False), content)

    def voice_context(self):
        # Explicit already-resolved input: no project adapter, role lookup or automatic selection.
        entry = {**self.shot.dialogue[0], "voiceover": True, "subject": "",
            "voice_profile": "calm restrained voice"}
        return reference_context_fixture(ordered_units=(reference_unit_fixture(dialogue=(entry,),
            voice_profiles=("calm restrained voice",)),), mode="Ref2VA",
            conditioning_mode_contract=conditioning_contract("Ref2VA", input_kind="classified_references")["modes"][0])

    def render(self, context):
        return compile_reference_context(context, rendering_config()["primitive_options"]["reference_context_ir_v1"]["rendering"])

    def test_explicit_voiceover_does_not_read_current_primary_or_global_eligibility(self):
        context = self.voice_context()
        before = deepcopy(context)
        original_dialogue = deepcopy(self.shot.dialogue)
        original = self.render(context)
        BibleVariant.objects.filter(pk=self.primary.pk).update(revision=999)
        self.voice_only.metadata = self._structured_mode("include")
        self.voice_only.save(update_fields=("metadata",))
        with patch("studio.services.visual_reference_policy.character_visual_reference_decisions",
                side_effect=AssertionError("current eligibility lookup")), CaptureQueriesContext(connection) as queries:
            current = self.render(context)
        self.assertEqual(current, original)
        self.assertEqual(context, before)
        for query in queries:
            for table in ("studio_biblevariant", "studio_shot", "studio_characterbible"):
                self.assertNotIn(table, query["sql"])
        self.shot.refresh_from_db()
        self.assertEqual(self.shot.dialogue, original_dialogue)
        self.assertFalse(GenerationAttempt.objects.exists())
        self.assertFalse(ExecutionPreparation.objects.exists())

    def test_incomplete_preverified_map_is_not_treated_as_missing_primary(self):
        with self.assertNumQueries(0), self.assertRaisesRegex(VisualReferencePolicyError, "缺少已核验"):
            character_visual_reference_decisions([self.voice_only], primary_settings={})

    def test_explicit_voice_only_dialogue_does_not_create_visual_references(self):
        context = self.voice_context()
        text = str(self.render(context)["fields"])
        for fact in ("Off-screen voiceover", "(S2)", "calm restrained voice",
                     "with a warning", "<d>[Chinese] 别碰它，它在呼吸。</d>"):
            self.assertIn(fact, text)
        self.assertNotIn("Unseen offscreen companion, voice only", text)
        self.assertNotIn("[Character S2]", text)
        self.assertNotIn("corresponding visible on-screen character", text)
        self.assertEqual(context.reference_bindings, ())
        image_context = replace(context, prompt_type="image", mode="", include_audio=False,
            conditioning_mode_contract=None, frame_policy=None,
            ordered_units=(replace(context.ordered_units[0], dialogue=(), voice_profiles=()),))
        image_text = str(self.render(image_context)["fields"])
        self.assertNotIn("Unseen offscreen companion, voice only", image_text)
        self.assertNotIn("别碰它", image_text)
        self.assertEqual(image_context.reference_bindings, ())
