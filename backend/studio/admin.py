from django.contrib import admin

from studio.models import (
    Asset,
    AssemblyJob,
    CharacterBible,
    ConsistencyLock,
    Episode,
    EpisodeCreativeDraft,
    GeneratedWorkflow,
    GenerationTask,
    PromptArtifact,
    PromptFieldTranslation,
    PropBible,
    ReleaseBundle,
    SceneBible,
    SeriesProject,
    Shot,
    Style,
    WorkflowTemplate,
)


@admin.register(Style)
class StyleAdmin(admin.ModelAdmin):
    list_display = ("name", "category", "owner", "is_system", "is_active", "updated_at")
    list_filter = ("category", "is_system", "is_active")
    search_fields = ("name", "slug", "description")


@admin.register(Asset)
class AssetAdmin(admin.ModelAdmin):
    list_display = ("name", "kind", "style", "owner", "is_active")
    list_filter = ("kind", "style", "is_active")
    search_fields = ("name", "description", "local_path", "checksum")


@admin.register(SeriesProject)
class SeriesProjectAdmin(admin.ModelAdmin):
    list_display = ("title", "owner", "status", "style", "target_model", "updated_at")
    list_filter = ("status", "style", "target_model")
    search_fields = ("title", "logline", "owner__username")


@admin.register(CharacterBible, SceneBible, PropBible)
class BibleAdmin(admin.ModelAdmin):
    list_display = ("name", "project", "slug", "is_locked", "updated_at")
    list_filter = ("is_locked",)
    search_fields = ("name", "slug", "visual_identity")


@admin.register(ConsistencyLock)
class ConsistencyLockAdmin(admin.ModelAdmin):
    list_display = ("project", "scope", "lock_key", "version", "is_active", "updated_at")
    list_filter = ("scope", "is_active")
    search_fields = ("project__title", "lock_key")


@admin.register(Episode)
class EpisodeAdmin(admin.ModelAdmin):
    list_display = ("project", "number", "title", "status", "updated_at")
    list_filter = ("status",)
    search_fields = ("project__title", "title", "synopsis")


@admin.register(EpisodeCreativeDraft)
class EpisodeCreativeDraftAdmin(admin.ModelAdmin):
    list_display = (
        "episode", "generation_no", "status", "revision", "creativity", "updated_at",
    )
    list_filter = ("status", "creativity", "provider")
    search_fields = ("project__title", "episode__title", "variation_note", "error_message")
    readonly_fields = (
        "context_hash", "context_snapshot", "draft_data", "validation", "warnings",
        "runtime_metadata", "revision_history", "confirmed_at", "applied_at",
    )


@admin.register(Shot)
class ShotAdmin(admin.ModelAdmin):
    list_display = ("episode", "number", "title", "media_type", "h3_mode", "duration_ms", "include_audio")
    list_filter = ("media_type", "h3_mode", "include_audio")
    search_fields = ("title", "narrative", "action")


@admin.register(PromptArtifact)
class PromptArtifactAdmin(admin.ModelAdmin):
    list_display = ("project", "episode", "shot", "prompt_type", "h3_mode", "revision", "created_at")
    list_filter = ("prompt_type", "h3_mode", "is_multi_shot", "include_audio")
    search_fields = ("prompt_text", "content_hash")
    readonly_fields = ("content_hash", "consistency_snapshot", "structured_payload")


@admin.register(PromptFieldTranslation)
class PromptFieldTranslationAdmin(admin.ModelAdmin):
    list_display = (
        "project", "scope_type", "field_key", "status", "revision", "updated_at",
    )
    list_filter = ("scope_type", "status", "provider")
    search_fields = ("project__title", "field_key", "source_text", "translated_text")
    readonly_fields = (
        "source_hash", "revision_history", "runtime_metadata", "confirmed_at",
    )


@admin.register(WorkflowTemplate)
class WorkflowTemplateAdmin(admin.ModelAdmin):
    list_display = ("name", "provider", "output_kind", "model", "version", "is_system", "is_active")
    list_filter = ("provider", "output_kind", "is_system", "is_active")
    search_fields = ("name", "slug", "description")


@admin.register(GeneratedWorkflow)
class GeneratedWorkflowAdmin(admin.ModelAdmin):
    list_display = ("project", "episode", "template", "content_hash", "created_at")
    search_fields = ("project__title", "content_hash")
    readonly_fields = ("workflow_json", "injected_values", "content_hash")


@admin.register(GenerationTask)
class GenerationTaskAdmin(admin.ModelAdmin):
    list_display = ("id", "project", "kind", "status", "progress", "attempts", "priority", "queued_at")
    list_filter = ("kind", "status")
    search_fields = ("comfy_prompt_id", "project__title", "error_message")
    readonly_fields = ("outputs", "error_message", "queued_at", "started_at", "finished_at")


@admin.register(AssemblyJob)
class AssemblyJobAdmin(admin.ModelAdmin):
    list_display = (
        "id", "project", "episode", "scope", "status", "attempts",
        "worker_id", "dry_run", "has_audio", "queued_at",
    )
    list_filter = ("scope", "status", "dry_run", "has_audio")
    search_fields = ("project__title", "output_path", "error_message")
    readonly_fields = (
        "ffmpeg_command", "concat_manifest_path", "source_signature", "claim_token",
        "error_message", "heartbeat_at", "started_at", "finished_at",
    )


@admin.register(ReleaseBundle)
class ReleaseBundleAdmin(admin.ModelAdmin):
    list_display = (
        "id", "project", "version", "status", "owner", "sealed_at", "released_at",
    )
    list_filter = ("status",)
    search_fields = ("project__title", "title", "manifest_sha256")
    readonly_fields = tuple(field.name for field in ReleaseBundle._meta.fields)

    def has_add_permission(self, request):
        return False

    def has_delete_permission(self, request, obj=None):
        return False
