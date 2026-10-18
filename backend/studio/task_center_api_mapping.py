"""Bounded REST field destinations from verified original task envelopes."""
from studio.services.async_media_schema import STANDARD_MEDIA_FIELDS
from studio.services.async_submit_mapping import async_submit_body
from studio.services.execution_parameter_mappings import ApiMappingUnavailable, read_async_field_map


def api_mapping(definition, transport):
    """Caller has authorized the original task and verified both JSON envelopes.

    Return only identifiers/states, never a reconstructed wire body or headers.
    At most the registered standard fields are returned (no user-added row keys).
    """
    if transport.value["driver_type"] != "async_rest":
        return None
    target = definition.value["target"]
    result = {"state": "unavailable", "code": "api_mapping_identity_invalid", "rows": [],
        "deployment_id": target["deployment_id"], "configuration_sha256": target["configuration_sha256"],
        "message": "冻结请求与执行定义身份不一致，无法核对 API 映射。"}
    if (transport.value["definition_sha256"] != definition.sha256 or target["driver_type"] != "async_rest"
            or transport.value["operation"] != definition.value["configuration"]["operation"]):
        return result
    try:
        mapping = read_async_field_map(target)
    except ApiMappingUnavailable as exc:
        return {**result, "code": exc.code, "message": exc.messages[0]}
    payload = transport.value["payload"]
    if set(payload) - STANDARD_MEDIA_FIELDS - {"schema_version", "idempotency_key"}:
        return {**result, "code": "api_mapping_payload_invalid", "message": "原标准体含未登记字段，不能解释为有效映射。"}
    body = async_submit_body(payload, mapping)
    rows = [{"key": key, "field": mapping.get(key),
        "state": "unmapped" if key not in mapping else "included" if mapping[key] in body else "omitted"}
        for key in sorted(set(mapping) | (set(payload) & STANDARD_MEDIA_FIELDS))]
    return {**result, "state": "verified", "code": "api_mapping_verified", "rows": rows,
        "message": "部署配置摘要与原合同一致；按适配器同一规则核对标准体到供应商 JSON 顶层字段。缺失／null 不进入正文，0／false／空字符串保留（仍须通过请求校验）。这不是已发送正文或送达回执；请求头不在此展示，生产调用的幂等标识由原任务派发时确定。映射在模型部署传输配置维护，覆盖服务连接配置；修改不回写历史。"}
