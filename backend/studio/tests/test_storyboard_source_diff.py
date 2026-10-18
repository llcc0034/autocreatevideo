"""Pure, bounded preview differences. No DB, file, service or API mutations."""
from copy import deepcopy

from django.core.exceptions import ValidationError
from django.test import SimpleTestCase

from studio.services.script_storyboard_plan import StoryboardSourcePlan, compare_storyboard_source_plans


class StoryboardSourceDiffTests(SimpleTestCase):
    def setUp(self):
        self.plan = StoryboardSourcePlan(basis={"schema_version": 2, "script": {"id": "exact-revision"}},
            episodes=[{"number": 9, "shots": [{"number": 8, "relations": {
                "scene": None, "visible_characters": [], "props": [], "speaker": {"entity_id": "offscreen"},
                "uses": [{"use_key": "clothes", "setting_revision_id": "uniform"},
                    {"use_key": "movement", "setting_revision_id": "uniform"}]}}]}], provenance=[])

    def diff(self, current, **kwargs):
        return compare_storyboard_source_plans(self.plan, current, max_bytes=kwargs.get("max_bytes", 100_000))

    def test_equal_preview_has_no_changes_or_mutation(self):
        original = deepcopy(self.plan)
        self.assertEqual(self.diff(deepcopy(self.plan)), [])
        self.assertEqual(self.plan, original)

    def test_explicit_empty_visibility_and_exact_setting_change_keep_speaker_separate(self):
        current = deepcopy(self.plan)
        current.episodes[0]["shots"][0]["relations"]["uses"][0]["setting_revision_id"] = "raincoat"
        changes = self.diff(current)
        self.assertEqual(len(changes), 1)
        self.assertEqual((changes[0]["scope"], changes[0]["episode"], changes[0]["shot"], changes[0]["field"]),
            ("relations", 9, 8, "uses"))
        self.assertEqual(changes[0]["before"][0]["setting_revision_id"], "uniform")
        self.assertEqual(changes[0]["after"][0]["setting_revision_id"], "raincoat")
        self.assertEqual(current.episodes[0]["shots"][0]["relations"]["visible_characters"], [])
        changes[0]["after"][0]["setting_revision_id"] = "caller-edit"
        self.assertEqual(current.episodes[0]["shots"][0]["relations"]["uses"][0]["setting_revision_id"], "raincoat")

    def test_source_identity_difference_is_not_silently_rebound(self):
        current = deepcopy(self.plan)
        current.basis["script"]["id"] = "another-revision"
        self.assertEqual(self.diff(current), [{"scope": "basis", "field": "script",
            "before": {"id": "exact-revision"}, "after": {"id": "another-revision"}}])

    def test_added_removed_and_reordered_coordinates_are_not_matched_by_array_index(self):
        current = deepcopy(self.plan)
        current.episodes[0]["shots"][0]["number"] = 2
        changes = self.diff(current)
        self.assertEqual([row["field"] for row in changes], ["shot_order", "shot", "shot"])
        self.assertIsNone(changes[1]["after"])
        self.assertIsNone(changes[2]["before"])
        self.plan.episodes.append({"number": 1, "shots": [deepcopy(self.plan.episodes[0]["shots"][0])]})
        current = deepcopy(self.plan)
        current.episodes.reverse()
        changes = self.diff(current)
        self.assertEqual(changes, [{"scope": "relations", "field": "shot_order",
            "before": [(9, 8), (1, 8)], "after": [(1, 8), (9, 8)]}])

    def test_use_order_and_removed_visible_relation_are_not_normalized_away(self):
        self.plan.episodes[0]["shots"][0]["relations"]["visible_characters"] = [{"entity_id": "visible"}]
        current = deepcopy(self.plan)
        relations = current.episodes[0]["shots"][0]["relations"]
        relations["visible_characters"] = []
        relations["uses"].reverse()
        self.assertEqual([row["field"] for row in self.diff(current)], ["visible_characters", "uses"])

    def test_oversized_difference_fails_without_truncating_or_mutating(self):
        current = deepcopy(self.plan)
        current.basis["script"]["id"] = "changed"
        with self.assertRaises(ValidationError): self.diff(current, max_bytes=1)
        self.assertEqual(self.plan.basis["script"]["id"], "exact-revision")
