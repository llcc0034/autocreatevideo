from django import forms
from django.contrib import admin
from django.db import transaction

from studio.models import (
    Asset,
    AssemblyJob,
    CharacterBible,
    ConsistencyLock,
    DeferredGenerationRequest,
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
from studio.services.script_bible import (
    ScriptBibleProvenanceProtectedError,
    validate_untrusted_script_bible_metadata,
)


class ImmutableProductionAuditAdmin(admin.ModelAdmin):
    """Expose immutable production evidence without an admin write bypass."""

    def get_readonly_fields(self, request, obj=None):
        return tuple(field.name for field in self.model._meta.fields)

    def has_add_permission(self, request):
        return False

    def has_delete_permission(self, request, obj=None):
        return False

    def has_change_permission(self, request, obj=None):
        return False


class BibleAdminForm(forms.ModelForm):
    """Keep reserved Script Bible provenance exclusive to the apply API."""

    class Meta:
        model = CharacterBible
        fields = "__all__"

    def clean_metadata(self):
        incoming = self.cleaned_data.get("metadata")
        previous = self.instance.metadata if self.instance.pk else {}
        try:
            validate_untrusted_script_bible_metadata(previous, incoming)
        except ScriptBibleProvenanceProtectedError as exc:
            raise forms.ValidationError(str(exc)) from exc
        return incoming


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
    form = BibleAdminForm
    list_display = ("name", "project", "slug", "is_locked", "updated_at")
    list_filter = ("is_locked",)
    search_fields = ("name", "slug", "visual_identity")

    def save_model(self, request, obj, form, change):
        """Recheck reserved provenance against the locked latest row."""

        model = type(obj)
        with transaction.atomic():
            if not change or not obj.pk:
                project = SeriesProject.objects.select_for_update().get(
                    pk=obj.project_id,
                )
                try:
                    validate_untrusted_script_bible_metadata({}, obj.metadata)
                except ScriptBibleProvenanceProtectedError as exc:
                    raise forms.ValidationError(str(exc)) from exc
                obj.project = project
                return super().save_model(request, obj, form, change)

            current_project_id = model.objects.filter(pk=obj.pk).values_list(
                "project_id", flat=True,
            ).first()
            if current_project_id is None:
                raise forms.ValidationError("Bible 已不存在，请刷新后重试。")
            requested_project_id = obj.project_id
            projects = {
                project.pk: project
                for project in SeriesProject.objects.select_for_update()
                .filter(pk__in={current_project_id, requested_project_id})
                .order_by("pk")
            }
            if len(projects) != len({current_project_id, requested_project_id}):
                raise forms.ValidationError("项目已不存在，请刷新后重试。")
            try:
                latest = model.objects.select_for_update().get(pk=obj.pk)
            except model.DoesNotExist as exc:
                raise forms.ValidationError("Bible 已不存在，请刷新后重试。") from exc
            if latest.project_id != current_project_id:
                raise forms.ValidationError(
                    "Bible 所属项目已被并发修改，请刷新后重试。",
                )
            try:
                validate_untrusted_script_bible_metadata(
                    latest.metadata, obj.metadata,
                )
            except ScriptBibleProvenanceProtectedError as exc:
                raise forms.ValidationError(str(exc)) from exc
            obj.project = projects[requested_project_id]
            return super().save_model(request, obj, form, change)


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
class PromptArtifactAdmin(ImmutableProductionAuditAdmin):
    list_display = ("project", "episode", "shot", "prompt_type", "h3_mode", "revision", "created_at")
    list_filter = ("prompt_type", "h3_mode", "is_multi_shot", "include_audio")
    search_fields = ("prompt_text", "content_hash")


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
class GeneratedWorkflowAdmin(ImmutableProductionAuditAdmin):
    list_display = ("project", "episode", "template", "content_hash", "created_at")
    search_fields = ("project__title", "content_hash")


@admin.register(GenerationTask)
class GenerationTaskAdmin(ImmutableProductionAuditAdmin):
    list_display = ("id", "project", "kind", "status", "progress", "attempts", "priority", "queued_at")
    list_filter = ("kind", "status")
    search_fields = ("comfy_prompt_id", "project__title", "error_message")


@admin.register(DeferredGenerationRequest)
class DeferredGenerationRequestAdmin(ImmutableProductionAuditAdmin):
    list_display = (
        "id", "project", "source_task", "target_shot", "frame_slot",
        "status", "attempts", "priority", "created_at",
    )
    list_filter = ("status", "frame_slot")
    search_fields = (
        "project__title", "idempotency_key", "request_fingerprint",
        "target_input_fingerprint", "error_code",
    )


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
