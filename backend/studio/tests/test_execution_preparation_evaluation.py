from unittest.mock import patch

from django.contrib.auth import get_user_model
from django.core.exceptions import PermissionDenied, ValidationError
from django.db import IntegrityError, transaction
from django.test import TestCase, override_settings
from django.utils import timezone

from studio.models import ExecutionPreparation
from studio.services.execution_preparation_evaluation import evaluate_preparation_fields
from studio.services.execution_preparation_fields import create_manual_execution_field, confirm_execution_field
from studio.services.execution_preparations import read_execution_preparation_detail
from studio.tests import test_execution_profile_preparation as fixtures


@override_settings(AI_PROVIDER_ALLOWED_HOSTS=("api.example.com", "cdn.example.com"))
class ExecutionPreparationEvaluationTests(TestCase):
    setUpTestData = classmethod(fixtures.ExecutionProfilePreparationTests.setUpTestData.__func__)
    write_contract = staticmethod(fixtures.ExecutionProfilePreparationTests.write_contract)
    input = staticmethod(fixtures.ExecutionProfilePreparationTests.input)
    bind = fixtures.ExecutionProfilePreparationTests.bind
    resolve = fixtures.ExecutionProfilePreparationTests.resolve
    prepare = fixtures.ExecutionProfilePreparationTests.prepare

    def setUp(self):
        fixtures.ExecutionProfilePreparationTests.setUp(self)
        self.prepare()

    def evaluate(self, **changes):
        self.prep.refresh_from_db()
        self.prep = evaluate_preparation_fields(**{"preparation_id": self.prep.pk,
            "expected_preparation_revision": self.prep.revision, "actor": self.actor, **changes})
        return read_execution_preparation_detail(self.prep).evaluation

    def create_field(self):
        self.prep.refresh_from_db()
        return create_manual_execution_field(preparation_id=self.prep.pk, unit_key="asset-unit", field_key="composite_direction",
            text="A courier opens a blue parcel.", revision=1, expected_preparation_revision=self.prep.revision, actor=self.actor)

    def confirm(self, value, *, select=False):
        self.prep.refresh_from_db()
        return confirm_execution_field(preparation_id=self.prep.pk, field_value_id=value.pk,
            expected_preparation_revision=self.prep.revision, expected_text_sha256=value.text_sha256,
            expected_source_sha256=value.source_sha256, select_for_execution=select, replace=False,
            expected_selection_revision=None, actor=self.actor)

    def test_input_review_selection_are_distinct_and_complete_fields_are_not_ready(self):
        result = self.evaluate()
        self.assertEqual(self.prep.status, "awaiting_input")
        self.assertEqual(result["outstanding_fields"][0]["action"], "input")
        field = self.create_field()
        self.prep.refresh_from_db()
        self.assertEqual(self.prep.status, "preparing")
        self.assertLess(self.prep.evaluation_revision, self.prep.revision)
        self.assertEqual(self.prep.reason_code, "")
        result = self.evaluate()
        self.assertEqual(self.prep.status, "awaiting_review")
        self.assertEqual(result["outstanding_fields"][0]["action"], "review")
        self.confirm(field)
        self.assertEqual(self.evaluate()["outstanding_fields"][0]["action"], "select")
        self.confirm(field, select=True)
        result = self.evaluate()
        self.assertEqual(result["outstanding_fields"], [])
        self.assertEqual(self.prep.status, "preparing")
        self.assertEqual(result["selected_count"], 1)
        self.assertIsNone(self.prep.request_frozen_at)
        self.assertIsNone(self.prep.request_sha256)

    def test_repeated_evaluation_is_not_an_endless_revision_bump(self):
        first = self.evaluate()
        revision = self.prep.revision
        self.assertEqual(self.evaluate(), first)
        self.assertEqual(self.prep.revision, revision)
        self.assertEqual(first["basis_preparation_revision"], revision - 1)
        self.assertEqual(first["preparation_revision"], revision)

    def test_no_get_side_effect_and_detail_matches_revision_and_time(self):
        self.evaluate()
        revision = self.prep.revision
        before = read_execution_preparation_detail(self.prep).evaluation
        self.assertEqual(before["evaluated_at"], self.prep.evaluated_at.isoformat())
        self.assertEqual(before, read_execution_preparation_detail(self.prep).evaluation)
        self.prep.refresh_from_db()
        self.assertEqual(self.prep.revision, revision)
        ExecutionPreparation.objects.filter(pk=self.prep.pk).update(evaluation_revision=revision - 1)
        self.prep.refresh_from_db()
        with self.assertRaises(ValidationError):
            read_execution_preparation_detail(self.prep)

    def test_owner_stale_revision_and_cancelled_preparation_are_rejected(self):
        other = get_user_model().objects.create_user(username="other-evaluation-owner")
        with self.assertRaises(PermissionDenied):
            self.evaluate(actor=other)
        with self.assertRaises(ValidationError):
            self.evaluate(expected_preparation_revision=0)
        self.prep.status = "cancelled"
        self.prep.save(update_fields=("status",))
        with self.assertRaises(ValidationError):
            self.evaluate()

    def test_detail_failure_rolls_back_the_index_and_status(self):
        revision = self.prep.revision
        with patch("studio.services.execution_preparation_evaluation.assign_json_contract", side_effect=RuntimeError("storage failure")):
            with self.assertRaises(RuntimeError):
                self.evaluate()
        self.prep.refresh_from_db()
        self.assertEqual(self.prep.revision, revision)
        self.assertEqual(self.prep.status, "preparing")
        self.assertIsNone(self.prep.evaluation_revision)

    def test_database_requires_complete_evaluation_index(self):
        for changes in ({"evaluated_at": timezone.now()}, {"evaluation_revision": 1},
                {"evaluation_revision": 2, "evaluated_at": timezone.now()}):
            with self.subTest(changes=changes), self.assertRaises(IntegrityError), transaction.atomic():
                ExecutionPreparation.objects.filter(pk=self.prep.pk).update(**changes)
