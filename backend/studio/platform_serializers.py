from __future__ import annotations

import hashlib
import math
import re
import uuid

from django.conf import settings
from django.core.exceptions import ValidationError as DjangoValidationError
from django.db import transaction
from django.db.models import Max
from django.utils import timezone
from django.urls import reverse
from drf_spectacular.extensions import OpenApiSerializerFieldExtension
from drf_spectacular.utils import PolymorphicProxySerializer, extend_schema_field
from rest_framework import serializers
from rest_framework.exceptions import PermissionDenied as DRFPermissionDenied

from studio.models import (
    ApprovalDecision,
    ArtifactSelectionDecision,
    ComfyWorkflowSource,
    ComfyWorkflowContractDraft,
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
    WorkflowSmokeAssetBinding,
    WorkflowSmokeAttempt,
    WorkflowSmokeAttemptResolution,
    WorkflowProjector,
    WorkflowProjectorVersion,
    WorkflowTemplate,
    WorkflowTemplateVersion,
)
from studio.services.configuration import (
    create_config_revision,
    publish_config_revision,
    redact_configuration_snapshot,
    redact_sensitive_bindings,
    revision_changes_security_boundaries,
    validate_config_value,
)
from studio.services.media_capabilities import (
    SAFE_CONDITIONING_IDENTIFIER_PATTERN,
    validate_model_profile_media_capabilities,
)
from studio.services.async_media_contract import deployment_target_sha256
from studio.services.comfyui_artifacts import (
    artifact_inventory_spec,
    safe_model_relative_filename,
)
from studio.services.json_contracts import (
    assign_json_contract,
    find_forbidden_secret_fields,
    find_sensitive_json_keys,
    json_contract,
    verify_json_contract,
)
from studio.services.model_profile_options import (
    MODEL_PROFILE_SPECIFICATION_SCHEMA_VERSION,
    model_capability_values,
    model_family_value,
    validate_model_profile_registry_selection,
    validate_provider_registry_selection,
)
from studio.services.model_profile_contracts import (
    verified_model_profile_specification,
)
from studio.services.provider_registry import (
    ProviderDriverError,
    driver_for,
    validate_deployment_execution_contract,
    validate_provider_profile,
)
from studio.services.prompt_profile_preview_examples import (
    prompt_profile_preview_example,
)
from studio.services.prompt_profiles import (
    PROMPT_PROFILE_CONTRACT_SCHEMA_VERSION,
    SUPPORTED_FIELD_SOURCE_SELECTORS,
    SUPPORTED_SOURCE_PROJECTION_KEYS,
    normalize_prompt_profile_channel_limits,
    normalize_prompt_profile_source_projection_config,
    refresh_prompt_profile_contract,
    validate_field_definition,
    validate_prompt_operation_key,
    validate_prompt_profile_binding,
    validate_profile_rule,
)
from studio.services.source_projection_primitives import (
    SOURCE_PROJECTION_PRIMITIVES,
)
from studio.media_signing import signed_media_token
from studio.services.workflow_smoke_fixtures import workflow_smoke_fixture_schema
from studio.services.workflow_projectors import (
    WORKFLOW_PROJECTOR_DEFINITION_SCHEMA_VERSION,
    validate_workflow_projector_definition,
)
from studio.services.generated_artifacts import generated_artifact_storage_owner_id
from studio.services.provenance import ProvenanceError
from studio.services.security_boundaries import boundary_int
from studio.services.content_contracts import (
    normalize_audit_note,
    normalize_optional_audit_comment,
)
from studio.services.prompt_normalization_contract import (
    normalization_batch_max_items,
    normalization_text_max_characters,
)
from studio.services.registry_deletion import validate_recipe_registry_references


def _stage_snapshot_max_bytes() -> int:
    return boundary_int("security.snapshot.stage_max_bytes")


def _raise_drf(exc: DjangoValidationError):
    detail = getattr(exc, "message_dict", None) or getattr(exc, "messages", None) or str(exc)
    code = str(getattr(exc, "code", None) or "invalid")
    error = serializers.ValidationError(detail, code=code)
    error.default_code = code
    raise error from exc


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
        if (
            self.instance
            and "key" in attrs
            and attrs["key"] != self.instance.key
        ):
            raise serializers.ValidationError({
                "key": "Provider 唯一键创建后不可修改；请新建服务连接",
            })
        driver_type = attrs.get(
            "driver_type",
            getattr(self.instance, "driver_type", ""),
        )
        same_driver = bool(
            self.instance and driver_type == self.instance.driver_type
        )
        try:
            modalities, operations = validate_provider_registry_selection(
                driver_type=str(driver_type or ""),
                modalities=attrs.get(
                    "modalities",
                    getattr(self.instance, "modalities", ""),
                ),
                operations=attrs.get(
                    "operations",
                    getattr(self.instance, "operations", ""),
                ),
                allowed_legacy_modalities=(
                    tuple(
                        item.strip()
                        for item in self.instance.modalities.split(",")
                        if item.strip()
                    ) if same_driver else ()
                ),
                allowed_legacy_operations=(
                    tuple(
                        item.strip()
                        for item in self.instance.operations.split(",")
                        if item.strip()
                    ) if same_driver else ()
                ),
            )
        except DjangoValidationError as exc:
            _raise_drf(exc)
        attrs["modalities"] = ",".join(modalities)
        attrs["operations"] = ",".join(operations)
        instance = self.instance or ProviderProfile()
        for field, value in attrs.items():
            setattr(instance, field, value)
        config = attrs.get("driver_config", getattr(instance, "driver_config", {}))
        try:
            assign_json_contract(
                instance,
                "driver_config",
                config,
                max_bytes=_stage_snapshot_max_bytes(),
            )
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
            "version",
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
        if (
            self.instance
            and "profile_key" in attrs
            and attrs["profile_key"] != self.instance.profile_key
        ):
            raise serializers.ValidationError({
                "profile_key": "模型版本族标识创建后不可修改；请新建模型族",
            })
        existing_specification: dict = {}
        if self.instance:
            try:
                existing_specification = verified_model_profile_specification(
                    self.instance,
                )
            except DjangoValidationError as exc:
                _raise_drf(exc)
        specification = attrs.get("specification", existing_specification)
        if not isinstance(specification, dict):
            raise serializers.ValidationError({"specification": "模型规格必须是 JSON 对象"})
        requested_schema_version = specification.get("schema_version")
        if requested_schema_version not in {
            None,
            MODEL_PROFILE_SPECIFICATION_SCHEMA_VERSION,
        }:
            raise serializers.ValidationError({
                "specification": (
                    "模型规格结构版本由服务端维护；当前仅支持 "
                    f"{MODEL_PROFILE_SPECIFICATION_SCHEMA_VERSION}"
                ),
            })
        specification = {
            **specification,
            "schema_version": MODEL_PROFILE_SPECIFICATION_SCHEMA_VERSION,
        }
        sensitive = find_forbidden_secret_fields(specification)
        if sensitive:
            raise serializers.ValidationError({
                "specification": f"版本合同不能保存明文密钥字段: {', '.join(sensitive)}",
            })
        modality = attrs.get(
            "modality",
            getattr(self.instance, "modality", ""),
        )
        provider = attrs.get(
            "provider",
            getattr(self.instance, "provider", None),
        )
        if provider is None:
            raise serializers.ValidationError({"provider": "必须选择服务提供方"})
        if not provider.is_active and (
            self.instance is None or "provider" in attrs
        ):
            raise serializers.ValidationError({
                "provider": "只能选择已启用的服务提供方",
            })
        profile_key = attrs.get(
            "profile_key",
            getattr(self.instance, "profile_key", ""),
        )
        allowed_legacy_capabilities = set(
            self.context.get("allowed_legacy_capabilities") or ()
        )
        allowed_model_families = set(
            self.context.get("allowed_model_families") or ()
        )
        if existing_specification:
            allowed_legacy_capabilities.update(
                model_capability_values(existing_specification),
            )
            existing_family = model_family_value(existing_specification)
            if existing_family:
                allowed_model_families.add(existing_family)
        try:
            validate_model_profile_registry_selection(
                specification,
                profile_key=str(profile_key or ""),
                modality=str(modality or ""),
                driver_type=str(provider.driver_type or ""),
                allowed_legacy_capabilities=tuple(
                    sorted(allowed_legacy_capabilities)
                ),
                allowed_model_families=tuple(sorted(allowed_model_families)),
            )
            validate_model_profile_media_capabilities(
                specification,
                modality=str(modality or ""),
            )
        except DjangoValidationError as exc:
            _raise_drf(exc)
        contract = json_contract(
            specification,
            max_bytes=_stage_snapshot_max_bytes(),
        )
        attrs.update({
            "specification": specification,
            "specification_schema_version": contract.schema_version,
            "specification_size_bytes": contract.size_bytes,
            "specification_sha256": contract.sha256,
        })
        return attrs


class ModelProfileNextVersionInputSerializer(serializers.Serializer):
    provider = serializers.PrimaryKeyRelatedField(
        queryset=ProviderProfile.objects.filter(is_active=True),
        required=False,
    )
    model_identifier = serializers.CharField(max_length=255, required=False)
    modality = serializers.ChoiceField(
        choices=ModelProfileVersion.Modality.choices,
        required=False,
    )
    specification = serializers.JSONField(required=False)


class ModelProfileCapabilityOptionSerializer(serializers.Serializer):
    value = serializers.CharField(read_only=True)
    label = serializers.CharField(read_only=True)
    modalities = serializers.ListField(
        child=serializers.CharField(),
        read_only=True,
    )
    driver_types = serializers.ListField(
        child=serializers.CharField(),
        read_only=True,
    )
    legacy = serializers.BooleanField(read_only=True)


class ModelProfileProviderDriverOptionSerializer(serializers.Serializer):
    value = serializers.CharField(read_only=True)
    label = serializers.CharField(read_only=True)
    modalities = serializers.ListField(
        child=serializers.CharField(),
        read_only=True,
    )
    operations = serializers.ListField(
        child=serializers.CharField(),
        read_only=True,
    )


class ConditioningContractEditorOptionsSerializer(serializers.Serializer):
    schema_version = serializers.IntegerField(read_only=True)
    adapter_key = serializers.CharField(read_only=True)
    input_kinds = serializers.ListField(child=serializers.DictField(), read_only=True)
    input_slots = serializers.ListField(child=serializers.DictField(), read_only=True)
    maximum_items = serializers.IntegerField(read_only=True)
    maximum_modes = serializers.IntegerField(read_only=True)
    identifier_pattern = serializers.CharField(read_only=True)
    mode_template = serializers.JSONField(read_only=True)


class ModelProfileRegistryOptionsSerializer(serializers.Serializer):
    conditioning_contract = ConditioningContractEditorOptionsSerializer(read_only=True)
    specification_schema_version = serializers.IntegerField(read_only=True)
    provider_drivers = ModelProfileProviderDriverOptionSerializer(
        many=True,
        read_only=True,
    )
    modalities = serializers.ListField(
        child=serializers.DictField(),
        read_only=True,
    )
    artifact_roles = serializers.ListField(
        child=serializers.DictField(),
        read_only=True,
    )
    capabilities = ModelProfileCapabilityOptionSerializer(
        many=True,
        read_only=True,
    )
    model_families = serializers.ListField(
        child=serializers.CharField(),
        read_only=True,
    )


class PromptFieldDefinitionSerializer(serializers.ModelSerializer):
    preview_example = serializers.SerializerMethodField()

    class Meta:
        model = PromptFieldDefinition
        fields = (
            "id", "key", "label", "category", "description", "impact",
            "value_type", "default_value", "preview_example", "value_options",
            "is_required", "is_active", "display_order", "created_at", "updated_at",
        )
        read_only_fields = (
            "preview_example", "display_order", "created_at", "updated_at",
        )

    @extend_schema_field(serializers.JSONField())
    def get_preview_example(self, obj):
        return prompt_profile_preview_example(obj)

    def validate(self, attrs):
        if (
            self.instance
            and "key" in attrs
            and attrs["key"] != self.instance.key
        ):
            raise serializers.ValidationError({
                "key": "通用字段 key 创建后不可修改；请新建字段并更新草稿 Profile",
            })
        instance = self.instance or PromptFieldDefinition()
        for field, value in attrs.items():
            setattr(instance, field, value)
        try:
            validate_field_definition(instance)
        except DjangoValidationError as exc:
            _raise_drf(exc)
        return attrs

    def create(self, validated_data):
        # Display order is presentation metadata, not business input.  Append
        # new definitions deterministically; reordering belongs to a dedicated
        # operation instead of an unlabelled number in the create form.
        with transaction.atomic():
            maximum = PromptFieldDefinition.objects.aggregate(
                maximum=Max("display_order"),
            )["maximum"]
            validated_data["display_order"] = int(maximum or 0) + 1
            return super().create(validated_data)

    def update(self, instance, validated_data):
        with transaction.atomic():
            instance = super().update(instance, validated_data)
            profile_ids = instance.profile_rules.filter(
                profile_version__status=PromptProfileVersion.Status.DRAFT,
            ).values_list("profile_version_id", flat=True).distinct()
            for profile in PromptProfileVersion.objects.filter(pk__in=profile_ids):
                refresh_prompt_profile_contract(
                    profile,
                    schema_version=PROMPT_PROFILE_CONTRACT_SCHEMA_VERSION,
                )
        return instance


class PromptFieldDefinitionCreateInputSerializer(PromptFieldDefinitionSerializer):
    """Create contract without server-owned presentation/lifecycle fields."""

    class Meta(PromptFieldDefinitionSerializer.Meta):
        read_only_fields = (
            *PromptFieldDefinitionSerializer.Meta.read_only_fields,
            "is_active",
        )


class FormCatalogChoiceSerializer(serializers.Serializer):
    value = serializers.CharField(read_only=True)
    label = serializers.CharField(read_only=True)


class PromptFieldDefinitionOptionsSerializer(serializers.Serializer):
    value_types = FormCatalogChoiceSerializer(many=True, read_only=True)
    categories = FormCatalogChoiceSerializer(many=True, read_only=True)
    custom_entry_fields = serializers.ListField(
        child=serializers.CharField(),
        read_only=True,
    )
    system_defaults = serializers.DictField(read_only=True)


class PromptProfileVersionSerializer(serializers.ModelSerializer):
    field_rule_count = serializers.IntegerField(read_only=True, default=0)
    source_projection_key = serializers.CharField(
        required=True,
        max_length=80,
        help_text=(
            "写入仅允许 options 返回的 direct_v1 或 declarative_v1；响应可能包含仅供"
            "历史冻结运行的 classified_reference_context_ir_v1。"
        ),
    )
    channel_limits = serializers.JSONField(required=False, default=dict)
    source_projection_config = serializers.JSONField(required=False, default=dict)

    class Meta:
        model = PromptProfileVersion
        fields = (
            "id", "profile_key", "version", "name", "description",
            "model_family", "operation", "mode", "source_projection_key",
            "source_projection_config", "official_reference", "channel_limits",
            "status", "review_state", "contract_schema_version",
            "contract_size_bytes", "contract_sha256", "field_rule_count",
            "published_at", "published_by", "created_at", "updated_at",
        )
        read_only_fields = (
            "version", "status", "contract_schema_version", "contract_size_bytes",
            "contract_sha256", "field_rule_count", "published_at",
            "published_by", "created_at", "updated_at",
        )
        extra_kwargs = {
            "operation": {
                "help_text": (
                    "可首次登记的受控 key；发布必须有受控 registry、同模型族已发布模型 "
                    "capability 或注册 Provider driver capability 证据，且不会因此创建执行路由。"
                ),
            },
        }

    def validate_operation(self, value):
        try:
            return validate_prompt_operation_key(value)
        except DjangoValidationError as exc:
            _raise_drf(exc)

    def validate_source_projection_key(self, value):
        if value not in SUPPORTED_SOURCE_PROJECTION_KEYS:
            raise serializers.ValidationError(
                "该来源投影器只支持历史冻结运行，不能用于新建或修改 PromptProfile",
            )
        return value

    def validate(self, attrs):
        if self.instance and self.instance.status in {
            PromptProfileVersion.Status.PUBLISHED,
            PromptProfileVersion.Status.RETIRED,
        }:
            raise serializers.ValidationError(
                "published/retired PromptProfile 不可修改；请创建新版本",
            )
        if (
            self.instance
            and "profile_key" in attrs
            and attrs["profile_key"] != self.instance.profile_key
        ):
            raise serializers.ValidationError({
                "profile_key": "Profile Key 创建后不可修改；请从精确来源创建后继草稿",
            })
        source_projection_key = attrs.get(
            "source_projection_key",
            self.instance.source_projection_key if self.instance else "",
        )
        if source_projection_key not in SUPPORTED_SOURCE_PROJECTION_KEYS:
            raise serializers.ValidationError({
                "source_projection_key": (
                    "该来源投影器只支持历史冻结运行，不能用于新建或修改 PromptProfile"
                ),
            })
        try:
            attrs["channel_limits"] = normalize_prompt_profile_channel_limits(
                attrs.get(
                    "channel_limits",
                    self.instance.channel_limits if self.instance else {},
                ),
            )
            attrs["source_projection_config"] = (
                normalize_prompt_profile_source_projection_config(
                    attrs.get(
                        "source_projection_config",
                        self.instance.source_projection_config
                        if self.instance else {},
                    ),
                )
            )
        except DjangoValidationError as exc:
            _raise_drf(exc)
        return attrs

    def create(self, validated_data):
        with transaction.atomic():
            validated_data["contract_schema_version"] = (
                PROMPT_PROFILE_CONTRACT_SCHEMA_VERSION
            )
            instance = super().create(validated_data)
            refresh_prompt_profile_contract(
                instance,
                schema_version=PROMPT_PROFILE_CONTRACT_SCHEMA_VERSION,
            )
        return instance

    def update(self, instance, validated_data):
        with transaction.atomic():
            validated_data["contract_schema_version"] = (
                PROMPT_PROFILE_CONTRACT_SCHEMA_VERSION
            )
            instance = super().update(instance, validated_data)
            refresh_prompt_profile_contract(
                instance,
                schema_version=PROMPT_PROFILE_CONTRACT_SCHEMA_VERSION,
            )
        return instance


