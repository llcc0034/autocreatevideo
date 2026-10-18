from django import forms
from django.contrib import admin
from django.contrib.auth import get_user_model
from django.contrib.auth.models import Group
from django.contrib.admin.sites import NotRegistered
from django.db import transaction

from studio.models import (
    Asset,
    ApprovalDecision,
    ArtifactSelectionDecision,
    AssemblyJob,
    CharacterBible,
    ConfigDefinition,
    ConfigRevision,
    ConsistencyLock,
    Episode,
    GeneratedArtifact,
    GenerationAttempt,
    GenerationAttemptResolution,
    GenerationTask,
    HostCapabilitySnapshot,
    ModelArtifact,
    ModelDeployment,
    ModelProfileVersion,
    PlatformLifecycleAuditEvent,
    PromptFieldTranslation,
    PropBible,
    ProductionAssemblyLink,
    ProductionRecipeVersion,
    ProductionRun,
    ProviderProfile,
    RecommendationCandidate,
    RecommendationDecision,
    RecommendationSnapshot,
    RuntimeHost,
    RuntimeHostProbeAttempt,
    ReleaseBundle,
    SceneBible,
    ScriptRevision,
    ScriptSource,
    SeriesProject,
    Shot,
    StageRun,
    StageRouteEvent,
    Style,
    WorkflowTemplate,
    WorkflowTemplateVersion,
    AccessControlAuditEvent,
    SecretAuditEvent,
    SecretBinding,
    SecretVersion,
    UserSecurityProfile,
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


for _auth_model in (get_user_model(), Group):
    try:
        admin.site.unregister(_auth_model)
    except NotRegistered:
        pass


@admin.register(
    UserSecurityProfile,
    AccessControlAuditEvent,
    SecretBinding,
    SecretVersion,
    SecretAuditEvent,
    PlatformLifecycleAuditEvent,
)
class SecurityAuditAdmin(ImmutableProductionAuditAdmin):
    pass


class BibleAdminForm(forms.ModelForm):
    """Edit entity metadata, not setting text or retired source provenance."""

    class Meta:
        model = CharacterBible
        fields = "__all__"

    def clean(self):
        data = super().clean()
        if "visual_identity" in self.data:
            raise forms.ValidationError("旧视觉锚点字段已停用；请在工作室主图/变体设定中另存修订。")
        return data

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
    search_fields = ("name", "description", "source_info", "local_path", "checksum")
    readonly_fields = ("source_info",)


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
    search_fields = ("name", "slug", "description")

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


@admin.register(Shot)
class ShotAdmin(admin.ModelAdmin):
    list_display = ("episode", "number", "title", "media_type", "conditioning_mode", "duration_ms", "include_audio")
    list_filter = ("media_type", "conditioning_mode", "include_audio")
    search_fields = ("title",)


@admin.register(PromptFieldTranslation)
class PromptFieldTranslationAdmin(ImmutableProductionAuditAdmin):
    list_display = (
        "project", "scope_type", "field_key", "status", "revision", "updated_at",
    )
    list_filter = ("scope_type", "status", "provider")
    search_fields = ("project__title", "field_key", "source_text", "translated_text")
    readonly_fields = (
        "source_hash", "revision_history", "runtime_metadata", "confirmed_at",
    )


@admin.register(WorkflowTemplate)
class WorkflowTemplateAdmin(ImmutableProductionAuditAdmin):
    list_display = ("name", "provider", "output_kind", "model", "version", "is_system", "is_active")
    list_filter = ("provider", "output_kind", "is_system", "is_active")
    search_fields = ("name", "slug", "description")


@admin.register(GenerationTask)
class GenerationTaskAdmin(ImmutableProductionAuditAdmin):
    list_display = ("id", "project", "kind", "status", "progress", "attempts", "priority", "queued_at")
    list_filter = ("kind", "status")
    search_fields = ("comfy_prompt_id", "project__title", "error_message")


@admin.register(AssemblyJob)
class AssemblyJobAdmin(ImmutableProductionAuditAdmin):
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
class ReleaseBundleAdmin(ImmutableProductionAuditAdmin):
    list_display = (
        "id", "project", "version", "status", "owner", "sealed_at", "released_at",
    )
    list_filter = ("status",)
    search_fields = ("project__title", "title", "manifest_sha256")
@admin.register(ProviderProfile)
class ProviderProfileAdmin(ImmutableProductionAuditAdmin):
    list_display = ("key", "name", "driver_type", "is_active", "is_system", "updated_at")
    list_filter = ("driver_type", "is_active", "is_system")
    search_fields = ("key", "name")


@admin.register(ModelProfileVersion)
class ModelProfileVersionAdmin(ImmutableProductionAuditAdmin):
    list_display = (
        "profile_key", "version", "model_identifier", "modality", "provider", "status",
    )
    list_filter = ("modality", "status", "provider")
    search_fields = ("profile_key", "model_identifier", "display_name")


@admin.register(ModelDeployment)
class ModelDeploymentAdmin(ImmutableProductionAuditAdmin):
    list_display = ("key", "name", "profile_version", "runtime_host", "status", "is_default")
    list_filter = ("status", "is_default", "runtime_host")
    search_fields = ("key", "name")


@admin.register(ModelArtifact)
class ModelArtifactAdmin(ImmutableProductionAuditAdmin):
    list_display = ("logical_name", "profile_version", "role", "filename", "status")
    list_filter = ("role", "status")
    search_fields = ("logical_name", "filename", "sha256")


@admin.register(WorkflowTemplateVersion)
class WorkflowTemplateVersionAdmin(ImmutableProductionAuditAdmin):
    list_display = (
        "template", "version", "provider", "status", "validation_state",
        "smoke_test_state", "is_current",
    )
    list_filter = ("status", "validation_state", "smoke_test_state", "is_current")
    search_fields = ("template__slug", "template__name")


@admin.register(ConfigDefinition)
class ConfigDefinitionAdmin(ImmutableProductionAuditAdmin):
    """Read-only mirror; managed configuration writes use the audited API.

    In particular, a security_boundary must never gain an admin-model write
    bypass. Only a superuser may change it through the formal settings/API
    path, where complete revision, checksum and lifecycle checks are enforced.
    """

    list_display = (
        "key", "domain", "configuration_class", "value_type",
        "is_required", "is_secret", "is_active",
    )
    list_filter = (
        "domain", "configuration_class", "value_type",
        "is_required", "is_secret", "is_active",
    )
    search_fields = ("key", "description")

    def has_change_permission(self, request, obj=None):
        return False


@admin.register(ConfigRevision)
class ConfigRevisionAdmin(ImmutableProductionAuditAdmin):
    """Immutable revision evidence; publish/activate only through audited API."""

    list_display = ("scope_type", "revision", "status", "project", "run", "published_at")
    list_filter = ("scope_type", "status")

    def has_change_permission(self, request, obj=None):
        return False


@admin.register(ScriptSource)
class ScriptSourceAdmin(ImmutableProductionAuditAdmin):
    list_display = ("project", "source_type", "source_format", "status", "owner", "updated_at")
    list_filter = ("source_type", "source_format", "status")
    search_fields = ("project__title", "original_name", "original_sha256")


@admin.register(ScriptRevision)
class ScriptRevisionAdmin(ImmutableProductionAuditAdmin):
    list_display = ("source", "revision", "status", "created_by", "confirmed_at")
    list_filter = ("status",)
    search_fields = ("source__project__title", "content_sha256")


@admin.register(ProductionRecipeVersion)
class ProductionRecipeVersionAdmin(ImmutableProductionAuditAdmin):
    list_display = ("recipe_key", "version", "name", "status", "published_at")
    list_filter = ("status",)
    search_fields = ("recipe_key", "name")


@admin.register(ProductionRun)
class ProductionRunAdmin(ImmutableProductionAuditAdmin):
    list_display = ("id", "project", "status", "current_stage_key", "owner", "created_at")
    list_filter = ("status", "recipe_version")
    search_fields = ("project__title", "idempotency_key", "blocked_code")


@admin.register(StageRun)
class StageRunAdmin(ImmutableProductionAuditAdmin):
    list_display = ("production_run", "ordinal", "stage_key", "mode", "status", "attempt_count")
    list_filter = ("status", "mode")
    search_fields = ("production_run__idempotency_key", "stage_key", "reason_code")


@admin.register(ApprovalDecision)
class ApprovalDecisionAdmin(ImmutableProductionAuditAdmin):
    list_display = ("stage_run", "revision", "decision", "decided_by", "created_at")
    list_filter = ("decision",)


@admin.register(GenerationAttempt)
class GenerationAttemptAdmin(ImmutableProductionAuditAdmin):
    list_display = ("stage_run", "unit_key", "attempt_number", "status", "generation_task")
    list_filter = ("status",)
    search_fields = ("unit_key", "provider_request_id", "request_sha256", "error_code")


@admin.register(GenerationAttemptResolution)
class GenerationAttemptResolutionAdmin(ImmutableProductionAuditAdmin):
    list_display = ("attempt", "revision", "outcome", "decided_by", "created_at")
    list_filter = ("outcome",)
    search_fields = ("attempt__unit_key", "provider_request_id", "evidence_sha256")


@admin.register(ArtifactSelectionDecision)
class ArtifactSelectionDecisionAdmin(ImmutableProductionAuditAdmin):
    list_display = ("stage_run", "variant_group", "revision", "artifact", "selected_by")
    search_fields = ("variant_group", "stage_run__production_run__idempotency_key")


@admin.register(StageRouteEvent)
class StageRouteEventAdmin(ImmutableProductionAuditAdmin):
    list_display = ("stage_run", "sequence", "unit_key", "route_index", "event_type", "code")
    search_fields = ("unit_key", "code", "stage_run__production_run__idempotency_key")


@admin.register(ProductionAssemblyLink)
class ProductionAssemblyLinkAdmin(ImmutableProductionAuditAdmin):
    list_display = ("production_run", "episode", "assembly_job", "created_at")
    search_fields = ("production_run__idempotency_key", "episode__title")


@admin.register(GeneratedArtifact)
class GeneratedArtifactAdmin(ImmutableProductionAuditAdmin):
    list_display = ("manifest_key", "production_run", "kind", "role", "status", "variant_index")
    list_filter = ("kind", "status", "storage_backend")
    search_fields = ("manifest_key", "object_key", "sha256")


@admin.register(HostCapabilitySnapshot, RecommendationSnapshot)
class PlatformSnapshotAdmin(ImmutableProductionAuditAdmin):
    list_display = ("id", "created_at")


@admin.register(RecommendationCandidate, RecommendationDecision)
class RecommendationAuditAdmin(ImmutableProductionAuditAdmin):
    list_display = ("id", "created_at")


@admin.register(RuntimeHost)
class RuntimeHostAdmin(admin.ModelAdmin):
    list_display = ("key", "name", "status", "last_seen_at", "updated_at")
    list_filter = ("status",)
    search_fields = ("key", "name", "fingerprint_sha256")


@admin.register(RuntimeHostProbeAttempt)
class RuntimeHostProbeAttemptAdmin(ImmutableProductionAuditAdmin):
    list_display = ("id", "host", "trigger", "status", "created_at", "finished_at")
    list_filter = ("trigger", "status")
    search_fields = ("id", "host__key", "worker_id", "error_code")
