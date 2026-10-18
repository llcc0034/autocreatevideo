from __future__ import annotations

from fractions import Fraction
from io import BytesIO
from pathlib import Path, PurePosixPath, PureWindowsPath
import hashlib
import json
import re
import secrets
import subprocess
import tempfile
import unicodedata
from uuid import UUID

from django.conf import settings
from django.contrib.auth import get_user_model
from django.contrib.auth.password_validation import validate_password
from django.core.files.base import ContentFile
from django.core.exceptions import ValidationError as DjangoValidationError
from django.db import transaction
from django.db.models import Q
from django.urls import reverse
from django.utils.dateparse import parse_datetime
from django.utils import timezone
from PIL import Image, ImageOps, UnidentifiedImageError
from drf_spectacular.extensions import OpenApiSerializerExtension
from drf_spectacular.utils import extend_schema_field
from rest_framework import serializers
from rest_framework.exceptions import APIException

from studio.models import (
    Asset,
    AssetGenerationJob,
    AssemblyJob,
    CharacterBible,
    ConsistencyLock,
    Episode,
    GenerationTask,
    ModelExecutionInstanceVersion,
    PromptFieldTranslation,
    PropBible,
    ReleaseBundle,
    SceneBible,
    SeriesProject,
    Shot,
    ShotCharacterRelation,
    Style,
    StyleCategory,
    WorkflowTemplate,
)
from studio.media_signing import classify_task_output, signed_media_token
from studio.services.provenance import ProvenanceError, assembly_manifest_path
from studio.services.dialogue import (
    DialogueContractError,
    normalize_dialogue,
    validate_dialogue_timing,
)
from studio.services.prompt_translation import (
    CanonicalTargetCollisionError,
    TranslationValidationError,
    ensure_no_canonical_target_collisions,
    resolve_current_source,
    translation_entry_context_current,
)
from studio.services.configuration import (
    ConfigurationResolutionError,
    project_creation_policy,
    redact_sensitive_bindings,
    resolve_configuration,
    video_shot_duration_policy,
)
from studio.services.media_processing_profiles import (
    MediaProcessingProfileError,
    resolve_reference_media_profile,
)
from studio.services.access_control import role_for_user, security_profile_for
from studio.services.bible_reference_contract import (
    BIBLE_IMAGE_GENERATION_PURPOSE,
)
from studio.services.shot_versions import RESERVED_SHOT_VERSION_METADATA_KEYS
from studio.services.script_bible import (
    ScriptBibleProvenanceProtectedError,
    validate_untrusted_script_bible_metadata,
)
from studio.services.script_sources import (
    ScriptSourceValidationError,
    normalize_script_asset_upload,
)
from studio.services.visual_reference_policy import (
    VisualReferencePolicyError,
    validate_visual_reference_policy_metadata,
)
from studio.services.json_contracts import json_contract
from studio.services.security_boundaries import boundary_int
from studio.services.content_contracts import (
    translation_request_max_entries,
    translation_text_max_characters,
)

User = get_user_model()


def _stage_snapshot_max_bytes() -> int:
    return boundary_int("security.snapshot.stage_max_bytes")


def _asset_runtime_limits() -> dict[str, int]:
    keys = (
        "artifact.upload_max_bytes", "artifact.video_upload_max_bytes",
        "artifact.audio_upload_max_bytes", "artifact.tenant_quota_bytes",
        "artifact.image_max_width", "artifact.image_max_height",
        "artifact.image_max_pixels", "runtime.task_stall_threshold_seconds",
    )
    return resolve_configuration(required_keys=keys).values

SCRIPT_BIBLE_METADATA_HELP = (
    "扩展元数据对象。script_bible_v1 是已停用的旧来源记录，无写入入口；"
    "通用创建、更新和 sync-bibles 不得注入、修改或删除已有保留成员。"
)


def _is_project_cover_asset(asset: Asset) -> bool:
    metadata = asset.metadata if isinstance(asset.metadata, dict) else {}
    return metadata.get("usage") == "project_cover"


def _is_formal_bible_reference_asset(asset: Asset) -> bool:
    metadata = asset.metadata if isinstance(asset.metadata, dict) else {}
    return metadata.get("generation_purpose") == BIBLE_IMAGE_GENERATION_PURPOSE


class FormalBibleAssetImmutableConflict(APIException):
    status_code = 409
    default_code = "bible_reference_asset_immutable"
    default_detail = (
        "已登记的 Bible 生成参考图是不可变生产事实，登记不等于审核采用；"
        "请新建 Asset 并通过新的 ProductionRun 重新审核绑定。"
    )


def _resolved_video_shot_duration_policy(
    project: SeriesProject | None = None,
) -> dict[str, int]:
    try:
        return video_shot_duration_policy(project=project)
    except ConfigurationResolutionError as exc:
        raise serializers.ValidationError({
            "configuration": {
                "code": exc.code,
                "detail": str(exc),
                "missing_keys": list(exc.missing_keys),
            },
        }) from exc


def _resolved_project_creation_policy(
    project: SeriesProject | None = None,
) -> dict[str, object]:
    try:
        result = project_creation_policy(project=project)
        maximum_fps = boundary_int("security.media.max_fps")
        maximum_dimension = boundary_int("security.media.max_dimension")
        if (
            int(result["fps"]) > maximum_fps
            or int(result["width"]) > maximum_dimension
            or int(result["height"]) > maximum_dimension
            or any(
                int(option[dimension]) > maximum_dimension
                for option in result["aspect_ratio_options"]
                for dimension in ("width", "height")
            )
        ):
            raise ConfigurationResolutionError(
                "项目创建策略超过当前安全边界",
                code="project_creation_policy_invalid",
            )
        seed_policy = result["continuity_seed_policy"]
        mode = seed_policy["mode"]
        seed = seed_policy["value"]
        if mode == "random_per_project":
            result["continuity_seed"] = secrets.randbelow(2**63 - 1) + 1
        elif mode == "explicit" and type(seed) is int and seed > 0:
            result["continuity_seed"] = seed
        else:
            raise ConfigurationResolutionError(
                "连续性种子策略必须指定 random_per_project 或有效 explicit seed",
                code="project_creation_policy_invalid",
            )
        result["maximum_fps"] = maximum_fps
        result["maximum_dimension"] = maximum_dimension
        return result
    except ConfigurationResolutionError as exc:
        raise serializers.ValidationError({
            "configuration": {"code": exc.code, "detail": str(exc)},
        }) from exc


def _validate_video_shot_duration(
    media_type: str,
    duration_ms: int,
    *,
    policy: dict[str, int],
) -> None:
    minimum = policy["minimum_duration_ms"]
    maximum = policy["maximum_duration_ms"]
    if (
        media_type == Shot.MediaType.VIDEO
        and not minimum <= duration_ms <= maximum
    ):
        raise serializers.ValidationError({
            "duration_ms": (
                f"视频分镜时长必须在 {minimum}–{maximum}ms"
            ),
        })


class UserSerializer(serializers.ModelSerializer):
    role = serializers.SerializerMethodField()
    must_change_password = serializers.SerializerMethodField()

    class Meta:
        model = User
        fields = (
            "id", "username", "email", "first_name", "last_name", "is_staff",
            "is_superuser", "role", "must_change_password", "date_joined",
        )
        read_only_fields = (
            "id", "is_staff", "is_superuser", "role", "must_change_password", "date_joined",
        )

    def get_role(self, obj) -> str:
        return role_for_user(obj)

    def get_must_change_password(self, obj) -> bool:
        return security_profile_for(obj).must_change_password


class RegisterSerializer(serializers.ModelSerializer):
    password = serializers.CharField(write_only=True, min_length=8, validators=[validate_password])

    class Meta:
        model = User
        fields = ("id", "username", "email", "first_name", "last_name", "password")
        read_only_fields = ("id",)

    def create(self, validated_data):
        user = User.objects.create_user(**validated_data)
        security_profile_for(user)
        return user


class StyleScopeField(serializers.ChoiceField):
    def __init__(self, **kwargs):
        super().__init__(choices=[("personal", "我的风格"), ("global", "共享风格")], **kwargs)

    def get_attribute(self, instance):
        return "global" if instance.owner_id is None else "personal"


class StyleCategorySerializer(serializers.ModelSerializer):
    style_count = serializers.IntegerField(read_only=True)
    can_manage = serializers.SerializerMethodField()

    def get_can_manage(self, obj) -> bool:
        return bool(self.context["request"].user.is_staff)

    class Meta:
        model = StyleCategory
        fields = "__all__"
        read_only_fields = ("id", "key", "created_at", "updated_at")


class StyleSerializer(serializers.ModelSerializer):
    owner = serializers.PrimaryKeyRelatedField(read_only=True)
    category = serializers.SlugRelatedField(slug_field="key", queryset=StyleCategory.objects.all(), allow_null=True, required=False)
    category_label = serializers.CharField(source="category.name", read_only=True, default="")
    category_active = serializers.BooleanField(source="category.is_active", read_only=True, allow_null=True, default=None)
    asset_count = serializers.IntegerField(read_only=True)
    project_count = serializers.IntegerField(read_only=True)
    generation_job_count = serializers.IntegerField(read_only=True)
    can_manage = serializers.SerializerMethodField()
    scope = StyleScopeField(required=False)
    # Stable technical identity is server-owned; changing a display name must
    # not change existing project/asset mappings.
    slug = serializers.SlugField(read_only=True)

    def get_can_manage(self, obj) -> bool:
        user = self.context["request"].user
        return bool(user.is_staff or obj.owner_id == user.pk)

    def validate(self, attrs):
        category = attrs.get("category")
        if category and not category.is_active and (not self.instance or self.instance.category_id != category.key):
            raise serializers.ValidationError({"category": "此题材分类已停用，不能用于新关联；原有分类可以保留。"})
        scope = attrs.get("scope")
        if self.instance and scope and scope != ("global" if self.instance.owner_id is None else "personal"):
            raise serializers.ValidationError({"scope": "创建后不能更改共享范围，请新建另一风格。"})
        if not self.instance and scope == "global" and not self.context["request"].user.is_staff:
            raise serializers.ValidationError({"scope": "只有管理员可以创建共享风格。"})
        for key in ("description", "visual_prompt", "negative_prompt"):
            if key in attrs and len(attrs[key].encode("utf-8")) > 65536:
                raise serializers.ValidationError({key: "内容不能超过 64 KiB。"})
        return attrs

    def update(self, instance, validated_data):
        validated_data.pop("scope", None)
        return super().update(instance, validated_data)

    class Meta:
        model = Style
        fields = "__all__"
        read_only_fields = ("id", "owner", "is_system", "created_at", "updated_at")


class StyleReferenceSerializer(serializers.Serializer):
    id = serializers.UUIDField()
    name = serializers.CharField()
    kind = serializers.ChoiceField(choices=["projects", "assets", "jobs"])


class StyleChoiceSerializer(serializers.Serializer):
    value = serializers.CharField()
    display_name = serializers.CharField()


class StyleCatalogSerializer(serializers.Serializer):
    scopes = StyleChoiceSerializer(many=True)
    can_manage_categories = serializers.BooleanField()