class PromptProfileVersionCreateInputSerializer(PromptProfileVersionSerializer):
    """Create contract with review/version facts allocated by the service."""

    class Meta(PromptProfileVersionSerializer.Meta):
        read_only_fields = (
            *PromptProfileVersionSerializer.Meta.read_only_fields,
            "review_state",
        )


class PromptProfileFieldRuleSerializer(serializers.ModelSerializer):
    field_key = serializers.CharField(source="field_definition.key", read_only=True)
    field_label = serializers.CharField(source="field_definition.label", read_only=True)
    field_value_type = serializers.CharField(
        source="field_definition.value_type", read_only=True,
    )
    generation_mode = serializers.ChoiceField(
        choices=PromptProfileFieldRule.GenerationMode.choices,
        required=True,
    )
    generation_source_selector = serializers.CharField(
        required=True,
        allow_blank=False,
        max_length=191,
    )
    generation_instruction = serializers.CharField(
        required=True,
        allow_blank=True,
        trim_whitespace=True,
    )
    generation_output_semantics = serializers.ChoiceField(
        choices=PromptProfileFieldRule.GenerationOutputSemantics.choices,
        required=True,
    )
    generation_required = serializers.BooleanField(required=True)
    language_policy = serializers.CharField(
        required=True,
        allow_blank=False,
        max_length=64,
    )
    length_unit = serializers.ChoiceField(
        choices=PromptProfileFieldRule.LengthUnit.choices,
        required=True,
    )
    generation_min = serializers.IntegerField(
        required=False,
        allow_null=True,
        min_value=1,
    )
    generation_max = serializers.IntegerField(
        required=False,
        allow_null=True,
        min_value=1,
    )
    generation_validator_keys = serializers.ListField(
        child=serializers.ChoiceField(
            choices=PromptProfileFieldRule.GenerationValidator.choices,
        ),
        required=True,
        allow_empty=True,
    )
    generation_context_selectors = serializers.ListField(
        child=serializers.ChoiceField(
            choices=PromptProfileFieldRule.GenerationContextSelector.choices,
        ),
        required=True,
        allow_empty=True,
    )
    projection_primitive = serializers.ChoiceField(
        choices=tuple(SOURCE_PROJECTION_PRIMITIVES),
        required=False,
        allow_blank=True,
        default="",
    )
    source_selector = serializers.CharField(required=True, max_length=80)

    class Meta:
        model = PromptProfileFieldRule
        fields = (
            "id", "profile_version", "field_definition", "field_key",
            "field_label", "field_value_type", "source_selector", "channel",
            "projection_primitive",
            "position", "label_style", "separator", "required",
            "omit_if_empty", "max_chars", "generation_mode",
            "generation_source_selector", "generation_instruction",
            "generation_output_semantics",
            "generation_required",
            "language_policy", "length_unit", "generation_min",
            "generation_max", "generation_validator_keys", "created_at",
            "generation_context_selectors", "updated_at",
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
        source_selector = attrs.get(
            "source_selector",
            self.instance.source_selector if self.instance else "",
        )
        if source_selector not in SUPPORTED_FIELD_SOURCE_SELECTORS:
            raise serializers.ValidationError({
                "source_selector": (
                    "该来源 selector 只支持已冻结历史 Profile，不能用于新建或修改规则"
                ),
            })
        instance = self.instance or PromptProfileFieldRule()
        for field, value in attrs.items():
            setattr(instance, field, value)
        if not getattr(instance, "profile_version_id", None):
            raise serializers.ValidationError({"profile_version": "此字段为必填项"})
        if not getattr(instance, "field_definition_id", None):
            raise serializers.ValidationError({"field_definition": "此字段为必填项"})
        try:
            validate_profile_rule(
                instance,
                enforce_current_generation_source=True,
            )
        except DjangoValidationError as exc:
            _raise_drf(exc)
        return attrs

    def validate_source_selector(self, value):
        if value not in SUPPORTED_FIELD_SOURCE_SELECTORS:
            raise serializers.ValidationError(
                "该来源 selector 只支持已冻结历史 Profile，不能用于新建或修改规则",
            )
        return value

    def create(self, validated_data):
        with transaction.atomic():
            instance = super().create(validated_data)
            refresh_prompt_profile_contract(
                instance.profile_version,
                schema_version=PROMPT_PROFILE_CONTRACT_SCHEMA_VERSION,
            )
        return instance

    def update(self, instance, validated_data):
        with transaction.atomic():
            instance = super().update(instance, validated_data)
            refresh_prompt_profile_contract(
                instance.profile_version,
                schema_version=PROMPT_PROFILE_CONTRACT_SCHEMA_VERSION,
            )
        return instance


class PromptProfileBindingSerializer(serializers.ModelSerializer):
    prompt_profile_name = serializers.CharField(source="prompt_profile_version.name", read_only=True)
    prompt_profile_version_number = serializers.IntegerField(source="prompt_profile_version.version", read_only=True)
    prompt_profile_status = serializers.CharField(source="prompt_profile_version.status", read_only=True)
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

    def validate_operation(self, value):
        try:
            return validate_prompt_operation_key(value)
        except DjangoValidationError as exc:
            _raise_drf(exc)

    class Meta:
        model = PromptProfileBinding
        fields = (
            "id", "key", "name", "operation", "model_family", "mode",
            "model_profile_version", "model_identifier", "prompt_profile_version",
            "prompt_profile_name", "prompt_profile_version_number", "prompt_profile_status",
            "prompt_profile_key", "prompt_profile_contract_sha256", "priority",
            "is_active", "created_at", "updated_at",
        )
        read_only_fields = (
            "model_identifier", "prompt_profile_key",
            "prompt_profile_contract_sha256", "created_at", "updated_at",
        )
        extra_kwargs = {
            "operation": {
                "help_text": (
                    "受控 operation key。Binding 不授予执行能力；非内置 operation 的 active "
                    "Binding 必须选择精确且声明同名 capability 的已发布模型版本。"
                ),
            },
        }

    def validate(self, attrs):
        if (
            self.instance is not None
            and "key" in attrs
            and attrs["key"] != self.instance.key
        ):
            raise serializers.ValidationError({
                "key": "PromptProfile binding key 创建后不可修改；请新建绑定",
            })
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


class PromptProfilePreviewRuleCandidateSerializer(serializers.Serializer):
    id = serializers.UUIDField(required=False, allow_null=True)
    field_definition = serializers.PrimaryKeyRelatedField(
        queryset=PromptFieldDefinition.objects.all(),
    )
    source_selector = serializers.ChoiceField(
        choices=tuple((value, value) for value in SUPPORTED_FIELD_SOURCE_SELECTORS),
    )
    projection_primitive = serializers.ChoiceField(
        choices=tuple((value, value) for value in SOURCE_PROJECTION_PRIMITIVES),
        required=False,
        allow_blank=True,
        default="",
    )
    channel = serializers.ChoiceField(choices=PromptProfileFieldRule.Channel.choices)
    position = serializers.IntegerField(min_value=1)
    label_style = serializers.ChoiceField(
        choices=PromptProfileFieldRule.LabelStyle.choices,
    )
    separator = serializers.ChoiceField(
        choices=PromptProfileFieldRule.Separator.choices,
    )
    required = serializers.BooleanField()
    omit_if_empty = serializers.BooleanField()
    max_chars = serializers.IntegerField(min_value=1)
    generation_mode = serializers.ChoiceField(
        choices=PromptProfileFieldRule.GenerationMode.choices,
    )
    generation_source_selector = serializers.CharField(
        allow_blank=False,
        max_length=191,
    )
    generation_context_selectors = serializers.ListField(
        child=serializers.ChoiceField(
            choices=PromptProfileFieldRule.GenerationContextSelector.choices,
        ),
        allow_empty=True,
    )
    generation_instruction = serializers.CharField(
        allow_blank=True,
        trim_whitespace=True,
    )
    generation_output_semantics = serializers.ChoiceField(
        choices=PromptProfileFieldRule.GenerationOutputSemantics.choices,
    )
    generation_required = serializers.BooleanField()
    language_policy = serializers.CharField(allow_blank=False, max_length=64)
    length_unit = serializers.ChoiceField(
        choices=PromptProfileFieldRule.LengthUnit.choices,
    )
    generation_min = serializers.IntegerField(
        required=False,
        allow_null=True,
        min_value=1,
    )
    generation_max = serializers.IntegerField(
        required=False,
        allow_null=True,
        min_value=1,
    )
    generation_validator_keys = serializers.ListField(
        child=serializers.ChoiceField(
            choices=PromptProfileFieldRule.GenerationValidator.choices,
        ),
        allow_empty=True,
    )


class PromptProfilePreviewCandidateSerializer(serializers.Serializer):
    model_family = serializers.RegexField(
        regex=r"^[a-z][a-z0-9_.-]{1,99}$",
        max_length=100,
    )
    operation = serializers.CharField(max_length=100)
    mode = serializers.RegexField(
        regex=r"^[A-Za-z0-9][A-Za-z0-9_.-]{0,39}$",
        max_length=40,
        required=False,
        allow_blank=True,
    )
    source_projection_key = serializers.ChoiceField(
        choices=tuple((value, value) for value in SUPPORTED_SOURCE_PROJECTION_KEYS),
    )
    source_projection_config = serializers.JSONField(required=False, default=dict)
    channel_limits = serializers.JSONField(required=False, default=dict)
    field_rules = PromptProfilePreviewRuleCandidateSerializer(many=True)

    def validate_operation(self, value):
        try:
            return validate_prompt_operation_key(value)
        except DjangoValidationError as exc:
            _raise_drf(exc)

    def validate(self, attrs):
        try:
            attrs["source_projection_config"] = (
                normalize_prompt_profile_source_projection_config(
                    attrs.get("source_projection_config", {}),
                )
            )
            attrs["channel_limits"] = normalize_prompt_profile_channel_limits(
                attrs.get("channel_limits", {}),
            )
        except DjangoValidationError as exc:
            _raise_drf(exc)
        return attrs


class PromptProfilePreviewInputSerializer(serializers.Serializer):
    sources = serializers.JSONField(required=False, default=dict)
    expected_base_contract_sha256 = serializers.RegexField(
        regex=r"^[0-9a-f]{64}$",
        required=False,
    )
    candidate = PromptProfilePreviewCandidateSerializer(required=False)

    def validate_sources(self, value):
        if not isinstance(value, dict):
            raise serializers.ValidationError("sources 必须是对象")
        return value

    def validate(self, attrs):
        if "candidate" in attrs and not attrs.get("expected_base_contract_sha256"):
            raise serializers.ValidationError({
                "expected_base_contract_sha256": "预览未保存配置时必须提交当前基础合同摘要",
            })
        return attrs


class PromptProfileCloneSuccessorInputSerializer(serializers.Serializer):
    expected_source_contract_sha256 = serializers.RegexField(
        regex=r"^[0-9a-f]{64}$",
    )
    name = serializers.CharField(min_length=1, max_length=160, trim_whitespace=True)


class PromptProfilePreviewSerializer(serializers.Serializer):
    schema_version = serializers.IntegerField(read_only=True)
    prompt_profile_version_id = serializers.UUIDField(read_only=True)
    profile_key = serializers.CharField(read_only=True)
    version = serializers.IntegerField(read_only=True)
    contract_sha256 = serializers.CharField(read_only=True)
    channels = serializers.DictField(read_only=True)
    channel_limits = serializers.DictField(read_only=True)
    channel_audit = serializers.DictField(read_only=True)
    warnings = serializers.ListField(child=serializers.CharField(), read_only=True)
    structured_fields = serializers.DictField(read_only=True)
    trace = serializers.ListField(child=serializers.DictField(), read_only=True)
    source_projection = serializers.DictField(read_only=True, required=False)
    ephemeral = serializers.BooleanField(read_only=True)
    preview_scope = serializers.CharField(read_only=True)
    base_contract_sha256 = serializers.CharField(read_only=True)
    executed_stages = serializers.DictField(read_only=True)


class PromptProfileGenerationContextSelectorOptionSerializer(
    serializers.Serializer,
):
    value = serializers.CharField(read_only=True)
    label = serializers.CharField(read_only=True)
    description = serializers.CharField(read_only=True)


class PromptProfileGenerationSourceOptionSerializer(serializers.Serializer):
    value = serializers.CharField(read_only=True)
    label = serializers.CharField(read_only=True)
    scope_type = serializers.CharField(read_only=True)
    group = serializers.CharField(read_only=True)
    group_label = serializers.CharField(read_only=True)
    description = serializers.CharField(read_only=True)
    availability = serializers.CharField(read_only=True)
    scope = serializers.CharField(read_only=True)
    dialogue_exposure = serializers.CharField(read_only=True)
    cross_shot_exposure = serializers.CharField(read_only=True)
    cardinality = serializers.CharField(read_only=True)
    status = serializers.CharField(read_only=True)
    generation_modes = serializers.ListField(
        child=serializers.CharField(),
        read_only=True,
    )


class PromptProfileSourceProjectionOptionSerializer(serializers.Serializer):
    value = serializers.CharField(read_only=True)
    label = serializers.CharField(read_only=True)


class PromptProfileSourceProjectionPrimitiveOptionSerializer(serializers.Serializer):
    value = serializers.CharField(read_only=True)
    label = serializers.CharField(read_only=True)
    description = serializers.CharField(read_only=True)
    engine_key = serializers.CharField(read_only=True)
    rendering_template_keys = serializers.ListField(child=serializers.CharField(), read_only=True)
    contribution_source_selectors = serializers.ListField(
        child=serializers.CharField(),
        read_only=True,
    )


class PromptProfileEffectiveLimitsSerializer(serializers.Serializer):
    field_text_max_characters = serializers.IntegerField(
        min_value=1,
        read_only=True,
    )
    max_validators = serializers.IntegerField(min_value=1, read_only=True)
    max_context_selectors = serializers.IntegerField(min_value=1, read_only=True)


class ReferencePlaceholderHelpSerializer(serializers.Serializer):
    label = serializers.CharField(read_only=True)
    description = serializers.CharField(read_only=True)
    example = serializers.CharField(read_only=True, allow_blank=True)


class ReferenceRenderingExampleSerializer(serializers.Serializer):
    template = serializers.CharField(read_only=True)
    result = serializers.CharField(read_only=True, allow_blank=True)


class ReferenceRenderingOptionSerializer(serializers.Serializer):
    key = serializers.CharField(read_only=True)
    label = serializers.CharField(read_only=True)
    placeholders = serializers.ListField(child=serializers.CharField(), read_only=True)
    description = serializers.CharField(read_only=True)
    assembly = serializers.CharField(read_only=True)
    requirement = serializers.CharField(read_only=True)
    placeholder_help = serializers.DictField(child=ReferencePlaceholderHelpSerializer(), read_only=True)
    example = ReferenceRenderingExampleSerializer(read_only=True)


class PromptProfileOptionsSerializer(serializers.Serializer):
    operations = serializers.ListField(
        child=serializers.CharField(),
        read_only=True,
        help_text=(
            "已登记 operation 的录入建议，不是允许列表或执行授权；API 仍接受符合 "
            "PromptProfileVersion.operation 格式的新 key。"
        ),
    )
    model_families = serializers.ListField(child=serializers.CharField(), read_only=True)
    modes = serializers.ListField(child=serializers.CharField(), read_only=True)
    field_value_types = FormCatalogChoiceSerializer(many=True, read_only=True)
    field_categories = FormCatalogChoiceSerializer(many=True, read_only=True)
    custom_entry_fields = serializers.ListField(
        child=serializers.CharField(),
        read_only=True,
    )
    field_system_defaults = serializers.DictField(read_only=True)
    rule_system_defaults = serializers.DictField(read_only=True)
    source_selectors = serializers.ListField(child=serializers.DictField(), read_only=True)
    channels = serializers.ListField(child=serializers.DictField(), read_only=True)
    label_styles = serializers.ListField(child=serializers.DictField(), read_only=True)
    separators = serializers.ListField(child=serializers.DictField(), read_only=True)
    generation_modes = serializers.ListField(
        child=serializers.DictField(), read_only=True,
    )
    generation_output_semantics = serializers.ListField(
        child=serializers.DictField(), read_only=True,
    )
    source_projection_keys = PromptProfileSourceProjectionOptionSerializer(
        many=True,
        read_only=True,
    )
    reference_rendering_fields = ReferenceRenderingOptionSerializer(many=True, read_only=True)
    source_projection_primitives = (
        PromptProfileSourceProjectionPrimitiveOptionSerializer(
            many=True,
            read_only=True,
        )
    )
    language_policies = serializers.ListField(
        child=serializers.DictField(), read_only=True,
    )
    length_units = serializers.ListField(
        child=serializers.DictField(), read_only=True,
    )
    generation_validators = serializers.ListField(
        child=serializers.DictField(), read_only=True,
    )
    generation_context_selectors = (
        PromptProfileGenerationContextSelectorOptionSerializer(
            many=True,
            read_only=True,
        )
    )
    generation_source_selectors = PromptProfileGenerationSourceOptionSerializer(
        many=True,
        read_only=True,
    )
    model_profile_versions = serializers.ListField(
        child=serializers.DictField(), read_only=True,
    )
    effective_limits = PromptProfileEffectiveLimitsSerializer(read_only=True)


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

    def validate_filename(self, value):
        normalized = safe_model_relative_filename(value)
        if normalized is None:
            raise serializers.ValidationError(
                "filename 必须是受控 models 目录内的安全相对路径",
            )
        return normalized

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
        # A draft artifact may have been attested already.  A locator/digest
        # change is accepted without asking the client to forge a lifecycle
        # status; the view atomically invalidates the old verification.  An
        # explicitly supplied status remains restricted to the initial
        # declaration value.
        if (
            "status" in attrs
            and attrs["status"] != ModelArtifact.Status.DECLARED
        ):
            raise serializers.ValidationError({
                "status": "模型文件状态只能由主机快照核验动作更新",
            })
        provider = getattr(target_profile, "provider", None)
        if provider and provider.driver_type == ProviderProfile.DriverType.COMFYUI:
            candidate = ModelArtifact(
                role=attrs.get("role", getattr(self.instance, "role", "")),
                inventory_folder=attrs.get(
                    "inventory_folder", getattr(self.instance, "inventory_folder", ""),
                ),
                inventory_node_type=attrs.get(
                    "inventory_node_type", getattr(self.instance, "inventory_node_type", ""),
                ),
                inventory_input_name=attrs.get(
                    "inventory_input_name", getattr(self.instance, "inventory_input_name", ""),
                ),
            )
            try:
                artifact_inventory_spec(candidate)
            except ValueError as exc:
                raise serializers.ValidationError({"inventory_folder": str(exc)}) from exc
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
                max_bytes=_stage_snapshot_max_bytes(),
            )
            verify_json_contract(
                snapshot.evidence,
                schema_version=snapshot.evidence_schema_version,
                size_bytes=snapshot.evidence_size_bytes,
                sha256=snapshot.evidence_sha256,
                max_bytes=_stage_snapshot_max_bytes(),
            )
            operations = operations_contract.value
            fresh = bool(
                snapshot.status == snapshot.Status.PASSED
                and snapshot.expires_at > timezone.now()
                and snapshot.target_sha256 == deployment_target_sha256(obj)
            )
            from studio.services.deployment_smoke_contract import TEXT_DEPLOYMENT_SMOKE_OPERATIONS
            from studio.services.deployment_smoke_profiles import snapshot_profile_matches
            if any(operation in TEXT_DEPLOYMENT_SMOKE_OPERATIONS for operation in operations):
                snapshot.deployment = obj
                fresh = fresh and snapshot_profile_matches(snapshot)
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
        if (
            self.instance is not None
            and self.instance.status == ModelDeployment.Status.RETIRED
        ):
            raise serializers.ValidationError(
                "retired deployment 不可修改；请创建新部署记录",
            )
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
        if target_status == ModelDeployment.Status.RETIRED:
            raise serializers.ValidationError({
                "status": "deployment 只能通过 retire 动作退役",
            })
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
        contract = json_contract(
            runtime_config,
            max_bytes=_stage_snapshot_max_bytes(),
        )
        attrs.update({
            "runtime_config_schema_version": contract.schema_version,
            "runtime_config_size_bytes": contract.size_bytes,
            "runtime_config_sha256": contract.sha256,
        })
        temporary = self.instance or ModelDeployment()
        for field, value in attrs.items():
            setattr(temporary, field, value)
        if target_status == ModelDeployment.Status.ACTIVE:
            try:
                validate_deployment_execution_contract(temporary)
            except (DjangoValidationError, ProviderDriverError, RuntimeError) as exc:
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


