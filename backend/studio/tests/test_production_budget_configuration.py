from decimal import Decimal
from types import SimpleNamespace
from unittest.mock import Mock, patch

from django.core.exceptions import ValidationError
from django.test import SimpleTestCase

from studio.services.production_budget_configuration import validate_budget_currency
from studio.services.execution_production_admission import production_budget_state
from studio.services.execution_production_dispatch import _reserve


class ProductionBudgetAccountingTests(SimpleTestCase):
    def test_zero_actual_media_bill_preserves_run_currency_and_does_not_double_settle(self):
        from studio.services.async_media_queue import _settle_actual_bill
        from studio.services.provider_registry import ProviderDriverError
        run = SimpleNamespace(actual_cost_total=Decimal("0.2"), committed_cost_total=Decimal("0"), cost_currency="USD", save=Mock())
        attempt = SimpleNamespace(actual_cost=None, committed_cost=Decimal("0"), cost_currency="CNY", save=Mock())
        _settle_actual_bill(attempt, run, Decimal("0"), "CNY")
        _settle_actual_bill(attempt, run, Decimal("0"), "CNY")
        self.assertEqual(run.cost_currency, "USD")
        self.assertEqual(run.actual_cost_total, Decimal("0.2"))
        self.assertEqual(run.save.call_count, 1)
        with self.assertRaises(ProviderDriverError):
            _settle_actual_bill(attempt, run, Decimal("0.1"), "CNY")

    def test_amount_requires_currency_and_never_infers_usd(self):
        for amount, currency in (("1", ""), (None, "USD"), ("NaN", "USD"), ("-1", "USD"), ("1.00001", "USD")):
            with self.subTest(amount=amount, currency=currency), self.assertRaises(ValidationError):
                validate_budget_currency(amount, currency)
        validate_budget_currency(None, "")
        validate_budget_currency("0", "USD")

    def test_free_calls_keep_declared_budget_currency_but_paid_mismatches_are_blocked(self):
        run = SimpleNamespace(actual_cost_total=Decimal("0.1"), committed_cost_total=Decimal("0"),
            estimated_cost_total=Decimal("0.1"), cost_currency="USD",
            allow_external_calls=False, save=Mock())
        with patch("studio.services.execution_resource_policy.read_resource_policy", return_value=SimpleNamespace(value={"values": {"budget_by_currency": {"USD": "1"}}})), \
                patch("studio.services.asset_generation._is_external", return_value=False), \
                patch("studio.services.execution_production_admission.estimate_execution_request") as estimate:
            for amount, currency, expected in (("0", "", []), ("0", "CNY", []),
                    ("0.2", "CNY", ["execution_budget_currency_conflict"]), (None, "USD", ["execution_estimate_unknown"])):
                estimate.return_value = SimpleNamespace(value={"amount": amount, "currency": currency})
                result = production_budget_state(run, object(), object(), None)
                self.assertEqual([row["code"] for row in result["blockers"]], expected)
                if amount == "0":
                    self.assertEqual(result["currency"], "USD")
                    self.assertEqual(result["budget_limit"], "1")
                    self.assertEqual(result["estimate"]["currency"], currency)
        attempt = SimpleNamespace(committed_cost=None, estimated_cost=Decimal("0"), cost_currency="", save=Mock())
        _reserve(run, attempt)
        self.assertEqual(run.cost_currency, "USD")
        self.assertEqual(run.committed_cost_total, 0)
