from __future__ import annotations

import hashlib
import re

from django.conf import settings
from django.core.exceptions import ValidationError as DjangoValidationError
from django.db import transaction
from django.db.models import Max
from django.utils import timezone
from django.urls import reverse
from drf_spectacular.utils import extend_schema_field
from rest_framework import serializers

from studio.models import (
    ApprovalDecision,
    ArtifactSelectionDecision,
    ConfigDefinition,
    ConfigRevision,
    ConfigValue,
    GeneratedArtifact,
    GenerationAttempt,
    GenerationAttemptResolution,
    HostCapabilitySnapshot,
    ModelArtifact,
    ModelDeployment,
    ModelDeploymentCapabilitySnapshot,
    ModelDeploymentSmokeAttempt,
    ModelDeploymentSmokeAttemptResolution,
    ModelProfileVersion,
    PlatformLifecycleAuditEvent,
    PromptFieldDefinition,
    PromptProfileBinding,
    PromptProfileFieldRule,
    PromptProfileVersion,
    ProductionRecipeVersion,
    ProductionRun,
    ProviderProfile,
    RecommendationCandidate,
    RecommendationDecision,
    RecommendationSnapshot,
    ReleaseBundle,
    RuntimeHost,
    RuntimeHostProbeAttempt,
    RuntimeModelAvailability,
    ScriptRevision,
    ScriptSource,
    SecretAuditEvent,
    SecretBinding,
    SeriesProject,
    StageRun,
    StageRouteEvent,
    WorkflowModelRequirement,
    WorkflowSemanticBinding,
    WorkflowSmokeAttempt,
    WorkflowTemplate,
    WorkflowTemplateVersion,
)
from studio.services.configuration import (
    create_config_revision,
    publish_config_revision,
    redact_configuration_snapshot,
    redact_sensitive_bindings,
    validate_config_value,
)
from studio.services.async_media_contract import deployment_target_sha256
from studio.services.json_contracts import (
    assign_json_contract,
    find_forbidden_secret_fields,
    find_sensitive_json_keys,
    json_contract,
    verify_json_contract,
)
from studio.services.provider_registry import driver_for, validate_provider_profile
from studio.services.prompt_profiles import (
    refresh_prompt_profile_contract,
    validate_field_definition,
    validate_prompt_profile_binding,
    validate_profile_rule,
)
from studio.media_signing import signed_media_token
from studio.services.workflow_versions import import_workflow_version


def _raise_drf(exc: DjangoValidationError):
    detail = getattr(exc, "message_dict", None) or getattr(exc, "messages", None) or str(exc)
    raise serializers.ValidationError(detail) from exc


class ProviderProfileSerializer(serializers.ModelSerializer):
    capabilities = serializers.SerializerMethodField()
    credential_id = serializers.PrimaryKeyRelatedField(
        source="credential",
        queryset=SecretBinding.objects.filter(is_active=True, active_version__isnull=False),
        write_only=True,
        required=False,
        allow_null=True,
    )
    credential_configured = serializers.SerializerMethodField()

    class Meta:
        model = ProviderProfile
        fields = (
            "id", "key", "name", "driver_type", "modalities", "operations",
            "credential_id", "credential_configured", "driver_config", "driver_config_schema_version",
            "driver_config_size_bytes", "driver_config_sha256", "is_system",
            "is_active", "capabilities", "created_at", "updated_at",
        )
        read_only_fields = (
            "driver_config_schema_version", "driver_config_size_bytes",
            "driver_config_sha256", "capabilities", "credential_configured",
            "is_system",
            "created_at", "updated_at",
        )

    def get_credential_configured(self, obj) -> bool:
        return bool(obj.credential_id and obj.credential.is_active and obj.credential.active_version_id)

    @extend_schema_field(serializers.JSONField())
    def get_capabilities(self, obj) -> dict[str, object]:
        try:
            return driver_for(obj).capabilities()
        except Exception as exc:
            return {"schema_version": 1, "available": False, "reason": str(exc)}

    def validate(self, attrs):
        request = self.context.get("request")
        if "credential" in attrs and not getattr(
            getattr(request, "user", None), "is_superuser", False,
        ):
            raise serializers.ValidationError({
                "credential_id": "只有超级管理员可以绑定受管凭据",
            })
        instance = self.instance or ProviderProfile()
        for field, value in attrs.items():
            setattr(instance, field, value)
        config = attrs.get("driver_config", getattr(instance, "driver_config", {}))
        try:
            assign_json_contract(instance, "driver_config", config)
            validate_provider_profile(instance)
        except DjangoValidationError as exc:
            _raise_drf(exc)
        attrs["driver_config"] = config
        attrs["driver_config_schema_version"] = instance.driver_config_schema_version
        attrs["driver_config_size_bytes"] = instance.driver_config_size_bytes
        attrs["driver_config_sha256"] = instance.driver_config_sha256
        return attrs


class ModelProfileVersionSerializer(serializers.ModelSerializer):
    provider_key = serializers.CharField(source="provider.key", read_only=True)

    class Meta:
        model = ModelProfileVersion
        fields = (
            "id", "profile_key", "version", "provider", "provider_key",
            "model_identifier", "modality", "specification",
            "specification_schema_version", "specification_size_bytes",
            "specification_sha256", "status", "published_at", "published_by",
            "created_at", "updated_at",
        )
        read_only_fields = (
            "specification_schema_version", "specification_size_bytes",
            "specification_sha256", "status", "published_at", "published_by",
            "created_at", "updated_at",
        )

    def validate(self, attrs):
        if self.instance and self.instance.status in {
            ModelProfileVersion.Status.PUBLISHED,
            ModelProfileVersion.Status.RETIRED,
        }:
            raise serializers.ValidationError("published/retired model profile version 不可修改")
        specification = attrs.get(
            "specification",
            getattr(self.instance, "specification", {}),
        )
        sensitive = find_forbidden_secret_fields(specification)
        if sensitive:
            raise serializers.ValidationError({
                "specification": f"版本合同不能保存明文密钥字段: {', '.join(sensitive)}",
            })
        contract = json_contract(specification)
        attrs.update({
            "specification_schema_version": contract.schema_version,
            "specification_size_bytes": contract.size_bytes,
            "specification_sha256": contract.sha256,
        })
        return attrs


class PromptFieldDefinitionSerializer(serializers.ModelSerializer):
    class Meta:
        model = PromptFieldDefinition
        fields = (
            "id", "key", "label", "category", "description", "impact",
            "value_type", "default_value", "value_options", "is_required",
            "is_active", "display_order", "created_at", "updated_at",
        )
        read_only_fields = ("created_at", "updated_at")

    def validate(self, attrs):
        instance = self.instance or PromptFieldDefinition()
        for field, value in attrs.items():
            setattr(instance, field, value)
        try:
            validate_field_definition(instance)
        except DjangoValidationError as exc:
            _raise_drf(exc)
        return attrs

    def update(self, instance, validated_data):
        with transaction.atomic():
            instance = super().update(instance, validated_data)
            profile_ids = instance.profile_rules.filter(
                profile_version__status=PromptProfileVersion.Status.DRAFT,
            ).values_list("profile_version_id", flat=True).distinct()
            for profile in PromptProfileVersion.objects.filter(pk__in=profile_ids):
                refresh_prompt_profile_contract(profile)
        return instance


class PromptProfileVersionSerializer(serializers.ModelSerializer):
    field_rule_count = serializers.IntegerField(read_only=True, default=0)

    class Meta:
        model = PromptProfileVersion
        fields = (
            "id", "profile_key", "version", "name", "description",
            "model_family", "operation", "mode", "official_reference",
            "status", "review_state", "contract_schema_version",
            "contract_size_bytes", "contract_sha256", "field_rule_count",
            "published_at", "published_by", "created_at", "updated_at",
        )
        read_only_fields = (
            "status", "contract_schema_version", "contract_size_bytes",
            "contract_sha256", "field_rule_count", "published_at",
            "published_by", "created_at", "updated_at",
        )

    def validate(self, attrs):
        if self.instance and self.instance.status in {
            PromptProfileVersion.Status.PUBLISHED,
            PromptProfileVersion.Status.RETIRED,
        }:
            raise serializers.ValidationError(
                "published/retired PromptProfile 不可修改；请创建新版本",
            )
        return attrs

    def create(self, validated_data):
        with transaction.atomic():
            instance = super().create(validated_data)
            refresh_prompt_profile_contract(instance)
        return instance

    def update(self, instance, validated_data):
        with transaction.atomic():
            instance = super().update(instance, validated_data)
            refresh_prompt_profile_contract(instance)
        return instance


