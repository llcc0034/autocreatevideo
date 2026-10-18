"""Explicit synthetic response structures for isolated tests, never runtime inference."""
from uuid import uuid4

from studio.tests.field_definition_fixtures import frozen_field_definition


def frozen_response_structure(example):
    nodes = []

    def visit(samples, parent=None, name="", required=False):
        kinds = {type(item) for item in samples}
        assert len(kinds) == 1, "Fixture must declare one type per node"
        kind = {dict: "object", list: "list", str: "long_text", bool: "boolean", int: "integer", float: "decimal"}[type(samples[0])]
        key = f"node_{len(nodes)}"
        nodes.append({"node_key": key, "parent_key": parent, "property_name": name, "position": len(nodes),
            "required": required, "allow_null": False, "allow_blank": True, "allow_empty": True, "constraints": {},
            "has_default": False, "default_value": None,
            "field_definition": frozen_field_definition("response_fixture_" + kind, kind)})
        if kind == "object":
            for prop in dict.fromkeys(prop for item in samples for prop in item):
                values = [item[prop] for item in samples if prop in item]
                visit(values, key, prop, required=len(values) == len(samples))
        elif kind == "list":
            # All empty arrays in these visual-rule fixtures declare text items.
            visit([item for values in samples for item in values] or [""], key)
    visit([example])
    return {"schema_version": 2, "id": str(uuid4()), "structure_key": str(uuid4()),
        "version": 1, "name": "Explicit isolated response structure", "nodes": nodes}


def response_structure(*, field_sources=None, response_keys=None, example=None, constraints=None, nullable_fields=()):
    from studio.models import FieldDefinition, FieldDefinitionVersion
    from studio.services.business_outputs import parser_contract
    from studio.services.data_structures import write_data_structure, transition_data_structure
    from studio.services.visual_rules_output_contract import reconfigure_visual_rule_sources, visual_rule_field_sources
    parser = parser_contract("visual_rules.v2")
    parser = reconfigure_visual_rule_sources(parser, field_sources or visual_rule_field_sources(parser), response_keys)
    frozen = frozen_response_structure(example if example is not None else parser["response_example"])
    definitions, nodes = {}, []
    for item in frozen["nodes"]:
        row = dict(item)
        row["constraints"] = (constraints or {}).get(row["property_name"], {})
        row["allow_null"] = row["property_name"] in nullable_fields
        kind = row.pop("field_definition")["value_type"]
        if kind not in definitions:
            identity = FieldDefinition.objects.create(key="response_fixture_" + uuid4().hex)
            definitions[kind] = FieldDefinitionVersion.objects.create(field_definition=identity,
                version=1, status="published", label=kind, category="test", value_type=kind)
        row["field_definition_version"] = str(definitions[kind].pk)
        nodes.append(row)
    structure = write_data_structure(content={"name": frozen["name"], "nodes": nodes})
    return transition_data_structure(version_id=structure.pk, status="published")
