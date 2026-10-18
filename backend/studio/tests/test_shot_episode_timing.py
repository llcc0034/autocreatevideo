from unittest.mock import patch

from django.contrib.auth import get_user_model
from django.db import IntegrityError, transaction
from django.test import TestCase
from rest_framework.test import APIClient

from studio.models import Episode, SeriesProject, Shot
from studio.services.shot_timing import validate_sequential_episode_timing


class ShotEpisodeTimingTests(TestCase):
    def setUp(self):
        self.actor = get_user_model().objects.create_user(username="shot-time", password="Test-password-1")
        self.project = SeriesProject.objects.create(owner=self.actor, title="时间合同", language="Chinese", aspect_ratio="9:16",
            width=768, height=1344, fps=24, default_shot_duration_ms=6000, continuity_seed=1)
        self.episode = Episode.objects.create(project=self.project, number=1, title="第一集")
        self.client = APIClient()
        self.client.force_authenticate(self.actor)
        # Only test configuration, never a model/provider or an API success stub.
        for target, value in (("_resolved_video_shot_duration_policy", {"minimum_duration_ms":1000,"maximum_duration_ms":10000,"default_duration_ms":6000}),
                ("_resolved_project_creation_policy", {"default_shot_media_type":"image","default_include_audio":False})):
            context = patch(f"studio.serializers.{target}", return_value=value)
            context.start(); self.addCleanup(context.stop)

    def test_formal_api_interval_derives_duration_and_rejects_conflicting_edits(self):
        response = self.client.post("/api/v1/shots/", {"episode":str(self.episode.pk),"number":1,
            "media_type":"image","episode_start_ms":45000,"episode_end_ms":50000}, format="json")
        self.assertEqual(response.status_code, 201, response.data)
        self.assertEqual(response.data["duration_ms"], 5000)
        url = f"/api/v1/shots/{response.data['id']}/"
        bad = self.client.patch(url, {"duration_ms":6000}, format="json")
        self.assertEqual(bad.status_code, 400, bad.data)
        self.assertEqual(Shot.objects.get().duration_ms, 5000)
        changed = self.client.patch(url, {"episode_end_ms":51000}, format="json")
        self.assertEqual(changed.status_code, 200, changed.data)
        self.assertEqual(changed.data["duration_ms"], 6000)
        duration_only = self.client.patch(url, {"episode_start_ms":None,"episode_end_ms":None,"duration_ms":2500}, format="json")
        self.assertEqual(duration_only.status_code, 200, duration_only.data)
        self.assertEqual(duration_only.data["duration_ms"], 2500)
        self.assertIsNone(duration_only.data["episode_start_ms"])

    def test_invalid_pair_is_blocked_by_api_and_database(self):
        for start, end, duration in ((None,5000,5000),(5000,None,5000),(5000,4000,1000),(0,5000,6000)):
            with self.subTest(start=start,end=end):
                response = self.client.post("/api/v1/shots/", {"episode":str(self.episode.pk),"number":1,
                    "media_type":"image","episode_start_ms":start,"episode_end_ms":end,"duration_ms":duration},format="json")
                self.assertEqual(response.status_code,400,response.data)
                with self.assertRaises(IntegrityError), transaction.atomic():
                    Shot.objects.create(episode=self.episode,number=1,media_type="image",duration_ms=duration,include_audio=False,
                        episode_start_ms=start,episode_end_ms=end)
        self.assertFalse(Shot.objects.exists())

    def test_assembly_does_not_treat_episode_gaps_as_source_video_offsets(self):
        shots = [{"number":1,"duration_ms":45000,"episode_start_ms":None,"episode_end_ms":None},
            {"number":2,"duration_ms":5000,"episode_start_ms":45000,"episode_end_ms":50000}]
        validate_sequential_episode_timing(shots)
        with self.assertRaisesMessage(ValueError,"前序镜头结束于 0ms"):
            validate_sequential_episode_timing(shots[1:])
