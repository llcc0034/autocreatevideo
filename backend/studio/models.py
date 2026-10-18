from __future__ import annotations

import uuid
from pathlib import Path

from django.conf import settings
from django.core.exceptions import ValidationError
from django.core.validators import MaxValueValidator, MinValueValidator, RegexValidator
from django.db import models
from django.db.models import F, Q
from django.utils import timezone


def asset_upload_path(instance, filename: str) -> str:
    """Store uploads in a non-user-controlled, owner-scoped media directory."""
    owner = str(instance.owner_id or "unassigned")
    extension = Path(filename).suffix.lower()[:16]
    now = timezone.now()
    return f"assets/{owner}/{now:%Y/%m}/{uuid.uuid4().hex}{extension}"


def script_source_upload_path(instance, filename: str) -> str:
    """Store imported scripts under an owner/project scoped opaque name."""

    owner = str(instance.owner_id or "unassigned")
    project = str(instance.project_id or "unassigned")
    extension = Path(filename).suffix.lower()[:16]
    now = timezone.now()
    return f"scripts/{owner}/{project}/{now:%Y/%m}/{uuid.uuid4().hex}{extension}"


class UUIDTimeStampedModel(models.Model):
    id = models.UUIDField(primary_key=True, default=uuid.uuid4, editable=False)
    created_at = models.DateTimeField(auto_now_add=True)
    updated_at = models.DateTimeField(auto_now=True)

    class Meta:
        abstract = True


class UserSecurityProfile(UUIDTimeStampedModel):
    """Security state that cannot be represented by Django's role flags alone."""

    user = models.OneToOneField(
        settings.AUTH_USER_MODEL,
        on_delete=models.CASCADE,
        related_name="security_profile",
    )
    must_change_password = models.BooleanField(default=False)
    auth_version = models.PositiveIntegerField(default=1)
    temporary_password_issued_at = models.DateTimeField(null=True, blank=True)
    password_changed_at = models.DateTimeField(null=True, blank=True)

    class Meta:
        ordering = ("user__username",)


class AccessControlAuditEvent(UUIDTimeStampedModel):
    """Append-only record for privileged identity and role mutations."""

    class Action(models.TextChoices):
        CREATE_USER = "create_user", "创建账号"
        CHANGE_ROLE = "change_role", "变更角色"
        CHANGE_STATUS = "change_status", "变更状态"
        RESET_PASSWORD = "reset_password", "重置临时密码"
        CHANGE_PASSWORD = "change_password", "用户修改密码"
        BOOTSTRAP_SUPERUSER = "bootstrap_superuser", "初始化超级管理员"

    actor = models.ForeignKey(
        settings.AUTH_USER_MODEL,
        null=True,
        blank=True,
        on_delete=models.PROTECT,
        related_name="access_control_actions",
    )
    target_user = models.ForeignKey(
        settings.AUTH_USER_MODEL,
        on_delete=models.PROTECT,
        related_name="access_control_audit_events",
    )
    action = models.CharField(max_length=32, choices=Action.choices)
    before_role = models.CharField(max_length=24, blank=True)
    after_role = models.CharField(max_length=24, blank=True)
    before_active = models.BooleanField(null=True, blank=True)
    after_active = models.BooleanField(null=True, blank=True)
    reason = models.CharField(max_length=500, blank=True)

    class Meta:
        ordering = ("-created_at",)
        indexes = [
            models.Index(fields=("target_user", "-created_at")),
            models.Index(fields=("actor", "-created_at")),
        ]

    def save(self, *args, **kwargs):
        if self.pk and AccessControlAuditEvent.objects.filter(pk=self.pk).exists():
            raise ValidationError("AccessControlAuditEvent 是不可变审计事实")
        return super().save(*args, **kwargs)

    def delete(self, *args, **kwargs):
        raise ValidationError("AccessControlAuditEvent 是不可变审计事实")


class SecretBinding(UUIDTimeStampedModel):
    """Stable database reference for encrypted credential material."""

    key = models.CharField(
        max_length=120,
        unique=True,
        validators=[RegexValidator(r"^[a-z][a-z0-9_.-]{1,119}$")],
    )
    label = models.CharField(max_length=160)
    purpose = models.CharField(max_length=120)
    is_active = models.BooleanField(default=True)
    active_version = models.ForeignKey(
        "SecretVersion",
        null=True,
        blank=True,
        on_delete=models.PROTECT,
        related_name="+",
    )
    created_by = models.ForeignKey(
        settings.AUTH_USER_MODEL,
        null=True,
        blank=True,
        on_delete=models.PROTECT,
        related_name="created_secret_bindings",
    )

    class Meta:
        ordering = ("key",)

    def __str__(self) -> str:
        return self.key

    def clean(self):
        super().clean()
        if self.active_version_id and self.active_version.binding_id != self.id:
            raise ValidationError({"active_version": "当前版本必须属于同一个受管凭据"})

    def save(self, *args, **kwargs):
        if self.pk and SecretBinding.objects.filter(pk=self.pk).exists():
            original = SecretBinding.objects.only("key", "purpose").get(pk=self.pk)
            if original.key != self.key or original.purpose != self.purpose:
                raise ValidationError("已有凭据版本后不能修改 key 或 purpose")
        self.full_clean(exclude=("active_version",) if not self.active_version_id else ())
        return super().save(*args, **kwargs)


class SecretVersion(UUIDTimeStampedModel):
    """Immutable AES-GCM ciphertext revision for one secret binding."""

    binding = models.ForeignKey(
        SecretBinding,
        on_delete=models.CASCADE,
        related_name="versions",
    )
    version = models.PositiveIntegerField(validators=[MinValueValidator(1)])
    key_id = models.CharField(max_length=64)
    algorithm = models.CharField(max_length=32, default="AES-256-GCM")
    nonce_b64 = models.CharField(max_length=32)
    ciphertext_b64 = models.TextField()
    ciphertext_sha256 = models.CharField(max_length=64)
    aad_version = models.PositiveIntegerField(default=1)
    created_by = models.ForeignKey(
        settings.AUTH_USER_MODEL,
        null=True,
        blank=True,
        on_delete=models.PROTECT,
        related_name="created_secret_versions",
    )

    class Meta:
        ordering = ("binding", "-version")
        constraints = [
            models.UniqueConstraint(
                fields=("binding", "version"),
                name="unique_secret_binding_version",
            ),
        ]

    def save(self, *args, **kwargs):
        if self.pk and SecretVersion.objects.filter(pk=self.pk).exists():
            raise ValidationError("SecretVersion 是不可变审计事实")
        return super().save(*args, **kwargs)

    def delete(self, *args, **kwargs):
        raise ValidationError("SecretVersion 是不可变审计事实")


class SecretAuditEvent(UUIDTimeStampedModel):
    """Append-only metadata audit; never stores secret material or ciphertext."""

    class Action(models.TextChoices):
        CREATE = "create", "创建"
        ROTATE = "rotate", "轮换"
        DEACTIVATE = "deactivate", "停用"
        ACTIVATE = "activate", "启用"

    binding = models.ForeignKey(
        SecretBinding,
        on_delete=models.PROTECT,
        related_name="audit_events",
    )
    version = models.PositiveIntegerField(null=True, blank=True)
    actor = models.ForeignKey(
        settings.AUTH_USER_MODEL,
        null=True,
        blank=True,
        on_delete=models.PROTECT,
        related_name="secret_audit_events",
    )
    action = models.CharField(max_length=16, choices=Action.choices)
    reason = models.CharField(max_length=500, blank=True)

    class Meta:
        ordering = ("-created_at",)
        indexes = [models.Index(fields=("binding", "-created_at"))]

    def save(self, *args, **kwargs):
        if self.pk and SecretAuditEvent.objects.filter(pk=self.pk).exists():
            raise ValidationError("SecretAuditEvent 是不可变审计事实")
        return super().save(*args, **kwargs)

    def delete(self, *args, **kwargs):
        raise ValidationError("SecretAuditEvent 是不可变审计事实")


class AppendOnlyAuditEventQuerySet(models.QuerySet):
    def update(self, **kwargs):
        raise ValidationError("平台生命周期审计事实不允许更新")

    def delete(self):
        raise ValidationError("平台生命周期审计事实不允许删除")


class AppendOnlyAuditEventManager(models.Manager.from_queryset(AppendOnlyAuditEventQuerySet)):
    pass


class PlatformLifecycleAuditEvent(UUIDTimeStampedModel):
    """Append-only, non-sensitive facts for platform registry mutations."""

    SAFE_SUMMARY_FIELDS = frozenset({
        "preparation_id", "parent_preparation_id", "field_batch_key", "attempt_number",
        "duplicate_billing_risk_acknowledged",
        "task_id", "attempt_id", "error_code", "error_message_sha256", "task_error_sha256",
        "finished_at", "receipt_sha256", "request_sha256", "new_model_call",
        "allow_external_calls", "cost_currency",
        "activated_at", "activation_state", "based_on_revision_id",
        "credential_bound", "definition_sha256", "domain", "driver_type",
        "category", "channel", "contract_sha256", "field_definition_id", "field_definition_version_id",
        "graph_sha256", "initial_value_sha256", "input_contract_sha256",
        "is_active", "is_current", "is_default", "is_required", "is_secret",
        "is_system", "key", "locator_sha256", "modality", "model_profile_version_id",
        "profile_key", "profile_version_id", "project_id", "provider_id",
        "configuration_class",
        "prompt_profile_version_id", "review_state", "source_selector",
        "published_at", "recipe_key", "restart_required", "retirement_reason", "revision", "run_id",
        "role", "runtime_config_sha256", "runtime_host_id", "scope_type", "sha256",
        "size_bytes", "sort_order",
        "smoke_test_evidence_sha256", "smoke_test_state", "specification_sha256",
        "status", "template_id", "validated_deployment_id", "validation_evidence_sha256",
        "validation_state", "value_schema_sha256", "value_type", "values_sha256", "verified_at",
        "verified_snapshot_id", "version", "position", "model_family", "mode",
        "operation", "output_kind", "priority", "engine_key", "engine_version", "projector_id", "parser_key",
    })

    actor = models.ForeignKey(
        settings.AUTH_USER_MODEL,
        null=True,
        blank=True,
        on_delete=models.PROTECT,
        related_name="platform_lifecycle_audit_events",
    )
    action = models.CharField(
        max_length=32,
        validators=[RegexValidator(r"^[a-z][a-z0-9_.-]{1,31}$")],
    )
    object_type = models.CharField(
        max_length=80,
        validators=[RegexValidator(r"^[a-z][a-z0-9_.-]{1,79}$")],
    )
    object_id = models.UUIDField()
    reason_code = models.CharField(
        max_length=80,
        validators=[RegexValidator(r"^[a-z][a-z0-9_.-]{1,79}$")],
    )
    before_sha256 = models.CharField(max_length=64, blank=True)
    after_sha256 = models.CharField(max_length=64, blank=True)
    before_summary = models.JSONField(default=dict, blank=True)
    after_summary = models.JSONField(default=dict, blank=True)
    objects = AppendOnlyAuditEventManager()

    class Meta:
        ordering = ("-created_at",)
        indexes = [
            models.Index(
                fields=("object_type", "object_id", "-created_at"),
                name="studio_plat_object_43e831_idx",
            ),
            models.Index(
                fields=("actor", "-created_at"),
                name="studio_plat_actor_i_fa3a5f_idx",
            ),
            models.Index(
                fields=("action", "-created_at"),
                name="studio_plat_action_4306dd_idx",
            ),
        ]

    @classmethod
    def _validate_summary(cls, field_name: str, summary) -> None:
        if not isinstance(summary, dict):
            raise ValidationError({field_name: "审计摘要必须是扁平对象"})
        unsupported = set(summary) - cls.SAFE_SUMMARY_FIELDS
        if unsupported:
            raise ValidationError({
                field_name: f"审计摘要包含未授权字段: {', '.join(sorted(unsupported))}",
            })
        if any(isinstance(value, (dict, list, tuple, set)) for value in summary.values()):
            raise ValidationError({field_name: "审计摘要不能包含嵌套正文"})

    def save(self, *args, **kwargs):
        if self.pk and PlatformLifecycleAuditEvent.objects.filter(pk=self.pk).exists():
            raise ValidationError("PlatformLifecycleAuditEvent 是不可变审计事实")
        self._validate_summary("before_summary", self.before_summary)
        self._validate_summary("after_summary", self.after_summary)
        self.full_clean()
        return super().save(*args, **kwargs)

    def delete(self, *args, **kwargs):
        raise ValidationError("PlatformLifecycleAuditEvent 是不可变审计事实")


class StyleCategory(UUIDTimeStampedModel):
    key = models.SlugField(max_length=80, unique=True)
    name = models.CharField(max_length=120, unique=True)
    description = models.CharField(max_length=500, blank=True)
    sort_order = models.PositiveIntegerField(default=0)
    is_active = models.BooleanField(default=True)

    class Meta:
        ordering = ("sort_order", "name", "pk")

    def __str__(self) -> str:
        return self.name


class Style(UUIDTimeStampedModel):

    owner = models.ForeignKey(settings.AUTH_USER_MODEL, null=True, blank=True, on_delete=models.CASCADE, related_name="styles")
    slug = models.SlugField(max_length=80)
    name = models.CharField(max_length=120)
    category = models.ForeignKey(StyleCategory, to_field="key", db_column="category", null=True, blank=True, on_delete=models.PROTECT, related_name="styles")
    description = models.TextField(blank=True)
    visual_prompt = models.TextField(blank=True)
    negative_prompt = models.TextField(blank=True)
    thumbnail_path = models.CharField(max_length=500, blank=True)
    defaults = models.JSONField(default=dict, blank=True)
    is_system = models.BooleanField(default=False)
    is_active = models.BooleanField(default=True)

    class Meta:
        ordering = ("name",)
        constraints = [
            models.UniqueConstraint(fields=("owner", "slug"), name="unique_style_slug_per_owner"),
            models.UniqueConstraint(fields=("slug",), condition=Q(owner__isnull=True), name="unique_global_style_slug"),
        ]

    def __str__(self) -> str:
        return self.name


class Asset(UUIDTimeStampedModel):
    class Kind(models.TextChoices):
        CHARACTER = "character", "人物"
        SCENE = "scene", "场景"
        PROP = "prop", "道具"
        REFERENCE = "reference", "参考"
        REFERENCE_VIDEO = "reference_video", "参考视频"
        REFERENCE_AUDIO = "reference_audio", "参考音频"
        AUDIO = "audio", "音频"
        SCRIPT = "script", "剧本"
        OUTPUT_IMAGE = "output_image", "生成图片"
        OUTPUT_VIDEO = "output_video", "生成视频"

    owner = models.ForeignKey(settings.AUTH_USER_MODEL, null=True, blank=True, on_delete=models.CASCADE, related_name="assets")
    style = models.ForeignKey(Style, null=True, blank=True, on_delete=models.PROTECT, related_name="assets")
    kind = models.CharField(max_length=32, choices=Kind.choices)
    name = models.CharField(max_length=160)
    description = models.TextField(blank=True)
    prompt_hint = models.TextField(blank=True)
    source_info = models.CharField(
        max_length=1000,
        blank=True,
        help_text="面向素材库用户的来源说明；系统生成素材由服务端写入并保留机器 provenance。",
    )
    file = models.FileField(upload_to=asset_upload_path, blank=True)
    local_path = models.CharField(max_length=1000, blank=True)
    reference_url = models.URLField(max_length=1000, blank=True)
    mime_type = models.CharField(max_length=120, blank=True)
    checksum = models.CharField(max_length=128, blank=True, db_index=True)
    metadata = models.JSONField(default=dict, blank=True)
    is_active = models.BooleanField(default=True)

    class Meta:
        ordering = ("kind", "name")
        indexes = [models.Index(fields=("style", "kind", "is_active"))]

    def __str__(self) -> str:
        return self.name


class AssetReferenceSeries(UUIDTimeStampedModel):
    """One primary or named variant; its versions still point to ordinary Assets."""

    class Role(models.TextChoices):
        PRIMARY = "primary", "主图"
        VARIANT = "variant", "变体"

    owner = models.ForeignKey(settings.AUTH_USER_MODEL, on_delete=models.CASCADE, related_name="asset_reference_series")
    name = models.CharField(max_length=160)
    role = models.CharField(max_length=8, choices=Role.choices)
    primary_series = models.ForeignKey("self", null=True, blank=True, on_delete=models.PROTECT, related_name="variants")
    bible_variant = models.ForeignKey("BibleVariant", null=True, blank=True, on_delete=models.PROTECT,
        related_name="asset_reference_series")

    class Meta:
        ordering = ("name", "id")
        indexes = [models.Index(fields=("owner", "role", "name"), name="idx_asset_series_owner_role")]
        constraints = [
            models.CheckConstraint(condition=(Q(role="primary", primary_series__isnull=True)
                | Q(role="variant", primary_series__isnull=False)), name="ck_asset_series_role_parent"),
            models.CheckConstraint(condition=~Q(primary_series=F("id")), name="ck_asset_series_not_self"),
        ]

    def clean(self):
        super().clean()
        if self.primary_series_id:
            primary = self.primary_series
            if primary.role != self.Role.PRIMARY or primary.primary_series_id is not None:
                raise ValidationError({"primary_series": "变体只能关联主图系列，不支持多级变体"})
            if primary.owner_id != self.owner_id:
                raise ValidationError({"primary_series": "主图与变体必须属于同一素材所有者"})
        if self.bible_variant_id:
            setting = self.bible_variant
            if setting.appearance_role != self.role or setting.project.owner_id != self.owner_id:
                raise ValidationError({"bible_variant": "设定角色或所属项目与素材系列不一致"})


class AssetReferenceVersion(UUIDTimeStampedModel):
    """A relation, not a second file, candidate, execution, or setting snapshot."""

    class Source(models.TextChoices):
        UPLOAD = "upload", "上传素材"
        EXISTING = "existing", "已有素材"
        GENERATED = "generated", "模型生成"

    series = models.ForeignKey(AssetReferenceSeries, on_delete=models.CASCADE, related_name="versions")
    version = models.PositiveIntegerField(validators=[MinValueValidator(1)])
    asset = models.ForeignKey(Asset, on_delete=models.PROTECT, related_name="reference_versions")
    source = models.CharField(max_length=16, choices=Source.choices)
    origin_history_cleared = models.BooleanField(default=False, editable=False,
        help_text="生成来源历史已显式清理；保留素材和主图/变体关系，不伪造上传来源。")
    generated_artifact = models.ForeignKey("GeneratedArtifact", null=True, blank=True, on_delete=models.PROTECT,
        related_name="reference_versions")
    generation_job = models.ForeignKey("AssetGenerationJob", null=True, blank=True, on_delete=models.PROTECT,
        related_name="reference_versions")
    generated_from = models.ForeignKey("self", null=True, blank=True, on_delete=models.PROTECT,
        related_name="generated_variants")
    setting_revision = models.ForeignKey("BibleVariantRevision", null=True, blank=True, on_delete=models.PROTECT,
        related_name="asset_reference_versions")

    class Meta:
        ordering = ("series_id", "-version", "id")
        constraints = [
            models.UniqueConstraint(fields=("series", "version"), name="uk_asset_series_version"),
            models.CheckConstraint(condition=Q(version__gte=1), name="ck_asset_ref_version_positive"),
            models.CheckConstraint(condition=(
                Q(source="generated", origin_history_cleared=False, generated_artifact__isnull=False, generation_job__isnull=True)
                | Q(source="generated", origin_history_cleared=False, generated_artifact__isnull=True, generation_job__isnull=False)
                | Q(source="generated", origin_history_cleared=True, generated_artifact__isnull=True, generation_job__isnull=True)
                | Q(source__in=("upload", "existing"), generated_artifact__isnull=True,
                    generation_job__isnull=True, generated_from__isnull=True, origin_history_cleared=False)),
                name="ck_asset_ref_source_origin"),
            models.CheckConstraint(condition=~Q(generated_from=F("id")), name="ck_asset_ref_basis_not_self"),
        ]

    def clean(self):
        super().clean()
        if not self.series_id or not self.asset_id:
            return  # Field validation reports missing required relations.
        if self.asset.owner_id is not None and self.asset.owner_id != self.series.owner_id:
            raise ValidationError({"asset": "不能关联其他用户的素材"})
        if self.setting_revision_id and self.setting_revision.variant_id != self.series.bible_variant_id:
            raise ValidationError({"setting_revision": "设定修订不属于本系列明确关联的设定"})
        if self.source == self.Source.GENERATED:
            if self.generated_artifact_id and self.generated_artifact.asset_id != self.asset_id:
                raise ValidationError({"generated_artifact": "生成产物不指向所选Asset"})
            if self.generation_job_id and self.generation_job.output_asset_id != self.asset_id:
                raise ValidationError({"generation_job": "素材任务输出不指向所选Asset"})
            if self.series.role == AssetReferenceSeries.Role.VARIANT:
                if not self.generated_from_id:
                    raise ValidationError({"generated_from": "生成变体须记录确切主图素材版本"})
                if self.generated_from.series_id != self.series.primary_series_id:
                    raise ValidationError({"generated_from": "生成依据不属于本变体的主图系列"})
            elif self.generated_from_id:
                raise ValidationError({"generated_from": "主图版本不能记录变体生成依据"})


class SeriesProject(UUIDTimeStampedModel):
    class Status(models.TextChoices):
        DRAFT = "draft", "草稿"
        ACTIVE = "active", "制作中"
        PAUSED = "paused", "已暂停"
        COMPLETED = "completed", "已完结"
        ARCHIVED = "archived", "已归档"

    owner = models.ForeignKey(settings.AUTH_USER_MODEL, on_delete=models.CASCADE, related_name="series_projects")
    title = models.CharField(max_length=200)
    logline = models.TextField(blank=True)
    master_script = models.TextField(blank=True)
    status = models.CharField(max_length=20, choices=Status.choices, default=Status.DRAFT)
    style = models.ForeignKey(Style, null=True, blank=True, on_delete=models.PROTECT, related_name="projects")
    cover_asset = models.ForeignKey(
        Asset,
        null=True,
        blank=True,
        on_delete=models.SET_NULL,
        related_name="cover_projects",
    )
    target_model = models.CharField(max_length=80, blank=True, default="")
    language = models.CharField(max_length=32)
    aspect_ratio = models.CharField(max_length=16)
    width = models.PositiveIntegerField()
    height = models.PositiveIntegerField()
    fps = models.PositiveSmallIntegerField(validators=[MinValueValidator(1)])
    default_shot_duration_ms = models.PositiveIntegerField(
        validators=[MinValueValidator(1)],
    )
    continuity_seed = models.BigIntegerField()
    continuity_summary = models.TextField(blank=True)
    production_settings = models.JSONField(default=dict, blank=True)
    completed_at = models.DateTimeField(null=True, blank=True)
    is_favorite = models.BooleanField(default=False)

    class Meta:
        ordering = ("-updated_at",)
        indexes = [models.Index(fields=("owner", "status"))]

    def __str__(self) -> str:
        return self.title


class BibleBase(UUIDTimeStampedModel):
    project = models.ForeignKey(SeriesProject, on_delete=models.CASCADE)
    slug = models.SlugField(max_length=80)
    name = models.CharField(max_length=160)
    description = models.TextField(blank=True)
    negative_constraints = models.TextField(blank=True)
    anchor_asset = models.ForeignKey(Asset, null=True, blank=True, on_delete=models.SET_NULL, related_name="+")
    reference_assets = models.ManyToManyField(Asset, blank=True, related_name="+")
    # Exact receipt for the current image bindings, not another Asset/version ledger.
    image_binding_review = models.ForeignKey("StageRun", null=True, blank=True,
        on_delete=models.RESTRICT, related_name="+")
    metadata = models.JSONField(default=dict, blank=True)
    is_locked = models.BooleanField(default=True)

    class Meta:
        abstract = True
        ordering = ("name",)


class CharacterBible(BibleBase):
    project = models.ForeignKey(SeriesProject, on_delete=models.CASCADE, related_name="characters")
    stable_speaker_id = models.CharField(
        max_length=16,
        validators=[RegexValidator(r"^S[1-9][0-9]*$", "speaker ID 必须为 S1、S2 这样的格式")],
    )
    role = models.CharField(max_length=120, blank=True)
    age_appearance = models.CharField(max_length=120, blank=True)
    face_identity = models.TextField(blank=True)
    wardrobe = models.TextField(blank=True)
    voice_profile = models.TextField(blank=True)
    personality = models.TextField(blank=True)

    class Meta(BibleBase.Meta):
        constraints = [
            models.UniqueConstraint(fields=("project", "slug"), name="unique_character_slug_per_project"),
            models.UniqueConstraint(fields=("project", "stable_speaker_id"), name="unique_speaker_per_project"),
        ]

    def __str__(self) -> str:
        return f"{self.name} ({self.stable_speaker_id})"


class SceneBible(BibleBase):
    project = models.ForeignKey(SeriesProject, on_delete=models.CASCADE, related_name="scenes")
    location = models.CharField(max_length=200, blank=True)
    time_of_day = models.CharField(max_length=80, blank=True)
    lighting = models.TextField(blank=True)
    color_palette = models.TextField(blank=True)
    ambient_sound = models.TextField(blank=True)

    class Meta(BibleBase.Meta):
        constraints = [models.UniqueConstraint(fields=("project", "slug"), name="unique_scene_slug_per_project")]

    def __str__(self) -> str:
        return self.name


class PropBible(BibleBase):
    project = models.ForeignKey(SeriesProject, on_delete=models.CASCADE, related_name="props")
    material = models.CharField(max_length=160, blank=True)
    distinctive_marks = models.TextField(blank=True)
    continuity_state = models.TextField(blank=True)

    class Meta(BibleBase.Meta):
        constraints = [models.UniqueConstraint(fields=("project", "slug"), name="unique_prop_slug_per_project")]

    def __str__(self) -> str:
        return self.name


class BibleEntityIdentity(UUIDTimeStampedModel):
    """Project-scoped declaration key; creative content stays on the Bible entity."""

    project = models.ForeignKey(SeriesProject, on_delete=models.CASCADE, related_name="bible_entity_identities")
    entity_key = models.CharField(max_length=64)
    character = models.OneToOneField(CharacterBible, null=True, blank=True, on_delete=models.CASCADE,
        related_name="declaration_identity")
    scene = models.OneToOneField(SceneBible, null=True, blank=True, on_delete=models.CASCADE,
        related_name="declaration_identity")
    prop = models.OneToOneField(PropBible, null=True, blank=True, on_delete=models.CASCADE,
        related_name="declaration_identity")

    class Meta:
        constraints = [
            models.UniqueConstraint(fields=("project", "entity_key"), name="uk_bible_project_entity_key"),
            models.CheckConstraint(condition=~Q(entity_key=""), name="ck_bible_entity_key_nonempty"),
            models.CheckConstraint(condition=(
                Q(character__isnull=False, scene__isnull=True, prop__isnull=True)
                | Q(character__isnull=True, scene__isnull=False, prop__isnull=True)
                | Q(character__isnull=True, scene__isnull=True, prop__isnull=False)
            ), name="ck_bible_identity_one_entity"),
        ]

    def clean(self):
        super().clean()
        for kind in ("character", "scene", "prop"):
            if not getattr(self, f"{kind}_id"):
                continue
            try:
                entity = getattr(self, kind)
            except (CharacterBible.DoesNotExist, SceneBible.DoesNotExist, PropBible.DoesNotExist) as exc:
                raise ValidationError({kind: "关联的Bible实体不存在"}) from exc
            if entity.project_id != self.project_id:
                raise ValidationError({kind: "声明key只能关联本项目的Bible实体"})
        if not self._state.adding:
            fields = ("project_id", "entity_key", "character_id", "scene_id", "prop_id")
            previous = type(self).objects.filter(pk=self.pk).values(*fields).first()
            if previous and any(previous[field] != getattr(self, field) for field in fields):
                raise ValidationError("稳定声明key与实体的关联不能改写；名称和设定应在原实体中编辑")

    def save(self, *args, **kwargs):
        self.full_clean()
        return super().save(*args, **kwargs)


class ScriptBibleEntityBinding(UUIDTimeStampedModel):
    """One exact script declaration's receipt, not the project entity identity."""

    script_revision = models.ForeignKey("ScriptRevision", on_delete=models.PROTECT, related_name="bible_bindings")
    entity_key = models.CharField(max_length=64)
    declaration_sha256 = models.CharField(max_length=64)
    character = models.ForeignKey(CharacterBible, null=True, blank=True, on_delete=models.PROTECT, related_name="script_bindings")
    scene = models.ForeignKey(SceneBible, null=True, blank=True, on_delete=models.PROTECT, related_name="script_bindings")
    prop = models.ForeignKey(PropBible, null=True, blank=True, on_delete=models.PROTECT, related_name="script_bindings")
    objects = AppendOnlyAuditEventManager()

    class Meta:
        constraints = [
            models.UniqueConstraint(fields=("script_revision", "entity_key"), name="uk_script_bible_entity"),
            *[models.UniqueConstraint(fields=("script_revision", kind),
                name=f"uk_script_bible_{kind}") for kind in ("character", "scene", "prop")],
            models.CheckConstraint(condition=(
                Q(character__isnull=False, scene__isnull=True, prop__isnull=True)
                | Q(character__isnull=True, scene__isnull=False, prop__isnull=True)
                | Q(character__isnull=True, scene__isnull=True, prop__isnull=False)
            ), name="ck_script_bible_one_entity"),
        ]

    def save(self, *args, **kwargs):
        if not self._state.adding:
            raise ValidationError("剧本声明来源绑定不可覆盖")
        kwargs["force_insert"] = True
        return super().save(*args, **kwargs)

    def delete(self, *args, **kwargs):
        raise ValidationError("剧本声明来源绑定不可删除")


class ScriptBibleAppearanceBinding(UUIDTimeStampedModel):
    """Exact setting used for a source declaration; no duplicate creative body."""

    source_binding = models.ForeignKey(ScriptBibleEntityBinding, on_delete=models.PROTECT, related_name="setting_bindings")
    variant_key = models.CharField(max_length=64, blank=True)
    declaration_sha256 = models.CharField(max_length=64)
    setting = models.ForeignKey("BibleVariantRevision", on_delete=models.PROTECT, related_name="script_uses")
    setting_sha256 = models.CharField(max_length=64)
    objects = AppendOnlyAuditEventManager()

    class Meta:
        constraints = [models.UniqueConstraint(fields=("source_binding", "variant_key"), name="uk_script_appearance_setting")]

    def save(self, *args, **kwargs):
        if not self._state.adding:
            raise ValidationError("声明引用的确切设定修订不可覆盖")
        kwargs["force_insert"] = True
        return super().save(*args, **kwargs)

    def delete(self, *args, **kwargs):
        raise ValidationError("声明设定引用不可删除")


class BibleVariant(UUIDTimeStampedModel):
    """A named appearance/state, never a generated candidate number."""

    project = models.ForeignKey(SeriesProject, on_delete=models.PROTECT, related_name="bible_variants")
    character = models.ForeignKey(CharacterBible, null=True, blank=True, on_delete=models.PROTECT, related_name="variants")
    scene = models.ForeignKey(SceneBible, null=True, blank=True, on_delete=models.PROTECT, related_name="variants")
    prop = models.ForeignKey(PropBible, null=True, blank=True, on_delete=models.PROTECT, related_name="variants")
    appearance_role = models.CharField(max_length=8, choices=(("primary", "主图"), ("variant", "变体")), default="variant")
    source_binding = models.ForeignKey(ScriptBibleEntityBinding, null=True, blank=True, on_delete=models.PROTECT, related_name="appearances")
    source_variant_key = models.CharField(max_length=64, blank=True)
    name = models.CharField(max_length=160)
    reference_name = models.CharField(max_length=160)
    reference_key = models.CharField(max_length=191)
    revision = models.PositiveIntegerField(default=1)
    created_by = models.ForeignKey(settings.AUTH_USER_MODEL, on_delete=models.PROTECT, related_name="created_bible_variants")

    class Meta:
        ordering = ("created_at", "id")
        constraints = [
            models.CheckConstraint(condition=(
                Q(character__isnull=False, scene__isnull=True, prop__isnull=True)
                | Q(character__isnull=True, scene__isnull=False, prop__isnull=True)
                | Q(character__isnull=True, scene__isnull=True, prop__isnull=False)
            ), name="ck_bible_variant_one_entity"),
            models.CheckConstraint(condition=Q(revision__gte=1), name="ck_bible_variant_revision"),
            models.UniqueConstraint(fields=("project", "reference_key"), name="uk_bible_variant_reference"),
            models.CheckConstraint(condition=Q(appearance_role__in=("primary", "variant")), name="ck_bible_appearance_role"),
            models.CheckConstraint(condition=(Q(source_binding__isnull=True, source_variant_key="")
                | Q(source_binding__isnull=False, appearance_role="primary", source_variant_key="")
                | (Q(source_binding__isnull=False, appearance_role="variant") & ~Q(source_variant_key=""))),
                name="ck_bible_appearance_source"),
            *[models.UniqueConstraint(fields=(kind, "source_variant_key"), condition=Q(source_binding__isnull=False),
                name=f"uk_{kind}_declared_look") for kind in ("character", "scene", "prop")],
            *[models.UniqueConstraint(fields=(kind,), condition=Q(appearance_role="primary"),
                name=f"uk_bible_primary_{kind}") for kind in ("character", "scene", "prop")],
        ]

    def save(self, *args, **kwargs):
        if not self._state.adding:
            fields = ("project_id", "character_id", "scene_id", "prop_id", "appearance_role",
                "source_binding_id", "source_variant_key", "created_by_id")
            original = type(self).objects.filter(pk=self.pk).values(*fields).get()
            if any(original[key] != getattr(self, key) for key in fields):
                raise ValidationError("设定归属、主图/变体角色与声明来源不可改写")
        return super().save(*args, **kwargs)

    def clean(self):
        super().clean()
        entities = [getattr(self, key) for key in ("character", "scene", "prop") if getattr(self, f"{key}_id")]
        if len(entities) != 1 or entities[0].project_id != self.project_id:
            raise ValidationError("变体必须属于同一项目的一个 Bible 对象")


