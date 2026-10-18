"""Model transport settings; script fields belong to the selected output."""
from typing import Any

from django.core.exceptions import ValidationError

from studio.services.model_profile_contracts import CHAT_REQUEST_OPTIONS_KEY, chat_request_options_from_policy
from studio.services.security_boundaries import boundary_number

SCRIPT_POLICIES = (
    ("series_concept", "script.generate", "剧本生成"),
    ("script_structure", "script.structure", "剧本结构化"),
)


def script_policy_descriptor(key: str) -> dict[str, Any]:
    key, operation, label = next(row for row in SCRIPT_POLICIES if row[0] == key)
    return {"key": key, "operation": operation, "label": label, "number_fields": [
        {"key": "timeout_seconds", "label": "模型请求等待时限", "unit": "秒", "integer": False, "minimum": 0,
         "maximum": boundary_number("security.provider.async_rest_timeout_max_seconds", minimum=0.001),
         "exclusive_minimum": True},
    ]}


def script_contract_editor_options() -> dict[str, Any]:
    return {"schema_version": 3, "policies": [script_policy_descriptor(row[0]) for row in SCRIPT_POLICIES],
            "description": "这里只维护模型传输设置。剧本字段、嵌套、含义和校验在字段维护的结构及业务输出版本中配置，"
                           "由执行实例明确选择；创作要求在提示词策略中配置。发布冻结当前设置，不触发生成。"}


def validate_script_model_policy(value: object, *, key: str) -> dict[str, Any]:
    descriptor = script_policy_descriptor(key)
    numbers = descriptor["number_fields"]
    if not isinstance(value, dict) or set(value) != {row["key"] for row in numbers} | {CHAT_REQUEST_OPTIONS_KEY}:
        raise ValidationError({"specification": f"{descriptor['operation']} 的 {key} 只接受模型传输设置；"
                               "字段与业务规则请配置在确切输出版本，不接受旧字段合同或未注册字段"})
    normalized = {}
    for row in numbers:
        name, number = row["key"], value.get(row["key"])
        if (type(number) not in (int, float) or not number > row["minimum"]
                or not number <= row["maximum"]):
            raise ValidationError({"specification": f"{key}.{name} 无效，须满足目录范围（{row['unit']}）"})
        normalized[name] = float(number)
    normalized[CHAT_REQUEST_OPTIONS_KEY] = chat_request_options_from_policy(value, label=key)
    return normalized


def validate_script_model_policies(specification: dict[str, Any]) -> None:
    for key, operation, _label in SCRIPT_POLICIES:
        if key in specification or operation in specification.get("capabilities", []):
            validate_script_model_policy(specification.get(key), key=key)
