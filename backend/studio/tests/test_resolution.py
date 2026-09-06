from django.test import SimpleTestCase

from studio.services.resolution import (
    ResolutionProfileError,
    official_resolution,
    resolve_production_resolution,
    validate_resolution_multiple,
)


ASPECT_RATIOS = {"1:1": (1, 1), "9:16": (9, 16), "16:9": (16, 9)}
PROFILES = (
    "project", "0.1", "0.2", "0.3", "0.4", "0.5", "0.6", "0.7",
    "0.8", "0.9", "0.98", "1.0", "1.2", "1.5", "1.8", "2.0",
)


def official(*, aspect_ratio: str, megapixels: float, multiple: int = 32):
    return official_resolution(
        aspect_ratio=aspect_ratio,
        megapixels=megapixels,
        multiple=multiple,
        aspect_ratios=ASPECT_RATIOS,
        multiple_minimum=8,
        multiple_maximum=128,
        multiple_step=4,
    )


class OfficialResolutionTests(SimpleTestCase):
    def test_widescreen_profiles_match_official_comfyui_selector(self):
        expected = {
            "0.1": (416, 256),
            "0.2": (608, 352),
            "0.3": (736, 416),
            "0.4": (864, 480),
            "0.5": (960, 544),
            "0.6": (1056, 608),
            "0.7": (1152, 640),
            "0.8": (1216, 672),
            "0.9": (1280, 736),
            "0.98": (1344, 768),
            "1.0": (1376, 768),
            "1.2": (1504, 832),
            "1.5": (1664, 928),
            "1.8": (1824, 1024),
            "2.0": (1920, 1088),
        }
        for profile, resolution in expected.items():
            with self.subTest(profile=profile):
                self.assertEqual(
                    official(aspect_ratio="16:9", megapixels=float(profile)),
                    resolution,
                )

    def test_portrait_profiles_are_transposed_and_square_is_natural(self):
        self.assertEqual(
            official(aspect_ratio="9:16", megapixels=0.1),
            (256, 416),
        )
        self.assertEqual(
            official(aspect_ratio="9:16", megapixels=0.2),
            (352, 608),
        )
        self.assertEqual(
            official(aspect_ratio="9:16", megapixels=0.3),
            (416, 736),
        )
        self.assertEqual(
            official(aspect_ratio="1:1", megapixels=0.1),
            (320, 320),
        )
        self.assertEqual(
            official(aspect_ratio="1:1", megapixels=0.2),
            (448, 448),
        )

    def test_unknown_project_aspect_is_rejected(self):
        with self.assertRaises(ResolutionProfileError):
            official(aspect_ratio="21:9", megapixels=0.3)

    def test_non_default_multiple_changes_official_rounding(self):
        self.assertEqual(
            official(aspect_ratio="9:16", megapixels=0.3, multiple=20),
            (420, 740),
        )

    def test_project_profile_is_rounded_to_selected_multiple(self):
        self.assertEqual(
            resolve_production_resolution(
                profile="project",
                aspect_ratio="9:16",
                project_width=768,
                project_height=1344,
                multiple=20,
                profiles=PROFILES,
                aspect_ratios=ASPECT_RATIOS,
                multiple_minimum=8,
                multiple_maximum=128,
                multiple_step=4,
            ),
            (760, 1340),
        )

    def test_resolution_multiple_uses_official_bounded_step(self):
        for value in (8, 12, 32, 124, 128):
            with self.subTest(value=value):
                self.assertEqual(validate_resolution_multiple(
                    value, minimum=8, maximum=128, step=4,
                ), value)
        for value in (4, 10, 18, 132, True):
            with self.subTest(value=value):
                with self.assertRaises(ResolutionProfileError):
                    validate_resolution_multiple(
                        value, minimum=8, maximum=128, step=4,
                    )