class PromptProfileFieldRuleSerializer(serializers.ModelSerializer):
    field_key = serializers.CharField(source="field_definition.key", read_only=True)
    field_label = serializers.CharField(source="field_definition.label", read_only=True)
    field_value_type = serializers.CharField(
        source="field_definition.value_type", read_only=True,
    )

    class Meta:
        model = PromptProfileFieldRule
        fields = (
            "id", "profile_version", "field_definition", "field_key",
            "field_label", "field_value_type", "source_selector", "channel",
            "position", "label_style", "separator", "required",
            "omit_if_empty", "max_chars", "created_at", "updated_at",
        )
        read_only_fields = (
            "field_key", "field_label", "field_value_type", "created_at", "updated_at",
        )

    def validate(self, attrs):
        if self.instance and "profile_version" in attrs and (
            attrs["profile_version"].pk != self.instance.profile_version_id
        ):
            raise serializers.ValidationError({
                "profile_version": "字段规则不能跨 PromptProfile 版本移动",
            })
        instance = self.instance or PromptProfileFieldRule()
        for field, value in attrs.items():
            setattr(instance, field, value)
        if not getattr(instance, "profile_version_id", None):
            raise serializers.ValidationError({"profile_version": "此字段为必填项"})
        if not getattr(instance, "field_definition_id", None):
            raise serializers.ValidationError({"field_definition": "此字段为必填项"})
        try:
            validate_profile_rule(instance)
        except DjangoValidationError as exc:
            _raise_drf(exc)
        return attrs

    def create(self, validated_data):
        with transaction.atomic():
            instance = super().create(validated_data)
            refresh_prompt_profile_contract(instance.profile_version)
        return instance

    def update(self, instance, validated_data):
        with transaction.atomic():
            instance = super().update(instance, validated_data)
            refresh_prompt_profile_contract(instance.profile_version)
        return instance


class PromptProfileBindingSerializer(serializers.ModelSerializer):
    prompt_profile_key = serializers.CharField(
        source="prompt_profile_version.profile_key", read_only=True,
    )
    prompt_profile_contract_sha256 = serializers.CharField(
        source="prompt_profile_version.contract_sha256", read_only=True,
    )
    model_identifier = serializers.CharField(
        source="model_profile_version.model_identifier", read_only=True,
        allow_null=True,
    )

    class Meta:
        model = PromptProfileBinding
        fields = (
            "id", "key", "name", "operation", "model_family", "mode",
            "model_profile_version", "model_identifier", "prompt_profile_version",
            "prompt_profile_key", "prompt_profile_contract_sha256", "priority",
            "is_active", "created_at", "updated_at",
        )
        read_only_fields = (
            "model_identifier", "prompt_profile_key",
            "prompt_profile_contract_sha256", "created_at", "updated_at",
        )

    def validate(self, attrs):
        instance = self.instance or PromptProfileBinding()
        for field, value in attrs.items():
            setattr(instance, field, value)
        if not getattr(instance, "prompt_profile_version_id", None):
            raise serializers.ValidationError({"prompt_profile_version": "此字段为必填项"})
        try:
            validate_prompt_profile_binding(instance)
        except DjangoValidationError as exc:
            _raise_drf(exc)
        if instance.is_active:
            duplicate = PromptProfileBinding.objects.filter(
                is_active=True,
                operation=instance.operation,
                model_family=instance.model_family,
                mode=instance.mode,
                model_profile_version_id=instance.model_profile_version_id,
                priority=instance.priority,
            )
            if self.instance:
                duplicate = duplicate.exclude(pk=self.instance.pk)
            if duplicate.exists():
                raise serializers.ValidationError(
                    "相同 selector 与 priority 已存在 active binding；运行时将无法唯一解析",
                )
        return attrs


class PromptProfilePreviewInputSerializer(serializers.Serializer):
    sources = serializers.JSONField(required=False, default=dict)

    def validate_sources(self, value):
        if not isinstance(value, dict):
            raise serializers.ValidationError("sources 必须是对象")
        return value


class PromptProfilePreviewSerializer(serializers.Serializer):
    schema_version = serializers.IntegerField(read_only=True)
    prompt_profile_version_id = serializers.UUIDField(read_only=True)
    profile_key = serializers.CharField(read_only=True)
    version = serializers.IntegerField(read_only=True)
    contract_sha256 = serializers.CharField(read_only=True)
    channels = serializers.DictField(read_only=True)
    structured_fields = serializers.DictField(read_only=True)
    trace = serializers.ListField(child=serializers.DictField(), read_only=True)


class PromptProfileOptionsSerializer(serializers.Serializer):
    operations = serializers.ListField(child=serializers.CharField(), read_only=True)
    model_families = serializers.ListField(child=serializers.CharField(), read_only=True)
    modes = serializers.ListField(child=serializers.CharField(), read_only=True)
    source_selectors = serializers.ListField(child=serializers.DictField(), read_only=True)
    channels = serializers.ListField(child=serializers.DictField(), read_only=True)
    label_styles = serializers.ListField(child=serializers.DictField(), read_only=True)
    separators = serializers.ListField(child=serializers.DictField(), read_only=True)
    model_profile_versions = serializers.ListField(
        child=serializers.DictField(), read_only=True,
    )


class ModelArtifactSerializer(serializers.ModelSerializer):
    class Meta:
        model = ModelArtifact
        fields = "__all__"
        read_only_fields = (
            "verified_snapshot", "verified_at", "verified_by",
            "created_at", "updated_at",
        )

    def validate_sha256(self, value):
        if value and re.fullmatch(r"[0-9a-f]{64}", value) is None:
            raise serializers.ValidationError("sha256 必须为 64 位小写十六进制")
        return value

    def validate(self, attrs):
        current_profile = getattr(self.instance, "profile_version", None)
        target_profile = attrs.get("profile_version", current_profile)
        immutable_statuses = {
            ModelProfileVersion.Status.PUBLISHED,
            ModelProfileVersion.Status.RETIRED,
        }
        if current_profile and current_profile.status in immutable_statuses:
            raise serializers.ValidationError(
                "绑定 published model profile version 的 artifact 不可修改或重新绑定；请创建新版本",
            )
        if target_profile and target_profile.status in immutable_statuses:
            raise serializers.ValidationError({
                "profile_version": "published model profile version 不再接受 artifact 变更；请创建新版本",
            })
        requested_status = attrs.get(
            "status",
            getattr(self.instance, "status", ModelArtifact.Status.DECLARED),
        )
        if requested_status != ModelArtifact.Status.DECLARED:
            raise serializers.ValidationError({
                "status": "模型文件状态只能由主机快照核验动作更新",
            })
        return attrs


