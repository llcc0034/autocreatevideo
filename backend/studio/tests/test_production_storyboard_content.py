"""Shared creative snapshots don't select an AI route or infer missing facts."""
from copy import deepcopy
from uuid import uuid4

from django.contrib.auth import get_user_model
from django.core.exceptions import ValidationError
from django.db import connection
from django.test import TestCase
from django.test.utils import CaptureQueriesContext

from studio.models import CharacterBible, Episode, PropBible, SceneBible, SeriesProject, Shot, ShotCharacterRelation, ShotReferenceUse
from studio.services.json_contracts import json_contract
from studio.services.production_storyboard_content import storyboard_content


class ProductionStoryboardContentTests(TestCase):
    def setUp(self):
        actor = get_user_model().objects.create_user(username="storyboard-content")
        self.project = SeriesProject.objects.create(owner=actor, title="已确认事实", language="Chinese", aspect_ratio="9:16",
            width=768, height=1344, fps=24, default_shot_duration_ms=6000, continuity_seed=1)
        self.character = CharacterBible.objects.create(project=self.project, slug="speaker", name="王强", stable_speaker_id="S1")
        self.episode = Episode.objects.create(project=self.project, number=1, title="正式分镜")
        self.scene = SceneBible.objects.create(project=self.project, slug="room", name="房间")
        self.prop = PropBible.objects.create(project=self.project, slug="umbrella", name="伞")
        self.shot = Shot.objects.create(episode=self.episode, number=1, scene=self.scene,
            duration_ms=6000, media_type="image", include_audio=False, conditioning_mode="",
            requires_first_frame_continuity=False, requires_last_frame_continuity=False)
        ShotCharacterRelation.objects.create(shot=self.shot, character=self.character, role="visible", position=0)
        self.shot.props.set([self.prop])

    def read(self, episodes=None):
        return storyboard_content(project_id=self.project.pk, episodes=[self.episode] if episodes is None else episodes)

    def test_exact_creative_facts_are_shared_without_route_and_without_filler(self):
        value = self.read()
        shot = value["episodes"][0]["shots"][0]
        self.assertEqual(shot["id"], str(self.shot.pk))
        self.assertEqual(shot["character_relations"], [{"role": "visible", "position": 0, "character_id": str(self.character.pk)}])
        self.assertNotIn("character_ids", shot)
        self.assertNotIn("visual_reference_character_ids", shot)
        self.assertEqual(shot["prop_ids"], [str(self.prop.pk)])
        self.assertEqual(shot["scene_id"], str(self.scene.pk))
        self.assertNotIn("conditioning_mode_capability", shot)
        self.assertNotIn("status", value["episodes"][0])
        for key in ("narrative", "camera", "action", "music", "sound_effects", "detailed_description", "continuity_notes"):
            self.assertNotIn(key, shot)
        before = deepcopy(value)
        Episode.objects.filter(pk=self.episode.pk).update(status="completed")
        self.assertEqual(json_contract(self.read()).sha256, json_contract(before).sha256)
        Shot.objects.filter(pk=self.shot.pk).update(title="人工明确修改后的标题")
        self.assertNotEqual(json_contract(self.read()).sha256, json_contract(before).sha256)
        self.assertEqual(before["episodes"][0]["shots"][0]["title"], "")

    def test_missing_relocated_or_duplicate_episode_is_not_silently_skipped(self):
        with self.assertRaises(ValidationError):
            self.read([self.episode, self.episode])
        with self.assertRaises(ValidationError):
            storyboard_content(project_id=uuid4(), episodes=[self.episode])
        Episode.objects.filter(pk=self.episode.pk).delete()
        with self.assertRaises(ValidationError):
            self.read()

    def test_related_content_reads_do_not_grow_per_shot(self):
        with CaptureQueriesContext(connection) as first:
            self.read()
        for number in range(2, 5):
            shot = Shot.objects.create(episode=self.episode, number=number, duration_ms=6000,
                media_type="image", include_audio=False, requires_first_frame_continuity=False, requires_last_frame_continuity=False)
            ShotCharacterRelation.objects.create(shot=shot, character=self.character, role="visible", position=0)
            shot.props.set([self.prop])
        with CaptureQueriesContext(connection) as second:
            result = self.read()
        self.assertEqual(len(result["episodes"][0]["shots"]), 4)
        self.assertEqual(len(first), len(second))
        self.assertEqual(len(second), 7)  # Episodes, shots, props, source/role/use and revision metadata batches.
        self.assertFalse(any('studio_shot_characters' in row['sql'] or 'studio_biblevariantrevision' in row['sql'] for row in second))

    def test_creative_projection_does_not_query_downstream_reference_bindings(self):
        self.assertNotIn("reference_bindings", self.read()["episodes"][0]["shots"][0])
        with CaptureQueriesContext(connection) as queries:
            value = storyboard_content(project_id=self.project.pk, episodes=[self.episode], include_references=False)
        self.assertEqual(len(queries), 7)
        self.assertFalse(any("reference_bindings" in row["sql"] or "reference_assets" in row["sql"] for row in queries))
        self.assertNotIn("reference_bindings", value["episodes"][0]["shots"][0])
        self.assertNotIn("reference_asset_ids", value["episodes"][0]["shots"][0])

    def test_voice_only_original_dialogue_and_millisecond_interval_stay_independent_of_old_pool(self):
        voice = CharacterBible.objects.create(project=self.project, slug="voice", name="画外发声", stable_speaker_id="S2")
        ShotCharacterRelation.objects.create(shot=self.shot, character=voice, role="speaker", position=0)
        original = [{"speaker_id": "S2", "text": "  原对白，不改写。 ", "voiceover": True, "start_ms": 125, "end_ms": 901}]
        Shot.objects.filter(pk=self.shot.pk).update(dialogue=original, episode_start_ms=1234, episode_end_ms=7234)
        before = self.read()
        with CaptureQueriesContext(connection) as queries:
            value = self.read()
        row = value["episodes"][0]["shots"][0]
        self.assertEqual(before, value)
        self.assertEqual(row["dialogue"], original)
        self.assertEqual((row["episode_start_ms"], row["episode_end_ms"], row["duration_ms"]), (1234, 7234, 6000))
        self.assertEqual([item["character_id"] for item in row["character_relations"] if item["role"] == "visible"], [str(self.character.pk)])
        self.assertFalse(any('studio_shot_characters' in item['sql'] or 'studio_shot_reference_assets' in item['sql'] for item in queries))
        self.assertFalse(any(item['sql'].lstrip().upper().startswith(('UPDATE ', 'INSERT ', 'DELETE ')) for item in queries))
        ShotCharacterRelation.objects.filter(shot=self.shot, role="speaker").delete()
        with self.assertRaisesMessage(ValidationError, "逐行发言者关系不完整"):
            self.read()

    def test_creative_adoption_excludes_only_selection_not_use_declaration(self):
        use = ShotReferenceUse.objects.create(shot=self.shot, use_key="coat", position=0, media_kind="image", purpose="本镜外观")
        creative = storyboard_content(project_id=self.project.pk, episodes=[self.episode], include_references=False)
        selected = self.read()
        self.assertEqual(creative["episodes"][0]["shots"][0]["reference_uses"], [{"use_key": "coat", "position": 0,
            "media_kind": "image", "purpose": "本镜外观", "setting_revision_id": None}])
        self.assertIn("source_mode", selected["episodes"][0]["shots"][0]["reference_uses"][0])
        use.purpose = "另一创作说明"; use.save(update_fields=("purpose",))
        self.assertNotEqual(creative, storyboard_content(project_id=self.project.pk, episodes=[self.episode], include_references=False))

    def test_retired_content_is_not_read_or_hashed_into_new_approval(self):
        before = self.read()
        Shot.objects.filter(pk=self.shot.pk).update(metadata={"framing": "旧构图", "negative_prompt": "旧词"})
        with CaptureQueriesContext(connection) as queries:
            after = self.read()
        self.assertEqual(before, after)
        for field in ("narrative", "camera", "action", "music", "sound_effects", "detailed_description", "continuity_notes"):
            self.assertFalse(any(f'"studio_shot"."{field}"' in row["sql"] for row in queries))
