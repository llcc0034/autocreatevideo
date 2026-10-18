"""Length rejection remains fatal to execution but readable by the owner."""
from unittest.mock import patch

from django.core.exceptions import ValidationError
from django.db import connection
from django.test import TestCase, override_settings
from rest_framework.test import APIRequestFactory, force_authenticate

from studio.execution_preparation_views import ExecutionPreparationViewSet
from studio.services.execution_finalization import finalize_execution
from studio.services.execution_prompt_display import read_preparation_prompt_display
from studio.services.prompt_profiles import PromptProfileChannelLengthError
from studio.tests import test_execution_profile_preparation as direct, test_execution_reference_preparation as reference


def readonly(execute, sql, params, many, context):
    assert sql.lstrip().upper().startswith(("SELECT", "SAVEPOINT", "RELEASE", "ROLLBACK")), sql
    return execute(sql, params, many, context)


def assert_diagnostic(test, result):
    test.assertEqual(result["mode"], "diagnostic")
    test.assertIsNone(result["request_sha256"])
    test.assertIsNone(result["frozen_at"])
    test.assertTrue(result["issues"])
    for channel, length in zip(result["channels"], result["length_diagnostics"]):
        test.assertEqual(channel["character_count"], len(channel["text"]))
        test.assertEqual(channel["key"], length["channel"])
        test.assertEqual(sum(f["text_chars"] + f["separator_chars"] for f in length["fragments"]), len(channel["text"]))
        if length["max_chars"] is not None:
            test.assertEqual(length["over_by"], max(0, len(channel["text"]) - length["max_chars"]))


@override_settings(AI_PROVIDER_ALLOWED_HOSTS=("api.example.com", "cdn.example.com"))
class PromptLengthDiagnosticTests(TestCase):
    channel_limits = {"positive_prompt": {"max_chars": 20, "warn_at_chars": 15}}
    setUpTestData = classmethod(direct.ExecutionProfilePreparationTests.setUpTestData.__func__)
    setUp = direct.ExecutionProfilePreparationTests.setUp
    write_contract = staticmethod(direct.ExecutionProfilePreparationTests.write_contract)
    input = staticmethod(direct.ExecutionProfilePreparationTests.input)
    bind = direct.ExecutionProfilePreparationTests.bind
    resolve = direct.ExecutionProfilePreparationTests.resolve
    prepare = direct.ExecutionProfilePreparationTests.prepare
    selected_text = direct.ExecutionProfilePreparationTests.selected_text
    candidate = direct.ExecutionProfilePreparationTests.candidate
    display = direct.ExecutionProfilePreparationTests.display
    preview = direct.ExecutionProfilePreparationTests.preview

    def test_selected_over_limit_is_full_readonly_diagnostic_and_cannot_finalize(self):
        self.prepare()
        value = self.selected_text("A courier opens a blue parcel in soft daylight. 😀")
        self.prep.refresh_from_db()
        before = (self.prep.revision, self.prep.status, self.prep.field_selections.count())
        with connection.execute_wrapper(readonly):
            result = self.display()
        assert_diagnostic(self, result)
        self.assertIn(value.text, next(row["text"] for row in result["channels"] if row["key"] == "positive_prompt"))
        self.assertEqual(result["field_basis"][0]["origin"], "adopted")
        with self.assertRaises(PromptProfileChannelLengthError):
            finalize_execution(preparation_id=self.prep.pk, expected_preparation_revision=self.prep.revision,
                actor=self.actor, collect_admission_facts=lambda *_: self.fail("No admission after length failure"))
        self.prep.refresh_from_db()
        self.assertEqual(before, (self.prep.revision, self.prep.status, self.prep.field_selections.count()))
        self.assertIsNone(self.prep.request_frozen_at)
        self.assertFalse(self.prep.generation_attempts.exists())

    def test_api_returns_rejected_candidate_without_adoption_or_retry(self):
        self.prepare()
        value = self.candidate("A courier opens a blue parcel in soft daylight.")
        self.prep.refresh_from_db()
        request = APIRequestFactory().get("/prompt-display/", {"expected_preparation_revision": self.prep.revision})
        force_authenticate(request, self.actor)
        with connection.execute_wrapper(readonly):
            response = ExecutionPreparationViewSet.as_view({"get": "prompt_display"})(request, pk=self.prep.pk)
        self.assertEqual(response.status_code, 200)
        assert_diagnostic(self, response.data)
        self.assertIn(value.text, next(row["text"] for row in response.data["channels"] if row["key"] == "positive_prompt"))
        self.assertEqual(response.data["field_basis"][0]["origin"], "candidate")
        self.assertFalse(self.prep.field_selections.exists())

    def test_other_invalid_sources_are_not_disguised_as_length_diagnostics(self):
        self.prepare()
        self.candidate("A courier opens a blue parcel in soft daylight.")
        with patch("studio.services.execution_prompt_display._compile_preparation_profile", side_effect=ValidationError("source corrupt")):
            with self.assertRaisesMessage(ValidationError, "source corrupt"):
                self.display()


@override_settings(AI_PROVIDER_ALLOWED_HOSTS=("api.example.com", "127.0.0.1"))
class ReferencePromptLengthDiagnosticTests(TestCase):
    channel_limits = {"positive_prompt": {"max_chars": 20, "warn_at_chars": None}}
    setUp = reference.ExecutionReferencePreparationTests.setUp
    input = staticmethod(reference.ExecutionReferencePreparationTests.input)
    write = reference.ExecutionReferencePreparationTests.write
    resolve = reference.ExecutionReferencePreparationTests.resolve
    prepare = reference.ExecutionReferencePreparationTests.prepare
    select = reference.ExecutionReferencePreparationTests.select
    preview = reference.ExecutionReferencePreparationTests.preview

    def test_reference_assembly_preserves_system_sections_and_adopted_contributions(self):
        self.prepare()
        first = self.select("first", "A courier arrives beside a blue doorway.")
        second = self.select("second", "A red parcel opens on the table.")
        self.prep.refresh_from_db()
        with connection.execute_wrapper(readonly):
            result = read_preparation_prompt_display(preparation_id=self.prep.pk,
                expected_preparation_revision=self.prep.revision, actor=self.user)
        assert_diagnostic(self, result)
        text = next(row["text"] for row in result["channels"] if row["key"] == "positive_prompt")
        self.assertIn(first.text, text)
        self.assertIn(second.text, text)
        self.assertGreater(len(text), len(first.text) + len(second.text))
        with self.assertRaises(PromptProfileChannelLengthError) as rejected:
            self.preview()
        self.assertEqual(text, rejected.exception.diagnostic["channels"]["positive_prompt"])
