"""Type-only examples for selected definitions; never execution input or defaults."""
from typing import Any

from studio.models import FieldDefinitionVersion


def prompt_profile_preview_example(definition: FieldDefinitionVersion) -> Any:
    kind = definition.value_type
    if kind == FieldDefinitionVersion.ValueType.LIST:
        return ["generic_reference"]
    if kind == FieldDefinitionVersion.ValueType.BOOLEAN:
        return True
    if kind in (FieldDefinitionVersion.ValueType.INTEGER, FieldDefinitionVersion.ValueType.DECIMAL):
        return 1
    if kind == FieldDefinitionVersion.ValueType.ENUM:
        return definition.value_options[0] if definition.value_options else ""
    if kind == FieldDefinitionVersion.ValueType.LONG_TEXT:
        return "A neutral example used only to preview the configured prompt structure."
    if kind in (FieldDefinitionVersion.ValueType.OBJECT, FieldDefinitionVersion.ValueType.MEDIA, ""):
        return None
    return "Generic preview value"
