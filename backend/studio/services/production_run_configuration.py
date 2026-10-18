"""One configuration/authorization merge for either ProductionRun source."""
from django.core.exceptions import ValidationError

from studio.models import ConfigRevision
from studio.services.configuration import (
    create_config_revision, publish_config_revision, resolve_configuration, validate_config_override_values,
)
from studio.services.json_contracts import assign_json_contract
from studio.services.security_boundaries import boundary_int


def prepare_run_configuration(*, base_config, config_overrides, allow_external_calls):
    if not isinstance(config_overrides, dict):
        raise ValidationError({"config_overrides": "运行覆盖必须是明确对象"})
    if allow_external_calls is not None and type(allow_external_calls) is not bool:
        raise ValidationError({"allow_external_calls": "必须是布尔值或不覆盖"})
    merged = dict(config_overrides)
    for key, value in (("production.allow_external_calls", allow_external_calls),):
        if value is None:
            continue
        if key in merged and (type(merged[key]) is not type(value) or merged[key] != value):
            raise ValidationError({"config_overrides": f"{key} 与同名运行参数冲突"})
        merged[key] = value
    validated = validate_config_override_values(ConfigRevision.ScopeType.RUN, merged)
    effective = {**base_config.values, **validated}
    external = effective["production.allow_external_calls"]
    if type(external) is not bool:
        raise ValidationError({"allow_external_calls": "配置中的外发授权不是布尔值"})
    return merged, validated, effective, external


def freeze_run_configuration(run, *, overrides, actor):
    """Called only inside the original Run creation transaction."""
    if overrides:
        revision = create_config_revision(scope_type="run", run=run, project=None, values=overrides,
            created_by=actor, change_summary=f"ProductionRun {run.pk} 创建时覆盖", clone_published=False)
        run.config_revision = publish_config_revision(revision, published_by=actor)
    resolved = resolve_configuration(run=run)
    if resolved.values["production.allow_external_calls"] != run.allow_external_calls:
        raise ValidationError("运行配置解析结果与运行字段不一致")
    # This existing Run field retains its original configured snapshot limit.
    # New RunPlan/receipt fields have their separate 64 KiB schema ceiling;
    # applying that new-field ceiling here would truncate/refuse the existing
    # complete ConfigRevision resolution (including its per-key provenance).
    assign_json_contract(run, "config_snapshot", resolved.snapshot(),
        max_bytes=boundary_int("security.snapshot.stage_max_bytes"))
    if run.config_revision_id is None and resolved.revision_ids:
        run.config_revision = ConfigRevision.objects.get(pk=resolved.revision_ids[-1])
    run.save(update_fields=("config_revision", "config_snapshot", "config_snapshot_schema_version",
        "config_snapshot_size_bytes", "config_snapshot_sha256", "updated_at"))
    return run
