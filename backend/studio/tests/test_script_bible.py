from __future__ import annotations

from django.contrib.auth import get_user_model
from django.test import TestCase
from django.urls import Resolver404, resolve
from rest_framework.test import APIClient

from studio.models import CharacterBible, PropBible, SceneBible, SeriesProject


def master_script(*, heading: bool = True) -> str:
    """Explicit historical text for retired-route and provenance protection tests."""
    return (
        "【SCRIPT_BIBLE_V1】\n"
        "【人物】王强｜大学新闻社学生｜college reporter in a navy jacket\n"
        "【场景】大学图书馆｜闭馆前的安静阅览室｜quiet university library under warm lamps\n"
        "【道具】红伞｜雨夜留下的关键物证｜bright red umbrella with a worn wooden handle\n"
        "【/SCRIPT_BIBLE_V1】\n\n"
        + ("第1集：雨夜相遇\n" if heading else "")
        + "镜头1【时长=6秒】【首帧连续=否】【尾帧连续=否】：大学图书馆\n"
        "[0-3秒] 动作：王强在门边捡起红伞。\n"
        "[3-6秒] 王强（softly）：这不是我的伞。"
    )


class ScriptBibleProductionServiceTests(TestCase):
    def setUp(self):
        self.user = get_user_model().objects.create_user(
            username="script-bible-owner",
            password="StrongPass123!",
        )
        self.other = get_user_model().objects.create_user(
            username="script-bible-other",
            password="StrongPass123!",
        )
        self.project = SeriesProject.objects.create(language="Chinese", aspect_ratio="9:16",
            owner=self.user,
            title="雨夜大学",
            width=768, height=1344, fps=24, default_shot_duration_ms=5000,
            continuity_seed=1,
            master_script=master_script(),
        )
        self.client = APIClient()
        self.client.force_authenticate(self.user)

    def test_retired_candidate_preview_has_no_route_or_database_side_effects(self):
        url = f"/api/v1/projects/{self.project.pk}/script-bible-candidates/"
        with self.assertRaises(Resolver404):
            resolve(url)
        other_client = APIClient()
        other_client.force_authenticate(self.other)
        for client in (self.client, other_client, APIClient()):
            for endpoint in (url, url + "apply/"):
                for method in (client.get, client.post):
                    with self.subTest(endpoint=endpoint, method=method.__name__), self.assertNumQueries(0):
                        self.assertEqual(method(endpoint).status_code, 404)
        self.assertFalse(CharacterBible.objects.exists())
        self.assertFalse(SceneBible.objects.exists())
        self.assertFalse(PropBible.objects.exists())
        self.project.refresh_from_db()
        self.assertEqual(self.project.master_script, master_script())

    def test_reserved_provenance_cannot_be_forged_or_removed_by_crud(self):
        forged = self.client.post("/api/v1/characters/", {
            "project": str(self.project.id),
            "slug": "forged",
            "name": "伪造人物",
            "metadata": {"script_bible_v1": {"schema_version": 1}},
        }, format="json")
        self.assertEqual(forged.status_code, 400, forged.data)

        # Preserved historical fact, not an invocation of the retired writer.
        saved = CharacterBible.objects.create(project=self.project, slug="historical", name="旧记录",
            metadata={"script_bible_v1": {"schema_version": 1}})
        removed = self.client.patch(
            f"/api/v1/characters/{saved.id}/",
            {"metadata": {}},
            format="json",
        )
        self.assertEqual(removed.status_code, 400, removed.data)
        saved.refresh_from_db()
        self.assertIn("script_bible_v1", saved.metadata)