class ModelDeploymentSerializer(serializers.ModelSerializer):
    credential_id = serializers.PrimaryKeyRelatedField(
        source="credential",
        queryset=SecretBinding.objects.filter(is_active=True, active_version__isnull=False),
        write_only=True,
        required=False,
        allow_null=True,
    )
    credential_configured = serializers.SerializerMethodField()
    provider_profile_id = serializers.UUIDField(
        source="profile_version.provider_id",
        read_only=True,
    )
    provider_driver = serializers.CharField(
        source="profile_version.provider.driver_type",
        read_only=True,
    )
    latest_smoke = serializers.SerializerMethodField()

    class Meta:
        model = ModelDeployment
        fields = (
            "id", "key", "name", "profile_version", "provider_profile_id",
            "provider_driver", "runtime_host", "credential_id", "credential_configured",
            "latest_smoke",
            "runtime_config",
            "runtime_config_schema_version", "runtime_config_size_bytes",
            "runtime_config_sha256", "priority", "is_default", "status",
            "created_at", "updated_at",
        )
        read_only_fields = (
            "provider_profile_id", "provider_driver", "credential_configured", "latest_smoke",
            "runtime_config_schema_version",
            "runtime_config_size_bytes", "runtime_config_sha256", "created_at", "updated_at",
        )

    def get_credential_configured(self, obj) -> bool:
        deployment_ready = bool(obj.credential_id and obj.credential.is_active and obj.credential.active_version_id)
        provider = obj.profile_version.provider
        provider_ready = bool(provider.credential_id and provider.credential.is_active and provider.credential.active_version_id)
        return deployment_ready or provider_ready

    def get_latest_smoke(self, obj) -> dict | None:
        unresolved = obj.smoke_attempts.filter(
            status__in=(
                ModelDeploymentSmokeAttempt.Status.SUBMITTING,
                ModelDeploymentSmokeAttempt.Status.UNCERTAIN,
            ),
            resolutions__isnull=True,
        ).order_by("-created_at").first()
        snapshot = obj.capability_snapshots.filter(
            probe_kind=ModelDeploymentCapabilitySnapshot.ProbeKind.SMOKE,
        ).order_by("-revision").first()
        unresolved_public = None
        if unresolved is not None:
            unresolved_public = {
                "id": str(unresolved.id),
                "status": unresolved.status,
                "operation": unresolved.operation,
                "request_sha256": unresolved.request_sha256,
                "target_sha256": unresolved.target_sha256,
                "provider_request_id_sha256": unresolved.provider_request_id_sha256,
                "submission_started_at": unresolved.submission_started_at,
            }
        if snapshot is None:
            if unresolved_public is not None:
                return {
                    "id": None,
                    "status": "unresolved",
                    "operations": [unresolved.operation],
                    "captured_at": None,
                    "expires_at": None,
                    "fresh": False,
                    "unresolved_submission": unresolved_public,
                }
            return None
        operations = []
        fresh = False
        try:
            operations_contract = verify_json_contract(
                snapshot.operations,
                schema_version=snapshot.operations_schema_version,
                size_bytes=snapshot.operations_size_bytes,
                sha256=snapshot.operations_sha256,
            )
            verify_json_contract(
                snapshot.evidence,
                schema_version=snapshot.evidence_schema_version,
                size_bytes=snapshot.evidence_size_bytes,
                sha256=snapshot.evidence_sha256,
            )
            operations = operations_contract.value
            fresh = bool(
                snapshot.status == snapshot.Status.PASSED
                and snapshot.expires_at > timezone.now()
                and snapshot.target_sha256 == deployment_target_sha256(obj)
            )
        except (DjangoValidationError, RuntimeError, TypeError, ValueError):
            # A damaged or stale snapshot remains visible as audit history, but
            # must never be advertised as executable readiness.
            fresh = False
        return {
            "id": str(snapshot.id),
            "status": snapshot.status,
            "operations": operations,
            "captured_at": snapshot.captured_at,
            "expires_at": snapshot.expires_at,
            "fresh": fresh,
            "unresolved_submission": unresolved_public,
        }

    def validate(self, attrs):
        request = self.context.get("request")
        if "credential" in attrs and not getattr(
            getattr(request, "user", None), "is_superuser", False,
        ):
            raise serializers.ValidationError({
                "credential_id": "只有超级管理员可以绑定受管凭据",
            })
        profile_version = attrs.get(
            "profile_version",
            getattr(self.instance, "profile_version", None),
        )
        if profile_version is None or profile_version.status != ModelProfileVersion.Status.PUBLISHED:
            raise serializers.ValidationError({"profile_version": "必须选择 published 版本"})
        target_status = attrs.get(
            "status",
            getattr(self.instance, "status", ModelDeployment.Status.ACTIVE),
        )
        runtime_host = attrs.get(
            "runtime_host",
            getattr(self.instance, "runtime_host", None),
        )
        if runtime_host is not None:
            runtime_host = RuntimeHost.objects.get(pk=runtime_host.pk)
        if (
            target_status == ModelDeployment.Status.ACTIVE
            and runtime_host is not None
            and runtime_host.status != RuntimeHost.Status.ACTIVE
        ):
            raise serializers.ValidationError({
                "runtime_host": "active deployment 只能绑定状态为 active 的运行主机",
            })
        runtime_config = attrs.get(
            "runtime_config",
            getattr(self.instance, "runtime_config", {}),
        )
        sensitive = find_sensitive_json_keys(runtime_config)
        if sensitive:
            raise serializers.ValidationError({
                "runtime_config": f"不能保存密钥字段: {', '.join(sensitive)}",
            })
        contract = json_contract(runtime_config)
        attrs.update({
            "runtime_config_schema_version": contract.schema_version,
            "runtime_config_size_bytes": contract.size_bytes,
            "runtime_config_sha256": contract.sha256,
        })
        temporary = self.instance or ModelDeployment()
        for field, value in attrs.items():
            setattr(temporary, field, value)
        try:
            driver = driver_for(profile_version.provider)
            driver.validate_config({
                **(profile_version.provider.driver_config or {}),
                **runtime_config,
            })
        except (DjangoValidationError, RuntimeError) as exc:
            raise serializers.ValidationError({"runtime_config": str(exc)}) from exc
        return attrs


class WorkflowSemanticBindingSerializer(serializers.ModelSerializer):
    class Meta:
        model = WorkflowSemanticBinding
        exclude = ("workflow_version", "created_at", "updated_at")


class WorkflowModelRequirementSerializer(serializers.ModelSerializer):
    class Meta:
        model = WorkflowModelRequirement
        exclude = ("workflow_version", "created_at", "updated_at")


class WorkflowTemplateVersionListSerializer(serializers.ModelSerializer):
    template_name = serializers.CharField(source="template.name", read_only=True)
    template_slug = serializers.CharField(source="template.slug", read_only=True)
    output_kind = serializers.CharField(source="template.output_kind", read_only=True)
    semantic_binding_count = serializers.IntegerField(read_only=True)
    model_requirement_count = serializers.IntegerField(read_only=True)

    class Meta:
        model = WorkflowTemplateVersion
        fields = (
            "id", "template", "template_name", "template_slug", "output_kind",
            "version", "provider", "model_profile_version", "status",
            "validation_state", "validation_message", "smoke_test_state",
            "smoke_test_message", "is_current", "graph_schema_version",
            "graph_size_bytes", "graph_sha256", "input_contract_schema_version",
            "input_contract_size_bytes", "input_contract_sha256",
            "compiler_id", "compiler_version", "semantic_bindings_sha256",
            "model_requirements_sha256", "execution_contract_sha256",
            "semantic_binding_count", "model_requirement_count", "validated_deployment",
            "validation_evidence_sha256", "smoke_test_evidence_sha256", "published_at",
            "published_by", "created_at", "updated_at",
        )


class WorkflowTemplateVersionDetailSerializer(WorkflowTemplateVersionListSerializer):
    semantic_bindings = WorkflowSemanticBindingSerializer(many=True, read_only=True)
    model_requirements = WorkflowModelRequirementSerializer(many=True, read_only=True)

    class Meta(WorkflowTemplateVersionListSerializer.Meta):
        fields = WorkflowTemplateVersionListSerializer.Meta.fields + (
            "graph", "input_contract", "semantic_bindings", "model_requirements",
            "validation_evidence", "smoke_test_evidence",
        )


class WorkflowTemplateCanvasExportQuerySerializer(serializers.Serializer):
    download = serializers.BooleanField(
        required=False,
        default=False,
        help_text="Return the converted ComfyUI UI workflow as a JSON attachment.",
    )
    deployment_id = serializers.UUIDField(
        required=False,
        help_text=(
            "Required only for a draft without validated_deployment. Validated and "
            "published versions are pinned to their persisted deployment evidence."
        ),
    )


class WorkflowTemplateCanvasExportSerializer(serializers.Serializer):
    """Read-only ComfyUI UI canvas derived from one saved template version."""

    workflow_version_id = serializers.UUIDField(read_only=True)
    format = serializers.ChoiceField(choices=("comfyui_ui",), read_only=True)
    filename = serializers.CharField(read_only=True)
    graph_sha256 = serializers.CharField(read_only=True)
    template_slug = serializers.CharField(read_only=True)
    template_version = serializers.IntegerField(read_only=True)
    deployment_id = serializers.UUIDField(read_only=True)
    deployment_name = serializers.CharField(read_only=True)
    deployment_source = serializers.ChoiceField(
        choices=("validated", "explicit_draft"),
        read_only=True,
    )
    workflow = serializers.JSONField(read_only=True)
    import_ready = serializers.BooleanField(read_only=True)
    open_reason = serializers.CharField(read_only=True)
    direct_open_supported = serializers.BooleanField(
        read_only=True,
        default=False,
        help_text=(
            "Always false: browsers cannot inject a workflow into another ComfyUI page."
        ),
    )
    direct_open_url = serializers.URLField(
        allow_null=True,
        read_only=True,
        default=None,
        help_text="Always null; download and import the JSON attachment instead.",
    )
    open_mode = serializers.ChoiceField(choices=("download_import",), read_only=True)
    download_url = serializers.URLField(read_only=True)


class WorkflowTemplateVersionImportSerializer(serializers.Serializer):
    template = serializers.PrimaryKeyRelatedField(queryset=WorkflowTemplate.objects.all())
    graph = serializers.JSONField()
    input_contract = serializers.JSONField(required=False, default=dict)
    semantic_bindings = serializers.ListField(child=serializers.DictField(), required=False, default=list)
    model_requirements = serializers.ListField(child=serializers.DictField(), required=False, default=list)
    provider = serializers.PrimaryKeyRelatedField(
        queryset=ProviderProfile.objects.filter(is_active=True),
        required=False,
        allow_null=True,
    )
    model_profile_version = serializers.PrimaryKeyRelatedField(
        queryset=ModelProfileVersion.objects.filter(status=ModelProfileVersion.Status.PUBLISHED),
        required=False,
        allow_null=True,
    )

    def create(self, validated_data):
        for field in ("graph", "input_contract"):
            sensitive = find_forbidden_secret_fields(validated_data.get(field, {}))
            if sensitive:
                raise serializers.ValidationError({
                    field: f"工作流合同不能保存明文密钥字段: {', '.join(sensitive)}",
                })
        try:
            return import_workflow_version(**validated_data)
        except DjangoValidationError as exc:
            _raise_drf(exc)


class DeploymentActionSerializer(serializers.Serializer):
    deployment_id = serializers.UUIDField()


class DeploymentProbeInputSerializer(serializers.Serializer):
    operation = serializers.ChoiceField(
        choices=(
            "image.text_to_image",
            "video.text_to_video",
            "prompt.normalize",
            "script.generate",
            "script.structure",
        ),
        required=False,
        allow_null=True,
    )


class ModelArtifactVerificationInputSerializer(serializers.Serializer):
    host_snapshot_id = serializers.UUIDField()


class RuntimeHostStatusSerializer(serializers.Serializer):
    is_active = serializers.BooleanField()


