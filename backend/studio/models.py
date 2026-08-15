from __future__ import annotations

import uuid
from pathlib import Path

from django.conf import settings
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


class UUIDTimeStampedModel(models.Model):
    id = models.UUIDField(primary_key=True, default=uuid.uuid4, editable=False)
    created_at = models.DateTimeField(auto_now_add=True)
    updated_at = models.DateTimeField(auto_now=True)

    class Meta:
        abstract = True


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
        OUTPUT_IMAGE = "output_image", "生成图片"
        OUTPUT_VIDEO = "output_video", "生成视频"

    owner = models.ForeignKey(settings.AUTH_USER_MODEL, null=True, blank=True, on_delete=models.CASCADE, related_name="assets")
    style = models.ForeignKey(Style, null=True, blank=True, on_delete=models.SET_NULL, related_name="assets")
    kind = models.CharField(max_length=32, choices=Kind.choices)
    name = models.CharField(max_length=160)
    description = models.TextField(blank=True)
    prompt_hint = models.TextField(blank=True)
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
    target_model = models.CharField(max_length=80, default="minimax-h3")
    language = models.CharField(max_length=32, default="Chinese")
    aspect_ratio = models.CharField(max_length=16, default="9:16")
    width = models.PositiveIntegerField(default=768)
    height = models.PositiveIntegerField(default=1344)
    fps = models.PositiveSmallIntegerField(default=24, validators=[MinValueValidator(1), MaxValueValidator(120)])
    default_shot_duration_ms = models.PositiveIntegerField(default=5000, validators=[MinValueValidator(100)])
    continuity_seed = models.BigIntegerField(default=20260808)
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

    class H3Mode(models.TextChoices):
        T2VA = "T2VA", "Text-to-video with audio"
        I2VA = "I2VA", "Image-to-video with audio"
        FL2VA = "FL2VA", "First/last frame-to-video with audio"
        L2VA = "L2VA", "Last-frame-to-video with audio"
        REF2VA = "Ref2VA", "Reference-to-video with audio"

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
    duration_ms = models.PositiveIntegerField(default=5000, validators=[MinValueValidator(100)])
    media_type = models.CharField(max_length=12, choices=MediaType.choices, default=MediaType.VIDEO)
    h3_mode = models.CharField(max_length=12, choices=H3Mode.choices, default=H3Mode.T2VA)
    include_audio = models.BooleanField(default=True)
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
    prompt_type = models.CharField(max_length=12, choices=PromptType.choices)
    model = models.CharField(max_length=80, default="minimax-h3")
    h3_mode = models.CharField(max_length=12, choices=Shot.H3Mode.choices, default=Shot.H3Mode.T2VA)
    revision = models.PositiveIntegerField(default=1)
    is_multi_shot = models.BooleanField(default=False)
    include_audio = models.BooleanField(default=True)
    total_duration_ms = models.PositiveIntegerField(default=3500)
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