class AssetSerializer(serializers.ModelSerializer):
    owner = serializers.PrimaryKeyRelatedField(read_only=True)
    preview_url = serializers.SerializerMethodField()
    download_url = serializers.SerializerMethodField()
    metadata_sha256 = serializers.SerializerMethodField()
    is_active = serializers.BooleanField(required=False, default=True)

    class Meta:
        model = Asset
        fields = "__all__"
        read_only_fields = (
            "id", "owner", "mime_type", "checksum", "source_info",
            "created_at", "updated_at",
        )

    def validate_style(self, style):
        user = self.context["request"].user
        if style and style.owner_id not in (None, user.id) and not user.is_staff:
            raise serializers.ValidationError("不能使用其他用户的风格")
        if style and not style.is_active and (not self.instance or self.instance.style_id != style.pk):
            raise serializers.ValidationError("此风格已停用，不能用于新关联；已有引用可保留。")
        return style
    def validate_kind(self, value):
        if self.instance is not None and value != self.instance.kind:
            editable_kinds = {
                Asset.Kind.CHARACTER,
                Asset.Kind.SCENE,
                Asset.Kind.PROP,
                Asset.Kind.REFERENCE,
            }
            if value not in editable_kinds or self.instance.kind not in editable_kinds:
                raise serializers.ValidationError(
                    "只允许在通用、人物、场景和道具图片之间更正类别；其他素材类型不可转换。"
                )
        return value

    def validate_local_path(self, value):
        if not value:
            return value
        user = self.context["request"].user
        if not user.is_staff:
            raise serializers.ValidationError(
                "普通用户不能声明服务端 local_path；请上传文件，由系统写入用户隔离目录"
            )
        try:
            resolved = Path(value).resolve()
            allowed = (
                resolved.is_relative_to(Path(settings.ASSET_LIBRARY_ROOT).resolve())
                or resolved.is_relative_to(Path(settings.MEDIA_ROOT).resolve())
            )
        except (OSError, ValueError):
            allowed = False
        if not allowed:
            raise serializers.ValidationError("local_path 只能指向已配置的素材库或媒体目录；其他文件请使用上传")
        return str(resolved)

    def validate(self, attrs):
        if "source_info" in self.initial_data:
            raise serializers.ValidationError({
                "source_info": "来源信息由服务端根据实际入库路径记录，客户端不能提交或改写。",
            })
        kind = attrs.get("kind", getattr(self.instance, "kind", None))
        existing_metadata = (
            self.instance.metadata
            if self.instance is not None and isinstance(self.instance.metadata, dict)
            else {}
        )
        if "metadata" in attrs:
            incoming_metadata = attrs.get("metadata") or {}
            if "cleared_history_origin" in incoming_metadata and incoming_metadata["cleared_history_origin"] != existing_metadata.get("cleared_history_origin"):
                raise serializers.ValidationError({"metadata": "历史清理来源由服务端维护，不能伪造、删除或改写。"})
        if (
            self.instance is not None
            and AssetGenerationJob.objects.filter(output_asset=self.instance).exists()
        ):
            immutable_fields = {
                "file", "local_path", "reference_url", "kind", "prompt_hint",
                "source_info",
            }
            changed = sorted(
                field for field in immutable_fields
                if field in attrs and attrs[field] != getattr(self.instance, field)
            )
            if "metadata" in attrs and attrs.get("metadata") != existing_metadata:
                changed.append("metadata")
            if changed:
                raise serializers.ValidationError({
                    "code": "generated_asset_lineage_immutable",
                    "detail": "模型生成素材的字节、类型、提示词、来源和血缘元数据不可原位改写；可编辑名称、说明、风格和启用状态。",
                    "fields": changed,
                })
        if self.instance is not None and _is_formal_bible_reference_asset(self.instance):
            immutable_fields = {
                "file", "local_path", "reference_url", "kind", "style", "is_active",
                "source_info",
            }
            changed = sorted(
                field for field in immutable_fields
                if field in attrs and attrs[field] != getattr(self.instance, field)
            )
            if "metadata" in attrs and attrs.get("metadata") != existing_metadata:
                changed.append("metadata")
            if changed:
                raise FormalBibleAssetImmutableConflict({
                    "code": "bible_reference_asset_immutable",
                    "detail": FormalBibleAssetImmutableConflict.default_detail,
                    "fields": changed,
                })
        if "metadata" in attrs:
            metadata = {**existing_metadata, **(attrs.get("metadata") or {})}
            attrs["metadata"] = metadata
        else:
            metadata = existing_metadata
        if (
            self.instance is not None
            and self.instance.kind == Asset.Kind.REFERENCE
            and "metadata" in attrs
            and metadata.get("reference_category") != existing_metadata.get("reference_category")
        ):
            raise serializers.ValidationError({
                "metadata": "请通过素材类别字段更正人物、场景或道具分类，不能直接改写 reference_category。",
            })
        if self.instance is not None and _is_project_cover_asset(self.instance):
            if "file" in attrs:
                raise serializers.ValidationError({
                    "file": "专用连续剧封面不能原位替换；请上传新封面后重新绑定项目。",
                })
            if "metadata" in attrs:
                if not isinstance(metadata, dict) or metadata.get("usage", "project_cover") != "project_cover":
                    raise serializers.ValidationError({
                        "metadata": "专用连续剧封面的 usage 标记不可修改。",
                    })
                existing_project_id = str(
                    (self.instance.metadata or {}).get("project_id") or ""
                )
                if (
                    "project_id" in metadata
                    and str(metadata.get("project_id") or "") != existing_project_id
                ):
                    raise serializers.ValidationError({
                        "metadata": "专用连续剧封面的 project_id 不可修改。",
                    })
                attrs["metadata"] = {
                    **(self.instance.metadata or {}),
                    **metadata,
                    "usage": "project_cover",
                    "project_id": existing_project_id,
                }
                metadata = attrs["metadata"]
        if (
            self.instance is not None
            and not _is_project_cover_asset(self.instance)
            and isinstance(metadata, dict)
            and metadata.get("usage") == "project_cover"
        ):
            raise serializers.ValidationError({
                "metadata": "普通素材不能改为专用连续剧封面；请通过封面上传入口新建素材。",
            })
        if isinstance(metadata, dict) and metadata.get("usage") == "project_cover":
            if kind != Asset.Kind.REFERENCE:
                raise serializers.ValidationError({
                    "metadata": "连续剧封面必须使用 reference 图片素材。",
                })
            project_id = str(metadata.get("project_id") or "").strip()
            request = self.context["request"]
            if not project_id or not SeriesProject.objects.filter(
                pk=project_id, owner=request.user,
            ).exists():
                raise serializers.ValidationError({
                    "metadata": "专用连续剧封面必须关联当前用户已有的连续剧项目。",
                })
            if self.instance is None and not attrs.get("file"):
                raise serializers.ValidationError({
                    "file": "连续剧封面必须通过受控文件上传创建。",
                })
        reference_category = (
            str(metadata.get("reference_category") or "").strip().casefold()
            if isinstance(metadata, dict) else ""
        )
        classified_image = kind in {
            Asset.Kind.CHARACTER, Asset.Kind.SCENE, Asset.Kind.PROP,
        }
        if kind == Asset.Kind.REFERENCE and reference_category:
            if reference_category not in {
                Asset.Kind.CHARACTER, Asset.Kind.SCENE, Asset.Kind.PROP,
            }:
                raise serializers.ValidationError({
                    "metadata": (
                        "reference_category 仅支持 character、scene 或 prop；"
                        "不参与 Bible 草稿的普通 reference 请不要设置该字段。"
                    ),
                })
            classified_image = True
        if classified_image:
            description = attrs.get("description", getattr(self.instance, "description", ""))
            prompt_hint = attrs.get("prompt_hint", getattr(self.instance, "prompt_hint", ""))
            if not str(description or "").strip():
                raise serializers.ValidationError({
                    "description": "分类图片素材必须填写基础描述；自动设定草稿不会读取图片像素。",
                })
            if not str(prompt_hint or "").strip():
                raise serializers.ValidationError({
                    "prompt_hint": "分类图片素材必须填写视觉锚点；自动设定草稿只使用已保存元数据。",
                })
        if (
            self.instance is not None
            and kind in {Asset.Kind.CHARACTER, Asset.Kind.SCENE, Asset.Kind.PROP}
            and existing_metadata.get("reference_category")
            and "metadata" not in attrs
        ):
            attrs["metadata"] = {
                key: value for key, value in existing_metadata.items()
                if key != "reference_category"
            }
        file_value = attrs.get("file", getattr(self.instance, "file", None))
        local_path = attrs.get("local_path", getattr(self.instance, "local_path", ""))
        reference_url = attrs.get(
            "reference_url",
            getattr(self.instance, "reference_url", ""),
        )
        supplied_sources = [
            label
            for label, value in (
                ("file", file_value),
                ("local_path", local_path),
                ("reference_url", reference_url),
            )
            if value
        ]
        source_fields_touched = any(
            field in attrs
            for field in ("file", "local_path", "reference_url")
        )
        enforce_source_contract = self.instance is None or source_fields_touched
        if enforce_source_contract and len(supplied_sources) > 1:
            raise serializers.ValidationError({
                "non_field_errors": (
                    "file、local_path 与 reference_url 只能使用一种素材来源。"
                ),
            })
        if enforce_source_contract and not supplied_sources:
            raise serializers.ValidationError({
                "non_field_errors": (
                    "素材必须保留上传文件、受管路径或外部引用地址中的一种来源。"
                ),
            })
        uploaded = attrs.get("file")
        if uploaded:
            from studio.services.asset_provenance import ASSET_UPLOAD_SOURCE_INFO
            attrs["source_info"] = ASSET_UPLOAD_SOURCE_INFO
            kind = attrs.get("kind", getattr(self.instance, "kind", None))
            if kind == Asset.Kind.SCRIPT:
                try:
                    canonical, verified_metadata = normalize_script_asset_upload(uploaded)
                except ScriptSourceValidationError as exc:
                    raise serializers.ValidationError({"file": str(exc)}) from exc
                canonical_name = f"{Path(uploaded.name).stem or 'script'}.md"
                uploaded = ContentFile(canonical, name=canonical_name)
                attrs["file"] = uploaded
                mime_type = "text/markdown"
            else:
                mime_type, verified_metadata = self._validate_upload(uploaded, kind)
            if isinstance(metadata, dict) and metadata.get("usage") == "project_cover":
                uploaded = self._sanitize_project_cover_upload(uploaded, mime_type)
                limits = _asset_runtime_limits()
                if uploaded.size > limits["artifact.upload_max_bytes"]:
                    raise serializers.ValidationError({
                        "file": f"处理后的封面不能超过 {limits['artifact.upload_max_bytes']} bytes",
                    })
                attrs["file"] = uploaded
                verified_metadata = {
                    **verified_metadata,
                    "manifest_schema_version": 1,
                    "source": "user_upload",
                    "size_bytes": uploaded.size,
                    "cover_processing": "static_metadata_stripped_v1",
                }
            attrs["mime_type"] = mime_type
            attrs["checksum"] = self._uploaded_checksum(uploaded)
            attrs["metadata"] = {
                **(getattr(self.instance, "metadata", {}) or {}),
                **(attrs.get("metadata") or {}),
                **verified_metadata,
            }
            self.enforce_tenant_quota(uploaded)
        elif local_path and (self.instance is None or "local_path" in attrs):
            attrs["source_info"] = "服务端受管路径登记"
        elif reference_url and (self.instance is None or "reference_url" in attrs):
            attrs["source_info"] = "外部引用地址登记"
        return attrs

    @staticmethod
    def _sanitize_project_cover_upload(uploaded, mime_type: str) -> ContentFile:
        """Normalize orientation, reject animation, and strip embedded metadata."""
        format_by_mime = {
            "image/jpeg": "JPEG",
            "image/png": "PNG",
            "image/webp": "WEBP",
        }
        try:
            uploaded.seek(0)
            with Image.open(uploaded) as image:
                if getattr(image, "is_animated", False) or getattr(image, "n_frames", 1) > 1:
                    raise serializers.ValidationError({
                        "file": "连续剧封面不支持动画图片。",
                    })
                image.load()
                normalized = ImageOps.exif_transpose(image)
                image_format = format_by_mime[mime_type]
                if image_format == "JPEG" and normalized.mode not in {"RGB", "L"}:
                    normalized = normalized.convert("RGB")
                elif image_format == "PNG" and normalized.mode not in {
                    "1", "L", "LA", "P", "RGB", "RGBA", "I", "I;16",
                }:
                    normalized = normalized.convert("RGBA" if "A" in normalized.mode else "RGB")
                elif image_format == "WEBP" and normalized.mode not in {"RGB", "RGBA"}:
                    normalized = normalized.convert("RGBA" if "A" in normalized.mode else "RGB")

                output = BytesIO()
                save_options = {
                    "JPEG": {"quality": 95, "optimize": True},
                    "PNG": {"optimize": True},
                    "WEBP": {"quality": 95, "method": 6},
                }[image_format]
                normalized.save(output, format=image_format, **save_options)
        except serializers.ValidationError:
            raise
        except (UnidentifiedImageError, Image.DecompressionBombError, OSError, ValueError) as exc:
            raise serializers.ValidationError({
                "file": "封面文件不是可安全解码的静态图片。",
            }) from exc
        finally:
            uploaded.seek(0)
        return ContentFile(output.getvalue(), name=uploaded.name)

    @staticmethod
    def _uploaded_checksum(uploaded) -> str:
        value = hashlib.sha256()
        uploaded.seek(0)
        for chunk in uploaded.chunks():
            value.update(chunk)
        uploaded.seek(0)
        return value.hexdigest()

    def _validate_upload(self, uploaded, kind: str) -> tuple[str, dict]:
        if kind == Asset.Kind.REFERENCE_VIDEO:
            return self._validate_reference_media_upload(uploaded, media_type="video")
        if kind == Asset.Kind.REFERENCE_AUDIO:
            return self._validate_reference_media_upload(uploaded, media_type="audio")
        return self._validate_image_upload(uploaded, kind), {}

    def _validate_image_upload(self, uploaded, kind: str) -> str:
        image_kinds = {
            Asset.Kind.CHARACTER, Asset.Kind.SCENE, Asset.Kind.PROP,
            Asset.Kind.REFERENCE, Asset.Kind.OUTPUT_IMAGE,
        }
        if kind not in image_kinds:
            raise serializers.ValidationError({
                "file": (
                    "当前上传接口允许人物、场景、道具、通用参考图片、参考视频和参考音频；"
                    "剧本请使用 script 类型；生成产物类型不能通过该入口上传"
                ),
            })
        limits = _asset_runtime_limits()
        if uploaded.size > limits["artifact.upload_max_bytes"]:
            raise serializers.ValidationError({
                "file": f"单文件不能超过 {limits['artifact.upload_max_bytes']} bytes",
            })
        extension = Path(uploaded.name).suffix.lower()
        allowed_extensions = {
            "JPEG": {".jpg", ".jpeg"}, "PNG": {".png"}, "WEBP": {".webp"},
        }
        format_mimes = {"JPEG": "image/jpeg", "PNG": "image/png", "WEBP": "image/webp"}
        try:
            uploaded.seek(0)
            with Image.open(uploaded) as image:
                image_format = (image.format or "").upper()
                width, height = image.size
                if image_format not in allowed_extensions or extension not in allowed_extensions[image_format]:
                    raise serializers.ValidationError({"file": "扩展名与实际图片格式不匹配，仅支持 JPEG/PNG/WebP"})
                expected_mime = format_mimes[image_format]
                if str(getattr(uploaded, "content_type", "")).lower() != expected_mime:
                    raise serializers.ValidationError({"file": "上传 MIME 与实际图片格式不匹配"})
                if (
                    width <= 0 or height <= 0
                    or width > limits["artifact.image_max_width"]
                    or height > limits["artifact.image_max_height"]
                    or width * height > limits["artifact.image_max_pixels"]
                ):
                    raise serializers.ValidationError({
                        "file": (
                            f"图片尺寸超限；最大 {limits['artifact.image_max_width']}x"
                            f"{limits['artifact.image_max_height']} / {limits['artifact.image_max_pixels']} pixels"
                        ),
                    })
                image.verify()
        except serializers.ValidationError:
            raise
        except (UnidentifiedImageError, Image.DecompressionBombError, OSError, ValueError) as exc:
            raise serializers.ValidationError({"file": "文件不是可安全解码的图片"}) from exc
        finally:
            uploaded.seek(0)
        return expected_mime

    def _validate_reference_media_upload(self, uploaded, *, media_type: str) -> tuple[str, dict]:
        media_label = {"video": "视频", "audio": "音频"}[media_type]
        limits = _asset_runtime_limits()
        specs = {
            "video": {
                "max_bytes": limits["artifact.video_upload_max_bytes"],
                "extensions": {
                    ".mp4": {"video/mp4"}, ".mov": {"video/quicktime"},
                    ".webm": {"video/webm"},
                },
            },
            "audio": {
                "max_bytes": limits["artifact.audio_upload_max_bytes"],
                "extensions": {
                    ".wav": {"audio/wav", "audio/x-wav"}, ".mp3": {"audio/mpeg"},
                    ".flac": {"audio/flac", "audio/x-flac"},
                    ".m4a": {"audio/mp4", "audio/x-m4a"}, ".ogg": {"audio/ogg"},
                },
            },
        }[media_type]
        if uploaded.size > specs["max_bytes"]:
            raise serializers.ValidationError({
                "file": f"参考{media_label}不能超过 {specs['max_bytes']} bytes",
            })
        extension = Path(uploaded.name).suffix.casefold()
        content_type = str(getattr(uploaded, "content_type", "")).casefold()
        if extension not in specs["extensions"] or content_type not in specs["extensions"][extension]:
            raise serializers.ValidationError({
                "file": f"参考{media_label}扩展名与 MIME 不匹配或不受支持",
            })
        try:
            reference_profile = resolve_reference_media_profile().value
            uploaded.seek(0)
            with tempfile.TemporaryDirectory() as temp_dir:
                temporary_path = Path(temp_dir) / f"reference{extension}"
                with temporary_path.open("wb") as temporary:
                    for chunk in uploaded.chunks():
                        temporary.write(chunk)
                completed = subprocess.run(
                    [
                        settings.FFPROBE_BINARY, "-v", "error", "-show_streams", "-show_format",
                        "-of", "json", str(temporary_path),
                    ],
                    capture_output=True,
                    text=True,
                    timeout=reference_profile["probe_timeout_seconds"],
                    check=False,
                )
            if completed.returncode:
                raise serializers.ValidationError({"file": f"ffprobe 无法安全解码参考{media_label}"})
            probe = json.loads(completed.stdout or "{}")
            streams = probe.get("streams") or []
            media_stream = next(
                (item for item in streams if item.get("codec_type") == media_type), None,
            )
            if media_stream is None:
                raise serializers.ValidationError({"file": f"文件不含有效{media_label}流"})
            duration = 0.0
            for value in [
                media_stream.get("duration"),
                (probe.get("format") or {}).get("duration"),
            ]:
                try:
                    if value is not None:
                        parsed_duration = float(value)
                        if parsed_duration > 0:
                            duration = parsed_duration
                            break
                except (TypeError, ValueError):
                    pass
            if duration <= 0:
                raise serializers.ValidationError({
                    "file": f"无法可靠确定参考{media_label}的正时长",
                })
            video_metadata = {}
            if media_type == "video":
                average_rate = str(media_stream.get("avg_frame_rate") or "")
                nominal_rate = str(media_stream.get("r_frame_rate") or "")
                source_rate = None
                for value in (average_rate, nominal_rate):
                    try:
                        parsed = Fraction(value)
                    except (ValueError, ZeroDivisionError):
                        continue
                    if parsed > 0:
                        source_rate = parsed
                        break
                video_metadata = {
                    "source_avg_frame_rate": average_rate,
                    "source_r_frame_rate": nominal_rate,
                }
                if source_rate is not None:
                    video_metadata.update({
                        "source_fps": round(float(source_rate), 6),
                        "source_fps_fraction": str(source_rate),
                    })
        except serializers.ValidationError:
            raise
        except (
            OSError, subprocess.SubprocessError, json.JSONDecodeError, ValueError,
            MediaProcessingProfileError, ConfigurationResolutionError,
        ) as exc:
            raise serializers.ValidationError({"file": f"无法验证参考{media_label}"}) from exc
        finally:
            uploaded.seek(0)
        return content_type, {
            "duration_ms": round(duration * 1000),
            "verified_media_type": media_type,
            "verified_by": "ffprobe",
            "has_audio": any(item.get("codec_type") == "audio" for item in streams),
            **video_metadata,
        }

    def enforce_tenant_quota(self, uploaded=None) -> None:
        uploaded = uploaded or self.validated_data.get("file")
        if not uploaded:
            return
        user = self.context["request"].user
        queryset = Asset.objects.filter(owner=user).exclude(file="")
        if self.instance:
            queryset = queryset.exclude(pk=self.instance.pk)
        used = 0
        for asset in queryset.only("file"):
            try:
                used += asset.file.size
            except (OSError, ValueError, FileNotFoundError):
                continue
        limits = _asset_runtime_limits()
        if used + uploaded.size > limits["artifact.tenant_quota_bytes"]:
            raise serializers.ValidationError({
                "file": (
                    f"租户素材配额不足：已使用 {used} bytes，"
                    f"上限 {limits['artifact.tenant_quota_bytes']} bytes"
                ),
            })

    def get_preview_url(self, obj) -> str:
        if obj.file:
            token = signed_media_token(purpose="asset", object_id=obj.id, owner_id=obj.owner_id)
            return reverse("signed-media", kwargs={"token": token})
        if obj.local_path:
            try:
                Path(obj.local_path).resolve().relative_to(Path(settings.ASSET_LIBRARY_ROOT).resolve())
                token = signed_media_token(purpose="asset", object_id=obj.id, owner_id=obj.owner_id)
                return reverse("signed-media", kwargs={"token": token})
            except (OSError, ValueError):
                pass
        return obj.reference_url or ""

    def get_download_url(self, obj) -> str:
        if obj.file or obj.local_path:
            token = signed_media_token(
                purpose="asset", object_id=obj.id, owner_id=obj.owner_id,
            )
            return reverse("signed-media", kwargs={"token": token}) + "?download=1"
        return ""

    @staticmethod
    def get_metadata_sha256(obj) -> str:
        from studio.services.asset_provenance import asset_snapshot_sha256

        return asset_snapshot_sha256([obj])


