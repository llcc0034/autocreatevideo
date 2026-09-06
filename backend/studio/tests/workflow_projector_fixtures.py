"""Explicit published Projector references for workflow execution fixtures."""

from copy import deepcopy

from studio.services.workflow_projectors import current_projector_catalog


def workflow_projector_reference(key="generic.comfy.fixed", *, image_resolution=None):
    projector = next(row for row in current_projector_catalog() if row.key == key)
    if image_resolution is not None:
        from dataclasses import replace
        from uuid import uuid4
        from studio.models import WorkflowProjector, WorkflowProjectorVersion
        from studio.services.json_contracts import assign_json_contract

        definition = deepcopy(projector.definition)
        definition["projection"]["config"]["image_resolution"] = deepcopy(image_resolution)
        family = WorkflowProjector.objects.create(key=f"fixture.resolution.{uuid4().hex}", name="Resolution fixture")
        version = WorkflowProjectorVersion(projector=family, version=1, status="published", is_current=True,
            provider_driver_type=projector.provider_driver_type, operation=projector.operation,
            output_kind=projector.output_kind, engine_key=projector.engine_key, engine_version=projector.engine_version)
        assign_json_contract(version, "definition", definition)
        version.save()
        projector = replace(projector, version_id=str(version.pk), key=family.key,
            definition_sha256=version.definition_sha256, definition=definition)
    return {
        "contract_projector": deepcopy(projector.identity),
        "workflow_adapters": deepcopy(projector.definition["adapters"]),
    }