class WorkflowLinkedModelSerializer(serializers.Serializer):
    id = serializers.UUIDField()
    key = serializers.CharField()
    version = serializers.IntegerField()
    status = serializers.CharField()
    instance_count = serializers.IntegerField(min_value=0)


class WorkflowLinkedDeploymentSerializer(serializers.Serializer):
    id = serializers.UUIDField()
    name = serializers.CharField()
    status = serializers.CharField()
    model_profile_version_id = serializers.UUIDField()


class WorkflowLinkedPromptSerializer(serializers.Serializer):
    binding_id = serializers.UUIDField()
    profile_id = serializers.UUIDField()
    name = serializers.CharField()
    version = serializers.IntegerField()
    status = serializers.CharField()
    scope = serializers.ChoiceField(choices=("exact_model", "model_family"))
    operation = serializers.CharField()
    mode = serializers.CharField(allow_blank=True)
    is_active = serializers.BooleanField()


class WorkflowLinkedPromptsSerializer(serializers.Serializer):
    state = serializers.ChoiceField(choices=("available", "model_unbound", "family_missing"))
    total = serializers.IntegerField(min_value=0)
    active_total = serializers.IntegerField(min_value=0)
    preview = WorkflowLinkedPromptSerializer(many=True, max_length=3)


class WorkflowLinkedProjectorSerializer(serializers.Serializer):
    state = serializers.ChoiceField(choices=("linked", "not_recorded", "invalid"))
    version_id = serializers.UUIDField(allow_null=True)
    name = serializers.CharField(allow_blank=True)
    key = serializers.CharField(allow_blank=True)
    version = serializers.IntegerField(allow_null=True)
    status = serializers.CharField(allow_blank=True)
    is_current = serializers.BooleanField()
    is_active = serializers.BooleanField()
    definition_sha256 = serializers.CharField(allow_blank=True)


class WorkflowConfigurationLinksSerializer(serializers.Serializer):
    model = WorkflowLinkedModelSerializer(allow_null=True)
    validated_deployment = WorkflowLinkedDeploymentSerializer(allow_null=True)
    prompt_bindings = WorkflowLinkedPromptsSerializer()
    projector = WorkflowLinkedProjectorSerializer()


class WorkflowVersionBatchListSerializer(serializers.ListSerializer):
    def to_representation(self, data):
        from studio.services.workflow_configuration_links import workflow_configuration_links

        rows = list(data.all() if hasattr(data, "all") else data)
        self.context["workflow_configuration_links"] = workflow_configuration_links([row.id for row in rows])
        return super().to_representation(rows)


class WorkflowTemplateVersionListSerializer(serializers.ModelSerializer):
    template_name = serializers.CharField(source="template.name", read_only=True)
    template_slug = serializers.CharField(source="template.slug", read_only=True)
    output_kind = serializers.CharField(source="template.output_kind", read_only=True)
    semantic_binding_count = serializers.IntegerField(read_only=True)
    model_requirement_count = serializers.IntegerField(read_only=True)
    configuration_links = serializers.SerializerMethodField()

    @extend_schema_field(WorkflowConfigurationLinksSerializer)
    def get_configuration_links(self, obj):
        from studio.services.workflow_configuration_links import workflow_configuration_links

        links = self.context.get("workflow_configuration_links")
        if links is None:
            links = workflow_configuration_links([obj.id])
        return links[str(obj.id)]

    class Meta:
        model = WorkflowTemplateVersion
        list_serializer_class = WorkflowVersionBatchListSerializer
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
            "published_by", "created_at", "updated_at", "configuration_links",
        )


class WorkflowExecutionParameterSchemaSerializer(serializers.Serializer):
    semantic_key = serializers.CharField()
    type = serializers.ChoiceField(choices=("string", "integer", "number", "boolean", "enum"))
    required = serializers.BooleanField()
    min = serializers.FloatField(allow_null=True)
    max = serializers.FloatField(allow_null=True)
    multiple_of = serializers.FloatField(allow_null=True)
    alignment_modulus = serializers.IntegerField(allow_null=True, min_value=1)
    alignment_remainder = serializers.IntegerField(allow_null=True, min_value=0)
    min_length = serializers.IntegerField(allow_null=True)
    max_length = serializers.IntegerField(allow_null=True)
    options = serializers.ListField(child=serializers.JSONField())
    selection_group = serializers.CharField(allow_blank=True)
    recommended_value = serializers.JSONField(allow_null=True)


class WorkflowExecutionAssetSlotSchemaSerializer(serializers.Serializer):
    semantic_key = serializers.CharField()
    input_slot = serializers.IntegerField(min_value=1)
    media_kind = serializers.ChoiceField(choices=("image", "video", "audio"))
    category = serializers.CharField(allow_blank=True)
    min_items = serializers.IntegerField(min_value=0)
    max_items = serializers.IntegerField(min_value=0)
    required = serializers.BooleanField()


class WorkflowExecutionAssetGroupSchemaSerializer(serializers.Serializer):
    key = serializers.CharField()
    media_kind = serializers.ChoiceField(choices=("image", "video", "audio"))
    min_items = serializers.IntegerField(min_value=0)
    max_items = serializers.IntegerField(min_value=1)
    semantic_keys = serializers.ListField(child=serializers.CharField())
    wiring = serializers.ChoiceField(choices=("ordered_ports", "reduce"))


class WorkflowExecutionInputSchemaSerializer(serializers.Serializer):
    schema_version = serializers.IntegerField(min_value=2, max_value=2)
    available = serializers.BooleanField()
    reason_code = serializers.CharField(allow_blank=True)
    parameters = WorkflowExecutionParameterSchemaSerializer(many=True)
    asset_slots = WorkflowExecutionAssetSlotSchemaSerializer(many=True)
    asset_groups = WorkflowExecutionAssetGroupSchemaSerializer(many=True)
    dynamic_references = serializers.JSONField(allow_null=True)
    dynamic_reference_modes = serializers.DictField(child=serializers.JSONField())


class WorkflowTemplateVersionDetailSerializer(WorkflowTemplateVersionListSerializer):
    semantic_bindings = WorkflowSemanticBindingSerializer(many=True, read_only=True)
    model_requirements = WorkflowModelRequirementSerializer(many=True, read_only=True)
    smoke_fixture_schema = serializers.SerializerMethodField()

    @extend_schema_field(WorkflowExecutionInputSchemaSerializer)
    def get_smoke_fixture_schema(self, obj) -> dict:
        try:
            return workflow_smoke_fixture_schema(obj)
        except DjangoValidationError:
            return {
                "schema_version": 2,
                "available": False,
                "reason_code": "workflow_fixture_contract_invalid",
                "parameters": [],
                "asset_slots": [],
                "asset_groups": [],
                "dynamic_references": None,
                "dynamic_reference_modes": {},
            }

    class Meta(WorkflowTemplateVersionListSerializer.Meta):
        fields = WorkflowTemplateVersionListSerializer.Meta.fields + (
            "graph", "input_contract", "semantic_bindings", "model_requirements",
            "validation_evidence", "smoke_test_evidence", "smoke_fixture_schema",
        )


class AcceptanceSmokeQuerySerializer(serializers.Serializer):
    subject = serializers.ChoiceField(
        choices=("deployment", "workflow"),
        required=True,
    )
    search = serializers.CharField(required=False, allow_blank=True, max_length=160)
    deployment_id = serializers.UUIDField(required=False)
    workflow_id = serializers.UUIDField(
        required=False,
        help_text="仅适用于 subject=workflow。",
    )
    page = serializers.IntegerField(required=False, min_value=1)
    page_size = serializers.IntegerField(
        required=False,
        min_value=1,
        help_text="每页数量；超过 200 时钳制为 200。",
    )

    def to_internal_value(self, data):
        unknown = sorted(set(data.keys()) - set(self.fields))
        if unknown:
            raise serializers.ValidationError({
                key: "未知查询参数"
                for key in unknown
            })
        return super().to_internal_value(data)

    def validate(self, attrs):
        if attrs.get("workflow_id") and attrs.get("subject") != "workflow":
            raise serializers.ValidationError({
                "workflow_id": "workflow_id 只适用于 subject=workflow",
            })
        return attrs


class AcceptanceActionSerializer(serializers.Serializer):
    kind = serializers.CharField()
    method = serializers.ChoiceField(choices=("GET", "POST"))
    url = serializers.CharField()
    ui_url = serializers.CharField()
    enabled = serializers.BooleanField()
    reason_code = serializers.CharField()
    requires_fixture = serializers.BooleanField()
    requires_confirmation = serializers.BooleanField()
    may_call_external_service = serializers.BooleanField()


class AcceptanceDeploymentReferenceSerializer(serializers.Serializer):
    id = serializers.UUIDField()
    key = serializers.CharField()
    name = serializers.CharField()
    status = serializers.CharField()
    profile_version_id = serializers.UUIDField()
    provider_id = serializers.UUIDField()
    provider_driver = serializers.CharField()
    runtime_host_id = serializers.UUIDField(allow_null=True)


class AcceptanceDeploymentSummarySerializer(
    AcceptanceDeploymentReferenceSerializer,
):
    current_target_sha256 = serializers.CharField(allow_blank=True)
    profile_contract_state = serializers.CharField()
    profile_contract_reason_code = serializers.CharField()
    target_state = serializers.CharField()
    target_reason_code = serializers.CharField()


class WorkflowAcceptanceSubjectSerializer(serializers.Serializer):
    id = serializers.UUIDField()
    template_id = serializers.UUIDField()
    template_name = serializers.CharField()
    template_slug = serializers.CharField()
    version = serializers.IntegerField()
    status = serializers.CharField()
    is_current = serializers.BooleanField()
    output_kind = serializers.CharField()
    model_profile_version_id = serializers.UUIDField(allow_null=True)
    provider_id = serializers.UUIDField(allow_null=True)


class WorkflowAssociationEvidenceSerializer(serializers.Serializer):
    level = serializers.ChoiceField(
        choices=("unbound", "compatible", "validated", "executed"),
    )
    deployment_id = serializers.UUIDField(allow_null=True)
    profile_matches = serializers.BooleanField()
    validated_deployment_matches = serializers.BooleanField()
    has_smoke_attempt = serializers.BooleanField()


class WorkflowValidationFreshnessSerializer(serializers.Serializer):
    stored_state = serializers.CharField()
    state = serializers.CharField()
    fresh = serializers.BooleanField()
    reason_code = serializers.CharField(allow_blank=True)
    message = serializers.CharField(allow_blank=True)
    evidence_sha256 = serializers.CharField(allow_blank=True)
    stored_target_sha256 = serializers.CharField(allow_blank=True)
    current_target_sha256 = serializers.CharField(allow_blank=True)
    validated_deployment_id = serializers.UUIDField(allow_null=True)


class SafeWorkflowSmokeAttemptSerializer(serializers.Serializer):
    id = serializers.UUIDField()
    deployment_id = serializers.UUIDField()
    status = serializers.CharField()
    target_sha256 = serializers.CharField(allow_blank=True)
    graph_sha256 = serializers.CharField(allow_blank=True)
    error_code = serializers.CharField(allow_blank=True)
    resolution_required = serializers.BooleanField()
    created_at = serializers.DateTimeField()
    started_at = serializers.DateTimeField(allow_null=True)
    finished_at = serializers.DateTimeField(allow_null=True)


class WorkflowSmokeFreshnessSerializer(serializers.Serializer):
    stored_state = serializers.CharField()
    state = serializers.CharField()
    fresh = serializers.BooleanField()
    reason_code = serializers.CharField(allow_blank=True)
    message = serializers.CharField(allow_blank=True)
    evidence_sha256 = serializers.CharField(allow_blank=True)
    evidence_deployment_id = serializers.UUIDField(allow_null=True)
    latest_attempt = SafeWorkflowSmokeAttemptSerializer(allow_null=True)


class WorkflowAcceptanceRowSerializer(serializers.Serializer):
    subject = serializers.ChoiceField(choices=("workflow",))
    workflow = WorkflowAcceptanceSubjectSerializer()
    deployment = AcceptanceDeploymentReferenceSerializer(allow_null=True)
    association = WorkflowAssociationEvidenceSerializer()
    validation = WorkflowValidationFreshnessSerializer()
    smoke = WorkflowSmokeFreshnessSerializer()
    actions = AcceptanceActionSerializer(many=True)


class SafeDeploymentSmokeAttemptSerializer(serializers.Serializer):
    prompt_profile = serializers.JSONField(allow_null=True)
    id = serializers.UUIDField()
    operation = serializers.CharField()
    status = serializers.CharField()
    request_sha256 = serializers.CharField(allow_blank=True)
    target_sha256 = serializers.CharField(allow_blank=True)
    error_code = serializers.CharField(allow_blank=True)
    resolution_required = serializers.BooleanField()
    created_at = serializers.DateTimeField()
    submission_started_at = serializers.DateTimeField(allow_null=True)
    finished_at = serializers.DateTimeField(allow_null=True)


class AcceptanceSnapshotSerializer(serializers.Serializer):
    prompt_profile = serializers.JSONField(allow_null=True)
    id = serializers.UUIDField()
    probe_kind = serializers.CharField()
    status = serializers.CharField()
    operations = serializers.ListField(child=serializers.CharField())
    target_sha256 = serializers.CharField(allow_blank=True)
    target_matches = serializers.BooleanField()
    fresh = serializers.BooleanField()
    captured_at = serializers.DateTimeField()
    expires_at = serializers.DateTimeField()


class DeploymentSmokePromptProfileSerializer(serializers.Serializer):
    prompt_profile = serializers.JSONField()
    evidence_state = serializers.CharField()
    latest_snapshot = AcceptanceSnapshotSerializer(allow_null=True)
    fresh_passed_snapshot = AcceptanceSnapshotSerializer(allow_null=True)