class BibleVariantRevision(UUIDTimeStampedModel):
    """Complete image-generation intent. Old revisions are never rewritten."""

    variant = models.ForeignKey(BibleVariant, on_delete=models.PROTECT, related_name="revisions")
    revision = models.PositiveIntegerField()
    name = models.CharField(max_length=160)
    reference_name = models.CharField(max_length=160)
    identity_anchor = models.TextField(blank=True)
    appearance_anchor = models.TextField()
    basis_asset = models.ForeignKey(Asset, null=True, blank=True, on_delete=models.PROTECT, related_name="bible_variant_bases")
    basis_asset_sha256 = models.CharField(max_length=64, blank=True)
    payload_schema_version = models.PositiveSmallIntegerField(default=2)
    source_declaration_sha256 = models.CharField(max_length=64, blank=True)
    primary_setting_revision = models.ForeignKey("self", null=True, blank=True, on_delete=models.PROTECT, related_name="derived_settings")
    content_sha256 = models.CharField(max_length=64)
    created_by = models.ForeignKey(settings.AUTH_USER_MODEL, on_delete=models.PROTECT, related_name="bible_variant_revisions")
    objects = AppendOnlyAuditEventManager()

    class Meta:
        ordering = ("-revision",)
        constraints = [
            models.UniqueConstraint(fields=("variant", "revision"), name="uk_bible_variant_revision"),
            models.CheckConstraint(condition=Q(revision__gte=1), name="ck_bible_variant_rev_positive"),
            models.CheckConstraint(condition=(Q(basis_asset__isnull=True, basis_asset_sha256="")
                | (Q(basis_asset__isnull=False) & ~Q(basis_asset_sha256=""))), name="ck_bible_variant_basis_sha"),
            models.CheckConstraint(condition=Q(payload_schema_version__in=(1, 2)), name="ck_bible_setting_schema"),
        ]

    def save(self, *args, **kwargs):
        if not self._state.adding:
            raise ValidationError("变体设定修订不可覆盖；请另存新修订")
        kwargs["force_insert"] = True
        return super().save(*args, **kwargs)

    def delete(self, *args, **kwargs):
        raise ValidationError("变体设定修订是不可删除的来源事实")


class BibleVariantReview(UUIDTimeStampedModel):
    """Human review of one exact setting revision, not adoption of an image."""

    setting = models.OneToOneField(BibleVariantRevision, on_delete=models.PROTECT, related_name="review")
    decision = models.CharField(max_length=8, choices=(("approve", "确认设定"), ("reject", "拒绝设定")))
    comment = models.CharField(max_length=1000, blank=True)
    decided_by = models.ForeignKey(settings.AUTH_USER_MODEL, on_delete=models.PROTECT, related_name="bible_variant_reviews")
    objects = AppendOnlyAuditEventManager()

    class Meta:
        constraints = [models.CheckConstraint(condition=Q(decision__in=("approve", "reject")), name="ck_bible_variant_review")]

    def save(self, *args, **kwargs):
        if not self._state.adding:
            raise ValidationError("变体设定审核不可覆盖")
        kwargs["force_insert"] = True
        return super().save(*args, **kwargs)

    def delete(self, *args, **kwargs):
        raise ValidationError("变体设定审核是不可删除的人工决定")


class ConsistencyLock(UUIDTimeStampedModel):
    class Scope(models.TextChoices):
        GLOBAL = "global", "全局"
        STYLE = "style", "风格"
        CHARACTER = "character", "人物"
        SCENE = "scene", "场景"
        PROP = "prop", "道具"

    project = models.ForeignKey(SeriesProject, on_delete=models.CASCADE, related_name="consistency_locks")
    scope = models.CharField(max_length=20, choices=Scope.choices)
    target_id = models.UUIDField(null=True, blank=True)
    lock_key = models.CharField(max_length=120)
    locked_value = models.JSONField(default=dict)
    version = models.PositiveIntegerField(default=1)
    is_active = models.BooleanField(default=True)

    class Meta:
        ordering = ("scope", "lock_key")
        constraints = [
            models.UniqueConstraint(
                fields=("project", "scope", "target_id", "lock_key"),
                name="unique_consistency_lock",
            )
        ]

    def __str__(self) -> str:
        return f"{self.scope}:{self.lock_key}@v{self.version}"


class Episode(UUIDTimeStampedModel):
    class Status(models.TextChoices):
        DRAFT = "draft", "草稿"
        READY = "ready", "待生成"
        GENERATING = "generating", "生成中"
        REVIEW = "review", "待审核"
        COMPLETED = "completed", "已完成"

    project = models.ForeignKey(SeriesProject, on_delete=models.CASCADE, related_name="episodes")
    number = models.PositiveIntegerField()
    title = models.CharField(max_length=200)
    synopsis = models.TextField(blank=True)
    script = models.TextField(blank=True)
    status = models.CharField(max_length=20, choices=Status.choices, default=Status.DRAFT)
    continuity_from_previous = models.TextField(blank=True)
    continuity_to_next = models.TextField(blank=True)
    prompt_settings = models.JSONField(default=dict, blank=True)

    class Meta:
        ordering = ("project", "number")
        constraints = [models.UniqueConstraint(fields=("project", "number"), name="unique_episode_number")]

    def __str__(self) -> str:
        return f"{self.project.title} E{self.number}: {self.title}"


class Shot(UUIDTimeStampedModel):
    class MediaType(models.TextChoices):
        VIDEO = "video", "视频"
        IMAGE = "image", "图片"

    episode = models.ForeignKey(Episode, on_delete=models.CASCADE, related_name="shots")
    number = models.PositiveIntegerField()
    title = models.CharField(max_length=200, blank=True)
    dialogue = models.JSONField(default=list, blank=True, help_text='[{"speaker_id":"S1","text":"..."}]')
    duration_ms = models.PositiveIntegerField(
        validators=[MinValueValidator(1)],
    )
    episode_start_ms = models.PositiveIntegerField(null=True, blank=True,
        help_text="镜头在本集中的明确开始时间（毫秒）；与结束时间成对填写，不是源视频截取时间")
    episode_end_ms = models.PositiveIntegerField(null=True, blank=True,
        help_text="镜头在本集中的明确结束时间（毫秒）；两者都为空表示只按时长顺序排列")
    media_type = models.CharField(max_length=12, choices=MediaType.choices)
    conditioning_mode = models.CharField(
        max_length=64,
        blank=True,
        default="",
        validators=[RegexValidator(r"^[A-Za-z][A-Za-z0-9_.-]{0,63}$")],
    )
    include_audio = models.BooleanField()
    scene = models.ForeignKey(SceneBible, null=True, blank=True, on_delete=models.SET_NULL, related_name="shots")
    props = models.ManyToManyField(PropBible, blank=True, related_name="shots")
    # These flags express a continuity requirement only. They never imply
    # that a concrete first/last-frame Asset has been selected for a call.
    requires_first_frame_continuity = models.BooleanField(default=False)
    requires_last_frame_continuity = models.BooleanField(default=False)
    metadata = models.JSONField(default=dict, blank=True)

    class Meta:
        ordering = ("episode", "number")
        constraints = [models.UniqueConstraint(fields=("episode", "number"), name="unique_shot_number"),
            models.CheckConstraint(condition=(
                models.Q(episode_start_ms__isnull=True, episode_end_ms__isnull=True)
                | (models.Q(episode_start_ms__isnull=False, episode_end_ms__isnull=False,
                    episode_end_ms__gt=models.F("episode_start_ms"))
                    & models.Q(duration_ms=models.F("episode_end_ms") - models.F("episode_start_ms")))
            ), name="shot_episode_interval_valid")]

    def __str__(self) -> str:
        return f"E{self.episode.number}S{self.number}: {self.title}"




class ShotScriptSource(models.Model):
    """Exact authored source of a materialized shot, without a second value store.

    Coordinates address the saved semantic document, not mutable display numbers.
    The original Stage/command owns execution/adoption receipts. These rows only
    bind shots to that creation basis; they never imply approval or media success.
    """

    shot = models.OneToOneField(Shot, primary_key=True, on_delete=models.CASCADE, related_name="script_source")
    structure = models.ForeignKey("ScriptRevisionStructure", on_delete=models.PROTECT, related_name="shot_sources")
    stage = models.ForeignKey("StageRun", on_delete=models.PROTECT, related_name="shot_sources", db_index=False)
    episode_number = models.PositiveIntegerField(validators=[MinValueValidator(1)])
    shot_number = models.PositiveIntegerField(validators=[MinValueValidator(1)])
    document_sha256 = models.CharField(max_length=64, validators=[RegexValidator(r"^[0-9a-f]{64}$")])
    materialization_sha256 = models.CharField(max_length=64, validators=[RegexValidator(r"^[0-9a-f]{64}$")])
    created_at = models.DateTimeField(auto_now_add=True)

    class Meta:
        constraints = [
            models.UniqueConstraint(fields=("stage", "episode_number", "shot_number"), name="uk_shot_script_coordinate"),
            models.CheckConstraint(condition=Q(episode_number__gt=0, shot_number__gt=0), name="ck_shot_script_coordinate"),
        ]

    def clean(self):
        super().clean()
        if self.shot_id and self.structure_id and self.stage_id:
            project_id = self.shot.episode.project_id
            if (self.structure.revision.source.project_id != project_id
                    or self.stage.production_run.project_id != project_id or self.stage.stage_key != "storyboard"):
                raise ValidationError("镜头、结构化修订和分镜阶段必须属于同一个项目")
            if self.document_sha256 != self.structure.document_sha256:
                raise ValidationError("镜头来源摘要与确切结构化修订不一致")


class ShotFieldRevision(UUIDTimeStampedModel):
    """An explicit manual revision of independent inputs, not another script."""

    shot = models.ForeignKey(Shot, on_delete=models.CASCADE, related_name="field_revisions", db_index=False)
    revision = models.PositiveIntegerField()
    previous = models.ForeignKey("self", null=True, blank=True, on_delete=models.PROTECT, related_name="+")
    source_sha256 = models.CharField(max_length=64)
    overrides = models.JSONField()
    content_schema_version = models.PositiveSmallIntegerField(default=1)
    content_size_bytes = models.PositiveIntegerField()
    content_sha256 = models.CharField(max_length=64)
    request_id = models.UUIDField()
    request_sha256 = models.CharField(max_length=64)
    created_by = models.ForeignKey(settings.AUTH_USER_MODEL, on_delete=models.PROTECT, related_name="shot_field_revisions")
    objects = AppendOnlyAuditEventManager()

    class Meta:
        ordering = ("-revision",)
        constraints = [
            models.UniqueConstraint(fields=("shot", "revision"), name="uk_shot_field_revision"),
            models.UniqueConstraint(fields=("shot", "request_id"), name="uk_shot_field_request"),
            models.CheckConstraint(condition=Q(revision__gte=1), name="ck_shot_field_revision"),
            models.CheckConstraint(condition=Q(content_size_bytes__lte=65536), name="ck_shot_field_bytes"),
            models.CheckConstraint(condition=Q(content_schema_version=1), name="ck_shot_field_schema"),
        ]

    def save(self, *args, **kwargs):
        if not self._state.adding:
            raise ValidationError("镜头独立字段修订不可覆盖，请另存新修订")
        kwargs["force_insert"] = True
        return super().save(*args, **kwargs)

    def delete(self, *args, **kwargs):
        raise ValidationError("镜头独立字段修订不能单独删除")


class ShotCharacterRelation(UUIDTimeStampedModel):
    """Authored visibility or dialogue identity, never inferred from a reference.

    Speaker position addresses the original dialogue row; visible position is
    authored visible-cast order. One character can independently have both roles.
    The common storyboard writer owns these rows.
    """

    class Role(models.TextChoices):
        VISIBLE = "visible", "可见人物"
        SPEAKER = "speaker", "对白发言者"

    shot = models.ForeignKey(Shot, on_delete=models.CASCADE, related_name="character_relations", db_index=False)
    character = models.ForeignKey(CharacterBible, on_delete=models.PROTECT, related_name="shot_relations")
    role = models.CharField(max_length=8, choices=Role.choices)
    position = models.PositiveIntegerField()

    class Meta:
        ordering = ("role", "position")
        constraints = [
            models.UniqueConstraint(fields=("shot", "role", "position"), name="uk_shot_character_role_position"),
            models.UniqueConstraint(fields=("shot", "character"), condition=Q(role="visible"),
                name="uk_shot_visible_character"),
            models.CheckConstraint(condition=Q(role__in=("visible", "speaker")), name="ck_shot_character_role"),
        ]

    def clean(self):
        super().clean()
        if self.shot_id and self.character_id and self.character.project_id != self.shot.episode.project_id:
            raise ValidationError({"character": "镜头人物必须属于同一项目"})
        if self.shot_id and self.role == self.Role.SPEAKER and (
                type(self.position) is not int or not isinstance(self.shot.dialogue, list)
                or not 0 <= self.position < len(self.shot.dialogue)):
            raise ValidationError({"position": "发言者位置必须指向本镜明确的对白行"})


class ShotReferenceUse(UUIDTimeStampedModel):
    """One ordered use of an exact setting and, when selected, an exact medium.

    No media bytes, setting prose, model slots or fabricated execution records.
    Blank source_mode means no choice yet, not automatic generation. Selection
    mode is a shot-level choice, separate from the Asset's actual provenance.
    """

    shot = models.ForeignKey(Shot, on_delete=models.CASCADE, related_name="reference_uses", db_index=False)
    use_key = models.CharField(max_length=64)
    position = models.PositiveIntegerField()
    media_kind = models.CharField(max_length=8, choices=(("image", "图片"), ("video", "视频"), ("audio", "音频")))
    purpose = models.CharField(max_length=160, blank=True)
    setting_revision = models.ForeignKey(BibleVariantRevision, null=True, blank=True, on_delete=models.PROTECT,
        related_name="shot_reference_uses")
    source_mode = models.CharField(max_length=16, choices=AssetReferenceVersion.Source.choices, blank=True)
    reference_version = models.ForeignKey(AssetReferenceVersion, null=True, blank=True, on_delete=models.PROTECT,
        related_name="shot_reference_uses")
    # Independent image/video/audio Assets need not acquire a Bible or series.
    asset = models.ForeignKey(Asset, null=True, blank=True, on_delete=models.PROTECT, related_name="shot_reference_uses")
    asset_sha256 = models.CharField(max_length=64, blank=True, validators=[RegexValidator(r"^[0-9a-f]{64}$")])

    class Meta:
        ordering = ("position",)
        constraints = [
            models.UniqueConstraint(fields=("shot", "use_key"), name="uk_shot_reference_use_key"),
            models.UniqueConstraint(fields=("shot", "position"), name="uk_shot_reference_use_position"),
            models.CheckConstraint(condition=Q(media_kind__in=("image", "video", "audio")), name="ck_shot_use_media_kind"),
            models.CheckConstraint(condition=~Q(use_key=""), name="ck_shot_use_identity"),
            models.CheckConstraint(condition=Q(source_mode__in=("", "generated", "existing", "upload")),
                name="ck_shot_use_source_mode"),
            models.CheckConstraint(condition=(Q(reference_version__isnull=True, asset__isnull=True, asset_sha256="")
                | ((Q(reference_version__isnull=False, asset__isnull=True)
                    | Q(reference_version__isnull=True, asset__isnull=False))
                    & ~Q(source_mode="") & ~Q(asset_sha256=""))), name="ck_shot_use_exact_source"),
        ]

    def clean(self):
        super().clean()
        if not self.shot_id:
            return
        project = self.shot.episode.project
        if self.setting_revision_id and self.setting_revision.variant.project_id != project.pk:
            raise ValidationError({"setting_revision": "逐镜设定修订必须属于同一项目"})
        selected_asset = self.asset if self.asset_id else None
        if self.reference_version_id:
            version = self.reference_version
            if version.series.owner_id != project.owner_id:
                raise ValidationError({"reference_version": "不能使用其他用户的素材版本"})
            if (version.setting_revision_id and self.setting_revision_id
                    and version.setting_revision_id != self.setting_revision_id):
                raise ValidationError({"reference_version": "素材版本关联的设定修订与本次用途不一致"})
            selected_asset = version.asset
        if selected_asset is not None:
            if selected_asset.owner_id not in (None, project.owner_id):
                raise ValidationError({"asset": "不能使用其他用户的素材"})
            if selected_asset.mime_type.split("/", 1)[0] != self.media_kind:
                raise ValidationError({"media_kind": "所选素材的已记录媒体类型与本次用途不一致"})
            if self.asset_sha256 != selected_asset.checksum:
                raise ValidationError({"asset_sha256": "所选素材指纹已改变或未明确指定"})


class PromptFieldTranslation(UUIDTimeStampedModel):
    """Human-reviewed translation overlay for one prompt-impacting source field.

    Source records remain authoritative and are never rewritten by translation.
    A source adapter may consume an overlay only when it is confirmed and its
    source hash still matches the current field value.
    """

    class ScopeType(models.TextChoices):
        PROJECT = "project", "Project"
        CHARACTER = "character", "Character"
        SCENE = "scene", "Scene"
        PROP = "prop", "Prop"
        SHOT = "shot", "Shot"

    class Status(models.TextChoices):
        DRAFT_TRANSLATION = "draft_translation", "Pending review"
        CONFIRMED = "confirmed", "Confirmed"
        NEEDS_RETRANSLATION = "needs_retranslation", "Needs retranslation"
        FAILED = "failed", "Translation failed"

    project = models.ForeignKey(
        SeriesProject, on_delete=models.CASCADE, related_name="prompt_field_translations",
    )
    scope_type = models.CharField(max_length=24, choices=ScopeType.choices)
    scope_id = models.UUIDField()
    field_key = models.CharField(max_length=160)
    source_text = models.TextField()
    source_hash = models.CharField(max_length=64, db_index=True)
    translated_text = models.TextField(blank=True)
    status = models.CharField(
        max_length=32, choices=Status.choices, default=Status.DRAFT_TRANSLATION,
    )
    revision = models.PositiveIntegerField(default=1)
    provider = models.CharField(max_length=80, blank=True)
    model = models.CharField(max_length=200, blank=True)
    error_message = models.TextField(blank=True)
    runtime_metadata = models.JSONField(default=dict, blank=True)
    revision_history = models.JSONField(default=list, blank=True)
    origin_generation_attempt = models.ForeignKey(
        "GenerationAttempt",
        null=True,
        blank=True,
        on_delete=models.PROTECT,
        related_name="prompt_field_translations",
    )
    confirmed_by = models.ForeignKey(
        settings.AUTH_USER_MODEL, null=True, blank=True, on_delete=models.SET_NULL,
        related_name="confirmed_prompt_field_translations",
    )
    confirmed_at = models.DateTimeField(null=True, blank=True)

    class Meta:
        ordering = ("scope_type", "field_key")
        constraints = [
            models.UniqueConstraint(
                fields=("project", "scope_type", "scope_id", "field_key"),
                name="unique_prompt_field_translation",
            ),
        ]
        indexes = [
            models.Index(
                fields=("project", "scope_type", "scope_id", "status"),
                name="studio_pft_scope_status_idx",
            ),
        ]

    def __str__(self) -> str:
        return f"{self.scope_type}:{self.scope_id}:{self.field_key}@{self.revision}"


class ProjectTranslationRequest(UUIDTimeStampedModel):
    """Project source ownership of one original text-candidate Job, not a queue."""

    project = models.ForeignKey(SeriesProject, on_delete=models.CASCADE, related_name="translation_requests", db_index=False)
    job = models.OneToOneField("AssetGenerationJob", on_delete=models.CASCADE, related_name="translation_request")
    request_sha256 = models.CharField(max_length=64)
    source_lang = models.CharField(max_length=40)
    target_lang = models.CharField(max_length=40)

    class Meta:
        ordering = ("-created_at", "-id")
        indexes = [models.Index(fields=("project", "-created_at", "-id"), name="idx_translation_request")]


class ProjectTranslationSource(UUIDTimeStampedModel):
    """Frozen write-back identity. Candidate text remains in the original Job."""

    request = models.ForeignKey(ProjectTranslationRequest, on_delete=models.CASCADE, related_name="sources", db_index=False)
    position = models.PositiveIntegerField()
    scope_type = models.CharField(max_length=24, choices=PromptFieldTranslation.ScopeType.choices)
    scope_id = models.UUIDField()
    field_key = models.CharField(max_length=160)
    expected_entry_id = models.UUIDField(null=True)
    expected_revision = models.PositiveIntegerField()
    source_text = models.TextField()
    source_sha256 = models.CharField(max_length=64)
    context_sha256 = models.CharField(max_length=64)
    item_sha256 = models.CharField(max_length=64)
    literals = models.JSONField()
    literals_schema_version = models.PositiveIntegerField(default=1)
    literals_size_bytes = models.PositiveIntegerField()
    literals_sha256 = models.CharField(max_length=64)
    adopted_entry = models.ForeignKey(PromptFieldTranslation, on_delete=models.RESTRICT, null=True, related_name="candidate_sources", db_index=False)
    adopted_revision = models.PositiveIntegerField(null=True)
    adopted_by = models.ForeignKey(settings.AUTH_USER_MODEL, on_delete=models.SET_NULL, null=True, related_name="adopted_translation_sources", db_index=False)
    adopted_at = models.DateTimeField(null=True)

    class Meta:
        ordering = ("position",)
        constraints = [
            models.UniqueConstraint(fields=("request", "position"), name="uk_translation_source_position"),
            models.UniqueConstraint(fields=("request", "scope_type", "scope_id", "field_key"), name="uk_translation_source_field"),
            models.CheckConstraint(condition=Q(expected_entry_id__isnull=True, expected_revision=0)
                | Q(expected_entry_id__isnull=False, expected_revision__gte=1), name="ck_translation_source_expected"),
            models.CheckConstraint(condition=Q(adopted_entry__isnull=True, adopted_revision__isnull=True, adopted_at__isnull=True)
                | Q(adopted_entry__isnull=False, adopted_revision__isnull=False, adopted_revision__gte=1, adopted_at__isnull=False), name="ck_translation_source_adopted"),
        ]


class WorkflowTemplate(UUIDTimeStampedModel):
    class OutputKind(models.TextChoices):
        VIDEO = "video", "视频"
        IMAGE = "image", "图片"
        AUDIO = "audio", "音频"
        TEXT = "text", "文本"

    owner = models.ForeignKey(settings.AUTH_USER_MODEL, null=True, blank=True, on_delete=models.CASCADE, related_name="workflow_templates")
    name = models.CharField(max_length=160)
    slug = models.SlugField(max_length=100)
    provider = models.CharField(max_length=80, default="comfyui")
    output_kind = models.CharField(max_length=12, choices=OutputKind.choices)
    model = models.CharField(max_length=80, blank=True, default="")
    version = models.PositiveIntegerField(default=1)
    definition = models.JSONField(default=dict)
    input_schema = models.JSONField(default=dict, blank=True)
    description = models.TextField(blank=True)
    is_system = models.BooleanField(default=False)
    is_active = models.BooleanField(default=True)

    class Meta:
        ordering = ("name", "-version")
        constraints = [
            models.UniqueConstraint(fields=("owner", "slug", "version"), name="unique_workflow_version_per_owner"),
            models.UniqueConstraint(
                fields=("slug", "version"), condition=Q(owner__isnull=True), name="unique_global_workflow_version"
            ),
        ]

    def __str__(self) -> str:
        return f"{self.name} v{self.version}"




class WorkerLease(models.Model):
    """Database mutex row used to serialize task claims across worker processes."""

    key = models.CharField(max_length=80, primary_key=True)
    holder_id = models.CharField(max_length=64, blank=True)
    expires_at = models.DateTimeField(null=True, blank=True)
    updated_at = models.DateTimeField(auto_now=True)


class GenerationTask(UUIDTimeStampedModel):
    class DistributionState(models.TextChoices):
        NOT_REQUESTED = "not_requested", "尚未安排副本"
        QUEUED = "queued", "副本等待处理"
        RUNNING = "running", "正在处理副本"
        READY = "ready", "副本已完成"
        FAILED = "failed", "副本处理失败"

    class Status(models.TextChoices):
        QUEUED = "queued", "排队中"
        RUNNING = "running", "运行中"
        SUCCEEDED = "succeeded", "已成功"
        FAILED = "failed", "已失败"
        CANCEL_REQUESTED = "cancel_requested", "取消中"
        CANCELLED = "cancelled", "已取消"

    class Kind(models.TextChoices):
        VIDEO = "video", "视频"
        IMAGE = "image", "图片"
        AUDIO = "audio", "音频"

    class SubmissionState(models.TextChoices):
        UNPREPARED = "unprepared", "未准备"
        PREPARED = "prepared", "已持久化提交标识"
        SUBMITTED = "submitted", "已提交"
        RECOVERED = "recovered", "已恢复远端提交"
        TERMINAL_SUCCEEDED = "terminal_succeeded", "远端执行已成功，平台产物另行验收"
        TERMINAL_FAILED = "terminal_failed", "远端执行已明确失败"
        TERMINAL_CANCELLED = "terminal_cancelled", "远端执行已明确取消"

    class RemoteState(models.TextChoices):
        UNKNOWN = "", "未知"
        CLAIMED = "claimed", "工作线程已领取"
        PREPARING = "preparing", "准备工作流"
        SUBMITTED = "submitted", "已提交到远端"
        RUNNING = "running", "远端执行中"
        DEGRADED = "degraded", "远端健康检查异常"
        CANCELLING = "cancelling", "正在取消远端任务"
        RECOVERED = "recovered", "已恢复远端任务"
        MISSING = "missing", "远端任务缺失"
        SUCCEEDED = "succeeded", "远端执行成功"
        FAILED = "failed", "远端执行失败"
        CANCELLED = "cancelled", "远端已取消"

    class ProgressSource(models.TextChoices):
        NONE = "none", "No quantitative progress"
        SUBMISSION = "submission", "Submission milestone"
        COMFYUI = "comfyui", "ComfyUI node telemetry"
        TERMINAL = "terminal", "Terminal task state"

    class ProgressTelemetryState(models.TextChoices):
        UNKNOWN = "unknown", "Not observed"
        CONNECTED = "connected", "WebSocket connected"
        RECEIVING = "receiving", "Progress telemetry received"
        UNAVAILABLE = "unavailable", "Progress telemetry unavailable"

    owner = models.ForeignKey(settings.AUTH_USER_MODEL, on_delete=models.CASCADE, related_name="generation_tasks")
    project = models.ForeignKey(SeriesProject, on_delete=models.CASCADE, related_name="generation_tasks")
    episode = models.ForeignKey(Episode, null=True, blank=True, on_delete=models.CASCADE, related_name="generation_tasks")
    kind = models.CharField(max_length=12, choices=Kind.choices)
    staged_inputs = models.JSONField(default=list, blank=True)
    submission_token = models.CharField(max_length=120, blank=True, db_index=True)
    submission_state = models.CharField(
        max_length=20, choices=SubmissionState.choices, default=SubmissionState.UNPREPARED,
    )
    submission_prepared_at = models.DateTimeField(null=True, blank=True)
    submission_submitted_at = models.DateTimeField(null=True, blank=True)
    status = models.CharField(max_length=24, choices=Status.choices, default=Status.QUEUED, db_index=True)
    priority = models.SmallIntegerField(default=0)
    progress = models.PositiveSmallIntegerField(default=0, validators=[MaxValueValidator(100)])
    progress_source = models.CharField(
        max_length=16, choices=ProgressSource.choices, default=ProgressSource.NONE,
    )
    comfy_progress = models.PositiveSmallIntegerField(
        null=True, blank=True, validators=[MaxValueValidator(100)],
    )
    progress_node_id = models.CharField(max_length=120, blank=True)
    progress_updated_at = models.DateTimeField(null=True, blank=True)
    deadline_started_at = models.DateTimeField(null=True, blank=True)
    hard_deadline_at = models.DateTimeField(null=True, blank=True, db_index=True)
    idle_deadline_at = models.DateTimeField(null=True, blank=True, db_index=True)
    last_meaningful_progress_at = models.DateTimeField(null=True, blank=True)
    progress_marker = models.CharField(max_length=191, blank=True)
    progress_telemetry_state = models.CharField(
        max_length=20,
        choices=ProgressTelemetryState.choices,
        default=ProgressTelemetryState.UNKNOWN,
    )
    progress_telemetry_error = models.TextField(blank=True)
    attempts = models.PositiveSmallIntegerField(default=0)
    comfy_prompt_id = models.CharField(max_length=120, blank=True, db_index=True)
    client_id = models.CharField(max_length=120, blank=True)
    outputs = models.JSONField(default=list, blank=True)
    # Scheduling state for this task's original post-processing phase, not a
    # second generation task or a replacement for signed output provenance.
    distribution_state = models.CharField(max_length=16, choices=DistributionState.choices, default=DistributionState.NOT_REQUESTED)
    distribution_revision = models.PositiveIntegerField(default=0)
    distribution_claim = models.UUIDField(null=True, blank=True)
    distribution_requested_at = models.DateTimeField(null=True, blank=True)
    distribution_started_at = models.DateTimeField(null=True, blank=True)
    distribution_heartbeat_at = models.DateTimeField(null=True, blank=True)
    distribution_finished_at = models.DateTimeField(null=True, blank=True)
    distribution_error = models.CharField(max_length=500, blank=True)
    error_message = models.TextField(blank=True)
    heartbeat_at = models.DateTimeField(null=True, blank=True, db_index=True)
    stalled_at = models.DateTimeField(null=True, blank=True)
    remote_state = models.CharField(
        max_length=20, choices=RemoteState.choices, default=RemoteState.UNKNOWN, blank=True,
    )
    remote_error = models.TextField(blank=True)
    queued_at = models.DateTimeField(auto_now_add=True)
    started_at = models.DateTimeField(null=True, blank=True)
    finished_at = models.DateTimeField(null=True, blank=True)

    class Meta:
        ordering = ("-priority", "queued_at")
        indexes = [models.Index(fields=("status", "priority", "queued_at")),
            models.Index(fields=("distribution_state", "distribution_requested_at", "id"), name="idx_task_distribution_work")]
        constraints = [models.CheckConstraint(condition=(
            Q(distribution_state="not_requested", distribution_claim__isnull=True, distribution_revision=0,
                distribution_requested_at__isnull=True, distribution_started_at__isnull=True, distribution_finished_at__isnull=True)
            | Q(status="succeeded", distribution_revision__gte=1, distribution_requested_at__isnull=False) & (
                Q(distribution_state="queued", distribution_claim__isnull=True, distribution_started_at__isnull=True, distribution_finished_at__isnull=True)
                | Q(distribution_state="running", distribution_claim__isnull=False, distribution_started_at__isnull=False, distribution_finished_at__isnull=True)
                | Q(distribution_state__in=("ready", "failed"), distribution_claim__isnull=True, distribution_finished_at__isnull=False)
            )), name="ck_task_distribution_phase"),
            models.CheckConstraint(condition=~Q(distribution_state="running") | Q(distribution_heartbeat_at__isnull=False),
                name="ck_task_distribution_beat")]

    def __str__(self) -> str:
        return f"{self.kind}:{self.id} [{self.status}]"


class GenerationTaskObservation(UUIDTimeStampedModel):
    """Small per-check worker facts, never a second execution-input snapshot."""

    class Kind(models.TextChoices):
        PREFLIGHT = "preflight", "提交前节点检查"
        RESOURCE_RELEASE = "resource_release", "提交前资源释放"

    class Status(models.TextChoices):
        PASSED = "passed", "通过"
        FAILED = "failed", "失败"

    task = models.ForeignKey(GenerationTask, on_delete=models.CASCADE, related_name="observations", db_index=False)
    kind = models.CharField(max_length=24, choices=Kind.choices)
    status = models.CharField(max_length=16, choices=Status.choices)
    claim_number = models.PositiveSmallIntegerField(validators=[MinValueValidator(1)])
    claim_started_at = models.DateTimeField()
    started_at = models.DateTimeField()
    finished_at = models.DateTimeField()
    request_sha256 = models.CharField(max_length=64, validators=[RegexValidator(r"^[a-f0-9]{64}$")])
    node_count = models.PositiveIntegerField(null=True, blank=True)
    link_count = models.PositiveIntegerField(null=True, blank=True)
    schema_sha256 = models.CharField(max_length=64, blank=True, validators=[RegexValidator(r"^[a-f0-9]{64}$")])
    policy_sha256 = models.CharField(max_length=64, blank=True, validators=[RegexValidator(r"^[a-f0-9]{64}$")])
    unload_models = models.BooleanField(null=True, blank=True)
    free_memory = models.BooleanField(null=True, blank=True)
    http_status = models.PositiveSmallIntegerField(null=True, blank=True, validators=[MinValueValidator(100), MaxValueValidator(599)])
    error_code = models.CharField(max_length=100, blank=True, validators=[RegexValidator(r"^[a-z][a-z0-9_]{0,99}$")])

    class Meta:
        ordering = ("-created_at", "-id")
        indexes = [models.Index(fields=("task", "-created_at", "-id"), name="idx_task_observation")]
        constraints = [
            models.CheckConstraint(condition=Q(claim_number__gte=1), name="ck_task_observation_claim"),
            models.CheckConstraint(condition=Q(finished_at__gte=models.F("started_at")), name="ck_task_observation_time"),
            models.CheckConstraint(condition=Q(status="passed", error_code="") | Q(status="failed") & ~Q(error_code=""),
                name="ck_task_observation_result"),
        ]


    def clean(self):
        super().clean()
        if self.kind == self.Kind.PREFLIGHT:
            if self.status == self.Status.PASSED and (self.node_count is None or self.link_count is None or not self.schema_sha256):
                raise ValidationError("通过的节点检查必须记录数量与节点目录摘要")
            if self.policy_sha256 or self.unload_models is not None or self.free_memory is not None or self.http_status is not None:
                raise ValidationError("节点检查不能携带资源释放事实")
        elif self.kind == self.Kind.RESOURCE_RELEASE:
            if not self.policy_sha256 or self.unload_models is None or self.free_memory is None:
                raise ValidationError("资源释放必须记录策略摘要及实际操作")
            if self.node_count is not None or self.link_count is not None or self.schema_sha256:
                raise ValidationError("资源释放不能携带节点检查事实")
            if self.status == self.Status.PASSED and (self.http_status is None or not 200 <= self.http_status < 300):
                raise ValidationError("资源释放通过必须有成功的HTTP回执")




