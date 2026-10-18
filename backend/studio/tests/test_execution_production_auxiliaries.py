"""Real production lineage/worker/field rows with isolated provider responses."""
from copy import deepcopy
from decimal import Decimal
import json
from unittest.mock import Mock, patch

from django.core.exceptions import ValidationError
from django.db import IntegrityError, transaction
from django.test import TestCase, override_settings

from studio.tests.field_definition_fixtures import create_profile_field_rule
from studio.models import AssetGenerationJob, ExecutionPreparation, GeneratedArtifact, GenerationAttempt
from studio.services.execution_advancement import advance_execution_preparation
from studio.services.execution_auxiliary_requests import read_production_field_context
from studio.services.execution_definitions import build_execution_definition
from studio.services.execution_field_review import apply_preparation_field_policy
from studio.services.execution_instances import clone_execution_draft, update_execution_draft, publish_execution_version
from studio.services.execution_production_auxiliaries import create_production_field_child, reconcile_production_preparation_fields
from studio.services.execution_production_dispatch import dispatch_production_execution
from studio.services.execution_task_links import validate_execution_task_links
from studio.services.json_contracts import json_contract
from studio.services.provider_registry import ProviderDriverError
from studio.services.text_generation_queue import claim_next_text_attempt, process_text_attempt
from studio.tests import test_execution_field_review as fixtures
from studio.tests.test_execution_asset_requests import isolated_observations


def production_observations(preparation, definition, request):
    from dataclasses import replace
    facts = isolated_observations(preparation, definition, request)
    body = deepcopy(facts.root_contract.value)
    body["budget"]["is_external"] = True
    body["dispatch_required"] = True
    return replace(facts, root_contract=json_contract(body))


