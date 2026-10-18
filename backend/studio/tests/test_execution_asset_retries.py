"""Explicit retries preserve facts and provenance without approving or calling AI."""
import uuid
from copy import deepcopy
from unittest.mock import patch

from django.core.exceptions import ValidationError
from django.db import IntegrityError, transaction
from django.test import TestCase, override_settings

from studio.models import AssetGenerationJob, ExecutionPreparation
from studio.services.asset_generation import request_asset_generation_cancel, retry_asset_generation_job
from studio.services.execution_preparation_fields import create_manual_execution_field, review_execution_field, select_execution_field
from studio.services.execution_task_links import validate_execution_task_links
from studio.tests import test_execution_asset_jobs as fixtures
from studio.tests.test_execution_asset_requests import freeze


@override_settings(AI_PROVIDER_ALLOWED_HOSTS=("api.example.com", "cdn.example.com"))
class ExecutionAssetRetryTests(TestCase):
    setUpTestData = fixtures.ExecutionAssetJobTests.__dict__["setUpTestData"]
    write_contract = staticmethod(fixtures.ExecutionAssetJobTests.write_contract)
    job_row = fixtures.ExecutionAssetJobTests.job_row
    resolve = fixtures.ExecutionAssetJobTests.resolve
    create = fixtures.ExecutionAssetJobTests.create

    def setUp(self):
        fixtures.ExecutionAssetJobTests.setUp(self)

    def source(self):
        job, _ = self.create()
        freeze(job, self.actor)
        return request_asset_generation_cancel(job)

    def test_retry_keeps_resolved_seed_and_inputs_but_not_remote_request_or_claim(self):
        original = self.source()
        prep = original.execution_preparations.get()
        with patch("studio.services.execution_parameter_preparation.randbelow", side_effect=AssertionError("no new seed")):
            retry, created = retry_asset_generation_job(original, client_idempotency_key="explicit-retry")
        next_prep = retry.execution_preparations.get()
        self.assertTrue(created)
        self.assertEqual(retry.retry_of_id, original.pk)
        self.assertEqual(next_prep.contract.resolved_inputs, prep.contract.resolved_inputs)
        self.assertEqual(next_prep.contract.intent["request"]["requested"], prep.contract.intent["request"]["requested"])
        self.assertEqual(next_prep.request_key, "explicit-retry")
        self.assertNotEqual(next_prep.intent_sha256, prep.intent_sha256)
        self.assertNotEqual(next_prep.pk, prep.pk)
        self.assertEqual((retry.status, next_prep.status), ("preparing", "preparing"))
        self.assertIsNone(next_prep.request_frozen_at)
        self.assertIsNone(retry.claim_token)
        self.assertIsNone(retry.submission_started_at)
        self.assertIsNone(retry.estimated_cost)
        self.assertFalse(retry.provider_request_id)
        validate_execution_task_links(retry)

    def test_large_input_retry_preserves_complete_original_intent(self):
        text = "Complete source. " * 4500
        job, _ = self.create(subject_facts=[{"id": "11111111-1111-4111-8111-111111111111",
            "category": "character", "name": "Declared source", "description": text}])
        original = request_asset_generation_cancel(job)
        prep = original.execution_preparations.get()
        self.assertGreater(prep.contract.intent_size_bytes, 65536)
        retry, created = retry_asset_generation_job(original, client_idempotency_key="large-retry")
        self.assertTrue(created)
        target = retry.execution_preparations.get()
        self.assertEqual(target.contract.intent["request"]["requested"], prep.contract.intent["request"]["requested"])
        self.assertEqual(target.contract.resolved_inputs, prep.contract.resolved_inputs)
        self.assertIsNone(target.request_frozen_at)

    def test_replay_before_active_target_check_and_key_cannot_change_source(self):
        source = self.source()
        retry, _ = retry_asset_generation_job(source, client_idempotency_key="same-retry")
        self.instance.is_active = False
        self.instance.save(update_fields=("is_active",))
        replay, created = retry_asset_generation_job(source, client_idempotency_key="same-retry")
        self.assertEqual(replay.pk, retry.pk)
        self.assertFalse(created)
        retry.status = "cancelled"
        retry.save(update_fields=("status",))
        with self.assertRaisesMessage(ValidationError, "停用"):
            retry_asset_generation_job(retry, client_idempotency_key="new-retry")
        self.instance.is_active = True
        self.instance.save(update_fields=("is_active",))
        # A separate original root uses the public creation service, not a clone
        # with invented lineage. Override the test helper's fixed key explicitly.
        from studio.services.asset_generation import create_asset_generation_job
        second, _ = create_asset_generation_job(owner=self.actor, client_idempotency_key="another", **self.body)
        request_asset_generation_cancel(second)
        with self.assertRaises(ValidationError) as caught:
            retry_asset_generation_job(second, client_idempotency_key="same-retry")
        self.assertEqual(caught.exception.code, "execution_preparation_conflict")

    def test_nonterminal_or_uncertain_source_cannot_be_retried(self):
        job, _ = self.create()
        before = (AssetGenerationJob.objects.count(), ExecutionPreparation.objects.count())
        for state in ("preparing", "queued", "running", "cancel_requested", "uncertain", "succeeded"):
            AssetGenerationJob.objects.filter(pk=job.pk).update(status=state)
            with self.subTest(state=state), self.assertRaises(ValidationError):
                retry_asset_generation_job(job, client_idempotency_key="blocked")
            self.assertEqual((AssetGenerationJob.objects.count(), ExecutionPreparation.objects.count()), before)

    def test_active_retry_or_claim_blocks_new_intent_with_no_partial_root(self):
        source = self.source()
        AssetGenerationJob.objects.filter(pk=source.pk).update(claim_token=uuid.uuid4())
        with self.assertRaises(ValidationError):
            retry_asset_generation_job(source, client_idempotency_key="claimed")
        AssetGenerationJob.objects.filter(pk=source.pk).update(claim_token=None)
        retry, _ = retry_asset_generation_job(source, client_idempotency_key="first")
        for state in ("preparing", "uncertain"):
            AssetGenerationJob.objects.filter(pk=retry.pk).update(status=state)
            before = (AssetGenerationJob.objects.count(), ExecutionPreparation.objects.count())
            with self.subTest(state=state), self.assertRaises(ValidationError) as caught:
                retry_asset_generation_job(source, client_idempotency_key="second")
            self.assertEqual(caught.exception.code, "execution_preparation_conflict")
            self.assertEqual((AssetGenerationJob.objects.count(), ExecutionPreparation.objects.count()), before)

    def test_terminal_successor_blocks_branch_but_same_key_replays_original_retry(self):
        source = self.source()
        retry, _ = retry_asset_generation_job(source, client_idempotency_key="first-successor")
        before = (AssetGenerationJob.objects.count(), ExecutionPreparation.objects.count())
        path = f"/api/v1/asset-generation-jobs/{source.pk}/retry/"
        # Isolated status fixtures exercise the guard; no media success is fabricated.
        for state in ("succeeded", "failed", "cancelled"):
            with self.subTest(state=state):
                AssetGenerationJob.objects.filter(pk=retry.pk).update(status=state)
                replay = self.client.post(path, format="json", HTTP_IDEMPOTENCY_KEY="first-successor")
                self.assertEqual(replay.status_code, 200, replay.data)
                self.assertEqual(replay.data["id"], str(retry.pk))
                branch = self.client.post(path, format="json", HTTP_IDEMPOTENCY_KEY=f"branch-{state}")
                self.assertEqual(branch.status_code, 409, branch.data)
                self.assertEqual((AssetGenerationJob.objects.count(), ExecutionPreparation.objects.count()), before)

    def test_retry_failed_leaf_not_ancestor_with_active_or_successful_descendant(self):
        source = self.source()
        child, _ = retry_asset_generation_job(source, client_idempotency_key="child")
        request_asset_generation_cancel(child)
        # Retry the exact failed leaf, using the public service, not a copied root.
        AssetGenerationJob.objects.filter(pk=child.pk).update(status="failed")
        leaf, created = retry_asset_generation_job(child, client_idempotency_key="leaf")
        self.assertTrue(created)
        self.assertEqual(leaf.retry_of_id, child.pk)
        before = (AssetGenerationJob.objects.count(), ExecutionPreparation.objects.count())
        for state in ("preparing", "running", "uncertain", "succeeded"):
            with self.subTest(state=state):
                AssetGenerationJob.objects.filter(pk=leaf.pk).update(status=state)
                with self.assertRaises(ValidationError) as caught:
                    retry_asset_generation_job(source, client_idempotency_key=f"ancestor-{state}")
                self.assertEqual(caught.exception.code, "execution_preparation_conflict")
                replay, new = retry_asset_generation_job(child, client_idempotency_key="leaf")
                self.assertFalse(new)
                self.assertEqual(replay.pk, leaf.pk)
                self.assertEqual((AssetGenerationJob.objects.count(), ExecutionPreparation.objects.count()), before)

    def test_corrupt_header_or_frozen_intent_cannot_create_retry_roots(self):
        source = self.source()
        preparation = source.execution_preparations.get()
        before = (AssetGenerationJob.objects.count(), ExecutionPreparation.objects.count())
        # This is deliberate persisted corruption, not a second legacy reader.
        corrupt_header = {**source.request_snapshot, "workflow_input_request": {}}
        for changes in ({"request_snapshot_schema_version": 2}, {"request_snapshot": corrupt_header}):
            with self.subTest(changes=changes), transaction.atomic():
                AssetGenerationJob.objects.filter(pk=source.pk).update(**changes)
                response = self.client.post(f"/api/v1/asset-generation-jobs/{source.pk}/retry/",
                    {}, format="json", HTTP_IDEMPOTENCY_KEY="corrupt-source-retry")
                self.assertEqual(response.status_code, 400, response.data)
                self.assertEqual((AssetGenerationJob.objects.count(), ExecutionPreparation.objects.count()), before)
                transaction.set_rollback(True)
        stored = preparation.contract
        modified = deepcopy(stored.intent)
        modified["request"]["requested"]["parameters"]["seed"] = 999
        # Bypass the immutable write API only to model damaged persisted bytes.
        stored.__class__._base_manager.filter(pk=stored.pk).update(intent=modified)
        response = self.client.post(f"/api/v1/asset-generation-jobs/{source.pk}/retry/",
            {}, format="json", HTTP_IDEMPOTENCY_KEY="corrupt-preparation-retry")
        self.assertEqual(response.status_code, 400, response.data)
        self.assertEqual((AssetGenerationJob.objects.count(), ExecutionPreparation.objects.count()), before)

    def test_schema_rejects_self_retry_and_two_active_retries(self):
        source = self.source()
        retry, _ = retry_asset_generation_job(source, client_idempotency_key="first")
        with self.assertRaises(IntegrityError), transaction.atomic():
            AssetGenerationJob.objects.filter(pk=source.pk).update(retry_of=source)
        # A distinct normal root cannot become a second active retry either.
        from studio.services.asset_generation import create_asset_generation_job
        other, _ = create_asset_generation_job(owner=self.actor, client_idempotency_key="distinct-root", **self.body)
        with self.assertRaises(IntegrityError), transaction.atomic():
            AssetGenerationJob.objects.filter(pk=other.pk).update(retry_of=source)
        self.assertEqual(AssetGenerationJob.objects.get(pk=retry.pk).retry_of_id, source.pk)

    def test_retry_source_query_uses_nonnull_index_for_all_statuses(self):
        source = self.source()
        retry_asset_generation_job(source, client_idempotency_key="index-retry")
        query = source.retries.order_by().values("id", "status")
        self.assertIn("idx_asset_retry_source", query.explain())
        self.assertNotIn("request_snapshot", str(query.query).split(" FROM ")[0])
        from django.db import connection
        from django.test.utils import CaptureQueriesContext
        with CaptureQueriesContext(connection) as captured:
            self.assertTrue(source.retries.exists())
        sql = captured[0]["sql"]
        self.assertNotIn("request_snapshot", sql)
        self.assertIn("LIMIT 1", sql)
        with connection.cursor() as cursor:
            cursor.execute("EXPLAIN QUERY PLAN " + sql)
            self.assertIn("idx_asset_retry_source", str(cursor.fetchall()))

    def test_formal_retry_202_location_replay_and_conflict(self):
        source = self.source()
        path = f"/api/v1/asset-generation-jobs/{source.pk}/retry/"
        response = self.client.post(path, format="json", HTTP_IDEMPOTENCY_KEY="api-retry")
        self.assertEqual(response.status_code, 202, response.data)
        self.assertEqual(str(response.data["retry_of"]), str(source.pk))
        self.assertIn(response.data["id"], response["Location"])
        self.assertEqual(response.data["preparation"]["status"], "preparing")
        replay = self.client.post(path, format="json", HTTP_IDEMPOTENCY_KEY="api-retry")
        self.assertEqual(replay.status_code, 200, replay.data)
        self.assertEqual(replay.data["id"], response.data["id"])
        conflict = self.client.post(path, format="json", HTTP_IDEMPOTENCY_KEY="second-api-retry")
        self.assertEqual(conflict.status_code, 409, conflict.data)
        changed = self.client.post(path, {"parameters": {"seed": 22}}, format="json")
        self.assertEqual(changed.status_code, 400, changed.data)


