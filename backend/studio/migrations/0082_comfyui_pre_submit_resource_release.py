import hashlib
import json

from django.db import migrations


TARGET_DEPLOYMENT_KEYS = (
    "comfyui-minimax-h3-ref2va-0p2mp",
    "z-image-turbo-local",
)
RELEASE_POLICY = {
    "schema_version": 1,
    "enabled": True,
    "unload_models": True,
    "free_memory": True,
}


def _canonical_contract(value):
    encoded = json.dumps(
        value,
        ensure_ascii=False,
        sort_keys=True,
        separators=(",", ":"),
        allow_nan=False,
    ).encode("utf-8")
    return len(encoded), hashlib.sha256(encoded).hexdigest()


def _verify_runtime_config(deployment):
    size_bytes, digest = _canonical_contract(deployment.runtime_config)
    if (
        deployment.runtime_config_schema_version != 1
        or deployment.runtime_config_size_bytes != size_bytes
        or deployment.runtime_config_sha256 != digest
    ):
        raise RuntimeError(
            f"Cannot configure pre-submit resource release: deployment {deployment.key} "
            "runtime_config checksum drifted",
        )


def configure_pre_submit_resource_release(apps, schema_editor):
    del schema_editor
    Deployment = apps.get_model("studio", "ModelDeployment")
    Workflow = apps.get_model("studio", "WorkflowTemplateVersion")

    deployments = list(
        Deployment.objects.select_related("profile_version__provider")
        .filter(key__in=TARGET_DEPLOYMENT_KEYS)
        .order_by("key")
    )
    if not deployments:
        # Fresh databases apply migrations before the operator creates local
        # deployment routes. The same policy remains available in Settings.
        return
    observed_keys = {row.key for row in deployments}
    if observed_keys != set(TARGET_DEPLOYMENT_KEYS):
        raise RuntimeError(
            "Cannot configure pre-submit resource release: current E2E ComfyUI "
            "deployment set is incomplete",
        )

    changed_ids = []
    for deployment in deployments:
        _verify_runtime_config(deployment)
        if (
            deployment.status != "active"
            or deployment.profile_version.status != "published"
            or deployment.profile_version.provider.driver_type != "comfyui"
            or not deployment.profile_version.provider.is_active
        ):
            raise RuntimeError(
                f"Cannot configure pre-submit resource release: deployment "
                f"{deployment.key} is not an active published ComfyUI route",
            )
        runtime_config = dict(deployment.runtime_config or {})
        existing = runtime_config.get("pre_submit_resource_release")
        if existing is not None and existing != RELEASE_POLICY:
            raise RuntimeError(
                f"Cannot configure pre-submit resource release: deployment "
                f"{deployment.key} already has a conflicting policy",
            )
        if existing == RELEASE_POLICY:
            continue
        runtime_config["pre_submit_resource_release"] = dict(RELEASE_POLICY)
        size_bytes, digest = _canonical_contract(runtime_config)
        Deployment.objects.filter(pk=deployment.pk).update(
            runtime_config=runtime_config,
            runtime_config_schema_version=1,
            runtime_config_size_bytes=size_bytes,
            runtime_config_sha256=digest,
        )
        changed_ids.append(deployment.pk)

    if not changed_ids:
        return
    # runtime_config participates in the execution target digest.  Keeping a
    # published workflow's old live-validation/smoke evidence would advertise
    # readiness for a target that no longer exists.  Preserve historical task
    # and smoke rows, but make the mutable workflow route prove the new target
    # before it can be published again.
    Workflow.objects.filter(validated_deployment_id__in=changed_ids).update(
        status="draft",
        validation_state="not_run",
        validation_message=(
            "ModelDeployment 提交前资源释放合同已更新；必须重新 live validate、"
            "真实 smoke-test 并发布后才能用于新任务。"
        ),
        smoke_test_state="not_run",
        smoke_test_message=(
            "旧 smoke 证据绑定的是更新前 deployment target，已保留为历史但不再可用。"
        ),
        validated_deployment_id=None,
        validation_evidence={},
        validation_evidence_sha256="",
        smoke_test_evidence={},
        smoke_test_evidence_sha256="",
        is_current=False,
        published_at=None,
    )


class Migration(migrations.Migration):
    dependencies = [("studio", "0081_prompt_normalization_smoke_capacity")]

    operations = [
        migrations.RunPython(
            configure_pre_submit_resource_release,
            migrations.RunPython.noop,
        ),
    ]