class ModelDeploymentCapabilitySnapshotSerializer(serializers.ModelSerializer):
    credential_bound = serializers.SerializerMethodField()

    class Meta:
        model = ModelDeploymentCapabilitySnapshot
        fields = (
            "id", "deployment", "revision", "probe_kind", "status", "target_sha256",
            "provider_config_sha256", "deployment_config_sha256",
            "credential_bound", "operations", "operations_schema_version",
            "operations_size_bytes", "operations_sha256", "evidence",
            "evidence_schema_version", "evidence_size_bytes", "evidence_sha256",
            "captured_at", "expires_at", "created_at",
        )
        read_only_fields = fields

    def get_credential_bound(self, obj) -> bool:
        return bool(obj.credential_binding_sha256)


class ModelDeploymentSmokeAttemptSerializer(serializers.ModelSerializer):
    class Meta:
        model = ModelDeploymentSmokeAttempt
        fields = (
            "id", "deployment", "operation", "status", "request_sha256",
            "target_sha256", "estimated_cost", "cost_currency",
            "provider_request_id_sha256", "worker_id", "claimed_at", "heartbeat_at",
            "submission_started_at", "submitted_at", "last_polled_at", "next_poll_at",
            "poll_count", "remote_status", "deadline_started_at", "hard_deadline_at",
            "idle_deadline_at", "last_meaningful_progress_at", "cancel_requested_at",
            "cancel_submitted_at", "error_code", "error_message", "finished_at",
            "final_snapshot", "created_at", "updated_at",
        )
        read_only_fields = fields


class WorkflowValidationResponseSerializer(serializers.Serializer):
    version = WorkflowTemplateVersionDetailSerializer()
    contract_report = serializers.JSONField()


class WorkflowSmokeTestResponseSerializer(serializers.Serializer):
    version = WorkflowTemplateVersionDetailSerializer()
    smoke_test = serializers.JSONField()


class WorkflowSmokeAttemptSerializer(serializers.ModelSerializer):
    class Meta:
        model = WorkflowSmokeAttempt
        fields = (
            "id", "workflow_version", "deployment", "created_by", "status",
            "graph_sha256", "execution_contract_sha256", "validation_evidence_sha256",
            "target_sha256", "worker_id", "claimed_at", "heartbeat_at",
            "submission_started_at", "submitted_at", "last_polled_at", "next_poll_at",
            "poll_count", "remote_status", "deadline_started_at", "hard_deadline_at",
            "idle_deadline_at", "last_meaningful_progress_at", "cancel_requested_at",
            "cancel_submitted_at", "finished_at", "error_code", "error_message",
            "result", "created_at", "updated_at",
        )
        read_only_fields = fields


class ConfigDefinitionSerializer(serializers.ModelSerializer):
    class Meta:
        model = ConfigDefinition
        fields = "__all__"
        read_only_fields = (
            "value_schema_version", "value_schema_size_bytes", "value_schema_sha256",
            "initial_value_schema_version", "initial_value_size_bytes",
            "initial_value_sha256", "created_at", "updated_at",
        )

    def validate(self, attrs):
        if self.instance and self.instance.values.exists():
            immutable = {
                "key", "value_type", "allowed_scopes", "is_required", "is_secret",
                "value_schema", "initial_value", "restart_required",
                "allowed_default_sources",
            }
            if immutable & set(attrs):
                raise serializers.ValidationError(
                    "已有 revision 使用该定义；只能修改描述或启停，结构变更请创建新 key。"
                )
        schema = attrs.get("value_schema", getattr(self.instance, "value_schema", {}))
        resource_ref = schema.get("resource_ref") if isinstance(schema, dict) else None
        if resource_ref is not None:
            if not isinstance(resource_ref, dict):
                raise serializers.ValidationError({
                    "value_schema": "resource_ref 必须是对象",
                })
            allowed_resource_ref_keys = {
                "resource", "capability", "allow_empty", "require_ready",
            }
            unknown_resource_ref_keys = set(resource_ref) - allowed_resource_ref_keys
            if unknown_resource_ref_keys:
                raise serializers.ValidationError({
                    "value_schema": (
                        "resource_ref 包含未注册字段: "
                        + ", ".join(sorted(unknown_resource_ref_keys))
                    ),
                })
            if (
                resource_ref.get("resource") != "model_deployment"
                or not isinstance(resource_ref.get("capability"), str)
                or not resource_ref.get("capability", "").strip()
                or type(resource_ref.get("allow_empty", False)) is not bool
                or type(resource_ref.get("require_ready", True)) is not bool
            ):
                raise serializers.ValidationError({
                    "value_schema": "resource_ref 必须声明受支持的资源、能力与布尔约束",
                })
        is_secret = attrs.get("is_secret", getattr(self.instance, "is_secret", False))
        initial = attrs.get("initial_value", getattr(self.instance, "initial_value", None))
        if is_secret and initial is not None:
            raise serializers.ValidationError({
                "initial_value": "秘密配置不得保存 initial_value；只能绑定受管凭据。",
            })
        impact_modules = attrs.get(
            "impact_modules",
            getattr(self.instance, "impact_modules", []),
        )
        if not isinstance(impact_modules, list) or any(
            not isinstance(item, str) or not item.strip() or len(item) > 80
            for item in impact_modules
        ):
            raise serializers.ValidationError({
                "impact_modules": "必须是非空短字符串组成的数组",
            })
        allowed_sources = {
            item.strip()
            for item in str(attrs.get(
                "allowed_default_sources",
                getattr(self.instance, "allowed_default_sources", ""),
            ) or "").split(",")
            if item.strip()
        }
        if not allowed_sources <= {"model", "workflow"}:
            raise serializers.ValidationError({
                "allowed_default_sources": "只允许 model,workflow",
            })
        if attrs.get("is_secret", getattr(self.instance, "is_secret", False)) and allowed_sources:
            raise serializers.ValidationError({
                "allowed_default_sources": "密钥定义不能由模型或工作流提供默认值",
            })
        if attrs.get(
            "restart_required", getattr(self.instance, "restart_required", False),
        ) and allowed_sources:
            raise serializers.ValidationError({
                "allowed_default_sources": "需重启定义不能由模型或工作流提供默认值",
            })
        schema_contract = json_contract(schema)
        attrs.update({
            "value_schema_version": schema_contract.schema_version,
            "value_schema_size_bytes": schema_contract.size_bytes,
            "value_schema_sha256": schema_contract.sha256,
        })
        initial = attrs.get("initial_value", getattr(self.instance, "initial_value", None))
        if initial is None:
            attrs.update({
                "initial_value_schema_version": 1,
                "initial_value_size_bytes": 0,
                "initial_value_sha256": "",
            })
        else:
            candidate = self.instance or ConfigDefinition()
            for field, value in attrs.items():
                setattr(candidate, field, value)
            try:
                validate_config_value(candidate, initial)
            except DjangoValidationError as exc:
                _raise_drf(exc)
            contract = json_contract(initial)
            attrs.update({
                "initial_value_schema_version": contract.schema_version,
                "initial_value_size_bytes": contract.size_bytes,
                "initial_value_sha256": contract.sha256,
            })
        return attrs

    def to_representation(self, instance):
        data = super().to_representation(instance)
        if instance.is_secret:
            data["initial_value"] = None
        return data


class ConfigResourceOptionSerializer(serializers.Serializer):
    value = serializers.UUIDField()
    label = serializers.CharField()
    resource = serializers.CharField()
    capability = serializers.CharField()
    state = serializers.CharField()
    reason_code = serializers.CharField()
    is_selectable = serializers.BooleanField()

class ConfigValueSerializer(serializers.ModelSerializer):
    key = serializers.CharField(source="definition.key", read_only=True)
    is_secret = serializers.BooleanField(source="definition.is_secret", read_only=True)
    credential_configured = serializers.SerializerMethodField()

    class Meta:
        model = ConfigValue
        fields = (
            "id", "key", "is_secret", "value", "credential_configured",
            "value_schema_version", "value_size_bytes", "value_sha256",
        )

    def to_representation(self, instance):
        data = super().to_representation(instance)
        if instance.definition.is_secret:
            data["value"] = None
        return data

    def get_credential_configured(self, obj) -> bool:
        return bool(obj.credential_version_id) if obj.definition.is_secret else False


class SecretBindingSerializer(serializers.ModelSerializer):
    configured = serializers.SerializerMethodField()
    current_version = serializers.SerializerMethodField()

    class Meta:
        model = SecretBinding
        fields = (
            "id", "key", "label", "purpose", "is_active", "configured",
            "current_version", "created_at", "updated_at",
        )
        read_only_fields = fields

    def get_configured(self, obj) -> bool:
        return bool(obj.is_active and obj.active_version_id)

    def get_current_version(self, obj) -> int | None:
        return obj.active_version.version if obj.active_version_id else None


class SecretBindingCreateSerializer(serializers.Serializer):
    key = serializers.RegexField(r"^[a-z][a-z0-9_.-]{1,119}$")
    label = serializers.CharField(max_length=160)
    purpose = serializers.CharField(max_length=120)
    secret_value = serializers.CharField(write_only=True, trim_whitespace=False)
    reason = serializers.CharField(max_length=500, required=False, allow_blank=True)


