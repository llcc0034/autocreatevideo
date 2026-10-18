from copy import deepcopy
from dataclasses import replace
from datetime import timedelta
from unittest.mock import Mock, patch

from django.core.exceptions import ValidationError
from django.test import TestCase, override_settings
from django.utils import timezone

from studio.models import ExecutionPreparation, ExecutionPreparationContract
from studio.services.execution_admission import ExecutionEvidenceIdentity, ExecutionSmokeFact
from studio.services.execution_finalization import ExecutionAdmissionFacts, finalize_execution
from studio.services.execution_policy_configuration import read_execution_policies
from studio.services.execution_preparation_requests import compile_prepared_execution_transport
from studio.services.json_contracts import json_contract
from studio.tests import test_execution_preparation_creation as fixtures
from studio.tests import test_execution_profile_preparation as profile_fixtures


@override_settings(AI_PROVIDER_ALLOWED_HOSTS=("api.example.com", "cdn.example.com"))
class ExecutionFinalizationTests(TestCase):
    """Real frozen preparation/transaction, isolated admission observations.

    These policy fixtures are not live Smoke results or actual budget permits.
    The existing-queue root/evidence adapters are not exercised by these tests.
    """
    setUpTestData = classmethod(fixtures.ExecutionPreparationCreationTests.setUpTestData.__func__)
    write_contract = staticmethod(fixtures.ExecutionPreparationCreationTests.write_contract)
    job_row = fixtures.ExecutionPreparationCreationTests.job_row
    resolve = fixtures.ExecutionPreparationCreationTests.resolve
    create = fixtures.ExecutionPreparationCreationTests.create

    def setUp(self):
        fixtures.ExecutionPreparationCreationTests.setUp(self)
        self.prep = self.create()
        self.now = timezone.now()
        self.smoke_facts = (ExecutionSmokeFact(ExecutionEvidenceIdentity(str(self.version.pk), self.definition.sha256,
            self.definition.value["target_fingerprint"], self.version.operation, self.version.mode, "instance_smoke"),
            "isolated-policy-fixture", "succeeded", self.now - timedelta(hours=2), self.now - timedelta(hours=1)),)
        self.collector = Mock(side_effect=self.collect)

    def collect(self, preparation, definition, request):
        return ExecutionAdmissionFacts(self.smoke_facts, "unknown", [], json_contract({"schema_version": 1,
            "preparation_id": str(preparation.pk), "request_sha256": request.sha256,
            "test_scope": "isolated policy input, not a live root authorization"}))

    def finalize(self, **changes):
        return finalize_execution(**{"preparation_id": self.prep.pk, "expected_preparation_revision": self.prep.revision,
            "actor": self.actor, "collect_admission_facts": self.collector, **changes})

    def test_freezes_exact_request_and_preserves_seed_and_expiry_warning(self):
        preview = compile_prepared_execution_transport(preparation_id=self.prep.pk,
            expected_preparation_revision=self.prep.revision, actor=self.actor)
        result = self.finalize()
        self.assertEqual(result.status, "ready")
        self.assertEqual(result.revision, self.prep.revision + 1)
        detail = result.contract
        self.assertEqual(detail.prepared_request["transport"], preview.transport.value)
        self.assertEqual(detail.prepared_request["field_selections"], preview.field_selections.value)
        self.assertEqual(result.request_sha256, detail.prepared_request_sha256)
        self.assertEqual(detail.evaluation["admission"]["evidence_state"], "expired")
        self.assertEqual(detail.evaluation["admission"]["capacity_state"], "unknown")
        self.assertEqual(len(detail.evaluation["admission"]["warnings"]), 2)
        self.job.refresh_from_db()
        self.assertEqual(self.job.status, "preparing")  # This primitive never dispatches or edits a root queue.

    def test_exact_replay_does_not_recompile_collect_or_rewrite_decision(self):
        first = self.finalize()
        self.collector.reset_mock()
        with patch("studio.services.execution_finalization.compile_prepared_execution_transport", side_effect=AssertionError("No recompile")):
            second = self.finalize()
        self.collector.assert_not_called()
        self.assertEqual((first.request_sha256, first.evaluated_at, first.revision),
            (second.request_sha256, second.evaluated_at, second.revision))
        with self.assertRaises(ValidationError):
            self.finalize(expected_preparation_revision=first.revision + 1)

    def test_large_envelope_freezes_to_file_without_relaxing_database_json_limit(self):
        from studio.services.json_contracts import canonical_json_bytes
        from studio.services.execution_preparations import read_frozen_execution_request
        preview = compile_prepared_execution_transport(preparation_id=self.prep.pk,
            expected_preparation_revision=self.prep.revision, actor=self.actor)
        transport = preview.transport.value
        transport["payload"]["storage_fixture"] = ""
        transport["payload"]["storage_fixture"] = "x" * (65520 - len(canonical_json_bytes(transport)))
        preview = replace(preview, transport=json_contract(transport))
        with patch("studio.services.execution_finalization.compile_prepared_execution_transport", return_value=preview):
            result = self.finalize()
        self.assertGreater(result.contract.prepared_request_size_bytes, 65536)
        detail, _, saved = read_frozen_execution_request(result)
        self.assertEqual(saved, preview.transport)
        self.assertEqual(result.request_sha256, detail.prepared_request_sha256)
        self.assertEqual(detail.prepared_request["field_selections"], preview.field_selections.value)
        with self.assertRaises(ValidationError):
            json_contract(detail.prepared_request)

    def test_storage_failure_records_exact_phase_without_freezing_or_automatic_retry(self):
        from django.core.files.storage import default_storage
        from studio.services.execution_advancement import _finalize_or_record_invalid_request, advance_execution_preparation
        with patch.object(default_storage, "open", side_effect=OSError("private path")):
            blocked = _finalize_or_record_invalid_request(self.prep, self.actor, self.collector)
        self.assertEqual(blocked.reason_code, "execution_request_storage_invalid")
        self.assertEqual(blocked.contract.evaluation["phase"], "request_storage")
        self.assertIsNone(blocked.request_frozen_at)
        self.assertIsNone(blocked.contract.prepared_request_key)
        self.assertIsNone(blocked.next_check_at)
        with patch("studio.services.execution_advancement.finalize_execution", side_effect=AssertionError("No automatic retry")):
            unchanged = advance_execution_preparation(preparation_id=blocked.pk,
                expected_preparation_revision=blocked.revision, actor=self.actor)
        self.assertEqual(unchanged.revision, blocked.revision)
        recovered = self.finalize(expected_preparation_revision=blocked.revision)
        self.assertEqual(recovered.status, "ready")

    def test_missing_combination_persists_block_without_freezing_request(self):
        self.smoke_facts = ()
        result = self.finalize()
        self.assertEqual((result.status, result.reason_code), ("blocked", "execution_smoke_missing"))
        self.assertIsNone(result.request_sha256)
        self.assertIsNone(result.request_frozen_at)
        self.assertIsNone(result.contract.prepared_request)
        self.assertEqual(result.contract.evaluation["preparation_revision"], result.revision)

    def test_blocked_request_can_be_explicitly_rechecked_without_redrawing_seed(self):
        passed = self.smoke_facts
        self.smoke_facts = ()
        blocked = self.finalize()
        self.smoke_facts = passed
        with patch("studio.services.execution_parameter_preparation.randbelow", side_effect=AssertionError("No redraw")):
            ready = self.finalize(expected_preparation_revision=blocked.revision)
        self.assertEqual(ready.status, "ready")
        self.assertEqual(ready.contract.prepared_request["transport"]["parameters"]["values"], self.prep.contract.resolved_inputs["parameters"]["values"])

    def test_latest_failure_or_real_root_blocker_is_not_a_warning(self):
        self.smoke_facts += (replace(self.smoke_facts[0], attempt_id="later-failed-fixture", status="failed",
            finished_at=self.now - timedelta(minutes=5), expires_at=None),)
        blocked = self.finalize()
        self.assertEqual(blocked.reason_code, "execution_smoke_failed")
        def hard_block(*args):
            return replace(self.collect(*args), hard_blockers=[{"code": "budget_required", "message": "Budget approval is required"}])
        blocked = self.finalize(expected_preparation_revision=blocked.revision, collect_admission_facts=hard_block)
        self.assertEqual(blocked.reason_code, "budget_required")

    def test_missing_or_wrong_request_facts_cannot_mark_ready(self):
        for collector in (None, lambda *args: True, lambda *args: replace(self.collect(*args),
                root_contract=json_contract({"schema_version": 1, "preparation_id": str(self.prep.pk), "request_sha256": "0" * 64}))):
            with self.subTest(collector=collector), self.assertRaises(ValidationError):
                self.finalize(collect_admission_facts=collector)
        self.prep.refresh_from_db()
        self.assertIsNone(self.prep.request_frozen_at)
        self.assertIsNone(self.prep.evaluated_at)

    def test_policy_revision_change_aborts_before_persisting_a_stale_decision(self):
        first = read_execution_policies()
        second = replace(first, configuration=json_contract({"schema_version": 1, "different_revision": True}))
        with patch("studio.services.execution_finalization.read_execution_policies", side_effect=[first, second]):
            with self.assertRaisesMessage(ValidationError, "准入配置"):
                self.finalize()
        self.prep.refresh_from_db()
        self.assertIsNone(self.prep.request_frozen_at)

    def test_current_target_drift_blocks_freezing_even_with_old_pass(self):
        config = deepcopy(self.provider.driver_config)
        config["poll_path"] = "/changed/jobs/{request_id}"
        self.write_contract(self.provider, "driver_config", config)
        with self.assertRaises(ValidationError):
            self.finalize()
        self.collector.assert_not_called()

    def test_deactivated_instance_persists_block_even_with_matching_pass(self):
        self.instance.is_active = False
        self.instance.save(update_fields=("is_active",))
        result = self.finalize()
        self.assertEqual(result.status, "blocked")
        self.assertEqual(result.reason_code, "execution_instance_inactive")
        self.assertIsNone(result.request_frozen_at)
        self.assertGreater(result.next_check_at, result.evaluated_at)

    def test_detail_write_failure_rolls_back_all_index_and_readiness_changes(self):
        with patch.object(ExecutionPreparationContract, "save", side_effect=RuntimeError("isolated storage failure")):
            with self.assertRaises(RuntimeError):
                self.finalize()
        self.prep.refresh_from_db()
        self.assertEqual((self.prep.revision, self.prep.status), (1, "preparing"))
        self.assertIsNone(self.prep.request_sha256)
        self.assertIsNone(self.prep.contract.evaluation)

    def test_cancelled_or_stale_preparation_cannot_freeze(self):
        with self.assertRaises(ValidationError):
            self.finalize(expected_preparation_revision=100)
        ExecutionPreparation.objects.filter(pk=self.prep.pk).update(status="cancelled")
        with self.assertRaisesMessage(ValidationError, "已取消"):
            self.finalize()
        self.collector.assert_not_called()


