"""Explicit version lifecycle for the shared field identity registry."""
from copy import deepcopy
from uuid import UUID

from django.core.exceptions import ValidationError
from django.db import transaction
from django.db.models import Max

from studio.models import FieldDefinition, FieldDefinitionVersion


DEFINITION_CONTENT_FIELDS = (
    "label", "category", "description", "impact", "value_type", "unit", "value_options",
)


def _content(data):
    unknown = set(data) - set(DEFINITION_CONTENT_FIELDS)
    if unknown:
        raise ValidationError({name: "此项不属于字段定义内容" for name in sorted(unknown)})
    return deepcopy(data)


@transaction.atomic
def create_field_definition_version(*, field_definition_id, content, source_version_id=None):
    definition = FieldDefinition.objects.select_for_update().get(pk=field_definition_id)
    values = {}
    if source_version_id is not None:
        source = FieldDefinitionVersion.objects.get(pk=source_version_id)
        if source.field_definition_id != definition.id:
            raise ValidationError({"source_version_id": "来源版本必须属于同一字段身份"})
        values = {name: deepcopy(getattr(source, name)) for name in DEFINITION_CONTENT_FIELDS}
    values.update(_content(content))
    maximum = definition.definition_versions.aggregate(maximum=Max("version"))["maximum"] or 0
    return FieldDefinitionVersion.objects.create(
        field_definition=definition, version=maximum + 1, **values,
    )


@transaction.atomic
def update_field_definition_draft(*, version_id, content):
    version = FieldDefinitionVersion.objects.select_for_update().get(pk=version_id)
    if version.status != FieldDefinitionVersion.Status.DRAFT:
        raise ValidationError("只能修改草稿；修改可用定义请创建新版本")
    for name, value in _content(content).items():
        setattr(version, name, value)
    version.save()
    return version


@transaction.atomic
def publish_field_definition_version(*, version_id):
    version = FieldDefinitionVersion.objects.select_for_update().select_related("field_definition").get(pk=version_id)
    if not version.field_definition.is_active:
        raise ValidationError("已停用字段不能启用新的定义版本")
    if version.status == FieldDefinitionVersion.Status.PUBLISHED:
        return version
    if version.status != FieldDefinitionVersion.Status.DRAFT:
        raise ValidationError("停用版本不能重新启用；请创建新版本")
    version.status = FieldDefinitionVersion.Status.PUBLISHED
    version.save()
    return version


@transaction.atomic
def retire_field_definition_version(*, version_id):
    version = FieldDefinitionVersion.objects.select_for_update().get(pk=version_id)
    if version.status == FieldDefinitionVersion.Status.DRAFT:
        raise ValidationError("草稿尚未启用，无需停用")
    version.status = FieldDefinitionVersion.Status.RETIRED
    version.save()
    return version


def field_definition_snapshot(version):
    """Read the exact version; never substitute the identity's current content."""
    if version.status == FieldDefinitionVersion.Status.DRAFT:
        raise ValidationError("草稿字段版本不能成为执行合同")
    return {
        "schema_version": 1,
        "field_definition_id": str(version.field_definition_id),
        "field_definition_version_id": str(version.id),
        "key": version.field_definition.key,
        "version": version.version,
        **{name: deepcopy(getattr(version, name)) for name in DEFINITION_CONTENT_FIELDS},
    }


def profile_rule_definition(rule, *, require_published=False):
    """Resolve only the selected definition; never infer a version for old rows."""
    version = rule.field_definition_version
    if version is None:
        raise ValidationError({"field_definition_version": "请选择确切字段定义版本"})
    if version.field_definition_id != rule.field_definition_id:
        raise ValidationError({"field_definition_version": "版本不属于所选字段身份"})
    allowed = {FieldDefinitionVersion.Status.PUBLISHED, FieldDefinitionVersion.Status.RETIRED}
    if version.status not in allowed or (require_published and version.status != FieldDefinitionVersion.Status.PUBLISHED):
        raise ValidationError({"field_definition_version": "新规则须引用可用定义版本，不能使用草稿或停用版本"})
    return version


def validate_field_definition_snapshot(value):
    """Validate frozen metadata without querying the current registry."""
    expected = {"schema_version", "field_definition_id", "field_definition_version_id", "key", "version", *DEFINITION_CONTENT_FIELDS}
    if not isinstance(value, dict) or set(value) != expected or type(value.get("schema_version")) is not int or value["schema_version"] != 1:
        raise ValidationError("字段定义快照结构无效")
    try:
        for name in ("field_definition_id", "field_definition_version_id"):
            if not isinstance(value[name], str) or str(UUID(value[name])) != value[name]:
                raise ValueError(name)
    except (ValueError, TypeError, AttributeError) as exc:
        raise ValidationError("字段定义快照必须包含确切身份与版本ID") from exc
    if type(value["version"]) is not int or value["version"] < 1:
        raise ValidationError("字段定义快照版本号无效")
    if not isinstance(value["key"], str):
        raise ValidationError("字段定义快照key无效")
    FieldDefinition._meta.get_field("key").clean(value["key"], None)
    if any(not isinstance(value[name], str) for name in DEFINITION_CONTENT_FIELDS if name != "value_options"):
        raise ValidationError("字段定义快照内容类型无效")
    definition = FieldDefinitionVersion(
        version=value["version"], status=FieldDefinitionVersion.Status.PUBLISHED,
        **{name: deepcopy(value[name]) for name in DEFINITION_CONTENT_FIELDS},
    )
    definition.clean_fields(exclude=("field_definition",))
    definition.clean()
    return deepcopy(value)