class DeploymentSmokeOperationSerializer(serializers.Serializer):
    prompt_profiles = DeploymentSmokePromptProfileSerializer(many=True)
    operation = serializers.CharField()
    applicable = serializers.BooleanField()
    evidence_state = serializers.CharField()
    latest_snapshot = AcceptanceSnapshotSerializer(allow_null=True)
    fresh_passed_snapshot = AcceptanceSnapshotSerializer(allow_null=True)
    latest_attempt = SafeDeploymentSmokeAttemptSerializer(allow_null=True)
    action = AcceptanceActionSerializer()


class DeploymentSmokeProjectionSerializer(serializers.Serializer):
    applicable = serializers.BooleanField()
    reason_code = serializers.CharField()
    operations = DeploymentSmokeOperationSerializer(many=True)


class WorkflowAssociationSummarySerializer(serializers.Serializer):
    profile_match_count = serializers.IntegerField(min_value=0)
    compatible_only_count = serializers.IntegerField(min_value=0)
    validated_count = serializers.IntegerField(min_value=0)
    smoke_history_count = serializers.IntegerField(min_value=0)
    fresh_validation_count = serializers.IntegerField(min_value=0)
    drifted_validation_count = serializers.IntegerField(min_value=0)
    fresh_smoke_count = serializers.IntegerField(min_value=0)
    unresolved_smoke_count = serializers.IntegerField(min_value=0)


class DeploymentAcceptanceRowSerializer(serializers.Serializer):
    subject = serializers.ChoiceField(choices=("deployment",))
    deployment = AcceptanceDeploymentSummarySerializer()
    health = AcceptanceSnapshotSerializer(allow_null=True)
    deployment_smoke = DeploymentSmokeProjectionSerializer()
    workflow_association_summary = WorkflowAssociationSummarySerializer()
    actions = AcceptanceActionSerializer(many=True)


class AcceptanceSmokePageSerializer(serializers.Serializer):
    count = serializers.IntegerField(min_value=0)
    next = serializers.URLField(allow_null=True)
    previous = serializers.URLField(allow_null=True)
    results = serializers.SerializerMethodField()

    @extend_schema_field(PolymorphicProxySerializer(
        component_name="AcceptanceSmokeRow",
        serializers={
            "workflow": WorkflowAcceptanceRowSerializer,
            "deployment": DeploymentAcceptanceRowSerializer,
        },
        resource_type_field_name="subject",
        many=True,
    ))
    def get_results(self, obj):
        return obj.get("results", [])


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


class WorkflowProjectorSerializer(serializers.ModelSerializer):
    class Meta:
        model = WorkflowProjector
        fields = (
            "id", "key", "name", "description", "is_active", "created_at", "updated_at",
        )
        read_only_fields = ("created_at", "updated_at")

    def validate(self, attrs):
        if self.instance and "key" in attrs and attrs["key"] != self.instance.key:
            raise serializers.ValidationError({
                "key": "工作流合同方案稳定 key 创建后不可修改",
            })
        return attrs


class WorkflowProjectorVersionSerializer(serializers.ModelSerializer):
    projector_key = serializers.CharField(source="projector.key", read_only=True)
    definition = serializers.JSONField(help_text="Projector 完整定义。parameter_policies[] 可在 runtime/object_info 策略下声明可选 constraints：min/max/default/multiple_of/alignment_modulus/alignment_remainder；仅数值参数，生成合同时只能收窄节点和引擎边界，默认值须合法。固定/来源锁定策略不允许非空范围。发布后不可变，更改需新版本和重新生成工作流。")

    class Meta:
        model = WorkflowProjectorVersion
        fields = (
            "id", "projector", "projector_key", "version", "engine_key",
            "engine_version", "provider_driver_type", "operation", "output_kind",
            "definition", "definition_schema_version",
            "definition_size_bytes", "definition_sha256", "status", "is_current",
            "published_at", "published_by", "created_at", "updated_at",
        )
        read_only_fields = (
            "version", "definition_schema_version", "definition_size_bytes",
            "definition_sha256", "status", "is_current", "published_at",
            "published_by", "created_at", "updated_at",
        )

    def validate(self, attrs):
        if self.instance and self.instance.status != WorkflowProjectorVersion.Status.DRAFT:
            raise serializers.ValidationError("published/retired 工作流合同方案版本不可修改")
        if self.instance and "projector" in attrs and attrs["projector"] != self.instance.projector:
            raise serializers.ValidationError({"projector": "方案版本创建后不可改挂其他方案"})
        projector = attrs.get("projector", getattr(self.instance, "projector", None))
        if projector is None or not projector.is_active:
            raise serializers.ValidationError({"projector": "只能为启用的工作流合同方案创建版本"})
        driver = attrs.get(
            "provider_driver_type",
            getattr(self.instance, "provider_driver_type", ""),
        )
        if driver != ProviderProfile.DriverType.COMFYUI:
            raise serializers.ValidationError({
                "provider_driver_type": "当前只支持 ComfyUI saved-workflow 合同方案",
            })
        operation = str(attrs.get("operation", getattr(self.instance, "operation", "")))
        if operation != "workflow.import":
            raise serializers.ValidationError({
                "operation": "当前只注册 workflow.import 操作",
            })
        engine_key = str(attrs.get("engine_key", getattr(self.instance, "engine_key", "")))
        engine_version = attrs.get(
            "engine_version", getattr(self.instance, "engine_version", 0),
        )
        definition = attrs.get(
            "definition", getattr(self.instance, "definition", {}),
        )
        try:
            definition = validate_workflow_projector_definition(
                definition,
                engine_key=engine_key,
                engine_version=engine_version,
            )
            contract = json_contract(
                definition,
                schema_version=WORKFLOW_PROJECTOR_DEFINITION_SCHEMA_VERSION,
                max_bytes=_stage_snapshot_max_bytes(),
            )
        except DjangoValidationError as exc:
            _raise_drf(exc)
        output_kind = str(attrs.get("output_kind", getattr(self.instance, "output_kind", "")))
        declared_output_kinds = set(definition.get("output_nodes", {}).values())
        if output_kind and output_kind not in declared_output_kinds:
            raise serializers.ValidationError({
                "output_kind": "版本输出类型必须由 definition.output_nodes 声明",
            })
        attrs.update({
            "definition": definition,
            "definition_schema_version": contract.schema_version,
            "definition_size_bytes": contract.size_bytes,
            "definition_sha256": contract.sha256,
        })
        return attrs


class WorkflowProjectorVersionCloneSerializer(serializers.Serializer):
    definition = serializers.JSONField(required=False, help_text="同一 Projector 定义；数值 parameter_policies.constraints 仅允许收窄节点／引擎边界，不能解锁引擎固定参数。")
    engine_key = serializers.CharField(required=False, max_length=100)
    engine_version = serializers.IntegerField(required=False, min_value=1)
    provider_driver_type = serializers.ChoiceField(
        choices=ProviderProfile.DriverType.choices,
        required=False,
    )
    operation = serializers.CharField(required=False, max_length=120)
    output_kind = serializers.ChoiceField(
        choices=("", *[value for value, _label in WorkflowTemplate.OutputKind.choices]),
        required=False,
    )


class WorkflowProjectorChoiceOptionSerializer(serializers.Serializer):
    value = serializers.CharField(allow_blank=True)
    label = serializers.CharField()


class WorkflowProjectorAdapterContractSerializer(serializers.Serializer):
    compiler = serializers.CharField()
    output = serializers.CharField()
    dynamic = serializers.CharField()


class WorkflowProjectorEditorFieldSerializer(serializers.Serializer):
    path = serializers.CharField()
    label = serializers.CharField()
    help = serializers.CharField()
    control = serializers.CharField()
    required = serializers.BooleanField()
    read_only = serializers.BooleanField()


class WorkflowProjectorCreationTemplateSerializer(serializers.Serializer):
    provider_driver_type = serializers.CharField()
    operation = serializers.CharField()
    output_kind = serializers.CharField(allow_blank=True)
    definition = serializers.JSONField(help_text="未保存的空白编辑骨架；业务字段需填写并通过完整校验，不能直接执行或发布。")


class WorkflowProjectorEngineOptionSerializer(serializers.Serializer):
    key = serializers.CharField()
    version = serializers.IntegerField(min_value=1)
    label = serializers.CharField()
    description = serializers.CharField()
    projection_kind = serializers.CharField()
    adapter_contract = WorkflowProjectorAdapterContractSerializer()
    locked_fields = serializers.ListField(child=serializers.CharField())
    projection_fields = WorkflowProjectorEditorFieldSerializer(many=True)
    creation_template = WorkflowProjectorCreationTemplateSerializer()


class WorkflowProjectorAdapterOptionsSerializer(serializers.Serializer):
    compiler = WorkflowProjectorChoiceOptionSerializer(many=True)
    output = WorkflowProjectorChoiceOptionSerializer(many=True)
    dynamic = WorkflowProjectorChoiceOptionSerializer(many=True)


class WorkflowProjectorEditorChoiceCatalogSerializer(serializers.Serializer):
    asset_group_wirings = WorkflowProjectorChoiceOptionSerializer(many=True)
    cardinalities = WorkflowProjectorChoiceOptionSerializer(many=True)
    parameter_policies = WorkflowProjectorChoiceOptionSerializer(many=True)
    artifact_roles = WorkflowProjectorChoiceOptionSerializer(many=True)
    media_kinds = WorkflowProjectorChoiceOptionSerializer(many=True)
    output_kinds = WorkflowProjectorChoiceOptionSerializer(many=True)
    contract_sources = WorkflowProjectorChoiceOptionSerializer(many=True)
    role_selectors = WorkflowProjectorChoiceOptionSerializer(many=True)
    scalar_types = WorkflowProjectorChoiceOptionSerializer(many=True)


class WorkflowProjectorEditorLimitsSerializer(serializers.Serializer):
    priority_min = serializers.IntegerField(min_value=0)
    priority_max = serializers.IntegerField(min_value=0)
    node_count_max = serializers.IntegerField(min_value=0)
    input_equals_max_items = serializers.IntegerField(min_value=0)
    model_selectors_max_items = serializers.IntegerField(min_value=0)
    required_edges_max_items = serializers.IntegerField(min_value=0)
    parameter_policies_max_items = serializers.IntegerField(min_value=0)
    asset_slots_max_items = serializers.IntegerField(min_value=0)


class WorkflowProjectorEditorSectionSerializer(serializers.Serializer):
    key = serializers.CharField()
    label = serializers.CharField()
    description = serializers.CharField()
    fields = WorkflowProjectorEditorFieldSerializer(many=True)


class WorkflowProjectorDefinitionEditorSerializer(serializers.Serializer):
    sections = WorkflowProjectorEditorSectionSerializer(many=True)
    choices = WorkflowProjectorEditorChoiceCatalogSerializer()
    limits = WorkflowProjectorEditorLimitsSerializer()


class WorkflowResolutionSizeSerializer(serializers.Serializer):
    width = serializers.IntegerField(min_value=1)
    height = serializers.IntegerField(min_value=1)


class WorkflowResolutionPresetSerializer(WorkflowResolutionSizeSerializer):
    label = serializers.CharField()


class WorkflowResolutionLimitsSerializer(serializers.Serializer):
    min_dimension = serializers.IntegerField(min_value=1)
    max_dimension = serializers.IntegerField(min_value=1)
    multiple = serializers.IntegerField(min_value=1)
    max_pixels = serializers.IntegerField(min_value=1)
    max_candidates = serializers.IntegerField(min_value=1)


class WorkflowResolutionOptionsSerializer(serializers.Serializer):
    native_adapter = serializers.CharField()
    configured_adapter = serializers.CharField()
    algorithm = serializers.CharField()
    modes = WorkflowProjectorChoiceOptionSerializer(many=True)
    native_sizes = WorkflowResolutionSizeSerializer(many=True)
    presets = WorkflowResolutionPresetSerializer(many=True)
    limits = WorkflowResolutionLimitsSerializer()


class WorkflowProjectorOptionsSerializer(serializers.Serializer):
    image_resolution = WorkflowResolutionOptionsSerializer()
    definition_schema_version = serializers.IntegerField(min_value=1)
    editor_schema_version = serializers.IntegerField(min_value=1)
    engines = WorkflowProjectorEngineOptionSerializer(many=True)
    provider_driver_types = WorkflowProjectorChoiceOptionSerializer(many=True)
    operations = WorkflowProjectorChoiceOptionSerializer(many=True)
    output_kinds = WorkflowProjectorChoiceOptionSerializer(many=True)
    adapters = WorkflowProjectorAdapterOptionsSerializer()
    definition_editor = WorkflowProjectorDefinitionEditorSerializer()


class WorkflowProjectorRecommendationInputSerializer(serializers.Serializer):
    candidate_source_id = serializers.UUIDField()
    expected_source_scope_schema_version = serializers.IntegerField(min_value=1)
    expected_source_scope_sha256 = serializers.RegexField(r"^[0-9a-f]{64}$")
    expected_source_sha256 = serializers.RegexField(r"^[0-9a-f]{64}$")
    primary_output_node_id = serializers.CharField(max_length=256)
    output_kind = serializers.CharField(max_length=20)

    def validate_output_kind(self, value):
        allowed = {item for item, _label in WorkflowTemplate.OutputKind.choices}
        if value not in allowed:
            raise serializers.ValidationError("产物类型必须是 image/video/audio/text")
        return value


class WorkflowProjectorRecommendationEvidenceSerializer(serializers.Serializer):
    candidate_source_id = serializers.UUIDField()
    source_scope_schema_version = serializers.IntegerField(min_value=1)
    source_scope_sha256 = serializers.RegexField(r"^[0-9a-f]{64}$")
    source_sha256 = serializers.RegexField(r"^[0-9a-f]{64}$")
    source_path_sha256 = serializers.RegexField(r"^[0-9a-f]{64}$")
    object_info_schema_sha256 = serializers.RegexField(r"^[0-9a-f]{64}$")
    converted_graph_sha256 = serializers.RegexField(r"^[0-9a-f]{64}$")
    primary_canvas_node_id = serializers.CharField()
    primary_api_node_id = serializers.CharField()
    primary_node_type = serializers.CharField()


class WorkflowProjectorRecommendationNodeTypeSerializer(serializers.Serializer):
    value = serializers.CharField()
    count = serializers.IntegerField(min_value=1)
    primary_output = serializers.BooleanField()


class WorkflowProjectorRecommendationEdgeSerializer(serializers.Serializer):
    from_node_type = serializers.CharField()
    output_index = serializers.IntegerField(min_value=0)
    to_node_type = serializers.CharField()
    input_name = serializers.CharField()
    recommended = serializers.BooleanField()


class WorkflowProjectorRecommendationParameterTargetSerializer(serializers.Serializer):
    semantic_key = serializers.CharField()
    node_type = serializers.CharField()
    input_name = serializers.CharField()
    value_type = serializers.CharField(allow_blank=True)


class WorkflowProjectorRecommendationCatalogSerializer(serializers.Serializer):
    node_types = WorkflowProjectorRecommendationNodeTypeSerializer(many=True)
    inputs_by_node_type = serializers.DictField(
        child=serializers.ListField(child=serializers.CharField()),
    )
    observed_edges = WorkflowProjectorRecommendationEdgeSerializer(many=True)
    parameter_targets = WorkflowProjectorRecommendationParameterTargetSerializer(many=True)


class WorkflowProjectorRecommendationSerializer(serializers.Serializer):
    schema_version = serializers.IntegerField(min_value=1)
    persistence = serializers.ChoiceField(choices=("unsaved_draft",))
    requires_human_confirmation = serializers.BooleanField()
    engine_key = serializers.CharField()
    engine_version = serializers.IntegerField(min_value=1)
    provider_driver_type = serializers.CharField()
    operation = serializers.CharField()
    output_kind = serializers.CharField()
    definition = serializers.JSONField()
    source_evidence = WorkflowProjectorRecommendationEvidenceSerializer()
    catalog = WorkflowProjectorRecommendationCatalogSerializer()
    warnings = serializers.ListField(child=serializers.CharField())
    recommendation_sha256 = serializers.RegexField(r"^[0-9a-f]{64}$")


class ComfySavedWorkflowPreviewInputSerializer(serializers.Serializer):
    source_path = serializers.CharField(
        max_length=500,
        help_text="必须先以 import_kind=candidate 纳管的精确来源路径。",
    )
    expected_sha256 = serializers.RegexField(r"^[0-9a-f]{64}$")
    expected_source_scope_schema_version = serializers.IntegerField(min_value=1)
    expected_source_scope_sha256 = serializers.RegexField(r"^[0-9a-f]{64}$")
    primary_output_node_ids = serializers.ListField(
        child=serializers.RegexField(r"^(0|[1-9][0-9]{0,17})$"),
        required=False,
        default=list,
        max_length=8,
        help_text=(
            "留空只返回主输出候选；选择一个主输出时会创建或复用不可变的"
            " ComfyWorkflowContractDraft。"
        ),
    )
    model_profile_version = serializers.PrimaryKeyRelatedField(
        queryset=ModelProfileVersion.objects.filter(status__in=(
            ModelProfileVersion.Status.DRAFT,
            ModelProfileVersion.Status.PUBLISHED,
        )),
        required=False,
        allow_null=True,
    )


