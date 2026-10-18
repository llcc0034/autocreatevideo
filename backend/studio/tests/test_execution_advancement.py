"""Formal continue action over real roots; GET cannot start preparation."""
from django.contrib.auth import get_user_model
from django.test import TestCase, override_settings
from rest_framework.test import APIClient

from studio.models import AssetGenerationJob
from studio.tests import test_execution_asset_admission as fixtures


@override_settings(AI_PROVIDER_ALLOWED_HOSTS=("api.example.com",))
class ExecutionAdvancementApiTests(TestCase):
    setUpTestData = fixtures.ExecutionAssetAdmissionTests.__dict__["setUpTestData"]
    version = fixtures.ExecutionAssetAdmissionTests.version
    attempt = fixtures.ExecutionAssetAdmissionTests.attempt
    finish = fixtures.ExecutionAssetAdmissionTests.finish
    passed_fixture = fixtures.ExecutionAssetAdmissionTests.passed_fixture

    def setUp(self):
        fixtures.ExecutionAssetAdmissionTests.setUp(self)
        self.client = APIClient()
        self.client.force_authenticate(self.actor)
        self.path = f"/api/v1/execution-preparations/{self.prep.pk}/"

    def advance(self, revision=None, **extra):
        self.prep.refresh_from_db()
        return self.client.post(self.path + "continue/", {
            "expected_preparation_revision": self.prep.revision if revision is None else revision, **extra}, format="json")

    def test_read_does_not_advance_but_formal_continue_freezes_and_queues_once(self):
        self.passed_fixture()
        initial = self.client.get(self.path)
        self.assertEqual(initial.status_code, 200, initial.data)
        self.assertIsNone(initial.data["prepared_request"])
        self.assertEqual(initial.data["status"], "preparing")
        result = self.advance()
        self.assertEqual(result.status_code, 200, result.data)
        self.assertEqual(result.data["status"], "dispatched")
        self.assertIsNotNone(result.data["prepared_request"])
        self.job.refresh_from_db()
        self.assertEqual(self.job.status, "queued")
        self.assertIsNone(self.job.submission_started_at)
        self.assertEqual(self.advance().data["request_sha256"], result.data["request_sha256"])
        self.assertEqual(AssetGenerationJob.objects.count(), 1)

    def test_blocked_continue_returns_actual_evaluation_without_fake_dispatch(self):
        result = self.advance()
        self.assertEqual(result.status_code, 200, result.data)
        self.assertEqual(result.data["status"], "blocked")
        self.assertEqual(result.data["reason_code"], "execution_smoke_missing")
        self.assertIsNone(result.data["prepared_request"])
        self.job.refresh_from_db()
        self.assertEqual(self.job.status, "preparing")

    def test_stale_revision_and_client_supplied_gate_fields_are_rejected(self):
        self.passed_fixture()
        old = self.prep.revision
        self.assertEqual(self.advance().status_code, 200)
        self.assertEqual(self.advance(old).status_code, 409)
        for key in ("skip_checks", "smoke_facts", "scope", "approve_fields", "allow_external_calls"):
            with self.subTest(key=key):
                self.assertEqual(self.advance(**{key: True}).status_code, 400)
        self.assertEqual(self.advance(True).status_code, 400)

    def test_other_owner_cannot_read_or_advance_preparation(self):
        other = get_user_model().objects.create_user(username="other-preparation-owner")
        self.client.force_authenticate(other)
        self.assertEqual(self.client.get(self.path).status_code, 404)
        self.assertEqual(self.advance().status_code, 404)