class AssemblyJob(UUIDTimeStampedModel):
    class Scope(models.TextChoices):
        EPISODE = "episode", "单集"
        SERIES = "series", "整剧"

    class Status(models.TextChoices):
        PLANNED = "planned", "已规划"
        QUEUED = "queued", "排队中"
        RUNNING = "running", "组装中"
        SUCCEEDED = "succeeded", "已完成"
        FAILED = "failed", "已失败"

    owner = models.ForeignKey(settings.AUTH_USER_MODEL, on_delete=models.CASCADE, related_name="assembly_jobs")
    project = models.ForeignKey(SeriesProject, on_delete=models.CASCADE, related_name="assembly_jobs")
    episode = models.ForeignKey(Episode, null=True, blank=True, on_delete=models.CASCADE, related_name="assembly_jobs")
    scope = models.CharField(max_length=16, choices=Scope.choices)
    status = models.CharField(max_length=16, choices=Status.choices, default=Status.PLANNED, db_index=True)
    dry_run = models.BooleanField(default=True)
    has_audio = models.BooleanField(default=False)
    output_path = models.CharField(max_length=1000, blank=True)
    concat_manifest_path = models.CharField(max_length=1000, blank=True)
    ffmpeg_command = models.JSONField(default=list, blank=True)
    source_signature = models.CharField(max_length=64, blank=True, db_index=True)
    source_contract_version = models.PositiveSmallIntegerField(default=5)
    # The consuming assembly step is not the step that generated a source clip.
    # These creation facts survive replacement of the current per-episode link.
    consumer_stage = models.ForeignKey(
        "StageRun", null=True, blank=True, on_delete=models.PROTECT, related_name="assembly_jobs",
    )
    consumer_input_sha256 = models.CharField(max_length=64, blank=True)
    attempts = models.PositiveSmallIntegerField(default=0)
    max_attempts = models.PositiveSmallIntegerField(validators=[MinValueValidator(1)])
    worker_id = models.CharField(max_length=160, blank=True, db_index=True)
    claim_token = models.UUIDField(null=True, blank=True, editable=False, db_index=True)
    error_message = models.TextField(blank=True)
    queued_at = models.DateTimeField(default=timezone.now, db_index=True)
    heartbeat_at = models.DateTimeField(null=True, blank=True, db_index=True)
    started_at = models.DateTimeField(null=True, blank=True)
    finished_at = models.DateTimeField(null=True, blank=True)

    class Meta:
        ordering = ("-created_at",)
        indexes = [models.Index(fields=("status", "queued_at"))]
        constraints = [
            models.CheckConstraint(
                condition=(Q(scope="episode", episode__isnull=False) | Q(scope="series", episode__isnull=True)),
                name="assembly_scope_episode_consistency",
            ),
            models.CheckConstraint(
                condition=(Q(consumer_stage__isnull=True, consumer_input_sha256="")
                    | (Q(consumer_stage__isnull=False) & ~Q(consumer_input_sha256=""))),
                name="assembly_consumer_input_pair",
            )
        ]

    def __str__(self) -> str:
        return f"{self.scope}:{self.project_id} [{self.status}]"


class ReleaseBundle(UUIDTimeStampedModel):
    """Explicit, immutable selection of the artifacts intended for release.

    A project may have many historical generation and assembly records.  A
    release bundle prevents "newest-looking" history from being treated as an
    approved release: every shot output and assembled master is selected by
    durable ID, verified, and sealed into one canonical manifest.
    """

    class Status(models.TextChoices):
        DRAFT = "draft", "草稿"
        SEALED = "sealed", "已封存"
        RELEASED = "released", "已发行"
        SUPERSEDED = "superseded", "已替代"

    owner = models.ForeignKey(
        settings.AUTH_USER_MODEL,
        on_delete=models.PROTECT,
        related_name="release_bundles",
    )
    project = models.ForeignKey(
        SeriesProject,
        on_delete=models.PROTECT,
        related_name="release_bundles",
    )
    production_run = models.OneToOneField(
        "ProductionRun",
        on_delete=models.PROTECT,
        related_name="release_bundle",
    )
    created_by = models.ForeignKey(
        settings.AUTH_USER_MODEL,
        on_delete=models.PROTECT,
        related_name="created_release_bundles",
    )
    version = models.PositiveIntegerField(validators=[MinValueValidator(1)])
    title = models.CharField(max_length=200)
    notes = models.TextField(blank=True)
    status = models.CharField(
        max_length=16,
        choices=Status.choices,
        default=Status.DRAFT,
        db_index=True,
    )
    selection = models.JSONField(default=dict)
    manifest = models.JSONField(default=dict, blank=True)
    manifest_sha256 = models.CharField(max_length=64, blank=True, db_index=True)
    sealed_at = models.DateTimeField(null=True, blank=True)
    released_at = models.DateTimeField(null=True, blank=True)

    class Meta:
        ordering = ("-version", "-created_at")
        constraints = [
            models.UniqueConstraint(
                fields=("project", "version"),
                name="unique_release_bundle_version_per_project",
            ),
            models.UniqueConstraint(
                fields=("project",),
                condition=Q(status="released"),
                name="one_released_bundle_per_project",
            ),
        ]

    def __str__(self) -> str:
        return f"{self.project_id}:release-v{self.version} [{self.status}]"


class ProviderProfile(UUIDTimeStampedModel):
    """Administrative provider endpoint bound to a registered code driver."""

    class DriverType(models.TextChoices):
        LOCAL_QWEN = "local_qwen", "Local Qwen"
        DEEPSEEK_API = "deepseek_api", "DeepSeek API"
        COMFYUI = "comfyui", "ComfyUI"
        OPENAI_COMPATIBLE = "openai_compatible", "OpenAI-compatible"
        ASYNC_REST = "async_rest", "Restricted asynchronous REST"

    key = models.CharField(
        max_length=80,
        unique=True,
        validators=[RegexValidator(r"^[a-z][a-z0-9_.-]{1,79}$")],
    )
    name = models.CharField(max_length=160)
    driver_type = models.CharField(max_length=32, choices=DriverType.choices)
    modalities = models.CharField(max_length=120, help_text="逗号分隔的 text,image,video,audio")
    operations = models.CharField(max_length=500, blank=True, help_text="逗号分隔的受控操作名")
    credential = models.ForeignKey(
        SecretBinding,
        null=True,
        blank=True,
        on_delete=models.PROTECT,
        related_name="provider_profiles",
    )
    driver_config = models.JSONField(default=dict, blank=True)
    driver_config_schema_version = models.PositiveIntegerField(default=1)
    driver_config_size_bytes = models.PositiveIntegerField(default=2)
    driver_config_sha256 = models.CharField(max_length=64)
    is_system = models.BooleanField(default=False)
    is_active = models.BooleanField(default=True)

    class Meta:
        ordering = ("name",)
        indexes = [models.Index(fields=("driver_type", "is_active"))]

    def __str__(self) -> str:
        return self.name


class AssetGenerationJob(UUIDTimeStampedModel):
    """Durable owner-scoped request producing an editable text candidate or Asset.

    Prompt drafting and final asset generation share one history surface but use
    explicit job types.  Provider requests are frozen before a worker can claim
    the row; browser requests never invoke a model directly.
    """

    class JobType(models.TextChoices):
        PROMPT = "prompt", "提示词候选"
        ASSET = "asset", "素材生成"
        FIELD = "field", "准备字段生成"

    class MediaType(models.TextChoices):
        IMAGE = "image", "图片"
        VIDEO = "video", "视频"
        AUDIO = "audio", "音频"
        SCRIPT = "script", "剧本"
        TEXT = "text", "文本候选"

    class ExecutionKind(models.TextChoices):
        DEPLOYMENT = "deployment", "模型部署"
        COMFY_WORKFLOW = "comfy_workflow", "ComfyUI 工作流"

    class Status(models.TextChoices):
        PREPARING = "preparing", "准备请求"
        QUEUED = "queued", "排队中"
        SUBMITTING = "submitting", "正在提交"
        SUBMITTED = "submitted", "已提交"
        RUNNING = "running", "运行中"
        CANCEL_REQUESTED = "cancel_requested", "取消中"
        SUCCEEDED = "succeeded", "已成功"
        FAILED = "failed", "已失败"
        CANCELLED = "cancelled", "已取消"
        UNCERTAIN = "uncertain", "远端状态不确定"

    owner = models.ForeignKey(
        settings.AUTH_USER_MODEL,
        on_delete=models.CASCADE,
        related_name="asset_generation_jobs",
    )
    style = models.ForeignKey(
        Style,
        null=True,
        blank=True,
        on_delete=models.PROTECT,
        related_name="asset_generation_jobs",
    )
    model_deployment = models.ForeignKey(
        "ModelDeployment",
        on_delete=models.PROTECT,
        related_name="asset_generation_jobs",
    )
    execution_instance_version = models.ForeignKey(
        "ModelExecutionInstanceVersion", on_delete=models.PROTECT,
        related_name="asset_generation_jobs",
    )
    parent_preparation = models.ForeignKey(
        "ExecutionPreparation", on_delete=models.PROTECT, null=True, blank=True,
        related_name="auxiliary_asset_jobs", db_index=False,
    )
    retry_of = models.ForeignKey("self", on_delete=models.PROTECT, null=True, blank=True,
        related_name="retries", db_index=False)
    auxiliary_purpose = models.CharField(max_length=24, blank=True)
    field_batch_key = models.CharField(max_length=191, blank=True)
    attempt_number = models.PositiveIntegerField(null=True, blank=True, validators=[MinValueValidator(1)])
    workflow_template_version = models.ForeignKey(
        "WorkflowTemplateVersion",
        null=True,
        blank=True,
        on_delete=models.PROTECT,
        related_name="asset_generation_jobs",
    )
    output_asset = models.OneToOneField(
        Asset,
        null=True,
        blank=True,
        on_delete=models.PROTECT,
        related_name="generation_job",
    )
    job_type = models.CharField(max_length=16, choices=JobType.choices)
    media_type = models.CharField(max_length=16, choices=MediaType.choices)
    execution_kind = models.CharField(
        max_length=24,
        choices=ExecutionKind.choices,
        default=ExecutionKind.DEPLOYMENT,
    )
    image_category = models.CharField(max_length=16, blank=True)
    name = models.CharField(max_length=160, blank=True)
    description = models.TextField(blank=True)
    prompt = models.TextField()
    negative_prompt = models.TextField(blank=True)
    parameters = models.JSONField(default=dict, blank=True)
    status = models.CharField(
        max_length=24,
        choices=Status.choices,
        default=Status.PREPARING,
        db_index=True,
    )
    operation = models.CharField(max_length=100)
    is_external = models.BooleanField(default=False)
    allow_external_calls = models.BooleanField(default=False)
    budget_limit = models.DecimalField(
        max_digits=14,
        decimal_places=6,
        null=True,
        blank=True,
    )
    estimated_cost = models.DecimalField(
        max_digits=14,
        decimal_places=6,
        null=True,
        blank=True,
    )
    actual_cost = models.DecimalField(
        max_digits=14,
        decimal_places=6,
        null=True,
        blank=True,
    )
    cost_currency = models.CharField(max_length=12, blank=True)
    request_snapshot = models.JSONField(default=dict)
    request_snapshot_schema_version = models.PositiveIntegerField(default=1)
    request_snapshot_size_bytes = models.PositiveIntegerField(default=2)
    request_snapshot_sha256 = models.CharField(max_length=64)
    staged_inputs = models.JSONField(default=list, blank=True)
    result_text = models.TextField(blank=True)
    result_text_size_bytes = models.PositiveIntegerField(default=0)
    result_text_sha256 = models.CharField(max_length=64, blank=True)
    provider_request_id = models.CharField(max_length=191, blank=True)
    provider_request_id_sha256 = models.CharField(max_length=64, blank=True)
    idempotency_key = models.CharField(max_length=191, unique=True)
    remote_status = models.CharField(max_length=120, blank=True)
    submission_started_at = models.DateTimeField(null=True, blank=True)
    submitted_at = models.DateTimeField(null=True, blank=True)
    last_polled_at = models.DateTimeField(null=True, blank=True)
    poll_count = models.PositiveIntegerField(default=0)
    cancel_requested_at = models.DateTimeField(null=True, blank=True)
    cancel_submitted_at = models.DateTimeField(null=True, blank=True)
    ignore_late_result = models.BooleanField(default=False)
    next_poll_at = models.DateTimeField(null=True, blank=True, db_index=True)
    claim_token = models.UUIDField(null=True, blank=True, editable=False, db_index=True)
    worker_id = models.CharField(max_length=160, blank=True)
    claimed_at = models.DateTimeField(null=True, blank=True)
    heartbeat_at = models.DateTimeField(null=True, blank=True, db_index=True)
    error_code = models.CharField(max_length=100, blank=True)
    error_message = models.TextField(blank=True)
    started_at = models.DateTimeField(null=True, blank=True)
    finished_at = models.DateTimeField(null=True, blank=True)

    class Meta:
        ordering = ("-created_at",)
        indexes = [
            models.Index(
                fields=("status", "next_poll_at", "created_at"),
                name="studio_asset_job_queue_idx",
            ),
            models.Index(fields=("retry_of",), condition=Q(retry_of__isnull=False), name="idx_asset_retry_source"),
        ]
        constraints = [
            models.CheckConstraint(condition=(
                Q(job_type="field", media_type="text", parent_preparation__isnull=False,
                    auxiliary_purpose__in=("generate_fields", "translate_fields"),
                    attempt_number__isnull=False, attempt_number__gte=1) & ~Q(field_batch_key="")
                | Q(job_type__in=("asset", "prompt"), parent_preparation__isnull=True,
                    auxiliary_purpose="", field_batch_key="", attempt_number__isnull=True) & (Q(job_type="prompt") | ~Q(media_type="text"))
            ), name="ck_asset_job_auxiliary"),
            models.UniqueConstraint(fields=("parent_preparation", "auxiliary_purpose", "field_batch_key", "attempt_number"),
                condition=Q(parent_preparation__isnull=False), name="uk_asset_job_field_attempt"),
            models.CheckConstraint(condition=Q(retry_of__isnull=True) | (
                Q(parent_preparation__isnull=True, job_type__in=("asset", "prompt")) & ~Q(id=F("retry_of"))
            ), name="ck_asset_job_retry_source"),
            models.UniqueConstraint(fields=("retry_of",), condition=Q(retry_of__isnull=False,
                status__in=("preparing", "queued", "submitting", "submitted", "running", "cancel_requested", "uncertain")),
                name="uk_asset_job_active_retry"),
            models.CheckConstraint(
                condition=(
                    Q(
                        execution_kind="deployment",
                        workflow_template_version__isnull=True,
                    )
                    | Q(
                        execution_kind="comfy_workflow",
                        workflow_template_version__isnull=False,
                    )
                ),
                name="asset_job_execution_target_consistent",
            ),
            models.CheckConstraint(
                condition=(
                    Q(budget_limit__isnull=True) | Q(budget_limit__gte=0)
                ),
                name="asset_job_budget_nonnegative",
            ),
            models.CheckConstraint(
                condition=(
                    Q(estimated_cost__isnull=True) | Q(estimated_cost__gte=0)
                ) & (
                    Q(actual_cost__isnull=True) | Q(actual_cost__gte=0)
                ),
                name="asset_job_costs_nonnegative",
            ),
        ]

    def __str__(self) -> str:
        return f"{self.job_type}:{self.media_type}:{self.id} [{self.status}]"

    def clean(self):
        super().clean()
        from studio.services.execution_task_links import validate_execution_task_links
        validate_execution_task_links(self)


class AppendOnlyAssetGenerationResolutionQuerySet(models.QuerySet):
    def update(self, **kwargs):
        raise ValidationError("AssetGenerationJobResolution 不允许更新")

    def delete(self):
        raise ValidationError("AssetGenerationJobResolution 不允许删除")


class AppendOnlyAssetGenerationResolutionManager(
    models.Manager.from_queryset(AppendOnlyAssetGenerationResolutionQuerySet),
):
    pass


class AssetGenerationJobResolution(UUIDTimeStampedModel):
    """Append-only system observations and staff resolutions for one asset job."""

    class Outcome(models.TextChoices):
        HTTP_BODY_PREPARED = "http_body_prepared", "HTTP正文已序列化（未证明送达）"
        UNCERTAIN_RETRY_AUTHORIZED = "uncertain_retry_authorized", "保留原未决调用并授权新辅助尝试"
        RESPONSE_RECEIVED = "response_received", "文本响应已返回（非扣费或采用证明）"
        CONFIRMED_NOT_SUBMITTED = "confirmed_not_submitted", "确认未提交"
        CONFIRMED_REMOTE_FAILED = "confirmed_remote_failed", "确认远端失败"
        CONFIRMED_REMOTE_CANCELLED = "confirmed_remote_cancelled", "确认远端取消"
        CONFIRMED_REMOTE_SUCCEEDED = "confirmed_remote_succeeded", "确认远端成功"

    OPERATOR_OUTCOMES = (
        Outcome.CONFIRMED_NOT_SUBMITTED, Outcome.CONFIRMED_REMOTE_FAILED,
        Outcome.CONFIRMED_REMOTE_CANCELLED, Outcome.CONFIRMED_REMOTE_SUCCEEDED,
    )
    OPERATOR_CHOICES = tuple((value, value.label) for value in OPERATOR_OUTCOMES)

    job = models.ForeignKey(
        AssetGenerationJob,
        on_delete=models.PROTECT,
        related_name="resolutions",
    )
    revision = models.PositiveIntegerField(validators=[MinValueValidator(1)])
    outcome = models.CharField(max_length=40, choices=Outcome.choices)
    idempotency_key = models.CharField(max_length=191)
    request_sha256 = models.CharField(max_length=64)
    provider_request_id_sha256 = models.CharField(max_length=64, blank=True)
    evidence = models.JSONField(default=dict)
    evidence_schema_version = models.PositiveIntegerField(default=1)
    evidence_size_bytes = models.PositiveIntegerField(default=2)
    evidence_sha256 = models.CharField(max_length=64)
    note = models.TextField(blank=True)
    decided_by = models.ForeignKey(
        settings.AUTH_USER_MODEL,
        on_delete=models.PROTECT,
        related_name="asset_generation_job_resolutions",
        null=True,
        blank=True,
    )
    objects = AppendOnlyAssetGenerationResolutionManager()

    class Meta:
        ordering = ("job", "revision")
        constraints = [
            models.UniqueConstraint(
                fields=("job", "revision"),
                name="unique_asset_job_resolution",
            ),
            models.UniqueConstraint(
                fields=("job", "idempotency_key"),
                name="unique_asset_job_resolution_key",
            ),
        ]

    def save(self, *args, **kwargs):
        if not self._state.adding:
            raise ValidationError("AssetGenerationJobResolution 是不可变审计事实，不能修改")
        return super().save(*args, **kwargs)

    def delete(self, *args, **kwargs):
        raise ValidationError("AssetGenerationJobResolution 是不可变审计事实，不能删除")


class ModelProfileVersion(UUIDTimeStampedModel):
    """Immutable-after-publish model capability and parameter contract."""

    class Modality(models.TextChoices):
        TEXT = "text", "文本"
        IMAGE = "image", "图片"
        VIDEO = "video", "视频"
        AUDIO = "audio", "音频"
        MULTIMODAL = "multimodal", "多模态"

    class Status(models.TextChoices):
        DRAFT = "draft", "草稿"
        PUBLISHED = "published", "已发布"
        RETIRED = "retired", "已停用"

    profile_key = models.CharField(
        max_length=100,
        validators=[RegexValidator(r"^[a-z][a-z0-9_.-]{1,99}$")],
    )
    version = models.PositiveIntegerField(validators=[MinValueValidator(1)])
    provider = models.ForeignKey(
        ProviderProfile,
        on_delete=models.PROTECT,
        related_name="model_profile_versions",
    )
    model_identifier = models.CharField(max_length=255)
    modality = models.CharField(max_length=16, choices=Modality.choices)
    specification = models.JSONField(default=dict)
    specification_schema_version = models.PositiveIntegerField(default=1)
    specification_size_bytes = models.PositiveIntegerField(default=2)
    specification_sha256 = models.CharField(max_length=64)
    status = models.CharField(max_length=16, choices=Status.choices, default=Status.DRAFT)
    published_at = models.DateTimeField(null=True, blank=True)
    published_by = models.ForeignKey(
        settings.AUTH_USER_MODEL,
        null=True,
        blank=True,
        on_delete=models.PROTECT,
        related_name="published_model_profile_versions",
    )

    class Meta:
        ordering = ("profile_key", "-version")
        constraints = [
            models.UniqueConstraint(
                fields=("profile_key", "version"),
                name="unique_model_profile_version",
            ),
        ]
        indexes = [models.Index(fields=("modality", "status"))]

    def __str__(self) -> str:
        return f"{self.profile_key} v{self.version}"


class FieldDefinition(UUIDTimeStampedModel):
    """Stable identity shared by configurable business fields."""

    key = models.CharField(
        max_length=100,
        unique=True,
        validators=[RegexValidator(r"^[a-z][a-z0-9_.-]{1,99}$")],
    )
    is_active = models.BooleanField(default=True)
    display_order = models.PositiveIntegerField(default=0)

    class Meta:
        ordering = ("display_order", "key")
        indexes = [models.Index(fields=("is_active", "display_order", "key"), name="idx_field_active_order")]

    def __str__(self) -> str:
        return self.key

    def save(self, *args, **kwargs):
        if not self._state.adding:
            original_key = type(self).objects.values_list("key", flat=True).get(pk=self.pk)
            if self.key != original_key:
                raise ValidationError({"key": "字段身份key不可修改；显示名可在新版本中修改"})
        return super().save(*args, **kwargs)


class FieldDefinitionVersion(UUIDTimeStampedModel):
    """Field meaning, without actual values or per-use required/default rules."""

    class Status(models.TextChoices):
        DRAFT = "draft", "草稿"
        PUBLISHED = "published", "可用"
        RETIRED = "retired", "停用"

    class ValueType(models.TextChoices):
        TEXT = "text", "短文本"
        LONG_TEXT = "long_text", "长文本"
        INTEGER = "integer", "整数"
        DECIMAL = "decimal", "小数"
        BOOLEAN = "boolean", "布尔值"
        ENUM = "enum", "枚举"
        OBJECT = "object", "对象"
        LIST = "list", "列表"
        MEDIA = "media", "媒体引用"

    field_definition = models.ForeignKey(
        FieldDefinition, on_delete=models.CASCADE, related_name="definition_versions",
    )
    version = models.PositiveIntegerField(validators=[MinValueValidator(1)])
    status = models.CharField(max_length=12, choices=Status.choices, default=Status.DRAFT)
    label = models.CharField(max_length=160, blank=True)
    category = models.CharField(
        max_length=80, blank=True,
        validators=[RegexValidator(r"^[a-z][a-z0-9_.-]{1,79}$")],
    )
    description = models.TextField(blank=True)
    impact = models.TextField(blank=True)
    value_type = models.CharField(max_length=24, blank=True, choices=ValueType.choices)
    unit = models.CharField(max_length=80, blank=True)
    value_options = models.JSONField(default=list, blank=True)

    class Meta:
        ordering = ("field_definition_id", "-version")
        constraints = [
            models.UniqueConstraint(fields=("field_definition", "version"), name="uk_field_definition_version"),
            models.CheckConstraint(condition=Q(version__gte=1), name="ck_field_definition_version_pos"),
        ]

    @property
    def key(self):
        return self.field_definition.key

    def clean(self):
        super().clean()
        if self.status != self.Status.DRAFT:
            errors = {
                name: "可用字段版本必须填写此项"
                for name in ("label", "category", "value_type")
                if not getattr(self, name).strip()
            }
            if errors:
                raise ValidationError(errors)
        options = self.value_options
        if not isinstance(options, list) or any(
            not isinstance(option, str) or not option.strip() for option in options
        ):
            raise ValidationError({"value_options": "枚举选项必须是非空文本数组"})
        if len(options) != len(set(options)):
            raise ValidationError({"value_options": "枚举选项不能重复"})
        if self.value_type != self.ValueType.ENUM and options:
            raise ValidationError({"value_options": "只有枚举字段可以设置枚举选项"})
        if self.value_type == self.ValueType.ENUM and not options and self.status != self.Status.DRAFT:
            raise ValidationError({"value_options": "可用枚举字段必须设置枚举选项"})

    def save(self, *args, **kwargs):
        if not self._state.adding:
            previous = type(self).objects.get(pk=self.pk)
            if (self.field_definition_id, self.version) != (previous.field_definition_id, previous.version):
                raise ValidationError("字段版本身份不可修改")
            if previous.status != self.Status.DRAFT:
                content_fields = ("label", "category", "description", "impact", "value_type", "unit", "value_options")
                if any(getattr(self, name) != getattr(previous, name) for name in content_fields):
                    raise ValidationError("可用或停用字段版本不可修改正文；请创建新版本")
                if self.status not in {previous.status, self.Status.RETIRED}:
                    raise ValidationError("已发布字段版本不能改回草稿或重新启用")
        self.full_clean()
        return super().save(*args, **kwargs)


class DataStructureVersion(UUIDTimeStampedModel):
    """One versioned shape shared by business inputs and outputs."""

    structure_key = models.UUIDField(default=uuid.uuid4, editable=False)
    version = models.PositiveIntegerField(default=1, validators=[MinValueValidator(1)])
    name = models.CharField(max_length=160, blank=True)
    description = models.TextField(blank=True)
    status = models.CharField(max_length=12, choices=FieldDefinitionVersion.Status.choices, default="draft")

    class Meta:
        ordering = ("structure_key", "-version", "id")
        indexes = [models.Index(fields=("structure_key", "-version", "id"), name="idx_structure_version_order")]
        constraints = [
            models.UniqueConstraint(fields=("structure_key", "version"), name="uk_data_structure_version"),
            models.CheckConstraint(condition=Q(version__gte=1), name="ck_data_structure_version_pos"),
        ]

    def save(self, *args, **kwargs):
        if not self._state.adding:
            previous = type(self).objects.get(pk=self.pk)
            if (self.structure_key, self.version) != (previous.structure_key, previous.version):
                raise ValidationError("结构版本身份不可修改")
            if previous.status != "draft":
                if (self.name, self.description) != (previous.name, previous.description):
                    raise ValidationError("可用或停用结构不可修改；请创建新版本")
                if self.status not in {previous.status, "retired"}:
                    raise ValidationError("已发布结构不能返回草稿或重新启用")
        if self.status != "draft" and not self.name.strip():
            raise ValidationError({"name": "可用结构必须填写名称"})
        if self.status == "published" and (self._state.adding or previous.status == "draft"):
            from studio.services.data_structures import compile_data_structure
            compile_data_structure(self, require_published_fields=True)
        self.full_clean()
        return super().save(*args, **kwargs)


class DataStructureNode(UUIDTimeStampedModel):
    """Field usage in a structure, not a second field dictionary or task value."""

    structure = models.ForeignKey(DataStructureVersion, on_delete=models.CASCADE, related_name="nodes")
    node_key = models.CharField(max_length=100, validators=[RegexValidator(r"^[a-z][a-z0-9_.-]{0,99}$")])
    parent = models.ForeignKey("self", null=True, blank=True, on_delete=models.CASCADE, related_name="children")
    field_definition_version = models.ForeignKey(
        FieldDefinitionVersion, null=True, blank=True, on_delete=models.PROTECT, related_name="structure_nodes",
    )
    property_name = models.CharField(max_length=160, blank=True)
    position = models.PositiveIntegerField(default=0)
    required = models.BooleanField(default=False)
    allow_null = models.BooleanField(default=False)
    allow_blank = models.BooleanField(default=True)
    allow_empty = models.BooleanField(default=True)
    has_default = models.BooleanField(default=False)
    default_value = models.JSONField(null=True, blank=True)
    constraints = models.JSONField(default=dict, blank=True)

    class Meta:
        ordering = ("position", "node_key")
        constraints = [models.UniqueConstraint(fields=("structure", "node_key"), name="uk_structure_node_key")]

    def save(self, *args, **kwargs):
        from django.db import transaction
        with transaction.atomic():
            ids = {self.structure_id}
            if not self._state.adding:
                ids.add(type(self).objects.get(pk=self.pk).structure_id)
            versions = DataStructureVersion.objects.select_for_update().filter(pk__in=ids)
            if any(version.status != "draft" for version in versions):
                raise ValidationError("可用或停用结构的节点不可修改")
            if self.parent_id and self.parent.structure_id != self.structure_id:
                raise ValidationError({"parent": "父节点必须属于同一结构版本"})
            self.full_clean()
            return super().save(*args, **kwargs)

    def delete(self, *args, **kwargs):
        from django.db import transaction
        with transaction.atomic():
            structure = DataStructureVersion.objects.select_for_update().get(pk=self.structure_id)
            if structure.status != "draft":
                raise ValidationError("可用或停用结构的节点不可删除")
            return super().delete(*args, **kwargs)


class PromptProfileVersion(UUIDTimeStampedModel):
    """Immutable-after-publish prompt assembly strategy."""

    class Status(models.TextChoices):
        DRAFT = "draft", "草稿"
        PUBLISHED = "published", "已发布"
        RETIRED = "retired", "已停用"

    class ReviewState(models.TextChoices):
        NEEDS_REVIEW = "needs_review", "待审核"
        READY = "ready", "可发布"

    profile_key = models.CharField(
        max_length=100,
        validators=[RegexValidator(r"^[a-z][a-z0-9_.-]{1,99}$")],
    )
    version = models.PositiveIntegerField(validators=[MinValueValidator(1)])
    name = models.CharField(max_length=160)
    description = models.TextField(blank=True)
    model_family = models.CharField(
        max_length=100,
        validators=[RegexValidator(r"^[a-z][a-z0-9_.-]{1,99}$")],
    )
    operation = models.CharField(
        max_length=100,
        validators=[RegexValidator(r"^[a-z][a-z0-9_.-]{1,99}$")],
    )
    mode = models.CharField(
        max_length=40,
        blank=True,
        validators=[RegexValidator(r"^[A-Za-z0-9][A-Za-z0-9_.-]{0,39}$")],
    )
    source_projection_key = models.CharField(
        max_length=64,
        default="direct_v1",
        validators=[RegexValidator(r"^[a-z][a-z0-9_.-]{1,63}$")],
        help_text="受控确定性来源投影器；普通策略使用 direct_v1。",
    )
    source_projection_config = models.JSONField(
        default=dict,
        blank=True,
        help_text=(
            "来源投影器的版本化数据合同；只接受登记的声明式配置与简单占位符模板，不接受表达式、函数或自定义正则。"
        ),
    )
    official_reference = models.URLField(max_length=1000, blank=True)
    input_structure_version = models.ForeignKey(
        DataStructureVersion, null=True, blank=True, on_delete=models.PROTECT,
        related_name="prompt_profiles",
    )
    channel_limits = models.JSONField(
        default=dict,
        blank=True,
        help_text=(
            "按输出通道配置最终提示词长度合同；键为受控 channel，值包含 "
            "max_chars 与可选 warn_at_chars。"
        ),
    )
    status = models.CharField(max_length=16, choices=Status.choices, default=Status.DRAFT)
    review_state = models.CharField(
        max_length=20,
        choices=ReviewState.choices,
        default=ReviewState.NEEDS_REVIEW,
    )
    contract_schema_version = models.PositiveIntegerField(default=8)
    contract_size_bytes = models.PositiveIntegerField(default=2)
    contract_sha256 = models.CharField(max_length=64, blank=True)
    published_at = models.DateTimeField(null=True, blank=True)
    published_by = models.ForeignKey(
        settings.AUTH_USER_MODEL,
        null=True,
        blank=True,
        on_delete=models.PROTECT,
        related_name="published_prompt_profile_versions",
    )

    class Meta:
        ordering = ("profile_key", "-version")
        constraints = [
            models.UniqueConstraint(
                fields=("profile_key", "version"),
                name="unique_prompt_profile_version",
            ),
        ]
        indexes = [models.Index(fields=("operation", "model_family", "status"))]

    def __str__(self) -> str:
        return f"{self.name} v{self.version}"


