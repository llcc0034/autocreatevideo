"""Read-only, bounded configuration navigation; never resolve a production route."""

from uuid import UUID

from django.db.models import Count, F, Q, Window
from django.db.models.functions import RowNumber

from studio.models import (
    ModelDeployment, ModelExecutionInstanceVersion, WorkflowProjectorVersion,
    WorkflowTemplateVersion,
)

EXECUTION_PREVIEW_LIMIT = 3


def _uuid(value):
    try:
        return str(UUID(str(value)))
    except (TypeError, ValueError, AttributeError):
        return None


def workflow_configuration_links(version_ids):
    """Batch one API page, selecting JSON identity paths instead of graph/definitions."""
    rows = list(WorkflowTemplateVersion.objects.filter(pk__in=version_ids).order_by().values(
        "id", "model_profile_version_id", "model_profile_version__profile_key",
        "model_profile_version__version", "model_profile_version__status",
        "validated_deployment_id", "validated_deployment__name",
        "validated_deployment__status", "validated_deployment__profile_version_id",
        "input_contract__contract_projector",
    ))
    profile_ids = {row["model_profile_version_id"] for row in rows} - {None}
    instances = dict(ModelDeployment.objects.filter(profile_version_id__in=profile_ids)
                     .order_by().values("profile_version_id").annotate(total=Count("id"))
                     .values_list("profile_version_id", "total")) if profile_ids else {}
    executions = ModelExecutionInstanceVersion.objects.filter(workflow_version_id__in=[row['id'] for row in rows])
    enabled = Q(status='published', instance__is_active=True)
    totals = {}
    active_totals = {}
    previews = {}
    if rows:
        for row in executions.order_by().values("workflow_version_id").annotate(
            total=Count("id"), active_total=Count("id", filter=enabled),
        ):
            totals[row['workflow_version_id']] = row['total']
            active_totals[row['workflow_version_id']] = row['active_total']
        # Filter before ranking; disabled/draft versions cannot hide published
        # choices. A preview is navigation, never an automatic route selection.
        for execution in executions.filter(enabled).annotate(position=Window(
            expression=RowNumber(), partition_by=[F("workflow_version_id")],
            order_by=[F("instance__key").asc(), F("version").desc()],
        )).filter(position__lte=EXECUTION_PREVIEW_LIMIT).order_by("instance__key", "-version").values(
            "id", "workflow_version_id", "instance__name", "version", "status",
            "operation", "mode", "prompt_mode",
        ):
            previews.setdefault(execution['workflow_version_id'], []).append({
                "id": str(execution['id']), "name": execution['instance__name'],
                "version": execution['version'], "status": execution['status'],
                "operation": execution['operation'], "mode": execution['mode'],
                "prompt_mode": execution['prompt_mode'],
            })
    identities = [row["input_contract__contract_projector"] for row in rows]
    projector_ids = {_uuid(item.get("version_id")) for item in identities if isinstance(item, dict)} - {None}
    projectors = {str(row["id"]): row for row in WorkflowProjectorVersion.objects.filter(pk__in=projector_ids).order_by().values(
        "id", "projector__key", "projector__name", "projector__is_active", "version", "status", "is_current",
        "definition_sha256", "provider_driver_type", "operation", "output_kind", "engine_key", "engine_version",
    )} if projector_ids else {}
    result = {}
    for row in rows:
        profile_id = row["model_profile_version_id"]
        model = None if not profile_id else {
            "id": str(profile_id), "key": row["model_profile_version__profile_key"],
            "version": row["model_profile_version__version"], "status": row["model_profile_version__status"],
            "instance_count": instances.get(profile_id, 0),
        }
        deployment = None if not row["validated_deployment_id"] else {
            "id": str(row["validated_deployment_id"]), "name": row["validated_deployment__name"],
            "status": row["validated_deployment__status"],
            "model_profile_version_id": str(row["validated_deployment__profile_version_id"]),
        }
        identity = row["input_contract__contract_projector"]
        projector = {"state": "not_recorded", "version_id": None, "name": "", "key": "", "version": None,
                     "status": "", "is_current": False, "is_active": False, "definition_sha256": ""}
        if identity is not None:
            projector["state"] = "invalid"
            if isinstance(identity, dict):
                target = projectors.get(_uuid(identity.get("version_id")))
                if target:
                    expected = {
                        "id": target["projector__key"], "version": target["version"],
                        "version_id": str(target["id"]), "definition_sha256": target["definition_sha256"],
                        "provider_driver_type": target["provider_driver_type"], "operation": target["operation"],
                        "output_kind": target["output_kind"],
                        "engine": {"key": target["engine_key"], "version": target["engine_version"]},
                    }
                    if identity == expected and target["status"] in {"published", "retired"}:
                        projector.update(
                            state="linked", version_id=str(target["id"]), name=target["projector__name"],
                            key=target["projector__key"], version=target["version"], status=target["status"],
                            is_current=target["is_current"], is_active=target["projector__is_active"],
                            definition_sha256=target["definition_sha256"],
                        )
        result[str(row["id"])] = {
            "model": model, "validated_deployment": deployment, "projector": projector,
            "execution_instances": {
                "total": totals.get(row['id'], 0), "active_total": active_totals.get(row['id'], 0),
                "preview": previews.get(row['id'], []),
            },
        }
    return result
