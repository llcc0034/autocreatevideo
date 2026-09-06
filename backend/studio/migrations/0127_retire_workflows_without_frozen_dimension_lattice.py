from django.db import migrations
from django.db.models import Q


SUPPORTED_COMPILERS = {("generic.comfy", 1), ("minimax_h3", 2), ("minimax_h3", 3)}
MEDIA_OUTPUT_KINDS = {"image", "video"}


def _positive_integer(value):
    return isinstance(value, int) and not isinstance(value, bool) and value > 0


def _value_matches_lattice(value, definition, multiple):
    if not _positive_integer(value) or value % multiple:
        return False
    minimum = definition.get("min")
    maximum = definition.get("max")
    if (
        isinstance(minimum, (int, float))
        and not isinstance(minimum, bool)
        and value < minimum
    ):
        return False
    if (
        isinstance(maximum, (int, float))
        and not isinstance(maximum, bool)
        and value > maximum
    ):
        return False
    return True


def _has_frozen_dimension_lattice(version):
    contract = version.input_contract
    graph = version.graph
    if not isinstance(contract, dict) or not isinstance(graph, dict):
        return False
    schema_version = contract.get("schema_version")
    if not _positive_integer(schema_version):
        return False
    parameters = contract.get("parameters")
    if not isinstance(parameters, dict):
        return False
    required_multiple = contract.get("required_dimension_multiple")
    if not _positive_integer(required_multiple):
        return False

    definitions = {}
    for key in ("image.width", "image.height"):
        definition = parameters.get(key)
        if not isinstance(definition, dict):
            return False
        if definition.get("multiple_of") != required_multiple:
            return False
        if "default" in definition and not _value_matches_lattice(
            definition["default"], definition, required_multiple,
        ):
            return False
        definitions[key] = definition

    bindings = list(
        version.semantic_bindings.filter(
            semantic_key__in=("image.width", "image.height"),
        ).values("semantic_key", "node_id", "input_name", "value_type")
    )
    if not bindings or {row["semantic_key"] for row in bindings} != {
        "image.width", "image.height",
    }:
        return False
    by_node = {}
    for row in bindings:
        if row["value_type"] != "integer":
            return False
        node = graph.get(str(row["node_id"]))
        inputs = node.get("inputs") if isinstance(node, dict) else None
        if not isinstance(inputs, dict) or row["input_name"] not in inputs:
            return False
        if not _value_matches_lattice(
            inputs[row["input_name"]],
            definitions[row["semantic_key"]],
            required_multiple,
        ):
            return False
        by_node.setdefault(str(row["node_id"]), set()).add(row["semantic_key"])
    expected_keys = {"image.width", "image.height"}
    if not by_node or any(keys != expected_keys for keys in by_node.values()):
        return False

    strategy = contract.get("resolution_strategy")
    if strategy is None and schema_version < 3:
        # Schema v2 had only the paired explicit-width/height shape. The
        # lattice itself still has to be frozen above; nothing is inferred
        # from a runtime host or a model default.
        return True
    if not isinstance(strategy, dict) or strategy.get("mode") != "explicit_width_height":
        return False
    node_ids = strategy.get("node_ids")
    return (
        isinstance(node_ids, list)
        and node_ids == sorted(set(by_node))
        and all(isinstance(node_id, str) and node_id for node_id in node_ids)
    )


def retire_workflows_without_frozen_dimension_lattice(apps, schema_editor):
    Version = apps.get_model("studio", "WorkflowTemplateVersion")
    candidates = Version.objects.filter(
        Q(status__in=("validated", "published")) | Q(is_current=True),
        template__output_kind__in=MEDIA_OUTPUT_KINDS,
    ).select_related("template")
    for version in candidates.iterator():
        if (version.compiler_id, version.compiler_version) not in SUPPORTED_COMPILERS:
            continue
        if _has_frozen_dimension_lattice(version):
            continue
        if version.status == "published":
            # Preserve immutable publication and smoke evidence as historical
            # facts, but remove the row from future production admission.
            Version.objects.filter(pk=version.pk).update(
                status="retired",
                is_current=False,
            )
            continue
        if version.status == "validated":
            Version.objects.filter(pk=version.pk).update(
                status="draft",
                validation_state="not_run",
                validation_message=(
                    "needs_review: workflow 缺少可证明的冻结尺寸倍数合同；"
                    "请导入新版本并重新 Live Validate、Smoke、Publish。"
                ),
                smoke_test_state="not_run",
                smoke_test_message=(
                    "needs_review: 旧 smoke 未验证当前 required_dimension_multiple 合同。"
                ),
                validated_deployment_id=None,
                validation_evidence={},
                validation_evidence_sha256="",
                smoke_test_evidence={},
                smoke_test_evidence_sha256="",
                is_current=False,
            )
            continue
        Version.objects.filter(pk=version.pk).update(is_current=False)


class Migration(migrations.Migration):
    dependencies = [
        ("studio", "0126_configurable_canonical_context_and_provider_limits"),
    ]

    operations = [
        migrations.RunPython(
            retire_workflows_without_frozen_dimension_lattice,
            migrations.RunPython.noop,
        ),
    ]
