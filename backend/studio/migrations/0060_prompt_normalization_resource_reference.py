import hashlib
import json

from django.db import migrations


KEY = "production.prompt_normalization_deployment_id"
OLD_SCHEMA = {"maxLength": 36, "pattern": "^$|^[0-9a-fA-F-]{36}$"}
NEW_SCHEMA = {
    "maxLength": 36,
    "pattern": "^$|^[0-9a-fA-F-]{36}$",
    "resource_ref": {
        "resource": "model_deployment",
        "capability": "prompt.normalize",
        # Existing installations may carry the pre-registry empty sentinel.
        # It remains an explicit unconfigured state and production readiness
        # stays blocked; every non-empty reference is now fully revalidated.
        "allow_empty": True,
        "require_ready": True,
    },
}


def _contract(value):
    payload = json.dumps(
        value,
        ensure_ascii=False,
        sort_keys=True,
        separators=(",", ":"),
        allow_nan=False,
    ).encode("utf-8")
    return len(payload), hashlib.sha256(payload).hexdigest()


def _set_schema(apps, schema):
    ConfigDefinition = apps.get_model("studio", "ConfigDefinition")
    size, sha256 = _contract(schema)
    ConfigDefinition.objects.filter(key=KEY).update(
        description=(
            "提示词翻译/标准化的默认 ModelDeployment；"
            "必须从已发布、启用且具有新鲜 prompt.normalize 烟测的部署中选择；"
            "空值表示尚未配置并阻断依赖该能力的生产"
        ),
        value_schema=schema,
        value_schema_version=1,
        value_schema_size_bytes=size,
        value_schema_sha256=sha256,
    )


def forwards(apps, schema_editor):
    _set_schema(apps, NEW_SCHEMA)


def backwards(apps, schema_editor):
    ConfigDefinition = apps.get_model("studio", "ConfigDefinition")
    size, sha256 = _contract(OLD_SCHEMA)
    ConfigDefinition.objects.filter(key=KEY).update(
        description="提示词翻译/标准化的默认 ModelDeployment UUID；空值表示必须人工选择",
        value_schema=OLD_SCHEMA,
        value_schema_version=1,
        value_schema_size_bytes=size,
        value_schema_sha256=sha256,
    )


class Migration(migrations.Migration):
    dependencies = [("studio", "0059_seed_script_structure_prompt_profiles")]

    operations = [migrations.RunPython(forwards, backwards)]
