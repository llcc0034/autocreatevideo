"""Conditioning cardinality remains a shared contract, not a root video list."""
from django.test import TestCase

from studio.services.reference_bindings import ReferenceBindingError, validate_reference_bindings_against_mode_contract
from studio.tests.factories import conditioning_contract


class ReferenceCardinalityContractTests(TestCase):
    def test_storyboard_video_cardinality_uses_the_same_mode_contract(self):
        optional = conditioning_contract(
            "NebulaTransfer",
            input_kind="classified_references",
            reference_videos=(0, 3),
            video_purposes=("orbit_rhythm_v7",),
        )["modes"][0]
        validate_reference_bindings_against_mode_contract(
            {},
            optional,
        )

        required = conditioning_contract(
            "NebulaTransfer",
            input_kind="classified_references",
            reference_videos=(1, 1),
            video_purposes=("orbit_rhythm_v7",),
        )["modes"][0]
        with self.assertRaisesMessage(ReferenceBindingError, "冻结范围 1–1"):
            validate_reference_bindings_against_mode_contract(
                {},
                required,
            )

        forbidden = conditioning_contract("NebulaText")["modes"][0]
        with self.assertRaisesMessage(ReferenceBindingError, "冻结范围 0–0"):
            validate_reference_bindings_against_mode_contract(
                {"videos": [{
                    "asset_id": "00000000-0000-0000-0000-000000000001",
                    "purpose": "orbit_rhythm_v7",
                    "use_embedded_audio": False,
                }]},
                forbidden,
            )
