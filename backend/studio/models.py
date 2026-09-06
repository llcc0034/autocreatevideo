from __future__ import annotations

import uuid
from pathlib import Path

from django.conf import settings
from django.core.exceptions import ValidationError
from django.core.validators import MaxValueValidator, MinValueValidator, RegexValidator
from django.db import models
from django.db.models import Q
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
        "activated_at", "activation_state", "based_on_revision_id",
        "credential_bound", "definition_sha256", "domain", "driver_type",
        "category", "channel", "contract_sha256", "field_definition_id",
        "graph_sha256", "initial_value_sha256", "input_contract_sha256",
        "is_active", "is_current", "is_default", "is_required", "is_secret",
        "is_system", "key", "locator_sha256", "modality", "model_profile_version_id",
        "profile_key", "profile_version_id", "project_id", "provider_id",
        "configuration_class",
        "prompt_profile_version_id", "review_state", "source_selector",
        "published_at", "recipe_key", "restart_required", "revision", "run_id",
        "role", "runtime_config_sha256", "runtime_host_id", "scope_type", "sha256",
        "size_bytes",
        "smoke_test_evidence_sha256", "smoke_test_state", "specification_sha256",
        "status", "template_id", "validated_deployment_id", "validation_evidence_sha256",
        "validation_state", "value_schema_sha256", "value_type", "values_sha256", "verified_at",
        "verified_snapshot_id", "version", "position", "model_family", "mode",
        "operation", "output_kind", "priority", "engine_key", "engine_version", "projector_id",
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


class Style(UUIDTimeStampedModel):
    class Category(models.TextChoices):
        WUXIA = "wuxia", "武侠"
        XUANHUAN = "xuanhuan", "玄幻"
        ANIME = "anime", "动漫"
        THRILLER = "thriller", "惊悚"
        DETECTIVE = "detective", "侦探"
        DRAMA = "drama", "剧情"
        SCIFI = "sci_fi", "科幻"
        ROMANCE = "romance", "爱情"
        COMEDY = "comedy", "喜剧"
        HISTORICAL = "historical", "历史"
        OTHER = "other", "其他"

    owner = models.ForeignKey(settings.AUTH_USER_MODEL, null=True, blank=True, on_delete=models.CASCADE, related_name="styles")
    slug = models.SlugField(max_length=80)
    name = models.CharField(max_length=120)
    category = models.CharField(max_length=32, choices=Category.choices, default=Category.OTHER)
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
    style = models.ForeignKey(Style, null=True, blank=True, on_delete=models.SET_NULL, related_name="assets")
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
    style = models.ForeignKey(Style, null=True, blank=True, on_delete=models.SET_NULL, related_name="projects")
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
    visual_identity = models.TextField(blank=True)
    negative_constraints = models.TextField(blank=True)
    anchor_asset = models.ForeignKey(Asset, null=True, blank=True, on_delete=models.SET_NULL, related_name="+")
    reference_assets = models.ManyToManyField(Asset, blank=True, related_name="+")
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
    narrative = models.TextField()
    # A review-gated, single-shot H3 director description.  This is kept
    # separate from the compact storyboard narrative so an operator can opt
    # into dense prompt prose without turning the timeline editor into a long
    # free-form field.  Its source language is resolved through the normal
    # PromptFieldTranslation overlay just like the other prompt-impacting
    # Shot fields.
    detailed_description = models.TextField(blank=True)
    camera = models.TextField(blank=True)
    action = models.TextField(blank=True)
    dialogue = models.JSONField(default=list, blank=True, help_text='[{"speaker_id":"S1","text":"..."}]')
    sound_effects = models.TextField(blank=True)
    music = models.TextField(blank=True)
    duration_ms = models.PositiveIntegerField(
        validators=[MinValueValidator(1)],
    )
    media_type = models.CharField(max_length=12, choices=MediaType.choices)
    conditioning_mode = models.CharField(
        max_length=64,
        blank=True,
        default="",
        validators=[RegexValidator(r"^[A-Za-z][A-Za-z0-9_.-]{0,63}$")],
    )
    include_audio = models.BooleanField()
    scene = models.ForeignKey(SceneBible, null=True, blank=True, on_delete=models.SET_NULL, related_name="shots")
    characters = models.ManyToManyField(CharacterBible, blank=True, related_name="shots")
    props = models.ManyToManyField(PropBible, blank=True, related_name="shots")
    reference_assets = models.ManyToManyField(Asset, blank=True, related_name="shots")
    # Explicit semantic bindings consumed by H3 image-conditioned workflows.
    # ``reference_assets`` remains the selectable asset pool only; it must not
    # be used to infer first/last/character/scene/prop meaning.
    reference_bindings = models.JSONField(default=dict, blank=True)
    # These flags express a continuity requirement only. They never imply
    # that a concrete first/last-frame Asset has already been generated or
    # bound in ``reference_bindings``.
    requires_first_frame_continuity = models.BooleanField(default=False)
    requires_last_frame_continuity = models.BooleanField(default=False)
    continuity_notes = models.TextField(blank=True)
    metadata = models.JSONField(default=dict, blank=True)

    class Meta:
        ordering = ("episode", "number")
        constraints = [models.UniqueConstraint(fields=("episode", "number"), name="unique_shot_number")]

    def __str__(self) -> str:
        return f"E{self.episode.number}S{self.number}: {self.title or self.narrative[:30]}"


