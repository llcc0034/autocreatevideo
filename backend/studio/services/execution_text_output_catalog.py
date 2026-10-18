"""Project selected JSON outputs from exact recipe routes, not mutable registries."""
from django.core.exceptions import ValidationError


def recipe_text_source_options(definition):
    """Read-only authoring projection; input mappings may still be unfinished."""
    from studio.services.production_recipe_contracts import recipe_stage_declarations
    from studio.services.execution_input_catalog import execution_text_output_options
    stages = recipe_stage_declarations(definition)
    positions = {stage["key"]: index for index, stage in enumerate(stages, 1)}
    return [{**row, "label": f"{positions[row['stage_key']]} · {row['label']}"}
        for row in execution_text_output_options(stages) if row["stage_key"] in positions]


def structured_text_output_options(stages):
    from studio.services.data_structures import validate_data_structure_snapshot
    from studio.services.execution_definitions import verify_stored_execution_definition
    from studio.services.execution_output_contracts import STRUCTURED_TEXT_OPERATION, verify_operation_output
    from studio.services.execution_production_text_contracts import PRODUCTION_TEXT_ROLES
    from studio.services.production_recipe_contracts import (
        recipe_stage_kind, recipe_execution_reference, recipe_execution_contract, validate_recipe_route_output_identity,
        recipe_validation_context,
    )

    options, definitions, seen = [], {}, set()
    for index, stage in enumerate(stages):
        if recipe_stage_kind(stage) != "structured_text" or stage["mode"] != "ai":
            continue
        with recipe_validation_context(stage_index=index, stage_key=stage["key"], stage_kind="structured_text", routing_key="routing"):
            if stage["key"] in seen:
                raise ValidationError("上游输出目录存在重复步骤身份")
            seen.add(stage["key"])
            routing = stage.get("routing", {"routes": []})
            if not isinstance(routing, dict) or not isinstance(routing.get("routes"), list):
                raise ValidationError({"routing.routes": "上游输出目录需要明确的路线列表"})
            routes = routing["routes"]
            if not routes:
                continue  # An unfinished draft has no output, not an inferred one.
            group = []
            for route_index, route in enumerate(routes):
                with recipe_validation_context(route_index=route_index):
                    identity = recipe_execution_reference(route)
                    if identity not in definitions:
                        contract = recipe_execution_contract(route)
                        definitions[identity] = verify_stored_execution_definition(contract)
                    definition = definitions[identity]
                    if definition.value["configuration"]["operation"] != STRUCTURED_TEXT_OPERATION:
                        raise ValidationError("上游通用文本步骤与执行实例能力不一致")
                    group.append(definition)
            validate_recipe_route_output_identity(group)
            output = verify_operation_output(STRUCTURED_TEXT_OPERATION, group[0].value["interface"]["business_output"])
            schema = validate_data_structure_snapshot(output["data_structure"])
            options.append({"stage_key": stage["key"], "output_key": f"structured_text.{output['id']}",
                "artifact_role": PRODUCTION_TEXT_ROLES[STRUCTURED_TEXT_OPERATION], "reader": "structured_text",
                "kind": "text" if schema.get("type") == "string" else "json", "scope_kind": "project",
                "label": f"{stage['name']} · {output['name']} v{output['version']}", "value_schema": schema,
                "output_version_id": output["id"], "output_contract_sha256": output["sha256"],
                "structure_version_id": output["data_structure"]["id"]})
    return options


def plan_text_output_options(plan):
    from studio.services.execution_input_catalog import execution_text_output_options
    stages = plan.value["recipe"]["definition"]["stages"] if plan.value["source"] == "recipe" else ()
    return execution_text_output_options(stages)


def text_output_schema_key(option):
    """Step identity matters when two steps deliberately reuse one output version."""
    if option.get("reader") == "structured_text":
        from studio.services.json_contracts import canonical_json_bytes
        return canonical_json_bytes([option["stage_key"], option["output_key"]]).decode("utf-8")
    return option["output_key"]
