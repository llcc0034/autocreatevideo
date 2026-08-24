from django.test import TestCase
from rest_framework.test import APIClient

from studio.models import PromptArtifact, Shot
from studio.tests.factories import build_story


class SingleShotPromptScopeTests(TestCase):
    def setUp(self):
        (
            self.user,
            self.project,
            self.character,
            self.scene,
            self.episode,
            self.shot1,
            self.shot2,
        ) = build_story(username="single-shot-prompt-scope")
        self.client = APIClient()
        self.client.force_authenticate(self.user)

    def _persist_invalid_sibling_dialogue(self) -> list[dict]:
        """Represent legacy data that predates the API speaker-binding guard."""
        invalid_dialogue = [{"speaker_id": "S4", "text": "Hand over the sword."}]
        Shot.objects.filter(pk=self.shot2.pk).update(dialogue=invalid_dialogue)
        self.shot2.refresh_from_db()
        return invalid_dialogue

    def test_invalid_sibling_does_not_block_current_shot_patch(self):
        invalid_dialogue = self._persist_invalid_sibling_dialogue()

        patch_response = self.client.patch(
            f"/api/v1/shots/{self.shot1.id}/",
            {
                "title": "Current shot saved independently",
                "duration_ms": 5000,
                "dialogue": self.shot1.dialogue,
                "characters": [str(self.character.id)],
            },
            format="json",
        )

        self.assertEqual(patch_response.status_code, 200, patch_response.data)
        self.shot1.refresh_from_db()
        self.assertEqual(self.shot1.duration_ms, 5000)
        self.assertEqual(self.shot1.title, "Current shot saved independently")
        self.shot2.refresh_from_db()
        self.assertEqual(self.shot2.dialogue, invalid_dialogue)

    def test_current_shot_unbound_speaker_is_rejected_without_writing(self):
        original_title = self.shot1.title
        original_duration_ms = self.shot1.duration_ms
        original_dialogue = self.shot1.dialogue

        response = self.client.patch(
            f"/api/v1/shots/{self.shot1.id}/",
            {
                "title": "Must not be persisted",
                "duration_ms": 5000,
                "dialogue": [{"speaker_id": "S4", "text": "Invalid current line."}],
                "characters": [str(self.character.id)],
            },
            format="json",
        )

        self.assertEqual(response.status_code, 400, response.data)
        self.assertIn("S4", str(response.data))
        self.shot1.refresh_from_db()
        self.assertEqual(self.shot1.title, original_title)
        self.assertEqual(self.shot1.duration_ms, original_duration_ms)
        self.assertEqual(self.shot1.dialogue, original_dialogue)
        self.assertEqual(PromptArtifact.objects.count(), 0)