class EpisodeCreativeDraft(UUIDTimeStampedModel):
    """Review-gated local-Qwen storyboard proposal for one existing episode.

    A creative draft is deliberately isolated from :class:`Episode` and
    :class:`Shot`.  Generation, regeneration, and manual editing only mutate
    this row (or create a successor row).  Formal production data changes only
    through the explicit ``apply`` action after a current-context confirmation.
    """

    class Status(models.TextChoices):
        DRAFT = "draft", "待审核"
        CONFIRMED = "confirmed", "已确认待应用"
        APPLIED = "applied", "已应用"
        SUPERSEDED = "superseded", "已被新版本替代"
        FAILED = "failed", "生成失败"

    class Creativity(models.TextChoices):
        CONSERVATIVE = "conservative", "保守"
        BALANCED = "balanced", "均衡"
        BOLD = "bold", "大胆"

    project = models.ForeignKey(
        SeriesProject, on_delete=models.CASCADE, related_name="creative_drafts",
    )
    episode = models.ForeignKey(
        Episode, on_delete=models.CASCADE, related_name="creative_drafts",
    )
    created_by = models.ForeignKey(
        settings.AUTH_USER_MODEL, on_delete=models.CASCADE,
        related_name="episode_creative_drafts",
    )
    parent_draft = models.ForeignKey(
        "self", null=True, blank=True, on_delete=models.SET_NULL,
        related_name="regenerations",
    )
    status = models.CharField(
        max_length=24, choices=Status.choices, default=Status.DRAFT,
    )
    revision = models.PositiveIntegerField(default=1)
    generation_no = models.PositiveIntegerField(default=1)
    context_hash = models.CharField(max_length=64, db_index=True)
    context_snapshot = models.JSONField(default=dict)
    input_settings = models.JSONField(default=dict)
    creativity = models.CharField(
        max_length=24, choices=Creativity.choices, default=Creativity.BALANCED,
    )
    variation_note = models.TextField(blank=True)
    draft_data = models.JSONField(default=dict, blank=True)
    validation = models.JSONField(default=dict, blank=True)
    warnings = models.JSONField(default=list, blank=True)
    error_message = models.TextField(blank=True)
    provider = models.CharField(max_length=80, default="local_qwen")
    model = models.CharField(max_length=200, blank=True)
    runtime_metadata = models.JSONField(default=dict, blank=True)
    revision_history = models.JSONField(default=list, blank=True)
    confirmed_by = models.ForeignKey(
        settings.AUTH_USER_MODEL, null=True, blank=True, on_delete=models.SET_NULL,
        related_name="confirmed_episode_creative_drafts",
    )
    confirmed_at = models.DateTimeField(null=True, blank=True)
    applied_by = models.ForeignKey(
        settings.AUTH_USER_MODEL, null=True, blank=True, on_delete=models.SET_NULL,
        related_name="applied_episode_creative_drafts",
    )
    applied_at = models.DateTimeField(null=True, blank=True)
    applied_shot_ids = models.JSONField(default=list, blank=True)

    class Meta:
        ordering = ("-created_at",)
        constraints = [
            models.UniqueConstraint(
                fields=("episode", "generation_no"),
                name="unique_creative_generation_per_episode",
            ),
        ]
        indexes = [
            models.Index(
                fields=("project", "episode", "status"),
                name="studio_ecdraft_scope_idx",
            ),
        ]

    def __str__(self) -> str:
        return f"E{self.episode.number} creative v{self.generation_no} ({self.status})"


class ShotDetailDraft(UUIDTimeStampedModel):
    """Review-gated high-detail expansion for one formal :class:`Shot`.

    Unlike ``EpisodeCreativeDraft`` this proposal never creates or replaces
    timeline shots.  It captures the exact target shot, adjacent continuity
    boundaries and relevant Bible context used by one local-Qwen request.
    Applying a confirmed draft is the only path that writes
    ``Shot.detailed_description``.
    """

    class Status(models.TextChoices):
        DRAFT = "draft", "待审核"
        CONFIRMED = "confirmed", "已确认待应用"
        APPLIED = "applied", "已应用"
        SUPERSEDED = "superseded", "已被新版本替代"
        FAILED = "failed", "生成失败"

    project = models.ForeignKey(
        SeriesProject, on_delete=models.CASCADE, related_name="shot_detail_drafts",
    )
    episode = models.ForeignKey(
        Episode, on_delete=models.CASCADE, related_name="shot_detail_drafts",
    )
    shot = models.ForeignKey(
        Shot, on_delete=models.CASCADE, related_name="detail_drafts",
    )
    created_by = models.ForeignKey(
        settings.AUTH_USER_MODEL, on_delete=models.CASCADE,
        related_name="shot_detail_drafts",
    )
    parent_draft = models.ForeignKey(
        "self", null=True, blank=True, on_delete=models.SET_NULL,
        related_name="regenerations",
    )
    status = models.CharField(
        max_length=24, choices=Status.choices, default=Status.DRAFT,
    )
    revision = models.PositiveIntegerField(default=1)
    generation_no = models.PositiveIntegerField(default=1)
    context_hash = models.CharField(max_length=64, db_index=True)
    context_snapshot = models.JSONField(default=dict)
    input_settings = models.JSONField(default=dict)
    variation_note = models.TextField(blank=True)
    draft_data = models.JSONField(default=dict, blank=True)
    validation = models.JSONField(default=dict, blank=True)
    warnings = models.JSONField(default=list, blank=True)
    error_message = models.TextField(blank=True)
    provider = models.CharField(max_length=80, default="local_qwen")
    model = models.CharField(max_length=200, blank=True)
    runtime_metadata = models.JSONField(default=dict, blank=True)
    revision_history = models.JSONField(default=list, blank=True)
    confirmed_by = models.ForeignKey(
        settings.AUTH_USER_MODEL, null=True, blank=True, on_delete=models.SET_NULL,
        related_name="confirmed_shot_detail_drafts",
    )
    confirmed_at = models.DateTimeField(null=True, blank=True)
    applied_by = models.ForeignKey(
        settings.AUTH_USER_MODEL, null=True, blank=True, on_delete=models.SET_NULL,
        related_name="applied_shot_detail_drafts",
    )
    applied_at = models.DateTimeField(null=True, blank=True)

    class Meta:
        ordering = ("-created_at",)
        constraints = [
            models.UniqueConstraint(
                fields=("shot", "generation_no"),
                name="unique_shot_detail_generation",
            ),
        ]
        indexes = [
            models.Index(
                fields=("project", "episode", "shot", "status"),
                name="studio_sdetail_scope_idx",
            ),
        ]

    def __str__(self) -> str:
        return f"E{self.episode.number}S{self.shot.number} detail v{self.generation_no} ({self.status})"


