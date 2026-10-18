"""Version navigation uses current Shot relations, not retired M2M prefetches."""
from django.db import connection, transaction
from django.test import TestCase
from django.test.utils import CaptureQueriesContext

from studio.models import Shot
from studio.services.shot_versions import (
    ShotVersionResolutionError, adjacent_version_shots, resolve_episode_shot_version,
)
from studio.tests.factories import build_story


class ShotVersionRelationTests(TestCase):
    def setUp(self):
        _, _, _, _, self.episode, self.first, self.second = build_story("version-current-relations")
        Shot.objects.filter(episode=self.episode).update(metadata={"creative_version_key": "authored-v1"})
        self.first.refresh_from_db(); self.second.refresh_from_db()

    def test_read_and_locked_paths_keep_order_and_original_content(self):
        before = list(Shot.objects.order_by("pk").values())
        for lock in (False, True):
            with self.subTest(lock=lock), transaction.atomic(), CaptureQueriesContext(connection) as queries:
                result = resolve_episode_shot_version(self.episode, "authored-v1", for_update=lock)
                self.assertEqual([row.pk for row in result.shots], [self.first.pk, self.second.pk])
                self.assertEqual(result.shots[0].dialogue, self.first.dialogue)
            self.assertFalse(any("studio_shot_characters" in row["sql"] or "studio_shot_reference_assets" in row["sql"] for row in queries))
        self.assertEqual(adjacent_version_shots(self.first), (None, self.second))
        self.assertEqual(adjacent_version_shots(self.second), (self.first, None))
        self.assertEqual(list(Shot.objects.order_by("pk").values()), before)

    def test_missing_or_non_contiguous_versions_still_fail_explicitly(self):
        with self.assertRaises(ShotVersionResolutionError):
            resolve_episode_shot_version(self.episode, "not-present")
        Shot.objects.filter(pk=self.second.pk).update(metadata={"creative_version_key": "authored-v1", "creative_version_shot_number": 3})
        with self.assertRaises(ShotVersionResolutionError):
            resolve_episode_shot_version(self.episode, "authored-v1")