class WorkflowSmokeAssetCandidateSerializer(serializers.Serializer):
    id = serializers.UUIDField(read_only=True)
    name = serializers.CharField(read_only=True)
    kind = serializers.CharField(read_only=True)
    mime_type = serializers.CharField(read_only=True)
    checksum = serializers.CharField(read_only=True)
    duration_ms = serializers.IntegerField(read_only=True, allow_null=True)
    source_fps = serializers.FloatField(read_only=True, allow_null=True)
    has_audio = serializers.BooleanField(read_only=True)
    smoke_eligible = serializers.BooleanField(read_only=True)
    smoke_ineligible_reason = serializers.CharField(read_only=True)

class ProjectDeleteErrorDetailsSerializer(serializers.Serializer):
    detail = serializers.CharField()
    active_generation_tasks = serializers.IntegerField(required=False, min_value=0)
    unresolved_remote_tasks = serializers.IntegerField(required=False, min_value=0)
    active_assembly_jobs = serializers.IntegerField(required=False, min_value=0)
    active_translation_jobs = serializers.IntegerField(required=False, min_value=0)


class ProjectDeleteErrorSerializer(serializers.Serializer):
    status_code = serializers.IntegerField()
    code = serializers.ChoiceField(choices=(
        "not_found",
        "project_has_active_work",
        "protected_resource",
    ))
    details = ProjectDeleteErrorDetailsSerializer()


class ProjectDeleteErrorResponseSerializer(serializers.Serializer):
    error = ProjectDeleteErrorSerializer()


