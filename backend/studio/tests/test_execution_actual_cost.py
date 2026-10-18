"""Billing receipts are facts, not estimates or inferred token charges."""
from decimal import Decimal
from types import SimpleNamespace
from unittest.mock import patch

from django.core.exceptions import ValidationError
from django.test import SimpleTestCase

from studio.services.asset_generation import _collected_actual_cost
from studio.services.provider_registry import ProviderDriverError


class ExecutionActualCostTests(SimpleTestCase):
    def setUp(self):
        self.job = SimpleNamespace(estimated_cost=Decimal("0.1"), cost_currency="USD")
        self.attested = self.enterContext(patch("studio.services.asset_generation._latest_attested_resolution_cost", return_value=(None, "")))

    def test_no_bill_does_not_use_estimate_price_currency_or_token_usage(self):
        for collected in ({}, {"actual_cost": None, "currency": "USD", "usage": {"total_tokens": 42}}):
            with self.subTest(collected=collected):
                self.assertEqual(_collected_actual_cost(self.job, collected), (None, ""))

    def test_provider_bill_requires_amount_and_own_currency_including_zero(self):
        self.assertEqual(_collected_actual_cost(self.job, {"actual_cost": "0", "currency": "USD"}), (Decimal("0"), "USD"))
        for amount in (True, "", "NaN", "Infinity", "-1", "100000000"):
            with self.subTest(amount=amount), self.assertRaises(ValidationError):
                _collected_actual_cost(self.job, {"actual_cost": amount, "currency": "USD"})
        for currency in (None, "", "US Dollar", "USD_TOO_LONG_CURRENCY", "usd"):
            with self.subTest(currency=currency), self.assertRaises(ValidationError):
                _collected_actual_cost(self.job, {"actual_cost": "0", "currency": currency})

    def test_attested_bill_is_used_when_provider_is_silent_and_conflicts_are_explicit(self):
        self.attested.return_value = (Decimal("0.02"), "USD")
        self.assertEqual(_collected_actual_cost(self.job, {}), (Decimal("0.02"), "USD"))
        self.assertEqual(_collected_actual_cost(self.job, {"actual_cost": "0.02", "currency": "USD"}), (Decimal("0.02"), "USD"))
        for collected in ({"actual_cost": "0.01", "currency": "USD"}, {"actual_cost": "0.02", "currency": "EUR"}):
            with self.subTest(collected=collected), self.assertRaises(ProviderDriverError) as caught:
                _collected_actual_cost(self.job, collected)
            self.assertEqual(caught.exception.code, "actual_cost_reconciliation_required")

    def test_no_automatic_conversion_of_provider_bill(self):
        with self.assertRaises(ProviderDriverError) as caught:
            _collected_actual_cost(self.job, {"actual_cost": "0", "currency": "EUR"})
        self.assertEqual(caught.exception.code, "actual_cost_currency_conflict")
