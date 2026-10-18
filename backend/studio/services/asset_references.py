"""Library relationships shared by library/production callers; never copy media or settings."""
from django.contrib.auth import get_user_model
from django.core.exceptions import ValidationError as ModelValidationError
from django.db import transaction
from django.db.models import Count, IntegerField, Max, OuterRef, Subquery, Value
from django.db.models.deletion import ProtectedError
from django.db.models.functions import Coalesce
from rest_framework.exceptions import APIException, NotFound, ValidationError

from studio.models import AssetReferenceSeries, AssetReferenceVersion, BibleVariant, ProductionMaterialFulfillment
from studio.services.media_generation_targets import resolve_media_fulfillment_target


def series_rows(actor):
    rows = AssetReferenceSeries.objects.all()
    if not actor.is_authenticated:
        return rows.none()
    return rows if actor.is_staff else rows.filter(owner=actor)


def series_summaries(actor):
    def count(query):
        return Coalesce(Subquery(query.order_by().values("primary_key").annotate(total=Count("id"))
            .values("total"), output_field=IntegerField()), Value(0))

    return series_rows(actor).annotate(
        version_count=count(AssetReferenceVersion.objects.filter(series_id=OuterRef("pk"))
            .values(primary_key=Value(1))),
        variant_count=count(AssetReferenceSeries.objects.filter(primary_series_id=OuterRef("pk"))
            .values(primary_key=Value(1))),
    )


def version_summaries(actor):
    rows = AssetReferenceVersion.objects.all()
    if not actor.is_authenticated:
        return rows.none()
    if not actor.is_staff:
        rows = rows.filter(series__owner=actor)
    return rows.select_related("asset", "generated_artifact", "generation_job").only(
        "id", "created_at", "updated_at", "series_id", "version", "source", "asset_id", "origin_history_cleared",
        "generated_from_id", "setting_revision_id", "generated_artifact_id", "generation_job_id",
        "asset__id", "asset__name", "asset__kind", "asset__mime_type", "asset__is_active",
        "generated_artifact__id", "generated_artifact__status", "generated_artifact__production_run_id",
        "generated_artifact__generation_attempt_id", "generation_job__id", "generation_job__status",
    )


def _reference(rows, value, field):
    row = rows.filter(pk=value).first()
    if row is None:
        raise ValidationError({field: "引用不存在或不属于当前可访问范围"})
    return row


def _lock_owner(owner_id):
    # Serialize graph changes and candidate numbering on the same existing owner lock.
    get_user_model().objects.select_for_update().get(pk=owner_id)


def _series_for_write(actor, series_id):
    owner_id = series_rows(actor).filter(pk=series_id).values_list("owner_id", flat=True).first()
    if owner_id is None:
        raise NotFound("素材系列不存在")
    _lock_owner(owner_id)
    row = series_rows(actor).select_for_update().filter(pk=series_id).first()
    if row is None:
        raise NotFound("素材系列不存在")
    return row


def _save(row):
    try:
        row.full_clean()
    except ModelValidationError as exc:
        fields = {field.name for field in row._meta.fields if field.is_relation}
        errors = exc.message_dict if hasattr(exc, "message_dict") else {"non_field_errors": exc.messages}
        raise ValidationError({f"{key}_id" if key in fields else key: value
            for key, value in errors.items()}) from exc
    row.save()
    return row


def _apply_series(row, values):
    if "name" in values:
        row.name = values["name"]
    for field, rows in (
        ("primary_series", AssetReferenceSeries.objects.filter(owner_id=row.owner_id)),
        ("bible_variant", BibleVariant.objects.filter(project__owner_id=row.owner_id).select_related("project")
            .only("id", "appearance_role", "project_id", "project__owner_id")),
    ):
        key = f"{field}_id"
        if key in values:
            setattr(row, field, None if values[key] is None else _reference(rows, values[key], key))


@transaction.atomic
def create_series(*, actor, values):
    _lock_owner(actor.pk)
    row = AssetReferenceSeries(owner=actor, role=values["role"])
    _apply_series(row, values)
    return _save(row)


@transaction.atomic
def update_series(*, actor, series_id, values):
    row = _series_for_write(actor, series_id)
    _apply_series(row, values)
    if row.versions.filter(setting_revision__isnull=False).exclude(
            setting_revision__variant_id=row.bible_variant_id).exists():
        raise ValidationError({"bible_variant_id": "已有版本引用原设定修订，请先明确移除这些版本关联"})
    if row.versions.filter(generated_from__isnull=False).exclude(
            generated_from__series_id=row.primary_series_id).exists():
        raise ValidationError({"primary_series_id": "已有生成版本引用原主图，请先明确移除这些版本关联"})
    return _save(row)


