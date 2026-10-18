"""Explicit synthetic versions for test rules; never a runtime migration path."""
from uuid import NAMESPACE_URL, uuid5
from studio.models import FieldDefinition, FieldDefinitionVersion, PromptProfileFieldRule
from studio.services.field_definitions import create_field_definition_version, publish_field_definition_version
from studio.services.field_definitions import field_definition_snapshot


def make_field_definition(*, save=False, **values):
    """Build an explicit test identity and its first definition, never dual-write."""
    content = {name: values.pop(name) for name in ("label", "category", "description", "impact", "value_type", "unit", "value_options") if name in values}
    definition = FieldDefinition(**values)
    content.setdefault("label", definition.key)
    content.setdefault("category", "test")
    content.setdefault("value_type", "text")
    content["value_type"] = {"number": "decimal", "string_list": "list"}.get(content["value_type"], content["value_type"])
    content["category"] = content["category"] or "test"
    if save:
        definition.save()
        version = create_field_definition_version(field_definition_id=definition.pk, content=content)
        version = publish_field_definition_version(version_id=version.pk)
    else:
        version = FieldDefinitionVersion(field_definition=definition, version=1, status="published", **content)
    definition._fixture_version = version
    return definition


def create_field_definition(**values):
    return make_field_definition(save=True, **values)


def get_or_create_field_definition(*, key, defaults):
    definition = FieldDefinition.objects.filter(key=key).first()
    if definition is not None:
        content = dict(defaults)
        content.setdefault("category", "test")
        content["value_type"] = {"number": "decimal", "string_list": "list"}.get(content.get("value_type"), content.get("value_type", "text"))
        version = create_field_definition_version(field_definition_id=definition.pk, content=content)
        definition._fixture_version = publish_field_definition_version(version_id=version.pk)
        return definition, False
    return create_field_definition(key=key, **defaults), True


def frozen_field_definition(key, value_type="long_text", value_options=None):
    definition = FieldDefinition(id=uuid5(NAMESPACE_URL, "fixture/field/" + key), key=key)
    version = FieldDefinitionVersion(id=uuid5(NAMESPACE_URL, "fixture/version/" + key), field_definition=definition, version=1, status="published",
        label=key, category="test", value_type=value_type, value_options=value_options or [])
    return field_definition_snapshot(version)


def make_profile_field_rule(*, save=False, **values):
    definition = values.get("field_definition")
    if definition is None:
        definition = FieldDefinition.objects.get(pk=values["field_definition_id"])
    if "field_definition_version" not in values and "field_definition_version_id" not in values:
        version = getattr(definition, "_fixture_version", None) or definition.definition_versions.get(version=1)
        if save and version.status == "draft":
            version = publish_field_definition_version(version_id=version.pk)
        values["field_definition_version"] = version
    rule = PromptProfileFieldRule(**values)
    if save:
        rule.save()
    return rule


def create_profile_field_rule(**values):
    return make_profile_field_rule(save=True, **values)


def publish_test_field_version(definition_id, content=None):
    definition = FieldDefinition.objects.get(pk=definition_id)
    if content is not None:
        version = create_field_definition_version(field_definition_id=definition.pk, content=content)
        return publish_field_definition_version(version_id=version.pk)
    version = definition.definition_versions.get(version=1)
    if version.status == "draft":
        return publish_field_definition_version(version_id=version.pk)
    return version