class SecretRotateSerializer(serializers.Serializer):
    secret_value = serializers.CharField(write_only=True, trim_whitespace=False)
    reason = serializers.CharField(max_length=500, required=False, allow_blank=True)


class SecretStatusSerializer(serializers.Serializer):
    is_active = serializers.BooleanField()
    reason = serializers.CharField(max_length=500, required=False, allow_blank=True)


class SecretAuditEventSerializer(serializers.ModelSerializer):
    actor_username = serializers.CharField(source="actor.username", read_only=True)
    binding_key = serializers.CharField(source="binding.key", read_only=True)

    class Meta:
        model = SecretAuditEvent
        fields = (
            "id", "binding", "binding_key", "version", "actor", "actor_username",
            "action", "reason", "created_at",
        )
        read_only_fields = fields


class PlatformLifecycleAuditEventSerializer(serializers.ModelSerializer):
    actor_username = serializers.CharField(source="actor.username", read_only=True)

    class Meta:
        model = PlatformLifecycleAuditEvent
        fields = (
            "id", "actor", "actor_username", "action", "object_type", "object_id",
            "reason_code", "before_sha256", "after_sha256", "before_summary",
            "after_summary", "created_at",
        )
        read_only_fields = fields


class ConfigRevisionSerializer(serializers.ModelSerializer):
    values = ConfigValueSerializer(many=True, read_only=True)
    activation_available = serializers.SerializerMethodField()

    class Meta:
        model = ConfigRevision
        fields = (
            "id", "scope_type", "project", "run", "revision", "status",
            "based_on_revision", "activation_state", "activated_at", "activation_error",
            "activation_available",
            "values_sha256", "change_summary", "created_by", "published_at",
            "values", "created_at", "updated_at",
        )
        read_only_fields = fields

    def get_activation_available(self, obj) -> bool:
        if obj.activation_state not in {
            ConfigRevision.ActivationState.PENDING,
            ConfigRevision.ActivationState.FAILED,
        }:
            return False
        current_boot_id = str(getattr(settings, "AUTOPROMPT_BOOT_ID", "") or "").strip()
        return bool(current_boot_id and current_boot_id != obj.activation_requested_boot_id)


class ConfigRevisionCreateSerializer(serializers.Serializer):
    scope_type = serializers.ChoiceField(choices=ConfigRevision.ScopeType.choices)
    project = serializers.PrimaryKeyRelatedField(
        queryset=SeriesProject.objects.all(),
        required=False,
        allow_null=True,
    )
    run = serializers.PrimaryKeyRelatedField(
        queryset=ProductionRun.objects.all(),
        required=False,
        allow_null=True,
    )
    values = serializers.DictField()
    unset_keys = serializers.ListField(
        child=serializers.CharField(max_length=160),
        required=False,
        default=list,
    )
    change_summary = serializers.CharField(required=False, allow_blank=True, max_length=500)
    publish = serializers.BooleanField(required=False, default=False)

    def validate_values(self, values):
        contains_secret_binding = any(
            isinstance(item, dict) and item.get("credential_id")
            for item in values.values()
        )
        request = self.context.get("request")
        if contains_secret_binding and not getattr(
            getattr(request, "user", None), "is_superuser", False,
        ):
            raise serializers.ValidationError("只有超级管理员可以发布敏感配置绑定")
        return values

    def validate(self, attrs):
        attrs = super().validate(attrs)
        request = self.context.get("request")
        if not getattr(getattr(request, "user", None), "is_superuser", False):
            secret_unsets = ConfigDefinition.objects.filter(
                key__in=attrs.get("unset_keys", ()),
                is_secret=True,
            ).exists()
            if secret_unsets:
                raise serializers.ValidationError({
                    "unset_keys": "只有超级管理员可以移除敏感配置绑定",
                })
        return attrs

    def create(self, validated_data):
        publish = validated_data.pop("publish")
        validated_data["created_by"] = self.context["request"].user
        try:
            revision = create_config_revision(**validated_data)
            if publish:
                revision = publish_config_revision(
                    revision,
                    published_by=self.context["request"].user,
                )
            return revision
        except DjangoValidationError as exc:
            _raise_drf(exc)


class PlatformErrorBodySerializer(serializers.Serializer):
    status_code = serializers.IntegerField()
    code = serializers.CharField()
    details = serializers.JSONField()


class ConfigurationErrorResponseSerializer(serializers.Serializer):
    error = PlatformErrorBodySerializer()


class ProductionRunConflictResponseSerializer(serializers.Serializer):
    error = PlatformErrorBodySerializer()


class ResolvedConfigurationResponseSerializer(serializers.Serializer):
    schema_version = serializers.IntegerField()
    values = serializers.DictField(child=serializers.JSONField())
    sources = serializers.DictField(child=serializers.DictField())
    revision_ids = serializers.ListField(child=serializers.UUIDField())
    sha256 = serializers.RegexField(r"^[0-9a-f]{64}$")


class ProjectEffectiveConfigurationResponseSerializer(serializers.Serializer):
    schema_version = serializers.IntegerField()
    values = serializers.DictField(child=serializers.JSONField())
    sources = serializers.DictField(child=serializers.DictField())
    sha256 = serializers.RegexField(r"^[0-9a-f]{64}$")


class ProjectConfigWriteSerializer(serializers.Serializer):
    values = serializers.DictField(
        child=serializers.JSONField(),
        required=False,
        default=dict,
    )
    unset_keys = serializers.ListField(
        child=serializers.CharField(max_length=160),
        required=False,
        default=list,
    )
    change_summary = serializers.CharField(
        required=False,
        allow_blank=True,
        max_length=500,
        default="",
    )


class ProjectConfigResponseSerializer(serializers.Serializer):
    revision = ConfigRevisionSerializer()
    effective = ProjectEffectiveConfigurationResponseSerializer()


class RuntimeCapabilityReadinessItemSerializer(serializers.Serializer):
    state = serializers.CharField(
        help_text="ready | smoke_required | blocked | unconfigured",
    )
    reason = serializers.CharField()
    deployment_count = serializers.IntegerField(min_value=0)
    viable_deployment_count = serializers.IntegerField(min_value=0)
    cloud_deployment_count = serializers.IntegerField(min_value=0)


class RuntimeCapabilityReadinessSerializer(serializers.Serializer):
    text = RuntimeCapabilityReadinessItemSerializer()
    image = RuntimeCapabilityReadinessItemSerializer()
    video = RuntimeCapabilityReadinessItemSerializer()


class RuntimeReadinessResponseSerializer(serializers.Serializer):
    schema_version = serializers.IntegerField()
    snapshot_id = serializers.UUIDField()
    captured_at = serializers.DateTimeField()
    capabilities = RuntimeCapabilityReadinessSerializer()


class ProductionReadinessCapabilitySerializer(serializers.Serializer):
    state = serializers.ChoiceField(choices=("not_required", "blocked", "ready"))
    required = serializers.BooleanField()
    reason_codes = serializers.ListField(child=serializers.CharField())


class ProductionReadinessRecipeSerializer(serializers.Serializer):
    id = serializers.UUIDField()
    recipe_key = serializers.CharField()
    version = serializers.IntegerField(min_value=1)
    state = serializers.ChoiceField(choices=("blocked", "ready"))
    reason_codes = serializers.ListField(child=serializers.CharField())
    capabilities = serializers.DictField(child=ProductionReadinessCapabilitySerializer())


class ProductionReadinessResponseSerializer(serializers.Serializer):
    schema_version = serializers.IntegerField()
    status = serializers.ChoiceField(choices=("unconfigured", "blocked", "ready"))
    reason_codes = serializers.ListField(child=serializers.CharField())
    recipes = ProductionReadinessRecipeSerializer(many=True)


class RuntimeHostSerializer(serializers.ModelSerializer):
    class Meta:
        model = RuntimeHost
        fields = "__all__"
        read_only_fields = tuple(field.name for field in RuntimeHost._meta.fields)


class RuntimeModelAvailabilitySerializer(serializers.ModelSerializer):
    class Meta:
        model = RuntimeModelAvailability
        fields = (
            "id", "deployment", "artifact", "availability_key", "status", "reason",
            "observed_size_bytes", "observed_sha256", "observation_source",
        )
        read_only_fields = fields


class HostCapabilitySnapshotSerializer(serializers.ModelSerializer):
    model_availability = RuntimeModelAvailabilitySerializer(many=True, read_only=True)

    class Meta:
        model = HostCapabilitySnapshot
        fields = "__all__"
        read_only_fields = tuple(field.name for field in HostCapabilitySnapshot._meta.fields)


class RuntimeHostProbeAttemptSerializer(serializers.ModelSerializer):
    final_snapshot = serializers.SerializerMethodField()

    class Meta:
        model = RuntimeHostProbeAttempt
        fields = (
            "id", "host", "created_by", "trigger", "status", "worker_id",
            "claimed_at", "heartbeat_at", "started_at", "progress_marker",
            "cancel_requested_at", "finished_at", "error_code", "error_message",
            "final_snapshot", "created_at", "updated_at",
        )
        read_only_fields = fields

    def get_final_snapshot(self, obj) -> str | None:
        try:
            snapshot = obj.final_snapshot
        except HostCapabilitySnapshot.DoesNotExist:
            snapshot = None
        return str(snapshot.id) if snapshot is not None else None


