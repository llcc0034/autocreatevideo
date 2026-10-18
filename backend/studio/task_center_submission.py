"""Local submission fields, never a reconstructed wire request or delivery claim."""
from datetime import datetime
from hashlib import sha256
import re


SUPPORTED_KINDS = {"generation_attempt", "asset_job", "deployment_smoke", "workflow_smoke"}


def submission_evidence(source, kind):
    """The authorized detail reader supplies its exact selected call; no queries."""
    result = {"state": "unavailable", "started_at": None, "registered_at": None,
        "identifier_state": "unavailable", "message": "当前记录没有可读取的原调用提交字段；不推断未发送。",
        "notes": ["这里只读本地提交登记，不含逐字节HTTP正文或供应商接收证明。冻结请求摘要不是发送正文摘要。",
                  "请求标识可能在发送前分配；提交时间也可能由恢复或人工核对登记。不能据此判断Smoke通过、费用已结算或结果已采用。"]}
    if source is None or kind not in SUPPORTED_KINDS:
        return result
    started, registered = source.submission_started_at, source.submitted_at
    identifier = source.provider_request_id
    digest = getattr(source, "provider_request_id_sha256", "")
    invalid = (any(value is not None and not isinstance(value, datetime) for value in (started, registered))
        or not isinstance(identifier, str) or not isinstance(digest, str))
    identifier_state = "not_recorded"
    if not isinstance(identifier, str) or not isinstance(digest, str):
        identifier_state = "inconsistent"
    elif digest and (re.fullmatch(r"[a-f0-9]{64}", digest) is None
                     or (identifier and sha256(identifier.encode("utf-8")).hexdigest() != digest)):
        identifier_state = "inconsistent"
        invalid = True
    elif identifier:
        identifier_state = "recorded"
    elif digest:
        identifier_state = "digest_only"
    if not invalid and started is not None and registered is not None:
        try:
            invalid = registered < started
        except TypeError:
            invalid = True
    result.update(state="inconsistent" if invalid else "recorded",
        started_at=started if isinstance(started, datetime) else None,
        registered_at=registered if isinstance(registered, datetime) else None,
        identifier_state=identifier_state,
        message=("原提交字段存在时间顺序或标识摘要异常；保留可读字段，不补值、不自动重试。" if invalid else
                 "来自当前选中调用的原任务字段。空时间只表示尚无此字段记录，不证明从未外呼；有登记也不代表业务成功。"))
    return result
