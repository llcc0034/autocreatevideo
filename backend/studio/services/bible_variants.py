"""The single writer/reader of reviewed appearance intents; no dispatch side effects."""
import hashlib
from pathlib import Path

from django.db import transaction
from rest_framework.exceptions import APIException, NotFound, ValidationError

from studio.models import Asset, BibleVariant, BibleVariantRevision, BibleVariantReview, SeriesProject
from studio.services.json_contracts import canonical_json_bytes
from studio.services.bible_reference_contract import bible_reference_name_key
from studio.services.production_execution_units import ENTITY_SCOPE_MODELS
from studio.services.asset_visibility import is_workflow_smoke_fixture_asset
from studio.services.provenance import ProvenanceError
from studio.services.generated_artifacts import verified_local_media_facts


class BibleVariantConflict(APIException):
    status_code = 409
    default_code = "bible_variant_conflict"
    default_detail = "变体设定或所选来源已变化，请重新读取后明确确认。"


def project_for_actor(project_id, actor, *, lock=False):
    rows = SeriesProject.objects.all()
    if lock:
        rows = rows.select_for_update()
    if not actor.is_staff:
        rows = rows.filter(owner=actor)
    project = rows.filter(pk=project_id).first()
    if project is None:
        raise NotFound("项目不存在或无权访问")
    return project


def variant_rows(actor):
    rows = BibleVariant.objects.all()
    return rows if actor.is_staff else rows.filter(project__owner=actor)


def _basis(project, value):
    identifier, digest = value["basis_asset_id"], value["basis_asset_sha256"]
    if identifier is None:
        if digest:
            raise ValidationError({"basis_asset_sha256": "未选择身份参考图时摘要必须为空"})
        return
    asset = Asset.objects.select_for_update().filter(pk=identifier).first()
    if (asset is None or asset.owner_id not in (None, project.owner_id) or not asset.is_active
            or is_workflow_smoke_fixture_asset(asset) or not asset.mime_type.startswith("image/")
            or not (asset.file or asset.local_path)):
        raise ValidationError({"basis_asset_id": "请选择有权使用且有正式文件的图片"})
    if asset.checksum != digest:
        raise BibleVariantConflict("身份参考图摘要已变化；未保存或审核")
    try:
        path = Path(asset.file.path if asset.file else asset.local_path)
        facts = verified_local_media_facts(path, expected_kind="image", max_bytes=path.stat().st_size)
    except (OSError, ValueError, ProvenanceError) as exc:
        raise ValidationError({"basis_asset_id": "身份参考图实际文件不可验证"}) from exc
    if facts["sha256"] != digest or facts["mime_type"] != asset.mime_type:
        raise BibleVariantConflict("身份参考图实际文件与登记摘要或 MIME 不符")


def _payload(value):
    return {key: (str(value[key]) if key == "basis_asset_id" and value[key] is not None else value[key])
        for key in ("name", "reference_name", "identity_anchor", "appearance_anchor", "basis_asset_id", "basis_asset_sha256")}


def setting_payload(setting):
    return {key: (str(getattr(setting, key)) if key == "basis_asset_id" and getattr(setting, key) is not None
        else getattr(setting, key)) for key in ("name", "reference_name", "identity_anchor", "appearance_anchor",
            "basis_asset_id", "basis_asset_sha256")}


def verify_setting(setting):
    if hashlib.sha256(canonical_json_bytes(_setting_contract(setting))).hexdigest() != setting.content_sha256:
        raise BibleVariantConflict("变体设定正文与摘要不一致；不会从当前 Bible 重建")
    return setting


def _entity_identity(variant):
    return tuple(getattr(variant, f"{kind}_id") for kind in ("character", "scene", "prop"))


def _setting_contract(setting):
    fields = setting_payload(setting)
    if setting.payload_schema_version != 2:
        raise BibleVariantConflict("设定摘要版本已停用或不支持；不会转换旧记录或补造归属与主图依赖")
    variant = setting.variant
    identity = _entity_identity(variant)
    if sum(identifier is not None for identifier in identity) != 1:
        raise BibleVariantConflict("设定缺少唯一实体归属")
    entity = next(getattr(variant, kind) for kind in ("character", "scene", "prop") if getattr(variant, f"{kind}_id"))
    if entity.project_id != variant.project_id:
        raise BibleVariantConflict("设定实体不属于本项目")
    binding = variant.source_binding
    if binding and (_entity_identity(binding) != identity or binding.script_revision.source.project_id != variant.project_id):
        raise BibleVariantConflict("设定的声明来源与实体归属不符")
    primary = setting.primary_setting_revision
    if primary:
        if (variant.appearance_role != "variant" or primary.variant.appearance_role != "primary"
                or primary.variant.project_id != variant.project_id or _entity_identity(primary.variant) != identity
                or primary.primary_setting_revision_id is not None):
            raise BibleVariantConflict("变体必须引用同实体的确切主图设定修订，不能依赖兄弟变体")
        verify_setting(primary)
    elif binding and variant.appearance_role == "variant":
        raise BibleVariantConflict("声明变体缺少主图设定依赖")
    return {"schema_version": 2, **fields, "revision": setting.revision,
        "variant_id": str(variant.pk), "project_id": str(variant.project_id),
        "entity_ids": [str(item) if item else None for item in identity], "appearance_role": variant.appearance_role,
        "source_binding_id": str(binding.pk) if binding else None,
        "source_binding_sha256": binding.declaration_sha256 if binding else "",
        "source_script_revision_id": str(binding.script_revision_id) if binding else None,
        "source_variant_key": variant.source_variant_key,
        "source_declaration_sha256": setting.source_declaration_sha256,
        "primary_setting_revision_id": str(primary.pk) if primary else None,
        "primary_setting_sha256": primary.content_sha256 if primary else ""}