class RecommendationCandidateSerializer(serializers.ModelSerializer):
    deployment_key = serializers.CharField(source="deployment.key", read_only=True)

    class Meta:
        model = RecommendationCandidate
        fields = (
            "id", "capability_key", "deployment", "deployment_key",
            "workflow_version", "status", "rank", "reason_code",
            "suggested_values", "suggested_values_sha256", "created_at",
        )
        read_only_fields = fields


class RecommendationDecisionSerializer(serializers.ModelSerializer):
    class Meta:
        model = RecommendationDecision
        fields = "__all__"
        read_only_fields = tuple(field.name for field in RecommendationDecision._meta.fields)

    def to_representation(self, instance):
        data = super().to_representation(instance)
        data["override_values"] = redact_sensitive_bindings(instance.override_values)
        return data


class RecommendationSnapshotSerializer(serializers.ModelSerializer):
    candidates = RecommendationCandidateSerializer(many=True, read_only=True)
    decisions = RecommendationDecisionSerializer(many=True, read_only=True)

    class Meta:
        model = RecommendationSnapshot
        fields = "__all__"
        read_only_fields = tuple(field.name for field in RecommendationSnapshot._meta.fields)


class RecommendationDecisionInputSerializer(serializers.Serializer):
    candidate_id = serializers.UUIDField(required=False, allow_null=True)
    decision = serializers.ChoiceField(choices=RecommendationDecision.Decision.choices)
    scope_type = serializers.ChoiceField(
        choices=(ConfigRevision.ScopeType.SYSTEM, ConfigRevision.ScopeType.PROJECT),
    )
    project_id = serializers.UUIDField(required=False, allow_null=True)
    override_values = serializers.DictField(required=False, default=dict)
    reason = serializers.CharField(required=False, allow_blank=True, max_length=500)


class ScriptSourceSerializer(serializers.ModelSerializer):
    latest_revision_id = serializers.SerializerMethodField()

    class Meta:
        model = ScriptSource
        fields = (
            "id", "project", "source_type", "source_format", "status",
            "original_name", "mime_type", "file_size_bytes", "file_sha256",
            "validation_message", "latest_revision_id", "created_at", "updated_at",
        )
        read_only_fields = fields

    def get_latest_revision_id(self, obj) -> str | None:
        revision = obj.revisions.order_by("-revision").first()
        return str(revision.id) if revision else None


class ScriptSourceCreateInputSerializer(serializers.Serializer):
    project_id = serializers.UUIDField()
    source_format = serializers.ChoiceField(
        choices=(
            ScriptSource.SourceFormat.PLAIN,
            ScriptSource.SourceFormat.MARKDOWN,
            ScriptSource.SourceFormat.FOUNTAIN,
        ),
        required=False,
        default=ScriptSource.SourceFormat.PLAIN,
    )
    content = serializers.CharField(required=False, allow_blank=True)
    file = serializers.FileField(required=False)


class ScriptRevisionListSerializer(serializers.ModelSerializer):
    source_status = serializers.CharField(source="source.status", read_only=True)

    class Meta:
        model = ScriptRevision
        fields = (
            "id", "source", "source_status", "revision", "content_size_bytes",
            "content_sha256", "status", "created_by", "confirmed_by", "confirmed_at",
            "created_at", "updated_at",
        )
        read_only_fields = fields


class ScriptRevisionDetailSerializer(ScriptRevisionListSerializer):
    class Meta(ScriptRevisionListSerializer.Meta):
        fields = ScriptRevisionListSerializer.Meta.fields + ("content",)


class ScriptSourceCreateResponseSerializer(serializers.Serializer):
    source = ScriptSourceSerializer()
    revision = ScriptRevisionDetailSerializer()


class ScriptRevisionCreateInputSerializer(serializers.Serializer):
    source_id = serializers.UUIDField()
    content = serializers.CharField()


class RuntimeRecommendationInputSerializer(serializers.Serializer):
    snapshot_id = serializers.UUIDField(required=False)
    project_id = serializers.UUIDField(required=False)


class NodeMetadataItemSerializer(serializers.Serializer):
    class_type = serializers.CharField()
    display_name = serializers.CharField()
    category = serializers.CharField()
    required_inputs = serializers.ListField(child=serializers.CharField())
    optional_inputs = serializers.ListField(child=serializers.CharField())
    output_count = serializers.IntegerField()


class NodeMetadataResponseSerializer(serializers.Serializer):
    schema_version = serializers.IntegerField()
    total_count = serializers.IntegerField(min_value=0)
    offset = serializers.IntegerField(min_value=0)
    limit = serializers.IntegerField(min_value=1, max_value=200)
    next_offset = serializers.IntegerField(min_value=0, allow_null=True)
    catalog_sha256 = serializers.RegexField(r"^[0-9a-f]{64}$")
    items = NodeMetadataItemSerializer(many=True)


class NodeMetadataQuerySerializer(serializers.Serializer):
    deployment_id = serializers.UUIDField()
    search = serializers.CharField(required=False, allow_blank=True, max_length=200, default="")
    offset = serializers.IntegerField(required=False, min_value=0, default=0)
    limit = serializers.IntegerField(required=False, min_value=1, max_value=200, default=50)


class ProductionRecipeVersionSerializer(serializers.ModelSerializer):
    class Meta:
        model = ProductionRecipeVersion
        fields = "__all__"
        read_only_fields = (
            "definition_schema_version", "definition_size_bytes", "definition_sha256",
            "status", "published_at", "published_by", "created_at", "updated_at",
        )

    def validate(self, attrs):
        if self.instance and self.instance.status in {
            ProductionRecipeVersion.Status.PUBLISHED,
            ProductionRecipeVersion.Status.RETIRED,
        }:
            raise serializers.ValidationError("published/retired recipe version 不可修改")
        definition = attrs.get("definition", getattr(self.instance, "definition", {}))
        sensitive = find_forbidden_secret_fields(definition)
        if sensitive:
            raise serializers.ValidationError({
                "definition": f"配方合同不能保存明文密钥字段: {', '.join(sensitive)}",
            })
        contract = json_contract(definition)
        attrs.update({
            "definition_schema_version": contract.schema_version,
            "definition_size_bytes": contract.size_bytes,
            "definition_sha256": contract.sha256,
        })
        return attrs


class ProductionRecipeMediaRouteOptionSerializer(serializers.Serializer):
    route_index = serializers.IntegerField(min_value=0, max_value=15)
    workflow_version_id = serializers.UUIDField(allow_null=True)
    workflow_label = serializers.CharField()
    model_deployment_id = serializers.UUIDField(allow_null=True)
    deployment_label = serializers.CharField()
    model_identifier = serializers.CharField(allow_blank=True)
    driver_type = serializers.CharField(allow_blank=True)
    output_kinds = serializers.ListField(child=serializers.CharField())
    h3_modes = serializers.ListField(child=serializers.CharField())
    media_operations = serializers.ListField(child=serializers.CharField())
    is_selectable = serializers.BooleanField()
    reason_codes = serializers.ListField(child=serializers.CharField())
    reasons = serializers.ListField(child=serializers.CharField())


class ProductionRecipeMediaRouteOptionsSerializer(serializers.Serializer):
    schema_version = serializers.IntegerField(min_value=1, max_value=1)
    recipe_version_id = serializers.UUIDField()
    recipe_definition_sha256 = serializers.RegexField(r"^[0-9a-f]{64}$")
    routes = ProductionRecipeMediaRouteOptionSerializer(many=True)


class ArtifactSelectionDecisionSerializer(serializers.ModelSerializer):
    class Meta:
        model = ArtifactSelectionDecision
        fields = ("id", "variant_group", "revision", "artifact", "selected_by", "created_at")
        read_only_fields = fields


class StageRouteEventSerializer(serializers.ModelSerializer):
    class Meta:
        model = StageRouteEvent
        fields = ("id", "sequence", "unit_key", "route_index", "event_type", "code", "created_at")
        read_only_fields = fields


_PUBLIC_ROUTE_FIELDS = (
    "index",
    "workflow_output_kind",
    "workflow_h3_modes",
    "model_identifier",
    "driver_type",
    "is_external",
    "effective_timeout_seconds",
    "media_operations",
)

_PUBLIC_CONFIG_SOURCE_FIELDS = (
    "source",
    "scope",
    "source_type",
    "version",
    "revision",
)


def public_configuration_snapshot(value: object) -> dict[str, object]:
    """Project an effective snapshot without revision ids or full-revision digests."""

    if not isinstance(value, dict):
        return {}
    redacted = redact_sensitive_bindings(redact_configuration_snapshot(value))
    values = redacted.get("values") if isinstance(redacted, dict) else None
    sources = redacted.get("sources") if isinstance(redacted, dict) else None
    public_sources = {}
    if isinstance(sources, dict):
        for key, source in sources.items():
            if not isinstance(source, dict):
                continue
            public_sources[str(key)] = {
                field: redact_sensitive_bindings(source[field])
                for field in _PUBLIC_CONFIG_SOURCE_FIELDS
                if field in source
            }
    return {
        "schema_version": int(redacted.get("schema_version") or 1),
        "values": values if isinstance(values, dict) else {},
        "sources": public_sources,
    }


