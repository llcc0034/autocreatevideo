"""One Chat JSON assembler for preparation and provider invocation.

The returned layout records assembly, not serialization, sending or receipt.
It contains field names only; credentials and endpoints never enter it.
"""
from copy import deepcopy

from django.core.exceptions import ValidationError

from studio.services.model_profile_contracts import validate_chat_request_options


CHAT_DRIVERS = frozenset({"deepseek_api", "openai_compatible", "local_qwen"})


def assemble_chat_body(model_identifier, payload):
    if not isinstance(payload, dict) or not isinstance(payload.get("messages"), list) or not payload["messages"]:
        raise ValidationError("messages 必须是非空数组")
    retired = sorted(set(payload) & {"temperature", "response_format", "chat_completion_options"})
    if retired:
        raise ValidationError("Chat 请求包含已退役的顶层字段；请使用 chat_request_options: " + ", ".join(retired))
    options = validate_chat_request_options(payload.get("chat_request_options"), resolved=True)
    body = {"model": model_identifier, "messages": deepcopy(payload["messages"]), **options}
    fields = [{"key": "model", "source": "interface.model_identifier"},
              {"key": "messages", "source": "payload.messages"}]
    fields.extend({"key": key, "source": "payload.chat_request_options"} for key in options)
    for key in ("max_tokens", "seed"):
        if key in payload:
            body[key] = deepcopy(payload[key])
            fields.append({"key": key, "source": "payload." + key})
    layout = {"schema_version": 1, "adapter": "chat.completions/v1", "fields": fields,
              "transport_only": ["timeout_seconds"] if "timeout_seconds" in payload else []}
    return body, layout


def validate_chat_body_layout(layout, payload):
    """Check stored field locations, not today's body builder or policy limits."""
    if (not isinstance(layout, dict) or set(layout) != {"schema_version", "adapter", "fields", "transport_only"}
            or type(layout["schema_version"]) is not int or layout["schema_version"] != 1
            or layout["adapter"] != "chat.completions/v1" or not isinstance(payload, dict)
            or not isinstance(payload.get("chat_request_options"), dict)
            or not isinstance(layout["fields"], list)):
        raise ValidationError("原Chat组包位置记录无法核验")
    expected = {"model": "interface.model_identifier", "messages": "payload.messages"}
    for key in payload["chat_request_options"]:
        if key in expected or key in {"max_tokens", "seed"}:
            raise ValidationError("原Chat组包位置存在争写")
        expected[key] = "payload.chat_request_options"
    expected.update({key: "payload." + key for key in ("max_tokens", "seed") if key in payload})
    actual = {}
    for row in layout["fields"]:
        if (not isinstance(row, dict) or set(row) != {"key", "source"}
                or not isinstance(row["key"], str) or row["key"] in actual):
            raise ValidationError("原Chat组包位置重复或结构异常")
        actual[row["key"]] = row["source"]
    if (actual != expected or layout["transport_only"] != (["timeout_seconds"] if "timeout_seconds" in payload else [])):
        raise ValidationError("原Chat组包位置与冻结输入不一致")
    return layout