class SeriesProjectSerializer(serializers.ModelSerializer):
    owner = serializers.PrimaryKeyRelatedField(read_only=True)
    expected_updated_at = serializers.DateTimeField(required=False, write_only=True)
    default_shot_duration_ms = serializers.IntegerField(
        min_value=1,
        required=False,
    )
    cover_asset = serializers.PrimaryKeyRelatedField(
        queryset=Asset.objects.all(), allow_null=True, required=False,
    )
    cover_url = serializers.SerializerMethodField()
    episode_count = serializers.IntegerField(read_only=True)
    completed_episode_count = serializers.IntegerField(read_only=True)
    total_duration_ms = serializers.IntegerField(read_only=True)
    character_count = serializers.IntegerField(read_only=True)
    scene_count = serializers.IntegerField(read_only=True)
    prop_count = serializers.IntegerField(read_only=True)

    class Meta:
        model = SeriesProject
        fields = "__all__"
        read_only_fields = ("id", "owner", "completed_at", "created_at", "updated_at")
        # Creation policy supplies these values in validate(), before persistence.
        # Keep explicit inputs validated; do not duplicate policy defaults here.
        extra_kwargs = {
            "language": {"required": False},
            "aspect_ratio": {"required": False},
            "width": {"required": False},
            "height": {"required": False},
            "fps": {"required": False},
            "continuity_seed": {"required": False},
        }

    def validate_style(self, style):
        user = self.context["request"].user
        if style and style.owner_id not in (None, user.id) and not user.is_staff:
            raise serializers.ValidationError("不能使用其他用户的风格")
        if style and not style.is_active and (not self.instance or self.instance.style_id != style.pk):
            raise serializers.ValidationError("此风格已停用，不能用于新关联；已有引用可保留。")
        return style

    def validate_cover_asset(self, asset):
        if asset is None:
            return None
        request = self.context["request"]
        owner_id = self.instance.owner_id if self.instance is not None else request.user.id
        if asset.owner_id != owner_id:
            raise serializers.ValidationError("封面素材必须属于当前连续剧的所有者")
        if asset.kind != Asset.Kind.REFERENCE:
            raise serializers.ValidationError("封面素材必须是 reference 图片素材")
        if not asset.is_active:
            raise serializers.ValidationError("封面素材已停用")
        allowed_mime_formats = {
            "image/jpeg": "JPEG",
            "image/png": "PNG",
            "image/webp": "WEBP",
        }
        mime_type = str(asset.mime_type or "").casefold()
        if not asset.file or mime_type not in allowed_mime_formats:
            raise serializers.ValidationError("封面素材必须是受控上传的 JPEG、PNG 或 WebP 图片")
        metadata = asset.metadata if isinstance(asset.metadata, dict) else {}
        if metadata.get("usage") == "project_cover":
            project_id = self.instance.pk if self.instance is not None else None
            if str(metadata.get("project_id") or "") != str(project_id or ""):
                raise serializers.ValidationError("专用封面素材不属于当前连续剧项目")
        try:
            with asset.file.open("rb") as stream, Image.open(stream) as image:
                if (image.format or "").upper() != allowed_mime_formats[mime_type]:
                    raise serializers.ValidationError("封面素材的 MIME 与实际图片格式不一致")
                if getattr(image, "is_animated", False) or getattr(image, "n_frames", 1) > 1:
                    raise serializers.ValidationError("连续剧封面不支持动画图片")
                image.verify()
        except serializers.ValidationError:
            raise
        except (UnidentifiedImageError, Image.DecompressionBombError, OSError, ValueError) as exc:
            raise serializers.ValidationError("封面素材文件不存在或无法安全解码") from exc
        return asset

    def get_cover_url(self, obj) -> str:
        asset = obj.cover_asset
        if not asset or not asset.is_active or not asset.file:
            return ""
        token = signed_media_token(purpose="asset", object_id=asset.id, owner_id=asset.owner_id)
        return reverse("signed-media", kwargs={"token": token})

    def validate_production_settings(self, value):
        if not isinstance(value, dict):
            raise serializers.ValidationError("production_settings 必须是 JSON 对象")
        if "default_conditioning_mode" in value:
            raise serializers.ValidationError("项目不再配置默认视频输入模式，请在生产工作台选择执行实例。")
        if self.instance is not None:
            try:
                ensure_no_canonical_target_collisions(
                    self.instance, production_settings=value,
                )
            except CanonicalTargetCollisionError as exc:
                raise serializers.ValidationError({
                    "code": exc.code,
                    "collisions": exc.collisions,
                }) from exc
        return value

    def validate(self, attrs):
        creation_policy = _resolved_project_creation_policy() if self.instance is None else None
        if creation_policy is not None:
            if "language" not in attrs:
                attrs["language"] = creation_policy["default_language"]
            if "aspect_ratio" not in attrs:
                attrs["aspect_ratio"] = creation_policy["default_aspect_ratio"]
            allowed_aspect_ratios = {
                option["value"] for option in creation_policy["aspect_ratio_options"]
            }
            if attrs["aspect_ratio"] not in allowed_aspect_ratios:
                raise serializers.ValidationError({
                    "aspect_ratio": "画幅比例必须来自当前项目创建策略选项",
                })
            aspect_option = next(
                option for option in creation_policy["aspect_ratio_options"]
                if option["value"] == attrs["aspect_ratio"]
            )
            for field in ("width", "height"):
                if field not in attrs:
                    attrs[field] = aspect_option[field]
            for field in ("fps", "default_shot_duration_ms", "continuity_seed"):
                if field not in attrs:
                    attrs[field] = creation_policy[field]
            if not 1 <= attrs["fps"] <= creation_policy["maximum_fps"]:
                raise serializers.ValidationError({
                    "fps": f"帧率必须在 1–{creation_policy['maximum_fps']}fps 的安全范围内",
                })
            for field in ("width", "height"):
                if not 1 <= attrs[field] <= creation_policy["maximum_dimension"]:
                    raise serializers.ValidationError({
                        field: f"尺寸必须在 1–{creation_policy['maximum_dimension']}px 的安全范围内",
                    })
        duration_policy = _resolved_video_shot_duration_policy(self.instance)
        default_duration = attrs.get(
            "default_shot_duration_ms",
            getattr(
                self.instance,
                "default_shot_duration_ms",
                duration_policy["default_duration_ms"],
            ),
        )
        if self.instance is None and "default_shot_duration_ms" not in attrs:
            attrs["default_shot_duration_ms"] = default_duration
        if not (
            duration_policy["minimum_duration_ms"]
            <= default_duration
            <= duration_policy["maximum_duration_ms"]
        ):
            raise serializers.ValidationError({
                "default_shot_duration_ms": (
                    "默认视频分镜时长必须在当前生产策略范围 "
                    f"{duration_policy['minimum_duration_ms']}–"
                    f"{duration_policy['maximum_duration_ms']}ms"
                ),
            })
        requested_status = attrs.get("status", getattr(self.instance, "status", SeriesProject.Status.DRAFT))
        if requested_status == SeriesProject.Status.COMPLETED and (
            not self.instance
            or not self.instance.assembly_jobs.filter(
                scope=AssemblyJob.Scope.SERIES, status=AssemblyJob.Status.SUCCEEDED
            ).exists()
        ):
            raise serializers.ValidationError({"status": "只有成功完成整剧组装后才能标记 completed"})
        return attrs


class ProjectOwnedSerializer(serializers.ModelSerializer):
    def validate_project(self, project):
        user = self.context["request"].user
        if project.owner_id != user.id and not user.is_staff:
            raise serializers.ValidationError("不能访问其他用户的项目")
        return project

    def validate(self, attrs):
        user = self.context["request"].user
        assets = []
        anchor = attrs.get("anchor_asset")
        if anchor:
            assets.append(anchor)
        assets.extend(attrs.get("reference_assets") or [])
        if any(_is_project_cover_asset(asset) for asset in assets):
            raise serializers.ValidationError({
                "reference_assets": "连续剧封面是项目展示专用素材，不能绑定到设定集或生成输入。",
            })
        if not user.is_staff and any(asset.owner_id not in (None, user.id) for asset in assets):
            raise serializers.ValidationError({"reference_assets": "不能使用其他用户的素材"})
        if self.Meta.model in {CharacterBible, SceneBible, PropBible}:
            if "metadata" in attrs:
                incoming_metadata = attrs["metadata"]
                if not isinstance(incoming_metadata, dict):
                    raise serializers.ValidationError({
                        "metadata": "metadata 必须是 JSON 对象。",
                    })
                previous_metadata = (
                    getattr(self.instance, "metadata", {})
                    if self.instance is not None else {}
                )
                try:
                    validate_untrusted_script_bible_metadata(
                        previous_metadata,
                        incoming_metadata,
                    )
                except ScriptBibleProvenanceProtectedError as exc:
                    raise serializers.ValidationError({
                        "metadata": str(exc),
                    }) from exc
            expected_kind = {
                CharacterBible: Asset.Kind.CHARACTER,
                SceneBible: Asset.Kind.SCENE,
                PropBible: Asset.Kind.PROP,
            }[self.Meta.model]
            for asset in assets:
                if not asset.is_active:
                    raise serializers.ValidationError({
                        "reference_assets": f"素材“{asset.name}”已停用，不能绑定到 Bible。",
                    })
                if not str(asset.mime_type or "").casefold().startswith("image/"):
                    raise serializers.ValidationError({
                        "reference_assets": f"素材“{asset.name}”不是图片，不能绑定到 Bible。",
                    })
                if asset.kind == Asset.Kind.REFERENCE:
                    category = str(
                        (asset.metadata or {}).get("reference_category") or ""
                    ).strip().casefold()
                    if category != expected_kind:
                        raise serializers.ValidationError({
                            "reference_assets": (
                                f"通用素材“{asset.name}”未标注 reference_category={expected_kind}。"
                            ),
                        })
                elif asset.kind != expected_kind:
                    raise serializers.ValidationError({
                        "reference_assets": (
                            f"素材“{asset.name}”类别为 {asset.kind}，不能绑定到 {expected_kind} Bible。"
                        ),
                    })
            previous_provenance = (
                (getattr(self.instance, "metadata", {}) or {}).get("asset_draft_provenance")
                if self.instance is not None else None
            )
            current_provenance = previous_provenance
            if "metadata" in attrs:
                current_provenance = (
                    (attrs.get("metadata") or {}).get("asset_draft_provenance")
                    if isinstance(attrs.get("metadata"), dict) else None
                )
            from studio.services.asset_provenance import provenance_snapshots_equal

            if previous_provenance is not None and not provenance_snapshots_equal(
                current_provenance, previous_provenance,
            ):
                raise serializers.ValidationError({
                    "metadata": "已应用草稿的 asset_draft_provenance 是不可修改的历史审计快照。",
                })
            if previous_provenance is not None and "metadata" in attrs:
                attrs["metadata"] = {
                    **(attrs.get("metadata") or {}),
                    "asset_draft_provenance": previous_provenance,
                }
            if previous_provenance is None and current_provenance is not None:
                if not isinstance(current_provenance, dict):
                    raise serializers.ValidationError({
                        "metadata": "asset_draft_provenance 必须是对象。",
                    })
                asset_id = str(current_provenance.get("asset_id") or "")
                source_asset = next(
                    (asset for asset in assets if str(asset.id) == asset_id), None,
                )
                if source_asset is None:
                    raise serializers.ValidationError({
                        "metadata": "草稿来源素材必须仍绑定为该 Bible 的参考素材。",
                    })
                from studio.services.asset_provenance import asset_snapshot_sha256

                current_hash = asset_snapshot_sha256([source_asset])
                if (
                    current_provenance.get("evidence_basis") != "asset_metadata"
                    or current_provenance.get("pixel_vision_used") is not False
                    or current_provenance.get("reference_mode") != "single_reference"
                    or current_provenance.get("entity_type") != expected_kind
                    or {
                        str(current_provenance.get("asset_metadata_sha256") or "").casefold(),
                        str(current_provenance.get("source_metadata_sha256") or "").casefold(),
                    } != {current_hash}
                    or str(current_provenance.get("asset_checksum") or "").casefold()
                    != str(source_asset.checksum or "").casefold()
                ):
                    raise serializers.ValidationError({
                        "metadata": "参考素材快照已变化或 provenance 无效；请重新生成设定草稿。",
                    })
                try:
                    provided_updated_at = parse_datetime(
                        str(current_provenance.get("asset_updated_at") or ""),
                    )
                except (TypeError, ValueError, OverflowError):
                    provided_updated_at = None
                if provided_updated_at is None or provided_updated_at != source_asset.updated_at:
                    raise serializers.ValidationError({
                        "metadata": "参考素材版本已变化；请重新生成设定草稿。",
                    })
            # Display names are not identities. Exact declaration keys, Bible
            # IDs and scoped reference names own identity/ambiguity checks.
        if self.instance and getattr(self.instance, "is_locked", False) and attrs.get("is_locked", True):
            changed = []
            for key, value in attrs.items():
                if key in {"is_locked", "project"}:
                    continue
                current = getattr(self.instance, key)
                if hasattr(current, "all"):
                    current_ids = {item.pk for item in current.all()}
                    value_ids = {item.pk for item in value}
                    different = current_ids != value_ids
                else:
                    different = current != value
                if different:
                    changed.append(key)
            if changed:
                raise serializers.ValidationError({
                    "is_locked": f"bible 已锁定；请先解锁再修改: {', '.join(changed)}"
                })
        return attrs


class BibleEntitySerializer(ProjectOwnedSerializer):
    """Entity metadata is not a second setting writer."""

    def get_field_names(self, declared_fields, info):
        return [name for name in super().get_field_names(declared_fields, info)
            if name != "image_binding_review"]

    def to_internal_value(self, data):
        if isinstance(data, dict) and "image_binding_review" in data:
            raise serializers.ValidationError({"image_binding_review": "图片审核引用只能由原审核写入。"})
        if isinstance(data, dict) and "visual_identity" in data:
            raise serializers.ValidationError({
                "visual_identity": "旧视觉锚点字段已停用；请通过主图/变体设定另存修订。",
            })
        return super().to_internal_value(data)

    def update(self, instance, validated_data):
        from studio.services.bible_material_bindings import reviewed_binding
        binding = None
        if instance.image_binding_review_id and {"anchor_asset", "reference_assets"}.intersection(validated_data):
            kind = {CharacterBible: "character", SceneBible: "scene", PropBible: "prop"}[type(instance)]
            binding = reviewed_binding(instance, kind)
        instance = super().update(instance, validated_data)
        if binding is not None:
            retained = {str(value) for value in instance.reference_assets.values_list("pk", flat=True)}
            if instance.anchor_asset_id:
                retained.add(str(instance.anchor_asset_id))
            original = {binding["anchor_asset_id"], *binding["reference_asset_ids"]}
            if not original.intersection(retained):
                instance.image_binding_review = None
                instance.save(update_fields=("image_binding_review",))
        return instance


class CharacterBibleSerializer(BibleEntitySerializer):
    metadata = serializers.JSONField(
        required=False, help_text=SCRIPT_BIBLE_METADATA_HELP,
    )

    class Meta:
        model = CharacterBible
        fields = "__all__"
        read_only_fields = ("id", "created_at", "updated_at")

    def validate_metadata(self, value):
        if not isinstance(value, dict):
            raise serializers.ValidationError("metadata 必须是 JSON 对象")
        try:
            validate_visual_reference_policy_metadata(value)
        except VisualReferencePolicyError as exc:
            raise serializers.ValidationError(str(exc)) from exc
        return value

    def validate_anchor_asset(self, asset):
        user = self.context["request"].user
        if asset and asset.owner_id not in (None, user.id) and not user.is_staff:
            raise serializers.ValidationError("不能使用其他用户的素材")
        return asset