class ShotDetailDraftBatch(UUIDTimeStampedModel):
    """Durable episode-level orchestration for independent shot detail drafts."""

    class GenerationStatus(models.TextChoices):
        QUEUED = "queued", "排队中"
        RUNNING = "running", "生成中"
        COMPLETED = "completed", "已完成"
        COMPLETED_WITH_ERRORS = "completed_with_errors", "部分完成"
        FAILED = "failed", "已失败"
        CANCELLED = "cancelled", "已取消"

    class ApplyStatus(models.TextChoices):
        NOT_STARTED = "not_started", "未应用"
        COMPLETE = "complete", "已全部应用"

    project = models.ForeignKey(
        SeriesProject, on_delete=models.CASCADE, related_name="shot_detail_batches",
    )
    episode = models.ForeignKey(
        Episode, on_delete=models.CASCADE, related_name="shot_detail_batches",
    )
    created_by = models.ForeignKey(
        settings.AUTH_USER_MODEL, on_delete=models.CASCADE,
        related_name="shot_detail_batches",
    )
    request_id = models.UUIDField(unique=True, editable=False)
    provider = models.CharField(max_length=80, default="local_qwen")
    variation_note = models.TextField(blank=True)
    source_manifest_hash = models.CharField(max_length=64, db_index=True)
    policy_snapshot = models.JSONField(default=dict)
    config_hash = models.CharField(max_length=64)
    generation_status = models.CharField(
        max_length=32, choices=GenerationStatus.choices,
        default=GenerationStatus.QUEUED, db_index=True,
    )
    apply_status = models.CharField(
        max_length=20, choices=ApplyStatus.choices,
        default=ApplyStatus.NOT_STARTED,
    )
    revision = models.PositiveIntegerField(default=1)
    total_count = models.PositiveIntegerField(default=0)
    pending_count = models.PositiveIntegerField(default=0)
    succeeded_count = models.PositiveIntegerField(default=0)
    failed_count = models.PositiveIntegerField(default=0)
    stale_count = models.PositiveIntegerField(default=0)
    skipped_count = models.PositiveIntegerField(default=0)
    applied_count = models.PositiveIntegerField(default=0)
    started_at = models.DateTimeField(null=True, blank=True)
    finished_at = models.DateTimeField(null=True, blank=True)

    class Meta:
        ordering = ("-created_at",)
        constraints = [
            models.UniqueConstraint(
                fields=("episode",),
                condition=Q(generation_status__in=("queued", "running")),
                name="unique_active_shot_detail_batch",
            ),
        ]
        indexes = [
            models.Index(
                fields=("project", "episode", "generation_status", "created_at"),
                name="studio_sdbatch_scope_idx",
            ),
        ]


