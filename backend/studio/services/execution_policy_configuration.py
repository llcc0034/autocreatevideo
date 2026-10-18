"""Read the single system policy source; never use instance/recipe defaults."""
from dataclasses import dataclass

from django.core.exceptions import ValidationError

from studio.services.configuration import resolve_configuration
from studio.services.execution_instances import execution_configuration_max_bytes
from studio.services.json_contracts import JsonContract, json_contract


SMOKE_POLICY = "execution.admission.smoke_policy"
CAPACITY_POLICY = "execution.admission.capacity_policy"
RECHECK_INTERVAL = "execution.preparation.recheck_interval_seconds"
CLAIM_TIMEOUT = "execution.preparation.claim_timeout_seconds"
EXECUTION_POLICY_KEYS = (SMOKE_POLICY, CAPACITY_POLICY, RECHECK_INTERVAL, CLAIM_TIMEOUT)


@dataclass(frozen=True)
class ExecutionPolicies:
    smoke_policy: str
    capacity_policy: str
    recheck_interval_seconds: int
    claim_timeout_seconds: int
    configuration: JsonContract


def read_execution_policies():
    resolved = resolve_configuration(required_keys=EXECUTION_POLICY_KEYS)
    values = {key: resolved.values[key] for key in EXECUTION_POLICY_KEYS}
    sources = {key: resolved.sources[key] for key in EXECUTION_POLICY_KEYS}
    if any(source.get("scope") != "system" or not source.get("revision_id") for source in sources.values()):
        raise ValidationError("执行策略必须来自明确启用的系统配置修订")
    if (values[SMOKE_POLICY] not in {"require_passed", "require_fresh"}
            or values[CAPACITY_POLICY] not in {"warn", "require_evidence"}
            or any(type(values[key]) is not int or values[key] <= 0 for key in (RECHECK_INTERVAL, CLAIM_TIMEOUT))):
        raise ValidationError("执行准入或恢复配置无效，不能使用隐藏默认值", code="execution_admission_configuration_invalid")
    # Only these non-secret keys enter the decision, never the whole system
    # configuration. Version/SHA references preserve the actual policy source.
    contract = json_contract({"schema_version": 1, "values": values, "sources": sources,
        "revision_ids": list(resolved.revision_ids)}, max_bytes=execution_configuration_max_bytes())
    return ExecutionPolicies(values[SMOKE_POLICY], values[CAPACITY_POLICY], values[RECHECK_INTERVAL], values[CLAIM_TIMEOUT], contract)