class SceneBibleSerializer(BibleEntitySerializer):
    metadata = serializers.JSONField(
        required=False, help_text=SCRIPT_BIBLE_METADATA_HELP,
    )

    class Meta:
        model = SceneBible
        fields = "__all__"
        read_only_fields = ("id", "created_at", "updated_at")


class PropBibleSerializer(BibleEntitySerializer):
    metadata = serializers.JSONField(
        required=False, help_text=SCRIPT_BIBLE_METADATA_HELP,
    )

    class Meta:
        model = PropBible
        fields = "__all__"
        read_only_fields = ("id", "created_at", "updated_at")


class ConsistencyLockSerializer(ProjectOwnedSerializer):
    class Meta:
        model = ConsistencyLock
        fields = "__all__"
        read_only_fields = ("id", "version", "created_at", "updated_at")


class EpisodeSerializer(serializers.ModelSerializer):
    project_title = serializers.CharField(source="project.title", read_only=True)
    shot_count = serializers.IntegerField(read_only=True)
    total_duration_ms = serializers.IntegerField(read_only=True)

    class Meta:
        model = Episode
        fields = "__all__"
        read_only_fields = ("id", "created_at", "updated_at")

    def validate_project(self, project):
        user = self.context["request"].user
        if project.owner_id != user.id and not user.is_staff:
            raise serializers.ValidationError("不能访问其他用户的项目")
        return project

    def validate(self, attrs):
        if self.instance is None:
            prompt_settings = attrs.get("prompt_settings")
            if prompt_settings is None:
                prompt_settings = {}
            if not isinstance(prompt_settings, dict):
                raise serializers.ValidationError({
                    "prompt_settings": "prompt_settings 必须是 JSON 对象",
                })
            if "include_audio" not in prompt_settings:
                project = attrs.get("project")
                prompt_settings = {
                    **prompt_settings,
                    "include_audio": _resolved_project_creation_policy(
                        project,
                    )["default_include_audio"],
                }
            if type(prompt_settings["include_audio"]) is not bool:
                raise serializers.ValidationError({
                    "prompt_settings": "include_audio 必须是布尔值",
                })
            attrs["prompt_settings"] = prompt_settings
        elif "prompt_settings" in attrs:
            prompt_settings = attrs["prompt_settings"]
            if (
                not isinstance(prompt_settings, dict)
                or type(prompt_settings.get("include_audio")) is not bool
            ):
                raise serializers.ValidationError({
                    "prompt_settings": "更新时必须显式保留布尔型 include_audio",
                })
        requested_status = attrs.get("status", getattr(self.instance, "status", Episode.Status.DRAFT))
        if requested_status == Episode.Status.COMPLETED and (
            not self.instance
            or not self.instance.assembly_jobs.filter(status=AssemblyJob.Status.SUCCEEDED).exists()
        ):
            raise serializers.ValidationError({"status": "只有成功完成单集组装后才能标记 completed"})
        return attrs




class ShotCharacterRelationValueSerializer(serializers.Serializer):
    role = serializers.ChoiceField(choices=ShotCharacterRelation.Role.choices)
    position = serializers.IntegerField(min_value=0)
    character_id = serializers.UUIDField()


@extend_schema_field(ShotCharacterRelationValueSerializer(many=True))
class ShotCharacterRelationsField(serializers.JSONField):
    def to_representation(self, value):
        return [{"role": row.role, "position": row.position, "character_id": str(row.character_id)}
                for row in value.all()]


class ShotSerializer(serializers.ModelSerializer):
    character_relations = ShotCharacterRelationsField(required=False)
    include_audio = serializers.BooleanField(required=False)
    duration_ms = serializers.IntegerField(
        min_value=1, required=False,
    )

    class Meta:
        model = Shot
        fields = ("id", "episode", "number", "title", "dialogue", "duration_ms",
            "episode_start_ms", "episode_end_ms", "media_type", "conditioning_mode", "include_audio",
            "scene", "props", "character_relations", "requires_first_frame_continuity",
            "requires_last_frame_continuity", "metadata", "created_at", "updated_at")
        read_only_fields = ("id", "created_at", "updated_at")

    def to_internal_value(self, data):
        retired_content = {key: "固定创作列已停用；请通过确切结构化字段和独立字段修订保存原值"
            for key in ("narrative", "detailed_description", "camera", "action",
                "sound_effects", "music", "continuity_notes") if key in data}
        if retired_content:
            raise serializers.ValidationError(retired_content)
        if "characters" in data:
            raise serializers.ValidationError({"characters": "旧混合人物池已停用，请提交 character_relations 的出镜/逐行发言关系"})
        retired = {key: "旧镜头素材绑定已停用，请通过生产运行的逐次素材引用选择；内容保存不会修改素材选择"
            for key in ("reference_bindings", "reference_assets") if key in data}
        if retired:
            raise serializers.ValidationError(retired)
        return super().to_internal_value(data)

    def validate_metadata(self, value):
        if not isinstance(value, dict):
            raise serializers.ValidationError("metadata 必须是 JSON 对象")
        if {"framing", "negative_prompt"} & value.keys():
            raise serializers.ValidationError("固定构图/负面提示词已停用；请使用独立结构化字段")
        return value

    def to_representation(self, instance):
        result = super().to_representation(instance)
        result["metadata"] = {key: value for key, value in result["metadata"].items()
            if key not in {"framing", "negative_prompt"}}
        return result

    @transaction.atomic
    def create(self, validated_data):
        relations = validated_data.pop("character_relations", [])
        shot = super().create(validated_data)
        ShotCharacterRelation.objects.bulk_create([
            ShotCharacterRelation(shot=shot, **row) for row in relations
        ])
        return shot

    @transaction.atomic
    def update(self, instance, validated_data):
        # Revalidate against locked current facts, not a pre-lock dialogue/pool.
        self.instance = Shot.objects.select_for_update().select_related("episode__project", "scene").get(pk=instance.pk)
        validated_data = self.validate(validated_data)
        relations = validated_data.pop("character_relations", None)
        shot = super().update(self.instance, validated_data)
        if relations is not None:
            current = [{"role": row.role, "position": row.position, "character_id": str(row.character_id)}
                for row in shot.character_relations.all()]
            identity = lambda row: (row["role"], row["position"], row["character_id"])
            if sorted(current, key=identity) != sorted(relations, key=identity):
                shot.character_relations.all().delete()
                ShotCharacterRelation.objects.bulk_create([
                    ShotCharacterRelation(shot=shot, **row) for row in relations
                ])
        return shot

    def _episode(self, attrs):
        return attrs.get("episode") or getattr(self.instance, "episode", None)

    def validate_episode(self, episode):
        user = self.context["request"].user
        if episode.project.owner_id != user.id and not user.is_staff:
            raise serializers.ValidationError("不能访问其他用户的项目")
        return episode

    def validate_dialogue(self, value):
        try:
            return normalize_dialogue(value)
        except DialogueContractError as exc:
            raise serializers.ValidationError(str(exc)) from exc

    def validate(self, attrs):
        episode = self._episode(attrs)
        duration_policy = _resolved_video_shot_duration_policy(
            episode.project if episode is not None else None,
        )
        creation_policy = (
            _resolved_project_creation_policy(
                episode.project if episode is not None else None,
            ) if self.instance is None else None
        )
        if self.instance is not None and "metadata" in attrs:
            current_metadata = (
                self.instance.metadata
                if isinstance(self.instance.metadata, dict)
                else {}
            )
            if not isinstance(attrs["metadata"], dict):
                raise serializers.ValidationError({"metadata": "metadata 必须是 JSON 对象"})
            submitted_metadata = dict(attrs["metadata"])
            for key in RESERVED_SHOT_VERSION_METADATA_KEYS:
                if key in submitted_metadata and submitted_metadata[key] != current_metadata.get(key):
                    raise serializers.ValidationError({
                        "metadata": (
                            f"{key} 是服务端维护的分镜版本字段，不能通过普通分镜更新修改"
                        ),
                    })
                if key in current_metadata:
                    submitted_metadata[key] = current_metadata[key]
                else:
                    submitted_metadata.pop(key, None)
            attrs["metadata"] = submitted_metadata
        media_type = attrs.get("media_type")
        if media_type is None:
            media_type = (
                self.instance.media_type
                if self.instance is not None
                else creation_policy["default_shot_media_type"]
            )
            if self.instance is None:
                attrs["media_type"] = media_type
        include_audio = attrs.get("include_audio")
        if include_audio is None:
            include_audio = (
                self.instance.include_audio
                if self.instance is not None
                else creation_policy["default_include_audio"]
            )
            if media_type == Shot.MediaType.IMAGE:
                include_audio = False
            if self.instance is None:
                attrs["include_audio"] = include_audio
        if media_type == Shot.MediaType.IMAGE and include_audio:
            raise serializers.ValidationError({
                "include_audio": "图片镜头不能启用音频",
            })
        if media_type == Shot.MediaType.IMAGE:
            attrs["include_audio"] = False
        duration_ms = attrs.get(
            "duration_ms",
            getattr(
                self.instance,
                "duration_ms",
                duration_policy["default_duration_ms"],
            ),
        )
        from studio.services.shot_timing import episode_interval_duration
        try:
            start_ms = attrs.get("episode_start_ms", getattr(self.instance, "episode_start_ms", None))
            end_ms = attrs.get("episode_end_ms", getattr(self.instance, "episode_end_ms", None))
            duration_ms = episode_interval_duration(start_ms, end_ms,
                duration_ms=(attrs.get("duration_ms") if {"episode_start_ms", "episode_end_ms"} & attrs.keys()
                    and start_ms is not None and end_ms is not None else duration_ms))
        except ValueError as exc:
            raise serializers.ValidationError({"episode_start_ms": str(exc)}) from exc
        if duration_ms is not None and (self.instance is None or {"episode_start_ms", "episode_end_ms"} & attrs.keys()):
            attrs["duration_ms"] = duration_ms
        _validate_video_shot_duration(
            media_type,
            duration_ms,
            policy=duration_policy,
        )
        if not episode:
            return attrs
        project_id = episode.project_id
        scene = attrs.get("scene")
        if scene and scene.project_id != project_id:
            raise serializers.ValidationError({"scene": "场景与分镜不属于同一项目"})
        for field in ("props",):
            values = attrs.get(field)
            if values is not None and any(value.project_id != project_id for value in values):
                raise serializers.ValidationError({field: "条目与分镜不属于同一项目"})
        dialogue = attrs.get("dialogue", getattr(self.instance, "dialogue", []))
        try:
            dialogue = validate_dialogue_timing(
                dialogue, duration_ms=duration_ms,
            )
            if "dialogue" in attrs:
                attrs["dialogue"] = dialogue
        except DialogueContractError as exc:
            raise serializers.ValidationError({"dialogue": str(exc)}) from exc
        from studio.services.shot_character_relations import shot_character_relations
        relations = attrs.get("character_relations")
        if relations is None:
            relations = ([{"role": row.role, "position": row.position, "character_id": str(row.character_id)}
                for row in self.instance.character_relations.all()] if self.instance else [])
        try:
            shot_character_relations({"character_relations": relations, "dialogue": dialogue})
        except DjangoValidationError as exc:
            raise serializers.ValidationError({"character_relations": exc.messages}) from exc
        identities = {row["character_id"] for row in relations}
        characters_by_id = {str(row.pk): row for row in CharacterBible.objects.filter(
            pk__in=identities, project_id=project_id)}
        if set(characters_by_id) != identities:
            raise serializers.ValidationError({"character_relations": "人物不存在或与分镜不属于同一项目"})
        for row in relations:
            if row["role"] == "speaker" and dialogue[row["position"]]["speaker_id"] != characters_by_id[row["character_id"]].stable_speaker_id:
                raise serializers.ValidationError({"character_relations": f"对白第{row['position'] + 1}行与明确发言者的 speaker_id 不一致"})
        mode = str(
            attrs.get(
                "conditioning_mode",
                getattr(self.instance, "conditioning_mode", ""),
            )
            or ""
        ).strip()
        # Unselected planning mode is valid; execution validates its own frozen contract.
        if media_type == Shot.MediaType.IMAGE:
            if mode and "conditioning_mode" in attrs:
                raise serializers.ValidationError({
                    "conditioning_mode": "图片镜头不接受视频条件输入模式",
                })
            attrs["conditioning_mode"] = ""
        return attrs