class PromptArtifact(UUIDTimeStampedModel):
    class PromptType(models.TextChoices):
        VIDEO = "video", "视频"
        IMAGE = "image", "图片"

    project = models.ForeignKey(SeriesProject, on_delete=models.CASCADE, related_name="prompt_artifacts")
    episode = models.ForeignKey(Episode, null=True, blank=True, on_delete=models.CASCADE, related_name="prompt_artifacts")
    shot = models.ForeignKey(Shot, null=True, blank=True, on_delete=models.CASCADE, related_name="prompt_artifacts")
    created_by = models.ForeignKey(settings.AUTH_USER_MODEL, on_delete=models.CASCADE, related_name="prompt_artifacts")
    # The published assembly strategy is frozen on the artifact.  Runtime
    # consumers must never re-resolve a mutable binding when replaying an old
    # prompt.
    prompt_profile_version = models.ForeignKey(
        "PromptProfileVersion",
        null=True,
        blank=True,
        on_delete=models.PROTECT,
        related_name="prompt_artifacts",
    )
    prompt_profile_binding_key = models.CharField(max_length=120, blank=True)
    prompt_profile_contract_sha256 = models.CharField(max_length=64, blank=True)
    prompt_type = models.CharField(max_length=12, choices=PromptType.choices)
    model = models.CharField(max_length=80, blank=True, default="")
    conditioning_mode = models.CharField(
        max_length=64,
        blank=True,
        default="",
        validators=[RegexValidator(r"^[A-Za-z][A-Za-z0-9_.-]{0,63}$")],
    )
    revision = models.PositiveIntegerField(default=1)
    is_multi_shot = models.BooleanField()
    include_audio = models.BooleanField()
    total_duration_ms = models.PositiveIntegerField()
    prompt_text = models.TextField()
    negative_prompt = models.TextField(blank=True)
    structured_payload = models.JSONField(default=dict)
    consistency_snapshot = models.JSONField(default=dict)
    request_options = models.JSONField(default=dict)
    content_hash = models.CharField(max_length=64, db_index=True)

    class Meta:
        ordering = ("-created_at",)
        constraints = [
            models.UniqueConstraint(
                fields=("project", "episode", "shot", "prompt_type", "revision"),
                name="unique_prompt_revision",
            )
        ]


