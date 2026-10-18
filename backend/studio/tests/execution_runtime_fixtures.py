"""Isolated recorded host/model observations for real admission checks.

These are test database inputs, not probes of the user's machine. Keep the
production reader and all of its integrity/freshness checks in place.
"""
from datetime import timedelta
from uuid import uuid4

from django.utils import timezone

from studio.models import HostCapabilitySnapshot, ModelArtifact, RuntimeHost, RuntimeModelAvailability
from studio.services.json_contracts import assign_json_contract


def record_workflow_runtime(deployment):
    host = deployment.runtime_host or RuntimeHost.objects.create(
        key=f"isolated-execution-{uuid4().hex}", name="Isolated execution host", status="active")
    deployment.runtime_host = host
    deployment.save(update_fields=("runtime_host", "updated_at"))
    snapshot = HostCapabilitySnapshot(host=host, revision=1, probe_status="succeeded",
        expires_at=timezone.now() + timedelta(hours=1))
    assign_json_contract(snapshot, "details", {"schema_version": 1,
        "test_scope": "isolated recorded observations", "gpus": []})
    snapshot.save()
    for artifact in deployment.profile_version.artifacts.all():
        artifact.status, artifact.size_bytes = ModelArtifact.Status.AVAILABLE, 1
        artifact.save(update_fields=("status", "size_bytes", "updated_at"))
        RuntimeModelAvailability.objects.create(snapshot=snapshot, deployment=deployment, artifact=artifact,
            availability_key=str(artifact.pk), status="available", observed_size_bytes=artifact.size_bytes,
            observed_sha256=artifact.sha256, observation_source="local_filesystem")
    return snapshot