class DurationOverrideSerializer(serializers.Serializer):
    shot_id = serializers.UUIDField()
    duration_ms = serializers.IntegerField(min_value=1)




class WorkflowTemplateSerializer(serializers.ModelSerializer):
    owner = serializers.PrimaryKeyRelatedField(read_only=True)

    class Meta:
        model = WorkflowTemplate
        fields = "__all__"
        read_only_fields = ("id", "owner", "is_system", "created_at", "updated_at")

    def validate_definition(self, value):
        if not isinstance(value, dict) or not value:
            raise serializers.ValidationError("definition 必须是非空 JSON 对象")
        return value




class GenerationTaskTimelineEventSerializer(serializers.Serializer):
    event = serializers.CharField(read_only=True)
    status = serializers.ChoiceField(choices=GenerationTask.Status.choices, read_only=True)
    label = serializers.CharField(read_only=True)
    timestamp = serializers.DateTimeField(read_only=True)
    details = serializers.JSONField(read_only=True)


class GenerationTaskMediaUseSerializer(serializers.Serializer):
    reference_binding = serializers.JSONField(read_only=True, help_text="原准备逐使用对应：position为逻辑输入内0起序号；original_name原名；reference_name/name_disambiguated/input_slot为原策略保存的引用名、消歧标记及槽序号，无策略对应时为null；shot_reference为已校验确切使用、版本和设定修订，未记录为null。不查询当前版本或重新消歧。")
    item_key = serializers.CharField(read_only=True)
    input_key = serializers.CharField(read_only=True)
    kind = serializers.ChoiceField(choices=("image", "video", "audio"), read_only=True)
    asset_id = serializers.UUIDField(allow_null=True, read_only=True)
    artifact_id = serializers.UUIDField(allow_null=True, read_only=True)
    display_name = serializers.CharField(allow_blank=True, read_only=True)
    purpose = serializers.CharField(allow_null=True, read_only=True)
    paired_to = serializers.CharField(allow_null=True, read_only=True)
    source_sha256 = serializers.CharField(read_only=True)
    checksum = serializers.CharField(read_only=True)
    input_label = serializers.CharField(allow_null=True, read_only=True)
    ports = serializers.ListField(child=serializers.JSONField(), read_only=True)
    staging_status = serializers.ChoiceField(choices=("staged", "not_staged", "not_applicable"), read_only=True)
    preview_url = serializers.CharField(allow_blank=True, read_only=True)


class GenerationTaskParameterValueSerializer(serializers.Serializer):
    key = serializers.CharField(read_only=True)
    value = serializers.JSONField(read_only=True)
    value_source = serializers.CharField(read_only=True)
    rule = serializers.JSONField(read_only=True)


class GenerationTaskPromptSummarySerializer(serializers.Serializer):
    key = serializers.CharField(read_only=True)
    semantic_key = serializers.CharField(read_only=True)
    character_count = serializers.IntegerField(read_only=True)


class GenerationTaskInputParametersSerializer(serializers.Serializer):
    """Exact prepared parameter values and distinct media uses; no raw prompts."""
    preparation_id = serializers.UUIDField(read_only=True)
    attempt_id = serializers.UUIDField(read_only=True)
    execution_instance_version_id = serializers.UUIDField(read_only=True)
    workflow_version_id = serializers.UUIDField(read_only=True)
    request_sha256 = serializers.CharField(read_only=True)
    operation = serializers.CharField(read_only=True)
    output_kind = serializers.ChoiceField(choices=("image", "video", "audio"), read_only=True)
    parameters = GenerationTaskParameterValueSerializer(many=True, read_only=True)
    media_uses = GenerationTaskMediaUseSerializer(many=True, read_only=True)
    prompt_mode = serializers.ChoiceField(choices=ModelExecutionInstanceVersion.PromptMode.choices, read_only=True)
    prompt_profile_version_id = serializers.UUIDField(allow_null=True, read_only=True)
    prompt_channels = GenerationTaskPromptSummarySerializer(many=True, read_only=True)


class GenerationTaskPromptChannelSerializer(serializers.Serializer):
    key = serializers.CharField(read_only=True)
    semantic_key = serializers.CharField(read_only=True)
    text = serializers.CharField(allow_blank=True, read_only=True)
    character_count = serializers.IntegerField(read_only=True)
    value_source = serializers.CharField(read_only=True)


class GenerationTaskInputPromptSerializer(serializers.Serializer):
    """Lazily loaded frozen request channels, not proof of remote acceptance."""

    task_id = serializers.UUIDField(read_only=True)
    attempt_id = serializers.UUIDField(read_only=True)
    preparation_id = serializers.UUIDField(read_only=True)
    execution_instance_version_id = serializers.UUIDField(read_only=True)
    request_sha256 = serializers.CharField(read_only=True)
    prompt_mode = serializers.ChoiceField(choices=ModelExecutionInstanceVersion.PromptMode.choices, read_only=True)
    prompt_profile_version_id = serializers.UUIDField(allow_null=True, read_only=True)
    channels = GenerationTaskPromptChannelSerializer(many=True, read_only=True)
    declared_reference_labels = serializers.ListField(child=serializers.CharField(), read_only=True)


class GenerationTaskObservationSerializer(serializers.ModelSerializer):
    class Meta:
        from studio.models import GenerationTaskObservation
        model = GenerationTaskObservation
        fields = ("id", "task", "kind", "status", "claim_number", "claim_started_at", "started_at", "finished_at",
            "created_at", "request_sha256", "node_count", "link_count", "schema_sha256", "policy_sha256",
            "unload_models", "free_memory", "http_status", "error_code")
        read_only_fields = fields


class GenerationTaskExecutionSerializer(serializers.Serializer):
    production_run_id = serializers.UUIDField(read_only=True)
    stage_run_id = serializers.UUIDField(read_only=True)
    stage_key = serializers.CharField(read_only=True)
    stage_ordinal = serializers.IntegerField(read_only=True)
    unit_key = serializers.CharField(read_only=True)
    attempt_id = serializers.UUIDField(read_only=True)
    attempt_number = serializers.IntegerField(read_only=True)
    preparation_id = serializers.UUIDField(read_only=True)
    execution_instance_version_id = serializers.UUIDField(read_only=True)
    operation = serializers.CharField(read_only=True)


