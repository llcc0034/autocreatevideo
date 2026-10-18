from django.core.exceptions import ValidationError
from django.test import TestCase

from studio.services.media_capabilities import (
    merge_conditioning_contract_modes,
    normalize_conditioning_contract,
    normalize_model_media_constraints,
    validate_media_execution_settings,
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
    def test_model_monitoring_options_are_independent_of_production_admission(self):
        from unittest.mock import patch
        from studio.services.execution_monitoring import resolve_execution_monitoring_contract
        from studio.services.model_profile_options import model_profile_registry_options
        from studio.platform_serializers import ModelProfileRegistryOptionsSerializer
        with patch("studio.services.execution_monitoring.boundary_int", side_effect=lambda key: {
            "security.execution.max_idle_timeout_seconds": 600,
            "security.execution.max_type_overrides": 1,
            "security.execution.max_node_overrides": 2,
            "security.snapshot.stage_max_bytes": 8192,
        }[key]):
            options = ModelProfileRegistryOptionsSerializer(model_profile_registry_options()).data
            self.assertEqual(options["execution_monitoring"], {
                "max_idle_timeout_seconds": 600, "max_node_type_overrides": 1,
            })
            valid = {"execution_monitoring": {"schema_version": 1,
                "node_type_overrides": {"SamplerCustomAdvanced": 600}}}
            self.assertEqual(resolve_execution_monitoring_contract(model_specification=valid)["node_type_overrides"], {"SamplerCustomAdvanced": 600})
            for overrides in ({"SamplerCustomAdvanced": 601}, {"One": 1, "Two": 1}):
                with self.assertRaises(ValidationError):
                    resolve_execution_monitoring_contract(model_specification={"execution_monitoring": {
                        "schema_version": 1, "node_type_overrides": overrides}})

    # Contract limits are current, database-backed security configuration.
    def test_media_form_catalog_and_runtime_use_same_keys_and_live_boundaries(self):
        from unittest.mock import patch
        from studio.services.media_capabilities import media_constraints_editor_options
        from studio.services.model_profile_options import model_profile_registry_options
        from studio.platform_serializers import ModelProfileRegistryOptionsSerializer
        from studio.services.security_boundaries import boundary_int
        def changed(key):
            return {"security.media.max_dimension": 2048, "security.media.max_fps": 60,
                "security.media.max_duration_ms": 10000, "security.reference.max_bindings": 4}.get(key, boundary_int(key))
        with patch("studio.services.media_capabilities.boundary_int", side_effect=changed):
            options = media_constraints_editor_options()
            serialized = ModelProfileRegistryOptionsSerializer(model_profile_registry_options()).data
            self.assertEqual(serialized["media_constraints"], options)
            self.assertEqual(options["fps"], {"minimum": 1, "maximum": 60, "maximum_items": 4})
            for field in options["resolution_fields"]:
                self.assertEqual(field["maximum"], 2048)
                for value in (1, 2048):
                    self.assertEqual(normalize_model_media_constraints({"media_constraints": {
                        "schema_version": options["schema_version"], "resolution": {field["key"]: value}}})["resolution"][field["key"]], value)
                with self.assertRaises(ValidationError):
                    normalize_model_media_constraints({"media_constraints": {
                        "schema_version": 1, "resolution": {field["key"]: 2049}}})
            for raw in (
                {"schema_version": True}, {"schema_version": 1, "resolution": {"unknown": 2}},
                {"schema_version": 1, "resolution": {"minimum_width": 20, "maximum_width": 10}},
                {"schema_version": 1, "fps": {"allowed": [61]}},
                {"schema_version": 1, "fps": {"allowed": [24, 24]}},
                {"schema_version": 1, "fps": {"allowed": [1,2,3,4,5]}},
                {"schema_version": 1, "duration_ms": {"minimum": 1000}},
                {"schema_version": 1, "duration_ms": {"minimum": 1000, "maximum": 10001}},
            ):
                with self.subTest(raw=raw), self.assertRaises(ValidationError):
                    normalize_model_media_constraints({"media_constraints": raw})

    def test_explicit_bible_requirement_is_not_inferred_from_mode_name(self):
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
        modes = {row["key"]: row for row in _contract(custom, literal_ref2va)["modes"]}
        self.assertTrue(modes["vendor_reference"]["requires_entity_reference_bible"])
        self.assertFalse(modes["Ref2VA"]["requires_entity_reference_bible"])
        # Actual Route/RunPlan gates are exercised with published exact
        # definitions in test_production_recipe_bible_closure, not old routes.

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

    def test_fixed_dimension_range_fields_remain_subject_to_runtime_constraints(self):
        from copy import deepcopy
        from studio.services.media_capabilities import validate_media_resolution
        specification = {"media_constraints": {"schema_version": 1, "resolution": {
            "minimum_width": 352, "maximum_width": 352,
            "minimum_height": 608, "maximum_height": 608,
            "width_multiple": 32, "height_multiple": 32,
            "maximum_short_side": 352, "maximum_long_side": 608,
        }}}
        original = deepcopy(specification)
        resolution = normalize_model_media_constraints(specification)["resolution"]
        validate_media_resolution(constraints=resolution, width=352, height=608)
        self.assertEqual(specification, original)
        for changes, width, height in (({}, 608, 352), ({}, 384, 608),
                ({"width_multiple": 64}, 352, 608),
                ({"maximum_short_side": 320}, 352, 608),
                ({"maximum_long_side": 576}, 352, 608)):
            with self.subTest(changes=changes, width=width, height=height), self.assertRaises(ValidationError):
                validate_media_resolution(constraints={**resolution, **changes}, width=width, height=height)
