from __future__ import annotations

import math


# This is the platform's complete H3 preset table, based on the values shown in
# the official ComfyUI ResolutionSelector workflow.  The underlying float node
# supports a wider range; this bounded table is the reviewed production UI.
H3_RESOLUTION_PROFILES = (
    "project",
    "0.1",
    "0.2",
    "0.3",
    "0.4",
    "0.5",
    "0.6",
    "0.7",
    "0.8",
    "0.9",
    "0.98",
    "1.0",
    "1.2",
    "1.5",
    "1.8",
    "2.0",
)

H3_RESOLUTION_MULTIPLE_DEFAULT = 32
H3_RESOLUTION_MULTIPLE_MIN = 8
H3_RESOLUTION_MULTIPLE_MAX = 128
H3_RESOLUTION_MULTIPLE_STEP = 4
IMAGE_RESOLUTION_MULTIPLE_STEP = 8

H3_ASPECT_RATIOS: dict[str, tuple[int, int]] = {
    "1:1": (1, 1),
    "9:16": (9, 16),
    "16:9": (16, 9),
}


class ResolutionProfileError(ValueError):
    pass


def validate_resolution_multiple(multiple: int) -> int:
    """Validate against the installed official ResolutionSelector input schema."""
    if (
        isinstance(multiple, bool)
        or not isinstance(multiple, int)
        or not H3_RESOLUTION_MULTIPLE_MIN <= multiple <= H3_RESOLUTION_MULTIPLE_MAX
        or multiple % H3_RESOLUTION_MULTIPLE_STEP
    ):
        raise ResolutionProfileError(
            "resolution_multiple must be between 8 and 128 and divisible by 4"
        )
    return multiple


def validate_image_resolution_multiple(multiple: int) -> int:
    """Validate the stricter lattice required by the reviewed SDXL graph."""

    multiple = validate_resolution_multiple(multiple)
    if multiple % IMAGE_RESOLUTION_MULTIPLE_STEP:
        raise ResolutionProfileError(
            "image_resolution_multiple must be between 8 and 128 and divisible by 8"
        )
    return multiple


def official_resolution(
    *, aspect_ratio: str, megapixels: float,
    multiple: int = H3_RESOLUTION_MULTIPLE_DEFAULT,
) -> tuple[int, int]:
    """Mirror ComfyUI's official ``ResolutionSelector.execute`` calculation."""
    try:
        width_ratio, height_ratio = H3_ASPECT_RATIOS[aspect_ratio]
    except KeyError as exc:
        raise ResolutionProfileError(
            f"resolution_profile currently supports project aspect ratios: "
            f"{', '.join(H3_ASPECT_RATIOS)}"
        ) from exc
    if megapixels <= 0:
        raise ResolutionProfileError("megapixels must be positive")
    multiple = validate_resolution_multiple(multiple)
    total_pixels = megapixels * 1024 * 1024
    scale = math.sqrt(total_pixels / (width_ratio * height_ratio))
    width = round(width_ratio * scale / multiple) * multiple
    height = round(height_ratio * scale / multiple) * multiple
    return width, height


def resolve_production_resolution(
    *, profile: str, aspect_ratio: str, project_width: int, project_height: int,
    multiple: int = H3_RESOLUTION_MULTIPLE_DEFAULT,
) -> tuple[int, int]:
    multiple = validate_resolution_multiple(multiple)
    if profile == "project":
        # Keep the project dimensions as closely as the selected official
        # rounding multiple allows.  With the default multiple=32, existing H3
        # project dimensions are unchanged.
        width = max(multiple, round(int(project_width) / multiple) * multiple)
        height = max(multiple, round(int(project_height) / multiple) * multiple)
        return width, height
    if profile not in H3_RESOLUTION_PROFILES:
        raise ResolutionProfileError(f"Unsupported resolution_profile: {profile}")
    return official_resolution(
        aspect_ratio=aspect_ratio,
        megapixels=float(profile),
        multiple=multiple,
    )
