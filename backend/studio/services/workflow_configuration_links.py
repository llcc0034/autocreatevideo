"""Read-only, bounded configuration navigation; never resolve a production route."""

from uuid import UUID

from django.db.models import Count, F, Q, Window
from django.db.models.functions import RowNumber

from studio.models import (
    ModelDeployment, PromptProfileBinding, WorkflowProjectorVersion,
    WorkflowTemplateVersion,
)

PROMPT_PREVIEW_LIMIT = 3


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
        "model_profile_version__specification__model_family",
        "validated_deployment_id", "validated_deployment__name",
        "validated_deployment__status", "validated_deployment__profile_version_id",
        "input_contract__contract_projector",
    ))
    profile_ids = {row["model_profile_version_id"] for row in rows} - {None}
    families = {
        value.strip().lower() for row in rows
        if isinstance(value := row["model_profile_version__specification__model_family"], str)
        and value.strip()
    }
    instances = dict(ModelDeployment.objects.filter(profile_version_id__in=profile_ids)
                     .order_by().values("profile_version_id").annotate(total=Count("id"))
                     .values_list("profile_version_id", "total")) if profile_ids else {}
    bindings = PromptProfileBinding.objects.filter(
        Q(model_profile_version_id__in=profile_ids)
        | Q(model_profile_version__isnull=True, model_family__in=families)
    )
    totals = {}
    active_totals = {}
    previews = {}
    if profile_ids:
        for row in bindings.order_by().values("model_profile_version_id", "model_family").annotate(
            total=Count("id"), active_total=Count("id", filter=Q(is_active=True)),
        ):
            scope = row["model_profile_version_id"] or row["model_family"]
            totals[scope] = totals.get(scope, 0) + row["total"]
            active_totals[scope] = active_totals.get(scope, 0) + row["active_total"]
        # Filter before ranking: inactive bindings must not crowd out active ones.
        # A window prevents large binding catalogs from becoming list payloads.
        for binding in bindings.filter(is_active=True).annotate(position=Window(
            expression=RowNumber(), partition_by=[F("model_profile_version_id"), F("model_family")],
            order_by=[F("priority").desc(), F("key").asc()],
        )).filter(position__lte=PROMPT_PREVIEW_LIMIT).order_by("-priority", "key").values(
            "id", "key", "model_profile_version_id", "model_family", "priority",
            "operation", "mode", "is_active", "prompt_profile_version_id",
            "prompt_profile_version__name", "prompt_profile_version__version",
            "prompt_profile_version__status",
        ):
            scope = binding["model_profile_version_id"] or binding["model_family"]
            previews.setdefault(scope, []).append({
                "binding_id": str(binding["id"]),
                "profile_id": str(binding["prompt_profile_version_id"]),
                "name": binding["prompt_profile_version__name"],
                "version": binding["prompt_profile_version__version"],
                "status": binding["prompt_profile_version__status"],
                "scope": "exact_model" if binding["model_profile_version_id"] else "model_family",
                "operation": binding["operation"], "mode": binding["mode"],
                "is_active": binding["is_active"],
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
        family_value = row["model_profile_version__specification__model_family"]
        family = family_value.strip().lower() if isinstance(family_value, str) else ""
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
            "prompt_bindings": {
                "state": "model_unbound" if not profile_id else "family_missing" if not family else "available",
                "total": totals.get(profile_id, 0) + (totals.get(family, 0) if profile_id else 0),
                "active_total": active_totals.get(profile_id, 0) + (active_totals.get(family, 0) if profile_id else 0),
                "preview": (previews.get(profile_id, []) + (previews.get(family, []) if profile_id else []))[:PROMPT_PREVIEW_LIMIT],
            },
        }
    return result