class BusinessOutputVersion(UUIDTimeStampedModel):
    """An immutable, authored contract consumed by the business output parser."""

    output_key = models.UUIDField(default=uuid.uuid4, editable=False)
    version = models.PositiveIntegerField(default=1)
    name = models.CharField(max_length=120)
    description = models.CharField(max_length=1000, blank=True)
    parser_key = models.CharField(max_length=80)
    parser_contract = models.JSONField(default=dict, editable=False)
    data_structure_version = models.ForeignKey(
        DataStructureVersion, null=True, blank=True, on_delete=models.PROTECT,
        related_name="business_output_versions",
    )
    quality_advisories = models.JSONField(default=dict, blank=True)
    status = models.CharField(max_length=16, choices=(("draft", "草稿"), ("published", "已发布"), ("retired", "已退役")), default="draft")
    revision = models.PositiveIntegerField(default=1)
    contract_sha256 = models.CharField(max_length=64)
    contract_size_bytes = models.PositiveIntegerField()
    published_at = models.DateTimeField(null=True, blank=True)
    published_by = models.ForeignKey(settings.AUTH_USER_MODEL, null=True, blank=True,
        on_delete=models.PROTECT, related_name="published_business_outputs")

    class Meta:
        ordering = ("-created_at", "id")
        constraints = [models.UniqueConstraint(fields=("output_key", "version"), name="studio_output_version_unique")]
        indexes = [models.Index(fields=("status", "-created_at", "id"), name="studio_output_status_idx")]


class ContentSourceVersion(UUIDTimeStampedModel):
    """An authored meaning bound to one platform-owned, permission-aware reader."""

    class Status(models.TextChoices):
        DRAFT = "draft", "草稿"
        PUBLISHED = "published", "已发布"
        RETIRED = "retired", "已退役"

    source_key = models.UUIDField(default=uuid.uuid4, editable=False)
    version = models.PositiveIntegerField(default=1)
    name = models.CharField(max_length=120)
    description = models.CharField(max_length=1000, blank=True)
    reader_selector = models.CharField(max_length=80)
    reader_contract = models.JSONField(default=dict, editable=False)
    data_structure_version = models.ForeignKey(
        DataStructureVersion, null=True, blank=True, on_delete=models.PROTECT,
        related_name="content_source_versions",
    )
    status = models.CharField(max_length=16, choices=Status.choices, default=Status.DRAFT)
    revision = models.PositiveIntegerField(default=1)
    contract_sha256 = models.CharField(max_length=64)
    contract_size_bytes = models.PositiveIntegerField()
    published_at = models.DateTimeField(null=True, blank=True)
    published_by = models.ForeignKey(settings.AUTH_USER_MODEL, null=True, blank=True,
        on_delete=models.PROTECT, related_name="published_content_sources")

    class Meta:
        ordering = ("-created_at", "id")
        constraints = [models.UniqueConstraint(fields=("source_key", "version"), name="studio_source_version_unique")]
        indexes = [models.Index(fields=("status", "-created_at", "id"), name="studio_source_status_idx")]


class PromptProfileFieldRule(UUIDTimeStampedModel):
    """One allow-listed source-to-channel mapping in a prompt profile."""

    class GenerationMode(models.TextChoices):
        SERVER_LOCKED = "server_locked", "服务端锁定 / Server locked"
        MODEL_GENERATED = "model_generated", "模型生成 / Model generated"
        TRANSLATE_ONLY = "translate_only", "仅翻译 / Translate only"
        PASSTHROUGH = "passthrough", "原值透传 / Passthrough"

    class GenerationOutputSemantics(models.TextChoices):
        FINAL = "final", "最终字段值 / Final field value"
        CONTRIBUTION = "contribution", "确定性投影贡献 / Projection contribution"

    class LengthUnit(models.TextChoices):
        NONE = "none", "不限制 / None"
        CHARACTERS = "characters", "字符数 / Characters"
        ENGLISH_WORDS = "english_words", "英文词数 / English words"
        ITEMS = "items", "列表项数 / Items"

    class GenerationValidator(models.TextChoices):
        NON_EMPTY = "non_empty", "非空 / Non-empty"
        NO_CONTROL_CHARACTERS = (
            "no_control_characters",
            "禁止控制字符 / No control characters",
        )
        NO_UNRESOLVED_PLACEHOLDERS = (
            "no_unresolved_placeholders",
            "禁止未解析占位符 / No unresolved placeholders",
        )
        SINGLE_LINE = "single_line", "仅单行 / Single line"
        NO_SHOT_HEADERS = "no_shot_headers", "不重复生成镜头标题 / No shot headers"
        VALID_JSON = "valid_json", "合法 JSON / Valid JSON"
        JSON_OBJECT = "json_object", "JSON 对象 / JSON object"
        JSON_ARRAY = "json_array", "JSON 数组 / JSON array"
        UNIQUE_ITEMS = "unique_items", "列表项唯一 / Unique items"
        LENGTH_ADVISORY = (
            "length_advisory",
            "长度仅建议 / Advisory length",
        )
        PROTECTED_TOKENS_PRESERVE_COUNT = (
            "protected_tokens_preserve_count",
            "受保护内容保持来源次数 / Preserve protected-token source count",
        )
        PROTECTED_TOKENS_AT_LEAST_ONCE = (
            "protected_tokens_at_least_once",
            "受保护内容至少出现一次 / Protected tokens at least once",
        )

    class GenerationContextSelector(models.TextChoices):
        PROJECT_CORE = "project.core", "项目核心事实 / Project core facts"
        PROJECT_STYLE = "project.style", "项目视觉风格 / Project visual style"
        TARGET_SHOT_CORE = (
            "target_shot.core",
            "当前镜头核心事实 / Target-shot core facts",
        )
        TARGET_SHOT_DIALOGUE_METADATA = (
            "target_shot.dialogue_metadata",
            "当前镜头对白元数据 / Target-shot dialogue metadata",
        )
        TARGET_SHOT_DIALOGUE_AUTHORED_TEXT = (
            "target_shot.dialogue_authored_text",
            "当前镜头对白正文 / Target-shot authored dialogue text",
        )
        TARGET_SHOT_LINKED_BIBLE = (
            "target_shot.linked_bible",
            "当前镜头关联 Bible / Target-shot linked Bible",
        )
        ADJACENT_SHOTS_CORE = (
            "adjacent_shots.core",
            "相邻镜头核心事实 / Adjacent-shot core facts",
        )
        ALL_SHOTS_CORE = (
            "all_shots.core",
            "全剧镜头核心事实 / All-shot core facts",
        )

    class SourceSelector(models.TextChoices):
        FIXED_VALUE = "fixed.value", "本规则固定值"
        REQUEST_STRUCTURED_INPUT = "request.structured_input", "本次结构化输入字段"
        PROJECTION_OUTPUT = "projection.output", "声明式投影输出"
        PROJECT_TITLE = "project.title", "项目名称"
        PROJECT_LOGLINE = "project.logline", "一句话梗概"
        PROJECT_SCRIPT = "project.master_script", "项目剧本"
        PROJECT_LANGUAGE = "project.language", "项目语言"
        PROJECT_ASPECT_RATIO = "project.aspect_ratio", "项目画幅"
        STYLE_NAME = "style.name", "风格名称"
        STYLE_DESCRIPTION = "style.description", "风格说明"
        STYLE_VISUAL_PROMPT = "style.visual_prompt", "风格视觉提示"
        STYLE_NEGATIVE_PROMPT = "style.negative_prompt", "风格负面提示"
        REVIEWED_VISUAL_CONTINUITY = "reviewed_visual_rules.continuity_summary", "已审核连续性说明"
        REVIEWED_VISUAL_COLOR = "reviewed_visual_rules.color_script", "已审核色彩脚本"
        REVIEWED_VISUAL_NEGATIVE = "reviewed_visual_rules.negative_prompt", "已审核负面约束"
        REVIEWED_VISUAL_CAMERA = "reviewed_visual_rules.camera_guidance", "已审核镜头指导"
        SHOT_TITLE = "shot.title", "镜头标题"
        SHOT_DIALOGUE = "shot.dialogue", "对白"
        REQUEST_THEME = "request.theme", "请求主题"
        REQUEST_LIGHTING = "request.lighting", "请求灯光"
        REQUEST_ENVIRONMENT = "request.environment", "请求环境"
        REQUEST_ACTION = "request.action", "请求动作"
        REQUEST_CAMERA = "request.camera", "请求镜头"
        REQUEST_STYLE = "request.style", "请求风格"
        REQUEST_LANGUAGE = "request.language", "请求语言"
        REQUEST_TEXT = "request.text", "请求正文"
        REQUEST_BGM_STYLE = "request.bgm_style", "背景音乐风格"
        REQUEST_DIALOGUE_DELIVERY = "request.dialogue_delivery", "对白语调"
        REQUEST_NEGATIVE = "request.negative_constraints", "负面约束"
        REQUEST_SUBJECT = "request.subject", "主体"
        REQUEST_COMPOSITION = "request.composition", "构图"
        REQUEST_COLOR = "request.color_palette", "色彩"
        REQUEST_MOTION = "request.motion", "运动"
        REQUEST_DURATION = "request.duration", "时长"
        REQUEST_ASPECT_RATIO = "request.aspect_ratio", "画幅"
        REQUEST_SOUND_EFFECTS = "request.sound_effects", "音效"
        REQUEST_VOICEOVER = "request.voiceover", "旁白"
        REQUEST_REFERENCE = "request.reference_assets", "参考素材"
        H3_REF2VA_SUBJECT_DEFINITIONS = (
            "request.h3_ref2va.subject_definitions",
            "H3 Ref2VA 主体定义",
        )
        H3_REF2VA_SUMMARY = "request.h3_ref2va.summary", "H3 Ref2VA 任务摘要"
        H3_REF2VA_RETENTION_ANALYSIS = (
            "request.h3_ref2va.retention_analysis",
            "H3 Ref2VA 保留关系",
        )
        H3_REF2VA_DETAILED_DESCRIPTION = (
            "request.h3_ref2va.detailed_description",
            "H3 Ref2VA 导演描述",
        )
        H3_REF2VA_OVERALL_SOUNDSCAPE = (
            "request.h3_ref2va.overall_soundscape",
            "H3 Ref2VA 总体声景",
        )
        H3_REF2VA_NON_DIEGETIC_MUSIC = (
            "request.h3_ref2va.non_diegetic_music",
            "H3 Ref2VA 非画内音乐",
        )

    class Channel(models.TextChoices):
        SYSTEM_INSTRUCTION = "system_instruction", "系统约束"
        POSITIVE_PROMPT = "positive_prompt", "正向提示"
        NEGATIVE_PROMPT = "negative_prompt", "负向提示"
        STRUCTURED_INPUT = "structured_input", "结构化字段"

    class LabelStyle(models.TextChoices):
        NONE = "none", "不显示字段名"
        LABEL_COLON = "label_colon", "字段名加冒号"
        LABEL_BLOCK = "label_block", "字段名独占一行"
        BRACKETED = "bracketed", "方括号字段名"

    class Separator(models.TextChoices):
        NEWLINE = "newline", "换行"
        COMMA = "comma", "逗号"
        SEMICOLON = "semicolon", "分号"
        SPACE = "space", "空格"

    profile_version = models.ForeignKey(
        PromptProfileVersion,
        on_delete=models.CASCADE,
        related_name="field_rules",
    )
    field_definition = models.ForeignKey(
        FieldDefinition,
        on_delete=models.PROTECT,
        related_name="profile_rules",
    )
    field_definition_version = models.ForeignKey(
        FieldDefinitionVersion, null=True, blank=True,
        on_delete=models.PROTECT, related_name="profile_rules",
    )
    source_version = models.ForeignKey(ContentSourceVersion, null=True, blank=True,
        on_delete=models.PROTECT, related_name="profile_rules")
    source_selector = models.CharField(max_length=80, choices=SourceSelector.choices)
    source_path = models.JSONField(null=True, blank=True, default=None)
    projection_primitive = models.CharField(
        max_length=80,
        blank=True,
        default="",
        help_text=(
            "source_selector=projection.output 时必填；只能选择服务端登记的"
            "确定性原语，不接受模板、函数或脚本。"
        ),
    )
    channel = models.CharField(max_length=32, choices=Channel.choices)
    position = models.PositiveIntegerField(validators=[MinValueValidator(1)])
    label_style = models.CharField(
        max_length=20,
        choices=LabelStyle.choices,
        default=LabelStyle.LABEL_COLON,
    )
    separator = models.CharField(
        max_length=16,
        choices=Separator.choices,
        default=Separator.NEWLINE,
    )
    required = models.BooleanField(default=False)
    omit_if_empty = models.BooleanField(default=True)
    fixed_value = models.JSONField(null=True, blank=True, default=None)
    has_default = models.BooleanField(default=False)
    default_value = models.JSONField(null=True, blank=True, default=None)
    max_chars = models.PositiveIntegerField(
        default=2000,
        validators=[MinValueValidator(1)],
    )
    generation_mode = models.CharField(
        max_length=24,
        choices=GenerationMode.choices,
        default=GenerationMode.PASSTHROUGH,
    )
    generation_source_selector = models.CharField(
        max_length=191,
        blank=True,
        default="",
        help_text=(
            "受控点路径；历史 schema v1 可为空，新建 schema v2 规则必须显式填写。"
        ),
    )
    generation_instruction = models.TextField(
        blank=True,
        default="",
        help_text=(
            "版本化、不可执行的字段生成说明；禁止模板、脚本和动态表达式。"
        ),
    )
    generation_output_semantics = models.CharField(
        max_length=16,
        choices=GenerationOutputSemantics.choices,
        default=GenerationOutputSemantics.FINAL,
    )
    generation_required = models.BooleanField(default=False)
    language_policy = models.CharField(
        max_length=64,
        default="inherit",
        help_text=(
            "inherit、preserve_source、any、BCP 47 语言标签，或 "
            "bilingual:<tag>+<tag>。"
        ),
    )
    length_unit = models.CharField(
        max_length=24,
        choices=LengthUnit.choices,
        default=LengthUnit.NONE,
    )
    generation_min = models.PositiveIntegerField(
        null=True,
        blank=True,
        validators=[MinValueValidator(1)],
    )
    generation_max = models.PositiveIntegerField(
        null=True,
        blank=True,
        validators=[MinValueValidator(1)],
    )
    generation_validator_keys = models.JSONField(default=list, blank=True)
    generation_context_selectors = models.JSONField(
        default=list,
        blank=True,
        help_text=(
            "字段生成可读取的服务端投影事实范围；空数组表示仅可读取当前 item/source。"
        ),
    )

    class Meta:
        ordering = ("profile_version", "position", "id")
        constraints = [
            models.UniqueConstraint(
                fields=("profile_version", "field_definition"),
                name="unique_prompt_profile_field",
            ),
            models.UniqueConstraint(
                fields=("profile_version", "position"),
                name="unique_prompt_profile_position",
            ),
        ]




class RuntimeHost(UUIDTimeStampedModel):
    class Status(models.TextChoices):
        ACTIVE = "active", "在线"
        UNAVAILABLE = "unavailable", "不可用"
        DISABLED = "disabled", "已禁用"

    key = models.CharField(
        max_length=80,
        unique=True,
        validators=[RegexValidator(r"^[a-z][a-z0-9_.-]{1,79}$")],
    )
    name = models.CharField(max_length=160)
    fingerprint_sha256 = models.CharField(max_length=64, blank=True)
    status = models.CharField(max_length=16, choices=Status.choices, default=Status.ACTIVE)
    last_seen_at = models.DateTimeField(null=True, blank=True)

    class Meta:
        ordering = ("key",)

    def __str__(self) -> str:
        return self.name


class RuntimeHostProbeAttempt(UUIDTimeStampedModel):
    """Durable background execution of one runtime-host capability probe."""

    class Status(models.TextChoices):
        QUEUED = "queued", "排队中"
        RUNNING = "running", "执行中"
        CANCEL_REQUESTED = "cancel_requested", "取消中"
        SUCCEEDED = "succeeded", "成功"
        FAILED = "failed", "失败"
        CANCELLED = "cancelled", "已取消"

    class Trigger(models.TextChoices):
        MANUAL = "manual", "管理员手动"
        SCHEDULED = "scheduled", "平台维护"

    class ProbeKind(models.TextChoices):
        CAPABILITY = "capability", "主机能力"
        DRAFT_MODEL_ARTIFACT_ATTESTATION = (
            "draft_model_artifact_attestation",
            "草稿模型文件证明",
        )

    host = models.ForeignKey(
        RuntimeHost,
        on_delete=models.PROTECT,
        related_name="probe_attempts",
    )
    created_by = models.ForeignKey(
        settings.AUTH_USER_MODEL,
        null=True,
        blank=True,
        on_delete=models.PROTECT,
        related_name="runtime_host_probe_attempts",
    )
    trigger = models.CharField(
        max_length=16,
        choices=Trigger.choices,
        default=Trigger.MANUAL,
    )
    probe_kind = models.CharField(
        max_length=48,
        choices=ProbeKind.choices,
        default=ProbeKind.CAPABILITY,
        db_index=True,
    )
    request_snapshot = models.JSONField(default=dict, blank=True)
    request_snapshot_schema_version = models.PositiveIntegerField(default=1)
    request_snapshot_size_bytes = models.PositiveIntegerField(default=2)
    request_snapshot_sha256 = models.CharField(
        max_length=64,
        default="44136fa355b3678a1146ad16f7e8649e94fb4fc21fe77e8310c060f61caaff8a",
    )
    status = models.CharField(
        max_length=24,
        choices=Status.choices,
        default=Status.QUEUED,
        db_index=True,
    )
    request_token = models.UUIDField(default=uuid.uuid4, editable=False, unique=True)
    claim_token = models.UUIDField(null=True, blank=True, editable=False, db_index=True)
    worker_id = models.CharField(max_length=160, blank=True)
    claimed_at = models.DateTimeField(null=True, blank=True)
    heartbeat_at = models.DateTimeField(null=True, blank=True)
    started_at = models.DateTimeField(null=True, blank=True)
    progress_marker = models.CharField(max_length=191, default="phase:queued")
    cancel_requested_at = models.DateTimeField(null=True, blank=True)
    finished_at = models.DateTimeField(null=True, blank=True)
    error_code = models.CharField(max_length=100, blank=True)
    error_message = models.TextField(blank=True)

    class Meta:
        ordering = ("-created_at",)
        constraints = [
            models.UniqueConstraint(
                fields=("host",),
                condition=Q(status__in=("queued", "running", "cancel_requested")),
                name="one_active_runtime_host_probe",
            ),
        ]
        indexes = [
            models.Index(
                fields=("host", "status", "-created_at"),
                name="studio_hp_host_st_39d0_idx",
            ),
        ]


class ModelDeployment(UUIDTimeStampedModel):
    class Status(models.TextChoices):
        ACTIVE = "active", "启用"
        DISABLED = "disabled", "禁用"
        ERROR = "error", "异常"
        RETIRED = "retired", "已退役"

    key = models.CharField(
        max_length=100,
        unique=True,
        validators=[RegexValidator(r"^[a-z][a-z0-9_.-]{1,99}$")],
    )
    name = models.CharField(max_length=160)
    profile_version = models.ForeignKey(
        ModelProfileVersion,
        on_delete=models.PROTECT,
        related_name="deployments",
    )
    runtime_host = models.ForeignKey(
        RuntimeHost,
        null=True,
        blank=True,
        on_delete=models.PROTECT,
        related_name="model_deployments",
    )
    credential = models.ForeignKey(
        SecretBinding,
        null=True,
        blank=True,
        on_delete=models.PROTECT,
        related_name="model_deployments",
    )
    runtime_config = models.JSONField(default=dict, blank=True)
    runtime_config_schema_version = models.PositiveIntegerField(default=1)
    runtime_config_size_bytes = models.PositiveIntegerField(default=2)
    runtime_config_sha256 = models.CharField(max_length=64)
    priority = models.SmallIntegerField(default=0)
    is_default = models.BooleanField(default=False)
    status = models.CharField(max_length=16, choices=Status.choices, default=Status.ACTIVE)

    class Meta:
        ordering = ("-is_default", "-priority", "key")
        indexes = [models.Index(fields=("status", "priority"))]
        constraints = [
            models.UniqueConstraint(
                fields=("runtime_host", "profile_version"),
                condition=Q(status="active"),
                name="unique_active_model_deployment",
            ),
        ]

    @property
    def provider(self):
        return self.profile_version.provider

    def __str__(self) -> str:
        return self.name


class ModelArtifact(UUIDTimeStampedModel):
    class Role(models.TextChoices):
        MODEL = "model", "模型"
        CHECKPOINT = "checkpoint", "Checkpoint"
        DIFFUSION_MODEL = "diffusion_model", "Diffusion model"
        TEXT_ENCODER = "text_encoder", "Text encoder"
        VAE = "vae", "VAE"
        LORA = "lora", "LoRA"
        TOKENIZER = "tokenizer", "Tokenizer"
        OTHER = "other", "其他"

    class Status(models.TextChoices):
        DECLARED = "declared", "已声明"
        AVAILABLE = "available", "可用"
        MISSING = "missing", "缺失"
        INVALID = "invalid", "校验失败"

    profile_version = models.ForeignKey(
        ModelProfileVersion,
        on_delete=models.PROTECT,
        related_name="artifacts",
    )
    role = models.CharField(max_length=24, choices=Role.choices)
    logical_name = models.CharField(max_length=120)
    filename = models.CharField(max_length=500)
    storage_uri = models.CharField(max_length=1000, blank=True)
    inventory_folder = models.CharField(
        max_length=120,
        blank=True,
        help_text="ComfyUI models 根下的受控目录；留空时按 role 使用平台默认目录。",
    )
    inventory_node_type = models.CharField(
        max_length=191,
        blank=True,
        help_text="自定义模型目录通过 object_info 枚举核对时使用的节点类型。",
    )
    inventory_input_name = models.CharField(
        max_length=120,
        blank=True,
        help_text="与 inventory_node_type 配套的模型文件输入名。",
    )
    sha256 = models.CharField(max_length=64, blank=True)
    size_bytes = models.PositiveBigIntegerField(null=True, blank=True)
    status = models.CharField(max_length=16, choices=Status.choices, default=Status.DECLARED)
    verified_snapshot = models.ForeignKey(
        "HostCapabilitySnapshot",
        null=True,
        blank=True,
        on_delete=models.PROTECT,
        related_name="verified_model_artifacts",
    )
    verified_at = models.DateTimeField(null=True, blank=True)
    verified_by = models.ForeignKey(
        settings.AUTH_USER_MODEL,
        null=True,
        blank=True,
        on_delete=models.PROTECT,
        related_name="verified_model_artifacts",
    )

    class Meta:
        ordering = ("profile_version", "role", "logical_name")
        constraints = [
            models.UniqueConstraint(
                fields=("profile_version", "role", "logical_name"),
                name="unique_model_artifact_role",
            ),
        ]

    def __str__(self) -> str:
        return f"{self.role}:{self.logical_name}"


class WorkflowTemplateVersion(UUIDTimeStampedModel):
    """Versioned executable graph; published rows are changed only by lifecycle service."""

    class Status(models.TextChoices):
        DRAFT = "draft", "草稿"
        VALIDATED = "validated", "已校验"
        PUBLISHED = "published", "已发布"
        RETIRED = "retired", "已停用"

    class ValidationState(models.TextChoices):
        NOT_RUN = "not_run", "未校验"
        PASSED = "passed", "通过"
        FAILED = "failed", "失败"

    template = models.ForeignKey(
        WorkflowTemplate,
        on_delete=models.PROTECT,
        related_name="platform_versions",
    )
    version = models.PositiveIntegerField(validators=[MinValueValidator(1)])
    provider = models.ForeignKey(
        ProviderProfile,
        null=True,
        blank=True,
        on_delete=models.PROTECT,
        related_name="workflow_versions",
    )
    model_profile_version = models.ForeignKey(
        ModelProfileVersion,
        null=True,
        blank=True,
        on_delete=models.PROTECT,
        related_name="workflow_versions",
    )
    graph = models.JSONField(default=dict)
    graph_schema_version = models.PositiveIntegerField(default=1)
    graph_size_bytes = models.PositiveIntegerField(default=2)
    graph_sha256 = models.CharField(max_length=64)
    input_contract = models.JSONField(default=dict, blank=True)
    input_contract_schema_version = models.PositiveIntegerField(default=1)
    input_contract_size_bytes = models.PositiveIntegerField(default=2)
    input_contract_sha256 = models.CharField(max_length=64)
    compiler_id = models.CharField(max_length=80, blank=True)
    compiler_version = models.PositiveIntegerField(default=0)
    semantic_bindings_sha256 = models.CharField(max_length=64, blank=True)
    model_requirements_sha256 = models.CharField(max_length=64, blank=True)
    execution_contract_sha256 = models.CharField(max_length=64, blank=True)
    status = models.CharField(max_length=16, choices=Status.choices, default=Status.DRAFT)
    validation_state = models.CharField(
        max_length=16,
        choices=ValidationState.choices,
        default=ValidationState.NOT_RUN,
    )
    validation_message = models.TextField(blank=True)
    smoke_test_state = models.CharField(
        max_length=16,
        choices=ValidationState.choices,
        default=ValidationState.NOT_RUN,
    )
    smoke_test_message = models.TextField(blank=True)
    validated_deployment = models.ForeignKey(
        ModelDeployment,
        null=True,
        blank=True,
        on_delete=models.PROTECT,
        related_name="validated_workflow_versions",
    )
    validation_evidence = models.JSONField(default=dict, blank=True)
    validation_evidence_sha256 = models.CharField(max_length=64, blank=True)
    smoke_test_evidence = models.JSONField(default=dict, blank=True)
    smoke_test_evidence_sha256 = models.CharField(max_length=64, blank=True)
    is_current = models.BooleanField(default=False)
    published_at = models.DateTimeField(null=True, blank=True)
    published_by = models.ForeignKey(
        settings.AUTH_USER_MODEL,
        null=True,
        blank=True,
        on_delete=models.PROTECT,
        related_name="published_workflow_template_versions",
    )

    class Meta:
        ordering = ("template", "-version")
        constraints = [
            models.UniqueConstraint(
                fields=("template", "version"),
                name="unique_platform_workflow_version",
            ),
            models.UniqueConstraint(
                fields=("template",),
                condition=Q(is_current=True),
                name="one_current_platform_workflow",
            ),
        ]
        indexes = [models.Index(fields=("status", "validation_state"))]

    def __str__(self) -> str:
        return f"{self.template.slug} platform-v{self.version}"


class ModelExecutionInstanceQuerySet(models.QuerySet):
    def update(self, **kwargs):
        if "key" in kwargs:
            raise ValidationError("执行实例稳定 key 创建后不可修改")
        return super().update(**kwargs)


class ModelExecutionInstance(UUIDTimeStampedModel):
    """A reusable business execution identity, not a second runtime deployment."""

    key = models.CharField(
        max_length=100, unique=True,
        validators=[RegexValidator(r"^[a-z][a-z0-9_.-]{1,99}$")],
    )
    name = models.CharField(max_length=160)
    description = models.TextField(blank=True)
    is_active = models.BooleanField(default=True)
    revision = models.PositiveIntegerField(default=1, validators=[MinValueValidator(1)])
    objects = ModelExecutionInstanceQuerySet.as_manager()

    class Meta:
        ordering = ("key",)
        constraints = [
            models.CheckConstraint(condition=Q(revision__gte=1), name="ck_exec_instance_revision"),
        ]

    def save(self, *args, **kwargs):
        previous = type(self).objects.filter(pk=self.pk).values_list("key", flat=True).first()
        if previous is not None and previous != self.key:
            raise ValidationError("执行实例稳定 key 创建后不可修改")
        return super().save(*args, **kwargs)

    def __str__(self):
        return self.name


class ModelExecutionInstanceVersion(UUIDTimeStampedModel):
    """One execution capability; prompt purpose belongs to the referenced profile."""

    class Status(models.TextChoices):
        DRAFT = "draft", "草稿"
        PUBLISHED = "published", "已发布"
        RETIRED = "retired", "已退役"

    class PromptMode(models.TextChoices):
        PROFILE = "profile", "策略组装"
        MANUAL = "manual", "人工完整提示词"
        NONE = "none", "无需提示词"

    instance = models.ForeignKey(ModelExecutionInstance, on_delete=models.PROTECT, related_name="versions")
    version = models.PositiveIntegerField(validators=[MinValueValidator(1)])
    status = models.CharField(max_length=16, choices=Status.choices, default=Status.DRAFT)
    deployment = models.ForeignKey(ModelDeployment, on_delete=models.PROTECT, related_name="execution_versions")
    operation = models.CharField(
        max_length=100,
        validators=[RegexValidator(r"^[a-z][a-z0-9_.-]{1,99}$")],
        help_text="模型和工作流声明的精确执行能力；不是提示词策略的用途。",
    )
    mode = models.CharField(
        max_length=40, blank=True,
        validators=[RegexValidator(r"^[A-Za-z0-9][A-Za-z0-9_.-]{0,39}$")],
    )
    prompt_mode = models.CharField(max_length=16, choices=PromptMode.choices)
    prompt_profile_version = models.ForeignKey(
        PromptProfileVersion, on_delete=models.PROTECT, null=True, blank=True,
        related_name="execution_versions",
    )
    workflow_version = models.ForeignKey(
        WorkflowTemplateVersion, on_delete=models.PROTECT, null=True, blank=True,
        related_name="execution_versions",
    )
    output_version = models.ForeignKey(
        BusinessOutputVersion, on_delete=models.PROTECT, null=True, blank=True,
        related_name="execution_versions",
    )
    revision = models.PositiveIntegerField(default=1, validators=[MinValueValidator(1)])
    published_at = models.DateTimeField(null=True, blank=True)
    published_by = models.ForeignKey(
        settings.AUTH_USER_MODEL, on_delete=models.PROTECT, null=True, blank=True,
        related_name="published_execution_instance_versions",
    )

    class Meta:
        ordering = ("instance", "-version")
        constraints = [
            models.UniqueConstraint(fields=("instance", "version"), name="uk_exec_instance_version"),
            models.CheckConstraint(condition=Q(version__gte=1, revision__gte=1), name="ck_exec_version_positive"),
            models.CheckConstraint(
                condition=(
                    Q(prompt_mode="profile", prompt_profile_version__isnull=False)
                    | Q(prompt_mode__in=("manual", "none"), prompt_profile_version__isnull=True)
                ),
                name="ck_exec_version_prompt_mode",
            ),
            models.CheckConstraint(
                condition=(
                    Q(status="draft", published_at__isnull=True, published_by__isnull=True)
                    | Q(status__in=("published", "retired"), published_at__isnull=False, published_by__isnull=False)
                ),
                name="ck_exec_version_publication",
            ),
        ]
        indexes = [models.Index(fields=("operation", "status"), name="idx_exec_operation_status")]

    def __str__(self):
        return f"{self.instance_id} v{self.version}"


class ExecutionInputSpec(UUIDTimeStampedModel):
    class Kind(models.TextChoices):
        TEXT = "text", "文本"
        JSON = "json", "结构化数据"
        IMAGE = "image", "图片"
        VIDEO = "video", "视频"
        AUDIO = "audio", "音频"

    version = models.ForeignKey(ModelExecutionInstanceVersion, on_delete=models.CASCADE, related_name="inputs")
    key = models.CharField(max_length=100, validators=[RegexValidator(r"^[a-z][a-z0-9_.-]{1,99}$")])
    position = models.PositiveIntegerField()
    kind = models.CharField(max_length=16, choices=Kind.choices)
    required = models.BooleanField()
    min_items = models.PositiveIntegerField()
    max_items = models.PositiveIntegerField()
    semantic_contract = models.JSONField()
    media_source_policy = models.JSONField(null=True, blank=True)
    data_structure_version = models.ForeignKey(
        DataStructureVersion, null=True, blank=True, on_delete=models.PROTECT, related_name="execution_inputs",
    )

    class Meta:
        ordering = ("position", "key")
        constraints = [
            models.UniqueConstraint(fields=("version", "key"), name="uk_exec_input_key"),
            models.UniqueConstraint(fields=("version", "position"), name="uk_exec_input_position"),
            models.CheckConstraint(condition=Q(min_items__lte=models.F("max_items")), name="ck_exec_input_count_range"),
            models.CheckConstraint(condition=Q(required=False) | Q(min_items__gte=1), name="ck_exec_input_required"),
            models.CheckConstraint(
                condition=Q(kind__in=("image", "video", "audio")) | Q(kind__in=("text", "json"), max_items__lte=1),
                name="ck_exec_input_scalar",
            ),
        ]

    def clean(self):
        from studio.services.execution_instances import validate_input_semantic_contract

        super().clean()
        validate_input_semantic_contract(self.semantic_contract)
        from studio.services.execution_media_source_policy import execution_media_source_policy
        execution_media_source_policy({"key": self.key, "kind": self.kind,
            "min_items": self.min_items, "max_items": self.max_items,
            "semantic_contract": self.semantic_contract, "media_source_policy": self.media_source_policy})
        if self.data_structure_version_id:
            from studio.services.execution_input_structures import input_structure_snapshot
            input_structure_snapshot(self)


class ExecutionInputTarget(UUIDTimeStampedModel):
    class Kind(models.TextChoices):
        WORKFLOW_SLOT = "workflow_slot", "工作流槽位"
        OPERATION_INPUT = "operation_input", "执行输入"
        PROFILE_INPUT = "profile_input", "策略输入"

    input_spec = models.ForeignKey(ExecutionInputSpec, on_delete=models.CASCADE, related_name="targets")
    target_kind = models.CharField(max_length=24, choices=Kind.choices)
    target_key = models.CharField(max_length=191)
    is_primary = models.BooleanField()

    class Meta:
        ordering = ("target_kind", "target_key")
        constraints = [
            models.UniqueConstraint(fields=("input_spec", "target_kind", "target_key"), name="uk_exec_input_target"),
            models.UniqueConstraint(fields=("input_spec",), condition=Q(is_primary=True), name="uk_exec_input_primary"),
            models.CheckConstraint(
                condition=(
                    Q(target_kind__in=("workflow_slot", "operation_input"))
                    | Q(target_kind="profile_input", is_primary=False)
                ),
                name="ck_exec_primary_target_kind",
            ),
        ]


