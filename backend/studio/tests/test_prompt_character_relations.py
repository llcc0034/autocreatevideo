"""Authored roles through the formal reviewed Shot/Bible input path.

The retired-style current-ORM prompt adapters are deliberately not exercised.
Snapshot batching is covered by test_production_storyboard_content; renderer
freeze/round-trip is covered by test_reference_context_facts.
"""
from copy import deepcopy
from unittest.mock import patch

from django.core.exceptions import ValidationError
from django.db import connection
from django.test import TestCase
from django.test.utils import CaptureQueriesContext

from studio.models import ExecutionPreparation, GenerationAttempt, ShotCharacterRelation
from studio.services.json_contracts import json_contract, verify_json_contract
from studio.tests import test_recipe_linked_context as linked_fixtures


class PromptCharacterRelationTests(TestCase):
    setUp = linked_fixtures.LinkedContextTests.setUp
    approve = linked_fixtures.LinkedContextTests.approve
    reapprove = linked_fixtures.LinkedContextTests.reapprove
    definition = linked_fixtures.LinkedContextTests.definition
    rule = linked_fixtures.LinkedContextTests.rule
    bind = linked_fixtures.LinkedContextTests.bind
    resolve = linked_fixtures.LinkedContextTests.resolve

    def author_dialogue(self):
        original = [
            {"speaker_id": "S2", "text": "  原文\n不得拼改。  ", "delivery": " softly ",
             "voiceover": True, "start_ms": 123, "end_ms": 456},
            {"speaker_id": "S1", "text": "second", "delivery": "", "voiceover": False,
             "start_ms": 457, "end_ms": 700},
            {"speaker_id": "S2", "text": "  原文\n不得拼改。  ", "delivery": " softly ",
             "voiceover": True, "start_ms": 701, "end_ms": 987},
        ]
        self.shot.dialogue = deepcopy(original)
        self.shot.episode_start_ms, self.shot.episode_end_ms = 1200, 2200
        self.shot.save(update_fields=("dialogue", "episode_start_ms", "episode_end_ms"))
        ShotCharacterRelation.objects.create(shot=self.shot, character=self.voice, role="speaker", position=2)
        self.reapprove()
        return original

    def test_reviewed_mapping_preserves_raw_dialogue_positions_and_roles_without_writes(self):
        original = self.author_dialogue()
        template = {"kind": "object", "fields": {"dialogue": self.rule(),
            "linked": self.rule("linked_bible")}}
        with patch("studio.services.visual_reference_policy.character_visual_reference_decisions",
                side_effect=AssertionError("global eligibility cannot rewrite authored roles")), CaptureQueriesContext(connection) as queries:
            resolved = self.resolve(self.bind(template))
        value = resolved["logical_values"]["brief"]
        for index, entry in enumerate(original):
            for key, text in entry.items():
                self.assertEqual(value["dialogue"][index][key], text)
        self.assertEqual([row["voice_profile"] for row in value["dialogue"]], ["轻柔高音", "沉稳低音", "轻柔高音"])
        self.assertEqual([row["character_id"] for row in value["linked"]["character_relations"] if row["role"] == "visible"],
            [str(self.first.pk)])
        self.assertEqual([row["character_id"] for row in value["linked"]["character_relations"] if row["role"] == "speaker"],
            [str(self.voice.pk), str(self.first.pk), str(self.voice.pk)])
        self.assertFalse(any(row["sql"].lstrip().upper().startswith(("INSERT", "UPDATE", "DELETE")) for row in queries))
        self.shot.refresh_from_db()
        self.assertEqual(self.shot.dialogue, original)
        self.assertEqual((self.shot.episode_start_ms, self.shot.episode_end_ms), (1200, 2200))
        self.assertFalse(ExecutionPreparation.objects.exists())
        self.assertFalse(GenerationAttempt.objects.exists())

    def test_same_speaker_text_reordering_cannot_reselect_a_character(self):
        original = self.author_dialogue()
        self.shot.dialogue = [original[1], original[0], original[2]]
        self.shot.save(update_fields=("dialogue",))
        self.reapprove()
        with self.assertRaisesMessage(ValidationError, "确切发言者不一致"):
            self.resolve(self.bind())
        # Repair explicit positions, not a name/voice-profile search.
        ShotCharacterRelation.objects.filter(shot=self.shot, role="speaker", position=0).update(character=self.first)
        ShotCharacterRelation.objects.filter(shot=self.shot, role="speaker", position=1).update(character=self.voice)
        self.reapprove()
        rows = self.resolve(self.bind())["logical_values"]["brief"]
        self.assertEqual([row["voice_profile"] for row in rows], ["沉稳低音", "轻柔高音", "轻柔高音"])

    def test_changed_or_missing_relation_rejects_original_review_instead_of_reusing_it(self):
        self.author_dialogue()
        bound = self.bind()
        before = deepcopy(bound)
        ShotCharacterRelation.objects.filter(shot=self.shot, role="speaker", position=0).update(character=self.first)
        with self.assertRaises(ValidationError):
            self.resolve(bound)
        self.assertEqual(bound, before)
        self.reapprove()
        with self.assertRaisesMessage(ValidationError, "确切发言者不一致"):
            self.resolve(self.bind())
        ShotCharacterRelation.objects.filter(shot=self.shot, role="speaker", position=0).delete()
        with self.assertRaisesMessage(ValidationError, "逐行发言者关系不完整"):
            self.reapprove()
        self.assertFalse(ExecutionPreparation.objects.exists())
        self.assertFalse(GenerationAttempt.objects.exists())

    def test_resolved_fact_snapshot_stays_exact_after_current_relation_changes(self):
        original = self.author_dialogue()
        bound = self.bind()
        resolved = self.resolve(bound)
        frozen = json_contract(resolved)
        before = deepcopy(frozen.value)
        ShotCharacterRelation.objects.filter(shot=self.shot, role="speaker").delete()
        with self.assertNumQueries(0):
            verify_json_contract(frozen.value, schema_version=frozen.schema_version, size_bytes=frozen.size_bytes,
                sha256=frozen.sha256, max_bytes=1024 * 1024)
        for index, row in enumerate(original):
            for key, value in row.items():
                self.assertEqual(frozen.value["logical_values"]["brief"][index][key], value)
        self.assertEqual(frozen.value, before)
        with self.assertRaises(ValidationError):
            self.resolve(bound)
