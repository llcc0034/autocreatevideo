"""Deterministic logical labels from exact reference identities, never names.

Physical input labels remain unchanged. The optional versioned rendering policy
owns the label spelling; all consumers use this same derived map.
"""
from studio.services.prompt_compiler_contract import PromptBuildError


def reference_subject_labels(context, policy):
    layout = policy.get("reference_layout")
    if layout is None:
        return {}
    known = {(row.category, row.identity) for row in context.subject_facts}
    physical = {row[key] for row in context.reference_bindings
                for key in ("input_label", "audio_input_label") if row.get(key)}
    result = {}
    for row in context.reference_bindings:
        if not row.get("entity_id"):
            continue
        key = (row.get("entity_category", row.get("category")), str(row["entity_id"]))
        if key not in known:
            raise PromptBuildError("逻辑主体标签必须来自已解析的确切主体身份")
        if key not in result:
            label = layout["subject_label_template"].format(index=len(result) + 1)
            if label in physical:
                raise PromptBuildError("逻辑主体标签不能与实际素材端口标签冲突")
            result[key] = {"label": label, "source_labels": [], "unit_keys": [],
                           "category": key[0], "identity": key[1]}
        entry = result[key]
        if row["input_label"] not in entry["source_labels"]:
            entry["source_labels"].append(row["input_label"])
        for unit in context.ordered_units:
            if (key in unit.subject_keys and ("unit_keys" not in row or unit.unit_key in row["unit_keys"])
                    and unit.unit_key not in entry["unit_keys"]):
                entry["unit_keys"].append(unit.unit_key)
    return result


def declared_reference_labels(context, policy, *, unit_key=None):
    units = {unit.unit_key: unit for unit in context.ordered_units}
    if unit_key is not None and unit_key not in units:
        raise PromptBuildError("引用标签必须属于本次明确单元")
    rows = [row for row in context.reference_bindings if unit_key is None or (
        ("unit_keys" not in row or unit_key in row["unit_keys"])
        and (not row.get("entity_id") or
             (row.get("entity_category", row.get("category")), str(row["entity_id"])) in units[unit_key].subject_keys))]
    labels = [row[key] for row in rows
              for key in ("input_label", "audio_input_label") if row.get(key)]
    labels.extend(row["label"] for row in reference_subject_labels(context, policy).values()
                  if unit_key is None or unit_key in row["unit_keys"])
    return list(dict.fromkeys(labels))