class ExecutionParameterSpec(UUIDTimeStampedModel):
    class ValueType(models.TextChoices):
        BOOLEAN = "boolean", "布尔值"
        INTEGER = "integer", "整数"
        NUMBER = "number", "数值"
        STRING = "string", "文本"

    class Policy(models.TextChoices):
        OVERRIDABLE = "overridable", "允许运行时配置"
        LOCKED = "locked", "固定值"

    class SeedPolicy(models.TextChoices):
        FIXED = "fixed", "使用明确种子"
        GENERATE_ONCE = "generate_once", "每次新请求生成一次"

    version = models.ForeignKey(ModelExecutionInstanceVersion, on_delete=models.CASCADE, related_name="parameters")
    key = models.CharField(max_length=100, validators=[RegexValidator(r"^[a-z][a-z0-9_.-]{1,99}$")])
    value_type = models.CharField(max_length=16, choices=ValueType.choices)
    minimum = models.JSONField(null=True, blank=True)
    maximum = models.JSONField(null=True, blank=True)
    enum_values = models.JSONField(default=list, blank=True)
    step = models.JSONField(null=True, blank=True)
    remainder = models.JSONField(null=True, blank=True)
    policy = models.CharField(max_length=16, choices=Policy.choices)
    default_value = models.JSONField(null=True, blank=True)
    locked_value = models.JSONField(null=True, blank=True)
    seed_policy = models.CharField(max_length=16, choices=SeedPolicy.choices, null=True, blank=True)

    class Meta:
        ordering = ("key",)
        constraints = [
            models.UniqueConstraint(fields=("version", "key"), name="uk_exec_parameter_key"),
            models.CheckConstraint(
                condition=(
                    Q(policy="locked", default_value__isnull=True, locked_value__isnull=False)
                    | Q(policy="overridable", locked_value__isnull=True)
                ),
                name="ck_exec_parameter_policy",
            ),
            models.CheckConstraint(
                condition=(
                    Q(seed_policy__isnull=True)
                    | Q(seed_policy="fixed", value_type="integer")
                    | Q(seed_policy="generate_once", value_type="integer", policy="overridable",
                        default_value__isnull=True, locked_value__isnull=True)
                ),
                name="ck_exec_parameter_seed_policy",
            ),
        ]

    def clean(self):
        from studio.services.execution_instances import validate_execution_parameter_spec

        super().clean()
        validate_execution_parameter_spec(self)


class ExecutionAuxiliaryBinding(UUIDTimeStampedModel):
    class Purpose(models.TextChoices):
        GENERATE_FIELDS = "generate_fields", "生成字段"
        TRANSLATE_FIELDS = "translate_fields", "翻译字段"

    version = models.ForeignKey(ModelExecutionInstanceVersion, on_delete=models.CASCADE, related_name="auxiliaries")
    purpose = models.CharField(max_length=24, choices=Purpose.choices)
    target_version = models.ForeignKey(ModelExecutionInstanceVersion, on_delete=models.PROTECT, related_name="auxiliary_consumers")

    class Meta:
        ordering = ("purpose",)
        constraints = [
            models.UniqueConstraint(fields=("version", "purpose"), name="uk_exec_auxiliary_purpose"),
            models.CheckConstraint(condition=~Q(version=models.F("target_version")), name="ck_exec_auxiliary_not_self"),
        ]


class ExecutionVersionContract(UUIDTimeStampedModel):
    """Derived detail snapshot; never a second editable configuration source."""

    version = models.OneToOneField(ModelExecutionInstanceVersion, on_delete=models.CASCADE, related_name="contract")
    definition = models.JSONField()
    schema_version = models.PositiveIntegerField(validators=[MinValueValidator(1)])
    size_bytes = models.PositiveIntegerField(validators=[MaxValueValidator(65536)])
    sha256 = models.CharField(max_length=64, validators=[RegexValidator(r"^[a-f0-9]{64}$")])
    target_fingerprint = models.CharField(max_length=64, validators=[RegexValidator(r"^[a-f0-9]{64}$")])

    class Meta:
        constraints = [
            models.CheckConstraint(condition=Q(schema_version__gte=1, size_bytes__lte=65536), name="ck_exec_contract_size_schema"),
        ]

    def clean(self):
        from studio.services.json_contracts import verify_json_contract
        from studio.services.execution_instances import execution_configuration_max_bytes

        super().clean()
        if not isinstance(self.definition, dict):
            raise ValidationError({"definition": "执行实例合同必须是对象"})
        verify_json_contract(
            self.definition, schema_version=self.schema_version,
            size_bytes=self.size_bytes, sha256=self.sha256, max_bytes=execution_configuration_max_bytes(),
        )


class ExecutionSnapshotQuerySet(models.QuerySet):
    """Snapshot changes require the locked preparation service, not bulk writes."""

    def update(self, **kwargs):
        protected = self.model._SNAPSHOT_FIELDS | self.model._WRITE_ONCE_FIELDS | getattr(self.model, "_REVIEW_FIELDS", frozenset())
        names = {self.model._meta.get_field(name).attname for name in kwargs}
        if protected & names:
            raise ValidationError("执行准备的冻结事实不能批量修改")
        return super().update(**kwargs)


def _validate_execution_snapshot_update(instance):
    fields = instance._SNAPSHOT_FIELDS | instance._WRITE_ONCE_FIELDS
    if instance._state.adding:
        return
    stored = type(instance).objects.filter(pk=instance.pk).values(*fields).get()
    for name in fields:
        if name in instance._WRITE_ONCE_FIELDS and stored[name] is None:
            continue
        if stored[name] != getattr(instance, name):
            raise ValidationError({name: "已冻结的执行事实不可改写，请创建新准备或字段修订"})


def _require_unfrozen_preparation(preparation_id):
    preparation = ExecutionPreparation.objects.only("request_frozen_at", "status").get(pk=preparation_id)
    if preparation.request_frozen_at is not None or preparation.status == "cancelled":
        raise ValidationError("执行准备已冻结或取消，不能修改字段或采用结果", code="execution_preparation_conflict")


class ExecutionPreparation(UUIDTimeStampedModel):
    """A recoverable preparation, not a second remote execution queue."""

    class Purpose(models.TextChoices):
        PRODUCTION = "production", "业务执行"
        INSTANCE_SMOKE = "instance_smoke", "实例组合验收"
        RESOURCE_SMOKE = "resource_smoke", "基础资源验收"

    class Status(models.TextChoices):
        AWAITING_INPUT = "awaiting_input", "待填写"
        PREPARING = "preparing", "准备中"
        AWAITING_REVIEW = "awaiting_review", "待审核字段"
        BLOCKED = "blocked", "准备受阻"
        READY = "ready", "请求已就绪"
        DISPATCHED = "dispatched", "已交给原任务队列"
        CANCELLED = "cancelled", "已取消准备"

    stage_run = models.ForeignKey("StageRun", on_delete=models.CASCADE, null=True, blank=True, db_index=False, related_name="execution_preparations")
    asset_job = models.ForeignKey("AssetGenerationJob", on_delete=models.CASCADE, null=True, blank=True, db_index=False, related_name="execution_preparations")
    deployment_smoke = models.ForeignKey("ModelDeploymentSmokeAttempt", on_delete=models.CASCADE, null=True, blank=True, db_index=False, related_name="execution_preparations")
    workflow_smoke = models.ForeignKey("WorkflowSmokeAttempt", on_delete=models.CASCADE, null=True, blank=True, db_index=False, related_name="execution_preparations")
    execution_instance_version = models.ForeignKey(ModelExecutionInstanceVersion, on_delete=models.PROTECT, null=True, blank=True, related_name="preparations")
    purpose = models.CharField(max_length=24, choices=Purpose.choices)
    unit_key = models.CharField(max_length=191)
    request_key = models.CharField(max_length=191)
    supersedes = models.OneToOneField("self", on_delete=models.PROTECT, null=True, blank=True,
        related_name="replacement")
    revision = models.PositiveIntegerField(default=1, validators=[MinValueValidator(1)])
    status = models.CharField(max_length=24, choices=Status.choices)
    created_by = models.ForeignKey(settings.AUTH_USER_MODEL, on_delete=models.PROTECT, related_name="execution_preparations")
    next_check_at = models.DateTimeField(null=True, blank=True)
    claim_token = models.UUIDField(null=True, blank=True)
    claim_until = models.DateTimeField(null=True, blank=True)
    intent_sha256 = models.CharField(max_length=64, validators=[RegexValidator(r"^[a-f0-9]{64}$")])
    request_sha256 = models.CharField(max_length=64, null=True, blank=True, validators=[RegexValidator(r"^[a-f0-9]{64}$")])
    request_frozen_at = models.DateTimeField(null=True, blank=True)
    cancelled_at = models.DateTimeField(null=True, blank=True)
    cancelled_by = models.ForeignKey(settings.AUTH_USER_MODEL, on_delete=models.PROTECT, null=True, blank=True, db_index=False,
        related_name="cancelled_execution_preparations")
    reason_code = models.CharField(max_length=100, blank=True)
    reason = models.CharField(max_length=500, blank=True)
    evaluation_revision = models.PositiveIntegerField(null=True, blank=True)
    evaluated_at = models.DateTimeField(null=True, blank=True)

    _SNAPSHOT_FIELDS = frozenset((
        "stage_run_id", "asset_job_id", "deployment_smoke_id", "workflow_smoke_id",
        "execution_instance_version_id", "purpose", "unit_key", "request_key", "created_by_id", "intent_sha256", "supersedes_id",
    ))
    _WRITE_ONCE_FIELDS = frozenset(("request_sha256", "request_frozen_at", "cancelled_at", "cancelled_by_id"))
    objects = models.Manager.from_queryset(ExecutionSnapshotQuerySet)()

    class Meta:
        ordering = ("created_at", "id")
        indexes = [models.Index(fields=("status", "next_check_at"), name="idx_exec_prep_due")]
        constraints = [
            models.CheckConstraint(condition=(
                Q(stage_run__isnull=False, asset_job__isnull=True, deployment_smoke__isnull=True, workflow_smoke__isnull=True)
                | Q(stage_run__isnull=True, asset_job__isnull=False, deployment_smoke__isnull=True, workflow_smoke__isnull=True)
                | Q(stage_run__isnull=True, asset_job__isnull=True, deployment_smoke__isnull=False, workflow_smoke__isnull=True)
                | Q(stage_run__isnull=True, asset_job__isnull=True, deployment_smoke__isnull=True, workflow_smoke__isnull=False)
            ), name="ck_exec_prep_one_owner"),
            models.CheckConstraint(condition=(
                Q(purpose="production", execution_instance_version__isnull=False, deployment_smoke__isnull=True, workflow_smoke__isnull=True)
                | Q(purpose="instance_smoke", execution_instance_version__isnull=False, stage_run__isnull=True, asset_job__isnull=True)
                | Q(purpose="resource_smoke", execution_instance_version__isnull=True, stage_run__isnull=True, asset_job__isnull=True)
            ), name="ck_exec_prep_purpose"),
            models.UniqueConstraint(fields=("stage_run", "unit_key", "request_key"), condition=Q(stage_run__isnull=False), name="uk_exec_prep_stage_request"),
            models.UniqueConstraint(fields=("asset_job",), condition=Q(asset_job__isnull=False), name="uk_exec_prep_asset_job"),
            models.UniqueConstraint(fields=("deployment_smoke",), condition=Q(deployment_smoke__isnull=False), name="uk_exec_prep_deploy_smoke"),
            models.UniqueConstraint(fields=("workflow_smoke",), condition=Q(workflow_smoke__isnull=False), name="uk_exec_prep_workflow_smoke"),
            models.CheckConstraint(condition=Q(revision__gte=1), name="ck_exec_prep_revision"),
            models.CheckConstraint(condition=(Q(evaluation_revision__isnull=True, evaluated_at__isnull=True)
                | Q(evaluation_revision__isnull=False, evaluation_revision__gte=1, evaluation_revision__lte=models.F("revision"), evaluated_at__isnull=False)), name="ck_exec_prep_evaluation"),
            models.CheckConstraint(condition=Q(status__in=("awaiting_input", "preparing", "awaiting_review", "blocked", "ready", "dispatched", "cancelled")), name="ck_exec_prep_status"),
            models.CheckConstraint(condition=(Q(claim_token__isnull=True, claim_until__isnull=True) | Q(claim_token__isnull=False, claim_until__isnull=False)), name="ck_exec_prep_claim"),
            models.CheckConstraint(condition=(Q(request_sha256__isnull=True, request_frozen_at__isnull=True) | Q(request_sha256__isnull=False, request_frozen_at__isnull=False)), name="ck_exec_prep_frozen"),
            models.CheckConstraint(condition=(~Q(status__in=("ready", "dispatched")) | Q(request_sha256__isnull=False, request_frozen_at__isnull=False)), name="ck_exec_prep_ready_request"),
            models.CheckConstraint(condition=(Q(request_frozen_at__isnull=True) | Q(status__in=("ready", "dispatched", "cancelled"))), name="ck_exec_prep_frozen_status"),
        ]

    def save(self, *args, **kwargs):
        _validate_execution_snapshot_update(self)
        super().save(*args, **kwargs)


class ExecutionResourceConfirmation(UUIDTimeStampedModel):
    """Append-only, exact-request human acknowledgement of business thresholds."""

    preparation = models.ForeignKey(ExecutionPreparation, on_delete=models.PROTECT, related_name="resource_confirmations")
    fingerprint = models.CharField(max_length=64, validators=[RegexValidator(r"^[a-f0-9]{64}$")])
    policy_sha256 = models.CharField(max_length=64, validators=[RegexValidator(r"^[a-f0-9]{64}$")])
    confirmed_by = models.ForeignKey(settings.AUTH_USER_MODEL, on_delete=models.PROTECT)
    evidence = models.JSONField()
    evidence_schema_version = models.PositiveIntegerField(default=1)
    evidence_size_bytes = models.PositiveIntegerField(default=2, validators=[MaxValueValidator(65536)])
    evidence_sha256 = models.CharField(max_length=64, default="", validators=[RegexValidator(r"^[a-f0-9]{64}$")])

    _SNAPSHOT_FIELDS = frozenset(("preparation_id", "fingerprint", "policy_sha256", "confirmed_by_id", "evidence",
        "evidence_schema_version", "evidence_size_bytes", "evidence_sha256"))
    _WRITE_ONCE_FIELDS = frozenset()
    objects = models.Manager.from_queryset(ExecutionSnapshotQuerySet)()

    class Meta:
        constraints = [models.UniqueConstraint(fields=("preparation", "fingerprint"), name="uk_exec_resource_confirmation")]

    def save(self, *args, **kwargs):
        _validate_execution_snapshot_update(self)
        super().save(*args, **kwargs)


class ExecutionCapacityObservation(UUIDTimeStampedModel):
    """One derived successful-call sample, owned by the original preparation.

    A nullable match key means dimensions are incomplete, not certified zero.
    Hot lookups select only the indexed key/time; details are read on demand.
    Deleting the original preparation removes its derived observation.
    """

    preparation = models.OneToOneField(ExecutionPreparation, on_delete=models.CASCADE, related_name="capacity_observation")
    execution_instance_version = models.ForeignKey(ModelExecutionInstanceVersion, on_delete=models.PROTECT,
        db_index=False, related_name="capacity_observations")
    request_sha256 = models.CharField(max_length=64, validators=[RegexValidator(r"^[a-f0-9]{64}$")])
    signature_sha256 = models.CharField(max_length=64, null=True, blank=True, validators=[RegexValidator(r"^[a-f0-9]{64}$")])
    observed_at = models.DateTimeField()
    elapsed_ms = models.PositiveBigIntegerField()
    details = models.JSONField()
    details_schema_version = models.PositiveIntegerField(validators=[MinValueValidator(1)])
    details_size_bytes = models.PositiveIntegerField(validators=[MaxValueValidator(65536)])
    details_sha256 = models.CharField(max_length=64, validators=[RegexValidator(r"^[a-f0-9]{64}$")])

    _SNAPSHOT_FIELDS = frozenset(("preparation_id", "execution_instance_version_id", "request_sha256",
        "signature_sha256", "observed_at", "elapsed_ms", "details", "details_schema_version", "details_size_bytes", "details_sha256"))
    _WRITE_ONCE_FIELDS = frozenset()
    objects = models.Manager.from_queryset(ExecutionSnapshotQuerySet)()

    class Meta:
        ordering = ("-observed_at", "-id")
        indexes = [models.Index(fields=("execution_instance_version", "signature_sha256", "-observed_at", "-id"), name="idx_exec_capacity_sample")]
        constraints = [models.CheckConstraint(condition=Q(details_size_bytes__lte=65536), name="ck_exec_capacity_detail_size")]

    def save(self, *args, **kwargs):
        _validate_execution_snapshot_update(self)
        super().save(*args, **kwargs)


class ExecutionPreparationContract(UUIDTimeStampedModel):
    """Low-frequency immutable intent and write-once finalized request."""

    preparation = models.OneToOneField(ExecutionPreparation, on_delete=models.CASCADE, related_name="contract")
    intent = models.JSONField()
    intent_schema_version = models.PositiveIntegerField(validators=[MinValueValidator(1)])
    intent_size_bytes = models.PositiveIntegerField(validators=[MinValueValidator(2)])
    intent_sha256 = models.CharField(max_length=64, validators=[RegexValidator(r"^[a-f0-9]{64}$")])
    resolved_inputs = models.JSONField()
    resolved_inputs_schema_version = models.PositiveIntegerField(validators=[MinValueValidator(1)])
    resolved_inputs_size_bytes = models.PositiveIntegerField(validators=[MinValueValidator(2)])
    resolved_inputs_sha256 = models.CharField(max_length=64, validators=[RegexValidator(r"^[a-f0-9]{64}$")])
    review_contract = models.JSONField()
    review_contract_schema_version = models.PositiveIntegerField(validators=[MinValueValidator(1)])
    review_contract_size_bytes = models.PositiveIntegerField(validators=[MaxValueValidator(65536)])
    review_contract_sha256 = models.CharField(max_length=64, validators=[RegexValidator(r"^[a-f0-9]{64}$")])
    prepared_request_key = models.CharField(max_length=191, null=True, blank=True)
    prepared_request_schema_version = models.PositiveIntegerField(null=True, blank=True, validators=[MinValueValidator(1)])
    prepared_request_size_bytes = models.PositiveIntegerField(null=True, blank=True, validators=[MinValueValidator(2)])
    prepared_request_sha256 = models.CharField(max_length=64, null=True, blank=True, validators=[RegexValidator(r"^[a-f0-9]{64}$")])
    evaluation = models.JSONField(null=True, blank=True)
    evaluation_schema_version = models.PositiveIntegerField(null=True, blank=True, validators=[MinValueValidator(1)])
    evaluation_size_bytes = models.PositiveIntegerField(null=True, blank=True, validators=[MaxValueValidator(65536)])
    evaluation_sha256 = models.CharField(max_length=64, null=True, blank=True, validators=[RegexValidator(r"^[a-f0-9]{64}$")])

    _SNAPSHOT_FIELDS = frozenset(("preparation_id",)) | frozenset(
        name + suffix for name in ("intent", "resolved_inputs", "review_contract")
        for suffix in ("", "_schema_version", "_size_bytes", "_sha256")
    )
    _WRITE_ONCE_FIELDS = frozenset("prepared_request" + suffix for suffix in ("_key", "_schema_version", "_size_bytes", "_sha256"))
    objects = models.Manager.from_queryset(ExecutionSnapshotQuerySet)()

    class Meta:
        constraints = [
            models.CheckConstraint(condition=Q(intent_schema_version__gte=1, resolved_inputs_schema_version__gte=1, review_contract_schema_version__gte=1, intent_size_bytes__gte=2, resolved_inputs_size_bytes__gte=2, review_contract_size_bytes__lte=65536), name="ck_exec_prep_contract_size"),
            models.CheckConstraint(condition=(
                Q(prepared_request_key__isnull=True, prepared_request_schema_version__isnull=True, prepared_request_size_bytes__isnull=True, prepared_request_sha256__isnull=True)
                | Q(prepared_request_key__isnull=False, prepared_request_schema_version__isnull=False, prepared_request_size_bytes__isnull=False, prepared_request_sha256__isnull=False, prepared_request_schema_version=1, prepared_request_size_bytes__gte=2)
            ), name="ck_exec_prepared_metadata"),
            models.CheckConstraint(condition=(
                Q(evaluation__isnull=True, evaluation_schema_version__isnull=True, evaluation_size_bytes__isnull=True, evaluation_sha256__isnull=True)
                | Q(evaluation__isnull=False, evaluation_schema_version__isnull=False, evaluation_size_bytes__isnull=False,
                    evaluation_sha256__isnull=False, evaluation_schema_version__gte=1, evaluation_size_bytes__lte=65536)
            ), name="ck_exec_evaluation_metadata"),
        ]

    def clean(self):
        from studio.services.json_contracts import verify_json_contract
        from studio.services.execution_instances import execution_configuration_max_bytes
        from studio.services.execution_request_storage import execution_request_max_bytes

        super().clean()
        for name in ("intent", "resolved_inputs", "review_contract", "evaluation"):
            value = getattr(self, name)
            if name == "evaluation" and value is None:
                continue
            if not isinstance(value, dict):
                raise ValidationError({name: "准备合同必须是明确对象"})
            verify_json_contract(value, schema_version=getattr(self, f"{name}_schema_version"), size_bytes=getattr(self, f"{name}_size_bytes"), sha256=getattr(self, f"{name}_sha256"), max_bytes=(execution_request_max_bytes() if name in {"intent", "resolved_inputs"} else execution_configuration_max_bytes()))
        if self.preparation_id and self.intent_sha256 != self.preparation.intent_sha256:
            raise ValidationError({"intent_sha256": "准备索引与详情的意图摘要不一致"})
        from studio.services.execution_request_storage import read_request
        read_request(self, "prepared_request")

    @property
    def prepared_request(self):
        from studio.services.execution_request_storage import read_request
        return read_request(self, "prepared_request")

    def save(self, *args, **kwargs):
        _validate_execution_snapshot_update(self)
        self.clean()
        super().save(*args, **kwargs)


class ExecutionFieldValue(UUIDTimeStampedModel):
    class Origin(models.TextChoices):
        SERVER = "server", "服务器派生"
        MANUAL = "manual", "人工填写"
        MODEL = "model", "真实模型结果"

    class Status(models.TextChoices):
        PENDING = "pending", "待确认"
        CONFIRMED = "confirmed", "已确认"
        REJECTED = "rejected", "已拒绝"

    class Approval(models.TextChoices):
        HUMAN = "human", "人工审核"
        POLICY = "policy", "明确策略批准"
        SERVER_DERIVED = "server_derived", "来源确定性派生"

    preparation = models.ForeignKey(ExecutionPreparation, on_delete=models.CASCADE, related_name="field_values", db_index=False)
    unit_key = models.CharField(max_length=191)
    field_key = models.CharField(max_length=100)
    revision = models.PositiveIntegerField(validators=[MinValueValidator(1)])
    source_sha256 = models.CharField(max_length=64, validators=[RegexValidator(r"^[a-f0-9]{64}$")])
    field_contract_sha256 = models.CharField(max_length=64, validators=[RegexValidator(r"^[a-f0-9]{64}$")])
    text = models.TextField()
    text_sha256 = models.CharField(max_length=64, validators=[RegexValidator(r"^[a-f0-9]{64}$")])
    origin_kind = models.CharField(max_length=16, choices=Origin.choices)
    origin_attempt = models.ForeignKey("GenerationAttempt", on_delete=models.PROTECT, null=True, blank=True, related_name="execution_field_values")
    origin_asset_job = models.ForeignKey("AssetGenerationJob", on_delete=models.PROTECT, null=True, blank=True, related_name="execution_field_values")
    created_by = models.ForeignKey(settings.AUTH_USER_MODEL, on_delete=models.PROTECT, related_name="execution_field_values")
    status = models.CharField(max_length=16, choices=Status.choices)
    approval_kind = models.CharField(max_length=24, choices=Approval.choices, null=True, blank=True)
    reviewed_by = models.ForeignKey(settings.AUTH_USER_MODEL, on_delete=models.PROTECT, null=True, blank=True, related_name="reviewed_execution_fields")
    reviewed_at = models.DateTimeField(null=True, blank=True)
    approval_policy_sha256 = models.CharField(max_length=64, null=True, blank=True, validators=[RegexValidator(r"^[a-f0-9]{64}$")])

    _SNAPSHOT_FIELDS = frozenset((
        "preparation_id", "unit_key", "field_key", "revision", "source_sha256", "field_contract_sha256",
        "text", "text_sha256", "origin_kind", "origin_attempt_id", "origin_asset_job_id", "created_by_id",
    ))
    _WRITE_ONCE_FIELDS = frozenset()
    _REVIEW_FIELDS = frozenset(("status", "approval_kind", "reviewed_by_id", "reviewed_at", "approval_policy_sha256"))
    objects = models.Manager.from_queryset(ExecutionSnapshotQuerySet)()

    class Meta:
        ordering = ("unit_key", "field_key", "revision")
        constraints = [
            models.UniqueConstraint(fields=("preparation", "unit_key", "field_key", "revision"), name="uk_exec_field_revision"),
            models.CheckConstraint(condition=Q(revision__gte=1), name="ck_exec_field_revision"),
            models.CheckConstraint(condition=(
                Q(origin_kind__in=("manual", "server"), origin_attempt__isnull=True, origin_asset_job__isnull=True)
                | Q(origin_kind="model", origin_attempt__isnull=False, origin_asset_job__isnull=True)
                | Q(origin_kind="model", origin_attempt__isnull=True, origin_asset_job__isnull=False)
            ), name="ck_exec_field_origin"),
            models.CheckConstraint(condition=(
                Q(status="pending", approval_kind__isnull=True, reviewed_by__isnull=True, reviewed_at__isnull=True, approval_policy_sha256__isnull=True)
                | Q(status="rejected", approval_kind__isnull=True, reviewed_by__isnull=False, reviewed_at__isnull=False, approval_policy_sha256__isnull=True)
                | (Q(status="confirmed", reviewed_at__isnull=False) & (
                    Q(approval_kind="human", reviewed_by__isnull=False, approval_policy_sha256__isnull=True)
                    | Q(approval_kind="policy", approval_policy_sha256__isnull=False)
                    | Q(approval_kind="server_derived", origin_kind="server", approval_policy_sha256__isnull=True)
                ))
            ), name="ck_exec_field_approval"),
        ]

    def clean(self):
        import hashlib
        from studio.services.execution_instances import execution_configuration_max_bytes

        super().clean()
        if not isinstance(self.text, str):
            raise ValidationError({"text": "字段正文必须是文本"})
        try:
            encoded = self.text.encode("utf-8")
        except UnicodeError as exc:
            raise ValidationError({"text": "字段正文包含无效字符"}) from exc
        if len(encoded) > execution_configuration_max_bytes():
            raise ValidationError({"text": "字段正文超出存储边界，请使用受管内容引用"})
        if hashlib.sha256(encoded).hexdigest() != self.text_sha256:
            raise ValidationError({"text_sha256": "字段正文与摘要不一致"})

    def save(self, *args, **kwargs):
        _require_unfrozen_preparation(self.preparation_id)
        _validate_execution_snapshot_update(self)
        if not self._state.adding:
            review_fields = ("status", "approval_kind", "reviewed_by_id", "reviewed_at", "approval_policy_sha256")
            old = type(self).objects.filter(pk=self.pk).values(*review_fields).get()
            if old["status"] != "pending" and any(old[name] != getattr(self, name) for name in review_fields):
                raise ValidationError("已确认或拒绝的字段不可重新审批，请创建新修订")
        self.clean()
        super().save(*args, **kwargs)


class ExecutionFieldSelection(UUIDTimeStampedModel):
    """Explicit choice; a newer confirmed field never replaces it implicitly."""

    preparation = models.ForeignKey(ExecutionPreparation, on_delete=models.CASCADE, related_name="field_selections", db_index=False)
    unit_key = models.CharField(max_length=191)
    field_key = models.CharField(max_length=100)
    field_value = models.ForeignKey(ExecutionFieldValue, on_delete=models.PROTECT, related_name="selections")
    revision = models.PositiveIntegerField(validators=[MinValueValidator(1)])
    selected_by = models.ForeignKey(settings.AUTH_USER_MODEL, on_delete=models.PROTECT, related_name="execution_field_selections")
    selected_at = models.DateTimeField()

    _SNAPSHOT_FIELDS = frozenset(("preparation_id", "unit_key", "field_key"))
    _WRITE_ONCE_FIELDS = frozenset()
    _REVIEW_FIELDS = frozenset(("field_value_id", "revision", "selected_by_id", "selected_at"))
    objects = models.Manager.from_queryset(ExecutionSnapshotQuerySet)()

    class Meta:
        ordering = ("unit_key", "field_key")
        constraints = [
            models.UniqueConstraint(fields=("preparation", "unit_key", "field_key"), name="uk_exec_field_selection"),
            models.CheckConstraint(condition=Q(revision__gte=1), name="ck_exec_selection_revision"),
        ]

    def clean(self):
        super().clean()
        value = self.field_value
        if value.status != "confirmed" or value.unit_key != self.unit_key or value.field_key != self.field_key:
            raise ValidationError({"field_value": "只能采用相同单元与字段的已确认修订"})

    def save(self, *args, **kwargs):
        _require_unfrozen_preparation(self.preparation_id)
        _validate_execution_snapshot_update(self)
        self.clean()
        super().save(*args, **kwargs)


class WorkflowProjectorQuerySet(models.QuerySet):
    """Preserve the stable family identity outside the API serializer too."""

    def update(self, **kwargs):
        if "key" in kwargs:
            raise ValidationError("WorkflowProjector 稳定 key 创建后不可修改")
        return super().update(**kwargs)


class WorkflowProjectorManager(models.Manager.from_queryset(WorkflowProjectorQuerySet)):
    pass


class WorkflowProjector(UUIDTimeStampedModel):
    """Stable identity for a server-reviewed saved-workflow contract family."""

    key = models.CharField(
        max_length=100,
        unique=True,
        validators=[RegexValidator(r"^[a-z][a-z0-9_.-]{1,99}$")],
    )
    name = models.CharField(max_length=160)
    description = models.TextField(blank=True)
    is_active = models.BooleanField(default=True)
    objects = WorkflowProjectorManager()

    class Meta:
        ordering = ("key",)

    def __str__(self) -> str:
        return self.key

    def save(self, *args, **kwargs):
        if self.pk:
            existing_key = WorkflowProjector.objects.filter(pk=self.pk).values_list(
                "key", flat=True,
            ).first()
            if existing_key is not None and existing_key != self.key:
                raise ValidationError("WorkflowProjector 稳定 key 创建后不可修改")
        return super().save(*args, **kwargs)


class WorkflowProjectorVersionQuerySet(models.QuerySet):
    """Keep published configuration content immutable outside lifecycle actions."""

    def update(self, **kwargs):
        if set(kwargs) - {"is_current", "updated_at"}:
            raise ValidationError(
                "WorkflowProjectorVersion 内容不能通过批量更新改写",
            )
        return super().update(**kwargs)

    def delete(self):
        raise ValidationError("WorkflowProjectorVersion 是版本化配置，不能批量删除")


class WorkflowProjectorVersionManager(
    models.Manager.from_queryset(WorkflowProjectorVersionQuerySet),
):
    pass