@transaction.atomic
def attach_asset(*, actor, series_id, asset_id, source, setting_revision_id=None, expected_media_type=None):
    """Associate an existing/uploaded Asset. Generated origins belong to the original writer (M4)."""
    row = _series_for_write(actor, series_id)
    try:
        target = resolve_media_fulfillment_target(series=row, asset_id=asset_id,
            source_mode=source, setting_revision_id=setting_revision_id, expected_media_type=expected_media_type)
    except ModelValidationError as exc:
        raise ValidationError(exc.message_dict if hasattr(exc, "message_dict") else exc.messages) from exc
    asset, setting = target.asset, target.setting
    source = target.source_mode
    same = row.versions.filter(asset=asset, source=source, setting_revision=setting).first()
    if same is not None:
        return same, False
    number = (row.versions.aggregate(last=Max("version"))["last"] or 0) + 1
    version = AssetReferenceVersion(series=row, asset=asset, source=source,
        setting_revision=setting, version=number)
    return _save(version), True


def _generated_material_origin(*, artifact=None, job=None):
    from studio.services.production_media_assets import read_registered_production_image
    from studio.services.media_generation_targets import read_media_generation_target
    from studio.services.bible_setting_evidence import primary_setting_content_reference, read_setting_revisions

    if (artifact is None) == (job is None):
        raise ValidationError("生成素材须明确唯一原产物或独立任务")
    if artifact is not None:
        owner = artifact.production_run.owner
        asset = read_registered_production_image(artifact)
        target = read_media_generation_target(artifact.generation_attempt.preparation).value
        if not target.get("appearance") or not target.get("setting"):
            raise ValidationError("配方素材登记需要明确外观及确切设定")
    else:
        from studio.services.asset_generation import read_generated_image_asset, _snapshot
        owner = job.owner
        asset = read_generated_image_asset(job)
        target = read_media_generation_target(_snapshot(job).preparation).value
        if not target.get("appearance"):
            return owner, asset, None, None, None
    setting = None
    if target["setting"] is not None:
        setting_id = target["setting"]["setting_revision_id"]
        variants = BibleVariant.objects.filter(revisions__pk=setting_id, project__owner=owner)
        if artifact is not None:
            variants = variants.filter(project_id=artifact.production_run.project_id)
        project_id = variants.values_list("project_id", flat=True).first()
        if project_id is None:
            raise ValidationError("生成素材的确切设定不属于原所有者")
        setting = read_setting_revisions([setting_id], project_id=project_id)[setting_id]
        if primary_setting_content_reference(setting) != target["setting"]:
            raise ValidationError("生成素材的设定与原冻结修订不一致")
    role = target["appearance"]["role"]
    if setting is not None and setting.variant.appearance_role != role:
        raise ValidationError("生成素材角色与原设定不一致")
    basis = None
    if role == "variant":
        primary = target["appearance"]["primary"]
        basis = AssetReferenceVersion.objects.select_related("series").filter(pk=primary["version_id"]).first()
        if (basis is None or basis.series.role != "primary" or basis.series.owner_id != owner.pk
                or str(basis.series_id) != primary["series_id"] or str(basis.asset_id) != primary["asset_id"]
                or (setting is not None and basis.setting_revision_id != setting.primary_setting_revision_id)):
            raise ValidationError("变体实际使用的原主图版本关系已损坏，不能改用最新版本")
    return owner, asset, setting, role, basis


def _registered_material_version(owner, asset, setting, role, basis, *, artifact=None, job=None):
    origin = {"generated_artifact": artifact} if artifact is not None else {"generation_job": job}
    existing = list(AssetReferenceVersion.objects.filter(**origin).select_related("series").order_by()[:2])
    if existing:
        if (len(existing) != 1 or existing[0].source != "generated" or existing[0].asset_id != asset.pk
                or existing[0].generation_job_id != (job.pk if job else None)
                or existing[0].generated_artifact_id != (artifact.pk if artifact else None)
                or existing[0].generated_from_id != (basis.pk if basis else None)
                or existing[0].setting_revision_id != (setting.pk if setting else None) or existing[0].series.owner_id != owner.pk
                or existing[0].series.role != role or existing[0].series.bible_variant_id != (setting.variant_id if setting else None)
                or existing[0].series.primary_series_id != (basis.series_id if basis else None)):
            raise ValidationError("生成素材的原版本关系不一致，不能补造或重新归属")
        return existing[0]
    return None


