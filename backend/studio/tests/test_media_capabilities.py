from django.core.exceptions import ValidationError
from django.test import TestCase

from studio.services.media_capabilities import (
    merge_conditioning_contract_modes,
    normalize_conditioning_contract,
    normalize_model_media_constraints,
    validate_media_execution_settings,
)
from studio.services.production_runs import (
    _route_mode_requires_entity_reference_bible,
)


def _slots(**overrides):
    zero = {"minimum_items": 0, "maximum_items": 0}
    result = {
        "total_images": dict(zero),
        "first_frame": dict(zero),
        "last_frame": dict(zero),
        "entity_images": dict(zero),
        "reference_videos": dict(zero),
        "paired_video_audios": dict(zero),
        "reference_audios": dict(zero),
    }
    result.update(overrides)
    return result


def _mode(key, *, requires_bible=False, input_kind="text_only", inputs=None):
    return {
        "key": key,
        "label": key,
        "description": f"{key} configured mode",
        "input_kind": input_kind,
        "requires_entity_reference_bible": requires_bible,
        "supports_native_audio": False,
        "inputs": inputs or _slots(),
    }


def _contract(*modes):
    return normalize_conditioning_contract({
        "schema_version": 1,
        "adapter_key": "media_conditioning_v1",
        "modes": list(modes),
    })


class MediaCapabilityContractTests(TestCase):
    # Contract limits are current, database-backed security configuration.
    def test_bible_gate_is_capability_driven_not_mode_name_driven(self):
        custom = _mode(
            "vendor_reference",
            requires_bible=True,
            input_kind="classified_references",
            inputs=_slots(
                total_images={"minimum_items": 0, "maximum_items": 4},
                entity_images={"minimum_items": 0, "maximum_items": 4},
            ),
        )
        literal_ref2va = _mode("Ref2VA")
        route = {"conditioning_contract": _contract(custom, literal_ref2va)}

        self.assertTrue(
            _route_mode_requires_entity_reference_bible(route, "vendor_reference"),
        )
        self.assertFalse(
            _route_mode_requires_entity_reference_bible(route, "Ref2VA"),
        )

    def test_catalog_rejects_same_mode_key_with_conflicting_contracts(self):
        first = _contract(_mode("vendor_mode"))
        second = _contract({**_mode("vendor_mode"), "supports_native_audio": True})

        with self.assertRaises(ValidationError):
            merge_conditioning_contract_modes([first, second])

    def test_model_media_constraints_drive_resolution_fps_and_duration(self):
        constraints = normalize_model_media_constraints({
            "media_constraints": {
                "schema_version": 1,
                "resolution": {
                    "width_multiple": 16,
                    "height_multiple": 8,
                    "maximum_short_side": 512,
                    "maximum_long_side": 1024,
                },
                "fps": {"allowed": [25, 30]},
                "duration_ms": {"minimum": 800, "maximum": 2400},
            },
        })
        validate_media_execution_settings(
            constraints=constraints,
            width=512,
            height=1024,
            fps=30,
            duration_ms=2000,
            media_type="video",
        )

        for override in (
            {"width": 510},
            {"fps": 24},
            {"duration_ms": 2500},
        ):
            values = {
                "width": 512,
                "height": 1024,
                "fps": 30,
                "duration_ms": 2000,
                "media_type": "video",
                **override,
            }
            with self.subTest(override=override), self.assertRaises(ValidationError):
                validate_media_execution_settings(
                    constraints=constraints,
                    **values,
                )