def public_stage_routing(value: object) -> dict[str, object]:
    """Return the route chooser contract without execution config or credential fingerprints."""

    if not isinstance(value, dict):
        return {}
    routes = value.get("routes")
    public_routes = []
    if isinstance(routes, list):
        for route in routes:
            if not isinstance(route, dict):
                continue
            public_routes.append({
                key: redact_sensitive_bindings(route[key])
                for key in _PUBLIC_ROUTE_FIELDS
                if key in route
            })
    return {
        key: redact_sensitive_bindings(value[key])
        for key in ("schema_version", "mode", "variant_count", "evaluator")
        if key in value
    } | {"routes": public_routes}


class StageRunSerializer(serializers.ModelSerializer):
    approval_decisions = serializers.PrimaryKeyRelatedField(many=True, read_only=True)
    artifact_selection_decisions = ArtifactSelectionDecisionSerializer(many=True, read_only=True)
    route_events = StageRouteEventSerializer(many=True, read_only=True)

    class Meta:
        model = StageRun
        fields = (
            "id", "stage_key", "ordinal", "mode", "status", "approval_required",
            "attempt_count", "input_reference", "output_reference", "reason_code",
            "reason", "routing", "routing_sha256", "routing_decision",
            "routing_decision_sha256", "selected_route_index",
            "config_snapshot", "config_snapshot_sha256",
            "input_snapshot", "input_snapshot_sha256",
            "output_snapshot", "output_snapshot_sha256",
            "approved_estimated_cost", "approved_cost_currency",
            "selection_confirmed_at", "started_at", "finished_at", "approval_decisions",
            "artifact_selection_decisions", "route_events",
            "created_at", "updated_at",
        )
        read_only_fields = fields

    def to_representation(self, instance):
        data = super().to_representation(instance)
        snapshot = instance.config_snapshot if isinstance(instance.config_snapshot, dict) else {}
        routes = snapshot.get("routes") if isinstance(snapshot.get("routes"), dict) else {}
        safe = {
            "schema_version": int(snapshot.get("schema_version") or 1),
            "base": public_configuration_snapshot(snapshot.get("base")),
            "routes": {
                str(key): public_configuration_snapshot(value)
                for key, value in routes.items()
                if isinstance(value, dict)
            },
        }
        routing = public_stage_routing(instance.routing)
        routing_decision = redact_sensitive_bindings(instance.routing_decision)
        input_snapshot = redact_sensitive_bindings(instance.input_snapshot)
        output_snapshot = redact_sensitive_bindings(instance.output_snapshot)
        data["routing"] = routing
        data["routing_sha256"] = json_contract(routing).sha256
        data["routing_decision"] = routing_decision
        data["routing_decision_sha256"] = json_contract(routing_decision).sha256
        data["config_snapshot"] = safe
        data["config_snapshot_sha256"] = json_contract(safe).sha256
        data["input_snapshot"] = input_snapshot
        data["input_snapshot_sha256"] = json_contract(input_snapshot).sha256
        data["output_snapshot"] = output_snapshot
        data["output_snapshot_sha256"] = json_contract(output_snapshot).sha256
        return data


class GenerationAttemptSerializer(serializers.ModelSerializer):
    provider_request_id_sha256 = serializers.SerializerMethodField()
    provider_request_id_suffix = serializers.SerializerMethodField()

    class Meta:
        model = GenerationAttempt
        fields = (
            "id", "stage_run", "unit_key", "attempt_number", "model_deployment",
            "generation_task", "prompt_profile_version", "prompt_profile_binding_key",
            "prompt_profile_contract_sha256", "status", "route_index", "operation", "is_external",
            "provider_request_id_sha256", "provider_request_id_suffix",
            "request_sha256", "request_snapshot_schema_version",
            "request_snapshot_size_bytes", "request_snapshot_sha256", "result_sha256",
            "estimated_cost", "committed_cost", "actual_cost", "cost_currency",
            "error_code", "worker_id", "claimed_at", "heartbeat_at",
            "submission_started_at", "submitted_at", "remote_status", "last_polled_at",
            "next_poll_at", "poll_count", "cancel_requested_at", "cancel_submitted_at",
            "deadline_started_at", "hard_deadline_at", "idle_deadline_at",
            "last_meaningful_progress_at",
            "ignore_late_result", "started_at", "finished_at", "created_at", "updated_at",
        )
        read_only_fields = fields

    def get_provider_request_id_sha256(self, obj) -> str:
        value = str(obj.provider_request_id or "")
        return hashlib.sha256(value.encode("utf-8")).hexdigest() if value else ""

    def get_provider_request_id_suffix(self, obj) -> str:
        request = self.context.get("request")
        if not getattr(getattr(request, "user", None), "is_staff", False):
            return ""
        value = str(obj.provider_request_id or "")
        return value[-6:] if value else ""


class ResolvePromptNormalizationSmokeInputSerializer(serializers.Serializer):
    smoke_attempt_id = serializers.UUIDField()
    outcome = serializers.ChoiceField(
        choices=ModelDeploymentSmokeAttemptResolution.Outcome.choices,
    )
    note = serializers.CharField(min_length=8, max_length=4000, trim_whitespace=True)
    provider_request_id = serializers.CharField(
        required=False,
        allow_blank=True,
        max_length=191,
        default="",
        write_only=True,
    )
    actual_cost = serializers.DecimalField(
        required=False,
        allow_null=True,
        max_digits=14,
        decimal_places=6,
        min_value=0,
    )
    cost_currency = serializers.CharField(
        required=False,
        allow_blank=True,
        max_length=12,
        default="",
    )


class PromptNormalizationSmokeResolutionSerializer(serializers.ModelSerializer):
    smoke_attempt_id = serializers.UUIDField(source="attempt_id", read_only=True)
    smoke_attempt_status = serializers.CharField(source="attempt.status", read_only=True)
    request_sha256 = serializers.CharField(source="attempt.request_sha256", read_only=True)
    target_sha256 = serializers.CharField(source="attempt.target_sha256", read_only=True)

    class Meta:
        model = ModelDeploymentSmokeAttemptResolution
        fields = (
            "id", "smoke_attempt_id", "smoke_attempt_status", "revision", "outcome",
            "request_sha256", "target_sha256", "provider_request_id_sha256",
            "evidence", "evidence_schema_version", "evidence_size_bytes",
            "evidence_sha256", "note", "decided_by", "created_at",
        )
        read_only_fields = fields


class GeneratedArtifactListSerializer(serializers.ModelSerializer):
    preview_url = serializers.SerializerMethodField()
    download_url = serializers.SerializerMethodField()

    class Meta:
        model = GeneratedArtifact
        fields = (
            "id", "manifest_key", "production_run", "stage_run", "generation_attempt",
            "generation_task", "asset", "kind", "role", "variant_group",
            "variant_index", "status", "storage_backend", "object_key", "mime_type",
            "size_bytes", "sha256", "manifest_schema_version",
            "manifest_metadata_size_bytes", "manifest_metadata_sha256",
            "comparison_parameters",
            "preview_url", "download_url",
            "created_at", "updated_at",
        )
        read_only_fields = fields

    def _media_url(self, obj) -> str:
        if (
            obj.production_run_id is None
            or obj.status not in {GeneratedArtifact.Status.AVAILABLE, GeneratedArtifact.Status.SELECTED}
            or obj.storage_backend not in {"managed_local", "local_media"}
            or obj.kind not in {
                GeneratedArtifact.Kind.IMAGE,
                GeneratedArtifact.Kind.VIDEO,
                GeneratedArtifact.Kind.ASSEMBLY,
            }
        ):
            return ""
        owner_id = getattr(getattr(obj, "production_run", None), "owner_id", None)
        if owner_id is None:
            owner_id = ProductionRun.objects.filter(pk=obj.production_run_id).values_list("owner_id", flat=True).first()
        if owner_id is None:
            return ""
        token = signed_media_token(
            purpose="generated_artifact",
            object_id=obj.id,
            owner_id=owner_id,
        )
        return reverse("signed-media", kwargs={"token": token})

    def get_preview_url(self, obj) -> str:
        return self._media_url(obj)

    def get_download_url(self, obj) -> str:
        url = self._media_url(obj)
        return f"{url}?download=1" if url else ""


class GeneratedArtifactDetailSerializer(GeneratedArtifactListSerializer):
    class Meta(GeneratedArtifactListSerializer.Meta):
        fields = GeneratedArtifactListSerializer.Meta.fields + ("manifest_metadata",)