@override_settings(AI_PROVIDER_ALLOWED_HOSTS=("api.example.com", "cdn.example.com"))
class ExecutionProfileFinalizationTests(TestCase):
    setUpTestData = classmethod(profile_fixtures.ExecutionProfilePreparationTests.setUpTestData.__func__)
    write_contract = staticmethod(profile_fixtures.ExecutionProfilePreparationTests.write_contract)
    input = staticmethod(profile_fixtures.ExecutionProfilePreparationTests.input)
    bind = profile_fixtures.ExecutionProfilePreparationTests.bind
    resolve = profile_fixtures.ExecutionProfilePreparationTests.resolve
    prepare = profile_fixtures.ExecutionProfilePreparationTests.prepare
    selected_text = profile_fixtures.ExecutionProfilePreparationTests.selected_text
    collect = ExecutionFinalizationTests.collect

    def setUp(self):
        profile_fixtures.ExecutionProfilePreparationTests.setUp(self)
        self.prepare()
        now = timezone.now()
        self.smoke_facts = (ExecutionSmokeFact(ExecutionEvidenceIdentity(str(self.version.pk), self.definition.sha256,
            self.definition.value["target_fingerprint"], self.version.operation, self.version.mode, "instance_smoke"),
            "isolated-profile-policy-fixture", "succeeded", now - timedelta(minutes=2), now + timedelta(minutes=20)),)

    def finalize(self):
        self.prep.refresh_from_db()
        return finalize_execution(preparation_id=self.prep.pk, expected_preparation_revision=self.prep.revision,
            actor=self.actor, collect_admission_facts=self.collect)

    def test_selected_profile_field_and_approval_are_frozen_once(self):
        value = self.selected_text("A courier opens a blue parcel in soft daylight.")
        ready = self.finalize()
        payload = ready.contract.prepared_request
        self.assertEqual(ready.status, "ready")
        self.assertIn(value.text, payload["transport"]["payload"]["prompt"])
        selected = payload["field_selections"]["fields"][0]
        self.assertEqual(selected["field_value_id"], str(value.pk))
        self.assertEqual(selected["approval"]["kind"], "human")
        ready.refresh_from_db()
        self.assertEqual(ready.contract.prepared_request, payload)
        from studio.services.execution_prompt_display import read_preparation_prompt_display
        with patch("studio.services.execution_prompt_display._compile_preparation_profile", side_effect=AssertionError("Frozen means saved, not recompiled")):
            display = read_preparation_prompt_display(preparation_id=ready.pk, expected_preparation_revision=ready.revision, actor=self.actor)
        self.assertEqual(display["mode"], "frozen")
        self.assertEqual(display["request_sha256"], ready.request_sha256)
        self.assertEqual(display["channels"][0]["text"], payload["transport"]["payload"]["prompt"])
        self.assertEqual(display["field_basis"][0]["field_value_id"], str(value.pk))
        # All ordinary post-freeze field writes continue to fail.
        from studio.services.execution_preparation_fields import create_manual_execution_field
        with self.assertRaises(ValidationError):
            create_manual_execution_field(preparation_id=ready.pk, unit_key="asset-unit", field_key="composite_direction",
                text="A different approved-looking phrase.", revision=2, expected_preparation_revision=ready.revision, actor=self.actor)

    def test_unselected_profile_field_is_not_automatically_generated_or_adopted(self):
        with self.assertRaisesMessage(ValidationError, "尚未明确采用"):
            self.finalize()
        self.prep.refresh_from_db()
        self.assertEqual(self.prep.status, "preparing")
        self.assertIsNone(self.prep.request_sha256)
        self.assertFalse(self.prep.field_values.exists())

    def test_prompt_display_rejects_corrupt_saved_manifest_without_rebuilding(self):
        from io import BytesIO
        from django.core.files.storage import default_storage
        from studio.services.execution_prompt_display import read_preparation_prompt_display
        self.selected_text("A courier opens a blue parcel in soft daylight.")
        ready = self.finalize()
        key = ready.contract.prepared_request_key
        original_open = default_storage.open
        def corrupt_saved_file(name, *args, **kwargs):
            return BytesIO(b"{}") if name == key else original_open(name, *args, **kwargs)
        with patch.object(default_storage, "open", side_effect=corrupt_saved_file), patch(
                "studio.services.execution_prompt_display._compile_preparation_profile", side_effect=AssertionError("No fallback to current text")):
            with self.assertRaises(ValidationError):
                read_preparation_prompt_display(preparation_id=ready.pk, expected_preparation_revision=ready.revision, actor=self.actor)
