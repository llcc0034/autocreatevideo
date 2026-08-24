from django.test import TestCase
from rest_framework.test import APIClient

from studio.tests.factories import build_story


class RetiredAssetBibleDraftSurfaceTests(TestCase):
    def setUp(self):
        user, self.project, _, _, _, _, _ = build_story()
        self.client = APIClient()
        self.client.force_authenticate(user)

    def test_asset_draft_generation_and_bible_snapshot_sync_are_not_public_routes(self):
        for route in (
            f"/api/v1/projects/{self.project.id}/bible-drafts/from-assets/",
            f"/api/v1/projects/{self.project.id}/sync-bibles/",
        ):
            with self.subTest(route=route):
                self.assertEqual(
                    self.client.post(route, {}, format="json").status_code,
                    404,
                )
