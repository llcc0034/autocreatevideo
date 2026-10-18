from __future__ import annotations

from collections.abc import Callable
from typing import Any

from studio.services.security_boundaries import boundary_int


def _dynamic_widgets_max_items() -> int:
    return boundary_int("security.comfy.contract.max_dynamic_widgets")


def _autogrow_inputs_max_items() -> int:
    return boundary_int("security.comfy.contract.max_autogrow_inputs")


def dynamic_widget_definitions(
    parent_name: str,
    input_type: str,
    config: dict,
    *,
    selected_value: Any,
    resolve_value: Callable[[str, object], Any] | None = None,
) -> tuple[tuple[str, object, bool], ...]:
    """Expand selected dynamic branches in depth-first widget order.

    The resolver reads an explicit API value, or consumes the next canvas
    widget. It never supplies schema defaults. All nesting shares one bounded
    traversal; callers still validate every returned definition and value.
    """
    pending = list(reversed(_direct_dynamic_widget_definitions(
        parent_name, input_type, config, selected_value=selected_value,
    )))
    rows: list[tuple[str, object, bool]] = []
    seen = {parent_name}
    limit = _dynamic_widgets_max_items()
    while pending:
        if len(rows) + len(pending) > limit:
            raise ValueError(f"{parent_name}: dynamic widgets exceed configured limit {limit}")
        name, definition, required = pending.pop()
        if name in seen:
            raise ValueError(f"{parent_name}: duplicate dynamic input {name!r}")
        seen.add(name)
        rows.append((name, definition, required))
        if resolve_value is None:
            continue
        value = resolve_value(name, definition)
        if not isinstance(definition, (list, tuple)) or not definition:
            continue  # The consumer reports the malformed definition.
        raw_type = definition[0]
        child_type = "COMBO" if isinstance(raw_type, (list, tuple)) else raw_type
        child_config = definition[1] if len(definition) > 1 and isinstance(definition[1], dict) else {}
        pending.extend(reversed(_direct_dynamic_widget_definitions(
            name, child_type, child_config, selected_value=value,
        )))
    return tuple(rows)


def _direct_dynamic_widget_definitions(
    parent_name: str, input_type: str, config: dict, *, selected_value: Any,
) -> tuple[tuple[str, object, bool], ...]:
    """Read the two reviewed Comfy dynamic-widget schema encodings.

    Comfy's V3 dynamic combo exposes nested ``inputs`` under the selected
    option and serializes them as ``parent.child`` API inputs. VHS exposes
    format-specific widgets through the legacy ``config.formats`` map and
    serializes those widgets as top-level API inputs. Only the selected branch
    is authoritative; unknown selections fail closed by returning no children
    so the normal input-contract validator rejects canvas extras.
    """

    rows: list[tuple[str, object, bool]] = []
    if input_type == "COMFY_DYNAMICCOMBO_V3":
        options = config.get("options")
        if not isinstance(options, list):
            return ()
        selected = next(
            (
                option for option in options
                if isinstance(option, dict) and option.get("key") == selected_value
            ),
            None,
        )
        nested = selected.get("inputs") if isinstance(selected, dict) else None
        if not isinstance(nested, dict):
            return ()
        for category in ("required", "optional"):
            definitions = nested.get(category) or {}
            if not isinstance(definitions, dict):
                return ()
            for child_name, definition in definitions.items():
                if not isinstance(child_name, str) or not child_name:
                    return ()
                rows.append((f"{parent_name}.{child_name}", definition, category == "required"))
    formats = config.get("formats")
    if isinstance(formats, dict) and isinstance(selected_value, (str, int, float, bool)) and selected_value in formats:
        definitions = formats[selected_value]
        if not isinstance(definitions, list):
            return ()
        for definition in definitions:
            if (
                not isinstance(definition, (list, tuple))
                or len(definition) < 2
                or not isinstance(definition[0], str)
                or not definition[0]
            ):
                return ()
            child_config = (
                definition[2]
                if len(definition) > 2 and isinstance(definition[2], dict)
                else {}
            )
            rows.append((definition[0], [definition[1], child_config], True))
    return tuple(rows)


def autogrow_canvas_names(group_name: str, config: dict) -> tuple[str, ...] | None:
    """Return every bounded V3 autogrow canvas port name or ``None``."""

    template = config.get("template") if isinstance(config, dict) else None
    if not isinstance(template, dict):
        return None
    prefix = template.get("prefix")
    names = template.get("names")
    minimum = template.get("min", 0)
    maximum = template.get("max")
    if isinstance(prefix, str) and prefix:
        if (
            not isinstance(maximum, int)
            or isinstance(maximum, bool)
            or maximum < 0
            or maximum > _autogrow_inputs_max_items()
        ):
            return None
        return tuple(f"{group_name}.{prefix}{index}" for index in range(maximum))
    if (
        not isinstance(names, list)
        or not names
        or len(names) > _autogrow_inputs_max_items()
        or not all(isinstance(name, str) and name for name in names)
        or len(set(names)) != len(names)
        or not isinstance(minimum, int)
        or isinstance(minimum, bool)
        or minimum < 0
        or minimum > len(names)
    ):
        return None
    return tuple(f"{group_name}.{name}" for name in names)