class GenerationTaskSerializer(serializers.ModelSerializer):
    execution = serializers.SerializerMethodField()
    project_title = serializers.CharField(source="project.title", read_only=True)
    input_parameters = serializers.SerializerMethodField()
    output_media = serializers.SerializerMethodField()
    completed_at = serializers.SerializerMethodField()
    duration_ms = serializers.SerializerMethodField()
    queue_wait_ms = serializers.SerializerMethodField()
    total_duration_ms = serializers.SerializerMethodField()
    timeline = serializers.SerializerMethodField()
    episode_id = serializers.UUIDField(read_only=True, allow_null=True)
    episode_number = serializers.SerializerMethodField()
    episode_title = serializers.SerializerMethodField()
    stalled = serializers.SerializerMethodField()
    retry_requires_reconciliation = serializers.SerializerMethodField()
    input_contract_status = serializers.SerializerMethodField()
    input_contract_code = serializers.SerializerMethodField()

    def get_input_contract_status(self, obj) -> str:
        return "available"

    def get_input_contract_code(self, obj) -> str:
        return ""

    class Meta:
        model = GenerationTask
        fields = (
            "id", "owner", "project", "project_title", "episode_id", "episode_number", "episode_title",
            "execution", "kind", "status", "priority", "progress", "attempts",
            "comfy_prompt_id", "client_id", "error_message", "queued_at", "started_at", "finished_at",
            "created_at", "updated_at", "submission_token", "submission_state",
            "submission_prepared_at", "submission_submitted_at",
            "heartbeat_at", "stalled_at", "stalled", "remote_state", "remote_error",
            "progress_source", "comfy_progress", "progress_node_id", "progress_updated_at",
            "progress_telemetry_state", "progress_telemetry_error",
            "deadline_started_at", "hard_deadline_at", "idle_deadline_at", "last_meaningful_progress_at",
            "input_parameters", "input_contract_status", "input_contract_code", "output_media",
            "completed_at", "duration_ms", "queue_wait_ms", "total_duration_ms", "timeline",
            "retry_requires_reconciliation",
        )
        read_only_fields = fields



    @staticmethod
    def _task_public_text(value, default: str = "") -> str:
        """Return a display identifier while reducing absolute paths to their basename."""
        if value is None:
            return default
        text = str(value)
        try:
            windows_path = PureWindowsPath(text)
            posix_path = PurePosixPath(text)
            if windows_path.is_absolute():
                return windows_path.name
            if posix_path.is_absolute():
                return posix_path.name
        except (TypeError, ValueError):
            return default
        return text


    def _task_asset_preview_url(self, asset: Asset) -> str:
        """Reuse the public asset URL policy only when its backing source is usable."""
        if asset.file:
            # SignedMediaView deliberately rejects ownerless uploaded files;
            # do not emit a token that can never resolve.
            if asset.owner_id is None:
                return ""
            try:
                if not asset.file.storage.exists(asset.file.name):
                    return ""
            except (OSError, ValueError, NotImplementedError):
                return ""
        elif asset.local_path:
            try:
                if not Path(asset.local_path).resolve().is_file():
                    return ""
            except (OSError, ValueError):
                return ""
        try:
            return AssetSerializer(context=self.context).get_preview_url(asset)
        except (OSError, ValueError):
            return ""

    @extend_schema_field(GenerationTaskInputParametersSerializer)
    def get_input_parameters(self, obj) -> dict:
        from django.core.exceptions import ValidationError as DjangoValidationError
        from studio.services.execution_workflow_inspection import inspect_prepared_task_inputs

        try:
            parameters = inspect_prepared_task_inputs(obj)
        except DjangoValidationError as exc:
            raise serializers.ValidationError({"input_parameters": exc.messages}) from exc
        # Names and usages are frozen. Only preview availability is current,
        # and never substitutes another asset or changed source bytes.
        request = self.context.get("request")
        user = getattr(request, "user", None)
        visible = {}
        if getattr(self.context.get("view"), "action", None) == "retrieve" and getattr(user, "is_authenticated", False):
            ids = [row["asset_id"] for row in parameters["media_uses"] if row["asset_id"]]
            queryset = Asset.objects.filter(pk__in=ids, is_active=True)
            if not user.is_staff:
                queryset = queryset.filter(Q(owner=user) | Q(owner__isnull=True))
            visible = {str(asset.pk): asset for asset in queryset}
        for item in parameters["media_uses"]:
            item["display_name"] = self._task_public_text(item["display_name"])
            asset = visible.get(item["asset_id"])
            if asset is not None and asset.checksum == item["checksum"]:
                item["preview_url"] = self._task_asset_preview_url(asset)
        return parameters

    def to_representation(self, instance):
        representation = super().to_representation(instance)
        view = self.context.get("view")
        if getattr(view, "action", None) == "list":
            # Polling responses expose the stable, path-free input_parameters
            # summary instead of raw worker payloads and verbose detail logs.
            representation.update({
                "error_message": "",
                "remote_error": "",
                "timeline": [],
            })
        return representation

    def get_stalled(self, obj) -> bool:
        if obj.stalled_at is not None:
            return True
        if obj.status not in {
            GenerationTask.Status.RUNNING,
            GenerationTask.Status.CANCEL_REQUESTED,
        }:
            return False
        last_activity = obj.heartbeat_at or obj.updated_at or obj.started_at
        if last_activity is None:
            return False
        threshold = float(_asset_runtime_limits()["runtime.task_stall_threshold_seconds"])
        return (timezone.now() - last_activity).total_seconds() > threshold


    @staticmethod
    def get_retry_requires_reconciliation(obj) -> bool:
        return bool(
            obj.status in {GenerationTask.Status.FAILED, GenerationTask.Status.CANCELLED}
            and not obj.outputs
            and obj.submission_token
            and obj.submission_state not in {
                GenerationTask.SubmissionState.TERMINAL_SUCCEEDED,
                GenerationTask.SubmissionState.TERMINAL_FAILED,
                GenerationTask.SubmissionState.TERMINAL_CANCELLED,
            }
        )


    def get_output_media(self, obj) -> list[dict]:
        if obj.status != GenerationTask.Status.SUCCEEDED:
            return []
        media = []
        if isinstance(obj.outputs, list):
            for index, item in enumerate(obj.outputs):
                kind = classify_task_output(item)
                if not kind:
                    continue
                token = signed_media_token(
                    purpose="task_output", object_id=obj.id, owner_id=obj.owner_id,
                    claims={"output_index": index},
                )
                preview_url = reverse("signed-media", kwargs={"token": token})
                provenance = item.get("provenance") if isinstance(item, dict) else None
                distribution = item.get("distribution") if isinstance(item, dict) else None
                observed_media = (
                    item.get("observed_media") if isinstance(item, dict) else None
                )
                provenance_url = ""
                if (
                    isinstance(provenance, dict)
                    and provenance.get("ai_generated") is True
                ):
                    provenance_token = signed_media_token(
                        purpose="task_provenance", object_id=obj.id, owner_id=obj.owner_id,
                        claims={"output_index": index},
                    )
                    provenance_url = reverse(
                        "signed-media", kwargs={"token": provenance_token},
                    )
                public_download_url = ""
                public_provenance_url = ""
                development_master_download = (
                    str(getattr(settings, "DEPLOYMENT_PROFILE", "development")).casefold()
                    != "production"
                )
                distribution_ready = (
                    isinstance(distribution, dict)
                    and distribution.get("status") == "ready"
                )
                if distribution_ready:
                    distribution_token = signed_media_token(
                        purpose="task_distribution", object_id=obj.id, owner_id=obj.owner_id,
                        claims={"output_index": index},
                    )
                    public_download_url = (
                        f"{reverse('signed-media', kwargs={'token': distribution_token})}?download=1"
                    )
                    distribution_provenance_token = signed_media_token(
                        purpose="task_distribution_provenance",
                        object_id=obj.id,
                        owner_id=obj.owner_id,
                        claims={"output_index": index},
                    )
                    public_provenance_url = reverse(
                        "signed-media", kwargs={"token": distribution_provenance_token},
                    )
                elif development_master_download and preview_url:
                    # Development is a production-workflow workbench, not a public
                    # release surface.  Let the owner download the exact ComfyUI
                    # master they are iterating on; production keeps requiring the
                    # independently rendered and verified distribution copy.
                    public_download_url = f"{preview_url}?download=1"
                media.append({
                    "index": index,
                    "kind": kind,
                    "filename": self._task_public_text(item["filename"]),
                    "preview_url": preview_url,
                    "download_url": public_download_url,
                    "ai_generated": True,
                    "provenance_url": provenance_url,
                    "artifact_sha256": (
                        self._task_public_text(provenance.get("artifact_sha256"))
                        if isinstance(provenance, dict) else ""
                    ),
                    "manifest_sha256": (
                        self._task_public_text(provenance.get("manifest_sha256"))
                        if isinstance(provenance, dict) else ""
                    ),
                    "observed_media": (
                        observed_media if isinstance(observed_media, dict) else {}
                    ),
                    "distribution_status": (
                        self._task_public_text(distribution.get("status"))
                        if isinstance(distribution, dict) else "not_prepared"
                    ) if distribution_ready or not development_master_download else "development_master",
                    "distribution_error": (
                        self._task_public_text(distribution.get("error"))
                        if isinstance(distribution, dict) else ""
                    ),
                    "public_provenance_url": public_provenance_url,
                    "public_artifact_sha256": (
                        self._task_public_text(distribution.get("artifact_sha256"))
                        if isinstance(distribution, dict) else ""
                    ),
                    "public_manifest_sha256": (
                        self._task_public_text(distribution.get("manifest_sha256"))
                        if isinstance(distribution, dict) else ""
                    ),
                    "public_label": (
                        self._task_public_text(distribution.get("label"))
                        if isinstance(distribution, dict) else ""
                    ),
                    "public_version": (
                        self._task_public_text(distribution.get("version"))
                        if isinstance(distribution, dict) else ""
                    ),
                    "promoted_asset_id": (
                        self._task_public_text(item.get("promoted_asset_id"))
                        if kind == "image" and isinstance(item, dict) else ""
                    ),
                })
        return media

    @extend_schema_field(serializers.DateTimeField(allow_null=True))
    def get_completed_at(self, obj):
        """Terminal completion time; a requeued failed attempt is not complete."""
        if obj.status not in {
            GenerationTask.Status.SUCCEEDED,
            GenerationTask.Status.FAILED,
            GenerationTask.Status.CANCELLED,
        }:
            return None
        return self._timeline_timestamp(obj.finished_at) if obj.finished_at else None

    @staticmethod
    def _duration_ms(started_at, finished_at) -> int | None:
        """Return a persisted interval in milliseconds without inventing missing time."""
        if started_at is None or finished_at is None or finished_at < started_at:
            return None
        return int((finished_at - started_at).total_seconds() * 1000)

    def get_duration_ms(self, obj) -> int | None:
        """GPU task execution time (worker start through terminal/attempt finish)."""
        return self._duration_ms(obj.started_at, obj.finished_at)

    def get_queue_wait_ms(self, obj) -> int | None:
        """Time spent waiting for a worker before the latest persisted start."""
        return self._duration_ms(obj.queued_at, obj.started_at)

    def get_total_duration_ms(self, obj) -> int | None:
        """End-to-end time from queue insertion through the persisted finish."""
        return self._duration_ms(obj.queued_at, obj.finished_at)

    @staticmethod
    def _timeline_timestamp(value) -> str:
        return serializers.DateTimeField().to_representation(value)

    @extend_schema_field(GenerationTaskTimelineEventSerializer(many=True))
    def get_timeline(self, obj) -> list[dict]:
        """Build a truthful lifecycle view from durable task audit timestamps only.

        GenerationTask does not currently persist every transient ComfyUI polling
        update.  Consequently this method deliberately exposes only transitions
        backed by an existing timestamp; it must not turn UI
        progress estimates into fabricated history.
        """
        events: list[tuple[object, int, dict]] = []

        def append_event(timestamp, order: int, *, event: str, status: str, label: str, details=None):
            if timestamp is None:
                return
            represented = self._timeline_timestamp(timestamp)
            events.append((timestamp, order, {
                "event": event,
                "status": status,
                "timestamp": represented,
                "label": label,
                "details": details or {},
            }))


        append_event(
            obj.queued_at,
            1,
            event="queued",
            status=GenerationTask.Status.QUEUED,
            label="任务已进入队列",
        )
        append_event(
            obj.started_at,
            2,
            event="started",
            status=GenerationTask.Status.RUNNING,
            label="工作线程已开始执行",
            details={"attempt": obj.attempts} if obj.attempts else {},
        )
        append_event(
            obj.submission_prepared_at,
            3,
            event="workflow_prepared",
            status=GenerationTask.Status.RUNNING,
            label="ComfyUI 工作流已准备",
        )
        append_event(
            obj.submission_submitted_at,
            4,
            event="comfyui_submitted",
            status=GenerationTask.Status.RUNNING,
            label="ComfyUI 提交已记录",
            details={
                "submission_state": obj.submission_state,
                **({"prompt_id": obj.comfy_prompt_id} if obj.comfy_prompt_id else {}),
            },
        )
        append_event(
            obj.stalled_at,
            5,
            event="stalled",
            status=(
                GenerationTask.Status.RUNNING
                if obj.status in {
                    GenerationTask.Status.RUNNING,
                    GenerationTask.Status.CANCEL_REQUESTED,
                }
                else obj.status
            ),
            label="ComfyUI 执行已停止推进",
            details={"remote_state": obj.remote_state, **({"error": obj.remote_error} if obj.remote_error else {})},
        )

        terminal_events = {
            GenerationTask.Status.SUCCEEDED: ("succeeded", "任务生成完成"),
            GenerationTask.Status.FAILED: ("failed", "任务执行失败"),
            GenerationTask.Status.CANCELLED: ("cancelled", "任务已取消"),
        }
        if obj.finished_at:
            if obj.status in terminal_events:
                event, label = terminal_events[obj.status]
            elif obj.status == GenerationTask.Status.QUEUED:
                # A retryable pre-submission attempt records finished_at before
                # returning the same task to the queue.
                event, label = "attempt_failed_requeued", "本次尝试失败，任务已重新排队"
            else:
                event, label = "attempt_finished", "本次执行已结束"
            append_event(
                obj.finished_at,
                6,
                event=event,
                status=obj.status,
                label=label,
            )

        events.sort(key=lambda item: (item[0], item[1]))
        return [item[2] for item in events]

    @extend_schema_field(GenerationTaskExecutionSerializer(allow_null=True))
    def get_execution(self, obj) -> dict | None:
        from django.core.exceptions import ValidationError as DjangoValidationError
        from studio.services.execution_production_workflow_queue import _linked_task_attempt
        try:
            attempt = _linked_task_attempt(obj)
        except DjangoValidationError:
            # A list can still expose a broken task's real lifecycle. Never
            # manufacture its business identity from a trace, cache or JSON.
            return None
        stage = attempt.stage_run
        return {
            "production_run_id": str(stage.production_run_id),
            "stage_run_id": str(stage.pk), "stage_key": stage.stage_key,
            "stage_ordinal": stage.ordinal, "unit_key": attempt.unit_key,
            "attempt_id": str(attempt.pk), "attempt_number": attempt.attempt_number,
            "preparation_id": str(attempt.preparation_id),
            "execution_instance_version_id": str(attempt.execution_instance_version_id),
            "operation": attempt.operation,
        }

    def get_episode_number(self, obj) -> int | None:
        return obj.episode.number if obj.episode_id else None

    def get_episode_title(self, obj) -> str:
        return obj.episode.title if obj.episode_id else ""


class GenerationTaskListSerializer(GenerationTaskSerializer):
    """List projection that never invents inputs for damaged task relations.

    Actual preparations keep the same safe input projection as task detail.
    Missing/damaged relations remain explicit and cannot revive an old path.
    """

    INPUT_CONTRACT_AVAILABLE = "available"
    INPUT_CONTRACT_UNAVAILABLE = "unavailable"
    INPUT_CONTRACT_FROZEN_INVALID = "frozen_contract_invalid"

    input_parameters = serializers.SerializerMethodField(
        help_text=(
            "本次实际准备与回执可验证时返回安全输入投影；缺失或损坏时返回 null。 "
            "Verified prepared inputs only; absent or invalid facts return null."
        ),
    )
    input_contract_status = serializers.SerializerMethodField(
        help_text=(
            "输入合同投影状态：available 可展示，unavailable 不会回填或推断。 "
            "Input contract projection status: available or unavailable; missing facts "
            "are never backfilled or inferred."
        ),
    )
    input_contract_code = serializers.SerializerMethodField(
        help_text=(
            "不可展示时的稳定原因码；available 时为空字符串。 "
            "Stable reason code when unavailable; blank when available."
        ),
    )

    def _list_input_contract_projection(self, obj) -> tuple[str, str, dict | None]:
        cache = self.context.setdefault("_task_list_input_contract_projection_cache", {})
        cache_key = str(obj.id)
        if cache_key in cache:
            return cache[cache_key]
        try:
            projection = (
                self.INPUT_CONTRACT_AVAILABLE,
                "",
                super().get_input_parameters(obj),
            )
        except serializers.ValidationError:
            projection = (
                self.INPUT_CONTRACT_UNAVAILABLE,
                self.INPUT_CONTRACT_FROZEN_INVALID,
                None,
            )
        cache[cache_key] = projection
        return projection

    @extend_schema_field(GenerationTaskInputParametersSerializer(allow_null=True))
    def get_input_parameters(self, obj) -> dict | None:
        return self._list_input_contract_projection(obj)[2]

    @extend_schema_field(serializers.ChoiceField(
        choices=(INPUT_CONTRACT_AVAILABLE, INPUT_CONTRACT_UNAVAILABLE),
        read_only=True,
    ))
    def get_input_contract_status(self, obj) -> str:
        return self._list_input_contract_projection(obj)[0]

    @extend_schema_field(serializers.ChoiceField(
        choices=(
            "",
            INPUT_CONTRACT_FROZEN_INVALID,
        ),
        allow_blank=True,
        read_only=True,
    ))
    def get_input_contract_code(self, obj) -> str:
        return self._list_input_contract_projection(obj)[1]


