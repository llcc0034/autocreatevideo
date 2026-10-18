"""Per-preparation compare-and-adopt facts for generated project scripts."""
from hashlib import sha256

from django.core.exceptions import ValidationError

from studio.models import ProductionRun
from studio.services.execution_preparations import read_execution_preparation_detail


def capture_script_adoption_baseline(*, run_id, project_id, owner):
    row = ProductionRun.objects.filter(pk=run_id, project_id=project_id, owner=owner,
        project__owner=owner).values("script_revision_id", "project__master_script").first()
    if row is None:
        raise ValidationError("剧本准备缺少本轮所属项目，不能推断采用基线")
    return {"schema_version": 2, "project_id": str(project_id),
        "script_revision_id": str(row["script_revision_id"]) if row["script_revision_id"] else None,
        "fields": {key: sha256(row[f"project__{key}"].encode("utf-8")).hexdigest()
            for key in ("master_script",)}}


def read_script_adoption_baseline(preparation):
    detail = read_execution_preparation_detail(preparation)
    value = detail.resolved_inputs.get("script_adoption_baseline")
    if (preparation.purpose != "production" or not preparation.stage_run_id or not isinstance(value, dict)
            or set(value) != {"schema_version", "project_id", "script_revision_id", "fields"}
            or type(value["schema_version"]) is not int or value["schema_version"] != 2
            or value["project_id"] != str(preparation.stage_run.production_run.project_id)
            or not isinstance(value["fields"], dict) or set(value["fields"]) != {"master_script"}
            or any(not isinstance(digest, str) or len(digest) != 64 or any(char not in "0123456789abcdef" for char in digest)
                for digest in value["fields"].values())):
        raise ValidationError("本次准备缺少完整剧本采用基线，不能从当前项目或旧阶段补齐")
    if value["script_revision_id"] is not None:
        from uuid import UUID
        try:
            if str(UUID(value["script_revision_id"])) != value["script_revision_id"]:
                raise ValueError
        except (ValueError, TypeError, AttributeError) as exc:
            raise ValidationError("本次准备的原剧本修订标识不完整") from exc
    return value