def _save_revision(variant, value, actor, *, source_declaration_sha256="", primary_setting_revision=None):
    if primary_setting_revision is not None and value["identity_anchor"]:
        raise ValidationError({"identity_anchor": "共同身份由确切主图设定提供；请在那里修改，变体只保存差异"})
    fields = _payload(value)
    setting = BibleVariantRevision(variant=variant, revision=variant.revision,
        **fields, payload_schema_version=2, source_declaration_sha256=source_declaration_sha256,
        primary_setting_revision=primary_setting_revision, created_by=actor)
    setting.content_sha256 = hashlib.sha256(canonical_json_bytes(_setting_contract(setting))).hexdigest()
    setting.save()
    return setting


def create_declared_appearance(*, actor, project, binding, entity, declaration, primary_setting_revision=None):
    """Internal writer for an already validated frozen declaration, within its transaction.

    Empty authored anchors remain empty per that declaration's policy. This does
    not approve a setting, adopt an image, or authorize a model invocation.
    """
    from studio.bible_variant_serializers import VariantAnchorField, VariantNameField
    role = "variant" if primary_setting_revision is not None else "primary"
    entity_kind = next(kind for kind in ("character", "scene", "prop") if getattr(binding, f"{kind}_id"))
    value = {"name": declaration["name"] if role == "variant" else entity["name"],
        "reference_name": declaration["reference_name"],
        "identity_anchor": entity.get("identity_anchor", "") if role == "primary" else "",
        "appearance_anchor": declaration.get("anchor", ""), "basis_asset_id": None, "basis_asset_sha256": ""}
    for key in ("name", "reference_name"):
        if VariantNameField().run_validation(value[key]) != value[key]:
            raise ValidationError({key: "声明名称不能被设定写入器静默改写"})
    for key in ("identity_anchor", "appearance_anchor"):
        VariantAnchorField(allow_blank=True, trim_whitespace=False).run_validation(value[key])
    variant = BibleVariant.objects.create(project=project, **{f"{entity_kind}_id": getattr(binding, f"{entity_kind}_id")},
        appearance_role=role, source_binding=binding, source_variant_key=declaration["key"] if role == "variant" else "",
        name=value["name"], reference_name=value["reference_name"],
        reference_key=_unique_reference(project, value["reference_name"]), created_by=actor)
    return _save_revision(variant, value, actor,
        source_declaration_sha256=hashlib.sha256(canonical_json_bytes(declaration)).hexdigest(),
        primary_setting_revision=primary_setting_revision)


def _validated_setting(value):
    from studio.bible_variant_serializers import BibleVariantSettingInput
    serializer = BibleVariantSettingInput(data=value)
    serializer.is_valid(raise_exception=True)
    return serializer.validated_data


def _unique_reference(project, value, *, exclude=None):
    key = bible_reference_name_key(value)
    if len(key) > 191:
        raise ValidationError({"reference_name": "规范化后的引用名过长"})
    rows = BibleVariant.objects.filter(project=project, reference_key=key)
    if exclude:
        rows = rows.exclude(pk=exclude)
    # The project lock serializes callers; inspect only the conflicting object's
    # identity/display fields, never setting bodies or another project's names.
    conflict = rows.values("id", "name", "reference_name", "appearance_role", "character_id", "scene_id", "prop_id",
        "character__name", "scene__name", "prop__name").first()
    if conflict:
        kind = next(kind for kind in ENTITY_SCOPE_MODELS if conflict[f"{kind}_id"])
        label = {"character": "人物", "scene": "场景", "prop": "道具"}[kind]
        role = "主图" if conflict["appearance_role"] == "primary" else "变体"
        raise BibleVariantConflict(
            f"引用名称与本项目{label}「{conflict[f'{kind}__name']}」（ID：{conflict[f'{kind}_id']}）的"
            f"{role}「{conflict['name']}」（ID：{conflict['id']}，引用名：{conflict['reference_name']}）冲突；"
            "请修改本次引用名称，不会自动合并或改名其他对象。")
    return key