class ComfyDraftArtifactAttestationInputSerializer(serializers.Serializer):
    source_path = serializers.CharField(max_length=500)
    expected_sha256 = serializers.RegexField(r"^[0-9a-f]{64}$")
    expected_source_scope_schema_version = serializers.IntegerField(min_value=1)
    expected_source_scope_sha256 = serializers.RegexField(r"^[0-9a-f]{64}$")
    primary_output_node_ids = serializers.ListField(
        child=serializers.RegexField(r"^(0|[1-9][0-9]{0,17})$"),
        min_length=1,
        max_length=1,
    )
    expected_object_info_schema_sha256 = serializers.RegexField(r"^[0-9a-f]{64}$")
    expected_converted_graph_sha256 = serializers.RegexField(r"^[0-9a-f]{64}$")
    model_profile_version = serializers.PrimaryKeyRelatedField(
        queryset=ModelProfileVersion.objects.filter(status=ModelProfileVersion.Status.DRAFT),
    )


class ComfyNewWorkflowTemplateSerializer(serializers.Serializer):
    name = serializers.CharField(max_length=160)
    slug = serializers.RegexField(r"^[a-z][a-z0-9-]{1,99}$")
    output_kind = serializers.ChoiceField(choices=WorkflowTemplate.OutputKind.choices)
    description = serializers.CharField(required=False, allow_blank=True, max_length=2000)


class ComfySavedWorkflowBatchItemSerializer(serializers.Serializer):
    source_path = serializers.CharField(max_length=500)
    expected_sha256 = serializers.RegexField(r"^[0-9a-f]{64}$")
    expected_source_scope_schema_version = serializers.IntegerField(min_value=1)
    expected_source_scope_sha256 = serializers.RegexField(r"^[0-9a-f]{64}$")
    import_kind = serializers.ChoiceField(choices=(
        "candidate", "template_version", "execution_snapshot", "ignored",
    ), help_text=(
        "candidate 只冻结安全来源；template_version 必须消费既有合同草稿，"
        "且不会重新读取本地工作流。"
    ))
    primary_output_node_ids = serializers.ListField(
        child=serializers.RegexField(r"^(0|[1-9][0-9]{0,17})$"),
        required=False,
        default=list,
        max_length=8,
    )
    expected_object_info_schema_sha256 = serializers.RegexField(
        r"^[0-9a-f]{64}$", required=False, allow_blank=True, default="",
    )
    expected_converted_graph_sha256 = serializers.RegexField(
        r"^[0-9a-f]{64}$", required=False, allow_blank=True, default="",
    )
    expected_parameter_schema_sha256 = serializers.RegexField(
        r"^[0-9a-f]{64}$", required=False, allow_blank=True, default="",
    )
    expected_structural_contract_sha256 = serializers.RegexField(
        r"^[0-9a-f]{64}$", required=False, allow_blank=True, default="",
    )
    contract_draft = serializers.PrimaryKeyRelatedField(
        queryset=ComfyWorkflowContractDraft.objects.select_related(
            "candidate_source", "model_profile_version", "projector_version",
        ),
        required=False,
        allow_null=True,
        help_text=(
            "仅 template_version 必填；必须是该 candidate、模型版本和主输出"
            "生成的不可变平台执行合同草稿。"
        ),
    )
    template = serializers.PrimaryKeyRelatedField(
        queryset=WorkflowTemplate.objects.filter(is_active=True, provider="comfyui"),
        required=False,
        allow_null=True,
    )
    new_template = ComfyNewWorkflowTemplateSerializer(required=False, allow_null=True)
    model_profile_version = serializers.PrimaryKeyRelatedField(
        queryset=ModelProfileVersion.objects.filter(
            status__in=(
                ModelProfileVersion.Status.DRAFT,
                ModelProfileVersion.Status.PUBLISHED,
            ),
        ),
        required=False,
        allow_null=True,
    )
    def validate(self, attrs):
        if attrs["import_kind"] == "template_version":
            if bool(attrs.get("template")) == bool(attrs.get("new_template")):
                raise serializers.ValidationError(
                    "template_version 必须且只能选择现有 template 或填写 new_template",
                )
            if attrs.get("model_profile_version") is None:
                raise serializers.ValidationError(
                    {"model_profile_version": "template_version 必须绑定可审阅的 model profile"},
                )
            if attrs.get("contract_draft") is None:
                raise serializers.ValidationError({
                    "contract_draft": "template_version 必须引用已生成的平台执行合同草稿",
                })
            if not attrs.get("primary_output_node_ids"):
                raise serializers.ValidationError(
                    {"primary_output_node_ids": "template_version 必须显式选择主输出节点"},
                )
            if len(attrs["primary_output_node_ids"]) != 1:
                raise serializers.ValidationError({
                    "primary_output_node_ids": "template_version 必须且只能选择一个主输出节点",
                })
            if not attrs.get("expected_object_info_schema_sha256"):
                raise serializers.ValidationError({
                    "expected_object_info_schema_sha256": "template_version 必须提交预览返回的 object_info schema hash",
                })
            if not attrs.get("expected_converted_graph_sha256"):
                raise serializers.ValidationError({
                    "expected_converted_graph_sha256": "template_version 必须提交预览返回的 converted graph hash",
                })
            if not attrs.get("expected_parameter_schema_sha256"):
                raise serializers.ValidationError({
                    "expected_parameter_schema_sha256": (
                        "template_version 必须提交预览返回的 parameter schema hash"
                    ),
                })
            if not attrs.get("expected_structural_contract_sha256"):
                raise serializers.ValidationError({
                    "expected_structural_contract_sha256": (
                        "template_version 必须提交预览返回的 structural contract hash"
                    ),
                })
        return attrs


class ComfySavedWorkflowBatchItemsField(serializers.ListField):
    """Nested serializer list whose request budget is part of OpenAPI."""


class ComfySavedWorkflowBatchItemsFieldSchema(OpenApiSerializerFieldExtension):
    target_class = "studio.platform_serializers.ComfySavedWorkflowBatchItemsField"

    def map_serializer_field(self, auto_schema, direction):
        component = auto_schema.resolve_serializer(self.target.child, direction)
        if component is None:
            return None
        schema = {"type": "array", "items": component.ref}
        if self.target.min_length is not None:
            schema["minItems"] = self.target.min_length
        if self.target.max_length is not None:
            schema["maxItems"] = self.target.max_length
        return schema


class ComfySavedWorkflowBatchInputSerializer(serializers.Serializer):
    items = ComfySavedWorkflowBatchItemsField(
        child=ComfySavedWorkflowBatchItemSerializer(),
        min_length=1,
    )


class ComfySavedWorkflowScanItemSerializer(serializers.Serializer):
    source_path = serializers.CharField()
    source_sha256 = serializers.CharField()
    size_bytes = serializers.IntegerField()
    source_workflow_id = serializers.CharField()
    suggested_import_kind = serializers.CharField()
    candidate_managed = serializers.BooleanField()
    candidate_source_id = serializers.CharField()
    imported = serializers.BooleanField()
    source_id = serializers.CharField()
    workflow_version_id = serializers.CharField()
    template_id = serializers.CharField()
    contract_upgrade_required = serializers.BooleanField()
    error_code = serializers.CharField()
    error_message = serializers.CharField()


class ComfySavedWorkflowEffectiveLimitsSerializer(serializers.Serializer):
    scan_max_items = serializers.IntegerField(min_value=1, read_only=True)
    scan_max_seconds = serializers.FloatField(min_value=0.001, read_only=True)
    request_max_bytes = serializers.IntegerField(min_value=1, read_only=True)
    batch_max_items = serializers.IntegerField(min_value=1, read_only=True)
    batch_max_seconds = serializers.FloatField(min_value=0.001, read_only=True)
    saved_workflow_directory = serializers.CharField(read_only=True)


class ComfySavedWorkflowDiscoverySourceSerializer(serializers.Serializer):
    source_scope_schema_version = serializers.IntegerField(min_value=1)
    source_scope_sha256 = serializers.CharField(allow_blank=True)
    source_deployment_id = serializers.UUIDField()
    provider_id = serializers.UUIDField()
    runtime_host_id = serializers.CharField(allow_blank=True)
    source_label = serializers.CharField()
    deployment_count = serializers.IntegerField(min_value=1)
    status = serializers.ChoiceField(choices=("succeeded", "failed"))
    item_count = serializers.IntegerField(min_value=0)
    error_code = serializers.CharField(allow_blank=True)
    error_message = serializers.CharField(allow_blank=True)


class ComfySavedWorkflowOutputCandidateSerializer(serializers.Serializer):
    canvas_node_id = serializers.CharField()
    node_type = serializers.CharField()
    suggested_output_kind = serializers.ChoiceField(
        choices=(
            ("", "待 Projector 解析"),
            *WorkflowTemplate.OutputKind.choices,
        ),
    )


class ComfySavedWorkflowMatchActionSerializer(serializers.Serializer):
    code = serializers.CharField(help_text="恢复动作；review_workflow_contract 表示已有模型候选仍受结构/尺寸合同阻断，不应重复登记模型。")


class ComfySavedWorkflowModelProfileCandidateSerializer(serializers.Serializer):
    model_profile_version_id = serializers.UUIDField()
    profile_key = serializers.CharField()
    version = serializers.IntegerField(min_value=1)
    status = serializers.ChoiceField(choices=("draft", "published", "retired"))
    modality = serializers.CharField()
    target_deployment_id = serializers.CharField(allow_blank=True)
    eligible = serializers.BooleanField()
    live_validate_ready = serializers.BooleanField()
    eligible_output_node_ids = serializers.ListField(child=serializers.CharField())
    rejection_codes = serializers.ListField(child=serializers.CharField())
    live_validate_rejection_codes = serializers.ListField(
        child=serializers.CharField(),
    )


class ComfySavedWorkflowTemplateSuggestionSerializer(serializers.Serializer):
    use_existing = serializers.BooleanField()
    template_id = serializers.CharField(allow_blank=True)
    name = serializers.CharField()
    slug = serializers.CharField()
    output_kind = serializers.ChoiceField(
        choices=WorkflowTemplate.OutputKind.choices,
    )


class ComfySavedWorkflowParameterTargetSerializer(serializers.Serializer):
    node_id = serializers.CharField()
    node_type = serializers.CharField()
    input_name = serializers.CharField()


class ComfySavedWorkflowParameterScalarField(serializers.JSONField):
    """A JSON scalar; mappings/lists are never valid typed control values."""

    @staticmethod
    def _is_scalar(value) -> bool:
        return (
            isinstance(value, (str, bool, int, float))
            and not (isinstance(value, float) and not math.isfinite(value))
        )

    def to_representation(self, value):
        if value is None and self.allow_null:
            return None
        if not self._is_scalar(value):
            raise serializers.ValidationError("typed smoke parameter 必须是 JSON scalar")
        return value

    def to_internal_value(self, data):
        value = super().to_internal_value(data)
        if value is None and self.allow_null:
            return None
        if not self._is_scalar(value):
            raise serializers.ValidationError("typed smoke parameter 必须是 JSON scalar")
        return value


class ComfySavedWorkflowParameterScalarFieldSchema(OpenApiSerializerFieldExtension):
    target_class = (
        "studio.platform_serializers.ComfySavedWorkflowParameterScalarField"
    )

    def map_serializer_field(self, auto_schema, direction):
        schema = {"oneOf": [
            {"type": "string"},
            {"type": "integer"},
            {"type": "number"},
            {"type": "boolean"},
        ]}
        if self.target.allow_null:
            schema["nullable"] = True
        return schema


class ComfySavedWorkflowConfigurableParameterSerializer(serializers.Serializer):
    semantic_key = serializers.CharField()
    source_value = ComfySavedWorkflowParameterScalarField()
    recommended_safe_value = ComfySavedWorkflowParameterScalarField()
    default = ComfySavedWorkflowParameterScalarField(allow_null=True)
    min = serializers.FloatField(allow_null=True)
    max = serializers.FloatField(allow_null=True)
    schema_min = serializers.FloatField(allow_null=True)
    schema_max = serializers.FloatField(allow_null=True)
    multiple_of = serializers.FloatField(allow_null=True)
    alignment_modulus = serializers.IntegerField(allow_null=True, min_value=1)
    alignment_remainder = serializers.IntegerField(allow_null=True, min_value=0)
    options = serializers.ListField(child=ComfySavedWorkflowParameterScalarField())
    type = serializers.ChoiceField(choices=("integer", "number", "enum"))
    selection_group = serializers.CharField(allow_null=True, allow_blank=False)
    target = ComfySavedWorkflowParameterTargetSerializer()


class ComfySavedWorkflowPreviewCoreSerializer(serializers.Serializer):
    schema_version = serializers.IntegerField()
    source_path = serializers.CharField()
    source_sha256 = serializers.CharField()
    size_bytes = serializers.IntegerField()
    source_workflow_id = serializers.CharField()
    primary_output_node_ids = serializers.ListField(child=serializers.CharField())
    primary_api_node_ids = serializers.ListField(child=serializers.CharField())
    output_candidates = ComfySavedWorkflowOutputCandidateSerializer(many=True)
    model_dependencies = serializers.ListField(child=serializers.DictField())
    asset_dependencies = serializers.ListField(child=serializers.DictField())
    conversion_report = serializers.JSONField()
    object_info_schema_sha256 = serializers.CharField()
    converted_graph_sha256 = serializers.CharField(allow_blank=True)
    configurable_parameters = ComfySavedWorkflowConfigurableParameterSerializer(many=True)
    parameter_schema_sha256 = serializers.CharField(allow_blank=True)
    structural_contract_sha256 = serializers.CharField(allow_blank=True)
    suggested_input_contract = serializers.JSONField(help_text="服务端投影的版本合同。source_image_derived 尺寸策略冻结受审 adapter、主输出尺寸路径和唯一来源素材语义；不包含具体运行素材或可编辑宽高。")
    suggested_semantic_bindings = serializers.ListField(child=serializers.DictField())
    suggested_model_requirements = serializers.ListField(child=serializers.DictField())
    blockers = serializers.ListField(child=serializers.DictField())
    live_validate_requirements = serializers.ListField(child=serializers.DictField())
    admission_tier = serializers.ChoiceField(
        choices=("candidate", "generic_fixed", "projected_dynamic"),
    )
    actionability = serializers.ChoiceField(
        choices=("select_primary_output", "candidate_only", "ready_for_executable_import"),
    )
    recovery_actions = serializers.ListField(child=serializers.DictField())
    contract_draft_id = serializers.CharField(allow_blank=True)
    contract_draft_revision = serializers.IntegerField(min_value=0)


class ComfySavedWorkflowPreviewResponseSerializer(ComfySavedWorkflowPreviewCoreSerializer):
    effective_limits = ComfySavedWorkflowEffectiveLimitsSerializer()


class ComfySavedWorkflowDiscoveryItemSerializer(ComfySavedWorkflowScanItemSerializer):
    source_scope_schema_version = serializers.IntegerField(min_value=1)
    source_scope_sha256 = serializers.CharField(allow_blank=True)
    source_key = serializers.CharField()
    source_deployment_id = serializers.UUIDField()
    source_provider_id = serializers.UUIDField()
    source_runtime_host_id = serializers.CharField(allow_blank=True)
    analysis_status = serializers.ChoiceField(
        choices=("complete", "blocked", "failed"),
    )
    analysis_code = serializers.CharField(allow_blank=True)
    analysis_message = serializers.CharField(allow_blank=True)
    admission_tier = serializers.ChoiceField(
        choices=("candidate", "generic_fixed", "projected_dynamic"),
    )
    actionability = serializers.ChoiceField(
        choices=("candidate_only", "ready_for_executable_import"),
    )
    recovery_actions = serializers.ListField(child=serializers.DictField())
    match_status = serializers.ChoiceField(
        choices=("matched", "ambiguous", "unmatched", "blocked"),
    )
    match_reason_codes = serializers.ListField(child=serializers.CharField())
    match_actions = ComfySavedWorkflowMatchActionSerializer(many=True)
    model_profile_candidates = ComfySavedWorkflowModelProfileCandidateSerializer(many=True)
    matched_model_profile_version_id = serializers.CharField(allow_blank=True)
    matched_target_deployment_id = serializers.CharField(allow_blank=True)
    matched_primary_output_node_id = serializers.CharField(allow_blank=True)
    matched_output_kind = serializers.CharField(allow_blank=True)
    suggested_template = ComfySavedWorkflowTemplateSuggestionSerializer(allow_null=True)
    preview = ComfySavedWorkflowPreviewCoreSerializer(allow_null=True)
    output_candidates = ComfySavedWorkflowOutputCandidateSerializer(
        many=True,
        required=False,
        default=list,
    )


class ComfySavedWorkflowDiscoveryResponseSerializer(serializers.Serializer):
    schema_version = serializers.IntegerField()
    status = serializers.ChoiceField(choices=("complete", "partial", "failed"))
    source_count = serializers.IntegerField(min_value=0)
    succeeded_source_count = serializers.IntegerField(min_value=0)
    failed_source_count = serializers.IntegerField(min_value=0)
    matched_count = serializers.IntegerField(min_value=0)
    review_count = serializers.IntegerField(min_value=0)
    sources = ComfySavedWorkflowDiscoverySourceSerializer(many=True)
    items = ComfySavedWorkflowDiscoveryItemSerializer(many=True)
    effective_limits = ComfySavedWorkflowEffectiveLimitsSerializer()


