"""Original root/child accounting facts; isolated prices and no paid requests."""
from decimal import Decimal
from unittest.mock import patch

from django.core.exceptions import ValidationError
from django.db import connection
from django.test import TestCase, override_settings
from django.utils import timezone

from studio.models import AssetGenerationJob
from studio.services.execution_asset_budget import estimate_execution_request, read_asset_budget_state, asset_budget_totals_query
from studio.services.execution_preparation_owners import locked_preparation_owners
from studio.services.execution_preparation_requests import compile_prepared_execution_transport
from studio.services.json_contracts import json_contract
from studio.tests import test_execution_asset_auxiliaries as fixtures


@override_settings(AI_PROVIDER_ALLOWED_HOSTS=("api.example.com", "cdn.example.com"))
class ExecutionAssetBudgetTests(TestCase):
    setUpTestData = fixtures.ExecutionAssetAuxiliaryTests.__dict__["setUpTestData"]
    setUp = fixtures.ExecutionAssetAuxiliaryTests.setUp
    write_contract = staticmethod(fixtures.ExecutionAssetAuxiliaryTests.write_contract)
    input = staticmethod(fixtures.ExecutionAssetAuxiliaryTests.input)
    bind = fixtures.ExecutionAssetAuxiliaryTests.bind
    child = fixtures.ExecutionAssetAuxiliaryTests.child

    def price(self, amount="0.4", currency="USD"):
        return json_contract({"schema_version": 1, "transport_sha256": "a" * 64, "amount": amount, "currency": currency, "basis": "isolated_price_fixture"})

    def budget(self, job, **changes):
        with locked_preparation_owners([("asset_job_id", job.pk)], self.actor) as owners:
            self.assertIn(("asset_job_id", str(self.root.pk)), owners)
            job = owners[("asset_job_id", str(job.pk))]
            return read_asset_budget_state(job, self.price(**changes)).value

    def test_current_estimate_uses_existing_driver_with_the_exact_compiled_payload(self):
        child, _ = self.child()
        prep = child.execution_preparations.get()
        transport = compile_prepared_execution_transport(preparation_id=prep.pk, expected_preparation_revision=prep.revision, actor=self.actor).transport
        with patch("studio.services.execution_asset_budget.driver_for") as driver:
            driver.return_value.estimate.return_value = {"schema_version": 1, "amount": "0.02", "currency": "USD", "basis": "configured_fixed"}
            estimate = estimate_execution_request(child.model_deployment, transport)
            self.assertEqual(estimate.value["transport_sha256"], transport.sha256)
            driver.return_value.estimate.assert_called_once_with(child.model_deployment, child.operation, transport.value["payload"])
            driver.return_value.invoke.assert_not_called()
            for invalid in (True, "NaN", "Infinity", "-1", "100000000"):
                driver.return_value.estimate.return_value["amount"] = invalid
                with self.subTest(amount=invalid), self.assertRaises(ValidationError):
                    estimate_execution_request(child.model_deployment, transport)

    def test_parent_total_counts_each_auxiliary_once_and_reserves_uncertain_cost(self):
        first, _ = self.child()
        AssetGenerationJob.objects.filter(pk=first.pk).update(status="failed", actual_cost=Decimal("0.2"), estimated_cost=Decimal("0.8"), cost_currency="USD")
        second, _ = self.child(attempt_number=2)
        AssetGenerationJob.objects.filter(pk=second.pk).update(status="uncertain", estimated_cost=Decimal("0.3"), cost_currency="USD", submission_started_at=timezone.now())
        result = self.budget(self.root, amount="0.6")
        self.assertEqual(Decimal(result["projected_total"]), Decimal("1.1"))
        # Accounting reports exposure; system review owns consent, not a
        # terminal per-job budget gate.
        self.assertEqual(result["blockers"], [])
        self.root.refresh_from_db()
        self.assertIsNone(self.root.actual_cost)
        self.assertEqual(Decimal(result["prior_totals"][0]["amount"]), Decimal("0.5"))

    def test_rechecking_same_child_replaces_its_reservation_instead_of_double_counting(self):
        child, _ = self.child()
        AssetGenerationJob.objects.filter(pk=child.pk).update(status="queued", estimated_cost=Decimal("0.4"), cost_currency="USD")
        first, second = self.budget(child), self.budget(child)
        self.assertEqual(first, second)
        self.assertEqual(Decimal(first["projected_total"]), Decimal("0.4"))
        self.assertEqual(first["blockers"], [])

    def test_successful_child_without_bill_keeps_estimate_reserved_not_actual(self):
        child, _ = self.child()
        AssetGenerationJob.objects.filter(pk=child.pk).update(status="succeeded", estimated_cost=Decimal("0.3"), cost_currency="USD")
        result = self.budget(self.root)
        self.assertEqual(Decimal(result["projected_total"]), Decimal("0.7"))
        child.refresh_from_db()
        self.assertIsNone(child.actual_cost)
        self.assertEqual(child.estimated_cost, Decimal("0.3"))

    def test_unknown_cost_and_different_currency_are_not_zero_or_implicitly_converted(self):
        child, _ = self.child()
        AssetGenerationJob.objects.filter(pk=child.pk).update(status="succeeded", cost_currency="USD")
        result = self.budget(self.root)
        self.assertIn("execution_prior_cost_unknown", {row["code"] for row in result["blockers"]})
        self.assertIsNone(result["projected_total"])
        AssetGenerationJob.objects.filter(pk=child.pk).update(actual_cost=Decimal("0.2"))
        result = self.budget(self.root, currency="EUR")
        self.assertIn("execution_budget_currency_conflict", {row["code"] for row in result["blockers"]})
        self.assertIsNone(result["projected_total"])
        result = self.budget(self.root, amount=None)
        self.assertIn("execution_estimate_unknown", {row["code"] for row in result["blockers"]})

    def test_pre_submission_failure_releases_only_unused_reservation_and_uses_root_authorization(self):
        child, _ = self.child()
        AssetGenerationJob.objects.filter(pk=child.pk).update(status="failed", estimated_cost=Decimal("0.9"), cost_currency="USD")
        self.assertEqual(Decimal(self.budget(self.root)["projected_total"]), Decimal("0.4"))
        second, _ = self.child(attempt_number=2)
        AssetGenerationJob.objects.filter(pk=self.root.pk).update(allow_external_calls=False, budget_limit=None)
        result = self.budget(second)
        self.assertEqual({row["code"] for row in result["blockers"]}, {"execution_external_authorization_required"})

    def test_aggregate_only_reads_cost_columns_and_uses_parent_index(self):
        self.child()
        query = asset_budget_totals_query(self.root, self.prep, self.root.pk)
        sql = str(query.query)
        self.assertNotIn('"request_snapshot"', sql)
        self.assertNotIn('"result_text"', sql)
        self.assertEqual(query.get()["count"], 1)
        if connection.vendor == "sqlite":
            plan = query.explain()
            self.assertIn("INDEX", plan)
            self.assertNotIn("SCAN studio_assetgenerationjob", plan)