class WorkflowProjectorVersion(UUIDTimeStampedModel):
    """Versioned data contract composed from registered, code-reviewed engines."""

    class Status(models.TextChoices):
        DRAFT = "draft", "草稿"
        PUBLISHED = "published", "已发布"
        RETIRED = "retired", "已停用"

    projector = models.ForeignKey(
        WorkflowProjector,
        on_delete=models.PROTECT,
        related_name="versions",
    )
    version = models.PositiveIntegerField(validators=[MinValueValidator(1)])
    provider_driver_type = models.CharField(
        max_length=32,
        choices=ProviderProfile.DriverType.choices,
        default=ProviderProfile.DriverType.COMFYUI,
    )
    operation = models.CharField(max_length=120, default="workflow.import")
    output_kind = models.CharField(
        max_length=16,
        choices=WorkflowTemplate.OutputKind.choices,
        blank=True,
        default="",
        help_text="留空表示由工作流主输出节点决定。",
    )
    engine_key = models.CharField(
        max_length=100,
        validators=[RegexValidator(r"^[a-z][a-z0-9_.-]{1,99}$")],
    )
    engine_version = models.PositiveIntegerField(validators=[MinValueValidator(1)])
    definition = models.JSONField(default=dict)
    definition_schema_version = models.PositiveIntegerField(default=1)
    definition_size_bytes = models.PositiveIntegerField(default=2)
    definition_sha256 = models.CharField(max_length=64)
    status = models.CharField(
        max_length=16,
        choices=Status.choices,
        default=Status.DRAFT,
    )
    is_current = models.BooleanField(default=False)
    published_at = models.DateTimeField(null=True, blank=True)
    published_by = models.ForeignKey(
        settings.AUTH_USER_MODEL,
        null=True,
        blank=True,
        on_delete=models.PROTECT,
        related_name="published_workflow_projector_versions",
    )
    objects = WorkflowProjectorVersionManager()

    class Meta:
        ordering = ("projector", "-version")
        constraints = [
            models.UniqueConstraint(
                fields=("projector", "version"),
                name="unique_workflow_projector_version",
            ),
            models.UniqueConstraint(
                fields=("projector",),
                condition=Q(is_current=True),
                name="one_current_workflow_projector",
            ),
            models.CheckConstraint(
                condition=Q(is_current=False) | Q(status="published"),
                name="current_workflow_projector_is_published",
            ),
        ]
        indexes = [models.Index(fields=("status", "engine_key"))]

    def __str__(self) -> str:
        return f"{self.projector.key} v{self.version}"

    def save(self, *args, **kwargs):
        if self.pk:
            existing = WorkflowProjectorVersion.objects.filter(pk=self.pk).first()
            if existing is not None:
                immutable_fields = (
                    "projector_id", "version", "provider_driver_type", "operation",
                    "output_kind", "engine_key", "engine_version", "definition",
                    "definition_schema_version", "definition_size_bytes",
                    "definition_sha256", "published_at", "published_by_id",
                )
                if existing.status != self.Status.DRAFT and any(
                    getattr(existing, field) != getattr(self, field)
                    for field in immutable_fields
                ):
                    raise ValidationError(
                        "published/retired WorkflowProjectorVersion 内容不可修改",
                    )
                allowed_transitions = {
                    self.Status.DRAFT: {self.Status.DRAFT, self.Status.PUBLISHED},
                    self.Status.PUBLISHED: {
                        self.Status.PUBLISHED, self.Status.RETIRED,
                    },
                    self.Status.RETIRED: {self.Status.RETIRED},
                }
                if self.status not in allowed_transitions[existing.status]:
                    raise ValidationError("WorkflowProjectorVersion 生命周期逆转无效")
        if self.is_current and self.status != self.Status.PUBLISHED:
            raise ValidationError("只有 published WorkflowProjectorVersion 可以是 current")
        return super().save(*args, **kwargs)

    def delete(self, *args, **kwargs):
        raise ValidationError(
            "WorkflowProjectorVersion 不能直接删除；未发布草稿请使用受控放弃动作",
        )

    def discard(self, *args, **kwargs):
        if self.status != self.Status.DRAFT or self.is_current:
            raise ValidationError("只有未发布且非 current 的 Projector 草稿可以放弃")
        return super().delete(*args, **kwargs)


class ImmutableComfyWorkflowSourceQuerySet(models.QuerySet):
    def update(self, **kwargs):
        raise ValidationError("ComfyWorkflowSource 不允许批量更新")

    def delete(self):
        raise ValidationError("ComfyWorkflowSource 不允许批量删除")


class ImmutableComfyWorkflowSourceManager(
    models.Manager.from_queryset(ImmutableComfyWorkflowSourceQuerySet),
):
    pass


class ComfyWorkflowSource(UUIDTimeStampedModel):
    """Immutable import provenance for one exact ComfyUI userdata revision."""

    class ImportKind(models.TextChoices):
        CANDIDATE = "candidate", "候选来源"
        TEMPLATE_VERSION = "template_version", "模板版本"
        EXECUTION_SNAPSHOT = "execution_snapshot", "执行快照"
        IGNORED = "ignored", "明确忽略"

    source_deployment = models.ForeignKey(
        ModelDeployment,
        on_delete=models.PROTECT,
        related_name="comfy_workflow_sources",
    )
    source_scope_schema_version = models.PositiveSmallIntegerField(default=1)
    source_scope_sha256 = models.CharField(max_length=64)
    source_path = models.CharField(max_length=500)
    source_path_key = models.CharField(max_length=500)
    source_path_sha256 = models.CharField(max_length=64)
    source_sha256 = models.CharField(max_length=64)
    source_size_bytes = models.PositiveIntegerField()
    source_workflow_id = models.CharField(max_length=191, blank=True)
    source_document = models.JSONField(default=dict, blank=True)
    source_document_schema_version = models.PositiveIntegerField(default=1)
    source_document_size_bytes = models.PositiveIntegerField(default=2)
    source_document_sha256 = models.CharField(
        max_length=64,
        default="44136fa355b3678a1146ad16f7e8649e94fb4fc21fe77e8310c060f61caaff8a",
    )
    import_contract_sha256 = models.CharField(max_length=64)
    import_kind = models.CharField(max_length=24, choices=ImportKind.choices)
    converted_graph_sha256 = models.CharField(max_length=64, blank=True)
    conversion_report = models.JSONField(default=dict, blank=True)
    conversion_report_schema_version = models.PositiveIntegerField(default=1)
    conversion_report_size_bytes = models.PositiveIntegerField(default=2)
    conversion_report_sha256 = models.CharField(max_length=64)
    workflow_version = models.ForeignKey(
        WorkflowTemplateVersion,
        null=True,
        blank=True,
        on_delete=models.PROTECT,
        related_name="comfy_sources",
    )
    generation_task = models.ForeignKey(
        GenerationTask,
        null=True,
        blank=True,
        on_delete=models.PROTECT,
        related_name="comfy_sources",
    )
    imported_by = models.ForeignKey(
        settings.AUTH_USER_MODEL,
        on_delete=models.PROTECT,
        related_name="imported_comfy_workflow_sources",
    )
    objects = ImmutableComfyWorkflowSourceManager()

    class Meta:
        ordering = ("source_scope_sha256", "source_path_key", "-created_at")
        constraints = [
            models.UniqueConstraint(
                fields=(
                    "source_scope_schema_version", "source_scope_sha256",
                    "source_path_sha256", "source_sha256", "import_contract_sha256",
                ),
                name="uq_comfy_src_scope_contract",
            ),
            models.CheckConstraint(
                condition=(
                    Q(
                        import_kind="template_version",
                        workflow_version__isnull=False,
                        generation_task__isnull=True,
                    )
                    | Q(import_kind="execution_snapshot", workflow_version__isnull=True)
                    | Q(
                        import_kind="candidate",
                        workflow_version__isnull=True,
                        generation_task__isnull=True,
                    )
                    | Q(
                        import_kind="ignored",
                        workflow_version__isnull=True,
                        generation_task__isnull=True,
                    )
                ),
                name="valid_comfy_workflow_source_target",
            ),
        ]
        indexes = [
            models.Index(
                fields=(
                    "source_scope_schema_version", "source_scope_sha256",
                    "source_path_sha256", "-created_at",
                ),
                name="studio_comfy_scope_path_idx",
            ),
        ]

    def __str__(self) -> str:
        return f"{self.source_path}@{self.source_sha256[:12]}"

    def save(self, *args, **kwargs):
        if self.pk and ComfyWorkflowSource.objects.filter(pk=self.pk).exists():
            raise ValidationError("ComfyWorkflowSource 是不可变来源事实")
        return super().save(*args, **kwargs)

    def delete(self, *args, **kwargs):
        raise ValidationError("ComfyWorkflowSource 是不可变来源事实")


class ComfyWorkflowPreviewReceipt(UUIDTimeStampedModel):
    """One actor's committed preview request; never a new execution contract."""

    owner = models.ForeignKey(settings.AUTH_USER_MODEL, on_delete=models.CASCADE)
    request_key = models.CharField(max_length=191)
    deployment = models.ForeignKey(ModelDeployment, on_delete=models.CASCADE)
    contract_draft = models.ForeignKey("ComfyWorkflowContractDraft", on_delete=models.CASCADE)
    request_sha256 = models.CharField(max_length=64)
    response = models.JSONField()
    response_schema_version = models.PositiveIntegerField(default=1)
    response_size_bytes = models.PositiveIntegerField()
    response_sha256 = models.CharField(max_length=64)

    class Meta:
        constraints = [models.UniqueConstraint(fields=("owner", "request_key"), name="uk_comfy_preview_receipt_key")]

    def save(self, *args, **kwargs):
        if self.pk and type(self).objects.filter(pk=self.pk).exists():
            raise ValidationError("工作流合同请求回执不可修改")
        return super().save(*args, **kwargs)


class ImmutableComfyWorkflowContractDraftQuerySet(models.QuerySet):
    def update(self, **kwargs):
        raise ValidationError("ComfyWorkflowContractDraft 不允许批量更新")

    def delete(self):
        raise ValidationError("ComfyWorkflowContractDraft 不允许批量删除")


class ImmutableComfyWorkflowContractDraftManager(
    models.Manager.from_queryset(ImmutableComfyWorkflowContractDraftQuerySet),
):
    pass


class ComfyWorkflowContractDraft(UUIDTimeStampedModel):
    """Immutable, non-executable projection of one managed candidate revision."""

    class AdmissionTier(models.TextChoices):
        CANDIDATE = "candidate", "候选"
        GENERIC_FIXED = "generic_fixed", "Generic 固定合同"
        PROJECTED_DYNAMIC = "projected_dynamic", "动态投影合同"

    class Actionability(models.TextChoices):
        CANDIDATE_ONLY = "candidate_only", "仅候选"
        READY_FOR_EXECUTABLE_IMPORT = (
            "ready_for_executable_import", "可提升为工作流版本",
        )

    candidate_source = models.ForeignKey(
        ComfyWorkflowSource,
        on_delete=models.PROTECT,
        related_name="contract_drafts",
    )
    revision = models.PositiveIntegerField(validators=[MinValueValidator(1)])
    primary_output_node_id = models.CharField(max_length=32)
    model_profile_version = models.ForeignKey(
        ModelProfileVersion,
        null=True,
        blank=True,
        on_delete=models.PROTECT,
        related_name="comfy_workflow_contract_drafts",
    )
    projector_version = models.ForeignKey(
        WorkflowProjectorVersion,
        null=True,
        blank=True,
        on_delete=models.PROTECT,
        related_name="contract_drafts",
    )
    object_info_schema_sha256 = models.CharField(max_length=64)
    converted_graph_sha256 = models.CharField(max_length=64, blank=True, default="")
    structural_contract_sha256 = models.CharField(max_length=64, blank=True, default="")
    projection = models.JSONField(default=dict)
    projection_schema_version = models.PositiveIntegerField(default=1)
    projection_size_bytes = models.PositiveIntegerField(default=2)
    projection_sha256 = models.CharField(max_length=64)
    admission_tier = models.CharField(max_length=24, choices=AdmissionTier.choices)
    actionability = models.CharField(max_length=40, choices=Actionability.choices)
    created_by = models.ForeignKey(
        settings.AUTH_USER_MODEL,
        on_delete=models.PROTECT,
        related_name="created_comfy_workflow_contract_drafts",
    )
    objects = ImmutableComfyWorkflowContractDraftManager()

    class Meta:
        ordering = ("candidate_source", "-revision")
        constraints = [
            models.UniqueConstraint(
                fields=("candidate_source", "revision"),
                name="unique_comfy_workflow_contract_draft_revision",
            ),
            models.UniqueConstraint(
                fields=("candidate_source", "projection_sha256"),
                name="unique_comfy_workflow_contract_draft_projection",
            ),
        ]
        indexes = [
            models.Index(
                fields=("candidate_source", "actionability", "-created_at"),
                name="st_comfy_contract_act_idx",
            ),
        ]

    def clean(self):
        super().clean()
        if (
            self.candidate_source_id
            and self.candidate_source.import_kind
            != ComfyWorkflowSource.ImportKind.CANDIDATE
        ):
            raise ValidationError("合同草稿必须绑定 candidate 来源")

    def save(self, *args, **kwargs):
        if self.pk and ComfyWorkflowContractDraft.objects.filter(pk=self.pk).exists():
            raise ValidationError("ComfyWorkflowContractDraft 是不可变合同草稿")
        return super().save(*args, **kwargs)

    def delete(self, *args, **kwargs):
        raise ValidationError("ComfyWorkflowContractDraft 是不可变合同草稿")


class WorkflowSmokeAttemptQuerySet(models.QuerySet):
    _IMMUTABLE_EXECUTION_FIELDS = frozenset((
        "workflow_version", "workflow_version_id",
        "deployment", "deployment_id", "created_by", "created_by_id",
        "submission_token",
        "scope", "execution_instance_version", "execution_instance_version_id",
        "graph_sha256", "execution_contract_sha256",
        "validation_evidence_sha256", "target_sha256",
        "fixture_values", "fixture_values_schema_version",
        "fixture_values_size_bytes", "fixture_values_sha256", "fixture_sha256",
    ))

    def update(self, **kwargs):
        if self._IMMUTABLE_EXECUTION_FIELDS & set(kwargs):
            raise ValidationError("WorkflowSmokeAttempt 冻结执行合同不允许批量更新")
        return super().update(**kwargs)

    def delete(self):
        raise ValidationError("WorkflowSmokeAttempt 是持久执行事实，不能删除")


class WorkflowSmokeAttemptManager(
    models.Manager.from_queryset(WorkflowSmokeAttemptQuerySet),
):
    pass


class ExecutionSmokeScope(models.TextChoices):
    RESOURCE = "resource", "基础资源验收"
    INSTANCE = "instance", "执行实例组合验收"


class WorkflowSmokeAttempt(UUIDTimeStampedModel):
    """Durable, single-submit execution of one workflow smoke test."""

    class Status(models.TextChoices):
        PREPARING = "preparing", "准备请求"
        QUEUED = "queued", "排队中"
        SUBMITTING = "submitting", "正在提交"
        RUNNING = "running", "执行中"
        CANCEL_REQUESTED = "cancel_requested", "取消中"
        SUCCEEDED = "succeeded", "成功"
        FAILED = "failed", "失败"
        CANCELLED = "cancelled", "已取消"
        UNCERTAIN = "uncertain", "远端状态不确定"

    scope = models.CharField(max_length=16, choices=ExecutionSmokeScope.choices, default=ExecutionSmokeScope.RESOURCE)
    idempotency_key = models.CharField(max_length=191, blank=True)
    allow_external_calls = models.BooleanField(default=False)
    budget_limit = models.DecimalField(max_digits=14, decimal_places=6, null=True, blank=True)
    estimated_cost = models.DecimalField(max_digits=14, decimal_places=6, null=True, blank=True)
    actual_cost = models.DecimalField(max_digits=14, decimal_places=6, null=True, blank=True)
    cost_currency = models.CharField(max_length=12, blank=True)
    execution_instance_version = models.ForeignKey("ModelExecutionInstanceVersion", on_delete=models.PROTECT,
        null=True, blank=True, db_index=False, related_name="workflow_smoke_attempts")
    workflow_version = models.ForeignKey(
        WorkflowTemplateVersion,
        on_delete=models.PROTECT,
        related_name="smoke_attempts",
    )
    deployment = models.ForeignKey(
        ModelDeployment,
        on_delete=models.PROTECT,
        related_name="workflow_smoke_attempts",
    )
    created_by = models.ForeignKey(
        settings.AUTH_USER_MODEL,
        on_delete=models.PROTECT,
        related_name="workflow_smoke_attempts",
    )
    status = models.CharField(max_length=24, choices=Status.choices, default=Status.QUEUED)
    submission_token = models.UUIDField(default=uuid.uuid4, editable=False, unique=True)
    provider_request_id = models.CharField(max_length=191, blank=True)
    graph_sha256 = models.CharField(max_length=64)
    execution_contract_sha256 = models.CharField(max_length=64)
    validation_evidence_sha256 = models.CharField(max_length=64)
    target_sha256 = models.CharField(max_length=64)
    fixture_values = models.JSONField(default=dict, blank=True)
    fixture_values_schema_version = models.PositiveIntegerField(default=2)
    fixture_values_size_bytes = models.PositiveIntegerField(default=2)
    fixture_values_sha256 = models.CharField(max_length=64, blank=True)
    fixture_sha256 = models.CharField(max_length=64, blank=True)
    staged_inputs = models.JSONField(default=list, blank=True)
    claim_token = models.UUIDField(null=True, blank=True, editable=False, db_index=True)
    worker_id = models.CharField(max_length=160, blank=True)
    claimed_at = models.DateTimeField(null=True, blank=True)
    heartbeat_at = models.DateTimeField(null=True, blank=True)
    submission_started_at = models.DateTimeField(null=True, blank=True)
    submitted_at = models.DateTimeField(null=True, blank=True)
    last_polled_at = models.DateTimeField(null=True, blank=True)
    next_poll_at = models.DateTimeField(null=True, blank=True, db_index=True)
    poll_count = models.PositiveIntegerField(default=0)
    remote_status = models.CharField(max_length=120, blank=True)
    deadline_started_at = models.DateTimeField(null=True, blank=True)
    hard_deadline_at = models.DateTimeField(null=True, blank=True, db_index=True)
    idle_deadline_at = models.DateTimeField(null=True, blank=True, db_index=True)
    last_meaningful_progress_at = models.DateTimeField(null=True, blank=True)
    progress_marker = models.CharField(max_length=191, blank=True)
    cancel_requested_at = models.DateTimeField(null=True, blank=True)
    cancel_submitted_at = models.DateTimeField(null=True, blank=True)
    finished_at = models.DateTimeField(null=True, blank=True)
    error_code = models.CharField(max_length=100, blank=True)
    error_message = models.TextField(blank=True)
    result = models.JSONField(default=dict, blank=True)
    objects = WorkflowSmokeAttemptManager()
    result_schema_version = models.PositiveIntegerField(default=1)
    result_size_bytes = models.PositiveIntegerField(default=2)
    result_sha256 = models.CharField(max_length=64, blank=True)
    evidence_expires_at = models.DateTimeField(null=True, blank=True)

    class Meta:
        ordering = ("-created_at",)
        constraints = [
            models.CheckConstraint(condition=(Q(scope="resource", execution_instance_version__isnull=True)
                | Q(scope="instance", execution_instance_version__isnull=False)), name="ck_workflow_smoke_scope"),
            models.UniqueConstraint(fields=("execution_instance_version", "idempotency_key"), condition=Q(scope="instance"), name="uq_workflow_instance_smoke_key"),
            models.CheckConstraint(condition=Q(budget_limit__isnull=True) | Q(budget_limit__gte=0), name="ck_workflow_smoke_budget"),
            models.UniqueConstraint(
                fields=("workflow_version", "deployment"),
                condition=Q(status__in=(
                    "preparing", "queued", "submitting", "running", "cancel_requested",
                )),
                name="one_active_workflow_smoke",
            ),
        ]
        indexes = [models.Index(
            fields=("status", "next_poll_at"),
            name="studio_work_status_70a205_idx",
        ), models.Index(fields=("execution_instance_version", "-finished_at", "-id"),
            condition=Q(execution_instance_version__isnull=False), name="idx_workflow_instance_smoke")]

    _IMMUTABLE_EXECUTION_FIELDS = (
        "workflow_version_id", "deployment_id", "created_by_id", "submission_token",
        "scope", "execution_instance_version_id", "idempotency_key",
        "graph_sha256", "execution_contract_sha256",
        "validation_evidence_sha256", "target_sha256",
        "fixture_values", "fixture_values_schema_version",
        "fixture_values_size_bytes", "fixture_values_sha256", "fixture_sha256",
    )

    def save(self, *args, **kwargs):
        if self.pk:
            prior = WorkflowSmokeAttempt.objects.filter(pk=self.pk).values(
                *self._IMMUTABLE_EXECUTION_FIELDS,
            ).first()
            if prior is not None and any(
                prior[field] != getattr(self, field)
                for field in self._IMMUTABLE_EXECUTION_FIELDS
            ):
                raise ValidationError("WorkflowSmokeAttempt 冻结合同是不可变执行事实")
        return super().save(*args, **kwargs)

    def delete(self, *args, **kwargs):
        raise ValidationError("WorkflowSmokeAttempt 是持久执行事实，不能删除")


class AppendOnlyWorkflowSmokeResolutionQuerySet(models.QuerySet):
    def update(self, **kwargs):
        raise ValidationError("WorkflowSmokeAttemptResolution 不允许更新")

    def delete(self):
        raise ValidationError("WorkflowSmokeAttemptResolution 不允许删除")


class AppendOnlyWorkflowSmokeResolutionManager(
    models.Manager.from_queryset(AppendOnlyWorkflowSmokeResolutionQuerySet),
):
    pass


class WorkflowSmokeAttemptResolution(UUIDTimeStampedModel):
    """Append-only operator evidence resolving an uncertain remote execution."""

    class Outcome(models.TextChoices):
        CONFIRMED_NOT_SUBMITTED = "confirmed_not_submitted", "确认未提交"
        CONFIRMED_REMOTE_FAILED = "confirmed_remote_failed", "确认远端失败"
        CONFIRMED_REMOTE_SUCCEEDED = "confirmed_remote_succeeded", "确认远端成功"

    attempt = models.ForeignKey(
        WorkflowSmokeAttempt,
        on_delete=models.PROTECT,
        related_name="resolutions",
    )
    revision = models.PositiveIntegerField(validators=[MinValueValidator(1)])
    outcome = models.CharField(max_length=40, choices=Outcome.choices)
    provider_request_id_sha256 = models.CharField(max_length=64, blank=True)
    evidence = models.JSONField(default=dict)
    evidence_schema_version = models.PositiveIntegerField(default=1)
    evidence_size_bytes = models.PositiveIntegerField(default=2)
    evidence_sha256 = models.CharField(max_length=64)
    note = models.TextField()
    decided_by = models.ForeignKey(
        settings.AUTH_USER_MODEL,
        on_delete=models.PROTECT,
        related_name="workflow_smoke_attempt_resolutions",
    )
    objects = AppendOnlyWorkflowSmokeResolutionManager()

    class Meta:
        ordering = ("attempt", "revision")
        constraints = [models.UniqueConstraint(
            fields=("attempt", "revision"),
            name="unique_workflow_smoke_resolution",
        )]

    def save(self, *args, **kwargs):
        if self.pk and WorkflowSmokeAttemptResolution.objects.filter(pk=self.pk).exists():
            raise ValidationError("WorkflowSmokeAttemptResolution 是不可变审计事实")
        return super().save(*args, **kwargs)

    def delete(self, *args, **kwargs):
        raise ValidationError("WorkflowSmokeAttemptResolution 是不可变审计事实")


class AppendOnlyWorkflowSmokeBindingQuerySet(models.QuerySet):
    def update(self, **kwargs):
        raise ValidationError("WorkflowSmokeAssetBinding 不允许更新")

    def delete(self):
        raise ValidationError("WorkflowSmokeAssetBinding 不允许删除")


class AppendOnlyWorkflowSmokeBindingManager(
    models.Manager.from_queryset(AppendOnlyWorkflowSmokeBindingQuerySet),
):
    pass


class WorkflowSmokeAssetBinding(UUIDTimeStampedModel):
    """Immutable concrete Asset fact used by one workflow smoke attempt."""

    attempt = models.ForeignKey(
        WorkflowSmokeAttempt,
        on_delete=models.PROTECT,
        related_name="asset_bindings",
    )
    semantic_key = models.CharField(max_length=120)
    input_slot = models.PositiveIntegerField()
    asset = models.ForeignKey(
        Asset,
        on_delete=models.PROTECT,
        related_name="workflow_smoke_bindings",
    )
    media_kind = models.CharField(max_length=12)
    category = models.CharField(max_length=80)
    checksum = models.CharField(
        max_length=64,
        validators=[RegexValidator(r"^[0-9a-f]{64}$")],
    )
    mime_type = models.CharField(max_length=120)
    size_bytes = models.PositiveBigIntegerField()
    objects = AppendOnlyWorkflowSmokeBindingManager()

    class Meta:
        ordering = ("attempt", "media_kind", "input_slot", "semantic_key")
        constraints = [
            models.UniqueConstraint(
                fields=("attempt", "semantic_key"),
                name="unique_workflow_smoke_semantic",
            ),
            models.UniqueConstraint(
                fields=("attempt", "media_kind", "input_slot"),
                name="unique_workflow_smoke_slot",
            ),
            models.CheckConstraint(
                condition=Q(input_slot__gte=1),
                name="workflow_smoke_slot_positive",
            ),
            models.CheckConstraint(
                condition=Q(size_bytes__gte=1),
                name="workflow_smoke_asset_size_positive",
            ),
            models.CheckConstraint(
                condition=Q(media_kind__in=("image", "video", "audio")),
                name="workflow_smoke_media_kind_valid",
            ),
        ]

    def save(self, *args, **kwargs):
        if self.pk and WorkflowSmokeAssetBinding.objects.filter(pk=self.pk).exists():
            raise ValidationError("WorkflowSmokeAssetBinding 是不可变烟测事实")
        return super().save(*args, **kwargs)

    def delete(self, *args, **kwargs):
        raise ValidationError("WorkflowSmokeAssetBinding 是不可变烟测事实")


class WorkflowSemanticBinding(UUIDTimeStampedModel):
    workflow_version = models.ForeignKey(
        WorkflowTemplateVersion,
        on_delete=models.CASCADE,
        related_name="semantic_bindings",
    )
    semantic_key = models.CharField(max_length=100)
    node_id = models.CharField(max_length=120)
    input_name = models.CharField(max_length=120)
    value_type = models.CharField(max_length=32, default="string")
    is_required = models.BooleanField(default=True)

    class Meta:
        ordering = ("workflow_version", "semantic_key")
        constraints = [
            models.UniqueConstraint(
                fields=("workflow_version", "node_id", "input_name"),
                name="unique_workflow_semantic_target",
            ),
        ]

    def save(self, *args, **kwargs):
        changed = False
        if not self._state.adding and self.pk:
            previous = type(self).objects.filter(pk=self.pk).values(
                "semantic_key", "node_id", "input_name", "value_type", "is_required",
            ).first()
            current = {
                "semantic_key": self.semantic_key,
                "node_id": self.node_id,
                "input_name": self.input_name,
                "value_type": self.value_type,
                "is_required": self.is_required,
            }
            changed = previous is not None and previous != current
        result = super().save(*args, **kwargs)
        if changed:
            self._invalidate_workflow_evidence()
        return result

    def delete(self, *args, **kwargs):
        workflow_version_id = self.workflow_version_id
        result = super().delete(*args, **kwargs)
        self._invalidate_workflow_evidence(workflow_version_id)
        return result

    def _invalidate_workflow_evidence(self, workflow_version_id=None):
        WorkflowTemplateVersion.objects.filter(
            pk=workflow_version_id or self.workflow_version_id,
        ).update(
            status=WorkflowTemplateVersion.Status.DRAFT,
            validation_state=WorkflowTemplateVersion.ValidationState.NOT_RUN,
            validation_message="semantic bindings 已变更；必须导入新版本并重新校验。",
            smoke_test_state=WorkflowTemplateVersion.ValidationState.NOT_RUN,
            smoke_test_message="semantic bindings 已变更；旧 smoke-test 证据失效。",
            validated_deployment=None,
            validation_evidence={},
            validation_evidence_sha256="",
            smoke_test_evidence={},
            smoke_test_evidence_sha256="",
            is_current=False,
            updated_at=timezone.now(),
        )


class WorkflowModelRequirement(UUIDTimeStampedModel):
    workflow_version = models.ForeignKey(
        WorkflowTemplateVersion,
        on_delete=models.CASCADE,
        related_name="model_requirements",
    )
    role = models.CharField(max_length=24, choices=ModelArtifact.Role.choices)
    alias = models.CharField(max_length=120)
    artifact = models.ForeignKey(
        ModelArtifact,
        null=True,
        blank=True,
        on_delete=models.PROTECT,
        related_name="workflow_requirements",
    )
    expected_filename = models.CharField(max_length=500)
    expected_sha256 = models.CharField(max_length=64, blank=True)
    is_required = models.BooleanField(default=True)

    class Meta:
        ordering = ("workflow_version", "role", "alias")
        constraints = [
            models.UniqueConstraint(
                fields=("workflow_version", "role", "alias"),
                name="unique_workflow_model_requirement",
            ),
        ]

    def save(self, *args, **kwargs):
        changed = False
        if not self._state.adding and self.pk:
            previous = type(self).objects.filter(pk=self.pk).values(
                "role", "alias", "artifact_id", "expected_filename",
                "expected_sha256", "is_required",
            ).first()
            current = {
                "role": self.role,
                "alias": self.alias,
                "artifact_id": self.artifact_id,
                "expected_filename": self.expected_filename,
                "expected_sha256": self.expected_sha256,
                "is_required": self.is_required,
            }
            changed = previous is not None and previous != current
        result = super().save(*args, **kwargs)
        if changed:
            self._invalidate_workflow_evidence()
        return result

    def delete(self, *args, **kwargs):
        workflow_version_id = self.workflow_version_id
        result = super().delete(*args, **kwargs)
        self._invalidate_workflow_evidence(workflow_version_id)
        return result

    def _invalidate_workflow_evidence(self, workflow_version_id=None):
        WorkflowTemplateVersion.objects.filter(
            pk=workflow_version_id or self.workflow_version_id,
        ).update(
            status=WorkflowTemplateVersion.Status.DRAFT,
            validation_state=WorkflowTemplateVersion.ValidationState.NOT_RUN,
            validation_message="model requirements 已变更；必须导入新版本并重新校验。",
            smoke_test_state=WorkflowTemplateVersion.ValidationState.NOT_RUN,
            smoke_test_message="model requirements 已变更；旧 smoke-test 证据失效。",
            validated_deployment=None,
            validation_evidence={},
            validation_evidence_sha256="",
            smoke_test_evidence={},
            smoke_test_evidence_sha256="",
            is_current=False,
            updated_at=timezone.now(),
        )


class ConfigDefinition(UUIDTimeStampedModel):
    class ConfigurationClass(models.TextChoices):
        STANDARD = "standard", "普通配置"
        SECURITY_BOUNDARY = "security_boundary", "安全边界"

    class ValueType(models.TextChoices):
        BOOLEAN = "boolean", "布尔"
        INTEGER = "integer", "整数"
        NUMBER = "number", "数字"
        STRING = "string", "字符串"
        OBJECT = "object", "对象"
        ARRAY = "array", "数组"

    key = models.CharField(
        max_length=160,
        unique=True,
        validators=[RegexValidator(r"^[a-z][a-z0-9_.-]{1,159}$")],
    )
    domain = models.CharField(max_length=80)
    configuration_class = models.CharField(
        max_length=32,
        choices=ConfigurationClass.choices,
        default=ConfigurationClass.STANDARD,
        db_index=True,
    )
    value_type = models.CharField(max_length=16, choices=ValueType.choices)
    description = models.TextField(blank=True)
    unit = models.CharField(max_length=40, blank=True)
    impact_modules = models.JSONField(default=list, blank=True)
    restart_required = models.BooleanField(default=False)
    allowed_scopes = models.CharField(max_length=80, default="system,project,run")
    allowed_default_sources = models.CharField(max_length=40, blank=True)
    is_required = models.BooleanField(default=False)
    is_secret = models.BooleanField(default=False)
    is_active = models.BooleanField(default=True)
    needs_review = models.BooleanField(default=False)
    review_reason = models.CharField(max_length=500, blank=True)
    value_schema = models.JSONField(default=dict, blank=True)
    value_schema_version = models.PositiveIntegerField(default=1)
    value_schema_size_bytes = models.PositiveIntegerField(default=2)
    value_schema_sha256 = models.CharField(max_length=64)
    initial_value = models.JSONField(null=True, blank=True)
    initial_value_schema_version = models.PositiveIntegerField(default=1)
    initial_value_size_bytes = models.PositiveIntegerField(default=0)
    initial_value_sha256 = models.CharField(max_length=64, blank=True)

    class Meta:
        ordering = ("domain", "key")
        indexes = [
            models.Index(fields=("domain", "is_active")),
            models.Index(fields=("configuration_class", "is_active")),
        ]

    def __str__(self) -> str:
        return self.key


class ConfigRevision(UUIDTimeStampedModel):
    class ScopeType(models.TextChoices):
        SYSTEM = "system", "系统"
        PROJECT = "project", "项目"
        RUN = "run", "运行"

    class Status(models.TextChoices):
        DRAFT = "draft", "草稿"
        PUBLISHED = "published", "已发布"
        SUPERSEDED = "superseded", "已替代"

    class ActivationState(models.TextChoices):
        INACTIVE = "inactive", "未激活"
        PENDING = "pending_activation", "等待重启激活"
        ACTIVE = "active", "已激活"
        FAILED = "failed", "激活失败"

    scope_type = models.CharField(max_length=16, choices=ScopeType.choices)
    project = models.ForeignKey(
        SeriesProject,
        null=True,
        blank=True,
        on_delete=models.CASCADE,
        related_name="config_revisions",
    )
    run = models.ForeignKey(
        "ProductionRun",
        null=True,
        blank=True,
        on_delete=models.CASCADE,
        related_name="run_config_revisions",
    )
    revision = models.PositiveIntegerField(validators=[MinValueValidator(1)])
    status = models.CharField(max_length=16, choices=Status.choices, default=Status.DRAFT)
    based_on_revision = models.ForeignKey(
        "self",
        null=True,
        blank=True,
        on_delete=models.PROTECT,
        related_name="derived_revisions",
    )
    activation_state = models.CharField(
        max_length=24,
        choices=ActivationState.choices,
        default=ActivationState.INACTIVE,
    )
    activated_at = models.DateTimeField(null=True, blank=True)
    activation_error = models.CharField(max_length=500, blank=True)
    activation_requested_boot_id = models.CharField(max_length=64, blank=True)
    values_sha256 = models.CharField(max_length=64, blank=True)
    change_summary = models.CharField(max_length=500, blank=True)
    created_by = models.ForeignKey(
        settings.AUTH_USER_MODEL,
        null=True,
        blank=True,
        on_delete=models.PROTECT,
        related_name="created_config_revisions",
    )
    published_at = models.DateTimeField(null=True, blank=True)

    class Meta:
        ordering = ("scope_type", "-revision")
        constraints = [
            models.CheckConstraint(
                condition=(
                    Q(scope_type="system", project__isnull=True, run__isnull=True)
                    | Q(scope_type="project", project__isnull=False, run__isnull=True)
                    | Q(scope_type="run", project__isnull=True, run__isnull=False)
                ),
                name="config_revision_scope_consistent",
            ),
            models.UniqueConstraint(
                fields=("revision",),
                condition=Q(scope_type="system"),
                name="unique_system_config_revision",
            ),
            models.UniqueConstraint(
                fields=("project", "revision"),
                condition=Q(scope_type="project"),
                name="unique_project_config_revision",
            ),
            models.UniqueConstraint(
                fields=("run", "revision"),
                condition=Q(scope_type="run"),
                name="unique_run_config_revision",
            ),
            models.UniqueConstraint(
                fields=("scope_type",),
                condition=Q(scope_type="system", activation_state="active"),
                name="one_active_system_config",
            ),
            models.UniqueConstraint(
                fields=("project",),
                condition=Q(scope_type="project", activation_state="active"),
                name="one_active_project_config",
            ),
            models.UniqueConstraint(
                fields=("run",),
                condition=Q(scope_type="run", activation_state="active"),
                name="one_active_run_config",
            ),
        ]


