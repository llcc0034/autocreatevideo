"""One pure route envelope for draft authors and frozen execution readers.

Current resource availability and configurable limits belong to authoring and
admission. Reading this shape never queries them or rewrites a frozen identity.
"""
from uuid import UUID

from django.core.exceptions import ValidationError


def _error(path, message):
    raise ValidationError({path: message})


def recipe_route_reference(route, *, path="route"):
    if not isinstance(route, dict) or set(route) != {
            "execution_instance_version_id", "expected_definition_sha256", "input_sources", "parameter_policies"}:
        _error(path, "必须明确提供全部合同字段，不接受旧资源组合或额外字段")
    identifier = route["execution_instance_version_id"]
    try:
        if not isinstance(identifier, str) or str(UUID(identifier)) != identifier:
            raise ValueError
    except (ValueError, TypeError, AttributeError) as exc:
        raise ValidationError({path + ".execution_instance_version_id":
            "请选择精确执行实例版本，使用目录返回的小写连字符UUID，不转换别名"}) from exc
    digest = route["expected_definition_sha256"]
    if not isinstance(digest, str) or len(digest) != 64 or any(c not in "0123456789abcdef" for c in digest):
        _error(path + ".expected_definition_sha256", "需要精确执行版本的 SHA-256")
    if not isinstance(route["input_sources"], dict):
        _error(path + ".input_sources", "输入来源必须为按实例输入key组织的对象")
    if not isinstance(route["parameter_policies"], list):
        _error(path + ".parameter_policies", "参数策略必须为列表")
    return identifier, digest


def validate_recipe_routing_shape(routing, *, require_complete=True, path="routing"):
    """Validate structure once; only an explicitly incomplete draft may be empty."""
    if not isinstance(routing, dict) or set(routing) != {"mode", "variant_count", "evaluator", "routes"}:
        _error(path, "必须明确提供全部合同字段，不接受旧资源组合或额外字段")
    if routing["mode"] not in ("fixed", "ordered_fallback", "manual_choice"):
        _error(path + ".mode", "选路方式未注册")
    if type(routing["variant_count"]) is not int or routing["variant_count"] < 1:
        _error(path + ".variant_count", "候选数量必须为正整数")
    if routing["evaluator"] not in (None, "first_succeeded"):
        _error(path + ".evaluator", "候选评估器未注册")
    routes = routing["routes"]
    if not isinstance(routes, list) or (require_complete and not routes):
        _error(path + ".routes", "完整选路合同需要非空路线列表")
    if routing["mode"] == "fixed" and len(routes) > 1:
        _error(path + ".routes", "固定路线必须且只能选择一个执行实例版本")
    for index, route in enumerate(routes):
        recipe_route_reference(route, path=f"{path}.routes[{index}]")
    return routing
