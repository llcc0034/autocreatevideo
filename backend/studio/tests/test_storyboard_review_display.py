"""Isolated read projection, never a name-based relationship repair."""
from copy import deepcopy
from uuid import uuid4

from django.contrib.auth import get_user_model
from django.core.exceptions import ValidationError
from django.db import connection
from django.test import TestCase
from django.test.utils import CaptureQueriesContext

from studio.models import CharacterBible, PropBible, SceneBible, SeriesProject
from studio.services.storyboard_review_display import storyboard_review_display


class StoryboardReviewDisplayTests(TestCase):
    def setUp(self):
        self.user = get_user_model().objects.create_user(username="isolated-relations")
        self.project = SeriesProject.objects.create(owner=self.user, title="isolated", language="zh",
            aspect_ratio="16:9", width=640, height=360, fps=24, default_shot_duration_ms=6000, continuity_seed=1)
        self.visible = CharacterBible.objects.create(project=self.project, slug="visible", name="旅人", stable_speaker_id="visible")
        self.speaker = CharacterBible.objects.create(project=self.project, slug="speaker", name="画外音", stable_speaker_id="speaker")
        self.scene = SceneBible.objects.create(project=self.project, slug="scene", name="雨中车站")
        self.prop = PropBible.objects.create(project=self.project, slug="prop", name="雨伞")
        self.shot = {"id": str(uuid4()), "number": 2, "title": "相遇", "scene_id": str(self.scene.pk),
            "character_relations": [{"role": "speaker", "position": 2, "character_id": str(self.speaker.pk)},
                {"role": "visible", "position": 0, "character_id": str(self.visible.pk)},
                {"role": "speaker", "position": 0, "character_id": str(self.speaker.pk)}], "prop_ids": [str(self.prop.pk)],
            "character_ids": [str(self.speaker.pk)]}  # Old mixed list must not become visibility.
        self.canonical = {"episodes": [{"id": str(uuid4()), "number": 2, "title": "第二集", "shots": [self.shot]}]}

    def read(self, value=None):
        return storyboard_review_display(project_id=self.project.pk, canonical=value or self.canonical)

    def test_orders_original_numbers_and_keeps_visibility_separate_from_speech_without_mutation(self):
        earlier = {**self.shot, "id": str(uuid4()), "number": 1}
        self.canonical["episodes"][0]["shots"].append(earlier)
        first_episode = {"id": str(uuid4()), "number": 1, "title": "第一集",
            "shots": [{**self.shot, "id": str(uuid4()), "number": 3}]}
        self.canonical["episodes"].append(first_episode)
        before = deepcopy(self.canonical)
        result = self.read()
        self.assertEqual([(row["episode_number"], row["number"]) for row in result["shots"]], [(1, 3), (2, 1), (2, 2)])
        row = result["shots"][0]
        self.assertEqual(row["scene"]["name"], "雨中车站")
        self.assertEqual(row["props"][0]["name"], "雨伞")
        self.assertEqual([(c["role"], c["position"], c["entity"]["name"]) for c in row["characters"]],
            [("speaker", 0, "画外音"), ("speaker", 2, "画外音"), ("visible", 0, "旅人")])
        self.assertEqual(self.canonical, before)

    def test_current_name_is_explicit_and_missing_or_foreign_id_never_uses_matching_name(self):
        before = deepcopy(self.canonical)
        self.visible.name = "旅人新名称"; self.visible.save(update_fields=("name",))
        result = self.read()
        self.assertEqual(result["name_basis"], "current_bible_by_id")
        self.assertEqual(result["shots"][0]["characters"][-1]["entity"]["name"], "旅人新名称")
        missing = str(uuid4())
        self.shot["scene_id"] = missing
        self.assertEqual(self.read()["shots"][0]["scene"], {"id": missing, "name": None, "status": "unavailable"})
        with self.assertNumQueries(3):
            result = storyboard_review_display(project_id=uuid4(), canonical=before)
        self.assertEqual(result["shots"][0]["characters"][-1]["entity"]["status"], "unavailable")
        self.assertIsNone(result["shots"][0]["props"][0]["name"])

    def test_missing_typed_relation_is_an_error_not_a_legacy_or_name_fallback(self):
        for changes in ({"character_relations": None}, {"character_relations": {}}, {"scene_id": "bad"},
                {"number": True}, {"prop_ids": [str(self.prop.pk)] * 2},
                {"character_relations": [{"role": "inferred", "position": 0, "character_id": str(self.visible.pk)}]}):
            value = deepcopy(self.canonical); value["episodes"][0]["shots"][0].update(changes)
            with self.assertRaises(ValidationError):
                self.read(value)
        value = deepcopy(self.canonical); del value["episodes"][0]["shots"][0]["character_relations"]
        with self.assertRaises(ValidationError):
            self.read(value)

    def test_names_are_three_narrow_indexed_queries_for_two_hundred_shots(self):
        self.canonical["episodes"][0]["shots"] = [{**self.shot, "id": str(uuid4()), "number": i + 1} for i in range(200)]
        with CaptureQueriesContext(connection) as queries:
            result = self.read()
        self.assertEqual(len(result["shots"]), 200)
        self.assertEqual(len(queries), 3)
        plans = []
        with connection.cursor() as cursor:
            for query in queries:
                sql = query["sql"]
                self.assertTrue(sql.startswith("SELECT "))
                self.assertNotIn("description", sql); self.assertNotIn("metadata", sql)
                self.assertNotIn("ORDER BY", sql)
                cursor.execute("EXPLAIN QUERY PLAN " + sql)
                plan = " ".join(str(row) for row in cursor.fetchall())
                self.assertIn("USING INDEX", plan); self.assertNotIn("TEMP B-TREE", plan)
                plans.append(plan)
        print("Isolated storyboard label EXPLAIN:", plans)
