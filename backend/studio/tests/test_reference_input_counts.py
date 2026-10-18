from copy import deepcopy

from django.test import SimpleTestCase

from studio.services.media_capabilities import CONDITIONING_INPUT_SLOTS
from studio.services.reference_bindings import ReferenceBindingError, validate_reference_input_counts, validate_reference_bindings_against_mode_contract


class ReferenceInputCountsTests(SimpleTestCase):
    def setUp(self):
        self.mode = {"input_kind": "classified_references", "inputs": {
            key: {"minimum_items": 0, "maximum_items": 0} for key in CONDITIONING_INPUT_SLOTS}}
        for key in ("entity_images", "total_images"):
            self.mode["inputs"][key] = {"minimum_items": 1, "maximum_items": 2}
        self.counts = dict.fromkeys(CONDITIONING_INPUT_SLOTS, 0)
        self.counts.update(total_images=1, entity_images=1)

    def test_project_binding_and_neutral_count_path_use_the_same_model_ranges(self):
        validate_reference_input_counts(self.counts, self.mode)
        validate_reference_bindings_against_mode_contract({"characters": [{"asset_id": "test-asset"}]}, self.mode)
        self.mode["inputs"]["total_images"]["minimum_items"] = 2
        with self.assertRaisesMessage(ReferenceBindingError, "total_images"):
            validate_reference_input_counts(self.counts, self.mode)
        with self.assertRaisesMessage(ReferenceBindingError, "total_images"):
            validate_reference_bindings_against_mode_contract({"characters": [{"asset_id": "test-asset"}]}, self.mode)

    def test_missing_inconsistent_boolean_or_string_counts_are_not_coerced(self):
        for changes in ({"total_images": 2}, {"entity_images": True}, {"entity_images": "1"},
                        {"extra": 0}, {"reference_videos": -1}, {"paired_video_audios": 1}):
            with self.subTest(changes=changes), self.assertRaises(ReferenceBindingError):
                validate_reference_input_counts({**self.counts, **changes}, self.mode)
        for value in (True, "1", -1, None):
            mode = deepcopy(self.mode)
            mode["inputs"]["entity_images"]["minimum_items"] = value
            with self.subTest(value=value), self.assertRaises(ReferenceBindingError):
                validate_reference_input_counts(self.counts, mode)

    def test_input_kind_never_accepts_disallowed_role_even_with_loose_counts(self):
        for kind in ("frame_slots", "text_only", "unregistered"):
            with self.subTest(kind=kind), self.assertRaises(ReferenceBindingError):
                validate_reference_input_counts(self.counts, {**self.mode, "input_kind": kind})