class ComfySavedWorkflowBatchResultItemSerializer(serializers.Serializer):
    source_path = serializers.CharField()
    source_sha256 = serializers.CharField()
    source_id = serializers.CharField()
    import_kind = serializers.CharField()
    result = serializers.ChoiceField(choices=("created", "reused", "failed"))
    code = serializers.CharField()
    detail = serializers.CharField()
    workflow_version_id = serializers.CharField()
    structural_contract_sha256 = serializers.CharField(allow_blank=True)


class ComfySavedWorkflowBatchResponseSerializer(serializers.Serializer):
    schema_version = serializers.IntegerField()
    deployment_id = serializers.UUIDField()
    status = serializers.ChoiceField(choices=("complete", "partial", "failed"))
    created_count = serializers.IntegerField()
    reused_count = serializers.IntegerField()
    failed_count = serializers.IntegerField()
    items = ComfySavedWorkflowBatchResultItemSerializer(many=True)


class ComfyWorkflowSourceSerializer(serializers.ModelSerializer):
    class Meta:
        model = ComfyWorkflowSource
        fields = (
            "id", "source_deployment", "source_scope_schema_version",
            "source_scope_sha256", "source_path", "source_path_sha256", "source_sha256",
            "source_size_bytes", "source_workflow_id", "source_document_schema_version",
            "source_document_size_bytes", "source_document_sha256",
            "import_contract_sha256", "import_kind",
            "converted_graph_sha256", "conversion_report",
            "conversion_report_schema_version", "conversion_report_size_bytes",
            "conversion_report_sha256", "workflow_version", "generated_workflow",
            "generation_task", "imported_by", "created_at",
        )
        read_only_fields = fields


class ComfyWorkflowContractDraftProjectionSummarySerializer(serializers.Serializer):
    schema_version = serializers.IntegerField(allow_null=True)
    source_sha256 = serializers.CharField(allow_blank=True)
    primary_output_node_ids = serializers.ListField(child=serializers.CharField())
    primary_api_node_ids = serializers.ListField(child=serializers.CharField())
    executable_graph_sha256 = serializers.CharField(allow_blank=True)
    executable_node_count = serializers.IntegerField(min_value=0)
    model_dependency_count = serializers.IntegerField(min_value=0)
    asset_dependency_count = serializers.IntegerField(min_value=0)
    blockers = serializers.ListField(child=serializers.DictField())
    live_validate_requirements = serializers.ListField(child=serializers.DictField())
    recovery_actions = serializers.ListField(child=serializers.DictField())


class ComfyWorkflowContractDraftSerializer(serializers.ModelSerializer):
    projector_key = serializers.CharField(
        source="projector_version.projector.key",
        read_only=True,
        allow_null=True,
    )
    projection_summary = serializers.SerializerMethodField()

    @extend_schema_field(ComfyWorkflowContractDraftProjectionSummarySerializer())
    def get_projection_summary(self, instance):
        projection = instance.projection if isinstance(instance.projection, dict) else {}
        executable_graph = projection.get("executable_graph")
        return {
            "schema_version": projection.get("schema_version"),
            "source_sha256": str(projection.get("source_sha256") or ""),
            "primary_output_node_ids": list(
                projection.get("primary_output_node_ids") or [],
            ),
            "primary_api_node_ids": list(
                projection.get("primary_api_node_ids") or [],
            ),
            "executable_graph_sha256": str(
                projection.get("executable_graph_sha256") or "",
            ),
            "executable_node_count": (
                len(executable_graph) if isinstance(executable_graph, dict) else 0
            ),
            "model_dependency_count": len(projection.get("model_dependencies") or []),
            "asset_dependency_count": len(projection.get("asset_dependencies") or []),
            "blockers": list(projection.get("blockers") or []),
            "live_validate_requirements": list(
                projection.get("live_validate_requirements") or [],
            ),
            "recovery_actions": list(projection.get("recovery_actions") or []),
        }

    class Meta:
        model = ComfyWorkflowContractDraft
        fields = (
            "id", "candidate_source", "revision", "primary_output_node_id",
            "model_profile_version", "projector_version", "projector_key",
            "object_info_schema_sha256", "converted_graph_sha256",
            "structural_contract_sha256", "projection_summary",
            "projection_schema_version",
            "projection_size_bytes", "projection_sha256", "admission_tier",
            "actionability", "created_by", "created_at",
        )
        read_only_fields = fields


class DeploymentActionSerializer(serializers.Serializer):
    deployment_id = serializers.UUIDField()


