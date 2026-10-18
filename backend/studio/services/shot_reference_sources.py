"""Resolve saved, ordered shot uses to exact library media, without selecting it.

Internal preparation/selection validation, not a list-page reader: selected
files are checked by the existing media reader. No writes, latest-version
lookup, legacy Shot pool, model slots, prompt prose or approval are produced.
Callers that write must resolve again inside their original write transaction.
"""
from copy import deepcopy
from dataclasses import dataclass
from uuid import UUID

from django.core.exceptions import ValidationError
from rest_framework.exceptions import ValidationError as APIValidationError

from studio.models import AssetReferenceVersion, SeriesProject, Shot, ShotReferenceUse
from studio.services.asset_provenance import is_uploaded_asset
from studio.services.bible_setting_evidence import primary_setting_content_reference, read_setting_revisions
from studio.services.bible_variants import BibleVariantConflict
from studio.services.execution_media_sources import resolve_execution_asset_media
from studio.services.media_generation_targets import read_media_fulfillment_version
from studio.services.security_boundaries import security_boundary_resolution_cache


@dataclass(frozen=True)
class ResolvedShotReferenceSources:
    project_id: str
    _shots: tuple

    @property
    def value(self):
        return {"schema_version": 1, "project_id": self.project_id, "shots": deepcopy(list(self._shots))}


def _fail(path, message):
    raise ValidationError({path: message})


def _uuid(value, path):
    try:
        return str(UUID(str(value)))
    except (TypeError, ValueError, AttributeError):
        _fail(path, "需要明确有效的对象ID")


def _use_rows(shot_ids):
    return ShotReferenceUse.objects.filter(shot_id__in=shot_ids).order_by("shot_id", "position").values(
        "id", "shot_id", "use_key", "position", "media_kind", "purpose", "setting_revision_id",
        "source_mode", "reference_version_id", "asset_id", "asset_sha256")


def _reference_project(actor, project_id, *, lock=False):
    if not getattr(actor, "is_authenticated", False):
        _fail("project_id", "需要登录后读取项目素材引用")
    project_id = _uuid(project_id, "project_id")
    projects = SeriesProject.objects.select_related("owner").only(
        "id", "owner_id", "owner__id", "owner__is_staff")
    if lock:
        projects = projects.select_for_update(of=("self",))
    if not actor.is_staff:
        projects = projects.filter(owner_id=actor.pk)
    project = projects.filter(pk=project_id).first()
    if project is None:
        _fail("project_id", "项目不存在或不属于当前可访问范围")
    return project


def _setting_identity(setting):
    if setting is None:
        return None
    variant = setting.variant
    kind = next(kind for kind in ("character", "scene", "prop") if getattr(variant, f"{kind}_id"))
    return {**primary_setting_content_reference(setting), "entity_kind": kind,
        "entity_id": str(getattr(variant, f"{kind}_id")), "appearance_role": variant.appearance_role,
        "reference_name": setting.reference_name}


@security_boundary_resolution_cache()
def resolve_shot_reference_sources(*, actor, project_id, shot_ids):
    """Resolve only persisted explicit choices, preserving caller shot/use order.

    An empty use list is legitimate. A declared but unselected use is not a
    completed input. The shot's selection mode and the version's acquisition
    source are separate facts (a generated version may be reused as existing).
    """
    project = _reference_project(actor, project_id)
    if not isinstance(shot_ids, (tuple, list)):
        _fail("shot_ids", "需要明确的有序镜头ID数组")
    shot_ids = [_uuid(pk, f"shot_ids[{index}]") for index, pk in enumerate(shot_ids)]
    if len(set(shot_ids)) != len(shot_ids):
        _fail("shot_ids", "镜头不能重复")
    found = {str(pk) for pk in Shot.objects.filter(episode__project_id=project.pk,
        pk__in=shot_ids).values_list("id", flat=True)}
    if found != set(shot_ids):
        _fail("shot_ids", "镜头缺失或不属于本项目，不能部分解析为成功")
    return _resolve_rows(project, shot_ids, list(_use_rows(shot_ids)))