class PromptFieldTranslation(UUIDTimeStampedModel):
    """Human-reviewed translation overlay for one prompt-impacting source field.

    Source records remain authoritative and are never rewritten by translation.
    The H3 compiler may consume an overlay only when it is confirmed and its
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


class GeneratedWorkflow(UUIDTimeStampedModel):
    project = models.ForeignKey(SeriesProject, on_delete=models.CASCADE, related_name="generated_workflows")
    episode = models.ForeignKey(Episode, null=True, blank=True, on_delete=models.CASCADE, related_name="generated_workflows")
    prompt_artifact = models.ForeignKey(PromptArtifact, on_delete=models.CASCADE, related_name="workflows")
    template = models.ForeignKey(WorkflowTemplate, on_delete=models.PROTECT, related_name="generated_workflows")
    workflow_template_version = models.ForeignKey(
        "WorkflowTemplateVersion",
        null=True,
        blank=True,
        on_delete=models.PROTECT,
        related_name="generated_workflows",
    )
    created_by = models.ForeignKey(settings.AUTH_USER_MODEL, on_delete=models.CASCADE, related_name="generated_workflows")
    workflow_json = models.JSONField(default=dict)
    injected_values = models.JSONField(default=dict)
    content_hash = models.CharField(max_length=64, db_index=True)
    compiler_id = models.CharField(max_length=80, blank=True)
    compiler_version = models.PositiveIntegerField(default=0)
    route_snapshot_sha256 = models.CharField(max_length=64, blank=True)
    workflow_contract_sha256 = models.CharField(max_length=64, blank=True)
    resolved_semantics_sha256 = models.CharField(max_length=64, blank=True)
    asset_bindings_sha256 = models.CharField(max_length=64, blank=True)
    model_requirements_sha256 = models.CharField(max_length=64, blank=True)

    class Meta:
        ordering = ("-created_at",)


class WorkerLease(models.Model):
    """Database mutex row used to serialize task claims across worker processes."""

    key = models.CharField(max_length=80, primary_key=True)
    holder_id = models.CharField(max_length=64, blank=True)
    expires_at = models.DateTimeField(null=True, blank=True)
    updated_at = models.DateTimeField(auto_now=True)


class GenerationTask(UUIDTimeStampedModel):
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

    class SubmissionState(models.TextChoices):
        UNPREPARED = "unprepared", "未准备"
        PREPARED = "prepared", "已持久化提交标识"
        SUBMITTED = "submitted", "已提交"
        RECOVERED = "recovered", "已恢复远端提交"
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
    generated_workflow = models.ForeignKey(GeneratedWorkflow, on_delete=models.PROTECT, related_name="tasks")
    kind = models.CharField(max_length=12, choices=Kind.choices)
    backend = models.CharField(max_length=80, blank=True, default="")
    model_profile = models.CharField(max_length=120, blank=True, default="")
    workflow_version = models.PositiveIntegerField(default=1)
    seed = models.BigIntegerField(default=0)
    # Immutable post-alignment request plan.  Actual returned-container facts
    # live in GeneratedArtifact.manifest_metadata["observed_media"].
    effective_frames = models.PositiveIntegerField(default=0)
    effective_duration_ms = models.PositiveIntegerField(default=0)
    capability_snapshot = models.JSONField(default=dict, blank=True)
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
    max_attempts = models.PositiveSmallIntegerField(validators=[MinValueValidator(1)])
    comfy_prompt_id = models.CharField(max_length=120, blank=True, db_index=True)
    client_id = models.CharField(max_length=120, blank=True)
    outputs = models.JSONField(default=list, blank=True)
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
        indexes = [models.Index(fields=("status", "priority", "queued_at"))]

    def __str__(self) -> str:
        return f"{self.kind}:{self.id} [{self.status}]"


class DeferredGenerationRequest(UUIDTimeStampedModel):
    """Persisted first/last-frame dependency between two generation tasks.

    The provisional prompt is reviewable immediately, while the formal
    PromptArtifact -> GeneratedWorkflow -> GenerationTask chain is created only
    after the declared upstream video output has a verified signed manifest and
    its last decodable frame has been materialized as an Asset.
    """

    class FrameSlot(models.TextChoices):
        FIRST_FRAME = "first_frame", "首帧"
        LAST_FRAME = "last_frame", "尾帧"

    class Status(models.TextChoices):
        PREPARING_PROMPT = "preparing_prompt", "正在准备提示词"
        WAITING_UPSTREAM = "waiting_upstream", "等待上游视频"
        EXTRACTING = "extracting", "正在提取末帧"
        MATERIALIZING = "materializing", "正在物化正式任务"
        MATERIALIZED = "materialized", "已物化"
        FAILED = "failed", "已失败"
        STALE = "stale", "目标输入已过期"
        CANCELLED = "cancelled", "已取消"

    SOURCE_OUTPUT_ROLE_PRIMARY_VIDEO = "primary_video"
    ACTIVE_STATUSES = (
        Status.PREPARING_PROMPT,
        Status.WAITING_UPSTREAM,
        Status.EXTRACTING,
        Status.MATERIALIZING,
    )

    owner = models.ForeignKey(
        settings.AUTH_USER_MODEL,
        on_delete=models.CASCADE,
        related_name="deferred_generation_requests",
    )
    project = models.ForeignKey(
        SeriesProject,
        on_delete=models.CASCADE,
        related_name="deferred_generation_requests",
    )
    source_task = models.ForeignKey(
        GenerationTask,
        on_delete=models.PROTECT,
        related_name="downstream_deferred_requests",
    )
    source_output_role = models.CharField(
        max_length=32,
        default=SOURCE_OUTPUT_ROLE_PRIMARY_VIDEO,
        editable=False,
    )
    target_shot = models.ForeignKey(
        Shot,
        on_delete=models.PROTECT,
        related_name="deferred_generation_requests",
    )
    frame_slot = models.CharField(max_length=20, choices=FrameSlot.choices)
    target_workflow_version = models.ForeignKey(
        "WorkflowTemplateVersion",
        on_delete=models.PROTECT,
        related_name="deferred_generation_requests",
    )
    target_workflow_snapshot = models.JSONField(default=dict)
    target_workflow_snapshot_sha256 = models.CharField(max_length=64)
    idempotency_key = models.CharField(max_length=120)
    request_fingerprint = models.CharField(max_length=64)
    target_input_fingerprint = models.CharField(max_length=64)
    target_workflow_overrides = models.JSONField(default=dict, blank=True)
    status = models.CharField(
        max_length=24,
        choices=Status.choices,
        default=Status.PREPARING_PROMPT,
    )
    provisional_prompt_text = models.TextField()
    provisional_negative_prompt = models.TextField(blank=True)
    provisional_prompt_sha256 = models.CharField(max_length=64)
    resolved_source_output_index = models.PositiveIntegerField(null=True, blank=True)
    source_artifact_sha256 = models.CharField(max_length=64, blank=True)
    source_manifest_sha256 = models.CharField(max_length=64, blank=True)
    derived_asset = models.ForeignKey(
        Asset,
        null=True,
        blank=True,
        on_delete=models.PROTECT,
        related_name="derived_deferred_generation_requests",
    )
    materialized_task = models.OneToOneField(
        GenerationTask,
        null=True,
        blank=True,
        on_delete=models.PROTECT,
        related_name="deferred_generation_request",
    )
    priority = models.SmallIntegerField(default=0)
    attempts = models.PositiveSmallIntegerField(default=0)
    max_attempts = models.PositiveSmallIntegerField(
        validators=[MinValueValidator(1)],
    )
    retry_backoff_seconds = models.PositiveIntegerField(
        validators=[MinValueValidator(1)],
    )
    next_attempt_at = models.DateTimeField(default=timezone.now)
    extended_generation_confirmed = models.BooleanField(default=False)
    worker_id = models.CharField(max_length=120, blank=True)
    claim_token = models.CharField(max_length=64, blank=True)
    claimed_at = models.DateTimeField(null=True, blank=True)
    heartbeat_at = models.DateTimeField(null=True, blank=True)
    error_code = models.CharField(max_length=80, blank=True)
    error_message = models.TextField(blank=True)
    started_at = models.DateTimeField(null=True, blank=True)
    finished_at = models.DateTimeField(null=True, blank=True)

    class Meta:
        ordering = ("-priority", "created_at")
        constraints = [
            models.UniqueConstraint(
                fields=("project", "idempotency_key"),
                name="uk_deferred_project_idem",
            ),
            models.UniqueConstraint(
                fields=("target_shot", "frame_slot"),
                condition=Q(status__in=(
                    "preparing_prompt", "waiting_upstream", "extracting", "materializing",
                )),
                name="uk_deferred_active_slot",
            ),
            models.CheckConstraint(
                condition=Q(attempts__lte=models.F("max_attempts")),
                name="ck_deferred_attempts_lte_max",
            ),
            models.CheckConstraint(
                condition=(
                    Q(status="materialized", materialized_task__isnull=False)
                    | (~Q(status="materialized") & Q(materialized_task__isnull=True))
                ),
                name="ck_deferred_materialized_task",
            ),
        ]
        indexes = [
            models.Index(
                fields=("status", "next_attempt_at", "created_at"),
                name="idx_deferred_due",
            ),
            models.Index(
                fields=("source_task", "status"),
                name="idx_deferred_source_status",
            ),
        ]

    def __str__(self) -> str:
        return f"{self.source_task_id}->{self.target_shot_id}:{self.frame_slot} [{self.status}]"


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
    source_tasks = models.ManyToManyField(GenerationTask, blank=True, related_name="assembly_jobs")
    scope = models.CharField(max_length=16, choices=Scope.choices)
    status = models.CharField(max_length=16, choices=Status.choices, default=Status.PLANNED, db_index=True)
    dry_run = models.BooleanField(default=True)
    has_audio = models.BooleanField(default=False)
    output_path = models.CharField(max_length=1000, blank=True)
    concat_manifest_path = models.CharField(max_length=1000, blank=True)
    ffmpeg_command = models.JSONField(default=list, blank=True)
    source_signature = models.CharField(max_length=64, blank=True, db_index=True)
    source_contract_version = models.PositiveSmallIntegerField(default=2)
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
        null=True,
        blank=True,
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

    class MediaType(models.TextChoices):
        IMAGE = "image", "图片"
        VIDEO = "video", "视频"
        AUDIO = "audio", "音频"
        SCRIPT = "script", "剧本"

    class ExecutionKind(models.TextChoices):
        DEPLOYMENT = "deployment", "模型部署"
        COMFY_WORKFLOW = "comfy_workflow", "ComfyUI 工作流"

    class Status(models.TextChoices):
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
        on_delete=models.SET_NULL,
        related_name="asset_generation_jobs",
    )
    model_deployment = models.ForeignKey(
        "ModelDeployment",
        on_delete=models.PROTECT,
        related_name="asset_generation_jobs",
    )
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
        default=Status.QUEUED,
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
        ]
        constraints = [
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
    """Append-only staff evidence resolving one uncertain library generation."""

    class Outcome(models.TextChoices):
        CONFIRMED_NOT_SUBMITTED = "confirmed_not_submitted", "确认未提交"
        CONFIRMED_REMOTE_FAILED = "confirmed_remote_failed", "确认远端失败"
        CONFIRMED_REMOTE_CANCELLED = "confirmed_remote_cancelled", "确认远端取消"
        CONFIRMED_REMOTE_SUCCEEDED = "confirmed_remote_succeeded", "确认远端成功"

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


class PromptFieldDefinition(UUIDTimeStampedModel):
    """Typed, reusable prompt field exposed by the control plane.

    The definition is data, not an executable template.  A published profile
    may only reference these stable field identities through an allow-listed
    rule model below.
    """

    class ValueType(models.TextChoices):
        TEXT = "text", "短文本"
        LONG_TEXT = "long_text", "长文本"
        STRING_LIST = "string_list", "文本列表"
        BOOLEAN = "boolean", "布尔值"
        NUMBER = "number", "数值"
        ENUM = "enum", "枚举"

    key = models.CharField(
        max_length=100,
        unique=True,
        validators=[RegexValidator(r"^[a-z][a-z0-9_.-]{1,99}$")],
    )
    label = models.CharField(max_length=160)
    category = models.CharField(
        max_length=80,
        validators=[RegexValidator(r"^[a-z][a-z0-9_.-]{1,79}$")],
    )
    description = models.TextField(blank=True)
    impact = models.TextField(blank=True)
    value_type = models.CharField(max_length=24, choices=ValueType.choices)
    default_value = models.JSONField(null=True, blank=True, default=None)
    value_options = models.JSONField(default=list, blank=True)
    is_required = models.BooleanField(default=False)
    is_active = models.BooleanField(default=True)
    display_order = models.PositiveIntegerField(default=0)

    class Meta:
        ordering = ("display_order", "key")
        indexes = [models.Index(fields=("category", "is_active", "display_order"))]

    def __str__(self) -> str:
        return self.label


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
    contract_schema_version = models.PositiveIntegerField(default=1)
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
        DEFINITION_DEFAULT = "definition.default", "字段默认值"
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
        SHOT_TITLE = "shot.title", "镜头标题"
        SHOT_NARRATIVE = "shot.narrative", "镜头叙事"
        SHOT_DETAIL = "shot.detailed_description", "镜头详细描述"
        SHOT_CAMERA = "shot.camera", "镜头语言"
        SHOT_ACTION = "shot.action", "动作"
        SHOT_DIALOGUE = "shot.dialogue", "对白"
        SHOT_SOUND_EFFECTS = "shot.sound_effects", "音效"
        SHOT_MUSIC = "shot.music", "音乐"
        SHOT_NEGATIVE_PROMPT = "shot.negative_prompt", "镜头负面提示"
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
        PromptFieldDefinition,
        on_delete=models.PROTECT,
        related_name="profile_rules",
    )
    source_selector = models.CharField(max_length=80, choices=SourceSelector.choices)
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


class PromptProfileBinding(UUIDTimeStampedModel):
    """Runtime selection rule for one published prompt profile version."""

    key = models.CharField(
        max_length=120,
        unique=True,
        validators=[RegexValidator(r"^[a-z][a-z0-9_.-]{1,119}$")],
    )
    name = models.CharField(max_length=160)
    operation = models.CharField(
        max_length=100,
        validators=[RegexValidator(r"^[a-z][a-z0-9_.-]{1,99}$")],
    )
    model_family = models.CharField(
        max_length=100,
        validators=[RegexValidator(r"^[a-z][a-z0-9_.-]{1,99}$")],
    )
    mode = models.CharField(
        max_length=40,
        blank=True,
        validators=[RegexValidator(r"^[A-Za-z0-9][A-Za-z0-9_.-]{0,39}$")],
    )
    model_profile_version = models.ForeignKey(
        ModelProfileVersion,
        null=True,
        blank=True,
        on_delete=models.PROTECT,
        related_name="prompt_profile_bindings",
    )
    prompt_profile_version = models.ForeignKey(
        PromptProfileVersion,
        on_delete=models.PROTECT,
        related_name="bindings",
    )
    priority = models.SmallIntegerField(
        default=0,
        validators=[MinValueValidator(-1000), MaxValueValidator(1000)],
    )
    is_active = models.BooleanField(default=True)

    class Meta:
        ordering = ("operation", "model_family", "-priority", "key")
        indexes = [models.Index(fields=("operation", "model_family", "is_active"))]

    def __str__(self) -> str:
        return self.name


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
    generated_workflow = models.ForeignKey(
        GeneratedWorkflow,
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
                        generated_workflow__isnull=True,
                        generation_task__isnull=True,
                    )
                    | Q(import_kind="execution_snapshot", workflow_version__isnull=True)
                    | Q(
                        import_kind="candidate",
                        workflow_version__isnull=True,
                        generated_workflow__isnull=True,
                        generation_task__isnull=True,
                    )
                    | Q(
                        import_kind="ignored",
                        workflow_version__isnull=True,
                        generated_workflow__isnull=True,
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


class WorkflowSmokeAttempt(UUIDTimeStampedModel):
    """Durable, single-submit execution of one workflow smoke test."""

    class Status(models.TextChoices):
        QUEUED = "queued", "排队中"
        SUBMITTING = "submitting", "正在提交"
        RUNNING = "running", "执行中"
        CANCEL_REQUESTED = "cancel_requested", "取消中"
        SUCCEEDED = "succeeded", "成功"
        FAILED = "failed", "失败"
        CANCELLED = "cancelled", "已取消"
        UNCERTAIN = "uncertain", "远端状态不确定"

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
    deadline_started_at = models.DateTimeField()
    hard_deadline_at = models.DateTimeField(db_index=True)
    idle_deadline_at = models.DateTimeField(db_index=True)
    last_meaningful_progress_at = models.DateTimeField()
    progress_marker = models.CharField(max_length=191, blank=True)
    cancel_requested_at = models.DateTimeField(null=True, blank=True)
    cancel_submitted_at = models.DateTimeField(null=True, blank=True)
    finished_at = models.DateTimeField(null=True, blank=True)
    error_code = models.CharField(max_length=100, blank=True)
    error_message = models.TextField(blank=True)
    result = models.JSONField(default=dict, blank=True)
    objects = WorkflowSmokeAttemptManager()

    class Meta:
        ordering = ("-created_at",)
        constraints = [
            models.UniqueConstraint(
                fields=("workflow_version", "deployment"),
                condition=Q(status__in=(
                    "queued", "submitting", "running", "cancel_requested",
                )),
                name="one_active_workflow_smoke",
            ),
        ]
        indexes = [models.Index(
            fields=("status", "next_poll_at"),
            name="studio_work_status_70a205_idx",
        )]

    _IMMUTABLE_EXECUTION_FIELDS = (
        "workflow_version_id", "deployment_id", "created_by_id", "submission_token",
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

    # Null means historical/unscoped evidence, never a certified prompt strategy.
    prompt_profile_binding = models.ForeignKey(
        "PromptProfileBinding", null=True, blank=True, on_delete=models.PROTECT,
        related_name="deployment_smoke_snapshots",
    )
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


class ModelDeploymentSmokeAttempt(UUIDTimeStampedModel):
    """Durable single-submit boundary for one real deployment smoke request."""

    prompt_profile_binding = models.ForeignKey(
        "PromptProfileBinding", null=True, blank=True, on_delete=models.PROTECT,
        related_name="deployment_smoke_attempts",
    )
    prompt_profile_version = models.ForeignKey(
        "PromptProfileVersion", null=True, blank=True, on_delete=models.PROTECT,
        related_name="deployment_smoke_attempts",
    )
    prompt_profile_contract_sha256 = models.CharField(max_length=64, blank=True)

    class Status(models.TextChoices):
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
    submission_started_at = models.DateTimeField(default=timezone.now)
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

    class Meta:
        ordering = ("deployment", "-created_at")
        constraints = [models.UniqueConstraint(
            fields=("deployment", "operation"),
            condition=Q(status__in=(
                "queued", "submitting", "submitted", "cancel_requested",
            )),
            name="one_active_deployment_smoke",
        )]
        indexes = [models.Index(
            fields=("deployment", "operation", "status", "created_at"),
            name="studio_smoke_unresolved_idx",
        )]


class ModelDeploymentSmokeAttemptResolution(UUIDTimeStampedModel):
    """Append-only operator evidence resolving an uncertain smoke submission."""

    class Outcome(models.TextChoices):
        CONFIRMED_NOT_SUBMITTED = "confirmed_not_submitted", "确认未提交"
        CONFIRMED_REMOTE_SUBMITTED = "confirmed_remote_submitted", "确认远端已提交"
        CONFIRMED_REMOTE_FAILED = "confirmed_remote_failed", "确认远端失败"

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
    recipe_version = models.ForeignKey(
        "ProductionRecipeVersion",
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
    class Status(models.TextChoices):
        QUEUED = "queued", "排队中"
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
    recipe_version = models.ForeignKey(
        ProductionRecipeVersion,
        on_delete=models.PROTECT,
        related_name="production_runs",
    )
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
    budget_limit = models.DecimalField(max_digits=12, decimal_places=4, null=True, blank=True)
    cost_currency = models.CharField(max_length=12, blank=True)
    estimated_cost_total = models.DecimalField(max_digits=14, decimal_places=6, default=0)
    committed_cost_total = models.DecimalField(max_digits=14, decimal_places=6, default=0)
    actual_cost_total = models.DecimalField(max_digits=14, decimal_places=6, default=0)
    max_generation_tasks = models.PositiveIntegerField(default=0)
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
            models.CheckConstraint(
                condition=Q(budget_limit__isnull=True) | Q(budget_limit__gte=0),
                name="production_run_budget_nonnegative",
            ),
            models.CheckConstraint(
                condition=(
                    Q(estimated_cost_total__gte=0)
                    & Q(committed_cost_total__gte=0)
                    & Q(actual_cost_total__gte=0)
                ),
                name="production_run_costs_nonnegative",
            ),
        ]
        indexes = [models.Index(fields=("owner", "status", "created_at"))]


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
    class Status(models.TextChoices):
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
    unit_key = models.CharField(max_length=191)
    attempt_number = models.PositiveIntegerField(validators=[MinValueValidator(1)])
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
    prompt_profile_binding_key = models.CharField(max_length=120, blank=True)
    prompt_profile_contract_sha256 = models.CharField(max_length=64, blank=True)
    generation_task = models.ForeignKey(
        GenerationTask,
        null=True,
        blank=True,
        on_delete=models.PROTECT,
        related_name="platform_attempts",
    )
    status = models.CharField(max_length=16, choices=Status.choices, default=Status.QUEUED)
    route_index = models.PositiveSmallIntegerField(null=True, blank=True)
    operation = models.CharField(max_length=100, blank=True)
    is_external = models.BooleanField(default=False)
    provider_request_id = models.CharField(max_length=191, blank=True)
    request_sha256 = models.CharField(max_length=64)
    request_snapshot = models.JSONField(default=dict)
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

    class Meta:
        ordering = ("stage_run", "unit_key", "-attempt_number")
        constraints = [
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
        indexes = [models.Index(fields=("status", "next_poll_at"))]


class GenerationAttemptResolution(UUIDTimeStampedModel):
    class Outcome(models.TextChoices):
        CONFIRMED_NOT_SUBMITTED = "confirmed_not_submitted", "确认未提交"
        CONFIRMED_REMOTE_SUBMITTED = "confirmed_remote_submitted", "确认远端已提交"
        CONFIRMED_REMOTE_FAILED = "confirmed_remote_failed", "确认远端失败"
        ACTUAL_COST_CONFIRMED = "actual_cost_confirmed", "确认实际费用"
        CANCEL_REQUESTED_IGNORE_LATE = "cancel_requested_ignore_late", "取消并忽略晚到结果"
        LATE_RESULT_IGNORED = "late_result_ignored", "已忽略晚到结果"
        KNOWN_RESPONSE_REJECTED = "known_response_rejected", "已知响应未通过校验"

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
    """Append-only human/evaluator decision for one generated variant group."""

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
    )

    class Meta:
        ordering = ("stage_run", "variant_group", "revision")
        constraints = [
            models.UniqueConstraint(
                fields=("stage_run", "variant_group", "revision"),
                name="unique_artifact_selection_revision",
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

    ComfyUI outputs and managed provider artifacts converge here.  The source
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
    generation_task = models.ForeignKey(
        GenerationTask,
        null=True,
        blank=True,
        on_delete=models.PROTECT,
        related_name="assembly_source_items",
    )
    generated_artifact = models.ForeignKey(
        GeneratedArtifact,
        null=True,
        blank=True,
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
            models.CheckConstraint(
                condition=(
                    Q(generation_task__isnull=False, generated_artifact__isnull=True)
                    | Q(generation_task__isnull=True, generated_artifact__isnull=False)
                ),
                name="assembly_source_exactly_one_origin",
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
