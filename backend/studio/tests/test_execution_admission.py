from dataclasses import replace
from datetime import timedelta

from django.core.exceptions import ValidationError
from django.test import SimpleTestCase
from django.utils import timezone

from studio.services.execution_admission import ExecutionEvidenceIdentity, ExecutionSmokeFact, evaluate_execution_admission


class ExecutionAdmissionPolicyTests(SimpleTestCase):
    """Pure policy fixtures are not generated Smoke evidence or live results."""

    def setUp(self):
        self.now = timezone.now()
        self.identity = ExecutionEvidenceIdentity("version", "d" * 64, "t" * 64, "video.reference_to_video", "Ref2VA", "instance_smoke")
        self.passed = ExecutionSmokeFact(self.identity, "actual-attempt-id", "succeeded", self.now - timedelta(hours=2), self.now + timedelta(hours=1))

    def decide(self, facts=None, **changes):
        return evaluate_execution_admission(**{"identity": self.identity, "smoke_facts": [self.passed] if facts is None else facts,
            "purpose": "production", "smoke_policy": "require_passed", "capacity_policy": "warn",
            "capacity_state": "unknown", "hard_blockers": [], "now": self.now, **changes})

    def test_exact_fresh_pass_with_unknown_capacity_warns_without_fake_coverage(self):
        result = self.decide()
        self.assertTrue(result["can_submit"])
        self.assertEqual(result["evidence_state"], "fresh")
        self.assertEqual(result["capacity_state"], "unknown")
        self.assertEqual(result["warnings"][0]["code"], "execution_capacity_unknown")

    def test_expiration_is_policy_controlled_and_exact_boundary_is_expired(self):
        expired = replace(self.passed, expires_at=self.now)
        result = self.decide([expired])
        self.assertTrue(result["can_submit"])
        self.assertEqual(result["evidence_state"], "expired")
        self.assertEqual(result["evidence_attempt_id"], expired.attempt_id)
        strict = self.decide([expired], smoke_policy="require_fresh")
        self.assertFalse(strict["can_submit"])
        self.assertEqual(strict["blockers"][0]["code"], "execution_smoke_expired")

    def test_no_policy_can_treat_missing_or_latest_failed_as_passed(self):
        failed = replace(self.passed, attempt_id="failed-later", status="failed", finished_at=self.now - timedelta(minutes=2), expires_at=None)
        for policy in ("require_passed", "require_fresh"):
            self.assertFalse(self.decide([], smoke_policy=policy)["can_submit"])
            result = self.decide([failed, self.passed], smoke_policy=policy)
            self.assertEqual(result["evidence_state"], "failed")
            self.assertFalse(result["can_submit"])

    def test_different_definition_target_operation_mode_version_and_scope_do_not_match(self):
        for field in ("definition_sha256", "target_fingerprint", "operation", "mode", "version_id", "scope"):
            with self.subTest(field=field):
                result = self.decide([replace(self.passed, identity=replace(self.identity, **{field: "different"}))])
                self.assertEqual(result["evidence_state"], "missing")
                self.assertFalse(result["can_submit"])

    def test_nonterminal_cancelled_or_uncertain_is_not_a_new_failed_result(self):
        for status in ("queued", "preparing", "running", "cancel_requested", "cancelled", "uncertain"):
            fact = replace(self.passed, attempt_id="unfinished", status=status, finished_at=None, expires_at=None)
            result = self.decide([self.passed, fact])
            self.assertEqual(result["evidence_state"], "fresh")

    def test_main_smoke_can_sample_unknown_capacity_without_claiming_coverage(self):
        for state in ("unknown", "uncovered"):
            for policy in ("warn", "require_evidence"):
                with self.subTest(state=state, policy=policy):
                    result = self.decide([], purpose="instance_smoke", capacity_state=state, capacity_policy=policy)
                    self.assertTrue(result["own_smoke_gate_exempt"])
                    self.assertEqual(result["own_capacity_gate_exempt"], policy == "require_evidence")
                    self.assertEqual(result["evidence_state"], "missing")
                    self.assertEqual(result["capacity_state"], state)
                    self.assertIsNone(result["evidence_attempt_id"])
                    self.assertTrue(result["can_submit"])
                    self.assertEqual(result["warnings"][0]["code"], f"execution_capacity_{state}")
        covered = self.decide([], purpose="instance_smoke", capacity_state="covered", capacity_policy="require_evidence")
        self.assertFalse(covered["own_capacity_gate_exempt"])
        self.assertEqual(covered["warnings"], [])

    def test_main_smoke_sampling_never_waives_hard_rules(self):
        for code in ("budget_exceeded", "permission_denied", "input_contract_invalid", "remote_uncertain"):
            blocked = self.decide([], purpose="instance_smoke", capacity_policy="require_evidence",
                hard_blockers=[{"code": code, "message": "Explicit hard blocker"}])
            self.assertFalse(blocked["can_submit"])
            self.assertEqual(blocked["configuration_state"], "blocked")
            self.assertEqual(blocked["blockers"][0]["code"], code)

    def test_resource_smoke_is_explicitly_scoped_and_does_not_borrow_instance_coverage(self):
        identity = replace(self.identity, version_id=None, scope="resource_smoke")
        result = self.decide([], identity=identity, purpose="resource_smoke", capacity_policy="require_evidence")
        self.assertTrue(result["can_submit"])
        self.assertTrue(result["own_smoke_gate_exempt"])
        self.assertTrue(result["own_capacity_gate_exempt"])
        self.assertEqual(result["capacity_state"], "unknown")
        self.assertIsNone(result["evidence_attempt_id"])
        blocked = self.decide([], identity=identity, purpose="resource_smoke",
            hard_blockers=[{"code": "budget_exceeded", "message": "Explicit budget denial"}])
        self.assertFalse(blocked["can_submit"])
        for changes in ({"purpose": "production", "identity": identity},
                {"purpose": "instance_smoke", "identity": identity},
                {"purpose": "resource_smoke"},
                {"purpose": "resource_smoke", "identity": identity, "capacity_state": "covered"}):
            with self.subTest(changes=changes), self.assertRaises(ValidationError):
                self.decide([], **changes)
        with self.assertRaises(ValidationError):
            self.decide([self.passed], identity=identity, purpose="resource_smoke")

    def test_known_uncovered_capacity_obeys_policy_without_changing_smoke(self):
        for state in ("uncovered", "unknown"):
            self.assertTrue(self.decide(capacity_state=state)["can_submit"])
            strict = self.decide(capacity_state=state, capacity_policy="require_evidence")
            self.assertFalse(strict["can_submit"])
            self.assertFalse(strict["own_capacity_gate_exempt"])
        self.assertTrue(self.decide(capacity_state="covered", capacity_policy="require_evidence")["can_submit"])

    def test_missing_invalid_config_or_malformed_evidence_never_silently_defaults(self):
        for changes in ({"smoke_policy": None}, {"smoke_policy": "warn"}, {"capacity_policy": None}, {"capacity_state": "certified"},
                {"now": self.now.replace(tzinfo=None)}, {"hard_blockers": None}):
            with self.subTest(changes=changes), self.assertRaises(ValidationError):
                self.decide(**changes)
        for fact in (replace(self.passed, expires_at=None), replace(self.passed, finished_at=self.now + timedelta(minutes=1)),
                replace(self.passed, status="passed"), replace(self.passed, expires_at=self.passed.finished_at)):
            with self.assertRaises(ValidationError):
                self.decide([fact])
        with self.assertRaises(ValidationError):
            self.decide([self.passed, self.passed])
