"""The original Job endpoint creates exact preparations without external calls."""
from copy import deepcopy
from unittest.mock import patch

from django.core.exceptions import ValidationError
from django.test import TestCase, override_settings
from django.db import connection
from django.test.utils import CaptureQueriesContext
from rest_framework.test import APIClient

from studio.models import AssetGenerationJob, ExecutionPreparation
from studio.services.asset_generation import asset_generation_options
from studio.services.execution_instances import clone_execution_draft, publish_execution_version
from studio.services.execution_definitions import build_execution_definition
from studio.services.asset_generation import create_asset_generation_job
from studio.tests import test_execution_preparation_creation as manual_fixtures
from studio.tests import test_execution_profile_preparation as profile_fixtures


@override_settings(AI_PROVIDER_ALLOWED_HOSTS=("api.example.com", "cdn.example.com"))
class ExecutionAssetJobTests(TestCase):
    setUpTestData = classmethod(manual_fixtures.ExecutionPreparationCreationTests.setUpTestData.__func__)
    write_contract = staticmethod(manual_fixtures.ExecutionPreparationCreationTests.write_contract)
    job_row = manual_fixtures.ExecutionPreparationCreationTests.job_row
    resolve = manual_fixtures.ExecutionPreparationCreationTests.resolve

    def setUp(self):
        manual_fixtures.ExecutionPreparationCreationTests.setUp(self)
        self.body = {"execution_instance_version_id": str(self.version.pk), "expected_definition_sha256": self.definition.sha256,
            "job_type": "asset", "media_type": "video", "name": "Prepared video", "field_values": [], **self.intent.value}
        self.client = APIClient()
        self.client.force_authenticate(self.actor)

    def create(self, **changes):
        return create_asset_generation_job(owner=self.actor, client_idempotency_key="one-asset-request", **{**self.body, **changes})

    def test_real_root_and_preparation_are_atomic_and_not_claimed_or_approved(self):
        with patch("studio.services.execution_parameter_preparation.randbelow", return_value=2) as seed:
            job, created = self.create()
            replay, new = self.create()
        self.assertTrue(created)
        self.assertFalse(new)
        self.assertEqual(job.pk, replay.pk)
        seed.assert_called_once()
        prep = job.execution_preparations.get()
        self.assertEqual(job.execution_instance_version_id, prep.execution_instance_version_id)
        self.assertEqual(prep.contract.resolved_inputs["parameters"]["generated_values"], {"seed": 10})
        self.assertEqual((job.status, prep.status), ("preparing", "preparing"))
        self.assertIsNone(prep.request_frozen_at)
        self.assertIsNone(job.claim_token)
        self.assertIsNone(job.estimated_cost)
        self.assertFalse(prep.field_values.exists())
        self.assertNotIn("payload", job.request_snapshot)
        self.assertNotIn("parameters", job.request_snapshot)

    def test_invalid_input_or_initial_field_rolls_back_both_records(self):
        before = (AssetGenerationJob.objects.count(), ExecutionPreparation.objects.count())
        for changes in ({"parameters": {"undeclared": 1}},
                {"field_values": [{"unit_key": "asset", "field_key": "no-profile", "text": "Invalid"}]},
                {"inputs": {"unknown": {"kind": "literal", "value": "hidden"}}}):
            with self.subTest(changes=changes), self.assertRaises(ValidationError):
                self.create(**changes)
            self.assertEqual((AssetGenerationJob.objects.count(), ExecutionPreparation.objects.count()), before)

    def test_conflict_does_not_change_seed_or_use_current_target(self):
        first, _ = self.create()
        with self.assertRaises(ValidationError) as caught:
            self.create(name="Another intent")
        self.assertEqual(caught.exception.code, "execution_preparation_conflict")
        config = deepcopy(self.provider.driver_config)
        config["poll_path"] = "/changed/{request_id}"
        self.write_contract(self.provider, "driver_config", config)
        self.assertEqual(self.create()[0].pk, first.pk)

    def test_formal_api_returns_202_location_then_idempotent_200_and_409(self):
        path = "/api/v1/asset-generation-jobs/"
        response = self.client.post(path, self.body, format="json", HTTP_IDEMPOTENCY_KEY="api-asset")
        self.assertEqual(response.status_code, 202, response.data)
        self.assertIn(response.data["id"], response["Location"])
        self.assertEqual(response.data["preparation"]["status"], "preparing")
        self.assertEqual(response.json()["execution_instance_version"], str(self.version.pk))
        self.assertNotIn("prompt", response.data)
        # A lost response replays the original intent, even when its selected
        # instance and transport have since been disabled. It is not a new run.
        self.instance.is_active = False
        self.instance.save(update_fields=["is_active"])
        self.version.deployment.status = "disabled"
        self.version.deployment.save(update_fields=["status"])
        replay = self.client.post(path, self.body, format="json", HTTP_IDEMPOTENCY_KEY="api-asset")
        self.assertEqual(replay.status_code, 200, replay.data)
        self.assertEqual(replay.data["id"], response.data["id"])
        conflict = self.client.post(path, {**self.body, "name": "Different"}, format="json", HTTP_IDEMPOTENCY_KEY="api-asset")
        self.assertEqual(conflict.status_code, 409, conflict.data)

    def test_old_selectors_fake_approvals_and_missing_idempotency_are_rejected(self):
        for extra in ({"model_deployment_id": str(self.deployment.pk)}, {"workflow_template_version_id": str(self.version.pk)},
                {"parent_preparation": str(self.version.pk)}, {"scope": "resource"}, {"allow_external_calls": "true"},
                {"job_type": "field"}, {"media_type": "text"}):
            with self.subTest(extra=extra):
                response = self.client.post("/api/v1/asset-generation-jobs/", {**self.body, **extra}, format="json", HTTP_IDEMPOTENCY_KEY="invalid")
                self.assertEqual(response.status_code, 400, response.data)
        response = self.client.post("/api/v1/asset-generation-jobs/", self.body, format="json")
        self.assertEqual(response.status_code, 400, response.data)

    def test_cancel_api_replay_keeps_first_deadline_and_frozen_request_without_worker_claim(self):
        from studio.services.asset_generation import claim_next_media_asset_job
        from studio.tests.test_execution_asset_requests import freeze

        job, _ = self.create()
        preparation = freeze(job, self.actor)
        request = deepcopy(preparation.contract.prepared_request)
        path = f"/api/v1/asset-generation-jobs/{job.pk}/cancel/"
        first = self.client.post(path, {}, format="json")
        self.assertEqual(first.status_code, 200, first.data)
        job.refresh_from_db()
        before = (job.cancel_requested_at, job.finished_at)
        self.assertIsNotNone(before[0])
        replay = self.client.post(path, {}, format="json")
        self.assertEqual(replay.status_code, 200, replay.data)
        self.assertEqual((first.data["status"], replay.data["status"]), ("cancelled", "cancelled"))
        self.assertTrue(replay.data["ignore_late_result"])
        job.refresh_from_db()
        self.assertEqual((job.cancel_requested_at, job.finished_at), before)
        preparation.refresh_from_db()
        self.assertEqual(preparation.status, "cancelled")
        self.assertEqual(preparation.contract.prepared_request, request)
        self.assertIsNone(claim_next_media_asset_job("cancelled-api-root"))

    def test_options_page_exact_instances_without_smoke_defaults_or_full_contracts(self):
        later = clone_execution_draft(source_id=self.version.pk, expected_revision=self.version.revision,
            expected_definition_sha256=self.definition.sha256, actor=self.actor)
        contract = build_execution_definition(later.pk)
        publish_execution_version(version_id=later.pk, expected_revision=later.revision,
            expected_definition_sha256=contract.sha256, actor=self.actor)
        self.assertIn("studio_modelexecutioninstanceversion", asset_generation_options(user=self.actor, job_type="asset", media_type="video").explain())
        with CaptureQueriesContext(connection) as queries:
            response = self.client.get("/api/v1/asset-generation-jobs/options/", {"job_type": "asset", "media_type": "video", "page_size": 1})
        self.assertEqual(response.status_code, 200, response.data)
        self.assertEqual(response.data["count"], 2)
        self.assertEqual(len(response.data["results"]), 1)
        self.assertEqual(response.data["results"][0]["execution_instance_version_id"], str(later.pk))
        self.assertNotIn("routes", response.data)
        self.assertNotIn("parameter_defaults", response.data["results"][0])
        self.assertNotIn("can_submit", response.data["results"][0])
        # The catalog query filters the frozen output kind but does not SELECT
        # the definition/graph JSON into every option row.
        instance_queries = [q["sql"] for q in queries if 'FROM "studio_modelexecutioninstanceversion"' in q["sql"]]
        self.assertEqual(len(instance_queries), 2)  # count + bounded page
        self.assertTrue(all('"definition"' not in q.split(" FROM ")[0] for q in instance_queries))
        second = self.client.get(response.data["next"])
        self.assertEqual(second.data["results"][0]["execution_instance_version_id"], str(self.version.pk))

    def test_member_can_read_published_input_contract_but_not_manage_registry(self):
        self.actor.is_staff = False
        self.actor.save(update_fields=["is_staff"])
        self.client.force_authenticate(self.actor)
        path = f"/api/v1/model-execution-instance-versions/{self.version.pk}/"
        response = self.client.get(path + "input-contract/")
        self.assertEqual(response.status_code, 200, response.data)
        self.assertEqual(response.data["sha256"], self.definition.sha256)
        self.assertEqual(response.data["subject_categories"], [
            {"value": "character", "label": "人物"}, {"value": "scene", "label": "场景"},
            {"value": "prop", "label": "道具"},
        ])
        self.assertEqual(self.client.get(path).status_code, 403)
        self.instance.is_active = False
        self.instance.save(update_fields=["is_active"])
        self.assertEqual(self.client.get(path + "input-contract/").status_code, 404)