@override_settings(AI_PROVIDER_ALLOWED_HOSTS=("api.example.com", "cdn.example.com"))
class ProductionAuxiliaryTests(TestCase):
    def test_provider_failure_unknown_bill_requires_explicit_batch_acknowledgement(self):
        from studio.models import PlatformLifecycleAuditEvent
        from studio.services.execution_auxiliary_actions import read_auxiliary_batches, retry_preparation_field_batch
        _, failed, _ = self.execute(effect=ProviderDriverError("PRIVATE HOST", code="provider_dns_unavailable"))
        self.assertIn("provider_dns_unavailable", failed.error_message)
        self.assertNotIn("PRIVATE", failed.error_message)
        before = (failed.status, failed.request_sha256, failed.actual_cost, failed.committed_cost)
        self.run.refresh_from_db()
        old_total = self.run.committed_cost_total
        self.run.status, self.run.blocked_code = "awaiting_input", "execution_preparation_review_required"
        self.run.save(update_fields=("status", "blocked_code"))
        self.parent.refresh_from_db()
        batch = read_auxiliary_batches(self.parent)[0]
        self.assertTrue(batch["can_retry"])
        self.assertTrue(batch["requires_uncertain_acknowledgement"])
        args = dict(preparation_id=self.parent.pk, expected_preparation_revision=self.parent.revision,
            field_batch_key=batch["field_batch_key"], attempt_number=2, actor=self.actor)
        for acknowledgement in (None, self.parent.pk):
            with self.assertRaises(ValidationError):
                retry_preparation_field_batch(**args, acknowledged_uncertain_task_id=acknowledgement)
        self.assertEqual(GenerationAttempt.objects.count(), 1)
        retry_preparation_field_batch(**args, acknowledged_uncertain_task_id=failed.pk)
        successor = GenerationAttempt.objects.get(parent_preparation=self.parent, attempt_number=2)
        self.assertEqual(successor.field_batch_key, failed.field_batch_key)
        self.assertEqual(successor.status, "queued")
        failed.refresh_from_db(); self.run.refresh_from_db()
        self.assertEqual(before, (failed.status, failed.request_sha256, failed.actual_cost, failed.committed_cost))
        self.assertEqual(self.run.committed_cost_total, old_total + successor.committed_cost)
        self.assertEqual(self.run.status, "running")
        self.assertEqual(PlatformLifecycleAuditEvent.objects.filter(action="retry_auxiliary_unknown_cost").count(), 1)
        with self.assertRaises(ValidationError):
            retry_preparation_field_batch(**args, acknowledged_uncertain_task_id=failed.pk)
        self.assertEqual(GenerationAttempt.objects.count(), 2)
        self.assertFalse(self.parent.field_values.exists())

    def test_unknown_bill_cannot_be_retried_by_automatic_child_allocator(self):
        _, failed, _ = self.execute(effect=ProviderDriverError("PRIVATE", code="provider_config_invalid"))
        with self.assertRaisesMessage(ValidationError, "必须明确确认"):
            self.child(attempt_number=2)
        self.assertEqual(GenerationAttempt.objects.count(), 1)
        from studio.services.execution_production_auxiliaries import production_field_retry_requires_acknowledgement
        for changes in ({"claim_token": __import__('uuid').uuid4()}, {"result_sha256": "a"*64},
                {"ignore_late_result": True}, {"error_code": "uncertain_remote_response"}):
            old = {key: getattr(failed, key) for key in changes}
            for key, value in changes.items():
                setattr(failed, key, value)
            with self.assertRaises(ValidationError):
                production_field_retry_requires_acknowledgement(failed)
            for key, value in old.items():
                setattr(failed, key, value)

    def fail_before_submission(self):
        from studio.tests.test_production_automatic_retry import freeze_retry_policy
        freeze_retry_policy(self.run, 2)
        child = self.child()
        self.advance(child)
        claim = claim_next_text_attempt("isolated-unsubmitted-fields")
        peer = Mock()
        with patch("studio.services.execution_production_text_worker.begin_production_execution_submission",
                side_effect=ValidationError("冻结输入不一致；token=private-example；https://private.example.invalid/input")), \
                patch("studio.services.execution_production_text_worker.driver_for", return_value=peer):
            failed = process_text_attempt(claim)
        peer.invoke.assert_not_called()
        return failed

    def test_unsubmitted_external_failure_retries_once_without_fake_bill_or_lost_reason(self):
        from datetime import timedelta
        from django.utils import timezone
        from studio.services.execution_auxiliary_actions import read_auxiliary_batches
        failed = self.fail_before_submission()
        self.assertEqual((failed.status, failed.error_code), ("failed", "execution_request_invalid"))
        self.assertIsNone(failed.submission_started_at)
        self.assertIsNone(failed.actual_cost)
        self.assertEqual(failed.committed_cost, 0)
        self.assertIn("提交前准入校验失败", failed.error_message)
        self.assertIn("冻结输入不一致", failed.error_message)
        self.assertNotIn("private-example", failed.error_message)
        self.assertNotIn("private.example", failed.error_message)
        self.assertEqual(read_auxiliary_batches(self.parent)[0]["reason"], failed.error_message)
        waiting = self.advance(self.parent)
        self.assertEqual(waiting.reason_code, "execution_retry_wait")
        GenerationAttempt.objects.filter(pk=failed.pk).update(finished_at=timezone.now()-timedelta(seconds=11))
        self.advance(self.parent)
        self.advance(self.parent)
        replacement = GenerationAttempt.objects.get(parent_preparation=self.parent, attempt_number=2)
        self.assertEqual(replacement.field_batch_key, failed.field_batch_key)
        self.assertEqual(GenerationAttempt.objects.count(), 2)
        failed.refresh_from_db()
        self.assertIsNone(failed.actual_cost)
        self.assertEqual(failed.status, "failed")
        self.assertFalse(failed.artifacts.exists())

    def test_unsubmitted_exception_requires_complete_negative_submission_evidence(self):
        import uuid
        from django.utils import timezone
        from studio.services.production_execution_recovery import production_retry_decision
        failed = self.fail_before_submission()
        self.assertEqual(production_retry_decision(failed)["state"], "retry")
        for changed in ({"submission_started_at": timezone.now()}, {"submitted_at": timezone.now()},
                {"provider_request_id": "remote-receipt"}, {"remote_status": "unknown"},
                {"claim_token": uuid.uuid4()}, {"result_sha256": "a"*64}, {"committed_cost": Decimal("0.01")},
                {"finished_at": None}, {"status": "cancelled"}, {"ignore_late_result": True},
                {"error_code": "uncertain_remote_response"}, {"error_code": "execution_response_storage_failed"}):
            with self.subTest(changed=changed):
                before = {key: getattr(failed, key) for key in changed}
                GenerationAttempt.objects.filter(pk=failed.pk).update(**changed)
                self.assertEqual(production_retry_decision(GenerationAttempt.objects.get(pk=failed.pk))["state"], "manual")
                GenerationAttempt.objects.filter(pk=failed.pk).update(**before)
        from studio.services.text_generation_queue import append_attempt_resolution
        append_attempt_resolution(failed, outcome="response_received", evidence={"schema_version": 1},
            note="Isolated contradictory receipt; must not infer non-submission.")
        self.assertEqual(production_retry_decision(GenerationAttempt.objects.get(pk=failed.pk))["state"], "manual")

    def test_configured_auxiliary_retry_waits_then_allocates_one_child_and_exhausts(self):
        from studio.tests.test_production_automatic_retry import freeze_retry_policy
        from django.utils import timezone
        from datetime import timedelta
        freeze_retry_policy(self.run, 2)
        child, failed, _ = self.execute(response={"text": None, "actual_cost": "0.02", "currency": "USD"})
        self.assertEqual(failed.status, "failed")
        waiting = self.advance(self.parent)
        self.assertEqual(waiting.reason_code, "execution_retry_wait")
        self.assertIsNotNone(waiting.next_check_at)
        self.assertEqual(GenerationAttempt.objects.count(), 1)
        GenerationAttempt.objects.filter(pk=failed.pk).update(finished_at=timezone.now()-timedelta(seconds=11))
        self.advance(self.parent)
        self.advance(self.parent)
        replacement = GenerationAttempt.objects.get(parent_preparation=self.parent, attempt_number=2)
        self.assertEqual(GenerationAttempt.objects.count(), 2)
        self.assertEqual(replacement.field_batch_key, failed.field_batch_key)
        peer = Mock()
        peer.invoke.return_value = {"text": None, "actual_cost": "0.02", "currency": "USD"}
        with patch("studio.services.execution_production_text_worker.driver_for", return_value=peer):
            result = process_text_attempt(claim_next_text_attempt("isolated-retry"))
        self.assertEqual(result.pk, replacement.pk)
        exhausted = self.advance(self.parent)
        self.assertEqual((exhausted.status, exhausted.reason_code), ("awaiting_review", "execution_retry_exhausted"))
        self.assertIn("2/2", exhausted.reason)
        self.assertIsNone(exhausted.next_check_at)
        self.advance(self.parent)
        self.assertEqual(GenerationAttempt.objects.count(), 2)
        self.assertFalse(self.parent.field_values.exists())
        # The formal batch action stays available after the Run hands off to a person.
        from studio.services.execution_auxiliary_actions import read_auxiliary_batches, retry_preparation_field_batch
        self.run.refresh_from_db()
        self.run.status, self.run.blocked_code = "awaiting_input", "execution_preparation_review_required"
        self.run.save(update_fields=("status", "blocked_code"))
        self.parent.refresh_from_db()
        batch = read_auxiliary_batches(self.parent)[0]
        self.assertTrue(batch["can_retry"])
        retry_preparation_field_batch(preparation_id=self.parent.pk,
            expected_preparation_revision=self.parent.revision, field_batch_key=batch["field_batch_key"],
            attempt_number=3, actor=self.actor)
        self.run.refresh_from_db()
        self.assertEqual(self.run.status, "running")
        self.assertEqual(GenerationAttempt.objects.filter(parent_preparation=self.parent, attempt_number=3).count(), 1)
        self.assertEqual(GenerationAttempt.objects.count(), 3)

    def test_field_selection_after_child_creation_does_not_refresh_away_conflict(self):
        from studio.services import execution_production_auxiliaries as coordinator
        from studio.services.execution_preparation_fields import create_manual_execution_field, confirm_execution_field
        original_create = coordinator.create_production_field_child

        def select_after_create(**kwargs):
            result = original_create(**kwargs)
            self.parent.refresh_from_db()
            value = create_manual_execution_field(preparation_id=self.parent.pk, unit_key="asset-unit",
                field_key="composite_direction", text="A courier opens a blue parcel.", revision=1,
                expected_preparation_revision=self.parent.revision, actor=self.actor)
            self.parent.refresh_from_db()
            confirm_execution_field(preparation_id=self.parent.pk, field_value_id=value.pk,
                expected_text_sha256=value.text_sha256, expected_source_sha256=value.source_sha256,
                expected_preparation_revision=self.parent.revision, select_for_execution=True,
                expected_selection_revision=None, replace=False, actor=self.actor)
            return result

        self.parent.refresh_from_db()
        with patch.object(coordinator, "create_production_field_child", side_effect=select_after_create):
            with self.assertRaises(ValidationError) as caught:
                reconcile_production_preparation_fields(preparation_id=self.parent.pk,
                    expected_preparation_revision=self.parent.revision, actor=self.actor)
        self.assertEqual(caught.exception.code, "execution_preparation_conflict")
        self.parent.refresh_from_db()
        resumed = reconcile_production_preparation_fields(preparation_id=self.parent.pk,
            expected_preparation_revision=self.parent.revision, actor=self.actor)
        self.assertEqual((resumed.status, resumed.reason_code), ("preparing", ""))
        self.assertEqual(self.parent.field_selections.count(), 1)
        self.assertEqual(ExecutionPreparation.objects.filter(stage_run=self.root_stage).count(), 2)
        self.assertFalse(GenerationAttempt.objects.exists())

    def test_field_selection_between_evaluation_and_child_wait_rejects_stale_write(self):
        child = self.child()
        ExecutionPreparation.objects.filter(pk=child.pk).update(
            status="blocked", reason_code="execution_request_invalid", next_check_at=None)
        from studio.services import execution_production_auxiliaries as coordinator
        from studio.services.execution_preparation_fields import create_manual_execution_field, confirm_execution_field
        original_wait = coordinator._record_wait
        selected_revision = None

        def select_before_wait(parent, actor):
            nonlocal selected_revision
            value = create_manual_execution_field(preparation_id=parent.pk, unit_key="asset-unit",
                field_key="composite_direction", text="A courier opens a blue parcel.", revision=1,
                expected_preparation_revision=parent.revision, actor=actor)
            self.parent.refresh_from_db()
            confirm_execution_field(preparation_id=parent.pk, field_value_id=value.pk,
                expected_text_sha256=value.text_sha256, expected_source_sha256=value.source_sha256,
                expected_preparation_revision=self.parent.revision, select_for_execution=True,
                expected_selection_revision=None, replace=False, actor=actor)
            self.parent.refresh_from_db()
            selected_revision = self.parent.revision
            return original_wait(parent, actor)

        self.parent.refresh_from_db()
        with patch.object(coordinator, "_record_wait", side_effect=select_before_wait):
            with self.assertRaises(ValidationError) as caught:
                reconcile_production_preparation_fields(preparation_id=self.parent.pk,
                    expected_preparation_revision=self.parent.revision, actor=self.actor)
        self.assertEqual(caught.exception.code, "execution_preparation_conflict")
        self.parent.refresh_from_db()
        self.assertEqual(self.parent.revision, selected_revision)
        resumed = reconcile_production_preparation_fields(preparation_id=self.parent.pk,
            expected_preparation_revision=self.parent.revision, actor=self.actor)
        self.assertEqual((resumed.status, resumed.reason_code), ("preparing", ""))
        self.assertEqual(self.parent.field_selections.count(), 1)
        self.assertEqual(ExecutionPreparation.objects.filter(stage_run=self.root_stage).count(), 2)
        self.assertFalse(GenerationAttempt.objects.exists())

    def test_unchanged_blocked_child_does_not_invalidate_material_change_revision(self):
        child = self.child()
        # Isolated failed-request fixture: no remote invocation was allocated.
        ExecutionPreparation.objects.filter(pk=child.pk).update(
            status="blocked", reason_code="execution_request_invalid", next_check_at=None)
        blocked = self.advance(self.parent)
        self.assertEqual(blocked.reason_code, "execution_auxiliary_requires_action")
        original = (blocked.revision, blocked.evaluation_revision,
            blocked.contract.evaluation_sha256, blocked.evaluated_at)
        for _ in range(3):
            current = self.advance(self.parent)
            self.assertEqual((current.revision, current.evaluation_revision,
                current.contract.evaluation_sha256, current.evaluated_at), original)
        self.assertFalse(GenerationAttempt.objects.exists())
        self.assertFalse(self.parent.field_selections.exists())
        self.assertEqual(ExecutionPreparation.objects.filter(stage_run=self.root_stage).count(), 2)
        # A changed child remains a new observation; do not freeze the wait.
        ExecutionPreparation.objects.filter(pk=child.pk).update(status="cancelled")
        changed = self.advance(self.parent)
        self.assertGreater(changed.revision, original[0])
        self.assertEqual(changed.contract.evaluation["children"][0]["status"], "cancelled")
        # An explicit manual value/selection must invalidate the old wait.
        from studio.services.execution_preparation_fields import create_manual_execution_field, confirm_execution_field
        value = create_manual_execution_field(preparation_id=changed.pk, unit_key="asset-unit",
            field_key="composite_direction", text="A courier opens a blue parcel.", revision=1,
            expected_preparation_revision=changed.revision, actor=self.actor)
        changed.refresh_from_db()
        confirm_execution_field(preparation_id=changed.pk, field_value_id=value.pk,
            expected_text_sha256=value.text_sha256, expected_source_sha256=value.source_sha256,
            expected_preparation_revision=changed.revision, select_for_execution=True,
            expected_selection_revision=None, replace=False, actor=self.actor)
        changed.refresh_from_db()
        resumed = reconcile_production_preparation_fields(preparation_id=changed.pk,
            expected_preparation_revision=changed.revision, actor=self.actor)
        self.assertEqual((resumed.status, resumed.reason_code), ("preparing", ""))
        self.assertFalse(GenerationAttempt.objects.exists())

    def test_outcome_separates_auxiliary_success_main_failure_and_output_records_without_writes(self):
        from rest_framework.test import APIClient
        from django.db import connection
        from django.test.utils import CaptureQueriesContext
        child, auxiliary, _ = self.execute()
        primary = GenerationAttempt.objects.create(stage_run=self.root_stage, preparation=self.parent,
            execution_instance_version=self.version, role="primary", unit_key=self.parent.unit_key,
            attempt_number=1, status="failed", operation=self.version.operation,
            request_sha256="a"*64, request_snapshot_key="isolated-status", error_code="fixture_failure")
        from django.utils import timezone
        # Narrow index fixture: the list must not read or reconstruct a transport.
        self.parent.status = "dispatched"
        self.parent.request_sha256 = "a"*64
        self.parent.request_frozen_at = timezone.now()
        self.parent.save(update_fields=("status", "request_sha256", "request_frozen_at"))
        client = APIClient(); client.force_authenticate(self.actor)
        url = "/api/v1/execution-preparations/"
        def read(kind):
            response = client.get(url, {"stage_run_id": self.root_stage.pk, "kind": kind})
            self.assertEqual(response.status_code, 200, response.data)
            return response.data["results"][0]
        before = list(GenerationAttempt.objects.values("id", "status", "updated_at"))
        with CaptureQueriesContext(connection) as queries:
            main, aux = read("primary"), read("auxiliary")
        self.assertEqual(main["presentation"]["task"]["status"], "failed")
        self.assertIn("主调用失败", main["outcome"]["message"])
        self.assertIn("不代表主调用成功", aux["outcome"]["message"])
        self.assertEqual(aux["presentation"]["task"]["status"], "succeeded")
        self.assertFalse(any(q["sql"].lstrip().upper().startswith(("INSERT", "UPDATE", "DELETE")) for q in queries))
        self.assertEqual(before, list(GenerationAttempt.objects.values("id", "status", "updated_at")))
        primary.status = "succeeded"; primary.error_code = ""; primary.save(update_fields=("status", "error_code"))
        self.assertIn("未读取到关联产物", read("primary")["outcome"]["message"])
        GeneratedArtifact.objects.create(manifest_key="isolated-status-output", production_run=self.run,
            stage_run=self.root_stage, generation_attempt=primary, kind="video", role="candidate", status="selected")
        main = read("primary")
        self.assertEqual(main["outcome"]["artifact_count"], 1)
        self.assertEqual(main["outcome"]["selected_count"], 1)
        self.assertEqual(read("auxiliary")["outcome"]["artifact_count"], 0)
        self.parent.status = "cancelled"; self.parent.save(update_fields=("status",))
        main = read("primary")
        self.assertIn("此准备已停止", main["outcome"]["message"])
        self.assertEqual(main["presentation"]["task"]["status"], "succeeded")
        self.assertEqual(main["outcome"]["artifact_count"], 1)
        self.assertEqual(GenerationAttempt.objects.count(), 2)

    def test_unit_groups_keep_distinct_same_label_units_stable_across_pages_and_stages(self):
        from rest_framework.test import APIClient
        # Only narrow index fixtures are needed: the listing must never read contracts.
        def index_row(unit, request, stage=None):
            return ExecutionPreparation.objects.create(stage_run=stage or self.root_stage,
                execution_instance_version=self.version, purpose="production", unit_key=unit,
                request_key=request, status="awaiting_input", created_by=self.actor, intent_sha256="a" * 64)
        second = index_row("other-unit", "candidate-1")
        retry = index_row(self.parent.unit_key, "candidate-2")
        foreign = index_row(self.parent.unit_key, "candidate-1", self.stage())
        client = APIClient(); client.force_authenticate(self.actor)
        params = {"stage_run_id": self.root_stage.pk, "kind": "primary", "page_size": 1}
        one = client.get("/api/v1/execution-preparations/groups/", params)
        two = client.get("/api/v1/execution-preparations/groups/", {**params, "page": 2})
        self.assertEqual(one.status_code, 200, one.data)
        self.assertEqual(two.status_code, 200, two.data)
        self.assertEqual(one.data["count"], 2)
        self.assertEqual(one.data["results"][0]["version_count"], 2)
        self.assertEqual(str(one.data["results"][0]["latest"]["id"]), str(retry.pk))
        self.assertEqual(str(two.data["results"][0]["latest"]["id"]), str(second.pk))
        self.assertEqual(one.data["results"][0]["label"], two.data["results"][0]["label"])
        history = client.get("/api/v1/execution-preparations/", {**params, "unit_key": self.parent.unit_key, "page_size": 50})
        self.assertEqual(history.data["count"], 2)
        self.assertNotIn(str(foreign.pk), [str(row["id"]) for row in history.data["results"]])

    def test_unit_groups_merge_retries_before_pagination_and_keep_failed_history(self):
        from rest_framework.test import APIClient
        from django.db import connection
        from django.test.utils import CaptureQueriesContext
        first, failed, _ = self.execute(response={"text": "invalid-json"})
        second = self.child(attempt_number=2)
        self.advance(second)
        client = APIClient(); client.force_authenticate(self.actor)
        params = {"stage_run_id": self.root_stage.pk, "kind": "auxiliary", "page_size": 1}
        before = list(GenerationAttempt.objects.values("id", "status", "updated_at"))
        with CaptureQueriesContext(connection) as queries:
            response = client.get("/api/v1/execution-preparations/groups/", params)
        self.assertEqual(response.status_code, 200, response.data)
        self.assertEqual(response.data["count"], 1)
        self.assertIsNone(response.data["next"])
        group = response.data["results"][0]
        self.assertEqual(group["unit_key"], self.parent.unit_key)
        self.assertEqual(group["version_count"], 2)
        self.assertEqual(str(group["latest"]["id"]), str(second.pk))
        self.assertEqual(group["latest"]["presentation"]["task"]["status"], "queued")
        self.assertFalse(any("executionpreparationcontract" in q["sql"].lower() for q in queries))
        self.assertFalse(any(q["sql"].lstrip().upper().startswith(("INSERT", "UPDATE", "DELETE")) for q in queries))
        business = [q for q in queries if any(name in q["sql"].lower() for name in (
            "studio_executionpreparation", "studio_generationattempt", "studio_shot", "studio_stagerun"))]
        self.assertLessEqual(len(business), 6)
        history = []
        for page in (1, 2):
            r = client.get("/api/v1/execution-preparations/", {**params, "unit_key": group["unit_key"], "page": page})
            self.assertEqual(r.status_code, 200, r.data)
            self.assertEqual(r.data["count"], 2)
            history.extend(r.data["results"])
        self.assertEqual([str(row["id"]) for row in history], [str(second.pk), str(first.pk)])
        self.assertEqual(history[1]["presentation"]["task"]["status"], failed.status)
        self.assertEqual(before, list(GenerationAttempt.objects.values("id", "status", "updated_at")))
        self.assertFalse(self.parent.field_selections.exists())

    def test_unit_groups_do_not_guess_undispatched_lineage_and_reject_invalid_scope(self):
        from rest_framework.test import APIClient
        from django.contrib.auth import get_user_model
        child = self.child()
        client = APIClient(); client.force_authenticate(self.actor)
        url = "/api/v1/execution-preparations/groups/"
        params = {"stage_run_id": self.root_stage.pk, "kind": "auxiliary"}
        response = client.get(url, params)
        self.assertEqual(response.status_code, 200, response.data)
        group = response.data["results"][0]
        self.assertEqual(group["unit_key"], child.unit_key)
        self.assertIsNone(group["latest"]["presentation"]["parent"])
        for bad in ({"stage_run_id": self.root_stage.pk}, {**params, "kind": "bad"},
                {**params, "unit_key": self.parent.unit_key}, {**params, "unknown": 1}):
            self.assertEqual(client.get(url, bad).status_code, 400)
        self.assertEqual(client.get(url, {**params, "kind": "primary"}).data["results"][0]["unit_key"], self.parent.unit_key)
        history_url = "/api/v1/execution-preparations/"
        self.assertEqual(client.get(history_url, {"stage_run_id": self.root_stage.pk, "unit_key": self.parent.unit_key}).status_code, 400)
        self.assertEqual(client.get(history_url, {**params, "unit_key": "not-a-real-unit"}).data["count"], 0)
        outsider = get_user_model().objects.create_user(username="group-outsider")
        client.force_authenticate(outsider)
        self.assertEqual(client.get(url, params).status_code, 404)
        self.assertEqual(client.get(history_url, {**params, "unit_key": child.unit_key}).status_code, 404)

    def test_presentation_filters_before_pagination_and_resolves_pre_dispatch_parent_on_detail_only(self):
        from rest_framework.test import APIClient
        child = self.child()
        client = APIClient()
        client.force_authenticate(self.actor)
        for kind, expected in (("primary", self.parent), ("auxiliary", child)):
            response = client.get("/api/v1/execution-preparations/", {"stage_run_id": self.root_stage.pk, "kind": kind, "page_size": 1})
            self.assertEqual(response.status_code, 200, response.data)
            self.assertEqual(response.data["count"], 1)
            row = response.data["results"][0]
            self.assertEqual(str(row["id"]), str(expected.pk))
            self.assertEqual(row["presentation"]["kind"], kind)
            self.assertIsNone(row["presentation"]["task"])
        detail = client.get(f"/api/v1/execution-preparations/{child.pk}/")
        self.assertEqual(detail.status_code, 200, detail.data)
        self.assertEqual(str(detail.data["presentation"]["parent"]["id"]), str(self.parent.pk))
        self.assertEqual(detail.data["field_slots"], [])
        self.assertFalse(GenerationAttempt.objects.exists())
        self.assertFalse(self.parent.field_selections.exists())
        self.assertEqual(client.get("/api/v1/execution-preparations/", {"stage_run_id": self.root_stage.pk, "kind": "made_up"}).status_code, 400)

    def test_presentation_actual_result_is_not_dispatched_or_approval_and_reads_are_bounded(self):
        from rest_framework.test import APIClient
        from django.db import connection
        from django.test.utils import CaptureQueriesContext
        child, task, _ = self.execute()
        client = APIClient()
        client.force_authenticate(self.actor)
        with CaptureQueriesContext(connection) as queries:
            response = client.get("/api/v1/execution-preparations/", {"stage_run_id": self.root_stage.pk, "kind": "auxiliary"})
        self.assertEqual(response.status_code, 200, response.data)
        row = response.data["results"][0]
        self.assertEqual(row["status"], "dispatched")
        self.assertEqual(row["presentation"]["task"]["status"], "succeeded")
        self.assertEqual(str(row["presentation"]["task"]["id"]), str(task.pk))
        self.assertEqual(str(row["presentation"]["parent"]["id"]), str(self.parent.pk))
        business_queries = [q for q in queries if any(name in q["sql"] for name in (
            "studio_executionpreparation", "studio_generationattempt", "studio_shot", "studio_stagerun"))]
        self.assertLessEqual(len(business_queries), 6)  # Excludes existing configuration-catalog reads.
        self.assertFalse(any("executionpreparationcontract" in q["sql"].lower() for q in queries))
        self.assertFalse(any(q["sql"].lstrip().upper().startswith(("INSERT", "UPDATE", "DELETE")) for q in queries))
        self.assertFalse(self.parent.field_selections.exists())
        self.assertEqual(self.parent.field_values.get().status, "pending")
        self.assertFalse(child.field_values.exists())

    def test_presentation_keeps_failed_task_visible_without_retry(self):
        from rest_framework.test import APIClient
        child, task, peer = self.execute(response={"text": "invalid-json"})
        self.assertEqual(task.status, "awaiting_review")
        client = APIClient()
        client.force_authenticate(self.actor)
        response = client.get(f"/api/v1/execution-preparations/{child.pk}/")
        self.assertEqual(response.status_code, 200, response.data)
        self.assertEqual(response.data["presentation"]["task"]["status"], "awaiting_review")
        self.assertEqual(response.data["presentation"]["task"]["error_code"], task.error_code)
        self.assertEqual(GenerationAttempt.objects.count(), 1)
        peer.invoke.assert_called_once()

    def test_stage_preparation_pages_keep_distinct_main_and_auxiliary_ids(self):
        from rest_framework.test import APIClient
        from django.db import connection
        from studio.execution_preparation_serializers import ExecutionPreparationSerializer
        child = self.child()
        client = APIClient()
        client.force_authenticate(self.actor)
        rows = []
        for page in (1, 2):
            response = client.get("/api/v1/execution-preparations/", {"stage_run_id": str(self.root_stage.pk), "page_size": 1, "page": page})
            self.assertEqual(response.status_code, 200, response.data)
            self.assertEqual(response.data["count"], 2)
            self.assertEqual(len(response.data["results"]), 1)
            self.assertEqual(response.data["next"] is None, page == 2)
            rows.extend(response.data["results"])
        self.assertEqual({str(row["id"]) for row in rows}, {str(child.pk), str(self.parent.pk)})
        self.assertEqual({str(row["execution_instance_version"]) for row in rows}, {str(self.version.pk), str(self.leaf.pk)})
        self.assertNotEqual(rows[0]["unit_key"], rows[1]["unit_key"])
        if connection.vendor == "sqlite":
            plan = ExecutionPreparation.objects.filter(stage_run=self.root_stage).only(
                *ExecutionPreparationSerializer.Meta.fields).order_by("unit_key", "request_key").explain().upper()
            self.assertIn("USING INDEX", plan)
            self.assertNotIn("USE TEMP B-TREE", plan)

    def test_stage_preparation_list_is_scoped_paged_and_never_reads_contracts(self):
        from django.db import connection
        from django.test.utils import CaptureQueriesContext
        from rest_framework.test import APIClient
        client = APIClient()
        client.force_authenticate(self.actor)
        before = (ExecutionPreparation.objects.count(), GenerationAttempt.objects.count())
        with CaptureQueriesContext(connection) as queries:
            response = client.get("/api/v1/execution-preparations/", {"stage_run_id": str(self.root_stage.pk), "page_size": 1})
        self.assertEqual(response.status_code, 200, response.data)
        self.assertEqual(response.data["count"], 1)
        row = response.data["results"][0]
        self.assertEqual(str(row["id"]), str(self.parent.pk))
        self.assertEqual(str(row["stage_run"]), str(self.root_stage.pk))
        self.assertNotIn("intent", row)
        self.assertNotIn("prepared_request", row)
        self.assertFalse(any("executionpreparationcontract" in item["sql"].lower() for item in queries))
        self.assertFalse(any(item["sql"].lstrip().upper().startswith(("INSERT", "UPDATE", "DELETE")) for item in queries))
        self.assertEqual(before, (ExecutionPreparation.objects.count(), GenerationAttempt.objects.count()))
        for params in ({}, {"stage_run_id": "invalid"}, {"stage_run_id": str(self.root_stage.pk), "unknown": "x"}):
            self.assertEqual(client.get("/api/v1/execution-preparations/", params).status_code, 400)
        self.assertEqual(client.get(f"/api/v1/execution-preparations/?stage_run_id={self.root_stage.pk}&stage_run_id={self.root_stage.pk}").status_code, 400)
        self.assertEqual(client.get("/api/v1/execution-preparations/", {"stage_run_id": "00000000-0000-0000-0000-000000000000"}).status_code, 404)
        from django.contrib.auth import get_user_model
        outsider = get_user_model().objects.create_user(username="preparation-outsider")
        client.force_authenticate(outsider)
        self.assertEqual(client.get("/api/v1/execution-preparations/", {"stage_run_id": str(self.root_stage.pk)}).status_code, 404)

    setUpTestData = classmethod(fixtures.ExecutionFieldReviewTests.setUpTestData.__func__)
    write_contract = staticmethod(fixtures.ExecutionFieldReviewTests.write_contract)
    input = staticmethod(fixtures.ExecutionFieldReviewTests.input)
    bind = fixtures.ExecutionFieldReviewTests.bind
    stage = fixtures.ExecutionFieldReviewTests.stage
    prepare = fixtures.ExecutionFieldReviewTests.prepare

    def setUp(self):
        fixtures.ExecutionFieldReviewTests.setUp(self)
        from studio.services.configuration import create_config_revision, publish_config_revision
        from studio.services.execution_resource_policy import RESOURCE_POLICY
        revision = create_config_revision(scope_type="system", values={RESOURCE_POLICY: {
            "budget_by_currency": {"USD": "1.00"}, "call_count": 100, "output_length": 4096,
        }}, created_by=self.actor, change_summary="isolated auxiliary review threshold")
        publish_config_revision(revision, published_by=self.actor)
        leaf = clone_execution_draft(source_id=self.leaf.pk, expected_revision=self.leaf.revision,
            expected_definition_sha256=self.leaf.contract.sha256, actor=self.actor)
        leaf = update_execution_draft(version_id=leaf.pk, expected_revision=leaf.revision, actor=self.actor, changes={"parameters": []})
        self.leaf = publish_execution_version(version_id=leaf.pk, expected_revision=leaf.revision,
            expected_definition_sha256=build_execution_definition(leaf.pk).sha256, actor=self.actor)
        version = clone_execution_draft(source_id=self.version.pk, expected_revision=self.version.revision,
            expected_definition_sha256=self.definition.sha256, actor=self.actor)
        version = update_execution_draft(version_id=version.pk, expected_revision=version.revision, actor=self.actor,
            changes={"auxiliaries": [{"purpose": "generate_fields", "target_version_id": str(self.leaf.pk)}]})
        self.definition = build_execution_definition(version.pk)
        self.version = publish_execution_version(version_id=version.pk, expected_revision=version.revision,
            expected_definition_sha256=self.definition.sha256, actor=self.actor)
        self.root_stage = self.stage(interaction="assisted", choice={"mode": "manual"})
        self.run = self.root_stage.production_run
        self.run.allow_external_calls = True
        self.run.save(update_fields=("allow_external_calls",))
        self.parent = self.prepare(stage=self.root_stage)
        self.batch = self.parent.contract.resolved_inputs["field_requests"][0]
        self.original_asset_count = AssetGenerationJob.objects.count()
        for target in ("studio.services.execution_production_admission.collect_production_execution_admission",
                "studio.services.execution_production_dispatch.collect_production_execution_admission"):
            self.enterContext(patch(target, side_effect=production_observations))

    def child(self, attempt_number=1):
        self.parent.refresh_from_db()
        return create_production_field_child(preparation_id=self.parent.pk, expected_preparation_revision=self.parent.revision,
            field_batch_key=self.batch["sha256"], attempt_number=attempt_number, actor=self.actor)[0]

    def large_child(self):
        # Both source/batch and the aggregate exceed the removed fixed caps.
        # This is still one batch; all bytes must reach the original child.
        self.sources["style"]["value"]["lighting"] = '"' * 140000
        self.root_stage = self.stage(interaction="assisted", choice={"mode": "manual"})
        self.parent = self.prepare(stage=self.root_stage)
        self.batch = self.parent.contract.resolved_inputs["field_requests"][0]
        self.assertGreater(self.batch["size_bytes"], 65536)
        return self.child()

    def test_large_auxiliary_create_replay_compile_freeze_and_read_without_dispatch(self):
        from tempfile import TemporaryDirectory
        from studio.services.execution_finalization import finalize_execution
        from studio.services.execution_preparation_requests import compile_prepared_execution_transport
        from studio.services.execution_preparations import read_frozen_execution_request
        child = self.large_child()
        self.assertGreater(child.contract.resolved_inputs_size_bytes, 65536)
        self.assertGreater(child.contract.resolved_inputs_size_bytes, 262144)
        self.assertGreater(child.contract.intent_size_bytes, 65536)
        self.assertEqual(self.child().pk, child.pk)
        preview = compile_prepared_execution_transport(preparation_id=child.pk,
            expected_preparation_revision=child.revision, actor=self.actor)
        self.assertGreater(preview.transport.size_bytes, 65536)
        with TemporaryDirectory() as media, self.settings(MEDIA_ROOT=media):
            frozen = finalize_execution(preparation_id=child.pk, expected_preparation_revision=child.revision,
                actor=self.actor, collect_admission_facts=production_observations)
            self.assertEqual(frozen.status, "ready", frozen.reason)
            detail, _, transport = read_frozen_execution_request(frozen)
            self.assertEqual(transport, preview.transport)
            self.assertGreater(detail.prepared_request_size_bytes, 65536)
            replay = finalize_execution(preparation_id=frozen.pk, expected_preparation_revision=frozen.revision,
                actor=self.actor, collect_admission_facts=lambda *_: self.fail("Replay must not re-admit"))
            self.assertEqual(replay.request_sha256, frozen.request_sha256)
        self.assertFalse(GenerationAttempt.objects.exists())
        self.assertFalse(self.parent.field_selections.exists())
        self.assertFalse(self.root_stage.approval_decisions.exists())

    def test_large_auxiliary_transport_keeps_document_limit_and_definition_limit(self):
        from studio.services.execution_capacity_signatures import describe_execution_capacity
        from studio.services.execution_definitions import stored_execution_contract, verify_current_execution_definition
        from studio.services.execution_preparation_requests import compile_prepared_execution_transport
        child = self.large_child()
        with patch("studio.services.execution_request_storage.boundary_int", return_value=65536):
            with self.assertRaisesMessage(ValidationError, "65536"):
                compile_prepared_execution_transport(preparation_id=child.pk,
                    expected_preparation_revision=child.revision, actor=self.actor)
        definition = verify_current_execution_definition(stored_execution_contract(self.leaf.pk))
        oversized = json_contract({**definition.value, "oversized": "x" * 65536}, max_bytes=262144)
        preview = compile_prepared_execution_transport(preparation_id=child.pk,
            expected_preparation_revision=child.revision, actor=self.actor)
        with self.assertRaisesMessage(ValidationError, "65536"):
            describe_execution_capacity(oversized, preview.transport,
                runtime={"kind": "provider_managed", "host_id": None, "fingerprint": None})

    def advance(self, preparation):
        preparation.refresh_from_db()
        return advance_execution_preparation(preparation_id=preparation.pk, expected_preparation_revision=preparation.revision, actor=self.actor)

    def execute(self, *, effect=None, response=None):
        child = self.child()
        advanced = self.advance(child)
        self.assertEqual(advanced.status, "dispatched", advanced.reason)
        claimed = claim_next_text_attempt("production-fields")
        self.assertIsNotNone(claimed)
        peer = Mock()
        peer.invoke.side_effect = effect
        peer.invoke.return_value = response or {"text": json.dumps({"items": [{
            "request_id": self.batch["value"]["items"][0]["request_id"], "text": "A courier opens a blue parcel in soft daylight."}]}),
            "request_id": "isolated-fields", "actual_cost": "0.03", "currency": "USD"}
        with patch("studio.services.execution_production_text_worker.driver_for", return_value=peer):
            result = process_text_attempt(claimed)
        return child, result, peer

    def test_child_identity_does_not_change_main_and_cannot_recurse(self):
        child = self.child()
        self.assertNotEqual(child.pk, self.parent.pk)
        self.assertEqual(child.execution_instance_version_id, self.leaf.pk)
        self.assertEqual(self.parent.execution_instance_version_id, self.version.pk)
        self.assertEqual(read_production_field_context(child)[0].pk, self.parent.pk)
        self.assertEqual(self.child().pk, child.pk)
        self.assertFalse(GenerationAttempt.objects.exists())
        with self.assertRaises(ValidationError):
            create_production_field_child(preparation_id=child.pk, expected_preparation_revision=child.revision,
                field_batch_key=self.batch["sha256"], attempt_number=1, actor=self.actor)
        self.assertEqual(AssetGenerationJob.objects.count(), self.original_asset_count)
        explanation = ExecutionPreparation.objects.filter(stage_run=self.root_stage, unit_key=child.unit_key).explain()
        self.assertIn("INDEX", explanation)

    def test_actual_worker_saves_pending_fields_to_parent_once_and_accounts_once(self):
        child, result, peer = self.execute()
        self.assertEqual(result.status, "succeeded", result.error_code)
        self.assertNotIn("system_message_layout", peer.invoke.call_args.args[2])
        self.assertEqual((result.role, result.preparation_id, result.parent_preparation_id), ("auxiliary", child.pk, self.parent.pk))
        validate_execution_task_links(result)
        from rest_framework.test import APIClient
        client = APIClient()
        client.force_authenticate(self.actor)
        response = client.get(f"/api/v1/task-center/preparation/{child.pk}/")
        self.assertEqual(response.status_code, 200, response.data)
        data = response.data.get("data", response.data)
        self.assertEqual(data["input"]["system_message_trace"]["state"], "matched")
        self.assertIn("不是策略system通道", data["input"]["system_message_trace"]["rows"][1]["source"])
        trace = data["input"]["field_batch_trace"]
        self.assertEqual(trace["state"], "matched")
        self.assertEqual(trace["batch_sha256"], self.batch["sha256"])
        self.assertEqual(trace["items"][0]["request_id"], self.batch["value"]["items"][0]["request_id"])
        explanation = GenerationAttempt.objects.filter(parent_preparation=self.parent,
            field_batch_key=self.batch["sha256"]).order_by("-attempt_number").only("id", "status").explain()
        self.assertRegex(explanation, r"SEARCH studio_generationattempt USING INDEX .*parent_preparation_id=\? AND field_batch_key=\?")
        self.assertNotIn("TEMP B-TREE", explanation)
        self.assertFalse(child.field_values.exists())
        value = self.parent.field_values.get()
        self.assertEqual((value.status, value.origin_attempt_id, value.origin_asset_job_id), ("pending", result.pk, None))
        self.assertFalse(self.parent.field_selections.exists())
        self.assertEqual(self.advance(self.parent).status, "awaiting_review")
        replay = dispatch_production_execution(preparation_id=child.pk, expected_preparation_revision=1, actor=self.actor)
        self.assertEqual(replay.pk, result.pk)
        self.assertEqual(GenerationAttempt.objects.count(), 1)
        self.assertEqual(AssetGenerationJob.objects.count(), self.original_asset_count)
        self.assertFalse(GeneratedArtifact.objects.exists())
        self.run.refresh_from_db()
        self.assertEqual((self.run.actual_cost_total, self.run.committed_cost_total), (Decimal("0.03"), Decimal("0")))
        self.assertEqual(self.run.generation_task_count, 1)
        peer.invoke.assert_called_once()
        self.assertEqual(json.loads(peer.invoke.call_args.args[2]["messages"][1]["content"]), self.batch["value"])

    def test_task_budget_counts_auxiliary_allocation_once_and_rolls_back_failed_allocation(self):
        from studio.services.execution_production_admission import production_task_budget_state
        child = self.child()
        with patch.object(GenerationAttempt, "full_clean", side_effect=ValidationError("invalid allocation")):
            with self.assertRaisesMessage(ValidationError, "invalid allocation"):
                self.advance(child)
        self.run.refresh_from_db()
        self.assertEqual(self.run.generation_task_count, 0)
        self.assertEqual(self.run.committed_cost_total, 0)
        self.assertFalse(GenerationAttempt.objects.exists())
        self.advance(child)
        attempt = GenerationAttempt.objects.get(preparation=child)
        self.run.refresh_from_db(); self.assertEqual(self.run.generation_task_count, 1)
        dispatch_production_execution(preparation_id=child.pk, expected_preparation_revision=1, actor=self.actor)
        self.run.refresh_from_db(); self.assertEqual(self.run.generation_task_count, 1)
        fresh = production_task_budget_state(self.run, None)
        from studio.services.execution_resource_policy import read_resource_policy
        self.assertEqual((fresh["allocated"], fresh["requested"], fresh["limit"]), (1, 1, read_resource_policy().value["values"]["call_count"]))
        self.assertEqual(fresh["blockers"], [])
        queued = production_task_budget_state(self.run, attempt)
        self.assertEqual(queued["requested"], 0); self.assertEqual(queued["blockers"], [])
        self.assertEqual(production_task_budget_state(self.run, attempt)["blockers"], [])

    def test_invalid_response_is_atomic_and_never_auto_retried(self):
        child, result, _ = self.execute(response={"text": '{"items": []}', "actual_cost": "0.02", "currency": "USD"})
        self.assertEqual(result.status, "awaiting_review", result.error_code)
        self.assertFalse(self.parent.field_values.exists())
        for _ in range(2):
            blocked = self.advance(self.parent)
            self.assertEqual(blocked.reason_code, "execution_auxiliary_review_required")
        self.assertEqual(GenerationAttempt.objects.count(), 1)
        self.assertNotEqual(self.child(attempt_number=2).pk, child.pk)
        self.assertEqual(GenerationAttempt.objects.count(), 1)

    def test_uncertain_response_retains_reservation_and_cannot_retry(self):
        _, result, peer = self.execute(effect=ProviderDriverError("lost response", submission_uncertain=True))
        self.assertEqual(result.error_code, "uncertain_remote_response")
        self.assertFalse(self.parent.field_values.exists())
        self.run.refresh_from_db()
        self.assertEqual(self.run.committed_cost_total, Decimal("0.1"))
        with self.assertRaises(ValidationError):
            self.child(attempt_number=2)
        from rest_framework.test import APIClient
        client = APIClient()
        client.force_authenticate(self.actor)
        url = f"/api/v1/execution-preparations/{self.parent.pk}/"
        viewed = client.get(url)
        self.assertEqual(viewed.status_code, 200, viewed.data)
        batch = viewed.data["auxiliary_batches"][0]
        self.assertFalse(batch["can_retry"])
        before = (ExecutionPreparation.objects.count(), GenerationAttempt.objects.count())
        rejected = client.post(url + "retry-field-batch/", {
            "expected_preparation_revision": viewed.data["revision"],
            "field_batch_key": batch["field_batch_key"], "attempt_number": 2}, format="json")
        self.assertEqual(rejected.status_code, 400, rejected.data)
        # Explicit continuation cannot turn an uncertain submission into a paid retry.
        continued = client.post(url + "continue/", {
            "expected_preparation_revision": viewed.data["revision"]}, format="json")
        self.assertEqual(continued.status_code, 200, continued.data)
        self.assertFalse(continued.data["auxiliary_batches"][0]["can_retry"])
        self.assertEqual((ExecutionPreparation.objects.count(), GenerationAttempt.objects.count()), before)
        result.refresh_from_db(); self.run.refresh_from_db()
        self.assertEqual(result.error_code, "uncertain_remote_response")
        self.assertEqual(self.run.committed_cost_total, Decimal("0.1"))
        peer.invoke.assert_called_once()

    def test_invalid_auxiliary_body_is_retained_readable_and_never_auto_adopted(self):
        from hashlib import sha256
        from rest_framework.test import APIClient
        from studio.models import ExecutionTextResponseReview
        raw = 'Actual model response\n```json\n{"items": []}\n```'
        child, result, peer = self.execute(response={"text": raw, "usage": {"total_tokens": 31}})
        self.assertEqual(result.status, "awaiting_review")
        row = ExecutionTextResponseReview.objects.get(attempt=result)
        self.assertEqual(row.original_text, raw)
        self.assertEqual(row.original_sha256, sha256(raw.encode()).hexdigest())
        self.assertEqual(row.diagnostics["validation_code"], "response_json_invalid")
        self.assertFalse(self.parent.field_values.exists())
        client = APIClient(); client.force_authenticate(self.actor)
        url = f"/api/v1/execution-preparations/{child.pk}/"
        data = client.get(url).data["text_response_review"]
        self.assertEqual(data["original_text"], raw)
        self.assertEqual(data["fields"], [])
        self.assertFalse(data["editable"])
        # Aux recovery is human field authoring, not relabelling corrected text
        # as a new model response through the primary candidate endpoint.
        denied = client.post(url + "complete-text-response/", {"expected_preparation_revision": child.revision,
            "review_id": str(row.pk), "original_sha256": row.original_sha256, "text": raw}, format="json")
        self.assertEqual(denied.status_code, 400)
        for _ in range(2):
            self.assertEqual(self.advance(self.parent).reason_code, "execution_auxiliary_review_required")
            process_text_attempt(result)
        self.assertEqual(ExecutionTextResponseReview.objects.count(), 1)
        self.assertFalse(self.parent.field_selections.exists())
        peer.invoke.assert_called_once()
        self.select_manual_replacement()
        row.refresh_from_db(); self.assertEqual(row.original_text, raw)
        client.force_authenticate(user=None)
        self.assertIn(client.get(url).status_code, {401, 403})
        from django.contrib.auth import get_user_model
        outsider = get_user_model().objects.create_user(username="other-response-reader")
        client.force_authenticate(outsider)
        self.assertEqual(client.get(url).status_code, 404)
        from studio.services.execution_text_response_review import describe_text_response_review
        from django.core.files.storage import default_storage
        stored_open = default_storage.open
        def read_file(name, mode, *args, **kwargs):
            if name == row.original_file:
                raise OSError("unavailable")
            return stored_open(name, mode, *args, **kwargs)
        with patch.object(default_storage, "open", side_effect=read_file):
            with self.assertRaisesMessage(ValidationError, "不会用空内容代替"):
                describe_text_response_review(child)

    def test_structured_invalid_field_is_shown_as_actual_text_without_partial_adoption(self):
        from studio.services.execution_text_response_review import describe_text_response_review
        request_id = self.batch["value"]["items"][0]["request_id"]
        raw = json.dumps({"items": [{"request_id": request_id, "text": ""}]})
        child, result, _ = self.execute(response={"text": raw})
        self.assertEqual(result.status, "awaiting_review")
        data = describe_text_response_review(child)
        self.assertEqual(data["fields"][0]["text"], "")
        self.assertEqual(data["diagnostics"]["validation_code"], "empty")
        self.assertFalse(self.parent.field_values.exists())

    def test_generated_label_keeps_exact_pending_text_and_advisory_through_human_confirmation(self):
        from rest_framework.test import APIClient
        from studio.services.execution_preparation_fields import confirm_execution_field
        from studio.models import ExecutionTextResponseReview
        request_id = self.batch["value"]["items"][0]["request_id"]
        raw_text = "composite_direction: A courier opens a blue parcel."
        raw = json.dumps({"items": [{"request_id": request_id, "text": raw_text}]})
        child, result, _ = self.execute(response={"text": raw})
        self.assertEqual(result.status, "succeeded", result.error_code)
        value = self.parent.field_values.get()
        self.assertEqual((value.text, value.status), (raw_text, "pending"))
        self.assertFalse(self.parent.field_selections.exists())
        client = APIClient(); client.force_authenticate(self.actor)
        response = client.get(f"/api/v1/execution-field-values/{value.pk}/")
        self.assertEqual(response.status_code, 200, response.data)
        self.assertEqual(response.data["warnings"][0]["code"], "generated_field_label")
        self.parent.refresh_from_db()
        confirmed, choice = confirm_execution_field(preparation_id=self.parent.pk, field_value_id=value.pk,
            expected_preparation_revision=self.parent.revision, expected_text_sha256=value.text_sha256,
            expected_source_sha256=value.source_sha256, select_for_execution=True,
            expected_selection_revision=None, replace=False, actor=self.actor)
        self.assertEqual(confirmed.approval_kind, "human")
        self.assertEqual(choice.field_value_id, value.pk)
        self.assertEqual(ExecutionTextResponseReview.objects.get(attempt=result).original_text, raw)
        self.assertNotEqual(self.advance(self.parent).status, "blocked")

    def test_postparse_validation_rolls_back_fields_but_retains_actual_response_and_safe_diagnostic(self):
        from studio.models import ExecutionTextResponseReview
        with patch("studio.services.execution_production_text_worker.save_pending_auxiliary_fields",
                side_effect=ValidationError("private internal marker")):
            _, result, _ = self.execute()
        self.assertEqual(result.status, "awaiting_review")
        row = ExecutionTextResponseReview.objects.get(attempt=result)
        self.assertIn("A courier", row.original_text)
        self.assertNotIn("private internal marker", json.dumps(row.diagnostics))
        self.assertFalse(self.parent.field_values.exists())

    def test_legacy_missing_body_is_explicit_and_does_not_reconstruct_history(self):
        from studio.models import ExecutionTextResponseReview
        from studio.services.execution_response_diagnostics import read_preparation_response_diagnostic
        child = self.child(); self.advance(child)
        attempt = GenerationAttempt.objects.get(preparation=child)
        # Historical fixture: absence is deliberate, never "recovered" at read.
        attempt.status = "failed"; attempt.error_code = "execution_response_invalid"
        attempt.save(update_fields=("status", "error_code"))
        child.refresh_from_db()
        data = read_preparation_response_diagnostic(child)
        self.assertEqual(data["state"], "unverifiable")
        self.assertIn("当时未保存", data["summary"])
        self.assertFalse(ExecutionTextResponseReview.objects.exists())

    def test_wrong_parent_stage_and_self_parent_are_rejected(self):
        child = self.child()
        self.advance(child)
        attempt = GenerationAttempt.objects.get(preparation=child)
        attempt.parent_preparation = child
        with self.assertRaises(ValidationError):
            validate_execution_task_links(attempt)
        with self.assertRaises(IntegrityError), transaction.atomic():
            GenerationAttempt.objects.filter(pk=attempt.pk).update(parent_preparation=child)
        other = self.prepare(stage=self.stage(interaction="assisted", choice={"mode": "manual"}))
        attempt.parent_preparation = other
        with self.assertRaises(ValidationError):
            validate_execution_task_links(attempt)

    def test_authorized_automatic_field_review_uses_parent_not_execution_preparation(self):
        self.parent = self.prepare(stage=self.stage())
        self.run = self.parent.stage_run.production_run
        self.batch = self.parent.contract.resolved_inputs["field_requests"][0]
        _, result, _ = self.execute()
        self.assertEqual(result.status, "succeeded", result.error_code)
        self.parent.refresh_from_db()
        apply_preparation_field_policy(preparation_id=self.parent.pk, expected_preparation_revision=self.parent.revision, actor=self.actor)
        value = self.parent.field_values.get()
        self.assertEqual((value.status, value.approval_kind), ("confirmed", "policy"))
        self.assertEqual(self.parent.field_selections.get().field_value_id, value.pk)
        self.parent.stage_run.refresh_from_db()
        self.assertEqual(self.parent.stage_run.status, "running")

    def test_cancel_during_remote_call_keeps_bill_without_writing_main_fields(self):
        def late_response(*args):
            self.run.status = "cancelled"
            self.run.save(update_fields=("status",))
            return {"text": json.dumps({"items": [{"request_id": self.batch["value"]["items"][0]["request_id"],
                "text": "A courier opens a blue parcel."}]}), "actual_cost": "0.04", "currency": "USD"}
        _, result, peer = self.execute(effect=late_response)
        self.assertEqual((result.status, result.ignore_late_result), ("cancelled", True))
        self.assertFalse(self.parent.field_values.exists())
        self.run.refresh_from_db()
        self.assertEqual((self.run.actual_cost_total, self.run.committed_cost_total), (Decimal("0.04"), Decimal("0")))
        self.assertEqual(process_text_attempt(result).pk, result.pk)
        peer.invoke.assert_called_once()

    def test_formal_retry_api_is_owner_bound_explicit_and_never_duplicates(self):
        from rest_framework.test import APIClient
        from django.contrib.auth import get_user_model
        _, failed, _ = self.execute(response={"text": "invalid"})
        self.assertEqual(failed.status, "awaiting_review")
        client = APIClient()
        client.force_authenticate(self.actor)
        url = f"/api/v1/execution-preparations/{self.parent.pk}/"
        viewed = client.get(url)
        self.assertEqual(viewed.status_code, 200, viewed.data)
        batch = viewed.data["auxiliary_batches"][0]
        self.assertTrue(batch["can_retry"])
        self.assertEqual(batch["task_id"], str(failed.pk))
        payload = {"expected_preparation_revision": viewed.data["revision"], "field_batch_key": batch["field_batch_key"], "attempt_number": 2}
        before = (ExecutionPreparation.objects.count(), GenerationAttempt.objects.count())
        for changes in ({"field_batch_key": "f" * 64}, {"attempt_number": 3}, {"expected_preparation_revision": viewed.data["revision"] + 1}):
            with self.subTest(changes=changes):
                rejected = client.post(url + "retry-field-batch/", {**payload, **changes}, format="json")
                self.assertEqual(rejected.status_code, 409 if "expected_preparation_revision" in changes else 400, rejected.data)
                self.assertEqual((ExecutionPreparation.objects.count(), GenerationAttempt.objects.count()), before)
        result = client.post(url + "retry-field-batch/", payload, format="json")
        self.assertEqual(result.status_code, 200, result.data)
        self.assertEqual(result.data["auxiliary_batches"][0]["status"], "queued")
        replay = client.post(url + "retry-field-batch/", payload, format="json")
        self.assertEqual(replay.status_code, 409, replay.data)
        self.assertEqual(GenerationAttempt.objects.count(), 2)
        self.assertEqual(result.data["id"], str(self.parent.pk))
        failed.refresh_from_db()
        self.assertEqual(failed.status, "awaiting_review")
        queued = result.data["auxiliary_batches"][0]
        denied = client.post(url + "retry-field-batch/", {
            "expected_preparation_revision": result.data["revision"], "field_batch_key": queued["field_batch_key"],
            "attempt_number": queued["attempt_number"] + 1}, format="json")
        self.assertEqual(denied.status_code, 400, denied.data)
        self.assertEqual(GenerationAttempt.objects.count(), 2)
        client.force_authenticate(get_user_model().objects.create_user(username="different-field-owner"))
        self.assertEqual(client.get(url).status_code, 404)
        self.assertEqual(client.post(url + "retry-field-batch/", payload, format="json").status_code, 404)

    def test_translation_uses_its_exact_leaf_and_frozen_field_language(self):
        from studio.models import PromptProfileFieldRule, PromptProfileVersion
        from studio.services.prompt_profiles import refresh_prompt_profile_contract, publish_prompt_profile
        leaf = clone_execution_draft(source_id=self.leaf.pk, expected_revision=self.leaf.revision,
            expected_definition_sha256=self.leaf.contract.sha256, actor=self.actor)
        leaf = update_execution_draft(version_id=leaf.pk, expected_revision=leaf.revision, actor=self.actor,
            changes={"operation": "prompt.fields.translate"})
        self.leaf = publish_execution_version(version_id=leaf.pk, expected_revision=leaf.revision,
            expected_definition_sha256=build_execution_definition(leaf.pk).sha256, actor=self.actor)
        profile = PromptProfileVersion.objects.create(profile_key="production-translation", version=1, name="Translation fixture",
            model_family="async_cloud", operation="video.generate", mode="T2VA", review_state="ready", contract_schema_version=8)
        create_profile_field_rule(profile_version=profile, field_definition=self.profile.field_rules.get().field_definition,
            source_selector="request.text", generation_mode="translate_only", generation_source_selector="request.action",
            generation_instruction="Translate only the supplied text.", channel="positive_prompt", position=1,
            required=True, generation_required=True, max_chars=1000, language_policy="en")
        refresh_prompt_profile_contract(profile)
        profile = publish_prompt_profile(profile, actor=self.actor)
        version = clone_execution_draft(source_id=self.version.pk, expected_revision=self.version.revision,
            expected_definition_sha256=self.definition.sha256, actor=self.actor)
        version = update_execution_draft(version_id=version.pk, expected_revision=version.revision, actor=self.actor,
            changes={"prompt_profile_version": profile, "inputs": [self.inputs[0]],
                "auxiliaries": [{"purpose": "translate_fields", "target_version_id": str(self.leaf.pk)}]})
        self.definition = build_execution_definition(version.pk)
        self.version = publish_execution_version(version_id=version.pk, expected_revision=version.revision,
            expected_definition_sha256=self.definition.sha256, actor=self.actor)
        self.sources = {"request.action": {"kind": "literal", "value": "A courier opens a blue parcel."}}
        self.parent = self.prepare(stage=self.stage(interaction="assisted", choice={"mode": "manual"}))
        self.run = self.parent.stage_run.production_run
        self.batch = self.parent.contract.resolved_inputs["field_requests"][0]
        _, result, peer = self.execute()
        self.assertEqual((result.status, result.operation, result.auxiliary_purpose), ("succeeded", "prompt.fields.translate", "translate_fields"))
        transmitted = json.loads(peer.invoke.call_args.args[2]["messages"][1]["content"])
        self.assertEqual(transmitted, self.batch["value"])
        self.assertEqual(transmitted["items"][0]["target_language"], "English")
        self.assertEqual(self.parent.field_values.get().status, "pending")

    def test_explicit_manual_selection_can_finish_field_phase_after_failure(self):
        self.execute(response={"text": "invalid"})
        self.select_manual_replacement()

    def test_truncated_auxiliary_keeps_original_and_uses_manually_sourced_fields(self):
        import tempfile
        from studio.models import ExecutionTextResponseReview
        from studio.services.execution_text_response_review import describe_text_response_review, complete_text_response
        with tempfile.TemporaryDirectory() as folder, override_settings(MEDIA_ROOT=folder):
            error = ProviderDriverError("length", code="provider_response_invalid")
            error.metadata = {"contract_error_code": "response_output_budget_exhausted", "finish_reason": "length"}
            error.partial_text = '{"items": ['
            child, result, peer = self.execute(effect=error)
            self.assertEqual(result.status, "awaiting_review")
            row = ExecutionTextResponseReview.objects.get(attempt=result)
            self.assertEqual(row.original_text, error.partial_text)
            self.assertFalse(describe_text_response_review(child)["editable"])
            child.refresh_from_db()
            with self.assertRaisesMessage(ValidationError, "不能将人工修订标为模型字段结果"):
                complete_text_response(preparation_id=child.pk, expected_preparation_revision=child.revision,
                    review_id=row.pk, original_sha256=row.original_sha256, text='{}', actor=self.actor)
            parent = self.advance(self.parent)
            self.assertEqual(parent.status, "awaiting_review")
            self.assertEqual(parent.reason_code, "execution_auxiliary_review_required")
            self.assertIsNone(parent.next_check_at)
            self.select_manual_replacement()
            value = self.parent.field_values.get()
            self.assertEqual(value.origin_kind, "manual")
            self.assertIsNone(value.origin_attempt_id)
            result.refresh_from_db(); self.assertEqual(result.status, "awaiting_review")
            peer.invoke.assert_called_once()

    def select_manual_replacement(self):
        from studio.services.execution_preparation_fields import create_manual_execution_field, confirm_execution_field
        self.parent.refresh_from_db()
        value = create_manual_execution_field(preparation_id=self.parent.pk, unit_key="asset-unit", field_key="composite_direction",
            text="A courier opens a blue parcel.", revision=1, expected_preparation_revision=self.parent.revision, actor=self.actor)
        self.parent.refresh_from_db()
        confirm_execution_field(preparation_id=self.parent.pk, field_value_id=value.pk, expected_text_sha256=value.text_sha256,
            expected_source_sha256=value.source_sha256, expected_preparation_revision=self.parent.revision,
            select_for_execution=True, expected_selection_revision=None, replace=False, actor=self.actor)
        self.parent.refresh_from_db()
        advanced = reconcile_production_preparation_fields(preparation_id=self.parent.pk,
            expected_preparation_revision=self.parent.revision, actor=self.actor)
        self.assertEqual((advanced.status, advanced.reason_code), ("preparing", ""))
        self.assertEqual(GenerationAttempt.objects.count(), 1)

    def test_migration_does_not_infer_parents_and_result_origin_cannot_be_relabelled(self):
        import importlib
        from django.apps import apps
        from django.db import connection
        from types import SimpleNamespace
        from studio.services.execution_auxiliary_results import verify_production_field_result
        migration = importlib.import_module("studio.migrations.0151_generation_attempt_parent_preparation")
        migration.require_no_legacy_auxiliaries(apps, SimpleNamespace(connection=connection))
        _, result, _ = self.execute()
        with self.assertRaisesMessage(RuntimeError, "no inferred parent backfill"):
            migration.require_no_legacy_auxiliaries(apps, SimpleNamespace(connection=connection))
        self.assertEqual(len(verify_production_field_result(result)), 1)
        GenerationAttempt.objects.filter(pk=result.pk).update(result_sha256="a" * 64)
        result.refresh_from_db()
        with self.assertRaisesMessage(ValidationError, "结果摘要不一致"):
            verify_production_field_result(result)