class ShotDetailDraftBatchItem(UUIDTimeStampedModel):
    """One frozen shot input and its independently persisted batch outcome."""

    class Status(models.TextChoices):
        PENDING = "pending", "待处理"
        RUNNING = "running", "处理中"
        SUCCEEDED = "succeeded", "已生成"
        FAILED = "failed", "失败"
        STALE = "stale", "源数据已变化"
        SKIPPED = "skipped", "已跳过"

    batch = models.ForeignKey(
        ShotDetailDraftBatch, on_delete=models.CASCADE, related_name="items",
    )
    shot = models.ForeignKey(
        Shot, on_delete=models.CASCADE, related_name="detail_batch_items",
    )
    ordinal = models.PositiveIntegerField()
    source_context_hash = models.CharField(max_length=64, db_index=True)
    before_snapshot = models.JSONField(default=dict)
    draft = models.OneToOneField(
        ShotDetailDraft, null=True, blank=True, on_delete=models.SET_NULL,
        related_name="batch_item",
    )
    status = models.CharField(
        max_length=20, choices=Status.choices, default=Status.PENDING,
        db_index=True,
    )
    attempts = models.PositiveSmallIntegerField(default=0)
    max_attempts = models.PositiveSmallIntegerField(
        default=3, validators=[MinValueValidator(1)],
    )
    next_attempt_at = models.DateTimeField(default=timezone.now, db_index=True)
    worker_id = models.CharField(max_length=160, blank=True)
    claim_token = models.UUIDField(null=True, blank=True, editable=False, db_index=True)
    claimed_at = models.DateTimeField(null=True, blank=True)
    heartbeat_at = models.DateTimeField(null=True, blank=True, db_index=True)
    finished_at = models.DateTimeField(null=True, blank=True)
    error_code = models.CharField(max_length=80, blank=True)
    error_message = models.TextField(blank=True)
    apply_run_id = models.UUIDField(null=True, blank=True, editable=False, db_index=True)
    applied_at = models.DateTimeField(null=True, blank=True)

    class Meta:
        ordering = ("batch", "ordinal")
        constraints = [
            models.UniqueConstraint(
                fields=("batch", "shot"), name="unique_shot_detail_batch_shot",
            ),
            models.UniqueConstraint(
                fields=("batch", "ordinal"), name="unique_shot_detail_batch_ordinal",
            ),
        ]
        indexes = [
            models.Index(
                fields=("status", "next_attempt_at", "created_at"),
                name="studio_sdbitem_claim_idx",
            ),
        ]


class WorkflowTemplate(UUIDTimeStampedModel):
    class OutputKind(models.TextChoices):
        VIDEO = "video", "视频"
        IMAGE = "image", "图片"

    owner = models.ForeignKey(settings.AUTH_USER_MODEL, null=True, blank=True, on_delete=models.CASCADE, related_name="workflow_templates")
    name = models.CharField(max_length=160)
    slug = models.SlugField(max_length=100)
    provider = models.CharField(max_length=80, default="comfyui")
    output_kind = models.CharField(max_length=12, choices=OutputKind.choices)
    model = models.CharField(max_length=80, default="minimax-h3")
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
    created_by = models.ForeignKey(settings.AUTH_USER_MODEL, on_delete=models.CASCADE, related_name="generated_workflows")
    workflow_json = models.JSONField(default=dict)
    injected_values = models.JSONField(default=dict)
    content_hash = models.CharField(max_length=64, db_index=True)

    class Meta:
        ordering = ("-created_at",)


class WorkerLease(models.Model):
    """Database mutex row used to serialize task claims across worker processes."""

    key = models.CharField(max_length=80, primary_key=True)
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
    backend = models.CharField(max_length=80, default="comfy_local_h3")
    model_profile = models.CharField(max_length=120, default="local_base_official")
    workflow_version = models.PositiveIntegerField(default=1)
    seed = models.BigIntegerField(default=0)
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
    progress_telemetry_state = models.CharField(
        max_length=20,
        choices=ProgressTelemetryState.choices,
        default=ProgressTelemetryState.UNKNOWN,
    )
    progress_telemetry_error = models.TextField(blank=True)
    attempts = models.PositiveSmallIntegerField(default=0)
    max_attempts = models.PositiveSmallIntegerField(default=3, validators=[MinValueValidator(1)])
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
    attempts = models.PositiveSmallIntegerField(default=0)
    max_attempts = models.PositiveSmallIntegerField(default=3, validators=[MinValueValidator(1)])
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