@security_boundary_resolution_cache()
def _resolve_rows(project, shot_ids, rows):
    """Shared core for scoped saved rows or a writer's locked prospective rows.

    Not an authorization entry point. Callers must first scope the project,
    shots and use identities; proposed choices use this same media validation.
    """
    try:
        settings = read_setting_revisions({row["setting_revision_id"] for row in rows
            if row["setting_revision_id"]}, project_id=project.pk)
    except BibleVariantConflict as exc:
        _fail("reference_uses.setting_revision_id", str(exc.detail))
    version_ids = {row["reference_version_id"] for row in rows if row["reference_version_id"]}
    versions = {row.pk: row for row in AssetReferenceVersion.objects.filter(pk__in=version_ids)
        .select_related("series").only("id", "series_id", "version", "asset_id", "source",
            "setting_revision_id", "generated_artifact_id", "generation_job_id", "generated_from_id",
            "series__id", "series__owner_id", "series__role", "series__primary_series_id",
            "series__bible_variant_id")}
    by_shot = {pk: [] for pk in shot_ids}
    # Per-operation deduplication only: never reuse media observations in a
    # later request. Multiple explicit uses of one asset remain separate uses.
    resolved_versions, resolved_assets = {}, {}
    for row in rows:
        path = f"reference_uses.{row['shot_id']}.{row['use_key']}"
        kind, mode = row["media_kind"], row["source_mode"]
        if kind not in {"image", "video", "audio"} or mode not in {"existing", "upload", "generated", ""}:
            _fail(path, "素材类型或来源方式不在已声明合同中")
        if not row["use_key"]:
            _fail(path, "素材使用身份缺失")
        if not row["reference_version_id"] and not row["asset_id"]:
            _fail(path, "本用途尚未选择确切素材，不推断主图或最新版本")
        if bool(row["reference_version_id"]) == bool(row["asset_id"]) or not mode:
            _fail(path, "必须明确选择一个素材版本或直接Asset及来源方式")
        setting = settings.get(str(row["setting_revision_id"]))
        version = None
        try:
            if row["reference_version_id"]:
                version = versions.get(row["reference_version_id"])
                if version is None or version.series.owner_id != project.owner_id:
                    _fail(path, "素材版本缺失或属于其他用户，不能寻找替代版本")
                if setting is not None:
                    if (version.series.bible_variant_id is not None
                            and version.series.bible_variant_id != setting.variant_id):
                        _fail(path, "素材系列关联的是另一实体或变体")
                    if version.setting_revision_id is not None and version.setting_revision_id != setting.pk:
                        _fail(path, "素材版本关联的确切设定修订与本用途不一致")
                if mode == "generated" and version.source != "generated":
                    _fail(path, "已有或上传版本不能当成本次生成来源")
                key = (version.pk, kind)
                if key not in resolved_versions:
                    resolved_versions[key] = read_media_fulfillment_version(version, expected_media_type=kind)
                resolved = resolved_versions[key]
                asset, checksum = resolved.asset, resolved.asset_sha256
            else:
                if mode == "generated":
                    _fail(path, "生成来源需要原登记素材版本，不能仅凭Asset补造生成依据")
                key = (row["asset_id"], kind)
                if key not in resolved_assets:
                    resolved_assets[key] = resolve_execution_asset_media(owner=project.owner,
                        asset_id=row["asset_id"], expected_kind=kind, max_media_bytes=None)
                resolved = resolved_assets[key]
                asset, checksum = resolved.asset, resolved.media_facts["sha256"]
                if resolved.media_facts["mime_type"] != asset.mime_type:
                    _fail(path, "素材登记MIME与实际文件不一致")
            if mode == "upload" and not is_uploaded_asset(asset):
                _fail(path, "所选素材没有正式上传来源，不能伪记上传")
            if row["asset_sha256"] != checksum:
                _fail(path, "素材指纹已偏离本次选择，不能改用当前文件冒充原输入")
        except ValidationError as exc:
            if hasattr(exc, "message_dict") and path in exc.message_dict:
                raise
            _fail(path, exc.messages)
        except (BibleVariantConflict, APIValidationError) as exc:
            _fail(path, str(exc.detail))
        source = {"kind": "asset", "asset_id": str(asset.pk)} if version is None else {
            "kind": "reference_version", "reference_version_id": str(version.pk),
            "series_id": str(version.series_id), "version": version.version,
            "acquisition_source": version.source}
        by_shot[str(row["shot_id"])].append({"use_id": str(row["id"]), "use_key": row["use_key"],
            "position": row["position"], "media_kind": kind, "purpose": row["purpose"],
            "setting": _setting_identity(setting), "source_mode": mode, "source": source,
            "asset": {"asset_id": str(asset.pk), "sha256": checksum, "mime_type": asset.mime_type}})
    return ResolvedShotReferenceSources(str(project.pk), tuple({"shot_id": pk, "uses": by_shot[pk]} for pk in shot_ids))