@transaction.atomic
def create_variant(*, actor, project_id, entity_kind, entity_id, appearance_role,
        primary_setting_revision_id, primary_setting_sha256, value):
    from studio.bible_variant_serializers import BibleVariantCreateInput
    request = BibleVariantCreateInput(data={**value, "project_id": project_id, "entity_kind": entity_kind,
        "entity_id": entity_id, "appearance_role": appearance_role,
        "primary_setting_revision_id": primary_setting_revision_id, "primary_setting_sha256": primary_setting_sha256})
    request.is_valid(raise_exception=True)
    value = _validated_setting(value)
    project = project_for_actor(project_id, actor, lock=True)
    model = ENTITY_SCOPE_MODELS.get(entity_kind)
    if model is None:
        raise ValidationError({"entity_kind": "请选择已登记的 Bible 对象类型"})
    entity = model.objects.select_for_update().filter(project=project, pk=entity_id).first()
    if entity is None:
        raise NotFound("Bible 对象不存在或不属于本项目")
    primary = None
    if appearance_role == "primary":
        if BibleVariant.objects.filter(project=project, **{entity_kind: entity}, appearance_role="primary").exists():
            raise BibleVariantConflict("该实体已有主图设定，请打开后另存修订，不重复创建")
    else:
        primary = BibleVariantRevision.objects.select_related("variant").filter(
            pk=primary_setting_revision_id, variant__project=project,
            variant__appearance_role="primary", **{f"variant__{entity_kind}": entity}).first()
        if primary is None:
            raise ValidationError({"primary_setting_revision_id": "主图设定必须属于同一实体；图片或兄弟变体不能替代"})
        verify_setting(primary)
        if primary.content_sha256 != primary_setting_sha256:
            raise BibleVariantConflict("主图设定摘要与所见修订不符，请重新读取")
    _basis(project, value)
    key = _unique_reference(project, value["reference_name"])
    variant = BibleVariant.objects.create(project=project, **{entity_kind: entity}, name=value["name"],
        reference_name=value["reference_name"], reference_key=key, created_by=actor, appearance_role=appearance_role)
    _save_revision(variant, value, actor, primary_setting_revision=primary)
    return variant


@transaction.atomic
def revise_variant(*, actor, variant, expected_revision, value):
    project = project_for_actor(variant.project_id, actor, lock=True)
    variant = variant_rows(actor).select_for_update().get(pk=variant.pk)
    if variant.revision != expected_revision:
        raise BibleVariantConflict()
    current = verify_setting(variant.revisions.get(revision=variant.revision))
    value = _validated_setting(value)
    _basis(project, value)
    key = _unique_reference(project, value["reference_name"], exclude=variant.pk)
    if setting_payload(current) == _payload(value):
        raise ValidationError("设定没有变化，不创建重复修订")
    variant.revision += 1
    variant.name, variant.reference_name, variant.reference_key = value["name"], value["reference_name"], key
    variant.save(update_fields=("revision", "name", "reference_name", "reference_key", "updated_at"))
    _save_revision(variant, value, actor, primary_setting_revision=current.primary_setting_revision)
    return variant


@transaction.atomic
def review_variant(*, actor, variant, expected_revision, expected_sha256, decision, comment):
    from studio.bible_variant_serializers import BibleVariantReviewInput
    serializer = BibleVariantReviewInput(data=dict(expected_revision=expected_revision,
        expected_sha256=expected_sha256, decision=decision, comment=comment))
    serializer.is_valid(raise_exception=True)
    expected_revision, expected_sha256, decision, comment = (serializer.validated_data[key]
        for key in ("expected_revision", "expected_sha256", "decision", "comment"))
    project = project_for_actor(variant.project_id, actor, lock=True)
    variant = variant_rows(actor).select_for_update().get(pk=variant.pk)
    if variant.revision != expected_revision:
        raise BibleVariantConflict()
    setting = verify_setting(variant.revisions.get(revision=expected_revision))
    if setting.content_sha256 != expected_sha256:
        raise BibleVariantConflict()
    existing = BibleVariantReview.objects.filter(setting=setting).first()
    if existing:
        if existing.decision == decision and existing.comment == comment and existing.decided_by_id == actor.pk:
            return variant
        raise BibleVariantConflict("该修订已作出审核决定；修改设定请另存修订，不覆盖审核")
    if decision == "approve":
        _basis(project, setting_payload(setting))
    BibleVariantReview.objects.create(setting=setting, decision=decision, comment=comment, decided_by=actor)
    return variant
