from django.test import TestCase
from studio.services.media_capabilities import normalize_conditioning_contract
from studio.services.reference_bindings import ReferenceBindingError, validate_reference_bindings_against_mode_contract
from studio.tests.factories import conditioning_contract


class GenericReferenceTopologyContractTests(TestCase):
    def mode_contract(self):
        contract = conditioning_contract(
            "NebulaTransfer",
            input_kind="classified_references",
            reference_videos=(0, 2),
            paired_video_audios=(0, 2),
            reference_audios=(0, 1),
            video_purposes=("orbit_rhythm_v7", "scene_retarget_x"),
            audio_purposes=("pulse_track_q",),
            video_preprocessing_adapter="fictional_video_adapter_v9",
            paired_audio_preprocessing_adapter="fictional_pair_adapter_v3",
            reference_audio_preprocessing_adapter="fictional_audio_adapter_v2",
        )
        contract["modes"][0]["dynamic_reference_topology"]["mutual_exclusion_rules"] = [{
            "key": "fictional_mutex",
            "label": "Fictional mutex",
            "description": "Configuration-only fictional rule",
            "media_kind": "video",
            "purpose_keys": ["orbit_rhythm_v7", "scene_retarget_x"],
            "maximum_distinct_purposes": 1,
        }]
        return normalize_conditioning_contract(contract)["modes"][0]

    def test_fictional_model_mode_and_purposes_validate_without_h3_names(self):
        validate_reference_bindings_against_mode_contract({
            "videos": [{
                "asset_id": "11111111-1111-4111-8111-111111111111",
                "purpose": "orbit_rhythm_v7",
                "use_embedded_audio": False,
            }],
            "audios": [{
                "asset_id": "22222222-2222-4222-8222-222222222222",
                "purpose": "pulse_track_q",
            }],
        }, self.mode_contract())

    def test_route_contract_rejects_unknown_safe_purpose_and_configured_mutex(self):
        mode = self.mode_contract()
        with self.assertRaises(ReferenceBindingError):
            validate_reference_bindings_against_mode_contract({
                "videos": [{
                    "asset_id": "11111111-1111-4111-8111-111111111111",
                    "purpose": "safe_but_undeclared",
                    "use_embedded_audio": False,
                }],
            }, mode)
        with self.assertRaises(ReferenceBindingError):
            validate_reference_bindings_against_mode_contract({
                "videos": [{
                    "asset_id": "11111111-1111-4111-8111-111111111111",
                    "purpose": "orbit_rhythm_v7",
                    "use_embedded_audio": False,
                }, {
                    "asset_id": "22222222-2222-4222-8222-222222222222",
                    "purpose": "scene_retarget_x",
                    "use_embedded_audio": False,
                }],
            }, mode)
