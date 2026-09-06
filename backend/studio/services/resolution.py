from __future__ import annotations

import math


class ResolutionProfileError(ValueError):
    pass


def validate_resolution_multiple(
    multiple: int,
    *,
    minimum: int,
    maximum: int,
    step: int,
) -> int:
    """Validate a versioned workflow's declared resolution lattice."""
    if (
        isinstance(multiple, bool)
        or not isinstance(multiple, int)
        or any(isinstance(value, bool) or not isinstance(value, int) for value in (
            minimum, maximum, step,
        ))
        or minimum < 1
        or maximum < minimum
        or step < 1
        or not minimum <= multiple <= maximum
        or multiple % step
    ):
        raise ResolutionProfileError(
            "resolution_multiple does not satisfy the frozen workflow lattice"
        )
    return multiple


def official_resolution(
    *, aspect_ratio: str, megapixels: float,
    multiple: int,
    aspect_ratios: dict[str, tuple[int, int]],
    multiple_minimum: int,
    multiple_maximum: int,
    multiple_step: int,
) -> tuple[int, int]:
    """Mirror ComfyUI's official ``ResolutionSelector.execute`` calculation."""
    try:
        width_ratio, height_ratio = aspect_ratios[aspect_ratio]
    except KeyError as exc:
        raise ResolutionProfileError(
            f"resolution_profile currently supports project aspect ratios: "
            f"{', '.join(aspect_ratios)}"
        ) from exc
    if megapixels <= 0:
        raise ResolutionProfileError("megapixels must be positive")
    multiple = validate_resolution_multiple(
        multiple,
        minimum=multiple_minimum,
        maximum=multiple_maximum,
        step=multiple_step,
    )
    total_pixels = megapixels * 1024 * 1024
    scale = math.sqrt(total_pixels / (width_ratio * height_ratio))
    width = round(width_ratio * scale / multiple) * multiple
    height = round(height_ratio * scale / multiple) * multiple
    return width, height


def resolve_production_resolution(
    *, profile: str, aspect_ratio: str, project_width: int, project_height: int,
    multiple: int,
    profiles: tuple[str, ...],
    aspect_ratios: dict[str, tuple[int, int]],
    multiple_minimum: int,
    multiple_maximum: int,
    multiple_step: int,
) -> tuple[int, int]:
    multiple = validate_resolution_multiple(
        multiple,
        minimum=multiple_minimum,
        maximum=multiple_maximum,
        step=multiple_step,
    )
    if profile == "project":
        # Keep the project dimensions as closely as the selected official
        # rounding multiple allows.  With the default multiple=32, existing H3
        # project dimensions are unchanged.
        width = max(multiple, round(int(project_width) / multiple) * multiple)
        height = max(multiple, round(int(project_height) / multiple) * multiple)
        return width, height
    if profile not in profiles:
        raise ResolutionProfileError(f"Unsupported resolution_profile: {profile}")
    return official_resolution(
        aspect_ratio=aspect_ratio,
        megapixels=float(profile),
        multiple=multiple,
        aspect_ratios=aspect_ratios,
        multiple_minimum=multiple_minimum,
        multiple_maximum=multiple_maximum,
        multiple_step=multiple_step,
    )
