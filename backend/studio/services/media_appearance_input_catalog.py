"""Authoring identities projected from one exact execution definition.

No business reads, file resolution, input defaults, readiness or target writes.
The original appearance preparation still verifies actual consumption and text.
"""
from studio.services.prompt_profile_structures import STRUCTURED_INPUT_SELECTOR
from studio.services.structure_paths import nonnull_schema, path_schema


def media_appearance_input_options(definition):
    interface = definition["interface"]
    prompt = interface["prompt"]
    primary, differences = [], []
    for spec in sorted(definition["configuration"]["inputs"], key=lambda item: item["position"]):
        for mapping in spec["targets"]:
            if mapping["target_kind"] == "workflow_slot" and mapping["is_primary"] and spec["kind"] == "image":
                target = interface["input_targets"][mapping["target_key"]]
                if target["target_kind"] == "workflow_slot" and target["kind"] == "image":
                    primary.append({"input_key": spec["key"], "target_key": mapping["target_key"]})
            if mapping["target_kind"] != "profile_input" or prompt["mode"] != "profile":
                continue
            target = interface["profile_inputs"][mapping["target_key"]]
            for field in target["fields"]:
                source = None
                if spec["kind"] == "text" and field["usage"] in {"assembly_source", "generation_source"}:
                    source = {"kind": "logical_input", "key": spec["key"]}
                elif spec["kind"] == "json" and field["usage"] == "reference_units":
                    source = {"kind": "reference_field", "key": spec["key"], "field_key": field["field_key"]}
                elif (spec["kind"] == "json" and field["usage"] == "assembly_source"
                        and field["selector"] == STRUCTURED_INPUT_SELECTOR and "source_path" in field
                        and nonnull_schema(path_schema(target["value_schema"], field["source_path"], "source_path")).get("type") == "string"):
                    source = {"kind": "structured_field", "key": spec["key"], "field_key": field["field_key"]}
                if source is not None and source not in differences:
                    differences.append(source)
    if prompt["mode"] == "manual":
        differences = [{"kind": "manual_channel", "key": key} for key in sorted(prompt["channels"])]
    return {"primary_inputs": primary, "difference_sources": differences}
