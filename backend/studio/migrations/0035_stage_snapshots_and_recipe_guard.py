import hashlib
import json
from copy import deepcopy

from django.db import migrations, models


def _contract(value):
    payload = json.dumps(
        value,
        ensure_ascii=False,
        sort_keys=True,
        separators=(",", ":"),
        allow_nan=False,
    ).encode("utf-8")
    return len(payload), hashlib.sha256(payload).hexdigest()


def _non_secret(value):
    if isinstance(value, list):
        return [_non_secret(item) for item in value]
    if not isinstance(value, dict):
        return value
    result = {}
    for raw_key, item in value.items():
        key = str(raw_key)
        normalized = key.casefold().replace("-", "_")
        if any(part in normalized for part in (
            "secret", "token", "password", "api_key", "authorization", "credential",
        )):
            continue
        result[key] = _non_secret(item)
    return result


def backfill_stage_contracts_and_guard_recipe(apps, schema_editor):
    StageRun = apps.get_model("studio", "StageRun")
    WorkflowVersion = apps.get_model("studio", "WorkflowTemplateVersion")
    Deployment = apps.get_model("studio", "ModelDeployment")
    empty_size, empty_sha = _contract({})
    StageRun.objects.filter(input_snapshot_sha256="").update(
        input_snapshot={},
        input_snapshot_schema_version=1,
        input_snapshot_size_bytes=empty_size,
        input_snapshot_sha256=empty_sha,
    )
    StageRun.objects.filter(output_snapshot_sha256="").update(
        output_snapshot={},
        output_snapshot_schema_version=1,
        output_snapshot_size_bytes=empty_size,
        output_snapshot_sha256=empty_sha,
    )
    for stage in StageRun.objects.exclude(routing_sha256=""):
        routing = deepcopy(stage.routing) if isinstance(stage.routing, dict) else {}
        routes = routing.get("routes") if isinstance(routing.get("routes"), list) else []
        changed = False
        for route in routes:
            if not isinstance(route, dict) or route.get("workflow_snapshot"):
                continue
            try:
                deployment = Deployment.objects.select_related(
                    "profile_version__provider",
                ).get(pk=route.get("model_deployment_id"))
            except (Deployment.DoesNotExist, ValueError, TypeError):
                continue
            workflow = None
            if route.get("workflow_version_id"):
                try:
                    workflow = WorkflowVersion.objects.select_related("template").get(
                        pk=route["workflow_version_id"],
                    )
                except (WorkflowVersion.DoesNotExist, ValueError, TypeError):
                    continue
            provider_config = _non_secret(deployment.profile_version.provider.driver_config or {})
            deployment_config = _non_secret(deployment.runtime_config or {})
            route["provider_config_record_sha256"] = route.get("provider_config_sha256", "")
            route["deployment_config_record_sha256"] = route.get("deployment_config_sha256", "")
            route["provider_config"] = provider_config
            route["deployment_config"] = deployment_config
            route["provider_config_sha256"] = _contract(provider_config)[1]
            route["deployment_config_sha256"] = _contract(deployment_config)[1]
            route["model_identifier"] = deployment.profile_version.model_identifier
            if workflow is not None:
                bindings = [
                    {
                        "semantic_key": row.semantic_key,
                        "node_id": row.node_id,
                        "input_name": row.input_name,
                        "value_type": row.value_type,
                        "is_required": row.is_required,
                    }
                    for row in workflow.semantic_bindings.order_by("semantic_key", "id")
                ]
                requirements = [
                    {
                        "role": row.role,
                        "alias": row.alias,
                        "artifact_id": str(row.artifact_id) if row.artifact_id else None,
                        "expected_filename": row.expected_filename,
                        "expected_sha256": row.expected_sha256,
                        "is_required": row.is_required,
                    }
                    for row in workflow.model_requirements.order_by("role", "alias", "id")
                ]
                route["workflow_snapshot"] = {
                    "schema_version": 1,
                    "template": {
                        "id": str(workflow.template_id),
                        "slug": workflow.template.slug,
                        "name": workflow.template.name,
                        "output_kind": workflow.template.output_kind,
                    },
                    "graph": workflow.graph,
                    "graph_schema_version": workflow.graph_schema_version,
                    "graph_size_bytes": workflow.graph_size_bytes,
                    "graph_sha256": workflow.graph_sha256,
                    "input_contract": workflow.input_contract,
                    "input_contract_schema_version": workflow.input_contract_schema_version,
                    "input_contract_size_bytes": workflow.input_contract_size_bytes,
                    "input_contract_sha256": workflow.input_contract_sha256,
                    "semantic_bindings": bindings,
                    "semantic_bindings_sha256": _contract(bindings)[1],
                    "model_requirements": requirements,
                    "model_requirements_sha256": _contract(requirements)[1],
                }
            changed = True
        if changed:
            size, digest = _contract(routing)
            stage.routing = routing
            stage.routing_size_bytes = size
            stage.routing_sha256 = digest
            stage.save(update_fields=("routing", "routing_size_bytes", "routing_sha256"))

    Recipe = apps.get_model("studio", "ProductionRecipeVersion")
    for recipe in Recipe.objects.filter(
        recipe_key="default-series-production",
        version=1,
        status="published",
    ):
        definition = recipe.definition if isinstance(recipe.definition, dict) else {}
        stages = definition.get("stages") if isinstance(definition.get("stages"), list) else []
        media = next(
            (item for item in stages if isinstance(item, dict) and item.get("key") == "media_generation"),
            None,
        )
        routing = media.get("routing") if isinstance(media, dict) else None
        if not isinstance(routing, dict) or not routing.get("routes"):
            recipe.status = "draft"
            recipe.published_at = None
            recipe.published_by_id = None
            recipe.save(update_fields=("status", "published_at", "published_by"))


class Migration(migrations.Migration):

    dependencies = [
        ("studio", "0034_route_events_selection_and_cost_constraints"),
    ]

    operations = [
        migrations.AddField(
            model_name="stagerun",
            name="input_snapshot",
            field=models.JSONField(default=dict),
        ),
        migrations.AddField(
            model_name="stagerun",
            name="input_snapshot_schema_version",
            field=models.PositiveIntegerField(default=1),
        ),
        migrations.AddField(
            model_name="stagerun",
            name="input_snapshot_size_bytes",
            field=models.PositiveIntegerField(default=2),
        ),
        migrations.AddField(
            model_name="stagerun",
            name="input_snapshot_sha256",
            field=models.CharField(blank=True, max_length=64),
        ),
        migrations.AddField(
            model_name="stagerun",
            name="output_snapshot",
            field=models.JSONField(default=dict),
        ),
        migrations.AddField(
            model_name="stagerun",
            name="output_snapshot_schema_version",
            field=models.PositiveIntegerField(default=1),
        ),
        migrations.AddField(
            model_name="stagerun",
            name="output_snapshot_size_bytes",
            field=models.PositiveIntegerField(default=2),
        ),
        migrations.AddField(
            model_name="stagerun",
            name="output_snapshot_sha256",
            field=models.CharField(blank=True, max_length=64),
        ),
        migrations.AddField(
            model_name="approvaldecision",
            name="output_snapshot_sha256",
            field=models.CharField(blank=True, max_length=64),
        ),
        migrations.RunPython(
            backfill_stage_contracts_and_guard_recipe,
            migrations.RunPython.noop,
        ),
    ]
