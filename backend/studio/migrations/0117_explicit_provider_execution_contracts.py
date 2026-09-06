import hashlib
import json

from django.db import migrations


ASYNC_REST_LEGACY_CONTRACT = {
    "health_path": "/health",
    "submit_path": "/jobs",
    "poll_path": "/jobs/{request_id}",
    "cancel_path": "/jobs/{request_id}",
    "collect_path": "/jobs/{request_id}",
    "request_id_field": "id",
    "status_field": "status",
    "artifacts_field": "artifacts",
    "artifact_url_field": "url",
    "artifact_kind_field": "kind",
    "artifact_id_field": "id",
    "artifact_sha256_field": "sha256",
    "actual_cost_field": "actual_cost",
    "currency_field": "currency",
    "idempotency_header": "Idempotency-Key",
    "poll_interval_seconds": 2,
    "max_poll_seconds": 3_600,
}


def _canonical(value):
    return json.dumps(
        value,
        ensure_ascii=False,
        sort_keys=True,
        separators=(",", ":"),
        allow_nan=False,
    ).encode("utf-8")


def _contract(value):
    payload = _canonical(value)
    return len(payload), hashlib.sha256(payload).hexdigest()


def _save_contract(row, field_name, value):
    size, digest = _contract(value)
    setattr(row, field_name, value)
    setattr(row, f"{field_name}_schema_version", 1)
    setattr(row, f"{field_name}_size_bytes", size)
    setattr(row, f"{field_name}_sha256", digest)
    row.save(update_fields=(
        field_name,
        f"{field_name}_schema_version",
        f"{field_name}_size_bytes",
        f"{field_name}_sha256",
        "updated_at",
    ))


def materialize_provider_execution_contracts(apps, schema_editor):
    Provider = apps.get_model("studio", "ProviderProfile")
    Deployment = apps.get_model("studio", "ModelDeployment")
    Revision = apps.get_model("studio", "ConfigRevision")
    Value = apps.get_model("studio", "ConfigValue")

    active_revision = Revision.objects.filter(
        scope_type="system",
        status="published",
        activation_state="active",
    ).order_by("-revision").first()
    if active_revision is None:
        raise RuntimeError("0117 Provider 执行合同缺少 active system revision")
    ttl_row = Value.objects.filter(
        revision=active_revision,
        definition__key="runtime.capability_ttl_seconds",
    ).first()
    if ttl_row is None or type(ttl_row.value) is not int or ttl_row.value <= 0:
        raise RuntimeError("0117 Provider 执行合同缺少有效 capability TTL 配置")
    capability_ttl_seconds = ttl_row.value

    providers = {row.pk: row for row in Provider.objects.all()}
    for provider in providers.values():
        config = dict(provider.driver_config or {})
        changed = False
        if provider.driver_type == "async_rest":
            for key, value in ASYNC_REST_LEGACY_CONTRACT.items():
                if key not in config:
                    config[key] = value
                    changed = True
            if "capability_ttl_seconds" not in config:
                config["capability_ttl_seconds"] = capability_ttl_seconds
                changed = True
            if config.get("progress_field") and "progress_max" not in config:
                config["progress_max"] = 100
                changed = True
        elif provider.driver_type == "openai_compatible":
            if "chat_path" not in config:
                config["chat_path"] = "/v1/chat/completions"
                changed = True
            if "timeout_seconds" not in config:
                config["timeout_seconds"] = 60
                changed = True
            if "capability_ttl_seconds" not in config:
                config["capability_ttl_seconds"] = capability_ttl_seconds
                changed = True
        elif (
            provider.driver_type in {"local_qwen", "deepseek_api"}
            and config.get("runtime_source") == "openai_compatible_endpoint"
        ):
            if "chat_path" not in config:
                config["chat_path"] = "/v1/chat/completions"
                changed = True
            if "timeout_seconds" not in config:
                config["timeout_seconds"] = 60
                changed = True
            if "capability_ttl_seconds" not in config:
                config["capability_ttl_seconds"] = capability_ttl_seconds
                changed = True
        if changed:
            _save_contract(provider, "driver_config", config)

    for deployment in Deployment.objects.select_related(
        "profile_version__provider",
    ).all():
        provider = providers[deployment.profile_version.provider_id]
        provider_config = dict(provider.driver_config or {})
        config = dict(deployment.runtime_config or {})
        combined = {**provider_config, **config}
        changed = False
        if provider.driver_type in {
            "local_qwen", "deepseek_api", "openai_compatible",
        }:
            if combined.get("runtime_source") == "deployment":
                config["runtime_source"] = "openai_compatible_endpoint"
                combined["runtime_source"] = "openai_compatible_endpoint"
                changed = True
            if "chat_path" not in combined:
                config["chat_path"] = "/v1/chat/completions"
                combined["chat_path"] = config["chat_path"]
                changed = True
            if "timeout_seconds" not in combined:
                config["timeout_seconds"] = 60
                combined["timeout_seconds"] = config["timeout_seconds"]
                changed = True
            if "capability_ttl_seconds" not in combined:
                config["capability_ttl_seconds"] = capability_ttl_seconds
                changed = True
        elif provider.driver_type == "comfyui":
            if not str(combined.get("base_url") or "").strip():
                if deployment.status == "active":
                    raise RuntimeError(
                        f"0117 active ComfyUI Deployment {deployment.key} 缺少 base_url",
                    )
                continue
            for key, value in {
                "timeout_seconds": 30,
                "poll_interval_seconds": 2,
                "max_poll_seconds": 3_600,
                "capability_ttl_seconds": capability_ttl_seconds,
            }.items():
                if key not in combined:
                    config[key] = value
                    combined[key] = value
                    changed = True
        elif provider.driver_type == "async_rest":
            if "capability_ttl_seconds" not in combined:
                config["capability_ttl_seconds"] = capability_ttl_seconds
                changed = True
        if changed:
            _save_contract(deployment, "runtime_config", config)


class Migration(migrations.Migration):
    dependencies = [("studio", "0116_runtime_policy_configuration")]

    operations = [
        migrations.RunPython(
            materialize_provider_execution_contracts,
            migrations.RunPython.noop,
        ),
    ]
