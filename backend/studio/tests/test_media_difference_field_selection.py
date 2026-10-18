"""Only the exact field used as a variant difference is required for consumption."""
from copy import deepcopy

from django.core.exceptions import ValidationError
from django.test import override_settings

from studio.models import GenerationAttempt
from studio.services.execution_preparation_evaluation import evaluate_preparation_fields
from studio.services.execution_preparation_fields import read_execution_field_selection_snapshot
from studio.services.media_generation_targets import read_media_generation_target
from studio.tests import test_media_appearance_profile_images as fixtures
from studio.tests.test_data_structures import StructureFixture


@override_settings(AI_PROVIDER_ALLOWED_HOSTS=("127.0.0.1", "api.example.com"))
class OptionalDifferenceSelectionTests(StructureFixture):
    base = fixtures.ProfileImageAppearanceTests
    setUp, create_job, primary_version, write = base.setUp, base.create_job, base.primary_version, base.write
    input = staticmethod(base.input)
    request, job, preview, select = base.request, base.job, base.preview, base.select
    contribution_required = False

    def snapshot(self):
        self.prep.refresh_from_db()
        return read_execution_field_selection_snapshot(preparation_id=self.prep.pk,
            expected_preparation_revision=self.prep.revision, actor=self.user).value

    def tearDown(self):
        self.external.assert_not_called()
        self.assertFalse(GenerationAttempt.objects.exists())

    def test_optional_reference_difference_waits_for_exact_choice_and_preserves_original_facts(self):
        job, _ = self.job("reference_field", "optional-difference")
        original = deepcopy(self.prep.contract.resolved_inputs)
        appearance = read_media_generation_target(self.prep).value
        self.assertEqual({(row["unit_key"], row["required"]) for row in self.snapshot()["unselected_fields"]},
            {("first", False), ("second", True)})
        evaluated = evaluate_preparation_fields(preparation_id=self.prep.pk,
            expected_preparation_revision=self.prep.revision, actor=self.user)
        self.assertEqual(evaluated.status, "awaiting_input")
        self.assertEqual(evaluated.contract.evaluation["outstanding_fields"][0]["unit_key"], "second")
        # Selecting a sibling with the same field key cannot satisfy this use.
        self.select("first", "Keep the parcel beside the blue box.")
        with self.assertRaisesMessage(ValidationError, "尚未明确采用"):
            self.preview()
        chosen = self.select("second", "Open the red parcel while preserving its shape.")
        rendered = self.preview().resolved_channels["values"]["positive_prompt"]
        self.assertIn(chosen.text, rendered)
        self.assertEqual(self.snapshot()["unselected_fields"], [])
        replay, created = self.job("reference_field", "optional-difference")
        self.assertFalse(created)
        self.assertEqual(replay.pk, job.pk)
        self.assertEqual(self.prep.contract.resolved_inputs, original)
        self.assertEqual(read_media_generation_target(self.prep).value, appearance)
        self.assertIsNone(self.prep.request_frozen_at)

    def test_unrelated_optional_contributions_stay_optional_for_direct_difference(self):
        self.job("structured_field", "direct-difference")
        self.assertEqual({(row["unit_key"], row["required"]) for row in self.snapshot()["unselected_fields"]},
            {("first", False), ("second", False)})
        self.assertIn("Turn only the red collar outward.",
            self.preview().resolved_channels["values"]["positive_prompt"])
        self.assertFalse(self.prep.field_selections.exists())
