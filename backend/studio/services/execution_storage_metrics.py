"""On-demand recorded byte facts, not a body read or physical DB size estimate."""
import re
from types import SimpleNamespace

from django.core.exceptions import ValidationError
from django.utils import timezone

from studio.services.execution_instances import execution_configuration_max_bytes, execution_resolved_inputs_max_bytes
from studio.services.execution_preparation_owners import visible_execution_preparations
from studio.services.execution_request_storage import execution_request_max_bytes, read_request_manifest
from studio.services.security_boundaries import security_boundary_resolution_cache


PARTS = (("intent", "原始输入意图"), ("resolved_inputs", "已解析输入与来源"),
    ("review_contract", "字段审核合同"), ("evaluation", "准备评估"), ("prepared_request", "最终冻结请求文件"))
SUFFIXES = ("schema_version", "size_bytes", "sha256")


def storage_metrics_queryset(actor, preparation_id):
    # A single statement observes preparation revision and all metadata together.
    # Do not use detail.clean(), a body property, file IO, or a secondary snapshot.
    return visible_execution_preparations(actor).filter(pk=preparation_id).values(
        "id", "revision", "intent_sha256", "request_sha256", "evaluation_revision", "request_frozen_at", "contract__id",
        "contract__prepared_request_key", *[f"contract__{key}_{suffix}" for key, _ in PARTS for suffix in SUFFIXES])


@security_boundary_resolution_cache()
def read_preparation_storage_metrics(*, preparation_id, actor, expected_preparation_revision):
    row = storage_metrics_queryset(actor, preparation_id).get()
    if row["revision"] != expected_preparation_revision:
        raise ValidationError("准备修订已变化，请先刷新准备状态", code="execution_preparation_conflict")
    if row["contract__id"] is None:
        raise ValidationError("准备缺少原存储合同，不能推算字节数", code="execution_storage_metadata_invalid")
    limits = {"resolved_inputs": execution_resolved_inputs_max_bytes(), "prepared_request": execution_request_max_bytes()}
    json_limit = execution_configuration_max_bytes()
    items = []
    for key, label in PARTS:
        values = {suffix: row[f"contract__{key}_{suffix}"] for suffix in SUFFIXES}
        if key == "prepared_request":
            manifest = read_request_manifest(SimpleNamespace(preparation_id=row["id"],
                prepared_request_key=row["contract__prepared_request_key"],
                **{f"prepared_request_{suffix}": value for suffix, value in values.items()}), "prepared_request")
            if (manifest is None) != (row["request_frozen_at"] is None):
                raise ValidationError("冻结状态与原请求文件元数据不一致", code="execution_storage_metadata_invalid")
            if values["sha256"] != row["request_sha256"]:
                raise ValidationError("冻结请求与原索引摘要不一致", code="execution_storage_metadata_invalid")
        absent = all(value is None for value in values.values())
        if key == "evaluation" and absent != (row["evaluation_revision"] is None):
            raise ValidationError("评估状态与原字节元数据不一致", code="execution_storage_metadata_invalid")
        if absent and key not in {"evaluation", "prepared_request"}:
            raise ValidationError("必要准备合同的字节元数据缺失", code="execution_storage_metadata_invalid")
        if not absent and (type(values["schema_version"]) is not int or values["schema_version"] < 1
                or type(values["size_bytes"]) is not int or values["size_bytes"] < 2
                or not isinstance(values["sha256"], str) or not re.fullmatch(r"[a-f0-9]{64}", values["sha256"])):
            raise ValidationError("准备字节元数据不完整，不能用0或重算值替代", code="execution_storage_metadata_invalid")
        if key == "intent" and values["sha256"] != row["intent_sha256"]:
            raise ValidationError("准备意图与原索引摘要不一致", code="execution_storage_metadata_invalid")
        limit = limits.get(key, json_limit)
        items.append({"key": key, "label": label, "state": "not_generated" if absent else "recorded",
            "basis": "canonical_request_file_utf8" if key == "prepared_request" else "canonical_json_utf8",
            **values, "current_limit_bytes": limit,
            "over_current_limit_bytes": None if absent else max(0, values["size_bytes"] - limit)})
    return {"preparation_id": row["id"], "preparation_revision": row["revision"],
        "observed_at": timezone.now(), "items": items}
