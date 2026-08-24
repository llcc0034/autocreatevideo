from django.test import TestCase
from rest_framework.test import APIClient

from studio.models import EpisodeCreativeDraft
from studio.tests.factories import build_story


class LegacyWriteSurfaceTests(TestCase):
    def setUp(self):
        user, self.project, _, _, self.episode, _, _ = build_story()
        self.client = APIClient()
        self.client.force_authenticate(user)

    def test_removed_legacy_generation_routes_return_not_found(self):
        routes = (
            "/api/v1/series-concept-drafts/generate/",
            f"/api/v1/projects/{self.project.id}/creative-drafts/generate/",
            f"/api/v1/projects/{self.project.id}/shot-detail-drafts/generate/",
            f"/api/v1/projects/{self.project.id}/asset-bible-draft/",
            f"/api/v1/projects/{self.project.id}/visual-rules-drafts/generate/",
            f"/api/v1/projects/{self.project.id}/visual-rules-drafts/apply/",
            f"/api/v1/projects/{self.project.id}/script-bible-candidates/apply/",
            f"/api/v1/projects/{self.project.id}/sync-bibles/",
            f"/api/v1/projects/{self.project.id}/breakdown-script/",
            f"/api/v1/episodes/{self.episode.id}/sync-shots/",
        )
        for route in routes:
            with self.subTest(route=route):
                response = self.client.post(route, {}, format="json")
                self.assertEqual(response.status_code, 404)

    def test_shared_history_routes_reject_post(self):
        for route in (
            f"/api/v1/projects/{self.project.id}/creative-drafts/",
            f"/api/v1/projects/{self.project.id}/shot-detail-drafts/",
            f"/api/v1/projects/{self.project.id}/shot-detail-draft-batches/",
            f"/api/v1/projects/{self.project.id}/bible-image-candidates/",
        ):
            with self.subTest(route=route):
                response = self.client.post(route, {}, format="json")
                self.assertEqual(response.status_code, 405)

    def test_creative_history_exposes_facts_without_live_actionability(self):
        draft = EpisodeCreativeDraft.objects.create(
            project=self.project,
            episode=self.episode,
            created_by=self.project.owner,
            context_hash="a" * 64,
            context_snapshot={"schema_version": 1},
        )

        response = self.client.get(
            f"/api/v1/projects/{self.project.id}/creative-drafts/",
        )

        self.assertEqual(response.status_code, 200, response.data)
        self.assertEqual([row["id"] for row in response.data], [str(draft.id)])
        self.assertTrue({
            "context_current", "can_edit", "can_regenerate", "can_confirm", "can_apply",
        }.isdisjoint(response.data[0]))

    def test_fixed_creative_provider_capability_is_removed(self):
        response = self.client.get("/api/v1/capabilities/creative-drafts/")

        self.assertEqual(response.status_code, 404)

    def test_low_level_task_assembly_and_release_mutations_are_unroutable(self):
        task_actions = (
            "retry",
            "cancel",
            "publish-output",
            "promote-output-to-asset",
            "sync-comfyui-workflow",
            "cancel-batch",
            "resume-batch",
            "delete-batch",
        )
        for action in task_actions:
            with self.subTest(action=action):
                response = self.client.post(
                    f"/api/v1/tasks/00000000-0000-0000-0000-000000000000/{action}/",
                    {},
                    format="json",
                )
                self.assertEqual(response.status_code, 404)

        self.assertEqual(
            self.client.delete(
                "/api/v1/tasks/00000000-0000-0000-0000-000000000000/",
            ).status_code,
            405,
        )

        self.assertEqual(
            self.client.post("/api/v1/assemblies/episode/", {}, format="json").status_code,
            405,
        )
        self.assertEqual(
            self.client.post("/api/v1/release-bundles/", {}, format="json").status_code,
            405,
        )
