from copy import deepcopy

from django.core.exceptions import ValidationError
from django.test import TestCase

from studio.services.execution_media_parameters import validate_resolved_execution_media
from studio.services.execution_parameters import resolve_execution_parameters
from studio.services.media_capabilities import validate_media_execution_settings


class ExecutionResolvedMediaTests(TestCase):
    def setUp(self):
        self.constraints = {
            "schema_version": 1,
            "resolution": {"maximum_width": 1024, "maximum_height": 1024,
                           "maximum_short_side": 768, "maximum_long_side": 1024,
                           "width_multiple": 16, "height_multiple": 16},
            "fps": {"allowed": [24, 30]}, "duration_ms": {"minimum": 500, "maximum": 6000},
        }
        self.mapping = {"width": "width", "height": "height", "fps": "fps", "duration_seconds": "seconds"}
        self.values = {"width": 1024, "height": 576, "fps": 24, "seconds": 2.5}

    def validate(self, *, kind="video"):
        return validate_resolved_execution_media(
            constraints=self.constraints, semantic_keys=self.mapping, values=self.values, output_kind=kind,
        )

    def test_scalar_pass_is_not_coupled_resolution_pass(self):
        layers = {key: [{"source": "frozen.api", "rule": {"type": "integer", "required": True, "minimum": 16, "maximum": 1024}}] for key in ("width", "height")}
        resolved = resolve_execution_parameters(layers, {"width": 1024, "height": 1024})
        self.values.update(resolved["values"])
        with self.assertRaisesMessage(ValidationError, "短边"):
            self.validate()

    def test_same_configured_dimensions_have_same_verdict_in_existing_production(self):
        for width, height in ((1024, 576), (1024, 1024), (576, 1040), (777, 512)):
            self.values.update(width=width, height=height)
            passed = []
            for action in (self.validate, lambda: validate_media_execution_settings(
                constraints=self.constraints, width=width, height=height, fps=24, duration_ms=2500, media_type="video",
            )):
                try:
                    action()
                    passed.append(True)
                except ValidationError:
                    passed.append(False)
            self.assertEqual(passed[0], passed[1])

    def test_seconds_conversion_is_exact_and_conflicting_units_rejected(self):
        self.values["seconds"] = 0.5
        self.validate()
        self.values["seconds"] = 0.4999999
        with self.assertRaises(ValidationError):
            self.validate()
        self.values["seconds"] = 2.5
        self.mapping["duration_ms"] = "millis"
        self.values["millis"] = 2500
        self.validate()
        self.values["millis"] = 2499
        with self.assertRaisesMessage(ValidationError, "冲突"):
            self.validate()

    def test_missing_constrained_operands_do_not_invent_defaults(self):
        original = dict(self.values)
        for key in ("width", "height", "fps", "seconds"):
            self.values = {name: value for name, value in original.items() if name != key}
            with self.subTest(key=key), self.assertRaises(ValidationError):
                self.validate()

    def test_absent_unconstrained_operands_are_not_required(self):
        self.constraints = {"schema_version": 1, "resolution": {}, "fps": {"allowed": []}, "duration_ms": {}}
        self.mapping, self.values = {}, {}
        self.validate()
        self.constraints["resolution"] = {"maximum_width": 1024}
        self.mapping, self.values = {"width": "w"}, {"w": 768}
        self.validate()

    def test_type_bounds_fps_and_nonfinite_values_are_not_coerced(self):
        for key, value in (("width", True), ("height", "576"), ("width", 0), ("fps", 25), ("fps", float("nan")), ("seconds", float("inf")), ("seconds", True)):
            original = self.values[key]
            self.values[key] = value
            with self.subTest(key=key, value=value), self.assertRaises(ValidationError):
                self.validate()
            self.values[key] = original

    def test_image_ignores_video_timing_and_audio_requires_only_its_time_bounds(self):
        self.values = {"width": 1024, "height": 576}
        self.validate(kind="image")
        self.values = {"seconds": 3}
        self.validate(kind="audio")
        self.values = {"seconds": 8}
        with self.assertRaises(ValidationError):
            self.validate(kind="audio")

    def test_role_mapping_is_exact_and_cannot_alias_two_operands(self):
        for mapping in ({"width": "one", "height": "one"}, {"frames_guess": "seconds"}, {"width": []}):
            self.mapping = mapping
            with self.assertRaises(ValidationError):
                self.validate()

    def test_readonly_validation_does_not_change_parameters_or_constraints(self):
        before = deepcopy((self.constraints, self.mapping, self.values))
        self.validate()
        self.assertEqual((self.constraints, self.mapping, self.values), before)