class ConfigValue(UUIDTimeStampedModel):
    revision = models.ForeignKey(
        ConfigRevision,
        on_delete=models.CASCADE,
        related_name="values",
    )
    definition = models.ForeignKey(
        ConfigDefinition,
        on_delete=models.PROTECT,
        related_name="values",
    )
    value = models.JSONField(null=True, blank=True)
    value_schema_version = models.PositiveIntegerField(default=1)
    value_size_bytes = models.PositiveIntegerField(default=0)
    value_sha256 = models.CharField(max_length=64, blank=True)
    credential_version = models.ForeignKey(
        SecretVersion,
        null=True,
        blank=True,
        on_delete=models.PROTECT,
        related_name="config_values",
    )

    class Meta:
        ordering = ("definition__key",)
        constraints = [
            models.UniqueConstraint(
                fields=("revision", "definition"),
                name="unique_config_value_per_revision",
            ),
        ]


class HostCapabilitySnapshot(UUIDTimeStampedModel):
    class ProbeStatus(models.TextChoices):
        SUCCEEDED = "succeeded", "成功"
        PARTIAL = "partial", "部分成功"
        FAILED = "failed", "失败"

    host = models.ForeignKey(
        RuntimeHost,
        on_delete=models.CASCADE,
        related_name="capability_snapshots",
    )
    probe_attempt = models.OneToOneField(
        RuntimeHostProbeAttempt,
        null=True,
        blank=True,
        on_delete=models.PROTECT,
        related_name="final_snapshot",
    )
    revision = models.PositiveIntegerField(validators=[MinValueValidator(1)])
    probe_status = models.CharField(max_length=16, choices=ProbeStatus.choices)
    cpu_architecture = models.CharField(max_length=80, blank=True)
    cpu_name = models.CharField(max_length=200, blank=True)
    logical_cpu_count = models.PositiveIntegerField(default=0)
    memory_total_bytes = models.PositiveBigIntegerField(default=0)
    disk_total_bytes = models.PositiveBigIntegerField(default=0)
    disk_free_bytes = models.PositiveBigIntegerField(default=0)
    gpu_count = models.PositiveIntegerField(default=0)
    ffmpeg_available = models.BooleanField(default=False)
    ffprobe_available = models.BooleanField(default=False)
    comfyui_version = models.CharField(max_length=120, blank=True)
    details = models.JSONField(default=dict, blank=True)
    details_schema_version = models.PositiveIntegerField(default=1)
    details_size_bytes = models.PositiveIntegerField(default=2)
    details_sha256 = models.CharField(max_length=64)
    captured_at = models.DateTimeField(default=timezone.now)
    expires_at = models.DateTimeField(null=True, blank=True)

    class Meta:
        ordering = ("host", "-revision")
        constraints = [
            models.UniqueConstraint(
                fields=("host", "revision"),
                name="unique_host_capability_revision",
            ),
        ]


class ModelDeploymentCapabilitySnapshot(UUIDTimeStampedModel):
    """Immutable evidence from a real deployment probe or smoke-test."""

    prompt_profile_version = models.ForeignKey(
        "PromptProfileVersion", null=True, blank=True, on_delete=models.PROTECT,
        related_name="deployment_smoke_snapshots",
    )
    prompt_profile_contract_sha256 = models.CharField(max_length=64, blank=True)

    class Status(models.TextChoices):
        PASSED = "passed", "通过"
        FAILED = "failed", "失败"

    class ProbeKind(models.TextChoices):
        HEALTH = "health", "健康探测"
        SMOKE = "smoke", "真实烟测"

    deployment = models.ForeignKey(
        ModelDeployment,
        on_delete=models.PROTECT,
        related_name="capability_snapshots",
    )
    revision = models.PositiveIntegerField(validators=[MinValueValidator(1)])
    probe_kind = models.CharField(max_length=16, choices=ProbeKind.choices)
    status = models.CharField(max_length=16, choices=Status.choices)
    target_sha256 = models.CharField(max_length=64)
    provider_config_sha256 = models.CharField(max_length=64)
    deployment_config_sha256 = models.CharField(max_length=64)
    credential_binding_sha256 = models.CharField(max_length=64, blank=True)
    operations = models.JSONField(default=list)
    operations_schema_version = models.PositiveIntegerField(default=1)
    operations_size_bytes = models.PositiveIntegerField(default=2)
    operations_sha256 = models.CharField(max_length=64)
    evidence = models.JSONField(default=dict)
    evidence_schema_version = models.PositiveIntegerField(default=1)
    evidence_size_bytes = models.PositiveIntegerField(default=2)
    evidence_sha256 = models.CharField(max_length=64)
    captured_at = models.DateTimeField(default=timezone.now)
    expires_at = models.DateTimeField()

    class Meta:
        ordering = ("deployment", "-revision")
        constraints = [
            models.UniqueConstraint(
                fields=("deployment", "revision"),
                name="unique_deployment_capability_revision",
            ),
        ]
        indexes = [models.Index(fields=("deployment", "probe_kind", "status", "expires_at"))]


class ModelDeploymentSmokeAttemptQuerySet(models.QuerySet):
    _IDENTITY_FIELDS = frozenset(("scope", "execution_instance_version", "execution_instance_version_id",
        "deployment", "deployment_id", "operation", "target_sha256"))

    def without_operator_resolution(self):
        # System transport observations are not decisions about remote state.
        return self.exclude(resolutions__outcome__in=ModelDeploymentSmokeAttemptResolution.OPERATOR_OUTCOMES)

    def update(self, **kwargs):
        if self._IDENTITY_FIELDS & set(kwargs):
            raise ValidationError("部署验收范围与执行身份不能批量改写")
        return super().update(**kwargs)


class ModelDeploymentSmokeAttempt(UUIDTimeStampedModel):
    """Durable single-submit boundary for one real deployment smoke request."""

    scope = models.CharField(max_length=16, choices=ExecutionSmokeScope.choices, default=ExecutionSmokeScope.RESOURCE)
    allow_external_calls = models.BooleanField(default=False)
    budget_limit = models.DecimalField(max_digits=14, decimal_places=6, null=True, blank=True)
    actual_cost = models.DecimalField(max_digits=14, decimal_places=6, null=True, blank=True)
    execution_instance_version = models.ForeignKey("ModelExecutionInstanceVersion", on_delete=models.PROTECT,
        null=True, blank=True, db_index=False, related_name="deployment_smoke_attempts")

    prompt_profile_version = models.ForeignKey(
        "PromptProfileVersion", null=True, blank=True, on_delete=models.PROTECT,
        related_name="deployment_smoke_attempts",
    )
    prompt_profile_contract_sha256 = models.CharField(max_length=64, blank=True)

    class Status(models.TextChoices):
        PREPARING = "preparing", "准备请求"
        QUEUED = "queued", "排队中"
        SUBMITTING = "submitting", "正在提交"
        SUBMITTED = "submitted", "已提交"
        CANCEL_REQUESTED = "cancel_requested", "取消中"
        SUCCEEDED = "succeeded", "成功"
        FAILED = "failed", "失败"
        CANCELLED = "cancelled", "已取消"
        UNCERTAIN = "uncertain", "远端状态不确定"

    deployment = models.ForeignKey(
        ModelDeployment,
        on_delete=models.PROTECT,
        related_name="smoke_attempts",
    )
    operation = models.CharField(max_length=100)
    status = models.CharField(max_length=24, choices=Status.choices)
    request_sha256 = models.CharField(max_length=64)
    target_sha256 = models.CharField(max_length=64)
    estimated_cost = models.DecimalField(
        max_digits=14, decimal_places=6, null=True, blank=True,
    )
    cost_currency = models.CharField(max_length=12, blank=True)
    provider_request_id_sha256 = models.CharField(max_length=64, blank=True)
    provider_request_id = models.CharField(max_length=191, blank=True)
    idempotency_key = models.CharField(max_length=191, blank=True)
    request_snapshot = models.JSONField(default=dict, blank=True)
    claim_token = models.UUIDField(null=True, blank=True, editable=False, db_index=True)
    worker_id = models.CharField(max_length=160, blank=True)
    claimed_at = models.DateTimeField(null=True, blank=True)
    heartbeat_at = models.DateTimeField(null=True, blank=True)
    submission_started_at = models.DateTimeField(null=True, blank=True)
    submitted_at = models.DateTimeField(null=True, blank=True)
    last_polled_at = models.DateTimeField(null=True, blank=True)
    next_poll_at = models.DateTimeField(null=True, blank=True, db_index=True)
    poll_count = models.PositiveIntegerField(default=0)
    remote_status = models.CharField(max_length=120, blank=True)
    deadline_started_at = models.DateTimeField(null=True, blank=True)
    hard_deadline_at = models.DateTimeField(null=True, blank=True, db_index=True)
    idle_deadline_at = models.DateTimeField(null=True, blank=True, db_index=True)
    last_meaningful_progress_at = models.DateTimeField(null=True, blank=True)
    progress_marker = models.CharField(max_length=191, blank=True)
    cancel_requested_at = models.DateTimeField(null=True, blank=True)
    cancel_submitted_at = models.DateTimeField(null=True, blank=True)
    error_code = models.CharField(max_length=100, blank=True)
    error_message = models.TextField(blank=True)
    finished_at = models.DateTimeField(null=True, blank=True)
    final_snapshot = models.OneToOneField(
        ModelDeploymentCapabilitySnapshot,
        null=True,
        blank=True,
        on_delete=models.PROTECT,
        related_name="smoke_attempt",
    )
    result = models.JSONField(default=dict, blank=True)
    result_schema_version = models.PositiveIntegerField(default=1)
    result_size_bytes = models.PositiveIntegerField(default=2)
    result_sha256 = models.CharField(max_length=64, blank=True)
    evidence_expires_at = models.DateTimeField(null=True, blank=True)
    objects = models.Manager.from_queryset(ModelDeploymentSmokeAttemptQuerySet)()

    def save(self, *args, **kwargs):
        fields = ("scope", "execution_instance_version_id", "deployment_id", "operation", "target_sha256")
        if self.pk:
            prior = type(self).objects.filter(pk=self.pk).values(*fields).first()
            if prior is not None and any(prior[key] != getattr(self, key) for key in fields):
                raise ValidationError("部署验收范围与执行身份不可变")
        return super().save(*args, **kwargs)

    class Meta:
        ordering = ("deployment", "-created_at")
        constraints = [models.CheckConstraint(condition=(Q(scope="resource", execution_instance_version__isnull=True)
            | Q(scope="instance", execution_instance_version__isnull=False)), name="ck_deployment_smoke_scope"), models.UniqueConstraint(
            fields=("execution_instance_version", "idempotency_key"), condition=Q(scope="instance"), name="uq_deploy_instance_smoke_key"), models.CheckConstraint(
            condition=Q(budget_limit__isnull=True) | Q(budget_limit__gte=0), name="ck_deployment_smoke_budget"), models.UniqueConstraint(
            fields=("deployment", "operation"),
            condition=Q(status__in=(
                "preparing", "queued", "submitting", "submitted", "cancel_requested",
            )),
            name="one_active_deployment_smoke",
        )]
        indexes = [models.Index(
            fields=("deployment", "operation", "status", "created_at"),
            name="studio_smoke_unresolved_idx",
        ), models.Index(fields=("execution_instance_version", "-finished_at", "-id"),
            condition=Q(execution_instance_version__isnull=False), name="idx_deploy_instance_smoke")]


class ModelDeploymentSmokeAttemptResolution(UUIDTimeStampedModel):
    """Append-only system observations and explicit operator decisions."""

    class Outcome(models.TextChoices):
        HTTP_BODY_PREPARED = "http_body_prepared", "HTTP正文已序列化（未证明送达）"
        CONFIRMED_NOT_SUBMITTED = "confirmed_not_submitted", "确认未提交"
        CONFIRMED_REMOTE_SUBMITTED = "confirmed_remote_submitted", "确认远端已提交"
        CONFIRMED_REMOTE_FAILED = "confirmed_remote_failed", "确认远端失败"

    OPERATOR_OUTCOMES = (Outcome.CONFIRMED_NOT_SUBMITTED, Outcome.CONFIRMED_REMOTE_SUBMITTED, Outcome.CONFIRMED_REMOTE_FAILED)
    OPERATOR_CHOICES = tuple((outcome.value, outcome.label) for outcome in OPERATOR_OUTCOMES)

    attempt = models.ForeignKey(
        ModelDeploymentSmokeAttempt,
        on_delete=models.PROTECT,
        related_name="resolutions",
    )
    revision = models.PositiveIntegerField(validators=[MinValueValidator(1)])
    outcome = models.CharField(max_length=40, choices=Outcome.choices)
    provider_request_id_sha256 = models.CharField(max_length=64, blank=True)
    evidence = models.JSONField(default=dict)
    evidence_schema_version = models.PositiveIntegerField(default=1)
    evidence_size_bytes = models.PositiveIntegerField(default=2)
    evidence_sha256 = models.CharField(max_length=64)
    note = models.TextField(blank=True)
    decided_by = models.ForeignKey(
        settings.AUTH_USER_MODEL,
        null=True,
        blank=True,
        on_delete=models.PROTECT,
        related_name="deployment_smoke_attempt_resolutions",
    )

    class Meta:
        ordering = ("attempt", "revision")
        constraints = [models.UniqueConstraint(
            fields=("attempt", "revision"),
            name="unique_deployment_smoke_resolution",
        )]

    def save(self, *args, **kwargs):
        if not self._state.adding:
            raise ValidationError("ModelDeploymentSmokeAttemptResolution 是不可变审计事实，不能修改")
        return super().save(*args, **kwargs)

    def delete(self, *args, **kwargs):
        raise ValidationError("ModelDeploymentSmokeAttemptResolution 是不可变审计事实，不能删除")


class RuntimeModelAvailability(UUIDTimeStampedModel):
    class Status(models.TextChoices):
        AVAILABLE = "available", "可用"
        MISSING = "missing", "缺失"
        INCOMPATIBLE = "incompatible", "不兼容"
        UNKNOWN = "unknown", "未知"

    class ObservationSource(models.TextChoices):
        NAME_ONLY = "name_only", "仅文件名"
        LOCAL_FILESYSTEM = "local_filesystem", "本机受控文件系统"
        REMOTE_ATTESTATION = "remote_attestation", "远程受认证证明"

    snapshot = models.ForeignKey(
        HostCapabilitySnapshot,
        on_delete=models.CASCADE,
        related_name="model_availability",
    )
    deployment = models.ForeignKey(
        ModelDeployment,
        on_delete=models.PROTECT,
        related_name="runtime_availability",
    )
    artifact = models.ForeignKey(
        ModelArtifact,
        null=True,
        blank=True,
        on_delete=models.PROTECT,
        related_name="runtime_availability",
    )
    availability_key = models.CharField(max_length=191)
    status = models.CharField(max_length=20, choices=Status.choices)
    reason = models.CharField(max_length=500, blank=True)
    observed_size_bytes = models.PositiveBigIntegerField(null=True, blank=True)
    observed_sha256 = models.CharField(max_length=64, blank=True)
    observation_source = models.CharField(
        max_length=24,
        choices=ObservationSource.choices,
        default=ObservationSource.NAME_ONLY,
    )

    class Meta:
        ordering = ("snapshot", "availability_key")
        constraints = [
            models.UniqueConstraint(
                fields=("snapshot", "availability_key"),
                name="unique_runtime_model_availability",
            ),
        ]


class RecommendationSnapshot(UUIDTimeStampedModel):
    class Status(models.TextChoices):
        READY = "ready", "可推荐"
        NO_COMPATIBLE_MODEL = "no_compatible_model", "无兼容模型"
        INSUFFICIENT_DATA = "insufficient_data", "信息不足"

    host_snapshot = models.ForeignKey(
        HostCapabilitySnapshot,
        on_delete=models.PROTECT,
        related_name="recommendations",
    )
    project = models.ForeignKey(
        SeriesProject,
        null=True,
        blank=True,
        on_delete=models.CASCADE,
        related_name="recommendation_snapshots",
    )
    config_revision = models.ForeignKey(
        ConfigRevision,
        null=True,
        blank=True,
        on_delete=models.PROTECT,
        related_name="recommendation_snapshots",
    )
    input_sha256 = models.CharField(max_length=64)
    status = models.CharField(max_length=32, choices=Status.choices)
    reason = models.CharField(max_length=500, blank=True)
    recommendation = models.JSONField(default=dict)
    recommendation_schema_version = models.PositiveIntegerField(default=1)
    recommendation_size_bytes = models.PositiveIntegerField(default=2)
    recommendation_sha256 = models.CharField(max_length=64)
    created_by = models.ForeignKey(
        settings.AUTH_USER_MODEL,
        null=True,
        blank=True,
        on_delete=models.PROTECT,
        related_name="created_recommendation_snapshots",
    )

    class Meta:
        ordering = ("-created_at",)
        constraints = [
            models.UniqueConstraint(
                fields=("host_snapshot", "input_sha256"),
                condition=Q(project__isnull=True),
                name="unique_global_recommendation_snapshot",
            ),
            models.UniqueConstraint(
                fields=("host_snapshot", "project", "input_sha256"),
                condition=Q(project__isnull=False),
                name="unique_project_recommendation_snapshot",
            ),
        ]


class RecommendationCandidate(UUIDTimeStampedModel):
    class Status(models.TextChoices):
        RECOMMENDED = "recommended", "推荐"
        COMPATIBLE = "compatible", "兼容"
        SMOKE_TEST_REQUIRED = "smoke_test_required", "需要烟测"
        UNAVAILABLE = "unavailable", "不可用"

    recommendation_snapshot = models.ForeignKey(
        RecommendationSnapshot,
        on_delete=models.CASCADE,
        related_name="candidates",
    )
    capability_key = models.CharField(max_length=100)
    deployment = models.ForeignKey(
        ModelDeployment,
        on_delete=models.PROTECT,
        related_name="recommendation_candidates",
    )
    workflow_version = models.ForeignKey(
        WorkflowTemplateVersion,
        null=True,
        blank=True,
        on_delete=models.PROTECT,
        related_name="recommendation_candidates",
    )
    status = models.CharField(max_length=24, choices=Status.choices)
    rank = models.PositiveIntegerField(null=True, blank=True)
    reason_code = models.CharField(max_length=100)
    suggested_values = models.JSONField(default=dict, blank=True)
    suggested_values_schema_version = models.PositiveIntegerField(default=1)
    suggested_values_size_bytes = models.PositiveIntegerField(default=2)
    suggested_values_sha256 = models.CharField(max_length=64)

    class Meta:
        ordering = ("recommendation_snapshot", "rank", "deployment__key")
        constraints = [
            models.UniqueConstraint(
                fields=(
                    "recommendation_snapshot", "capability_key", "deployment",
                    "workflow_version",
                ),
                name="unique_recommendation_candidate",
            ),
        ]


class RecommendationDecision(UUIDTimeStampedModel):
    class Decision(models.TextChoices):
        ACCEPT = "accept", "接受"
        OVERRIDE = "override", "人工覆盖"
        REJECT = "reject", "拒绝"

    recommendation_snapshot = models.ForeignKey(
        RecommendationSnapshot,
        on_delete=models.PROTECT,
        related_name="decisions",
    )
    candidate = models.ForeignKey(
        RecommendationCandidate,
        null=True,
        blank=True,
        on_delete=models.PROTECT,
        related_name="decisions",
    )
    decision = models.CharField(max_length=16, choices=Decision.choices)
    scope_type = models.CharField(max_length=16, choices=ConfigRevision.ScopeType.choices)
    project = models.ForeignKey(
        SeriesProject,
        null=True,
        blank=True,
        on_delete=models.PROTECT,
        related_name="recommendation_decisions",
    )
    config_revision = models.ForeignKey(
        ConfigRevision,
        null=True,
        blank=True,
        on_delete=models.PROTECT,
        related_name="recommendation_decisions",
    )
    override_values = models.JSONField(default=dict, blank=True)
    override_values_schema_version = models.PositiveIntegerField(default=1)
    override_values_size_bytes = models.PositiveIntegerField(default=2)
    override_values_sha256 = models.CharField(max_length=64)
    reason = models.CharField(max_length=500, blank=True)
    decided_by = models.ForeignKey(
        settings.AUTH_USER_MODEL,
        on_delete=models.PROTECT,
        related_name="recommendation_decisions",
    )

    class Meta:
        ordering = ("-created_at",)
        constraints = [
            models.CheckConstraint(
                condition=(
                    Q(scope_type="system", project__isnull=True)
                    | Q(scope_type="project", project__isnull=False)
                ),
                name="recommendation_decision_scope_consistent",
            ),
        ]


class ScriptSource(UUIDTimeStampedModel):
    class SourceType(models.TextChoices):
        PASTE = "paste", "粘贴"
        UPLOAD = "upload", "上传"
        LIBRARY_ASSET = "library_asset", "素材库剧本"
        AI_GENERATED = "ai_generated", "AI 生成"

    class SourceFormat(models.TextChoices):
        PLAIN = "plain", "纯文本"
        MARKDOWN = "markdown", "Markdown"
        FOUNTAIN = "fountain", "Fountain"
        DOCX = "docx", "DOCX"

    class Status(models.TextChoices):
        PENDING = "pending", "待提取"
        PREVIEW_READY = "preview_ready", "待确认"
        CONFIRMED = "confirmed", "已确认"
        FAILED = "failed", "失败"

    owner = models.ForeignKey(
        settings.AUTH_USER_MODEL,
        on_delete=models.CASCADE,
        related_name="script_sources",
    )
    project = models.ForeignKey(
        SeriesProject,
        on_delete=models.CASCADE,
        related_name="script_sources",
    )
    source_asset = models.ForeignKey(
        Asset,
        null=True,
        blank=True,
        on_delete=models.PROTECT,
        related_name="script_sources",
    )
    source_type = models.CharField(max_length=16, choices=SourceType.choices)
    source_format = models.CharField(max_length=16, choices=SourceFormat.choices)
    status = models.CharField(max_length=20, choices=Status.choices, default=Status.PENDING)
    original_name = models.CharField(max_length=255, blank=True)
    mime_type = models.CharField(max_length=120, blank=True)
    file = models.FileField(upload_to=script_source_upload_path, blank=True)
    file_size_bytes = models.PositiveBigIntegerField(default=0)
    file_sha256 = models.CharField(max_length=64, blank=True)
    validation_message = models.TextField(blank=True)

    class Meta:
        ordering = ("-created_at",)
        indexes = [models.Index(fields=("project", "status", "created_at"))]


class ScriptRevision(UUIDTimeStampedModel):
    class Status(models.TextChoices):
        PREVIEW = "preview", "预览"
        CONFIRMED = "confirmed", "已确认"
        SUPERSEDED = "superseded", "已替代"

    source = models.ForeignKey(
        ScriptSource,
        on_delete=models.CASCADE,
        related_name="revisions",
    )
    revision = models.PositiveIntegerField(validators=[MinValueValidator(1)])
    content = models.TextField()
    content_size_bytes = models.PositiveBigIntegerField()
    content_sha256 = models.CharField(max_length=64)
    status = models.CharField(max_length=16, choices=Status.choices, default=Status.PREVIEW)
    created_by = models.ForeignKey(
        settings.AUTH_USER_MODEL,
        on_delete=models.PROTECT,
        related_name="created_script_revisions",
    )
    confirmed_by = models.ForeignKey(
        settings.AUTH_USER_MODEL,
        null=True,
        blank=True,
        on_delete=models.PROTECT,
        related_name="confirmed_script_revisions",
    )
    confirmed_at = models.DateTimeField(null=True, blank=True)
    origin_generation_attempt = models.OneToOneField(
        "GenerationAttempt",
        null=True,
        blank=True,
        on_delete=models.PROTECT,
        related_name="generated_script_revision",
    )

    class Meta:
        ordering = ("source", "-revision")
        constraints = [
            models.UniqueConstraint(
                fields=("source", "revision"),
                name="unique_script_source_revision",
            ),
            models.UniqueConstraint(
                fields=("source",),
                condition=Q(status="confirmed"),
                name="one_confirmed_script_revision",
            ),
        ]


class ScriptRevisionStructure(models.Model):
    """On-demand structured detail of the existing script revision, not a candidate queue."""

    revision = models.OneToOneField(ScriptRevision, primary_key=True, on_delete=models.CASCADE,
        related_name="structured_content")
    output_version = models.ForeignKey(BusinessOutputVersion, on_delete=models.PROTECT,
        related_name="script_revision_structures")
    document = models.JSONField()
    document_schema_version = models.PositiveIntegerField()
    document_size_bytes = models.PositiveIntegerField()
    document_sha256 = models.CharField(max_length=64)
    created_at = models.DateTimeField(auto_now_add=True)


class ProductionRecipeVersion(UUIDTimeStampedModel):
    class Status(models.TextChoices):
        DRAFT = "draft", "草稿"
        PUBLISHED = "published", "已发布"
        RETIRED = "retired", "已停用"

    recipe_key = models.CharField(
        max_length=100,
        validators=[RegexValidator(r"^[a-z][a-z0-9_.-]{1,99}$")],
    )
    version = models.PositiveIntegerField(validators=[MinValueValidator(1)])
    name = models.CharField(max_length=160)
    definition = models.JSONField(default=dict)
    definition_schema_version = models.PositiveIntegerField(default=1)
    definition_size_bytes = models.PositiveIntegerField(default=2)
    definition_sha256 = models.CharField(max_length=64)
    status = models.CharField(max_length=16, choices=Status.choices, default=Status.DRAFT)
    published_at = models.DateTimeField(null=True, blank=True)
    published_by = models.ForeignKey(
        settings.AUTH_USER_MODEL,
        null=True,
        blank=True,
        on_delete=models.PROTECT,
        related_name="published_production_recipe_versions",
    )

    class Meta:
        ordering = ("recipe_key", "-version")
        constraints = [
            models.UniqueConstraint(
                fields=("recipe_key", "version"),
                name="unique_production_recipe_version",
            ),
        ]

    def __str__(self) -> str:
        return f"{self.name} v{self.version}"


class ProductionRun(UUIDTimeStampedModel):
    class PlanSource(models.TextChoices):
        RECIPE = "recipe", "配方生产"
        MANUAL = "manual", "人工逐步制作"

    class Status(models.TextChoices):
        QUEUED = "queued", "排队中"
        AWAITING_INPUT = "awaiting_input", "等待人工操作"
        RUNNING = "running", "运行中"
        AWAITING_APPROVAL = "awaiting_approval", "待审批"
        BLOCKED = "blocked", "已阻塞"
        SUCCEEDED = "succeeded", "成功"
        FAILED = "failed", "失败"
        CANCELLED = "cancelled", "已取消"

    owner = models.ForeignKey(
        settings.AUTH_USER_MODEL,
        on_delete=models.PROTECT,
        related_name="production_runs",
    )
    project = models.ForeignKey(
        SeriesProject,
        on_delete=models.PROTECT,
        related_name="production_runs",
    )
    script_revision = models.ForeignKey(
        ScriptRevision,
        null=True,
        blank=True,
        on_delete=models.PROTECT,
        related_name="production_runs",
    )
    # Current manual choice references its original decision. Per-stage
    # revisions are not a global ordering and timestamps are not selection.
    script_adoption_decision = models.ForeignKey(
        "ApprovalDecision", null=True, blank=True, on_delete=models.RESTRICT,
        related_name="script_selected_by_runs", db_index=False,
    )
    recipe_version = models.ForeignKey(
        ProductionRecipeVersion,
        null=True,
        blank=True,
        on_delete=models.PROTECT,
        related_name="production_runs",
    )
    plan_source = models.CharField(max_length=6, choices=PlanSource.choices)
    config_revision = models.ForeignKey(
        ConfigRevision,
        null=True,
        blank=True,
        on_delete=models.PROTECT,
        related_name="production_runs",
    )
    idempotency_key = models.CharField(max_length=120)
    request_sha256 = models.CharField(max_length=64)
    status = models.CharField(max_length=24, choices=Status.choices, default=Status.QUEUED)
    current_stage_key = models.CharField(max_length=100, blank=True)
    blocked_code = models.CharField(max_length=100, blank=True)
    blocked_reason = models.TextField(blank=True)
    allow_external_calls = models.BooleanField(default=False)
    cost_currency = models.CharField(max_length=12, blank=True)
    estimated_cost_total = models.DecimalField(max_digits=14, decimal_places=6, default=0)
    committed_cost_total = models.DecimalField(max_digits=14, decimal_places=6, default=0)
    actual_cost_total = models.DecimalField(max_digits=14, decimal_places=6, default=0)
    generation_task_count = models.PositiveIntegerField(default=0)
    config_snapshot = models.JSONField(default=dict)
    config_snapshot_schema_version = models.PositiveIntegerField(default=1)
    config_snapshot_size_bytes = models.PositiveIntegerField(default=2)
    config_snapshot_sha256 = models.CharField(max_length=64)
    input_snapshot = models.JSONField(default=dict)
    input_snapshot_schema_version = models.PositiveIntegerField(default=1)
    input_snapshot_size_bytes = models.PositiveIntegerField(default=2)
    input_snapshot_sha256 = models.CharField(max_length=64)
    started_at = models.DateTimeField(null=True, blank=True)
    finished_at = models.DateTimeField(null=True, blank=True)

    class Meta:
        ordering = ("-created_at",)
        constraints = [
            models.UniqueConstraint(
                fields=("owner", "idempotency_key"),
                name="unique_production_run_idempotency",
            ),
            models.CheckConstraint(condition=(Q(plan_source="recipe", recipe_version__isnull=False)
                | Q(plan_source="manual", recipe_version__isnull=True)), name="ck_run_plan_source"),
            models.CheckConstraint(
                condition=(
                    Q(estimated_cost_total__gte=0)
                    & Q(committed_cost_total__gte=0)
                    & Q(actual_cost_total__gte=0)
                ),
                name="production_run_costs_nonnegative",
            ),
        ]
        indexes = [
            models.Index(fields=("owner", "status", "created_at")),
            models.Index(fields=("plan_source", "status", "id"), name="idx_run_auto_advance"),
        ]


class ProductionRunPlan(UUIDTimeStampedModel):
    """One immutable execution plan; recipe/catalog are its creation sources."""

    run = models.OneToOneField(ProductionRun, on_delete=models.CASCADE, related_name="execution_plan")
    source = models.CharField(max_length=6, choices=ProductionRun.PlanSource.choices)
    stage_catalog_key = models.CharField(max_length=100)
    stage_catalog_version = models.PositiveIntegerField(validators=[MinValueValidator(1)])
    stage_catalog_sha256 = models.CharField(max_length=64, validators=[RegexValidator(r"^[a-f0-9]{64}$")])
    definition = models.JSONField()
    definition_schema_version = models.PositiveIntegerField(validators=[MinValueValidator(1)])
    definition_size_bytes = models.PositiveIntegerField(validators=[MaxValueValidator(65536)])
    definition_sha256 = models.CharField(max_length=64, validators=[RegexValidator(r"^[a-f0-9]{64}$")])
    created_by = models.ForeignKey(settings.AUTH_USER_MODEL, on_delete=models.PROTECT, related_name="production_run_plans")

    _SNAPSHOT_FIELDS = frozenset(("run_id", "source", "stage_catalog_key", "stage_catalog_version", "stage_catalog_sha256",
        "definition", "definition_schema_version", "definition_size_bytes", "definition_sha256", "created_by_id"))
    _WRITE_ONCE_FIELDS = frozenset()
    objects = models.Manager.from_queryset(ExecutionSnapshotQuerySet)()

    class Meta:
        constraints = [models.CheckConstraint(condition=Q(definition_schema_version=1, definition_size_bytes__lte=65536),
            name="ck_run_plan_definition_size")]

    def save(self, *args, **kwargs):
        _validate_execution_snapshot_update(self)
        super().save(*args, **kwargs)


class ProductionStageInputAmendmentQuerySet(ExecutionSnapshotQuerySet):
    def delete(self):
        raise ValidationError("阶段输入修订是不可变审计事实，不能删除")