class ProductionRunListSerializer(serializers.ModelSerializer):
    project_title = serializers.CharField(source="project.title", read_only=True)
    release_bundle_id = serializers.SerializerMethodField()

    class Meta:
        model = ProductionRun
        fields = (
            "id", "project", "project_title", "script_revision", "recipe_version",
            "idempotency_key", "request_sha256", "status", "current_stage_key",
            "blocked_code", "blocked_reason", "allow_external_calls", "budget_limit",
            "cost_currency", "estimated_cost_total", "committed_cost_total",
            "actual_cost_total", "release_bundle_id",
            "max_generation_tasks", "generation_task_count", "config_snapshot_sha256",
            "input_snapshot_sha256", "started_at", "finished_at", "created_at", "updated_at",
        )
        read_only_fields = fields

    def get_release_bundle_id(self, obj) -> str | None:
        try:
            return str(obj.release_bundle.id)
        except (AttributeError, ReleaseBundle.DoesNotExist):
            return None

    def to_representation(self, instance):
        data = super().to_representation(instance)
        safe_config = public_configuration_snapshot(instance.config_snapshot)
        safe_input = redact_sensitive_bindings(instance.input_snapshot)
        data["config_snapshot_sha256"] = json_contract(safe_config).sha256
        data["input_snapshot_sha256"] = json_contract(safe_input).sha256
        return data


class ProductionRunDetailSerializer(ProductionRunListSerializer):
    stages = StageRunSerializer(many=True, read_only=True)
    artifacts = GeneratedArtifactListSerializer(many=True, read_only=True)

    class Meta(ProductionRunListSerializer.Meta):
        fields = ProductionRunListSerializer.Meta.fields + (
            "config_revision", "config_snapshot", "input_snapshot", "stages", "artifacts",
        )

    def to_representation(self, instance):
        data = super().to_representation(instance)
        data["config_snapshot"] = public_configuration_snapshot(instance.config_snapshot)
        data["input_snapshot"] = redact_sensitive_bindings(instance.input_snapshot)
        return data


class ProductionWorkflowShotRouteSerializer(serializers.Serializer):
    shot_id = serializers.UUIDField()
    route_index = serializers.IntegerField(min_value=0, max_value=15)


class ProductionWorkflowSelectionSerializer(serializers.Serializer):
    schema_version = serializers.IntegerField(default=1, min_value=1, max_value=1)
    mode = serializers.ChoiceField(choices=("uniform", "per_shot"))
    route_index = serializers.IntegerField(
        required=False,
        allow_null=True,
        default=None,
        min_value=0,
        max_value=15,
    )
    shot_routes = ProductionWorkflowShotRouteSerializer(
        many=True,
        required=False,
        default=list,
        allow_empty=True,
    )

    def validate(self, attrs):
        mode = attrs["mode"]
        route_index = attrs.get("route_index")
        shot_routes = attrs.get("shot_routes") or []
        if mode == "uniform":
            if route_index is None:
                raise serializers.ValidationError({
                    "route_index": "整集统一工作流必须选择一个配方 route",
                })
            if shot_routes:
                raise serializers.ValidationError({
                    "shot_routes": "整集统一工作流不能同时提交逐镜头 route",
                })
            return attrs
        if route_index is not None:
            raise serializers.ValidationError({
                "route_index": "逐镜头工作流不能同时提交整集 route",
            })
        if not shot_routes:
            raise serializers.ValidationError({
                "shot_routes": "逐镜头工作流必须提交完整镜头映射",
            })
        shot_ids = [str(item["shot_id"]) for item in shot_routes]
        if len(shot_ids) != len(set(shot_ids)):
            raise serializers.ValidationError({
                "shot_routes": "同一镜头不能重复选择 route",
            })
        return attrs


class ProductionRunCreateSerializer(serializers.Serializer):
    project_id = serializers.UUIDField()
    script_revision_id = serializers.UUIDField(required=False, allow_null=True)
    recipe_version_id = serializers.UUIDField()
    idempotency_key = serializers.CharField(max_length=120)
    allow_external_calls = serializers.BooleanField(required=False, allow_null=True, default=None)
    budget_limit = serializers.DecimalField(
        max_digits=12,
        decimal_places=4,
        required=False,
        allow_null=True,
        default=None,
        min_value=0,
    )
    max_generation_tasks = serializers.IntegerField(
        required=False,
        allow_null=True,
        default=None,
        min_value=1,
    )
    config_overrides = serializers.DictField(required=False, default=dict)
    idea = serializers.CharField(required=False, allow_blank=True, max_length=6000, default="")
    style_hint = serializers.CharField(required=False, allow_blank=True, max_length=2000, default="")
    episode_count = serializers.IntegerField(required=False, default=1, min_value=1, max_value=100)
    shots_per_episode = serializers.IntegerField(required=False, allow_null=True, default=None, min_value=1, max_value=100)
    episode_duration_seconds = serializers.IntegerField(
        required=False, allow_null=True, default=None, min_value=1, max_value=3600,
    )
    episode_ids = serializers.ListField(
        child=serializers.UUIDField(),
        required=False,
        allow_empty=True,
        default=list,
    )
    workflow_selection = ProductionWorkflowSelectionSerializer(
        required=False,
        allow_null=True,
        default=None,
    )

    def validate_episode_ids(self, value):
        return sorted(set(value), key=str)


class ApprovalDecisionInputSerializer(serializers.Serializer):
    stage_key = serializers.CharField(max_length=100)
    decision = serializers.ChoiceField(choices=ApprovalDecision.Decision.choices)
    comment = serializers.CharField(
        required=False,
        allow_blank=True,
        max_length=4000,
        default="",
    )
    candidate_overrides = serializers.DictField(
        child=serializers.CharField(
            allow_blank=False,
            trim_whitespace=False,
            max_length=8000,
        ),
        required=False,
        default=dict,
    )
    expected_source_snapshot_sha256 = serializers.RegexField(
        regex=r"^[0-9a-f]{64}$",
        required=False,
        allow_blank=True,
        default="",
    )
    adopt_generated_metadata = serializers.BooleanField(
        required=False,
        default=False,
        help_text=(
            "仅在批准 script_review 时可设为 true；服务端只采用冻结候选的 title/logline，"
            "并保留运行创建后发生的人工并发编辑。"
        ),
    )

    def validate_candidate_overrides(self, value):
        if len(value) > 100:
            raise serializers.ValidationError("candidate_overrides 最多包含 100 项")
        if any(not isinstance(key, str) or not key or len(key) > 191 for key in value):
            raise serializers.ValidationError("candidate_overrides 的 request_id 无效")
        return value

    def validate(self, attrs):
        is_snapshot_bound_approval = (
            attrs.get("stage_key") in {"storyboard_review", "script_normalize_review"}
            and attrs.get("decision") == ApprovalDecision.Decision.APPROVE
        )
        expected = str(attrs.get("expected_source_snapshot_sha256") or "")
        if is_snapshot_bound_approval and not expected:
            raise serializers.ValidationError({
                "expected_source_snapshot_sha256": "批准前必须提交页面展示的来源快照 hash",
            })
        if expected and not is_snapshot_bound_approval:
            raise serializers.ValidationError({
                "expected_source_snapshot_sha256": "该字段只允许用于批准 storyboard_review 或 script_normalize_review",
            })
        if attrs.get("adopt_generated_metadata") and not (
            attrs.get("stage_key") == "script_review"
            and attrs.get("decision") == ApprovalDecision.Decision.APPROVE
        ):
            raise serializers.ValidationError({
                "adopt_generated_metadata": "只允许在批准 script_review 时启用",
            })
        return attrs


class SelectArtifactInputSerializer(serializers.Serializer):
    artifact_id = serializers.UUIDField()


class SelectRouteInputSerializer(serializers.Serializer):
    stage_key = serializers.CharField(max_length=100)
    route_index = serializers.IntegerField(min_value=0, max_value=15)


class AuthorizeStageCostInputSerializer(serializers.Serializer):
    stage_key = serializers.CharField(max_length=100)
    allow_external_calls = serializers.BooleanField(required=False, allow_null=True, default=None)
    budget_limit = serializers.DecimalField(
        max_digits=12,
        decimal_places=4,
        required=False,
        allow_null=True,
        default=None,
        min_value=0,
    )
    approved_estimated_cost = serializers.DecimalField(
        max_digits=14,
        decimal_places=6,
        required=False,
        allow_null=True,
        default=None,
        min_value=0,
    )
    currency = serializers.RegexField(
        r"^[A-Z][A-Z0-9]{2,11}$",
        required=False,
        allow_blank=True,
        default="",
    )
    comment = serializers.CharField(
        required=False,
        allow_blank=True,
        max_length=4000,
        default="",
    )


class ResolveUncertainAttemptInputSerializer(serializers.Serializer):
    attempt_id = serializers.UUIDField()
    outcome = serializers.ChoiceField(choices=(
        GenerationAttemptResolution.Outcome.CONFIRMED_NOT_SUBMITTED,
        GenerationAttemptResolution.Outcome.CONFIRMED_REMOTE_SUBMITTED,
        GenerationAttemptResolution.Outcome.CONFIRMED_REMOTE_FAILED,
        GenerationAttemptResolution.Outcome.ACTUAL_COST_CONFIRMED,
    ))
    note = serializers.CharField(max_length=4000, allow_blank=False, trim_whitespace=True)
    provider_request_id = serializers.CharField(
        required=False, allow_blank=True, max_length=191, default="",
    )
    actual_cost = serializers.DecimalField(
        required=False, allow_null=True, max_digits=14, decimal_places=6,
    )
    cost_currency = serializers.CharField(
        required=False, allow_blank=True, max_length=12, default="",
    )