@override_settings(AI_PROVIDER_ALLOWED_HOSTS=("api.example.com", "cdn.example.com"))
class ExecutionProfileAssetJobTests(TestCase):
    setUpTestData = classmethod(profile_fixtures.ExecutionProfilePreparationTests.setUpTestData.__func__)
    write_contract = staticmethod(profile_fixtures.ExecutionProfilePreparationTests.write_contract)
    input = staticmethod(profile_fixtures.ExecutionProfilePreparationTests.input)
    bind = profile_fixtures.ExecutionProfilePreparationTests.bind

    def setUp(self):
        profile_fixtures.ExecutionProfilePreparationTests.setUp(self)

    def create(self, fields):
        return create_asset_generation_job(owner=self.actor, execution_instance_version_id=self.version.pk,
            expected_definition_sha256=self.definition.sha256, job_type="asset", media_type="video", name="Field-based video",
            client_idempotency_key="profile-asset", field_values=fields, **profile_fixtures.request_for(self))[0]

    def test_missing_fields_is_a_persisted_wait_not_a_creation_error(self):
        job = self.create([])
        preparation = job.execution_preparations.get()
        self.assertEqual((job.status, preparation.status), ("preparing", "awaiting_input"))
        self.assertFalse(preparation.field_values.exists())
        self.assertIsNone(preparation.next_check_at)

    def test_initial_manual_fields_remain_pending_without_auxiliary_model_calls(self):
        job = self.create([{"unit_key": "asset", "field_key": "composite_direction", "text": "A courier opens a blue parcel."}])
        preparation = job.execution_preparations.get()
        self.assertEqual(preparation.status, "awaiting_review")
        self.assertEqual(preparation.field_values.get().status, "pending")
        self.assertFalse(preparation.field_selections.exists())
        self.assertFalse(preparation.auxiliary_asset_jobs.exists())

    def test_trial_missing_then_pending_field_stays_unqueued_after_worker_scan_and_get(self):
        from studio.models import GenerationAttempt, GenerationTask, ModelDeploymentSmokeAttempt, WorkflowSmokeAttempt
        from studio.services.execution_preparation_queue import advance_next_execution_preparation

        counts = lambda: tuple(model.objects.count() for model in
            (GenerationAttempt, GenerationTask, ModelDeploymentSmokeAttempt, WorkflowSmokeAttempt))
        before = counts()
        job = self.create([])
        self.assertFalse(job.allow_external_calls)
        preparation = job.execution_preparations.get()
        self.assertEqual(preparation.status, "awaiting_input")
        self.assertIsNone(preparation.next_check_at)
        self.assertIsNone(advance_next_execution_preparation())
        client = APIClient()
        client.force_authenticate(self.actor)
        text = "A courier carefully opens the blue parcel in soft daylight."
        response = client.post("/api/v1/execution-field-values/", {
            "preparation_id": str(preparation.pk), "unit_key": "asset", "field_key": "composite_direction",
            "text": text, "revision": 1, "expected_preparation_revision": preparation.revision}, format="json")
        self.assertEqual(response.status_code, 201, response.data)
        # Saving an unconfirmed value makes the root due. The real scanner
        # must stop at review even with a valid auxiliary binding available.
        preparation.refresh_from_db()
        self.assertIsNotNone(preparation.next_check_at)
        with patch("studio.services.execution_advancement.finalize_execution",
                side_effect=AssertionError("Pending fields cannot finalize")) as finalize, \
                patch("studio.services.execution_advancement.dispatch_asset_execution",
                side_effect=AssertionError("Trial cannot dispatch")) as dispatch:
            scanned = advance_next_execution_preparation()
            self.assertEqual(scanned.pk, preparation.pk)
            self.assertEqual(scanned.status, "awaiting_review")
            self.assertIsNone(scanned.next_check_at)
            self.assertIsNone(advance_next_execution_preparation())
            basis = scanned.revision
            for _ in range(2):
                base = f"/api/v1/execution-preparations/{scanned.pk}/"
                preview = client.get(base + "prompt-display/", {"expected_preparation_revision": basis})
                self.assertEqual(preview.status_code, 200, preview.data)
                self.assertEqual(preview.data["mode"], "preview")
                self.assertEqual({row["origin"] for row in preview.data["field_basis"]}, {"candidate"})
                self.assertIn(text, next(row["text"] for row in preview.data["channels"] if row["key"] == "positive_prompt"))
                metrics = client.get(base + "storage-metrics/", {"expected_preparation_revision": basis})
                self.assertEqual(metrics.status_code, 200, metrics.data)
                stored = next(row for row in metrics.data["items"] if row["key"] == "prepared_request")
                self.assertEqual(stored["state"], "not_generated")
                self.assertIsNone(stored["sha256"])
            finalize.assert_not_called()
            dispatch.assert_not_called()
        preparation.refresh_from_db()
        job.refresh_from_db()
        self.assertEqual(preparation.revision, basis)
        self.assertEqual(job.status, "preparing")
        self.assertIsNone(job.submission_started_at)
        self.assertIsNone(preparation.request_frozen_at)
        self.assertFalse(preparation.field_selections.exists())
        self.assertFalse(preparation.auxiliary_asset_jobs.exists())
        self.assertEqual(preparation.field_values.get().status, "pending")
        self.assertEqual(counts(), before)