class ProductionStageInputAmendment(UUIDTimeStampedModel):
    """An explicit pre-execution correction; the original RunPlan stays immutable."""

    stage_run = models.OneToOneField("StageRun", on_delete=models.PROTECT, related_name="input_amendment")
    evidence = models.JSONField()
    evidence_schema_version = models.PositiveIntegerField(validators=[MinValueValidator(1)])
    evidence_size_bytes = models.PositiveIntegerField(validators=[MaxValueValidator(65536)])
    evidence_sha256 = models.CharField(max_length=64, validators=[RegexValidator(r"^[a-f0-9]{64}$")])
    created_by = models.ForeignKey(settings.AUTH_USER_MODEL, on_delete=models.PROTECT,
        related_name="production_stage_input_amendments")

    _SNAPSHOT_FIELDS = frozenset(("stage_run_id", "evidence", "evidence_schema_version",
        "evidence_size_bytes", "evidence_sha256", "created_by_id"))
    _WRITE_ONCE_FIELDS = frozenset()
    objects = models.Manager.from_queryset(ProductionStageInputAmendmentQuerySet)()

    class Meta:
        constraints = [models.CheckConstraint(condition=Q(evidence_schema_version=1, evidence_size_bytes__lte=65536),
            name="ck_stage_input_amendment_size")]

    def save(self, *args, **kwargs):
        _validate_execution_snapshot_update(self)
        super().save(*args, **kwargs)

    def delete(self, *args, **kwargs):
        raise ValidationError("阶段输入修订是不可变审计事实，不能删除")


class ProductionManualCommand(UUIDTimeStampedModel):
    """Atomic action receipt; never another task state or output store."""

    class Action(models.TextChoices):
        GENERATE = "generate", "生成候选"
        RETRY = "retry", "重试失败主调用"
        ADOPT = "adopt", "采用已有内容"
        SELECT = "select", "选择镜头素材"
        APPROVE = "approve", "批准"
        ASSEMBLE = "assemble", "装配"
        DELIVER = "deliver", "交付"

    run = models.ForeignKey(ProductionRun, on_delete=models.CASCADE, related_name="manual_commands")
    stage_run = models.ForeignKey("StageRun", null=True, blank=True, on_delete=models.PROTECT, related_name="manual_commands")
    action = models.CharField(max_length=8, choices=Action.choices)
    idempotency_key = models.CharField(max_length=191)
    request_sha256 = models.CharField(max_length=64, validators=[RegexValidator(r"^[a-f0-9]{64}$")])
    created_by = models.ForeignKey(settings.AUTH_USER_MODEL, on_delete=models.PROTECT, related_name="production_manual_commands")
    result_kind = models.CharField(max_length=40)
    target_id = models.UUIDField()
    result_refs = models.JSONField()
    result_refs_schema_version = models.PositiveIntegerField(validators=[MinValueValidator(1)])
    result_refs_size_bytes = models.PositiveIntegerField(validators=[MaxValueValidator(65536)])
    result_refs_sha256 = models.CharField(max_length=64, validators=[RegexValidator(r"^[a-f0-9]{64}$")])

    _SNAPSHOT_FIELDS = frozenset(("run_id", "stage_run_id", "action", "idempotency_key", "request_sha256", "created_by_id",
        "result_kind", "target_id", "result_refs", "result_refs_schema_version", "result_refs_size_bytes", "result_refs_sha256"))
    _WRITE_ONCE_FIELDS = frozenset()
    objects = models.Manager.from_queryset(ExecutionSnapshotQuerySet)()

    class Meta:
        constraints = [models.UniqueConstraint(fields=("run", "idempotency_key"), name="uk_manual_command_key"),
            models.CheckConstraint(condition=Q(result_refs_schema_version=1, result_refs_size_bytes__lte=65536),
                name="ck_manual_command_refs_size")]
        ordering = ("-created_at", "-id")

    def save(self, *args, **kwargs):
        _validate_execution_snapshot_update(self)
        super().save(*args, **kwargs)


class ProductionMaterialFulfillment(UUIDTimeStampedModel):
    """One explicit run choice of an existing version, not a task or approval."""

    class SelectionMode(models.TextChoices):
        MANUAL = "manual", "人工"
        AUTOMATIC = "automatic", "自动"

    stage_run = models.ForeignKey("StageRun", on_delete=models.CASCADE, db_index=False,
        related_name="material_fulfillments")
    unit_key = models.CharField(max_length=191)
    requirement_key = models.CharField(max_length=191)
    run_plan_sha256 = models.CharField(max_length=64, validators=[RegexValidator(r"^[a-f0-9]{64}$")])
    reference_version = models.ForeignKey(AssetReferenceVersion, on_delete=models.PROTECT,
        related_name="production_fulfillments")
    target_sha256 = models.CharField(max_length=64, validators=[RegexValidator(r"^[a-f0-9]{64}$")])
    selection_mode = models.CharField(max_length=16, choices=SelectionMode.choices, default=SelectionMode.MANUAL)
    selected_by = models.ForeignKey(settings.AUTH_USER_MODEL, on_delete=models.PROTECT, null=True, blank=True,
        related_name="production_material_fulfillments")

    _SNAPSHOT_FIELDS = frozenset(("stage_run_id", "unit_key", "requirement_key", "run_plan_sha256",
        "reference_version_id", "target_sha256", "selection_mode", "selected_by_id"))
    _WRITE_ONCE_FIELDS = frozenset()
    objects = models.Manager.from_queryset(ExecutionSnapshotQuerySet)()

    class Meta:
        constraints = [models.UniqueConstraint(fields=("stage_run", "unit_key"),
            name="uk_material_fulfillment_unit"), models.CheckConstraint(
            condition=(Q(selection_mode="manual", selected_by__isnull=False)
                | Q(selection_mode="automatic", selected_by__isnull=True)),
            name="material_fulfillment_actor_mode")]

    def clean(self):
        super().clean()
        if not ((self.selection_mode == "manual" and self.selected_by_id is not None)
                or (self.selection_mode == "automatic" and self.selected_by_id is None)):
            raise ValidationError({"selected_by": "人工选择必须记录操作人，自动选择不能伪造人工身份"})
        if not self.stage_run_id or not self.reference_version_id:
            return
        stage, version = self.stage_run, self.reference_version
        if (stage.production_run.plan_source != "recipe" or stage.stage_key != "bible_image_generation"
                or stage.mode != "existing_pipeline"):
            raise ValidationError({"stage_run": "素材选择必须归属原配方素材阶段"})
        # Reuse references the original immutable version, including its true
        # generated origin. The shared reader verifies that origin and bytes.
        version.clean()
        if (version.series.owner_id != stage.production_run.owner_id or not version.setting_revision_id
                or self.requirement_key != f"setting:{version.setting_revision_id}"
                or self.unit_key != f"appearance:{version.setting_revision.variant_id}"):
            raise ValidationError({"reference_version": "素材版本与本轮所有者或确切需求不一致"})

    def save(self, *args, **kwargs):
        _validate_execution_snapshot_update(self)
        super().save(*args, **kwargs)


class StageRun(UUIDTimeStampedModel):
    class Status(models.TextChoices):
        PENDING = "pending", "待执行"
        RUNNING = "running", "执行中"
        AWAITING_APPROVAL = "awaiting_approval", "待审批"
        BLOCKED = "blocked", "已阻塞"
        SUCCEEDED = "succeeded", "成功"
        FAILED = "failed", "失败"
        CANCELLED = "cancelled", "已取消"

    production_run = models.ForeignKey(
        ProductionRun,
        on_delete=models.CASCADE,
        related_name="stages",
    )
    stage_key = models.CharField(max_length=100)
    ordinal = models.PositiveIntegerField()
    mode = models.CharField(max_length=32)
    status = models.CharField(max_length=24, choices=Status.choices, default=Status.PENDING)
    idempotency_key = models.CharField(max_length=191)
    approval_required = models.BooleanField(default=False)
    decision_policy = models.CharField(max_length=120, blank=True)
    decision_policy_sha256 = models.CharField(max_length=64, blank=True)
    attempt_count = models.PositiveIntegerField(default=0)
    routing = models.JSONField(default=dict)
    routing_schema_version = models.PositiveIntegerField(default=1)
    routing_size_bytes = models.PositiveIntegerField(default=2)
    routing_sha256 = models.CharField(max_length=64, blank=True)
    routing_decision = models.JSONField(default=dict)
    routing_decision_schema_version = models.PositiveIntegerField(default=1)
    routing_decision_size_bytes = models.PositiveIntegerField(default=2)
    routing_decision_sha256 = models.CharField(max_length=64, blank=True)
    config_snapshot = models.JSONField(default=dict)
    config_snapshot_schema_version = models.PositiveIntegerField(default=1)
    config_snapshot_size_bytes = models.PositiveIntegerField(default=2)
    config_snapshot_sha256 = models.CharField(max_length=64, blank=True)
    input_snapshot = models.JSONField(default=dict)
    input_snapshot_schema_version = models.PositiveIntegerField(default=1)
    input_snapshot_size_bytes = models.PositiveIntegerField(default=2)
    input_snapshot_sha256 = models.CharField(max_length=64, blank=True)
    output_snapshot = models.JSONField(default=dict)
    output_snapshot_schema_version = models.PositiveIntegerField(default=1)
    output_snapshot_size_bytes = models.PositiveIntegerField(default=2)
    output_snapshot_sha256 = models.CharField(max_length=64, blank=True)
    selected_route_index = models.PositiveSmallIntegerField(null=True, blank=True)
    selected_variant_route_index = models.PositiveSmallIntegerField(null=True, blank=True)
    approved_estimated_cost = models.DecimalField(
        max_digits=14,
        decimal_places=6,
        null=True,
        blank=True,
    )
    approved_cost_currency = models.CharField(max_length=12, blank=True)
    selection_confirmed_by = models.ForeignKey(
        settings.AUTH_USER_MODEL,
        null=True,
        blank=True,
        on_delete=models.PROTECT,
        related_name="confirmed_production_stage_selections",
    )
    selection_confirmed_at = models.DateTimeField(null=True, blank=True)
    input_reference = models.CharField(max_length=255, blank=True)
    output_reference = models.CharField(max_length=255, blank=True)
    reason_code = models.CharField(max_length=100, blank=True)
    reason = models.TextField(blank=True)
    started_at = models.DateTimeField(null=True, blank=True)
    finished_at = models.DateTimeField(null=True, blank=True)

    class Meta:
        ordering = ("production_run", "ordinal")
        constraints = [
            models.UniqueConstraint(
                fields=("production_run", "stage_key"),
                name="unique_run_stage_key",
            ),
            models.UniqueConstraint(
                fields=("production_run", "idempotency_key"),
                name="unique_run_stage_idempotency",
            ),
            models.UniqueConstraint(
                fields=("production_run", "ordinal"),
                name="unique_run_stage_ordinal",
            ),
            models.CheckConstraint(
                condition=Q(approved_estimated_cost__isnull=True) | Q(approved_estimated_cost__gte=0),
                name="stage_approved_cost_nonnegative",
            ),
            models.CheckConstraint(
                condition=(
                    Q(decision_policy="", decision_policy_sha256="")
                    | (
                        ~Q(decision_policy="")
                        & Q(decision_policy_sha256__regex=r"^[0-9a-f]{64}$")
                    )
                ),
                name="stage_decision_policy_hash_consistent",
            ),
        ]
        indexes = [models.Index(fields=("status", "updated_at"))]


class ApprovalDecision(UUIDTimeStampedModel):
    class Decision(models.TextChoices):
        APPROVE = "approve", "通过"
        REJECT = "reject", "拒绝"

    class Actor(models.TextChoices):
        HUMAN = "human", "人工"
        RECIPE_POLICY = "recipe_policy", "配方策略"

    stage_run = models.ForeignKey(
        StageRun,
        on_delete=models.CASCADE,
        related_name="approval_decisions",
    )
    revision = models.PositiveIntegerField(validators=[MinValueValidator(1)])
    decision = models.CharField(max_length=16, choices=Decision.choices)
    comment = models.TextField(blank=True)
    output_snapshot_sha256 = models.CharField(max_length=64, blank=True)
    actor = models.CharField(
        max_length=24,
        choices=Actor.choices,
        default=Actor.HUMAN,
    )
    decision_policy = models.CharField(max_length=120, blank=True)
    decision_policy_sha256 = models.CharField(max_length=64, blank=True)
    decided_by = models.ForeignKey(
        settings.AUTH_USER_MODEL,
        null=True,
        blank=True,
        on_delete=models.PROTECT,
        related_name="production_approval_decisions",
    )

    class Meta:
        ordering = ("stage_run", "-revision")
        constraints = [
            models.UniqueConstraint(
                fields=("stage_run", "revision"),
                name="unique_stage_approval_revision",
            ),
            models.CheckConstraint(
                condition=(
                    Q(
                        actor="human",
                        decided_by__isnull=False,
                        decision_policy="",
                        decision_policy_sha256="",
                    )
                    | (
                        Q(actor="recipe_policy", decided_by__isnull=True)
                        & ~Q(decision_policy="")
                        & Q(decision_policy_sha256__regex=r"^[0-9a-f]{64}$")
                    )
                ),
                name="approval_decision_actor_consistent",
            ),
        ]


class GenerationAttempt(UUIDTimeStampedModel):
    class Role(models.TextChoices):
        PRIMARY = "primary", "主调用"
        AUXILIARY = "auxiliary", "准备字段调用"

    class Status(models.TextChoices):
        AWAITING_REVIEW = "awaiting_review", "输出待人工处理"
        QUEUED = "queued", "排队中"
        RUNNING = "running", "执行中"
        SUBMITTING = "submitting", "正在提交"
        SUBMITTED = "submitted", "已提交"
        CANCEL_REQUESTED = "cancel_requested", "取消中"
        SUCCEEDED = "succeeded", "成功"
        FAILED = "failed", "失败"
        CANCELLED = "cancelled", "已取消"

    stage_run = models.ForeignKey(
        StageRun,
        on_delete=models.CASCADE,
        related_name="generation_attempts",
    )
    execution_instance_version = models.ForeignKey(
        ModelExecutionInstanceVersion, on_delete=models.PROTECT, related_name="generation_attempts",
    )
    preparation = models.ForeignKey(
        ExecutionPreparation, on_delete=models.PROTECT, related_name="generation_attempts", db_index=False,
    )
    parent_preparation = models.ForeignKey(
        ExecutionPreparation, on_delete=models.PROTECT, related_name="auxiliary_generation_attempts",
        null=True, blank=True, db_index=False,
    )
    role = models.CharField(max_length=16, choices=Role.choices)
    auxiliary_purpose = models.CharField(max_length=24, blank=True)
    field_batch_key = models.CharField(max_length=191, blank=True)
    unit_key = models.CharField(max_length=191)
    attempt_number = models.PositiveIntegerField(validators=[MinValueValidator(1)])
    retry_of = models.OneToOneField(
        "self", on_delete=models.PROTECT, null=True, blank=True, related_name="retry_successor",
    )
    retry_requested_by = models.ForeignKey(
        settings.AUTH_USER_MODEL, on_delete=models.PROTECT, null=True, blank=True,
        related_name="requested_generation_retries",
    )
    model_deployment = models.ForeignKey(
        ModelDeployment,
        null=True,
        blank=True,
        on_delete=models.PROTECT,
        related_name="generation_attempts",
    )
    prompt_profile_version = models.ForeignKey(
        PromptProfileVersion,
        null=True,
        blank=True,
        on_delete=models.PROTECT,
        related_name="generation_attempts",
    )
    prompt_profile_contract_sha256 = models.CharField(max_length=64, blank=True)
    generation_task = models.ForeignKey(
        GenerationTask,
        null=True,
        blank=True,
        on_delete=models.PROTECT,
        related_name="platform_attempts",
        db_index=False,
    )
    status = models.CharField(max_length=16, choices=Status.choices, default=Status.QUEUED)
    route_index = models.PositiveSmallIntegerField(null=True, blank=True)
    operation = models.CharField(max_length=100, blank=True)
    is_external = models.BooleanField(default=False)
    provider_request_id = models.CharField(max_length=191, blank=True)
    request_sha256 = models.CharField(max_length=64)
    request_snapshot_key = models.CharField(max_length=191)
    request_snapshot_schema_version = models.PositiveIntegerField(default=1)
    request_snapshot_size_bytes = models.PositiveIntegerField(default=2)
    request_snapshot_sha256 = models.CharField(max_length=64, blank=True)
    result_sha256 = models.CharField(max_length=64, blank=True)
    estimated_cost = models.DecimalField(max_digits=14, decimal_places=6, null=True, blank=True)
    committed_cost = models.DecimalField(max_digits=14, decimal_places=6, null=True, blank=True)
    actual_cost = models.DecimalField(max_digits=14, decimal_places=6, null=True, blank=True)
    cost_currency = models.CharField(max_length=12, blank=True)
    error_code = models.CharField(max_length=100, blank=True)
    error_message = models.TextField(blank=True)
    worker_id = models.CharField(max_length=160, blank=True)
    claim_token = models.UUIDField(null=True, blank=True, editable=False, db_index=True)
    claimed_at = models.DateTimeField(null=True, blank=True)
    heartbeat_at = models.DateTimeField(null=True, blank=True)
    submission_started_at = models.DateTimeField(null=True, blank=True)
    submitted_at = models.DateTimeField(null=True, blank=True)
    remote_status = models.CharField(max_length=120, blank=True)
    last_polled_at = models.DateTimeField(null=True, blank=True)
    next_poll_at = models.DateTimeField(null=True, blank=True, db_index=True)
    poll_count = models.PositiveIntegerField(default=0)
    deadline_started_at = models.DateTimeField(null=True, blank=True)
    hard_deadline_at = models.DateTimeField(null=True, blank=True, db_index=True)
    idle_deadline_at = models.DateTimeField(null=True, blank=True, db_index=True)
    last_meaningful_progress_at = models.DateTimeField(null=True, blank=True)
    progress_marker = models.CharField(max_length=191, blank=True)
    cancel_requested_at = models.DateTimeField(null=True, blank=True)
    cancel_submitted_at = models.DateTimeField(null=True, blank=True)
    ignore_late_result = models.BooleanField(default=False)
    started_at = models.DateTimeField(default=timezone.now)
    finished_at = models.DateTimeField(null=True, blank=True)

    @property
    def request_snapshot(self):
        from studio.services.execution_request_storage import read_request
        return read_request(self, "request_snapshot")

    class Meta:
        ordering = ("stage_run", "unit_key", "-attempt_number")
        constraints = [
            models.CheckConstraint(condition=(
                Q(role="primary", auxiliary_purpose="", field_batch_key="", parent_preparation__isnull=True)
                | Q(role="auxiliary", parent_preparation__isnull=False,
                    auxiliary_purpose__in=("generate_fields", "translate_fields")) & ~Q(field_batch_key="")
            ), name="ck_attempt_execution_role"),
            models.CheckConstraint(condition=~Q(parent_preparation=models.F("preparation")), name="ck_attempt_distinct_parent"),
            models.CheckConstraint(condition=~Q(retry_of=models.F("pk")), name="ck_attempt_retry_not_self"),
            models.CheckConstraint(condition=(Q(retry_of__isnull=True, retry_requested_by__isnull=True)
                | Q(retry_of__isnull=False, retry_requested_by__isnull=False, role="primary")), name="ck_attempt_retry_actor"),
            models.UniqueConstraint(fields=("parent_preparation", "field_batch_key", "attempt_number"), name="uk_attempt_parent_batch"),
            models.UniqueConstraint(fields=("generation_task",), name="uk_attempt_generation_task"),
            models.CheckConstraint(condition=Q(generation_task__isnull=True) | Q(role="primary"),
                name="ck_attempt_task_primary"),
            models.UniqueConstraint(fields=("preparation",), condition=Q(
                status__in=("queued", "running", "submitting", "submitted", "cancel_requested")),
                name="uk_attempt_preparation_active"),
            models.UniqueConstraint(
                fields=("stage_run", "unit_key", "attempt_number"),
                name="unique_stage_generation_attempt",
            ),
            models.CheckConstraint(
                condition=(
                    (Q(estimated_cost__isnull=True) | Q(estimated_cost__gte=0))
                    & (Q(committed_cost__isnull=True) | Q(committed_cost__gte=0))
                    & (Q(actual_cost__isnull=True) | Q(actual_cost__gte=0))
                ),
                name="generation_attempt_costs_nonnegative",
            ),
        ]
        indexes = [models.Index(fields=("status", "next_poll_at")),
            models.Index(fields=("preparation", "-attempt_number"), name="idx_attempt_preparation")]

    def clean(self):
        super().clean()
        from studio.services.execution_task_links import validate_execution_task_links
        validate_execution_task_links(self)


class ExecutionTextResponseReview(UUIDTimeStampedModel):
    """Private original text/collect receipt; only eligible text permits recovery."""

    attempt = models.OneToOneField(GenerationAttempt, on_delete=models.PROTECT, related_name="text_response_review", null=True, blank=True)
    asset_job = models.OneToOneField(AssetGenerationJob, on_delete=models.PROTECT, related_name="text_response_review", null=True, blank=True)
    original_file = models.CharField(max_length=191)
    original_size = models.PositiveIntegerField()
    original_sha256 = models.CharField(max_length=64)
    diagnostics = models.JSONField()
    diagnostics_schema_version = models.PositiveIntegerField(default=1)
    diagnostics_size_bytes = models.PositiveIntegerField(default=2, validators=[MaxValueValidator(65536)])
    diagnostics_sha256 = models.CharField(max_length=64, default="", validators=[RegexValidator(r"^[a-f0-9]{64}$")])
    corrected_file = models.CharField(max_length=191, blank=True, null=True)
    corrected_size = models.PositiveIntegerField(null=True)
    corrected_sha256 = models.CharField(max_length=64, null=True)
    confirmed_by = models.ForeignKey(settings.AUTH_USER_MODEL, on_delete=models.PROTECT, null=True, blank=True)
    confirmed_at = models.DateTimeField(null=True, blank=True)
    _SNAPSHOT_FIELDS = frozenset(("attempt_id", "asset_job_id", "original_file", "original_size", "original_sha256", "diagnostics",
        "diagnostics_schema_version", "diagnostics_size_bytes", "diagnostics_sha256"))
    _WRITE_ONCE_FIELDS = frozenset(("corrected_file", "corrected_size", "corrected_sha256", "confirmed_by_id", "confirmed_at"))
    objects = models.Manager.from_queryset(ExecutionSnapshotQuerySet)()

    class Meta:
        constraints = [models.CheckConstraint(condition=(
            models.Q(attempt__isnull=False, asset_job__isnull=True)
            | models.Q(attempt__isnull=True, asset_job__isnull=False)), name="ck_text_response_single_owner")]

    @property
    def original_text(self):
        from studio.services.execution_response_storage import read_response
        return read_response(self.original_file, self.original_size, self.original_sha256)

    @property
    def corrected_text(self):
        from studio.services.execution_response_storage import read_response
        return read_response(self.corrected_file, self.corrected_size, self.corrected_sha256) if self.corrected_file else None

    def save(self, *args, **kwargs):
        _validate_execution_snapshot_update(self)
        super().save(*args, **kwargs)


class GenerationAttemptResolution(UUIDTimeStampedModel):
    class Outcome(models.TextChoices):
        CONFIRMED_NOT_SUBMITTED = "confirmed_not_submitted", "确认未提交"
        CONFIRMED_REMOTE_SUBMITTED = "confirmed_remote_submitted", "确认远端已提交"
        CONFIRMED_REMOTE_FAILED = "confirmed_remote_failed", "确认远端失败"
        ACTUAL_COST_CONFIRMED = "actual_cost_confirmed", "确认实际费用"
        CANCEL_REQUESTED_IGNORE_LATE = "cancel_requested_ignore_late", "取消并忽略晚到结果"
        LATE_RESULT_IGNORED = "late_result_ignored", "已忽略晚到结果"
        KNOWN_RESPONSE_REJECTED = "known_response_rejected", "已知响应未通过校验"
        RESPONSE_RECEIVED = "response_received", "已记录服务响应用量"
        HTTP_BODY_PREPARED = "http_body_prepared", "已记录HTTP库序列化正文摘要"

    attempt = models.ForeignKey(
        GenerationAttempt,
        on_delete=models.PROTECT,
        related_name="resolutions",
    )
    revision = models.PositiveIntegerField(validators=[MinValueValidator(1)])
    outcome = models.CharField(max_length=40, choices=Outcome.choices)
    provider_request_id = models.CharField(max_length=191, blank=True)
    evidence = models.JSONField(default=dict)
    evidence_schema_version = models.PositiveIntegerField(default=1)
    evidence_size_bytes = models.PositiveIntegerField(default=2)
    evidence_sha256 = models.CharField(max_length=64)
    note = models.TextField(blank=True)
    decided_by = models.ForeignKey(
        settings.AUTH_USER_MODEL,
        null=True,
        blank=True,
        on_delete=models.PROTECT,
        related_name="generation_attempt_resolutions",
    )

    class Meta:
        ordering = ("attempt", "revision")
        constraints = [
            models.UniqueConstraint(
                fields=("attempt", "revision"),
                name="unique_generation_attempt_resolution",
            ),
        ]

    def save(self, *args, **kwargs):
        if not self._state.adding:
            raise ValidationError("GenerationAttemptResolution 是不可变审计事实，不能修改")
        return super().save(*args, **kwargs)

    def delete(self, *args, **kwargs):
        raise ValidationError("GenerationAttemptResolution 是不可变审计事实，不能删除")


class ArtifactSelectionDecision(UUIDTimeStampedModel):
    """Selection provenance for one generated variant group."""

    class SelectionMode(models.TextChoices):
        MANUAL = "manual", "人工选择"
        AUTOMATIC = "automatic", "按配置自动选择"

    selection_mode = models.CharField(max_length=16, choices=SelectionMode.choices, default=SelectionMode.MANUAL)

    stage_run = models.ForeignKey(
        StageRun,
        on_delete=models.CASCADE,
        related_name="artifact_selection_decisions",
    )
    variant_group = models.CharField(max_length=120)
    revision = models.PositiveIntegerField(validators=[MinValueValidator(1)])
    artifact = models.ForeignKey(
        "GeneratedArtifact",
        on_delete=models.PROTECT,
        related_name="selection_decisions",
    )
    selected_by = models.ForeignKey(
        settings.AUTH_USER_MODEL,
        on_delete=models.PROTECT,
        related_name="artifact_selection_decisions",
        null=True,
        blank=True,
    )

    class Meta:
        ordering = ("stage_run", "variant_group", "revision")
        constraints = [
            models.UniqueConstraint(
                fields=("stage_run", "variant_group", "revision"),
                name="unique_artifact_selection_revision",
            ),
            models.CheckConstraint(
                condition=(models.Q(selection_mode="manual", selected_by__isnull=False)
                    | models.Q(selection_mode="automatic", selected_by__isnull=True)),
                name="artifact_selection_actor_mode",
            ),
        ]
        indexes = [models.Index(
            fields=("stage_run", "variant_group", "revision"),
            name="idx_artifact_selection_group",
        )]


class StageRouteEvent(UUIDTimeStampedModel):
    """Append-only routing evidence; JSON on StageRun is summary-only."""

    stage_run = models.ForeignKey(
        StageRun,
        on_delete=models.CASCADE,
        related_name="route_events",
    )
    sequence = models.PositiveIntegerField(validators=[MinValueValidator(1)])
    unit_key = models.CharField(max_length=191)
    route_index = models.PositiveSmallIntegerField(null=True, blank=True)
    event_type = models.CharField(max_length=64)
    code = models.CharField(max_length=100, blank=True)

    class Meta:
        ordering = ("stage_run", "sequence")
        constraints = [
            models.UniqueConstraint(
                fields=("stage_run", "sequence"),
                name="unique_stage_route_event_sequence",
            ),
        ]
        indexes = [models.Index(
            fields=("stage_run", "unit_key", "sequence"),
            name="idx_stage_route_event_unit",
        )]


class ProductionAssemblyLink(UUIDTimeStampedModel):
    """Durable per-episode assembly membership for a recoverable ProductionRun."""

    production_run = models.ForeignKey(
        ProductionRun,
        on_delete=models.CASCADE,
        related_name="assembly_links",
    )
    stage_run = models.ForeignKey(
        StageRun,
        on_delete=models.CASCADE,
        related_name="assembly_links",
    )
    episode = models.ForeignKey(
        Episode,
        on_delete=models.PROTECT,
        related_name="production_assembly_links",
    )
    assembly_job = models.ForeignKey(
        AssemblyJob,
        on_delete=models.PROTECT,
        related_name="production_run_links",
    )

    class Meta:
        ordering = ("production_run", "episode__number")
        constraints = [
            models.UniqueConstraint(
                fields=("production_run", "episode"),
                name="unique_run_episode_assembly",
            ),
            models.UniqueConstraint(
                fields=("stage_run", "episode"),
                name="unique_stage_episode_assembly",
            ),
        ]


class GeneratedArtifact(UUIDTimeStampedModel):
    class Kind(models.TextChoices):
        SCRIPT = "script", "剧本"
        PROMPT = "prompt", "提示词"
        IMAGE = "image", "图片"
        VIDEO = "video", "视频"
        AUDIO = "audio", "音频"
        ASSEMBLY = "assembly", "成片"
        OTHER = "other", "其他"

    class Status(models.TextChoices):
        AVAILABLE = "available", "可用"
        SELECTED = "selected", "已选择"
        REJECTED = "rejected", "已拒绝"
        INVALID = "invalid", "无效"
        LEGACY_MISSING = "legacy_missing", "历史文件缺失"

    manifest_key = models.CharField(max_length=191, unique=True)
    production_run = models.ForeignKey(
        ProductionRun,
        null=True,
        blank=True,
        on_delete=models.CASCADE,
        related_name="artifacts",
    )
    stage_run = models.ForeignKey(
        StageRun,
        null=True,
        blank=True,
        on_delete=models.CASCADE,
        related_name="artifacts",
    )
    generation_attempt = models.ForeignKey(
        GenerationAttempt,
        null=True,
        blank=True,
        on_delete=models.SET_NULL,
        related_name="artifacts",
    )
    generation_task = models.ForeignKey(
        GenerationTask,
        null=True,
        blank=True,
        on_delete=models.PROTECT,
        related_name="generated_artifacts",
    )
    asset = models.ForeignKey(
        Asset,
        null=True,
        blank=True,
        on_delete=models.PROTECT,
        related_name="generated_artifacts",
    )
    kind = models.CharField(max_length=16, choices=Kind.choices)
    role = models.CharField(max_length=100)
    variant_group = models.CharField(max_length=120, blank=True)
    variant_index = models.PositiveIntegerField(default=1)
    status = models.CharField(max_length=20, choices=Status.choices)
    storage_backend = models.CharField(max_length=40, default="local")
    object_key = models.CharField(max_length=1000, blank=True)
    mime_type = models.CharField(max_length=120, blank=True)
    size_bytes = models.PositiveBigIntegerField(null=True, blank=True)
    sha256 = models.CharField(max_length=64, blank=True)
    manifest_metadata = models.JSONField(default=dict, blank=True)
    comparison_parameters = models.JSONField(default=dict, blank=True)
    manifest_schema_version = models.PositiveIntegerField(default=1)
    manifest_metadata_size_bytes = models.PositiveIntegerField(default=2)
    manifest_metadata_sha256 = models.CharField(max_length=64)

    class Meta:
        ordering = ("production_run", "variant_group", "variant_index", "created_at")
        constraints = [
            models.UniqueConstraint(
                fields=("production_run", "variant_group"),
                condition=Q(status="selected", production_run__isnull=False),
                name="one_selected_artifact_variant",
            ),
            models.UniqueConstraint(
                fields=("production_run", "variant_group", "variant_index"),
                condition=Q(production_run__isnull=False),
                name="unique_artifact_variant_index",
            ),
        ]
        indexes = [
            models.Index(fields=("production_run", "kind", "status")),
            models.Index(fields=("generation_task", "status")),
        ]


class AssemblySourceItem(UUIDTimeStampedModel):
    """Immutable, per-shot input frozen for one assembly job.

    Generated and explicitly adopted media converge here. The source
    identity and verified byte facts are copied onto the item so an assembly
    never has to infer its inputs from whichever task or artifact looks newest.
    """

    assembly_job = models.ForeignKey(
        AssemblyJob,
        on_delete=models.CASCADE,
        related_name="source_items",
    )
    ordinal = models.PositiveIntegerField(validators=[MinValueValidator(1)])
    shot = models.ForeignKey(
        Shot,
        on_delete=models.PROTECT,
        related_name="assembly_source_items",
    )
    generated_artifact = models.ForeignKey(
        GeneratedArtifact,
        on_delete=models.PROTECT,
        related_name="assembly_source_items",
    )
    storage_backend = models.CharField(max_length=40)
    object_key = models.CharField(max_length=1000)
    mime_type = models.CharField(max_length=120)
    size_bytes = models.PositiveBigIntegerField()
    sha256 = models.CharField(max_length=64)
    media_kind = models.CharField(
        max_length=16,
        choices=Shot.MediaType.choices,
        default=Shot.MediaType.VIDEO,
    )
    start_ms = models.PositiveIntegerField(default=0)
    duration_ms = models.PositiveIntegerField(validators=[MinValueValidator(1)])
    trim_to_shot = models.BooleanField(default=False)
    has_audio_stream = models.BooleanField(default=False)

    class Meta:
        ordering = ("assembly_job", "ordinal")
        constraints = [
            models.UniqueConstraint(
                fields=("assembly_job", "ordinal"),
                name="unique_assembly_source_ordinal",
            ),
            models.UniqueConstraint(
                fields=("assembly_job", "shot"),
                name="unique_assembly_source_shot",
            ),
        ]
        indexes = [
            models.Index(
                fields=("assembly_job", "ordinal"),
                name="studio_asse_assembl_cdebbd_idx",
            ),
            models.Index(
                fields=("generated_artifact", "assembly_job"),
                name="studio_asse_generat_3b4e90_idx",
            ),
        ]
