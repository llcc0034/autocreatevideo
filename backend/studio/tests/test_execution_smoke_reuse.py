"""Reuse explicit source approvals and resolved inputs, not another AI call."""
from copy import deepcopy
from unittest.mock import patch

from django.core.exceptions import PermissionDenied, ValidationError
from django.test import TestCase, override_settings
from rest_framework.test import APIClient

from studio.models import ModelDeploymentSmokeAttempt, ExecutionPreparation
from studio.services.execution_preparation_fields import select_execution_field
from studio.services.execution_smoke import create_instance_smoke
from studio.tests import test_execution_asset_retries as fixtures


@override_settings(AI_PROVIDER_ALLOWED_HOSTS=("api.example.com", "cdn.example.com"))
class ExecutionSmokeReuseTests(TestCase):
    setUpTestData = fixtures.ExecutionProfileAssetRetryTests.__dict__["setUpTestData"]
    write_contract = staticmethod(fixtures.ExecutionProfileAssetRetryTests.write_contract)
    input = staticmethod(fixtures.ExecutionProfileAssetRetryTests.input)
    bind = fixtures.ExecutionProfileAssetRetryTests.bind
    create = fixtures.ExecutionProfileAssetRetryTests.create
    field = fixtures.ExecutionProfileAssetRetryTests.field
    approve = fixtures.ExecutionProfileAssetRetryTests.approve

    def setUp(self):
        fixtures.ExecutionProfileAssetRetryTests.setUp(self)
        self.root = self.create([])
        self.prep = self.root.execution_preparations.get()
        self.chosen = self.approve(self.prep, self.field(self.prep, "A courier opens a blue parcel.", 1))
        self.prep.refresh_from_db()
        select_execution_field(preparation_id=self.prep.pk, source_preparation_id=self.prep.pk, field_value_id=self.chosen.pk,
            expected_preparation_revision=self.prep.revision, expected_selection_revision=None, replace=False, actor=self.actor)
        self.newer = self.approve(self.prep, self.field(self.prep, "A courier leaves a different parcel.", 2))
        self.prep.refresh_from_db()
        self.fixture = deepcopy(self.prep.contract.intent["request"]["requested"])
        self.fields = [{"source_preparation_id": str(self.prep.pk), "field_value_id": str(self.chosen.pk),
            "unit_key": "asset", "field_key": "composite_direction"}]
        self.body = {"expected_definition_sha256": self.definition.sha256, "fixture": self.fixture,
            "field_values": [], "allow_external_calls": True,
            "reuse_preparation_id": str(self.prep.pk), "expected_reuse_revision": self.prep.revision,
            "field_selections": self.fields}

    def smoke(self, **changes):
        return create_instance_smoke(version_id=self.version.pk, client_idempotency_key="reuse-smoke", actor=self.actor, **{**self.body, **changes})

    def test_reuses_selected_approval_not_newest_and_never_redraws_seed_or_invokes_leaf(self):
        original = deepcopy(self.prep.contract.resolved_inputs)
        with patch("studio.services.execution_smoke.resolve_execution_request_facts", side_effect=AssertionError("No re-resolve")):
            attempt, target, created = self.smoke()
        self.assertTrue(created)
        self.assertEqual(attempt.status, "preparing")
        self.assertEqual(target.unit_key, "asset")
        self.assertEqual(target.contract.resolved_inputs, original)
        self.assertEqual(target.field_selections.get().field_value_id, self.chosen.pk)
        self.assertEqual(target.field_selections.get().field_value.reviewed_at, self.chosen.reviewed_at)
        self.assertFalse(target.field_values.exists())
        self.assertFalse(target.auxiliary_asset_jobs.exists())
        self.assertIsNone(attempt.submission_started_at)
        self.assertIsNone(target.request_frozen_at)

    def test_exact_replay_does_not_depend_on_later_source_revision(self):
        attempt, target, _ = self.smoke()
        self.field(self.prep, "Unselected pending new text.", 3)
        replay, same, created = self.smoke()
        self.assertFalse(created)
        self.assertEqual((replay.pk, same.pk), (attempt.pk, target.pk))
        with self.assertRaises(ValidationError):
            self.smoke(field_selections=[])

    def test_changed_fixture_unselected_revision_duplicate_and_stale_source_are_atomic_rejections(self):
        count = (ModelDeploymentSmokeAttempt.objects.count(), ExecutionPreparation.objects.count())
        newer = {**self.fields[0], "field_value_id": str(self.newer.pk)}
        altered = deepcopy(self.fixture)
        altered["source_language"] = "changed"
        for changes in ({"fixture": altered}, {"field_selections": [newer]}, {"field_selections": self.fields * 2},
                {"expected_reuse_revision": self.prep.revision - 1}, {"field_values": [{"unit_key": "asset", "field_key": "composite_direction", "text": "changed"}]}):
            with self.subTest(changes=changes), self.assertRaises(ValidationError):
                self.smoke(**changes)
            self.assertEqual((ModelDeploymentSmokeAttempt.objects.count(), ExecutionPreparation.objects.count()), count)

    def test_reuse_obeys_existing_global_staff_permission_and_rejects_disabled_staff(self):
        from django.contrib.auth import get_user_model
        other = get_user_model().objects.create_user(username="unrelated-smoke-operator", is_staff=True)
        # Registry/Preparation management already grants staff global access;
        # reusing a field must not introduce a contradictory permission model.
        attempt, target, created = create_instance_smoke(version_id=self.version.pk, client_idempotency_key="other-staff", actor=other, **self.body)
        self.assertTrue(created)
        self.assertEqual(target.created_by_id, other.pk)
        self.assertEqual(target.field_selections.get().field_value_id, self.chosen.pk)
        other.is_active = False
        other.save(update_fields=["is_active"])
        with self.assertRaises(PermissionDenied):
            create_instance_smoke(version_id=self.version.pk, client_idempotency_key="foreign-owner", actor=other, **self.body)

    def test_formal_api_accepts_explicit_reuse_and_returns_original_preparation_link(self):
        client = APIClient()
        client.force_authenticate(self.actor)
        response = client.post(f"/api/v1/model-execution-instance-versions/{self.version.pk}/smoke-test/", self.body,
            format="json", HTTP_IDEMPOTENCY_KEY="reuse-from-ui")
        self.assertEqual(response.status_code, 202, response.data)
        self.assertEqual(response.data["preparation"]["unit_key"], "asset")
        target = ExecutionPreparation.objects.get(pk=response.data["preparation"]["id"])
        self.assertEqual(target.field_selections.get().field_value_id, self.chosen.pk)

    def test_fixture_read_exports_only_explicit_choice_without_authorizing_or_creating_a_call(self):
        client = APIClient()
        client.force_authenticate(self.actor)
        url = f"/api/v1/execution-preparations/{self.prep.pk}/smoke-fixture/"
        counts = (ExecutionPreparation.objects.count(), ModelDeploymentSmokeAttempt.objects.count())
        response = client.get(url, {"expected_preparation_revision": self.prep.revision})
        self.assertEqual(response.status_code, 200, response.data)
        self.assertEqual(response.json()["fixture"], self.fixture)
        self.assertEqual(response.json()["field_selections"], self.fields)
        self.assertNotIn("allow_external_calls", response.data)
        self.assertNotIn("budget_limit", response.data)
        self.assertEqual((ExecutionPreparation.objects.count(), ModelDeploymentSmokeAttempt.objects.count()), counts)
        self.assertEqual(client.get(url, {"expected_preparation_revision": self.prep.revision - 1}).status_code, 409)
        self.assertEqual(client.get(url, {"expected_preparation_revision": self.prep.revision, "scope": "resource"}).status_code, 400)
        self.assertEqual(client.get(url + '?expected_preparation_revision=1&expected_preparation_revision=2').status_code, 400)
        for revision in ("true", "1.0", "1e1", "-1", "0", "01", "2147483648"):
            self.assertEqual(client.get(url, {"expected_preparation_revision": revision}).status_code, 400)
        self.actor.is_staff = False
        self.actor.save(update_fields=["is_staff"])
        self.assertEqual(client.get(url, {"expected_preparation_revision": self.prep.revision}).status_code, 403)
