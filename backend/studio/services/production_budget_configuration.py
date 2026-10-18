"""System resource policy presentation; never grant external-call consent."""
import re
from decimal import Decimal, InvalidOperation

from django.core.exceptions import ValidationError



def budget_currency_options():
    """Configured billing units, not a pricing gate or exchange-rate service."""
    from django.db.models import Case, F, JSONField, When
    from studio.models import ModelDeployment
    from studio.services.execution_resource_policy import read_resource_policy
    policy = read_resource_policy()
    labels = {"USD": "美元", "CNY": "人民币", "EUR": "欧元", "HKD": "港币", "JPY": "日元", "GBP": "英镑"}
    codes = set(labels) | set(policy.value["values"]["budget_by_currency"])
    # Whole-object override matches the driver, including explicit null/{}.
    # Project just pricing, never credential or full runtime configuration.
    pricing_rows = ModelDeployment.objects.filter(status="active").order_by().annotate(
        budget_pricing=Case(When(runtime_config__has_key="pricing", then=F("runtime_config__pricing")),
            default=F("profile_version__provider__driver_config__pricing"), output_field=JSONField())
    ).values_list("budget_pricing", flat=True)
    for pricing in pricing_rows:
        if isinstance(pricing, dict) and pricing.get("currency"):
            codes.add(str(pricing["currency"]).strip().upper())
    if any(not re.fullmatch(r"[A-Z][A-Z0-9]{2,11}", code) for code in codes):
        raise ValidationError("计价配置中的币种代码无效，请先修正模型计价或系统预算配置")
    return {"currencies": [{"value": code, "label": f"{labels[code]}（{code}）" if code in labels else code}
        for code in sorted(codes)], "resource_policy": policy.value}


def validate_budget_currency(amount, currency):
    if not isinstance(currency, str) or (currency and not re.fullmatch(r"[A-Z][A-Z0-9]{2,11}", currency)):
        raise ValidationError({"budget_currency": "请输入 3–12 位大写币种代码"})
    if amount is None:
        if currency:
            raise ValidationError({"budget_currency": "未设置预算时不单独设置预算币种"})
        return
    try:
        value = Decimal(str(amount))
    except (InvalidOperation, ValueError):
        raise ValidationError({"budget_limit": "预算必须是有限非负金额"})
    if not value.is_finite() or value < 0 or value >= Decimal("100000000") or value != value.quantize(Decimal("0.0001")):
        raise ValidationError({"budget_limit": "预算必须是非负金额，最多 8 位整数和 4 位小数"})
    if not currency:
        raise ValidationError({"budget_currency": "设置预算时必须同时声明币种，不会默认使用美元"})


def generation_call_limit():
    from studio.services.execution_resource_policy import read_resource_policy
    return read_resource_policy().value["values"]["call_count"]

