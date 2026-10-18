from copy import deepcopy
from unittest.mock import patch
from uuid import uuid4

from django.core.exceptions import ValidationError
from django.test import SimpleTestCase

from studio.platform_serializers import BibleImageBindingSelectionSerializer
from studio.services.bible_image_selections import normalize_bible_image_selection


class BibleImageSelectionContractTests(SimpleTestCase):
    def setUp(self):
        self.first, self.second, self.library = (str(uuid4()) for _ in range(3))
        self.selection = {"entity_type": "prop", "entity_id": str(uuid4()),
            "selected_artifact_ids": [self.first, self.second],
            "selected_library_assets": [{"asset_id": self.library, "expected_sha256": "a" * 64}],
            "anchor_artifact_id": self.second, "anchor_library_asset_id": None}

    def test_multiple_references_keep_one_explicit_anchor_and_original_order(self):
        original = deepcopy(self.selection)
        normalized = normalize_bible_image_selection(original, maximum_assets=3)
        self.assertEqual(normalized, self.selection)
        self.assertEqual(original, self.selection)
        with patch("studio.platform_serializers.boundary_int", return_value=3) as boundary:
            serializer = BibleImageBindingSelectionSerializer(data=original)
            self.assertTrue(serializer.is_valid(), serializer.errors)
            boundary.assert_called_once_with("security.production.max_bible_reference_assets_per_entity")
            self.assertEqual(normalize_bible_image_selection(serializer.validated_data, maximum_assets=3), normalized)

    def test_library_only_selection_needs_no_generated_candidate(self):
        self.selection.update(selected_artifact_ids=[], anchor_artifact_id=None, anchor_library_asset_id=self.library)
        self.assertEqual(normalize_bible_image_selection(self.selection, maximum_assets=1), self.selection)

    def test_invalid_choices_cannot_implicitly_pick_an_anchor(self):
        changes = [
            {"anchor_artifact_id": None}, {"anchor_library_asset_id": self.library},
            {"anchor_artifact_id": str(uuid4())}, {"selected_artifact_ids": [self.first, self.first]},
            {"selected_library_assets": self.selection["selected_library_assets"] * 2},
            {"selected_artifact_ids": "not-a-list"}, {"entity_id": "looks-like-a-prop-name"},
            {"entity_type": []}, {"selected_library_assets": [{"asset_id": self.library, "expected_sha256": ""}]},
            {"hidden_route": "not-accepted"},
        ]
        for change in changes:
            with self.subTest(change=change), self.assertRaises(ValidationError):
                normalize_bible_image_selection({**self.selection, **change}, maximum_assets=3)
        with self.assertRaises(ValidationError):
            normalize_bible_image_selection(self.selection, maximum_assets=2)
        for maximum in (None, True, 0):
            with self.subTest(maximum=maximum), self.assertRaises(ValidationError):
                normalize_bible_image_selection(self.selection, maximum_assets=maximum)

