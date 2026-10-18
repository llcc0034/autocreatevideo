"""Public receipt contract using isolated stored candidates, not real media."""
from unittest.mock import patch

from django.test import TestCase
from rest_framework.test import APIClient

from studio.models import ArtifactSelectionDecision, ProductionManualCommand
from studio.tests.test_artifact_selection_receipts import SelectionFixture


class ArtifactSelectionReceiptAPITests(SelectionFixture, TestCase):
    def setUp(self):
        super().setUp()
        self.client = APIClient()
        self.client.force_authenticate(self.actor)
        self.url = f"/api/v1/production-runs/{self.run.pk}/select-artifact/"

    def submit(self, artifact=None, key="choose-a", **extra):
        headers = {} if key is None else {"HTTP_IDEMPOTENCY_KEY": key}
        return self.client.post(self.url, {"artifact_id": str((artifact or self.first).pk), **extra},
            format="json", **headers)

    def test_receipt_replay_keeps_current_choice_and_reuses_exact_command(self):
        first = self.submit()
        self.assertEqual(first.status_code, 200, first.data)
        self.assertEqual(set(first.data), {"id", "action", "created_at", "result_kind", "target_id"})
        self.assertEqual(first["Cache-Control"], "private, no-store")
        self.assertEqual((first.data["action"], first.data["result_kind"]), ("select", "artifact_selection"))
        second = self.submit(self.second, "choose-b")
        self.assertEqual(second.status_code, 200, second.data)
        with patch("studio.services.production_runs.select_run_artifact", side_effect=AssertionError("no replay write")):
            replay = self.submit()
        self.assertEqual(replay.status_code, 200, replay.data)
        self.assertEqual(first.data, replay.data)
        self.assertNotEqual(first.data["target_id"], second.data["target_id"])
        current = self.client.get(f"/api/v1/generated-artifacts/{self.second.pk}/")
        self.assertEqual(current.status_code, 200, current.data)
        self.assertEqual(current.data["status"], "selected")
        self.first.refresh_from_db()
        self.assertEqual(self.first.status, "available")
        self.assertEqual((ProductionManualCommand.objects.count(), ArtifactSelectionDecision.objects.count()), (2, 2))

    def test_new_key_same_candidate_remains_new_intent(self):
        first, second = self.submit(), self.submit(key="new-intent")
        self.assertEqual((first.status_code, second.status_code), (200, 200))
        self.assertNotEqual(first.data["id"], second.data["id"])
        self.assertNotEqual(first.data["target_id"], second.data["target_id"])

    def test_missing_blank_or_oversized_key_is_rejected_without_writes(self):
        for key in (None, "", " ", "x" * 192):
            with self.subTest(key=key):
                response = self.submit(key=key)
                self.assertEqual(response.status_code, 400, response.data)
        self.assertFalse(ProductionManualCommand.objects.exists())
        self.assertFalse(ArtifactSelectionDecision.objects.exists())

    def test_changed_body_conflicts_and_unauthorized_owner_cannot_replay(self):
        self.assertEqual(self.submit().status_code, 200)
        response = self.submit(self.second)
        self.assertEqual(response.status_code, 409, response.data)
        self.assertIn("run_plan_source_conflict", str(response.data))
        self.client.force_authenticate(self.other)
        for key in (None, "choose-a"):
            response = self.submit(self.second, key)
            self.assertEqual(response.status_code, 404, response.data)
        self.assertEqual((ProductionManualCommand.objects.count(), ArtifactSelectionDecision.objects.count()), (1, 1))

    def test_failed_candidate_and_untrusted_selection_mode_never_create_receipts(self):
        response = self.submit(selection_mode="automatic")
        self.assertEqual(response.status_code, 400, response.data)
        self.first.status = "invalid"
        self.first.save(update_fields=("status",))
        response = self.submit()
        self.assertEqual(response.status_code, 400, response.data)
        self.assertFalse(ProductionManualCommand.objects.exists())
        self.assertFalse(ArtifactSelectionDecision.objects.exists())

    def test_receipt_failure_rolls_back_and_original_key_can_be_retried(self):
        with patch.object(ProductionManualCommand, "save", side_effect=RuntimeError("isolated receipt failure")):
            with self.assertRaisesMessage(RuntimeError, "isolated receipt failure"):
                self.submit()
        self.assertFalse(ProductionManualCommand.objects.exists())
        self.assertFalse(ArtifactSelectionDecision.objects.exists())
        self.first.refresh_from_db()
        self.assertEqual(self.first.status, "available")
        response = self.submit()
        self.assertEqual(response.status_code, 200, response.data)