def read_generated_material(*, artifact=None, job=None):
    """Read only this candidate's original version; never register on a read."""
    if (artifact is None) == (job is None):
        raise ValidationError("生成素材须明确唯一原产物或独立任务")
    if artifact is not None and not artifact.asset_id:
        if AssetReferenceVersion.objects.filter(generated_artifact=artifact).exists():
            raise ValidationError("生成素材版本缺少原Asset关联，不能补造来源")
        return None
    return _registered_material_version(*_generated_material_origin(artifact=artifact, job=job), artifact=artifact, job=job)


@transaction.atomic
def register_generated_material(*, artifact=None, job=None):
    """Original completion/selection callers own their root; one version writer."""
    if (artifact is None) == (job is None):
        raise ValidationError("生成素材须明确唯一原产物或独立任务")
    owner = artifact.production_run.owner if artifact is not None else job.owner
    _lock_owner(owner.pk)
    origin = _generated_material_origin(artifact=artifact, job=job)
    owner, asset, setting, role, basis = origin
    existing = _registered_material_version(*origin, artifact=artifact, job=job)
    if existing is not None:
        return existing, False
    if role is None:
        return None, False  # Ordinary image: no appearance relationship was declared.
    # An unbound independent declaration has no shared identity. Do not group
    # unrelated jobs by a null setting, display name or a guessed current row.
    series = list(AssetReferenceSeries.objects.filter(owner=owner, bible_variant_id=setting.variant_id,
        role=role).select_for_update().order_by()[:2]) if setting is not None else []
    if len(series) > 1:
        raise ValidationError("本外观关联多个素材系列，请先明确关系，不能自动猜选")
    if series and series[0].primary_series_id != (basis.series_id if basis else None):
        raise ValidationError("本外观系列与实际主图系列不一致，不能自动改绑")
    row = series[0] if series else create_series(actor=owner, values={"role": role,
        "name": setting.name if setting else job.name, "bible_variant_id": str(setting.variant_id) if setting else None,
        "primary_series_id": str(basis.series_id) if basis else None})
    number = (row.versions.aggregate(last=Max("version"))["last"] or 0) + 1
    version = AssetReferenceVersion(series=row, version=number, asset=asset, source="generated",
        generated_artifact=artifact, generation_job=job, setting_revision=setting, generated_from=basis)
    return _save(version), True


class RelationshipInUse(APIException):
    status_code = 409
    default_code = "asset_reference_in_use"
    default_detail = "关系仍被其他系列、生成版本或生产素材选择引用；请先处理依赖关系。Asset及文件未删除。"


def removal_impact(row):
    """Read the exact relation scope, never collect or delete the referenced Assets."""
    series = isinstance(row, AssetReferenceSeries)
    versions = row.versions.all() if series else AssetReferenceVersion.objects.filter(pk=row.pk)
    version_ids = versions.values("pk")
    counts = versions.aggregate(version_count=Count("pk"), asset_count=Count("asset_id", distinct=True))
    variants = row.variants.count() if series else 0
    dependents = AssetReferenceVersion.objects.filter(generated_from_id__in=version_ids).exclude(pk__in=version_ids).count()
    production_uses = ProductionMaterialFulfillment.objects.filter(reference_version_id__in=version_ids).count()
    return {"target_type": "series" if series else "version", "id": row.pk,
        "name": row.name if series else f"v{row.version}", **counts,
        "variant_count": variants, "dependent_version_count": dependents, "production_use_count": production_uses,
        "can_remove": not (variants or dependents or production_uses)}


def bible_options(series):
    # Reuse the original settings. Staff editing another owner's series cannot bind their own Bible.
    return BibleVariant.objects.filter(project__owner_id=series.owner_id, appearance_role=series.role).values(
        "id", "name", "reference_name", "project_id", "project__title",
        "character__name", "scene__name", "prop__name").order_by("reference_name", "id")


def _remove(row):
    try:
        row.delete()
    except ProtectedError as exc:
        raise RelationshipInUse() from exc


@transaction.atomic
def remove_series(*, actor, series_id):
    _remove(_series_for_write(actor, series_id))


@transaction.atomic
def remove_version(*, actor, version_id):
    series_id = version_summaries(actor).filter(pk=version_id).values_list("series_id", flat=True).first()
    if series_id is None:
        raise NotFound("素材版本不存在")
    series = _series_for_write(actor, series_id)
    row = series.versions.filter(pk=version_id).first()
    if row is None:
        raise NotFound("素材版本不存在")
    _remove(row)