class ComfyUIWorkflowExportSerializer(serializers.Serializer):
    """Read-only description of a task's API graph or importable UI canvas."""

    task_id = serializers.UUIDField(read_only=True)
    attempt_id = serializers.UUIDField(read_only=True)
    preparation_id = serializers.UUIDField(read_only=True)
    execution_instance_version_id = serializers.UUIDField(read_only=True)
    workflow_version_id = serializers.UUIDField(read_only=True)
    request_sha256 = serializers.CharField(read_only=True)
    definition_sha256 = serializers.CharField(read_only=True)
    graph_state = serializers.ChoiceField(choices=("prepared", "staged"), read_only=True)
    missing_receipt_count = serializers.IntegerField(read_only=True)
    format = serializers.ChoiceField(choices=("comfyui_api", "comfyui_ui"), read_only=True)
    filename = serializers.CharField(read_only=True)
    content_hash = serializers.CharField(read_only=True)
    workflow = serializers.JSONField(read_only=True)
    import_ready = serializers.BooleanField(read_only=True)
    open_reason = serializers.CharField(read_only=True)
    contains_staged_placeholders = serializers.BooleanField(read_only=True)
    staged_placeholder_count = serializers.IntegerField(read_only=True)
    comfyui_url = serializers.URLField(allow_blank=True, read_only=True)
    safe_to_open = serializers.BooleanField(read_only=True)
    direct_open_supported = serializers.BooleanField(read_only=True)
    direct_open_url = serializers.URLField(allow_null=True, read_only=True)
    open_mode = serializers.ChoiceField(choices=("download_import",), read_only=True)
    download_url = serializers.URLField(read_only=True)
    available_formats = serializers.ListField(
        child=serializers.ChoiceField(choices=("comfyui_api", "comfyui_ui")),
        read_only=True,
    )
    api_download_url = serializers.URLField(read_only=True)
    ui_download_url = serializers.URLField(read_only=True)










class GenerationTaskRetrySerializer(serializers.Serializer):
    extended_generation_confirmed = serializers.BooleanField(
        default=False,
        help_text="当前队列超过推荐值时的显式继续确认；不会改变原任务参数。",
    )










class NextEpisodeSerializer(serializers.Serializer):
    title = serializers.CharField(max_length=200)
    synopsis = serializers.CharField(required=False, allow_blank=True, default="")
    script = serializers.CharField(required=False, allow_blank=True, default="")


































class APIValidationErrorSerializer(serializers.Serializer):
    """The single envelope produced by ``api_exception_handler`` for 400s."""

    status_code = serializers.IntegerField()
    code = serializers.CharField(max_length=64)
    details = serializers.JSONField()


class APIValidationErrorEnvelopeSerializer(serializers.Serializer):
    error = APIValidationErrorSerializer()


class StrictSerializer(serializers.Serializer):
    """Serializer that rejects unknown JSON keys instead of ignoring them."""

    def to_internal_value(self, data):
        if not isinstance(data, dict):
            raise serializers.ValidationError("必须是 JSON 对象")
        unknown = set(data) - set(self.fields)
        if unknown:
            raise serializers.ValidationError({
                key: "不支持的字段" for key in sorted(unknown)
            })
        return super().to_internal_value(data)


class PromptFieldTranslationSerializer(serializers.ModelSerializer):
    runtime_metadata = serializers.SerializerMethodField()
    revision_history = serializers.SerializerMethodField()
    context_current = serializers.SerializerMethodField()
    canonical_value = serializers.SerializerMethodField()
    confirmation_target = serializers.SerializerMethodField()

    @extend_schema_field(serializers.ChoiceField(choices=("field_review", "canonical_glossary")))
    def get_confirmation_target(self, obj):
        from studio.services.canonical_glossary import is_canonical_name
        return "canonical_glossary" if is_canonical_name(obj.scope_type, obj.field_key) else "field_review"

    @extend_schema_field(serializers.CharField(allow_null=True))
    def get_canonical_value(self, obj):
        from studio.services.canonical_glossary import project_glossary
        return project_glossary(obj.project).get(obj.source_text) if self.get_confirmation_target(obj) == "canonical_glossary" else None

    @extend_schema_field(serializers.DictField())
    def get_runtime_metadata(self, obj: PromptFieldTranslation) -> dict:
        return redact_sensitive_bindings(obj.runtime_metadata or {})

    @extend_schema_field(serializers.ListField(child=serializers.DictField()))
    def get_revision_history(self, obj: PromptFieldTranslation) -> list:
        value = obj.revision_history if isinstance(obj.revision_history, list) else []
        return redact_sensitive_bindings(value)

    @extend_schema_field(serializers.BooleanField())
    def get_context_current(self, obj: PromptFieldTranslation) -> bool:
        """Report whether this candidate can still be reviewed or compiled.

        The source text and the canonical terminology/identity context are both
        part of the translation contract.  Returning the effective state keeps
        the review UI aligned with the compiler after a glossary or Bible-name
        change, without mutating audit history during a GET request.
        """

        try:
            current_source = resolve_current_source(
                project=obj.project,
                scope_type=obj.scope_type,
                scope_id=obj.scope_id,
                field_key=obj.field_key,
            )
        except TranslationValidationError:
            return False
        return translation_entry_context_current(
            obj, project=obj.project, current_source=current_source,
        )

    class Meta:
        model = PromptFieldTranslation
        fields = (
            "id", "project", "scope_type", "scope_id", "field_key",
            "source_text", "translated_text", "status", "revision",
            "provider", "model", "error_message", "runtime_metadata",
            "revision_history", "context_current", "confirmation_target", "canonical_value",
            "confirmed_by", "confirmed_at", "created_at", "updated_at",
        )
        read_only_fields = fields


class PromptTranslationListQuerySerializer(serializers.Serializer):
    episode = serializers.UUIDField(required=False)
    shot = serializers.UUIDField(required=False)
    status = serializers.ChoiceField(
        choices=PromptFieldTranslation.Status.choices,
        required=False,
    )


class PromptTranslationEntryRequestSerializer(serializers.Serializer):
    field_key = serializers.CharField(max_length=160)
    source_text = serializers.CharField(trim_whitespace=False)
    scope_type = serializers.ChoiceField(
        choices=PromptFieldTranslation.ScopeType.choices,
        default=PromptFieldTranslation.ScopeType.PROJECT,
    )
    scope_id = serializers.UUIDField(required=False, allow_null=True)

    def validate_source_text(self, value):
        maximum = translation_text_max_characters()
        if len(value) > maximum:
            raise serializers.ValidationError(
                f"不能超过当前安全边界 {maximum} 个字符",
            )
        return value

    def validate(self, attrs):
        from studio.services.prompt_translation import field_key_allowed

        scope_type = attrs["scope_type"]
        if not field_key_allowed(scope_type, attrs["field_key"]):
            raise serializers.ValidationError({
                "field_key": f"{scope_type} scope 不支持该提示词字段",
            })
        if scope_type != PromptFieldTranslation.ScopeType.PROJECT and not attrs.get("scope_id"):
            raise serializers.ValidationError({"scope_id": "非 project scope 必须提供 scope_id"})
        return attrs




class PromptTranslationManualBatchRequestSerializer(serializers.Serializer):
    entries = PromptTranslationEntryRequestSerializer(many=True, allow_empty=False)

    def validate_entries(self, entries):
        maximum = translation_request_max_entries()
        if len(entries) > maximum:
            raise serializers.ValidationError(
                f"单次最多创建 {maximum} 个手工英文候选",
            )
        seen = set()
        for item in entries:
            key = (item["scope_type"], item.get("scope_id"), item["field_key"])
            if key in seen:
                raise serializers.ValidationError("同一批次不能重复提交同一字段")
            seen.add(key)
        return entries


class PromptTranslationConfirmSerializer(serializers.Serializer):
    expected_revision = serializers.IntegerField(min_value=1)
    translation = serializers.CharField(
        trim_whitespace=False, required=False,
    )

    def validate_translation(self, value):
        maximum = translation_text_max_characters()
        if len(value) > maximum:
            raise serializers.ValidationError(
                f"不能超过当前安全边界 {maximum} 个字符",
            )
        return value


class CanonicalGlossaryConfirmSerializer(PromptTranslationConfirmSerializer):
    expected_canonical = serializers.CharField(allow_null=True, trim_whitespace=False)
    expected_project_updated_at = serializers.DateTimeField()
    translation = serializers.CharField(trim_whitespace=False)

    def to_internal_value(self, data):
        if not isinstance(data, dict) or set(data) != {"expected_revision", "expected_canonical", "expected_project_updated_at", "translation"}:
            raise serializers.ValidationError({"non_field_errors": ["需明确候选修订、所见术语值和最终译名"]})
        if type(data["expected_revision"]) is not int or not isinstance(data["translation"], str) or (
            data["expected_canonical"] is not None and not isinstance(data["expected_canonical"], str)
        ):
            raise serializers.ValidationError({"non_field_errors": ["术语确认字段类型无效"]})
        return super().to_internal_value(data)


class CanonicalGlossaryConfirmResponseSerializer(serializers.Serializer):
    entry = PromptFieldTranslationSerializer()
    project_id = serializers.UUIDField()
    source_text = serializers.CharField()
    canonical_english = serializers.CharField()
    previous_canonical = serializers.CharField(allow_null=True)
    updated_at = serializers.DateTimeField()


class PromptTranslationConflictErrorSerializer(serializers.Serializer):
    status_code = serializers.IntegerField()
    code = serializers.CharField()
    details = serializers.DictField()


class PromptTranslationConflictErrorResponseSerializer(serializers.Serializer):
    error = PromptTranslationConflictErrorSerializer()


class AssemblyJobSerializer(serializers.ModelSerializer):
    source_media_count = serializers.SerializerMethodField()
    output_url = serializers.SerializerMethodField()
    download_url = serializers.SerializerMethodField()
    provenance_url = serializers.SerializerMethodField()
    ai_generated = serializers.SerializerMethodField()

    class Meta:
        model = AssemblyJob
        fields = "__all__"
        read_only_fields = tuple(field.name for field in AssemblyJob._meta.fields)

    def get_source_media_count(self, obj) -> int:
        count = getattr(obj, "_source_media_count", None)
        return count if count is not None else obj.source_items.count()

    def get_output_url(self, obj) -> str:
        if (
            obj.status != AssemblyJob.Status.SUCCEEDED
            or not obj.output_path
        ):
            return ""
        token = signed_media_token(purpose="assembly", object_id=obj.id, owner_id=obj.owner_id)
        return reverse("signed-media", kwargs={"token": token})

    def get_download_url(self, obj) -> str:
        output_url = self.get_output_url(obj)
        return f"{output_url}?download=1" if output_url else ""

    @staticmethod
    def get_ai_generated(obj) -> bool | None:
        """Recorded model source, not live integrity verification or a human-origin claim."""
        if obj.status != AssemblyJob.Status.SUCCEEDED:
            return None
        count = getattr(obj, "_model_source_count", None)
        if count is not None:
            return True if count > 0 else None
        return True if obj.source_items.filter(
            generated_artifact__generation_attempt__isnull=False,
        ).exists() else None

    def get_provenance_url(self, obj) -> str:
        if (
            obj.status != AssemblyJob.Status.SUCCEEDED
            or not obj.output_path
        ):
            return ""
        try:
            if not assembly_manifest_path(obj).is_file():
                return ""
        except (OSError, ProvenanceError):
            return ""
        token = signed_media_token(
            purpose="assembly_provenance", object_id=obj.id, owner_id=obj.owner_id,
        )
        return reverse("signed-media", kwargs={"token": token})





class ReleaseBundleBatchListSerializer(serializers.ListSerializer):
    def to_representation(self, data):
        from studio.models import ProductionRunPlan
        rows = list(data.all() if hasattr(data, "all") else data)
        run_ids = {row.production_run_id for row in rows if row.status != ReleaseBundle.Status.DRAFT}
        plans = ProductionRunPlan.objects.filter(run_id__in=run_ids).select_related("run__project").only(
            "run_id", "source", "stage_catalog_key", "stage_catalog_version", "stage_catalog_sha256",
            "definition", "definition_schema_version", "definition_size_bytes", "definition_sha256",
            "run__id", "run__project_id", "run__plan_source", "run__recipe_version_id", "run__project__owner_id",
        )
        self.child._release_run_plan_records = {row.run_id: row for row in plans}
        return super().to_representation(rows)


class ReleaseBundleSerializer(serializers.ModelSerializer):
    verification_status = serializers.SerializerMethodField()

    class Meta:
        model = ReleaseBundle
        list_serializer_class = ReleaseBundleBatchListSerializer
        fields = (
            "id", "owner", "project", "production_run", "created_by", "version", "title", "notes",
            "status", "selection", "manifest", "manifest_sha256", "sealed_at",
            "released_at", "verification_status", "created_at", "updated_at",
        )
        read_only_fields = (
            "id", "owner", "project", "production_run", "created_by", "version", "status", "manifest",
            "manifest_sha256", "sealed_at", "released_at", "verification_status",
            "created_at", "updated_at",
        )

    def get_verification_status(self, obj) -> str:
        if obj.status == ReleaseBundle.Status.DRAFT:
            return "draft"
        try:
            from studio.services.release_bundles import verified_release_bundle

            verified_release_bundle(obj, run_plan_records=getattr(self, "_release_run_plan_records", None))
            return "verified"
        except (ValueError, TypeError):
            return "invalid"
