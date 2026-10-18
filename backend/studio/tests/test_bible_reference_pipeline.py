from uuid import uuid4

from django.core.exceptions import ValidationError
from django.test import TestCase

from studio.platform_serializers import ApprovalDecisionInputSerializer
from studio.services.bible_reference_contract import bible_candidate_unit_key, parse_bible_candidate_unit_key
from studio.services.content_contracts import audit_note_bounds
from studio.services.prompt_normalization_contract import normalization_batch_max_items, normalization_text_max_characters


class BibleReferenceContractTests(TestCase):
    def test_approval_input_uses_security_boundaries_for_comment_and_overrides(self):
        _minimum, comment_maximum = audit_note_bounds()
        text_maximum = normalization_text_max_characters()
        item_maximum = normalization_batch_max_items()
        too_long_comment = ApprovalDecisionInputSerializer(data={
            "stage_key": "script_review",
            "decision": "approve",
            "comment": "x" * (comment_maximum + 1),
        })
        self.assertFalse(too_long_comment.is_valid())
        self.assertIn("comment", too_long_comment.errors)
        too_long_text = ApprovalDecisionInputSerializer(data={
            "stage_key": "script_normalize_review",
            "decision": "approve",
            "expected_source_snapshot_sha256": "a" * 64,
            "candidate_overrides": {"request": "x" * (text_maximum + 1)},
        })
        self.assertFalse(too_long_text.is_valid())
        self.assertIn("candidate_overrides", too_long_text.errors)
        too_many_items = ApprovalDecisionInputSerializer(data={
            "stage_key": "script_normalize_review",
            "decision": "approve",
            "expected_source_snapshot_sha256": "a" * 64,
            "candidate_overrides": {
                f"request-{index}": "valid" for index in range(item_maximum + 1)
            },
        })
        self.assertFalse(too_many_items.is_valid())
        self.assertIn("candidate_overrides", too_many_items.errors)

    def test_candidates_are_independently_selectable_variant_groups(self):
        entity_id = uuid4()
        first = parse_bible_candidate_unit_key(
            bible_candidate_unit_key("character", entity_id, 1),
        )
        second = parse_bible_candidate_unit_key(
            bible_candidate_unit_key("character", entity_id, 2),
        )

        self.assertEqual(first["entity_id"], str(entity_id))
        self.assertNotEqual(first["variant_group"], second["variant_group"])
        self.assertTrue(first["variant_group"].endswith(":candidate:1"))
        self.assertTrue(second["variant_group"].endswith(":candidate:2"))

    def test_review_accepts_multiple_candidates_with_one_anchor(self):
        entity_id = uuid4()
        first = uuid4()
        second = uuid4()
        serializer = ApprovalDecisionInputSerializer(data={
            "stage_key": "bible_image_review",
            "decision": "approve",
            "expected_source_snapshot_sha256": "a" * 64,
            "bible_image_bindings": [{
                "entity_type": "character",
                "entity_id": str(entity_id),
                "selected_artifact_ids": [str(first), str(second)],
                "anchor_artifact_id": str(second),
            }],
        })

        self.assertTrue(serializer.is_valid(), serializer.errors)
        binding = serializer.validated_data["bible_image_bindings"][0]
        self.assertEqual(binding["selected_artifact_ids"], [first, second])
        self.assertEqual(binding["anchor_artifact_id"], second)

    def test_review_accepts_mixed_model_and_asset_library_sources(self):
        entity_id = uuid4()
        artifact_id = uuid4()
        asset_id = uuid4()
        serializer = ApprovalDecisionInputSerializer(data={
            "stage_key": "bible_image_review",
            "decision": "approve",
            "expected_source_snapshot_sha256": "9" * 64,
            "bible_image_bindings": [{
                "entity_type": "character",
                "entity_id": str(entity_id),
                "selected_artifact_ids": [str(artifact_id)],
                "selected_library_assets": [{
                    "asset_id": str(asset_id),
                    "expected_sha256": "7" * 64,
                }],
                "anchor_library_asset_id": str(asset_id),
            }],
        })

        self.assertTrue(serializer.is_valid(), serializer.errors)
        binding = serializer.validated_data["bible_image_bindings"][0]
        self.assertEqual(binding["selected_artifact_ids"], [artifact_id])
        self.assertEqual(binding["selected_library_assets"], [{
            "asset_id": asset_id,
            "expected_sha256": "7" * 64,
        }])
        self.assertEqual(binding["anchor_library_asset_id"], asset_id)
        self.assertIsNone(binding.get("anchor_artifact_id"))

    def test_review_rejects_two_anchors_across_sources(self):
        entity_id = uuid4()
        artifact_id = uuid4()
        asset_id = uuid4()
        serializer = ApprovalDecisionInputSerializer(data={
            "stage_key": "bible_image_review",
            "decision": "approve",
            "expected_source_snapshot_sha256": "8" * 64,
            "bible_image_bindings": [{
                "entity_type": "character",
                "entity_id": str(entity_id),
                "selected_artifact_ids": [str(artifact_id)],
                "selected_library_assets": [{
                    "asset_id": str(asset_id),
                    "expected_sha256": "6" * 64,
                }],
                "anchor_artifact_id": str(artifact_id),
                "anchor_library_asset_id": str(asset_id),
            }],
        })

        self.assertFalse(serializer.is_valid())
        self.assertIn("bible_image_bindings", serializer.errors)

    def test_review_rejects_anchor_outside_selection_and_duplicate_entity(self):
        entity_id = uuid4()
        first = uuid4()
        outside = uuid4()
        serializer = ApprovalDecisionInputSerializer(data={
            "stage_key": "bible_image_review",
            "decision": "approve",
            "expected_source_snapshot_sha256": "b" * 64,
            "bible_image_bindings": [{
                "entity_type": "character",
                "entity_id": str(entity_id),
                "selected_artifact_ids": [str(first)],
                "anchor_artifact_id": str(outside),
            }],
        })
        self.assertFalse(serializer.is_valid())
        self.assertIn("bible_image_bindings", serializer.errors)

        duplicate = ApprovalDecisionInputSerializer(data={
            "stage_key": "bible_image_review",
            "decision": "approve",
            "expected_source_snapshot_sha256": "c" * 64,
            "bible_image_bindings": [
                {
                    "entity_type": "character",
                    "entity_id": str(entity_id),
                    "selected_artifact_ids": [str(first)],
                    "anchor_artifact_id": str(first),
                },
                {
                    "entity_type": "character",
                    "entity_id": str(entity_id),
                    "selected_artifact_ids": [str(outside)],
                    "anchor_artifact_id": str(outside),
                },
            ],
        })
        self.assertFalse(duplicate.is_valid())
        self.assertIn("bible_image_bindings", duplicate.errors)

    def test_storyboard_review_confirms_saved_selection_instead_of_writing_bindings(self):
        serializer = ApprovalDecisionInputSerializer(data={
            "stage_key": "storyboard_review", "decision": "approve",
            "expected_source_snapshot_sha256": "d" * 64, "expected_selection_sha256": "e" * 64,
        })
        self.assertTrue(serializer.is_valid(), serializer.errors)
        self.assertEqual(serializer.validated_data["expected_selection_sha256"], "e" * 64)
        old = ApprovalDecisionInputSerializer(data={**serializer.initial_data, "shot_reference_bindings": []})
        self.assertFalse(old.is_valid())
