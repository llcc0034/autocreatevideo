import hashlib
import json
from urllib.parse import urlparse

from django.db import migrations, models
from django.db.models import Count


def _canonical_bytes(value):
    return json.dumps(
        value,
        ensure_ascii=False,
        sort_keys=True,
        separators=(",", ":"),
        allow_nan=False,
    ).encode("utf-8")


def _canonical_endpoint(value):
    parsed = urlparse(str(value or "").strip().rstrip("/"))
    scheme = parsed.scheme.casefold()
    hostname = (parsed.hostname or "").casefold()
    if scheme not in {"http", "https"} or not hostname:
        raise RuntimeError("ComfyWorkflowSource 历史 deployment 缺少有效 base_url")
    default_port = 443 if scheme == "https" else 80
    try:
        port = parsed.port
    except ValueError as exc:
        raise RuntimeError("ComfyWorkflowSource 历史 deployment 的 base_url 端口无效") from exc
    display_host = f"[{hostname}]" if ":" in hostname else hostname
    authority = display_host if port in {None, default_port} else f"{display_host}:{port}"
    path = (parsed.path or "").rstrip("/")
    return f"{scheme}://{authority}{path}"


def _scope_sha256(deployment):
    provider = deployment.profile_version.provider
    config = {
        **(provider.driver_config if isinstance(provider.driver_config, dict) else {}),
        **(deployment.runtime_config if isinstance(deployment.runtime_config, dict) else {}),
    }
    payload = {
        "schema_version": 1,
        "kind": "comfyui.userdata.workflows",
        "provider_id": str(provider.id),
        "runtime_host_id": str(deployment.runtime_host_id or ""),
        "endpoint": _canonical_endpoint(config.get("base_url")),
    }
    return hashlib.sha256(_canonical_bytes(payload)).hexdigest()


def _legacy_deployment_fingerprint(deployment):
    provider = deployment.profile_version.provider
    payload = {
        "schema_version": 1,
        "deployment_id": str(deployment.id),
        "deployment_status": deployment.status,
        "deployment_runtime_config_sha256": deployment.runtime_config_sha256,
        "runtime_host_id": str(deployment.runtime_host_id or ""),
        "profile_version_id": str(deployment.profile_version_id),
        "profile_status": deployment.profile_version.status,
        "provider_id": str(provider.id),
        "provider_active": provider.is_active,
        "provider_driver_type": provider.driver_type,
        "provider_driver_config_sha256": provider.driver_config_sha256,
    }
    return hashlib.sha256(_canonical_bytes(payload)).hexdigest()


def _quarantined_legacy_scope(source, stored_fingerprint):
    payload = {
        "schema_version": 1,
        "kind": "comfyui.userdata.workflows.legacy_quarantine",
        "source_deployment_id": str(source.source_deployment_id),
        "historical_deployment_fingerprint_sha256": stored_fingerprint,
        **({"source_id": str(source.id)} if not stored_fingerprint else {}),
    }
    return hashlib.sha256(_canonical_bytes(payload)).hexdigest()


def backfill_source_scope(apps, schema_editor):
    Source = apps.get_model("studio", "ComfyWorkflowSource")
    rows = Source.objects.select_related(
        "source_deployment__credential__active_version",
        "source_deployment__profile_version__provider__credential__active_version",
    ).order_by("id")
    for source in rows.iterator():
        report = source.conversion_report if isinstance(source.conversion_report, dict) else {}
        stored_fingerprint = str(
            report.get("source_deployment_fingerprint_sha256") or ""
        ).casefold()
        current_fingerprint = _legacy_deployment_fingerprint(source.source_deployment)
        scope_sha256 = (
            _scope_sha256(source.source_deployment)
            if stored_fingerprint and stored_fingerprint == current_fingerprint
            else _quarantined_legacy_scope(source, stored_fingerprint)
        )
        path_sha256 = hashlib.sha256(source.source_path_key.encode("utf-8")).hexdigest()
        Source.objects.filter(pk=source.pk).update(
            source_scope_schema_version=1,
            source_scope_sha256=scope_sha256,
            source_path_sha256=path_sha256,
        )

    duplicate_keys = list(
        Source.objects.values(
            "source_scope_schema_version",
            "source_scope_sha256",
            "source_path_sha256",
            "source_sha256",
            "import_contract_sha256",
        )
        .annotate(row_count=Count("id"))
        .filter(row_count__gt=1)
        .order_by("source_scope_sha256", "source_path_sha256")[:10]
    )
    if duplicate_keys:
        raise RuntimeError(
            "ComfyWorkflowSource 新来源范围存在不可自动合并的重复事实: "
            + json.dumps(duplicate_keys, ensure_ascii=False, default=str)
        )


class Migration(migrations.Migration):

    dependencies = [
        ("studio", "0132_h3_declarative_prompt_profile_v19"),
    ]

    operations = [
        migrations.AddField(
            model_name="comfyworkflowsource",
            name="source_scope_schema_version",
            field=models.PositiveSmallIntegerField(default=1),
        ),
        migrations.AddField(
            model_name="comfyworkflowsource",
            name="source_scope_sha256",
            field=models.CharField(default="", max_length=64),
            preserve_default=False,
        ),
        migrations.AddField(
            model_name="comfyworkflowsource",
            name="source_path_sha256",
            field=models.CharField(default="", max_length=64),
            preserve_default=False,
        ),
        migrations.RunPython(backfill_source_scope, migrations.RunPython.noop),
        migrations.RemoveConstraint(
            model_name="comfyworkflowsource",
            name="unique_comfy_workflow_source",
        ),
        migrations.RemoveIndex(
            model_name="comfyworkflowsource",
            name="studio_comfy_source_path_idx",
        ),
        migrations.AlterModelOptions(
            name="comfyworkflowsource",
            options={
                "ordering": (
                    "source_scope_sha256",
                    "source_path_key",
                    "-created_at",
                ),
            },
        ),
        migrations.AddConstraint(
            model_name="comfyworkflowsource",
            constraint=models.UniqueConstraint(
                fields=(
                    "source_scope_schema_version",
                    "source_scope_sha256",
                    "source_path_sha256",
                    "source_sha256",
                    "import_contract_sha256",
                ),
                name="uq_comfy_src_scope_contract",
            ),
        ),
        migrations.AddIndex(
            model_name="comfyworkflowsource",
            index=models.Index(
                fields=(
                    "source_scope_schema_version",
                    "source_scope_sha256",
                    "source_path_sha256",
                    "-created_at",
                ),
                name="studio_comfy_scope_path_idx",
            ),
        ),
    ]