@override_settings(AI_PROVIDER_ALLOWED_HOSTS=("api.example.com", "cdn.example.com"))
class ExecutionProfileAssetRetryTests(TestCase):
    setUpTestData = fixtures.ExecutionProfileAssetJobTests.__dict__["setUpTestData"]
    write_contract = staticmethod(fixtures.ExecutionProfileAssetJobTests.write_contract)
    input = staticmethod(fixtures.ExecutionProfileAssetJobTests.input)
    bind = fixtures.ExecutionProfileAssetJobTests.bind
    create = fixtures.ExecutionProfileAssetJobTests.create

    def setUp(self):
        fixtures.ExecutionProfileAssetJobTests.setUp(self)

    def field(self, prep, text, revision):
        prep.refresh_from_db()
        return create_manual_execution_field(preparation_id=prep.pk, unit_key="asset", field_key="composite_direction",
            text=text, revision=revision, expected_preparation_revision=prep.revision, actor=self.actor)

    def approve(self, prep, value):
        prep.refresh_from_db()
        return review_execution_field(preparation_id=prep.pk, field_value_id=value.pk, approve=True,
            expected_text_sha256=value.text_sha256, expected_source_sha256=value.source_sha256,
            expected_preparation_revision=prep.revision, actor=self.actor)

    def test_pending_work_survives_without_fabricated_approval(self):
        source = self.create([{"unit_key": "asset", "field_key": "composite_direction", "text": "A courier opens a parcel."}])
        prep = source.execution_preparations.get()
        before = prep.field_values.get()
        request_asset_generation_cancel(source)
        retry, _ = retry_asset_generation_job(source)
        target = retry.execution_preparations.get()
        copied = target.field_values.get()
        self.assertNotEqual(copied.pk, before.pk)
        self.assertEqual((copied.text, copied.source_sha256, copied.origin_kind), (before.text, before.source_sha256, "manual"))
        self.assertEqual((copied.status, target.status), ("pending", "awaiting_review"))
        self.assertIsNone(copied.reviewed_at)
        self.assertFalse(target.field_selections.exists())
        self.assertFalse(target.auxiliary_asset_jobs.exists())

    def test_reuses_exact_selected_approval_not_newer_confirmed_field(self):
        source = self.create([])
        prep = source.execution_preparations.get()
        chosen = self.approve(prep, self.field(prep, "A courier opens a blue parcel.", 1))
        prep.refresh_from_db()
        select_execution_field(preparation_id=prep.pk, source_preparation_id=prep.pk, field_value_id=chosen.pk,
            expected_preparation_revision=prep.revision, expected_selection_revision=None, replace=False, actor=self.actor)
        self.approve(prep, self.field(prep, "A courier opens a different parcel.", 2))
        request_asset_generation_cancel(source)
        retry, _ = retry_asset_generation_job(source)
        target = retry.execution_preparations.get()
        selection = target.field_selections.get()
        self.assertEqual(selection.field_value_id, chosen.pk)
        self.assertEqual(selection.field_value.preparation_id, prep.pk)
        self.assertEqual(selection.field_value.reviewed_at, chosen.reviewed_at)
        self.assertFalse(target.field_values.exists())
        self.assertIsNone(target.request_frozen_at)