class DeploymentProbeInputSerializer(serializers.Serializer):
    prompt_profile_binding_id = serializers.UUIDField(required=False)
    operation = serializers.ChoiceField(
        choices=(
            "image.text_to_image",
            "video.text_to_video",
            "audio.text_to_audio",
            "prompt.normalize",
            "script.generate",
            "script.structure",
            "visual_rules.generate",
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
    prompt_profile = serializers.SerializerMethodField()

    def get_prompt_profile(self, obj) -> dict | None:
        return obj.request_snapshot.get("prompt_profile")

    class Meta:
        model = ModelDeploymentSmokeAttempt
        fields = (
            "id", "deployment", "operation", "status", "request_sha256",
            "prompt_profile",
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


class WorkflowSmokeFixtureAssetInputSerializer(serializers.Serializer):
    semantic_key = serializers.CharField(max_length=120)
    asset_id = serializers.UUIDField()


class WorkflowSmokeReferencePictureInputSerializer(serializers.Serializer):
    asset_id = serializers.UUIDField()
    category = serializers.ChoiceField(
        choices=("character", "scene", "prop", "continuity_frame"),
    )


class WorkflowSmokeReferenceVideoInputSerializer(serializers.Serializer):
    asset_id = serializers.UUIDField()
    purpose = serializers.RegexField(SAFE_CONDITIONING_IDENTIFIER_PATTERN)
    use_embedded_audio = serializers.BooleanField()
    audio_asset_id = serializers.UUIDField(required=False, allow_null=True)
    audio_purpose = serializers.RegexField(
        SAFE_CONDITIONING_IDENTIFIER_PATTERN,
        required=False,
    )


class WorkflowSmokeReferenceAudioInputSerializer(serializers.Serializer):
    asset_id = serializers.UUIDField()
    purpose = serializers.RegexField(SAFE_CONDITIONING_IDENTIFIER_PATTERN)


class WorkflowSmokeDynamicReferencesInputSerializer(serializers.Serializer):
    pictures = WorkflowSmokeReferencePictureInputSerializer(
        many=True, required=False, default=list,
    )
    videos = WorkflowSmokeReferenceVideoInputSerializer(
        many=True, required=False, default=list,
    )
    audios = WorkflowSmokeReferenceAudioInputSerializer(
        many=True, required=False, default=list,
    )


class WorkflowExecutionInputSerializer(serializers.Serializer):
    schema_version = serializers.IntegerField(min_value=2, max_value=2, default=2)
    values = serializers.DictField(
        child=ComfySavedWorkflowParameterScalarField(),
        required=False,
        default=dict,
    )
    assets = WorkflowSmokeFixtureAssetInputSerializer(
        many=True,
        required=False,
        default=list,
    )
    references = WorkflowSmokeDynamicReferencesInputSerializer(
        required=False,
        default=dict,
    )

class WorkflowSmokeActionSerializer(serializers.Serializer):
    deployment_id = serializers.UUIDField()
    fixture = WorkflowExecutionInputSerializer()


class WorkflowSmokeAssetBindingSerializer(serializers.ModelSerializer):
    class Meta:
        model = WorkflowSmokeAssetBinding
        fields = (
            "semantic_key", "input_slot", "asset", "media_kind", "category",
            "checksum", "mime_type", "size_bytes", "created_at",
        )
        read_only_fields = fields


class WorkflowSmokeResolutionInputSerializer(serializers.Serializer):
    outcome = serializers.ChoiceField(
        choices=WorkflowSmokeAttemptResolution.Outcome.choices,
    )
    note = serializers.CharField(trim_whitespace=True)
    provider_request_id = serializers.CharField(
        required=False,
        allow_blank=True,
        max_length=191,
        default="",
        write_only=True,
    )

    def validate_note(self, value):
        try:
            return normalize_audit_note(value)
        except DjangoValidationError as exc:
            _raise_drf(exc)


class WorkflowSmokeResolutionSerializer(serializers.ModelSerializer):
    workflow_smoke_attempt_id = serializers.UUIDField(source="attempt_id", read_only=True)

    class Meta:
        model = WorkflowSmokeAttemptResolution
        fields = (
            "id", "workflow_smoke_attempt_id", "revision", "outcome",
            "provider_request_id_sha256", "evidence", "evidence_schema_version",
            "evidence_size_bytes", "evidence_sha256", "note", "decided_by",
            "created_at",
        )
        read_only_fields = fields


class WorkflowSmokeAttemptSerializer(serializers.ModelSerializer):
    asset_bindings = WorkflowSmokeAssetBindingSerializer(many=True, read_only=True)
    resolutions = WorkflowSmokeResolutionSerializer(many=True, read_only=True)
    resolution_required = serializers.SerializerMethodField()

    def get_resolution_required(self, obj) -> bool:
        return (
            obj.status == WorkflowSmokeAttempt.Status.UNCERTAIN
            and not obj.resolutions.exists()
        )

    class Meta:
        model = WorkflowSmokeAttempt
        fields = (
            "id", "workflow_version", "deployment", "created_by", "status",
            "graph_sha256", "execution_contract_sha256", "validation_evidence_sha256",
            "target_sha256", "fixture_values_schema_version",
            "fixture_values_size_bytes", "fixture_values_sha256", "fixture_sha256",
            "asset_bindings", "resolutions", "resolution_required",
            "worker_id", "claimed_at", "heartbeat_at",
            "submission_started_at", "submitted_at", "last_polled_at", "next_poll_at",
            "poll_count", "remote_status", "deadline_started_at", "hard_deadline_at",
            "idle_deadline_at", "last_meaningful_progress_at", "cancel_requested_at",
            "cancel_submitted_at", "finished_at", "error_code", "error_message",
            "result", "created_at", "updated_at",
        )
        read_only_fields = fields


CONFIG_DEFAULT_SOURCE_CHOICES = (
    ("model", "模型版本 / Model"),
    ("workflow", "工作流版本 / Workflow"),
)
CONFIG_DEFAULT_SOURCE_ORDER = tuple(
    value for value, _label in CONFIG_DEFAULT_SOURCE_CHOICES
)


def prompt_field_system_defaults() -> dict:
    return {
        "default_value": PromptFieldDefinition._meta.get_field(
            "default_value",
        ).get_default(),
        "value_options": PromptFieldDefinition._meta.get_field(
            "value_options",
        ).get_default(),
        "is_required": PromptFieldDefinition._meta.get_field(
            "is_required",
        ).get_default(),
        "is_active": PromptFieldDefinition._meta.get_field(
            "is_active",
        ).get_default(),
        "display_order": "append",
    }


def prompt_rule_system_defaults() -> dict:
    field_names = (
        "label_style",
        "separator",
        "required",
        "omit_if_empty",
        "max_chars",
        "generation_output_semantics",
        "generation_required",
        "language_policy",
        "length_unit",
        "generation_validator_keys",
        "generation_context_selectors",
    )
    return {
        name: PromptProfileFieldRule._meta.get_field(name).get_default()
        for name in field_names
    }


def config_definition_system_defaults() -> dict:
    field_names = (
        "configuration_class",
        "allowed_scopes",
        "allowed_default_sources",
        "restart_required",
        "is_required",
        "is_secret",
        "is_active",
        "needs_review",
    )
    return {
        name: ConfigDefinition._meta.get_field(name).get_default()
        for name in field_names
    }


def _canonical_csv_choices(value, *, allowed, field_name):
    raw_items = [item.strip() for item in str(value or "").split(",") if item.strip()]
    unknown = sorted(set(raw_items) - set(allowed))
    if unknown:
        raise serializers.ValidationError({
            field_name: f"包含未登记选项: {', '.join(unknown)}",
        })
    return ",".join(item for item in allowed if item in set(raw_items))


class ConfigDefinitionSerializer(serializers.ModelSerializer):
    has_values = serializers.SerializerMethodField()
    mutable_fields = serializers.SerializerMethodField()

    class Meta:
        model = ConfigDefinition
        fields = "__all__"
        read_only_fields = (
            "value_schema_version", "value_schema_size_bytes", "value_schema_sha256",
            "initial_value_schema_version", "initial_value_size_bytes",
            "initial_value_sha256", "has_values", "mutable_fields",
            "created_at", "updated_at",
        )

    def get_has_values(self, obj) -> bool:
        count = getattr(obj, "config_value_count", None)
        return bool(count) if count is not None else obj.values.exists()

    def get_mutable_fields(self, obj) -> list[str]:
        if self.get_has_values(obj):
            return [
                "description",
                "unit",
                "impact_modules",
                "is_active",
                "needs_review",
                "review_reason",
            ]
        return [
            "domain",
            "value_type",
            "description",
            "unit",
            "impact_modules",
            "restart_required",
            "allowed_scopes",
            "allowed_default_sources",
            "is_required",
            "is_secret",
            "is_active",
            "needs_review",
            "review_reason",
            "value_schema",
            "initial_value",
        ]

    def validate(self, attrs):
        request = self.context.get("request")
        actor = getattr(request, "user", None)
        current_class = getattr(
            self.instance,
            "configuration_class",
            ConfigDefinition.ConfigurationClass.STANDARD,
        )
        requested_class = attrs.get("configuration_class", current_class)
        if (
            self.instance
            and "key" in attrs
            and attrs["key"] != self.instance.key
        ):
            raise serializers.ValidationError({
                "key": "配置 key 创建后不可修改；请创建新的受控定义",
            })
        if (
            self.instance
            and "configuration_class" in attrs
            and attrs["configuration_class"] != self.instance.configuration_class
        ):
            raise serializers.ValidationError({
                "configuration_class": "配置类别创建后由所属设置分区锁定；请创建新的受控定义",
            })
        if (
            ConfigDefinition.ConfigurationClass.SECURITY_BOUNDARY
            in {current_class, requested_class}
            and not getattr(actor, "is_superuser", False)
        ):
            raise DRFPermissionDenied(
                "只有超级管理员可以创建或修改安全边界配置定义",
            )
        scope_order = tuple(
            value for value, _label in ConfigRevision.ScopeType.choices
        )
        canonical_scopes = _canonical_csv_choices(
            attrs.get(
                "allowed_scopes",
                getattr(self.instance, "allowed_scopes", ""),
            ),
            allowed=scope_order,
            field_name="allowed_scopes",
        )
        canonical_sources = _canonical_csv_choices(
            attrs.get(
                "allowed_default_sources",
                getattr(self.instance, "allowed_default_sources", ""),
            ),
            allowed=CONFIG_DEFAULT_SOURCE_ORDER,
            field_name="allowed_default_sources",
        )
        if "allowed_scopes" in attrs or not self.instance:
            attrs["allowed_scopes"] = canonical_scopes
        if "allowed_default_sources" in attrs or not self.instance:
            attrs["allowed_default_sources"] = canonical_sources
        impact_modules = attrs.get(
            "impact_modules",
            getattr(self.instance, "impact_modules", []),
        )
        if not isinstance(impact_modules, list) or any(
            not isinstance(item, str) or not item.strip() or len(item.strip()) > 80
            for item in impact_modules
        ):
            raise serializers.ValidationError({
                "impact_modules": "必须是非空短字符串组成的数组",
            })
        canonical_modules = list(dict.fromkeys(
            item.strip() for item in impact_modules
        ))
        if "impact_modules" in attrs or not self.instance:
            attrs["impact_modules"] = canonical_modules
        if self.instance and self.instance.values.exists():
            immutable = {
                "key", "domain", "configuration_class", "value_type",
                "allowed_scopes", "is_required", "is_secret",
                "value_schema", "initial_value", "restart_required",
                "allowed_default_sources",
            }
            def persisted_contract_value(field):
                value = getattr(self.instance, field)
                if field == "allowed_scopes":
                    return _canonical_csv_choices(
                        value,
                        allowed=scope_order,
                        field_name=field,
                    )
                if field == "allowed_default_sources":
                    return _canonical_csv_choices(
                        value,
                        allowed=CONFIG_DEFAULT_SOURCE_ORDER,
                        field_name=field,
                    )
                return value

            changed = sorted(
                field for field in immutable
                if field in attrs and attrs[field] != persisted_contract_value(field)
            )
            if changed:
                raise serializers.ValidationError({
                    field: "已有 revision 使用该定义；结构变更请创建新 key。"
                    for field in changed
                })
        if requested_class == ConfigDefinition.ConfigurationClass.SECURITY_BOUNDARY:
            security_contract = {
                "domain": attrs.get("domain", getattr(self.instance, "domain", "")),
                "allowed_scopes": attrs.get(
                    "allowed_scopes",
                    getattr(self.instance, "allowed_scopes", ""),
                ),
                "allowed_default_sources": attrs.get(
                    "allowed_default_sources",
                    getattr(self.instance, "allowed_default_sources", ""),
                ),
                "is_required": attrs.get(
                    "is_required",
                    getattr(self.instance, "is_required", False),
                ),
                "is_secret": attrs.get(
                    "is_secret",
                    getattr(self.instance, "is_secret", False),
                ),
            }
            errors = {}
            if security_contract["domain"] != "security":
                errors["domain"] = "安全边界配置必须归入 security 域"
            if security_contract["allowed_scopes"] != ConfigRevision.ScopeType.SYSTEM:
                errors["allowed_scopes"] = "安全边界只允许 system scope"
            if security_contract["allowed_default_sources"]:
                errors["allowed_default_sources"] = "安全边界不能由模型或工作流提供默认值"
            if security_contract["is_required"] is not True:
                errors["is_required"] = "安全边界必须是必需配置"
            if security_contract["is_secret"] is not False:
                errors["is_secret"] = "安全边界不承载秘密正文；秘密必须使用受管凭据"
            if errors:
                raise serializers.ValidationError(errors)
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
        allowed_sources = set(canonical_sources.split(",")) if canonical_sources else set()
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
        schema_contract = json_contract(
            schema,
            max_bytes=_stage_snapshot_max_bytes(),
        )
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
            contract = json_contract(
                initial,
                max_bytes=_stage_snapshot_max_bytes(),
            )
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


class ConfigDefinitionCreateInputSerializer(ConfigDefinitionSerializer):
    """Create contract without service-assigned lifecycle defaults."""

    class Meta(ConfigDefinitionSerializer.Meta):
        read_only_fields = (
            *ConfigDefinitionSerializer.Meta.read_only_fields,
            "is_active",
            "needs_review",
            "review_reason",
        )


class ConfigDefinitionOptionsSerializer(serializers.Serializer):
    configuration_classes = FormCatalogChoiceSerializer(many=True, read_only=True)
    value_types = FormCatalogChoiceSerializer(many=True, read_only=True)
    scopes = FormCatalogChoiceSerializer(many=True, read_only=True)
    default_sources = FormCatalogChoiceSerializer(many=True, read_only=True)
    domains = FormCatalogChoiceSerializer(many=True, read_only=True)
    units = FormCatalogChoiceSerializer(many=True, read_only=True)
    impact_modules = FormCatalogChoiceSerializer(many=True, read_only=True)
    custom_entry_fields = serializers.ListField(
        child=serializers.CharField(),
        read_only=True,
    )
    system_defaults = serializers.DictField(read_only=True)


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
    changes_security_boundaries = serializers.SerializerMethodField(
        help_text=(
            "Whether this complete revision changes any security-boundary value "
            "relative to based_on_revision. Carrying identical boundary rows is false."
        ),
    )

    class Meta:
        model = ConfigRevision
        fields = (
            "id", "scope_type", "project", "run", "revision", "status",
            "based_on_revision", "activation_state", "activated_at", "activation_error",
            "activation_available", "changes_security_boundaries",
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

    def get_changes_security_boundaries(self, obj) -> bool:
        return revision_changes_security_boundaries(
            obj,
            baseline=obj.based_on_revision,
        )


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
            managed_keys = set(attrs.get("values", {})) | set(
                attrs.get("unset_keys", ()),
            )
            if ConfigDefinition.objects.filter(
                key__in=managed_keys,
                configuration_class=(
                    ConfigDefinition.ConfigurationClass.SECURITY_BOUNDARY
                ),
            ).exists():
                raise DRFPermissionDenied(
                    "只有超级管理员可以修改安全边界配置",
                )
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


class RegistryDeletionPreviewInputSerializer(serializers.Serializer):
    resource_type = serializers.ChoiceField(choices=(
        "provider_profile",
        "model_profile_version",
        "model_deployment",
        "model_artifact",
        "workflow_template_version",
        "production_recipe_version",
        "config_definition",
        "prompt_field_definition",
        "prompt_profile_version",
        "prompt_profile_field_rule",
        "prompt_profile_binding",
    ))
    object_id = serializers.UUIDField()


class RegistryDeletionExecuteInputSerializer(RegistryDeletionPreviewInputSerializer):
    expected_impact_sha256 = serializers.RegexField(r"^[0-9a-f]{64}$")
    selected_dependency_keys = serializers.ListField(
        child=serializers.RegexField(r"^[0-9a-f]{32}$"),
        required=False,
        default=list,
    )
    confirmation_phrase = serializers.CharField(max_length=500, trim_whitespace=False)


class RegistryDeletionDependencySerializer(serializers.Serializer):
    dependency_key = serializers.RegexField(r"^[0-9a-f]{32}$")
    resource_type = serializers.CharField()
    object_id = serializers.UUIDField()
    label = serializers.CharField()
    status = serializers.CharField()
    relationship = serializers.CharField()
    relationship_label = serializers.CharField()
    disposition = serializers.ChoiceField(choices=(
        "selectable_delete",
        "automatic_delete",
        "blocking_reference",
        "preserved_history",
    ))
    reason_code = serializers.CharField()
    reason = serializers.CharField()
    reference_paths = serializers.ListField(child=serializers.CharField())
    state_sha256 = serializers.RegexField(r"^[0-9a-f]{64}$")


class RegistryDeletionTargetSerializer(serializers.Serializer):
    resource_type = serializers.CharField()
    object_id = serializers.UUIDField()
    label = serializers.CharField()
    status = serializers.CharField()
    state_sha256 = serializers.RegexField(r"^[0-9a-f]{64}$")


class RegistryDeletionTargetPolicySerializer(serializers.Serializer):
    allowed = serializers.BooleanField()
    reason_code = serializers.CharField(allow_blank=True)
    reason = serializers.CharField(allow_blank=True)


class RegistryDeletionImpactSummarySerializer(serializers.Serializer):
    selectable_count = serializers.IntegerField(min_value=0)
    automatic_count = serializers.IntegerField(min_value=0)
    blocking_count = serializers.IntegerField(min_value=0)
    preserved_history_count = serializers.IntegerField(min_value=0)


class RegistryDeletionImpactSerializer(serializers.Serializer):
    schema_version = serializers.IntegerField()
    requested_action = serializers.ChoiceField(choices=("delete",))
    target = RegistryDeletionTargetSerializer()
    target_policy = RegistryDeletionTargetPolicySerializer()
    complete = serializers.BooleanField()
    can_execute = serializers.BooleanField()
    dependencies = RegistryDeletionDependencySerializer(many=True)
    summary = RegistryDeletionImpactSummarySerializer()
    confirmation_phrase = serializers.CharField()
    advisories = serializers.ListField(child=serializers.CharField())
    impact_sha256 = serializers.RegexField(r"^[0-9a-f]{64}$")


class RegistryDeletionExecutedDependencySerializer(serializers.Serializer):
    dependency_key = serializers.RegexField(r"^[0-9a-f]{32}$")
    resource_type = serializers.CharField()
    object_id = serializers.UUIDField()
    disposition = serializers.ChoiceField(choices=(
        "selectable_delete",
        "automatic_delete",
    ))


class RegistryDeletionExecuteResponseSerializer(serializers.Serializer):
    deleted = serializers.BooleanField()
    target = RegistryDeletionTargetSerializer()
    deleted_dependencies = RegistryDeletionExecutedDependencySerializer(many=True)


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
            "id", "host", "created_by", "trigger", "probe_kind", "status", "worker_id",
            "request_snapshot", "request_snapshot_schema_version",
            "request_snapshot_size_bytes", "request_snapshot_sha256",
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
            "id", "project", "source_asset", "source_type", "source_format", "status",
            "original_name", "mime_type", "file_size_bytes", "file_sha256",
            "validation_message", "latest_revision_id", "created_at", "updated_at",
        )
        read_only_fields = fields

    def get_latest_revision_id(self, obj) -> str | None:
        revision = obj.revisions.order_by("-revision").first()
        return str(revision.id) if revision else None


class ScriptSourceCreateInputSerializer(serializers.Serializer):
    project_id = serializers.UUIDField()
    asset_id = serializers.UUIDField(required=False)
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

    def validate(self, attrs):
        supplied = sum((
            bool(attrs.get("asset_id")),
            bool(attrs.get("file")),
            bool(str(attrs.get("content") or "").strip()),
        ))
        if supplied != 1:
            raise serializers.ValidationError(
                "asset_id、file 与 content 必须且只能提供一个",
            )
        return attrs


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
    limit = serializers.IntegerField(min_value=1)
    next_offset = serializers.IntegerField(min_value=0, allow_null=True)
    catalog_sha256 = serializers.RegexField(r"^[0-9a-f]{64}$")
    items = NodeMetadataItemSerializer(many=True)


class NodeMetadataQuerySerializer(serializers.Serializer):
    deployment_id = serializers.UUIDField()
    search = serializers.CharField(required=False, allow_blank=True, default="")
    offset = serializers.IntegerField(required=False, min_value=0, default=0)
    limit = serializers.IntegerField(required=False, min_value=1)

    def validate_search(self, value):
        maximum = boundary_int("security.api.max_search_chars")
        if len(value) > maximum:
            raise serializers.ValidationError(
                f"search 不能超过安全上限 {maximum} 个字符",
            )
        return value

    def validate_limit(self, value):
        maximum = boundary_int("security.api.max_page_size")
        if value > maximum:
            raise serializers.ValidationError(
                f"limit 不能超过安全上限 {maximum}",
            )
        return value


class ProductionRecipeVersionSerializer(serializers.ModelSerializer):
    class Meta:
        model = ProductionRecipeVersion
        fields = "__all__"
        read_only_fields = (
            "version", "definition_schema_version", "definition_size_bytes", "definition_sha256",
            "status", "published_at", "published_by", "created_at", "updated_at",
        )

    def validate(self, attrs):
        if self.instance and self.instance.status in {
            ProductionRecipeVersion.Status.PUBLISHED,
            ProductionRecipeVersion.Status.RETIRED,
        }:
            raise serializers.ValidationError("published/retired recipe version 不可修改")
        if (
            self.instance
            and "recipe_key" in attrs
            and attrs["recipe_key"] != self.instance.recipe_key
        ):
            raise serializers.ValidationError({
                "recipe_key": "Recipe Key 创建后不可修改；请创建精确来源的后继草稿",
            })
        definition = attrs.get("definition", getattr(self.instance, "definition", {}))
        try:
            validate_recipe_registry_references(definition)
        except DjangoValidationError as exc:
            _raise_drf(exc)
        sensitive = find_forbidden_secret_fields(definition)
        if sensitive:
            raise serializers.ValidationError({
                "definition": f"配方合同不能保存明文密钥字段: {', '.join(sensitive)}",
            })
        contract = json_contract(
            definition,
            max_bytes=_stage_snapshot_max_bytes(),
        )
        attrs.update({
            "definition_schema_version": contract.schema_version,
            "definition_size_bytes": contract.size_bytes,
            "definition_sha256": contract.sha256,
        })
        return attrs


class ProductionRecipeCloneSuccessorInputSerializer(serializers.Serializer):
    expected_definition_sha256 = serializers.RegexField(
        regex=r"^[0-9a-f]{64}$",
    )


class ProductionRecipeEditorOptionsSerializer(serializers.Serializer):
    definition_schema_version = serializers.IntegerField(read_only=True)
    stage_options = serializers.ListField(child=serializers.DictField(), read_only=True)
    approval_options = FormCatalogChoiceSerializer(many=True, read_only=True)
    routing_mode_options = FormCatalogChoiceSerializer(many=True, read_only=True)
    presets = serializers.ListField(child=serializers.DictField(), read_only=True)


class ProductionPromptProfileChoiceSerializer(serializers.Serializer):
    binding_id = serializers.UUIDField()
    binding_key = serializers.CharField()
    binding_name = serializers.CharField()
    prompt_profile_version_id = serializers.UUIDField()
    profile_key = serializers.CharField()
    profile_version = serializers.IntegerField(min_value=1)
    profile_name = serializers.CharField()
    contract_sha256 = serializers.RegexField(r"^[0-9a-f]{64}$")
    priority = serializers.IntegerField()
    binding_scope = serializers.ChoiceField(choices=("exact_model", "model_family"))


class ProductionPromptProfileRequirementSerializer(serializers.Serializer):
    operation = serializers.CharField()
    mode = serializers.CharField(allow_blank=True)
    choices = ProductionPromptProfileChoiceSerializer(many=True)


class ProductionRecipeMediaRouteOptionSerializer(serializers.Serializer):
    route_index = serializers.IntegerField(min_value=0)
    workflow_version_id = serializers.UUIDField(allow_null=True)
    workflow_label = serializers.CharField()
    model_deployment_id = serializers.UUIDField(allow_null=True)
    deployment_label = serializers.CharField()
    model_identifier = serializers.CharField(allow_blank=True)
    driver_type = serializers.CharField(allow_blank=True)
    output_kinds = serializers.ListField(child=serializers.CharField())
    conditioning_modes = serializers.ListField(child=serializers.DictField())
    conditioning_contract_sha256 = serializers.CharField(allow_blank=True)
    media_constraints = serializers.DictField(allow_null=True)
    media_constraints_sha256 = serializers.CharField(allow_blank=True)
    media_operations = serializers.ListField(child=serializers.CharField())
    is_selectable = serializers.BooleanField()
    reason_codes = serializers.ListField(child=serializers.CharField())
    reasons = serializers.ListField(child=serializers.CharField())
    prompt_profile_requirements = ProductionPromptProfileRequirementSerializer(many=True)


class ProductionRecipeMediaRouteEffectiveLimitsSerializer(serializers.Serializer):
    max_routing_routes = serializers.IntegerField(min_value=1)


class ProductionRecipeMediaRouteOptionsSerializer(serializers.Serializer):
    schema_version = serializers.IntegerField(min_value=2, max_value=2)
    recipe_version_id = serializers.UUIDField()
    recipe_definition_sha256 = serializers.RegexField(r"^[0-9a-f]{64}$")
    routes = ProductionRecipeMediaRouteOptionSerializer(many=True)
    bible_image_generation = ProductionPromptProfileRequirementSerializer(allow_null=True)
    effective_limits = ProductionRecipeMediaRouteEffectiveLimitsSerializer(
        read_only=True,
    )


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
    "conditioning_contract",
    "conditioning_contract_sha256",
    "model_identifier",
    "driver_type",
    "is_external",
    "effective_timeout_seconds",
    "media_operations",
    "prompt_profile",
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
            # ``redacted`` was already traversed as one tree above. Re-running
            # the redactor per scalar source field would re-resolve the live
            # security boundary thousands of times for a normal full snapshot.
            public_sources[str(key)] = {
                field: source[field]
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
    # Redact the frozen routing contract once, then project its public fields.
    # This keeps the output identical while making work independent of route
    # and nested-field count.
    redacted = redact_sensitive_bindings(value)
    routes = redacted.get("routes") if isinstance(redacted, dict) else None
    public_routes = []
    if isinstance(routes, list):
        for route in routes:
            if not isinstance(route, dict):
                continue
            public_routes.append({
                key: route[key]
                for key in _PUBLIC_ROUTE_FIELDS
                if key in route
            })
    public = {
        key: redacted[key]
        for key in ("schema_version", "mode", "variant_count", "evaluator")
        if key in redacted
    } | {"routes": public_routes}
    if "prompt_profile_selection" in redacted:
        public["prompt_profile_selection"] = redacted["prompt_profile_selection"]
    return public


class ApprovalDecisionFactSerializer(serializers.ModelSerializer):
    class Meta:
        model = ApprovalDecision
        fields = (
            "id", "revision", "decision", "comment", "output_snapshot_sha256",
            "actor", "decision_policy", "decision_policy_sha256", "decided_by",
            "created_at",
        )
        read_only_fields = fields


class StageRunSerializer(serializers.ModelSerializer):
    approval_decisions = serializers.PrimaryKeyRelatedField(many=True, read_only=True)
    approval_decision_facts = ApprovalDecisionFactSerializer(
        source="approval_decisions",
        many=True,
        read_only=True,
    )
    artifact_selection_decisions = ArtifactSelectionDecisionSerializer(many=True, read_only=True)
    route_events = StageRouteEventSerializer(many=True, read_only=True)

    class Meta:
        model = StageRun
        fields = (
            "id", "stage_key", "ordinal", "mode", "status", "approval_required",
            "decision_policy", "decision_policy_sha256",
            "attempt_count", "input_reference", "output_reference", "reason_code",
            "reason", "routing", "routing_sha256", "routing_decision",
            "routing_decision_sha256", "selected_route_index",
            "config_snapshot", "config_snapshot_sha256",
            "input_snapshot", "input_snapshot_sha256",
            "output_snapshot", "output_snapshot_sha256",
            "approved_estimated_cost", "approved_cost_currency",
            "selection_confirmed_at", "started_at", "finished_at", "approval_decisions",
            "approval_decision_facts",
            "artifact_selection_decisions", "route_events",
            "created_at", "updated_at",
        )
        read_only_fields = fields

    def to_representation(self, instance):
        data = super().to_representation(instance)
        snapshot_max_bytes = _stage_snapshot_max_bytes()
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
        data["routing_sha256"] = json_contract(
            routing,
            max_bytes=snapshot_max_bytes,
        ).sha256
        data["routing_decision"] = routing_decision
        data["routing_decision_sha256"] = json_contract(
            routing_decision,
            max_bytes=snapshot_max_bytes,
        ).sha256
        data["config_snapshot"] = safe
        data["config_snapshot_sha256"] = json_contract(
            safe,
            max_bytes=snapshot_max_bytes,
        ).sha256
        data["input_snapshot"] = input_snapshot
        data["input_snapshot_sha256"] = json_contract(
            input_snapshot,
            max_bytes=snapshot_max_bytes,
        ).sha256
        data["output_snapshot"] = output_snapshot
        data["output_snapshot_sha256"] = json_contract(
            output_snapshot,
            max_bytes=snapshot_max_bytes,
        ).sha256
        return data


class GenerationAttemptSerializer(serializers.ModelSerializer):
    provider_request_id_sha256 = serializers.SerializerMethodField()
    provider_request_id_suffix = serializers.SerializerMethodField()
    repair_of_attempt_id = serializers.SerializerMethodField()
    retry_feedback_sha256 = serializers.SerializerMethodField()

    class Meta:
        model = GenerationAttempt
        fields = (
            "id", "stage_run", "unit_key", "attempt_number", "model_deployment",
            "generation_task", "prompt_profile_version", "prompt_profile_binding_key",
            "prompt_profile_contract_sha256", "status", "route_index", "operation", "is_external",
            "provider_request_id_sha256", "provider_request_id_suffix",
            "repair_of_attempt_id", "retry_feedback_sha256",
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

    def get_repair_of_attempt_id(self, obj) -> str:
        snapshot = obj.request_snapshot if isinstance(obj.request_snapshot, dict) else {}
        value = snapshot.get("repair_of_attempt_id")
        try:
            return str(uuid.UUID(str(value))) if value else ""
        except (TypeError, ValueError, AttributeError):
            return ""

    def get_retry_feedback_sha256(self, obj) -> str:
        snapshot = obj.request_snapshot if isinstance(obj.request_snapshot, dict) else {}
        value = str(snapshot.get("retry_feedback_sha256") or "").casefold()
        if len(value) == 64 and all(character in "0123456789abcdef" for character in value):
            return value
        return ""


class ResolvePromptNormalizationSmokeInputSerializer(serializers.Serializer):
    smoke_attempt_id = serializers.UUIDField()
    outcome = serializers.ChoiceField(
        choices=ModelDeploymentSmokeAttemptResolution.Outcome.choices,
    )
    note = serializers.CharField(trim_whitespace=True)
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

    def validate_note(self, value):
        try:
            return normalize_audit_note(value)
        except DjangoValidationError as exc:
            _raise_drf(exc)


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
        try:
            owner_id = generated_artifact_storage_owner_id(obj)
        except (ProvenanceError, AttributeError, ValueError):
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
        data["config_snapshot_sha256"] = json_contract(
            safe_config,
            max_bytes=_stage_snapshot_max_bytes(),
        ).sha256
        data["input_snapshot_sha256"] = json_contract(
            safe_input,
            max_bytes=_stage_snapshot_max_bytes(),
        ).sha256
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
    route_index = serializers.IntegerField(min_value=0)
    prompt_profile_binding_id = serializers.UUIDField()


class ProductionWorkflowPromptProfileSelectionSerializer(serializers.Serializer):
    operation = serializers.CharField(max_length=100)
    mode = serializers.CharField(required=False, allow_blank=True, default="", max_length=100)
    prompt_profile_binding_id = serializers.UUIDField()


class ProductionWorkflowSelectionSerializer(serializers.Serializer):
    schema_version = serializers.IntegerField(min_value=2, max_value=2)
    mode = serializers.ChoiceField(choices=("uniform", "per_shot"))
    route_index = serializers.IntegerField(
        required=False,
        allow_null=True,
        default=None,
        min_value=0,
    )
    shot_routes = ProductionWorkflowShotRouteSerializer(
        many=True,
        required=False,
        default=list,
        allow_empty=True,
    )
    prompt_profile_selections = ProductionWorkflowPromptProfileSelectionSerializer(
        many=True,
        required=False,
        default=list,
        allow_empty=True,
    )

    def validate(self, attrs):
        mode = attrs["mode"]
        route_index = attrs.get("route_index")
        shot_routes = attrs.get("shot_routes") or []
        profile_selections = attrs.get("prompt_profile_selections") or []
        if mode == "uniform":
            if route_index is None:
                raise serializers.ValidationError({
                    "route_index": "整集统一工作流必须选择一个配方 route",
                })
            if shot_routes:
                raise serializers.ValidationError({
                    "shot_routes": "整集统一工作流不能同时提交逐镜头 route",
                })
            if not profile_selections:
                raise serializers.ValidationError({
                    "prompt_profile_selections": "整集 route 必须显式选择 PromptProfile binding",
                })
            contracts = [
                (item["operation"], item.get("mode") or "")
                for item in profile_selections
            ]
            if len(contracts) != len(set(contracts)):
                raise serializers.ValidationError({
                    "prompt_profile_selections": "同一 operation/mode 不能重复选择",
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
        if profile_selections:
            raise serializers.ValidationError({
                "prompt_profile_selections": "逐镜头模式的 binding 必须随 shot_routes 提交",
            })
        shot_ids = [str(item["shot_id"]) for item in shot_routes]
        if len(shot_ids) != len(set(shot_ids)):
            raise serializers.ValidationError({
                "shot_routes": "同一镜头不能重复选择 route",
            })
        return attrs


class UnattendedReferenceInputSerializer(serializers.Serializer):
    asset_id = serializers.UUIDField()
    purpose = serializers.RegexField(SAFE_CONDITIONING_IDENTIFIER_PATTERN)
    use_embedded_audio = serializers.BooleanField()


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
    idea = serializers.CharField(required=False, allow_blank=True, default="")
    style_hint = serializers.CharField(required=False, allow_blank=True, default="")
    episode_count = serializers.IntegerField(
        required=False,
        allow_null=True,
        default=None,
        min_value=1,
        help_text=(
            "AI 剧本或含 script_structure/ai 的导入结构化计划集数；省略时使用"
            "当前项目创建策略的冻结默认值。"
        ),
    )
    shots_per_episode = serializers.IntegerField(
        required=False,
        allow_null=True,
        default=None,
        min_value=1,
        help_text=(
            "AI 剧本必须提供；导入剧本仅在配方含 script_structure/ai 时可与"
            "episode_duration_seconds 成对提供。"
        ),
    )
    episode_duration_seconds = serializers.IntegerField(
        required=False,
        allow_null=True,
        default=None,
        min_value=1,
        help_text=(
            "单集总时长预算（秒）；会与所选结构化文本模型全部冻结 route 的"
            "镜头时长策略求交并在外部调用前冻结或拒绝。"
        ),
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
    bible_image_prompt_profile_binding_id = serializers.UUIDField(
        required=False,
        allow_null=True,
        default=None,
    )
    unattended_confirmed = serializers.BooleanField(required=False, default=False)
    reference_inputs = UnattendedReferenceInputSerializer(
        many=True,
        required=False,
        default=list,
    )

    def validate_episode_ids(self, value):
        return sorted(set(value), key=str)


class BibleLibraryAssetSelectionSerializer(serializers.Serializer):
    asset_id = serializers.UUIDField()
    expected_sha256 = serializers.RegexField(regex=r"^[0-9a-f]{64}$")


class BibleImageBindingSelectionSerializer(serializers.Serializer):
    entity_type = serializers.ChoiceField(choices=("character", "scene", "prop"))
    entity_id = serializers.UUIDField()
    selected_artifact_ids = serializers.ListField(
        child=serializers.UUIDField(),
        required=False,
        default=list,
    )
    selected_library_assets = serializers.ListField(
        child=BibleLibraryAssetSelectionSerializer(),
        required=False,
        default=list,
        min_length=0,
    )
    anchor_artifact_id = serializers.UUIDField(required=False, allow_null=True)
    anchor_library_asset_id = serializers.UUIDField(required=False, allow_null=True)

    def validate(self, attrs):
        selected_artifacts = attrs.get("selected_artifact_ids") or []
        selected_library_assets = attrs.get("selected_library_assets") or []
        selected_asset_ids = [item["asset_id"] for item in selected_library_assets]
        if len(selected_artifacts) != len(set(selected_artifacts)):
            raise serializers.ValidationError({
                "selected_artifact_ids": "同一候选不能重复选择",
            })
        if len(selected_asset_ids) != len(set(selected_asset_ids)):
            raise serializers.ValidationError({
                "selected_library_assets": "同一素材库图片不能重复选择",
            })
        if not selected_artifacts and not selected_library_assets:
            raise serializers.ValidationError(
                "每个 Bible 实体必须至少选择一个模型候选或素材库图片",
            )
        maximum_assets = boundary_int(
            "security.production.max_bible_reference_assets_per_entity"
        )
        if len(selected_artifacts) + len(selected_library_assets) > maximum_assets:
            raise serializers.ValidationError(
                f"每个 Bible 实体最多选择 {maximum_assets} 张参考图"
            )
        anchor_artifact = attrs.get("anchor_artifact_id")
        anchor_asset = attrs.get("anchor_library_asset_id")
        if bool(anchor_artifact) == bool(anchor_asset):
            raise serializers.ValidationError(
                "必须且只能从模型候选或素材库图片中指定一个 anchor",
            )
        if anchor_artifact and anchor_artifact not in selected_artifacts:
            raise serializers.ValidationError({
                "anchor_artifact_id": "anchor 必须属于 selected_artifact_ids",
            })
        if anchor_asset and anchor_asset not in selected_asset_ids:
            raise serializers.ValidationError({
                "anchor_library_asset_id": (
                    "anchor 必须属于 selected_library_assets"
                ),
            })
        return attrs


class BibleImageRegenerationEntitySerializer(serializers.Serializer):
    entity_type = serializers.ChoiceField(choices=("character", "scene", "prop"))
    entity_id = serializers.UUIDField()


class BibleImageRegenerationInputSerializer(serializers.Serializer):
    request_id = serializers.UUIDField()
    expected_source_snapshot_sha256 = serializers.RegexField(
        regex=r"^[0-9a-f]{64}$",
    )
    entities = serializers.ListField(
        child=BibleImageRegenerationEntitySerializer(),
        min_length=1,
    )

    def validate_entities(self, value):
        keys = [
            (str(item["entity_type"]), str(item["entity_id"]))
            for item in value
        ]
        if len(keys) != len(set(keys)):
            raise serializers.ValidationError("同一 Bible 实体不能重复提交")
        return value


class ShotReferenceEntityBindingSerializer(serializers.Serializer):
    entity_id = serializers.UUIDField()
    asset_id = serializers.UUIDField()


class StoryboardVideoReferenceSerializer(serializers.Serializer):
    asset_id = serializers.UUIDField()
    purpose = serializers.RegexField(SAFE_CONDITIONING_IDENTIFIER_PATTERN)
    use_embedded_audio = serializers.BooleanField()
    audio_asset_id = serializers.UUIDField(required=False, allow_null=True)
    audio_purpose = serializers.RegexField(
        SAFE_CONDITIONING_IDENTIFIER_PATTERN,
        required=False,
    )

    def validate(self, attrs):
        embedded = bool(attrs.get("use_embedded_audio", False))
        external_audio = attrs.get("audio_asset_id") is not None
        if (embedded or external_audio) != (attrs.get("audio_purpose") is not None):
            raise serializers.ValidationError(
                "启用视频配套音频时必须提供 audio_purpose；未启用时不得提供",
            )
        return attrs


class StoryboardAudioReferenceSerializer(serializers.Serializer):
    asset_id = serializers.UUIDField()
    purpose = serializers.RegexField(SAFE_CONDITIONING_IDENTIFIER_PATTERN)


class StoryboardShotReferenceBindingsSerializer(serializers.Serializer):
    first_frame_asset_id = serializers.UUIDField(required=False)
    last_frame_asset_id = serializers.UUIDField(required=False)
    first_frame_reference_asset_id = serializers.UUIDField(required=False)
    characters = ShotReferenceEntityBindingSerializer(many=True, required=False)
    scene = ShotReferenceEntityBindingSerializer(required=False)
    props = ShotReferenceEntityBindingSerializer(many=True, required=False)
    videos = StoryboardVideoReferenceSerializer(many=True, required=False)
    audios = StoryboardAudioReferenceSerializer(many=True, required=False)


class StoryboardShotReferenceSelectionSerializer(serializers.Serializer):
    shot_id = serializers.UUIDField()
    reference_bindings = StoryboardShotReferenceBindingsSerializer()


class ApprovalDecisionInputSerializer(serializers.Serializer):
    stage_key = serializers.CharField(max_length=100)
    decision = serializers.ChoiceField(choices=ApprovalDecision.Decision.choices)
    comment = serializers.CharField(
        required=False,
        allow_blank=True,
        default="",
    )
    candidate_overrides = serializers.DictField(
        child=serializers.CharField(
            allow_blank=False,
            trim_whitespace=False,
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
    bible_image_bindings = BibleImageBindingSelectionSerializer(
        many=True,
        required=False,
        default=list,
        help_text=(
            "bible_image_review 按实体选择 1..N 个模型候选和/或素材库图片，"
            "并从所选来源中指定唯一 anchor。"
        ),
    )
    shot_reference_bindings = StoryboardShotReferenceSelectionSerializer(
        many=True,
        required=False,
        default=list,
        help_text=(
            "storyboard_review 按 Shot 完整提交精确 reference_bindings；"
            "Bible 生产闭包要求覆盖所有已选分集的 Shot。"
        ),
    )

    def validate_candidate_overrides(self, value):
        maximum_items = normalization_batch_max_items()
        maximum_characters = normalization_text_max_characters()
        if len(value) > maximum_items:
            raise serializers.ValidationError(
                f"candidate_overrides 最多包含 {maximum_items} 项",
            )
        if any(not isinstance(key, str) or not key or len(key) > 191 for key in value):
            raise serializers.ValidationError("candidate_overrides 的 request_id 无效")
        if any(len(item) > maximum_characters for item in value.values()):
            raise serializers.ValidationError(
                f"candidate_overrides 文本不能超过 {maximum_characters} 个字符",
            )
        return value

    def validate_comment(self, value):
        try:
            return normalize_optional_audit_comment(value)
        except DjangoValidationError as exc:
            _raise_drf(exc)

    def validate(self, attrs):
        is_snapshot_bound_approval = (
            attrs.get("stage_key") in {
                "storyboard_review", "script_normalize_review", "visual_rules_review",
                "bible_image_review",
            }
            and attrs.get("decision") == ApprovalDecision.Decision.APPROVE
        )
        expected = str(attrs.get("expected_source_snapshot_sha256") or "")
        if is_snapshot_bound_approval and not expected:
            raise serializers.ValidationError({
                "expected_source_snapshot_sha256": "批准前必须提交页面展示的来源快照 hash",
            })
        if expected and not is_snapshot_bound_approval:
            raise serializers.ValidationError({
                "expected_source_snapshot_sha256": (
                    "该字段只允许用于批准 storyboard_review、script_normalize_review、"
                    "visual_rules_review 或 bible_image_review"
                ),
            })
        if attrs.get("adopt_generated_metadata") and not (
            attrs.get("stage_key") == "script_review"
            and attrs.get("decision") == ApprovalDecision.Decision.APPROVE
        ):
            raise serializers.ValidationError({
                "adopt_generated_metadata": "只允许在批准 script_review 时启用",
            })
        bindings = attrs.get("bible_image_bindings") or []
        is_bible_approval = (
            attrs.get("stage_key") == "bible_image_review"
            and attrs.get("decision") == ApprovalDecision.Decision.APPROVE
        )
        if is_bible_approval and not bindings:
            raise serializers.ValidationError({
                "bible_image_bindings": (
                    "批准前必须逐实体选择至少一个模型候选或素材库图片并指定 anchor"
                ),
            })
        if bindings and not is_bible_approval:
            raise serializers.ValidationError({
                "bible_image_bindings": "该字段只允许用于批准 bible_image_review",
            })
        entity_keys = [
            (str(item["entity_type"]), str(item["entity_id"])) for item in bindings
        ]
        if len(entity_keys) != len(set(entity_keys)):
            raise serializers.ValidationError({
                "bible_image_bindings": "同一 Bible 实体不能重复提交",
            })
        shot_bindings = attrs.get("shot_reference_bindings") or []
        is_storyboard_approval = (
            attrs.get("stage_key") == "storyboard_review"
            and attrs.get("decision") == ApprovalDecision.Decision.APPROVE
        )
        if shot_bindings and not is_storyboard_approval:
            raise serializers.ValidationError({
                "shot_reference_bindings": "该字段只允许用于批准 storyboard_review",
            })
        shot_ids = [str(item["shot_id"]) for item in shot_bindings]
        if len(shot_ids) != len(set(shot_ids)):
            raise serializers.ValidationError({
                "shot_reference_bindings": "同一 Shot 不能重复提交",
            })
        return attrs


class SelectArtifactInputSerializer(serializers.Serializer):
    artifact_id = serializers.UUIDField()


class SelectRouteInputSerializer(serializers.Serializer):
    stage_key = serializers.CharField(max_length=100)
    route_index = serializers.IntegerField(min_value=0)


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
        default="",
    )

    def validate_comment(self, value):
        try:
            return normalize_optional_audit_comment(value)
        except DjangoValidationError as exc:
            _raise_drf(exc)


class ResolveUncertainAttemptInputSerializer(serializers.Serializer):
    attempt_id = serializers.UUIDField()
    outcome = serializers.ChoiceField(choices=(
        GenerationAttemptResolution.Outcome.CONFIRMED_NOT_SUBMITTED,
        GenerationAttemptResolution.Outcome.CONFIRMED_REMOTE_SUBMITTED,
        GenerationAttemptResolution.Outcome.CONFIRMED_REMOTE_FAILED,
        GenerationAttemptResolution.Outcome.ACTUAL_COST_CONFIRMED,
    ))
    note = serializers.CharField(allow_blank=False, trim_whitespace=True)
    provider_request_id = serializers.CharField(
        required=False, allow_blank=True, max_length=191, default="",
    )
    actual_cost = serializers.DecimalField(
        required=False, allow_null=True, max_digits=14, decimal_places=6,
    )
    cost_currency = serializers.CharField(
        required=False, allow_blank=True, max_length=12, default="",
    )

    def validate_note(self, value):
        try:
            return normalize_audit_note(value)
        except DjangoValidationError as exc:
            _raise_drf(exc)
