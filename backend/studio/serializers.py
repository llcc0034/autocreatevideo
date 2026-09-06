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
    DeferredGenerationRequest,
    Episode,
    GeneratedWorkflow,
    GenerationTask,
    PromptFieldTranslation,
    PromptArtifact,
    PropBible,
    ReleaseBundle,
    SceneBible,
    SeriesProject,
    Shot,
    Style,
    WorkflowTemplate,
)
from studio.media_signing import classify_task_output, signed_media_token
from studio.services.provenance import ProvenanceError, assembly_manifest_path
from studio.services.reference_bindings import (
    ReferenceBindingError,
    normalize_shot_reference_bindings,
)
from studio.services.media_capabilities import SAFE_CONDITIONING_IDENTIFIER_PATTERN
from studio.services.dialogue import (
    DialogueContractError,
    normalize_dialogue,
    validate_dialogue_timing,
)
from studio.services.prompt_compiler_contract import PROMPT_COMPILER_VERSION
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
from studio.services.production_batch_slots import (
    build_production_batch_slots,
    effective_batch_snapshot_for_task,
)
from studio.services.bible_reference_contract import (
    BIBLE_IMAGE_GENERATION_PURPOSE,
    RETIRED_BIBLE_IMAGE_CANDIDATE_PURPOSE,
    RETIRED_BIBLE_IMAGE_CANDIDATE_SCHEMA_VERSION,
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
    canonical_glossary_max_entities,
    translation_promotion_max_entries,
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
    "扩展元数据对象。script_bible_v1 是完整剧本候选 apply 专属保留成员；"
    "通用创建、更新和 sync-bibles 不得注入、替换或删除。"
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
        "已审核的 Bible 参考图是不可变生产事实；"
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


class StyleSerializer(serializers.ModelSerializer):
    owner = serializers.PrimaryKeyRelatedField(read_only=True)
    asset_count = serializers.IntegerField(read_only=True)

    class Meta:
        model = Style
        fields = "__all__"
        read_only_fields = ("id", "owner", "is_system", "created_at", "updated_at")


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
        return style
    def validate_kind(self, value):
        if self.instance is not None and value != self.instance.kind:
            editable_kinds = {
                Asset.Kind.CHARACTER,
                Asset.Kind.SCENE,
                Asset.Kind.PROP,
            }
            if value not in editable_kinds or not (
                self.instance.kind in editable_kinds
                or self.instance.kind == Asset.Kind.REFERENCE
            ):
                raise serializers.ValidationError(
                    "只允许在人物、场景和道具图片之间更正类别；其他素材类型不可转换。"
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
            attrs["source_info"] = "用户本地上传"
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
            project = attrs.get("project") or getattr(self.instance, "project", None)
            raw_name = attrs.get("name", getattr(self.instance, "name", ""))
            normalized_name = unicodedata.normalize(
                "NFKC", str(raw_name or ""),
            ).strip().casefold()
            if project is not None and normalized_name:
                for bible_model in (CharacterBible, SceneBible, PropBible):
                    queryset = bible_model.objects.filter(project=project).only("id", "name")
                    if isinstance(self.instance, bible_model):
                        queryset = queryset.exclude(pk=self.instance.pk)
                    if any(
                        unicodedata.normalize("NFKC", str(item.name or "")).strip().casefold()
                        == normalized_name
                        for item in queryset
                    ):
                        raise serializers.ValidationError({
                            "name": "项目内人物、场景和道具名称不可重复；请修改名称后再保存。",
                        })
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


class CharacterBibleSerializer(ProjectOwnedSerializer):
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


class SceneBibleSerializer(ProjectOwnedSerializer):
    metadata = serializers.JSONField(
        required=False, help_text=SCRIPT_BIBLE_METADATA_HELP,
    )

    class Meta:
        model = SceneBible
        fields = "__all__"
        read_only_fields = ("id", "created_at", "updated_at")


class PropBibleSerializer(ProjectOwnedSerializer):
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


class ReferenceEntityBindingSchema(serializers.Serializer):
    entity_id = serializers.UUIDField()
    asset_id = serializers.UUIDField()


class ReferenceVideoBindingSchema(serializers.Serializer):
    asset_id = serializers.UUIDField()
    purpose = serializers.RegexField(SAFE_CONDITIONING_IDENTIFIER_PATTERN)
    use_embedded_audio = serializers.BooleanField()
    audio_asset_id = serializers.UUIDField(required=False)
    audio_purpose = serializers.RegexField(
        SAFE_CONDITIONING_IDENTIFIER_PATTERN,
        required=False,
    )


class ReferenceAudioBindingSchema(serializers.Serializer):
    asset_id = serializers.UUIDField()
    purpose = serializers.RegexField(SAFE_CONDITIONING_IDENTIFIER_PATTERN)


class ReferenceBindingsSchema(serializers.Serializer):
    """OpenAPI shape only; runtime validation remains the strict canonical service."""

    first_frame_asset_id = serializers.UUIDField(required=False)
    last_frame_asset_id = serializers.UUIDField(required=False)
    first_frame_reference_asset_id = serializers.UUIDField(required=False)
    characters = ReferenceEntityBindingSchema(many=True, required=False)
    scene = ReferenceEntityBindingSchema(required=False)
    props = ReferenceEntityBindingSchema(many=True, required=False)
    videos = ReferenceVideoBindingSchema(many=True, required=False)
    audios = ReferenceAudioBindingSchema(many=True, required=False)


@extend_schema_field(ReferenceBindingsSchema)
class ReferenceBindingsJSONField(serializers.JSONField):
    """Preserve raw JSON so unknown keys still reach strict service validation."""


class ShotSerializer(serializers.ModelSerializer):
    reference_bindings = ReferenceBindingsJSONField(required=False)
    include_audio = serializers.BooleanField(required=False)
    duration_ms = serializers.IntegerField(
        min_value=1, required=False,
    )

    class Meta:
        model = Shot
        fields = "__all__"
        read_only_fields = ("id", "created_at", "updated_at")

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
        if self.instance is None and "duration_ms" not in attrs:
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
        for field in ("characters", "props"):
            values = attrs.get(field)
            if values is not None and any(value.project_id != project_id for value in values):
                raise serializers.ValidationError({field: "条目与分镜不属于同一项目"})
        reference_assets = attrs.get("reference_assets")
        user = self.context["request"].user
        if reference_assets is not None and any(
            _is_project_cover_asset(asset) for asset in reference_assets
        ):
            raise serializers.ValidationError({
                "reference_assets": "连续剧封面是项目展示专用素材，不能用于分镜生成。",
            })
        if reference_assets is not None and not user.is_staff:
            if any(asset.owner_id not in (None, user.id) for asset in reference_assets):
                raise serializers.ValidationError({"reference_assets": "不能使用其他用户的素材"})
        characters = attrs.get("characters")
        if characters is None and self.instance:
            characters = self.instance.characters.all()
        speaker_ids = {item.stable_speaker_id for item in (characters or [])}
        dialogue = attrs.get("dialogue", getattr(self.instance, "dialogue", []))
        try:
            attrs["dialogue"] = validate_dialogue_timing(
                dialogue, duration_ms=duration_ms,
            )
            dialogue = attrs["dialogue"]
        except DialogueContractError as exc:
            raise serializers.ValidationError({"dialogue": str(exc)}) from exc
        unknown = {item["speaker_id"] for item in dialogue if item.get("speaker_id") not in speaker_ids}
        if unknown:
            raise serializers.ValidationError({"dialogue": f"未绑定的人物 speaker_id: {', '.join(sorted(unknown))}"})
        mode = str(
            attrs.get(
                "conditioning_mode",
                getattr(self.instance, "conditioning_mode", ""),
            )
            or ""
        ).strip()
        if media_type == Shot.MediaType.VIDEO and not mode:
            settings_value = (
                episode.project.production_settings
                if isinstance(episode.project.production_settings, dict)
                else {}
            )
            mode = str(settings_value.get("default_conditioning_mode") or "").strip()
            if not mode:
                raise serializers.ValidationError({
                    "conditioning_mode": "视频镜头必须明确选择已配置的条件输入模式",
                })
            attrs["conditioning_mode"] = mode
        if media_type == Shot.MediaType.IMAGE:
            if mode and "conditioning_mode" in attrs:
                raise serializers.ValidationError({
                    "conditioning_mode": "图片镜头不接受视频条件输入模式",
                })
            attrs["conditioning_mode"] = ""
        bindings = attrs.get(
            "reference_bindings", getattr(self.instance, "reference_bindings", {}),
        )
        scene = attrs.get("scene", getattr(self.instance, "scene", None))
        characters = attrs.get("characters")
        if characters is None:
            characters = list(self.instance.characters.all()) if self.instance else []
        props = attrs.get("props")
        if props is None:
            props = list(self.instance.props.all()) if self.instance else []
        shot_assets = attrs.get("reference_assets")
        if shot_assets is None:
            shot_assets = list(self.instance.reference_assets.all()) if self.instance else []
        if media_type == Shot.MediaType.IMAGE:
            if bindings and "reference_bindings" in attrs:
                raise serializers.ValidationError({
                    "reference_bindings": "图片镜头不接受视频条件输入绑定",
                })
            attrs["reference_bindings"] = {}
            return attrs
        try:
            attrs["reference_bindings"] = normalize_shot_reference_bindings(
                mode=mode, bindings=bindings, project=episode.project,
                characters=list(characters), scene=scene, props=list(props),
                shot_reference_assets=list(shot_assets), user=user,
            )
        except ReferenceBindingError as exc:
            raise serializers.ValidationError({"reference_bindings": str(exc)}) from exc
        return attrs


class BibleSyncBaseSerializer(serializers.Serializer):
    id = serializers.UUIDField(required=False)
    slug = serializers.SlugField(max_length=80)
    name = serializers.CharField(max_length=160)
    description = serializers.CharField(required=False, allow_blank=True)
    visual_identity = serializers.CharField(required=False, allow_blank=True)
    negative_constraints = serializers.CharField(required=False, allow_blank=True)
    anchor_asset = serializers.PrimaryKeyRelatedField(
        queryset=Asset.objects.all(), required=False, allow_null=True,
    )
    reference_assets = serializers.PrimaryKeyRelatedField(
        queryset=Asset.objects.all(), many=True, required=False,
    )
    metadata = serializers.JSONField(
        required=False, help_text=SCRIPT_BIBLE_METADATA_HELP,
    )
    is_locked = serializers.BooleanField(required=False)

    def validate_metadata(self, value):
        if not isinstance(value, dict):
            raise serializers.ValidationError("metadata 必须是 JSON 对象")
        return value


class CharacterBibleSyncItemSerializer(BibleSyncBaseSerializer):
    stable_speaker_id = serializers.RegexField(r"^S[1-9][0-9]*$", max_length=16)
    role = serializers.CharField(max_length=120, required=False, allow_blank=True)
    age_appearance = serializers.CharField(max_length=120, required=False, allow_blank=True)
    face_identity = serializers.CharField(required=False, allow_blank=True)
    wardrobe = serializers.CharField(required=False, allow_blank=True)
    voice_profile = serializers.CharField(required=False, allow_blank=True)
    personality = serializers.CharField(required=False, allow_blank=True)

    def validate_metadata(self, value):
        value = super().validate_metadata(value)
        try:
            validate_visual_reference_policy_metadata(value)
        except VisualReferencePolicyError as exc:
            raise serializers.ValidationError(str(exc)) from exc
        return value


class SceneBibleSyncItemSerializer(BibleSyncBaseSerializer):
    location = serializers.CharField(max_length=200, required=False, allow_blank=True)
    time_of_day = serializers.CharField(max_length=80, required=False, allow_blank=True)
    lighting = serializers.CharField(required=False, allow_blank=True)
    color_palette = serializers.CharField(required=False, allow_blank=True)
    ambient_sound = serializers.CharField(required=False, allow_blank=True)


class PropBibleSyncItemSerializer(BibleSyncBaseSerializer):
    material = serializers.CharField(max_length=160, required=False, allow_blank=True)
    distinctive_marks = serializers.CharField(required=False, allow_blank=True)
    continuity_state = serializers.CharField(required=False, allow_blank=True)




class PromptArtifactSerializer(serializers.ModelSerializer):
    compiler_current = serializers.SerializerMethodField()

    @staticmethod
    def get_compiler_current(obj) -> bool:
        try:
            version = int((obj.structured_payload or {}).get("prompt_compiler_version", 0))
        except (TypeError, ValueError):
            return False
        return version == PROMPT_COMPILER_VERSION

    class Meta:
        model = PromptArtifact
        fields = "__all__"
        read_only_fields = tuple(field.name for field in PromptArtifact._meta.fields)


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




class GeneratedWorkflowSerializer(serializers.ModelSerializer):
    class Meta:
        model = GeneratedWorkflow
        fields = "__all__"
        read_only_fields = tuple(field.name for field in GeneratedWorkflow._meta.fields)




class GenerationTaskTimelineEventSerializer(serializers.Serializer):
    event = serializers.CharField(read_only=True)
    status = serializers.ChoiceField(choices=GenerationTask.Status.choices, read_only=True)
    label = serializers.CharField(read_only=True)
    timestamp = serializers.DateTimeField(read_only=True)
    occurred_at = serializers.DateTimeField(
        read_only=True,
        help_text="Deprecated compatibility alias of timestamp.",
    )
    details = serializers.JSONField(read_only=True)


class GenerationTaskReferenceAssetSummarySerializer(serializers.Serializer):
    """Public, path-free summary of one persisted task-related media asset."""

    asset_id = serializers.CharField(allow_blank=True, allow_null=True, read_only=True)
    name = serializers.CharField(allow_blank=True, read_only=True)
    category = serializers.CharField(allow_blank=True, read_only=True)
    input_slot = serializers.IntegerField(allow_null=True, read_only=True)
    input_label = serializers.CharField(allow_blank=True, read_only=True)
    project_slot = serializers.IntegerField(allow_null=True, read_only=True)
    entity_id = serializers.CharField(allow_blank=True, allow_null=True, read_only=True)
    entity_name = serializers.CharField(allow_blank=True, read_only=True)
    entity_label = serializers.CharField(allow_blank=True, read_only=True)
    checksum = serializers.CharField(allow_blank=True, read_only=True)
    use_embedded_audio = serializers.BooleanField(read_only=True)
    audio_asset_id = serializers.CharField(allow_blank=True, allow_null=True, read_only=True)
    audio_input_label = serializers.CharField(allow_blank=True, allow_null=True, read_only=True)
    audio_name = serializers.CharField(allow_blank=True, read_only=True)
    audio_checksum = serializers.CharField(allow_blank=True, read_only=True)
    source_fps = serializers.FloatField(allow_null=True, read_only=True)
    effective_fps = serializers.IntegerField(allow_null=True, read_only=True)
    effective_duration_ms = serializers.IntegerField(allow_null=True, read_only=True)
    cfr_guaranteed = serializers.BooleanField(allow_null=True, read_only=True)
    paired_audio_duration_aligned = serializers.BooleanField(allow_null=True, read_only=True)
    media_kind = serializers.CharField(
        read_only=True, help_text="Browser media kind: image, video, or audio.",
    )
    display_name = serializers.CharField(allow_blank=True, read_only=True)
    preview_url = serializers.CharField(allow_blank=True, read_only=True)


class GenerationTaskInputParametersSerializer(serializers.Serializer):
    """Stable task-detail contract assembled from immutable execution inputs."""

    model = serializers.CharField(allow_blank=True, read_only=True)
    model_profile = serializers.CharField(allow_blank=True, read_only=True)
    conditioning_mode = serializers.CharField(allow_blank=True, read_only=True)
    effective_conditioning_mode = serializers.CharField(allow_blank=True, read_only=True)
    workflow_mode = serializers.CharField(allow_blank=True, read_only=True)
    template_slug = serializers.CharField(allow_blank=True, read_only=True)
    source_workflow = serializers.CharField(allow_blank=True, read_only=True)
    workflow_parameters = serializers.JSONField(read_only=True)
    prompt_template_key = serializers.CharField(allow_blank=True, read_only=True)
    resolution_profile = serializers.CharField(allow_blank=True, read_only=True)
    width = serializers.IntegerField(read_only=True)
    height = serializers.IntegerField(read_only=True)
    resolution_multiple = serializers.IntegerField(read_only=True)
    fps = serializers.IntegerField(read_only=True)
    requested_duration_ms = serializers.IntegerField(read_only=True)
    effective_duration_ms = serializers.IntegerField(read_only=True)
    effective_frames = serializers.IntegerField(read_only=True)
    seed = serializers.IntegerField(read_only=True)
    include_audio = serializers.BooleanField(read_only=True)
    reference_assets = GenerationTaskReferenceAssetSummarySerializer(many=True, read_only=True)
    reference_asset_count = serializers.IntegerField(read_only=True)
    reference_input_count = serializers.IntegerField(read_only=True)
    reference_image_count = serializers.IntegerField(read_only=True)
    reference_video_count = serializers.IntegerField(read_only=True)
    reference_audio_count = serializers.IntegerField(read_only=True)
    continuity_assets = GenerationTaskReferenceAssetSummarySerializer(
        many=True,
        read_only=True,
        help_text=(
            "Frozen continuity assets associated with the task but not connected "
            "to the execution graph."
        ),
    )
    continuity_asset_count = serializers.IntegerField(
        read_only=True,
        help_text="Number of continuity assets that were not connected to the execution graph.",
    )
    prompt_available = serializers.BooleanField(read_only=True)
    prompt_character_count = serializers.IntegerField(read_only=True)
    negative_prompt_character_count = serializers.IntegerField(read_only=True)
    shot_count = serializers.IntegerField(read_only=True)
    shot_ids = serializers.ListField(child=serializers.CharField(), read_only=True)
    batch_id = serializers.CharField(allow_blank=True, allow_null=True, read_only=True)
    batch_kind = serializers.CharField(allow_blank=True, allow_null=True, read_only=True)
    batch_role = serializers.CharField(allow_blank=True, allow_null=True, read_only=True)
    batch_index = serializers.IntegerField(allow_null=True, read_only=True)
    batch_size = serializers.IntegerField(allow_null=True, read_only=True)
    whole_episode = serializers.BooleanField(read_only=True)
    idempotency_key = serializers.CharField(allow_blank=True, allow_null=True, read_only=True)


class GenerationTaskInputPromptSerializer(serializers.Serializer):
    """Lazily loaded prompt text and its immutable artifact provenance."""

    task_id = serializers.UUIDField(read_only=True)
    prompt_artifact_id = serializers.UUIDField(read_only=True)
    prompt_type = serializers.ChoiceField(choices=PromptArtifact.PromptType.choices, read_only=True)
    model = serializers.CharField(allow_blank=True, read_only=True)
    conditioning_mode = serializers.CharField(allow_blank=True, read_only=True)
    effective_conditioning_mode = serializers.CharField(allow_blank=True, read_only=True)
    prompt_template_key = serializers.CharField(allow_blank=True, read_only=True)
    content_hash = serializers.CharField(allow_blank=True, read_only=True)
    prompt = serializers.CharField(allow_blank=True, read_only=True)
    negative_prompt = serializers.CharField(allow_blank=True, read_only=True)
    prompt_character_count = serializers.IntegerField(read_only=True)
    negative_prompt_character_count = serializers.IntegerField(read_only=True)
    negative_prompt_submitted = serializers.BooleanField(read_only=True)
    prompt_source = serializers.ChoiceField(
        choices=(
            (
                "generated_workflow.injected_values.resolved_semantics.prompt.positive",
                "generated_workflow.injected_values.resolved_semantics.prompt.positive",
            ),
        ),
        read_only=True,
    )
    negative_prompt_source = serializers.ChoiceField(
        choices=(
            (
                "generated_workflow.injected_values.resolved_semantics.prompt.negative",
                "generated_workflow.injected_values.resolved_semantics.prompt.negative",
            ),
            ("not_submitted", "not_submitted"),
        ),
        read_only=True,
    )


class GenerationTaskBibleTargetSerializer(serializers.Serializer):
    """Safe public projection of one frozen Bible image candidate target."""

    schema_version = serializers.IntegerField(
        min_value=RETIRED_BIBLE_IMAGE_CANDIDATE_SCHEMA_VERSION,
        max_value=RETIRED_BIBLE_IMAGE_CANDIDATE_SCHEMA_VERSION,
    )
    reference_category = serializers.ChoiceField(
        choices=("character", "scene", "prop"),
    )
    entity_id = serializers.UUIDField()
    entity_name = serializers.CharField(max_length=160, trim_whitespace=False)
    source_fingerprint = serializers.RegexField(r"^[0-9a-f]{64}$")

    def to_internal_value(self, data):
        expected_fields = {
            "schema_version",
            "reference_category",
            "entity_id",
            "entity_name",
            "source_fingerprint",
        }
        if not isinstance(data, dict) or set(data) != expected_fields:
            raise serializers.ValidationError("Invalid frozen Bible target shape.")
        if type(data.get("schema_version")) is not int:
            raise serializers.ValidationError({
                "schema_version": "schema_version must be an integer.",
            })
        for field_name in (
            "reference_category", "entity_id", "entity_name", "source_fingerprint",
        ):
            if type(data.get(field_name)) is not str:
                raise serializers.ValidationError({
                    field_name: f"{field_name} must be a string.",
                })
        entity_name = data["entity_name"]
        if not entity_name or any(
            unicodedata.category(character).startswith("C") for character in entity_name
        ):
            raise serializers.ValidationError({
                "entity_name": "entity_name must be non-empty and contain no control characters.",
            })
        return super().to_internal_value(data)


class GenerationTaskBibleTargetSchemaExtension(OpenApiSerializerExtension):
    target_class = GenerationTaskBibleTargetSerializer

    def map_serializer(self, auto_schema, direction):
        schema = auto_schema._map_serializer(
            self.target, direction, bypass_extensions=True,
        )
        schema["additionalProperties"] = False
        return schema


class GenerationTaskBatchSlotSerializer(serializers.Serializer):
    batch_id = serializers.CharField(read_only=True, allow_null=True)
    batch_kind = serializers.CharField(read_only=True, allow_null=True)
    shot_version_key = serializers.CharField(
        read_only=True, allow_blank=True, required=False,
    )
    shot_version_label = serializers.CharField(
        read_only=True, allow_blank=True, required=False,
    )
    batch_index = serializers.IntegerField(read_only=True)
    batch_size = serializers.IntegerField(read_only=True)
    slot_status = serializers.ChoiceField(
        choices=(
            "materialized",
            "waiting_deferred",
            "cancelled",
            "cancelled_by_upstream",
            "not_created",
        ),
        read_only=True,
    )
    message = serializers.CharField(read_only=True)
    workflow_mode = serializers.CharField(read_only=True, allow_null=True, required=False)
    task_id = serializers.UUIDField(read_only=True, allow_null=True)
    task_status = serializers.CharField(read_only=True, allow_null=True)
    workflow_id = serializers.UUIDField(read_only=True, allow_null=True)
    deferred_generation_request_id = serializers.UUIDField(read_only=True, allow_null=True)
    deferred_generation_status = serializers.CharField(read_only=True, allow_null=True)
    deferred_upstream_request_id = serializers.UUIDField(read_only=True, allow_null=True)
    episode_id = serializers.UUIDField(read_only=True, allow_null=True)
    episode_number = serializers.IntegerField(read_only=True, allow_null=True)
    episode_title = serializers.CharField(read_only=True, allow_blank=True)
    shot_id = serializers.UUIDField(read_only=True, allow_null=True)
    shot_number = serializers.IntegerField(read_only=True, allow_null=True)
    shot_title = serializers.CharField(read_only=True, allow_blank=True)
    created_at = serializers.DateTimeField(read_only=True, allow_null=True)
    updated_at = serializers.DateTimeField(read_only=True, allow_null=True)


class GenerationTaskSerializer(serializers.ModelSerializer):
    generation_purpose = serializers.SerializerMethodField()
    bible_target = serializers.SerializerMethodField()
    input_parameters = serializers.SerializerMethodField()
    output_media = serializers.SerializerMethodField()
    completed_at = serializers.SerializerMethodField()
    duration_ms = serializers.SerializerMethodField()
    queue_wait_ms = serializers.SerializerMethodField()
    total_duration_ms = serializers.SerializerMethodField()
    timeline = serializers.SerializerMethodField()
    batch_id = serializers.SerializerMethodField()
    batch_kind = serializers.SerializerMethodField()
    batch_role = serializers.SerializerMethodField()
    batch_index = serializers.SerializerMethodField()
    batch_size = serializers.SerializerMethodField()
    batch_slots = serializers.SerializerMethodField()
    production_run_id = serializers.SerializerMethodField()
    production_variant_number = serializers.SerializerMethodField()
    episode_id = serializers.SerializerMethodField()
    episode_number = serializers.SerializerMethodField()
    episode_title = serializers.SerializerMethodField()
    shot_ids = serializers.SerializerMethodField()
    shot_id = serializers.SerializerMethodField()
    shot_number = serializers.SerializerMethodField()
    shot_order = serializers.SerializerMethodField()
    shot_title = serializers.SerializerMethodField()
    retry_of = serializers.SerializerMethodField()
    stalled = serializers.SerializerMethodField()
    can_retry = serializers.SerializerMethodField()
    can_cancel = serializers.SerializerMethodField()
    retry_requires_reconciliation = serializers.SerializerMethodField()

    class Meta:
        model = GenerationTask
        exclude = ("capability_snapshot", "progress_marker")
        read_only_fields = (
            "id", "owner", "project", "episode", "kind", "status", "progress", "attempts",
            "comfy_prompt_id", "client_id", "outputs", "error_message", "queued_at", "started_at",
            "finished_at", "created_at", "updated_at",
            "backend", "model_profile", "workflow_version", "seed", "effective_frames",
            "effective_duration_ms", "staged_inputs",
            "submission_token", "submission_state", "submission_prepared_at", "submission_submitted_at",
            "heartbeat_at", "stalled_at", "remote_state", "remote_error",
            "progress_source", "comfy_progress", "progress_node_id", "progress_updated_at",
            "progress_telemetry_state", "progress_telemetry_error",
            "deadline_started_at", "hard_deadline_at", "idle_deadline_at",
            "last_meaningful_progress_at",
        )

    @staticmethod
    def _task_mapping(value) -> dict:
        return value if isinstance(value, dict) else {}

    @classmethod
    def _task_execution_contract(cls, obj) -> tuple[dict, dict, dict, dict]:
        """Verify the single immutable contract used by current task projections."""

        def invalid(detail: str):
            raise serializers.ValidationError({
                "input_parameters": (
                    "任务缺少当前版本的冻结执行合同，不能从其他记录推断展示；"
                    f"请从 ProductionRun 新建任务。{detail}"
                ),
            })

        snapshot = cls._task_mapping(obj.capability_snapshot)
        workflow = obj.generated_workflow
        injected = cls._task_mapping(workflow.injected_values)
        if injected.get("schema_version") != 2:
            invalid("GeneratedWorkflow schema_version 无效。")

        workflow_metadata = injected.get("execution_metadata")
        task_metadata = snapshot.get("execution_metadata")
        workflow_digest = str(injected.get("execution_metadata_sha256") or "")
        task_digest = str(snapshot.get("execution_metadata_sha256") or "")
        if (
            not isinstance(workflow_metadata, dict)
            or not isinstance(task_metadata, dict)
            or workflow_metadata != task_metadata
            or not workflow_digest
            or workflow_digest != task_digest
            or json_contract(
                workflow_metadata,
                max_bytes=_stage_snapshot_max_bytes(),
            ).sha256 != workflow_digest
        ):
            invalid("execution_metadata 缺失、漂移或哈希不一致。")

        required_snapshot_keys = {
            "generated_workflow_id", "workflow_hash", "prompt_artifact_id",
            "prompt_content_hash", "template_id", "template_slug", "model",
            "conditioning_mode", "effective_conditioning_mode",
            "prompt_template_key", "workflow_mode", "source_workflow",
            "workflow_parameters", "resolution_profile", "resolution_multiple",
            "width", "height", "shot_ids", "shot_count", "whole_episode",
            "asset_bindings", "asset_bindings_sha256", "continuity_assets",
            "continuity_assets_sha256", "resolved_semantics_sha256",
        }
        missing = sorted(required_snapshot_keys - set(snapshot))
        if missing:
            invalid("缺少字段：" + ", ".join(missing) + "。")
        prompt_content_hash = str(snapshot.get("prompt_content_hash") or "")
        if (
            len(prompt_content_hash) != 64
            or any(character not in "0123456789abcdef" for character in prompt_content_hash.casefold())
        ):
            invalid("prompt_content_hash 无效。")
        if "reference_assets" in snapshot:
            invalid("旧 reference_assets 副本字段已退役。")
        prompt = workflow.prompt_artifact
        identity_checks = (
            (snapshot.get("generated_workflow_id"), str(workflow.id), "generated_workflow_id"),
            (snapshot.get("workflow_hash"), workflow.content_hash, "workflow_hash"),
            (snapshot.get("prompt_artifact_id"), str(prompt.id), "prompt_artifact_id"),
            (snapshot.get("template_id"), str(workflow.template_id), "template_id"),
        )
        for actual, expected, field_name in identity_checks:
            if str(actual or "") != str(expected or ""):
                invalid(f"{field_name} 与当前冻结对象不一致。")

        semantics = injected.get("resolved_semantics")
        semantics_digest = str(workflow.resolved_semantics_sha256 or "")
        if (
            not isinstance(semantics, dict)
            or not semantics_digest
            or json_contract(
                semantics,
                max_bytes=_stage_snapshot_max_bytes(),
            ).sha256 != semantics_digest
            or snapshot.get("resolved_semantics_sha256") != semantics_digest
            or snapshot.get("workflow_parameters") != semantics
        ):
            invalid("resolved_semantics 缺失、漂移或哈希不一致。")

        asset_bindings = snapshot.get("asset_bindings")
        binding_digest = str(snapshot.get("asset_bindings_sha256") or "")
        if (
            not isinstance(asset_bindings, list)
            or asset_bindings != injected.get("asset_bindings")
            or not binding_digest
            or binding_digest != str(workflow.asset_bindings_sha256 or "")
            or json_contract(
                asset_bindings,
                max_bytes=_stage_snapshot_max_bytes(),
            ).sha256 != binding_digest
        ):
            invalid("asset_bindings 缺失、漂移或哈希不一致。")

        continuity_assets = snapshot.get("continuity_assets")
        continuity_digest = str(snapshot.get("continuity_assets_sha256") or "")
        if (
            not isinstance(continuity_assets, list)
            or not continuity_digest
            or json_contract(
                continuity_assets,
                max_bytes=_stage_snapshot_max_bytes(),
            ).sha256 != continuity_digest
        ):
            invalid("continuity_assets 缺失、漂移或哈希不一致。")

        template_metadata = workflow_metadata.get("template")
        required_multiple = workflow_metadata.get("required_dimension_multiple")
        width = snapshot.get("width")
        height = snapshot.get("height")
        if (
            workflow_metadata.get("schema_version") != 1
            or not isinstance(template_metadata, dict)
            or str(template_metadata.get("output_kind") or "") != str(obj.kind)
            or not str(workflow_metadata.get("model_identifier") or "").strip()
            or isinstance(required_multiple, bool)
            or not isinstance(required_multiple, int)
            or required_multiple <= 0
            or snapshot.get("resolution_multiple") != required_multiple
            or isinstance(width, bool)
            or not isinstance(width, int)
            or width <= 0
            or isinstance(height, bool)
            or not isinstance(height, int)
            or height <= 0
            or not str(snapshot.get("resolution_profile") or "").strip()
        ):
            invalid("输出类型或分辨率执行元数据无效。")

        metadata_checks = (
            (snapshot.get("template_slug"), template_metadata.get("slug"), "template_slug"),
            (snapshot.get("model"), workflow_metadata.get("model_identifier"), "model"),
            (snapshot.get("workflow_mode"), workflow_metadata.get("workflow_mode"), "workflow_mode"),
            (snapshot.get("source_workflow"), workflow_metadata.get("source_workflow"), "source_workflow"),
            (snapshot.get("width"), semantics.get("image.width"), "width"),
            (snapshot.get("height"), semantics.get("image.height"), "height"),
        )
        for actual, expected, field_name in metadata_checks:
            if actual != expected:
                invalid(f"{field_name} 与冻结 execution metadata 不一致。")
        if (
            not isinstance(snapshot.get("shot_ids"), list)
            or isinstance(snapshot.get("shot_count"), bool)
            or not isinstance(snapshot.get("shot_count"), int)
            or snapshot.get("shot_count") < 0
            or not isinstance(snapshot.get("whole_episode"), bool)
        ):
            invalid("任务范围合同无效。")

        if obj.kind == GenerationTask.Kind.VIDEO:
            selected_mode = workflow_metadata.get("conditioning_mode_contract")
            if (
                not isinstance(selected_mode, dict)
                or str(selected_mode.get("key") or "")
                != str(snapshot.get("effective_conditioning_mode") or "")
            ):
                invalid("视频 conditioning mode 合同与任务快照不一致。")
            for field_name in ("fps", "requested_duration_ms", "include_audio"):
                if field_name not in snapshot:
                    invalid(f"视频任务缺少 {field_name}。")
            semantic_frames = semantics.get("video.frames")
            semantic_fps = semantics.get("video.fps")
            semantic_duration_ms = semantics.get("video.duration_ms")
            semantic_audio_enabled = semantics.get("audio.enabled")
            if (
                isinstance(semantic_frames, bool)
                or not isinstance(semantic_frames, int)
                or semantic_frames <= 0
            ):
                invalid("视频任务缺少有效的冻结 video.frames。")
            if (
                isinstance(semantic_fps, bool)
                or not isinstance(semantic_fps, (int, float))
            ):
                invalid("视频任务缺少有效的冻结 video.fps。")
            try:
                semantic_fps_fraction = Fraction(semantic_fps)
            except (TypeError, ValueError, OverflowError):
                invalid("视频任务缺少有效的冻结 video.fps。")
            if semantic_fps_fraction <= 0:
                invalid("视频任务缺少有效的冻结 video.fps。")
            expected_effective_duration_ms = round(
                Fraction(semantic_frames * 1000, 1) / semantic_fps_fraction,
            )
            if (
                isinstance(snapshot["fps"], bool)
                or not isinstance(snapshot["fps"], int)
                or snapshot["fps"] <= 0
                or isinstance(snapshot["requested_duration_ms"], bool)
                or not isinstance(snapshot["requested_duration_ms"], int)
                or snapshot["requested_duration_ms"] <= 0
                or not isinstance(snapshot["include_audio"], bool)
                or snapshot["fps"] != semantics.get("video.fps")
                or obj.effective_frames != semantic_frames
                or obj.effective_duration_ms != expected_effective_duration_ms
            ):
                invalid("视频时钟或音频合同与 resolved_semantics 不一致。")
            # Some registered compilers bind only frames/FPS into the executable
            # graph.  requested_duration_ms and include_audio remain explicit,
            # immutable task-snapshot facts in that case; absence from
            # resolved_semantics is not permission to infer either value from
            # another record.  When a compiler does bind the semantic key, the
            # two frozen layers must still agree exactly.
            if (
                semantic_duration_ms is not None
                and snapshot["requested_duration_ms"] != semantic_duration_ms
            ):
                invalid("视频请求时长与冻结 resolved_semantics 不一致。")
            if (
                semantic_audio_enabled is not None
                and snapshot["include_audio"] != semantic_audio_enabled
            ):
                invalid("视频音频开关与冻结 resolved_semantics 不一致。")
        elif any(
            field_name in snapshot
            for field_name in ("fps", "requested_duration_ms", "include_audio")
        ):
            invalid("图片任务包含已退役的视频参数。")
        return snapshot, injected, workflow_metadata, semantics

    @staticmethod
    def _task_optional_int(value) -> int | None:
        if value is None or value == "":
            return None
        try:
            return int(value)
        except (TypeError, ValueError, OverflowError):
            return None

    @staticmethod
    def _task_optional_float(value) -> float | None:
        if value is None or value == "":
            return None
        try:
            return float(value)
        except (TypeError, ValueError, OverflowError):
            return None

    @staticmethod
    def _task_bool(value, default: bool = False) -> bool:
        if value is None:
            return default
        if isinstance(value, bool):
            return value
        if isinstance(value, str):
            normalized = value.strip().lower()
            if normalized in {"true", "1", "yes", "on"}:
                return True
            if normalized in {"false", "0", "no", "off", ""}:
                return False
        return bool(value)

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

    @staticmethod
    def _task_uuid_text(value) -> str | None:
        if value in (None, ""):
            return None
        try:
            return str(UUID(str(value)))
        except (TypeError, ValueError, AttributeError):
            return None

    @classmethod
    def _task_reference_assets(cls, raw_assets) -> list[dict]:
        """Whitelist reference metadata; executable filesystem locations never leave the API."""
        if not isinstance(raw_assets, list):
            return []
        summaries = []
        for index, raw in enumerate(raw_assets):
            if not isinstance(raw, dict):
                raise serializers.ValidationError({
                    "input_parameters": f"任务冻结素材第 {index + 1} 项必须是对象。",
                })
            asset_id = cls._task_uuid_text(raw.get("asset_id"))
            if asset_id is None:
                raise serializers.ValidationError({
                    "input_parameters": f"任务冻结素材第 {index + 1} 项缺少有效 asset_id。",
                })
            audio_asset_id = cls._task_uuid_text(raw.get("audio_asset_id"))
            if raw.get("audio_asset_id") not in (None, "") and audio_asset_id is None:
                raise serializers.ValidationError({
                    "input_parameters": f"任务冻结素材第 {index + 1} 项的 audio_asset_id 无效。",
                })
            media_metadata = raw.get("media_metadata")
            if not isinstance(media_metadata, dict):
                media_metadata = {}
            summaries.append({
                "asset_id": asset_id,
                "name": cls._task_public_text(raw.get("name")),
                "category": cls._task_public_text(raw.get("category")),
                "media_kind": cls._task_public_text(raw.get("media_kind")).casefold(),
                "input_slot": cls._task_optional_int(raw.get("input_slot")),
                "input_label": cls._task_public_text(raw.get("input_label")),
                "project_slot": cls._task_optional_int(raw.get("project_slot")),
                "entity_id": cls._task_uuid_text(raw.get("entity_id")),
                "entity_name": cls._task_public_text(raw.get("entity_name")),
                "entity_label": cls._task_public_text(raw.get("entity_label")),
                "checksum": cls._task_public_text(raw.get("checksum")),
                "use_embedded_audio": cls._task_bool(raw.get("use_embedded_audio")),
                "audio_asset_id": audio_asset_id,
                "audio_input_label": (
                    cls._task_public_text(raw.get("audio_input_label"))
                    if raw.get("audio_input_label") is not None else None
                ),
                "audio_name": cls._task_public_text(raw.get("audio_name")),
                "audio_checksum": cls._task_public_text(raw.get("audio_checksum")),
                "source_fps": cls._task_optional_float(
                    raw.get("source_fps", media_metadata.get("source_fps")),
                ),
                "effective_fps": cls._task_optional_int(
                    raw.get("effective_fps"),
                ),
                "effective_duration_ms": cls._task_optional_int(
                    raw.get("effective_duration_ms"),
                ),
                "cfr_guaranteed": (
                    cls._task_bool(raw.get("cfr_guaranteed"))
                    if raw.get("cfr_guaranteed") is not None else None
                ),
                "paired_audio_duration_aligned": (
                    cls._task_bool(raw.get("paired_audio_duration_aligned"))
                    if raw.get("paired_audio_duration_aligned") is not None else None
                ),
            })
        return summaries

    @staticmethod
    def _task_reference_counts(reference_assets: list[dict]) -> tuple[int, int, int]:
        image_count = video_count = audio_count = 0
        for item in reference_assets:
            media_kind = str(item.get("media_kind") or "").casefold()
            if media_kind == "video":
                video_count += 1
                if (
                    item.get("audio_asset_id")
                    or item.get("audio_input_label")
                    or item.get("use_embedded_audio")
                ):
                    audio_count += 1
            elif media_kind == "audio":
                audio_count += 1
            else:
                image_count += 1
        return image_count, video_count, audio_count

    @staticmethod
    def _task_reference_media_kind(item: dict) -> str:
        media_kind = str(item.get("media_kind") or "").casefold()
        if media_kind not in {"image", "video", "audio"}:
            raise serializers.ValidationError({
                "input_parameters": "任务冻结素材缺少显式 media_kind。",
            })
        return media_kind

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

    def _task_reference_assets_for_request(self, raw_assets) -> list[dict]:
        summaries = self._task_reference_assets(raw_assets)
        request = self.context.get("request")
        view = self.context.get("view")
        user = getattr(request, "user", None)
        visible_assets: dict[str, Asset] = {}
        asset_ids = [item["asset_id"] for item in summaries if item.get("asset_id")]
        # Task lists are polled frequently.  Only an explicit detail retrieve
        # may query current Asset state or touch its storage backend.
        include_live_previews = getattr(view, "action", None) == "retrieve"
        if include_live_previews and asset_ids and getattr(user, "is_authenticated", False):
            queryset = Asset.objects.filter(pk__in=asset_ids, is_active=True)
            if not getattr(user, "is_staff", False):
                queryset = queryset.filter(Q(owner=user) | Q(owner__isnull=True))
            visible_assets = {str(asset.id): asset for asset in queryset}
        result = []
        for item in summaries:
            asset = visible_assets.get(item.get("asset_id"))
            display_name = (
                item.get("name") or item.get("entity_name")
                or (asset.name if asset is not None else "")
                or item.get("input_label") or "参考素材"
            )
            result.append({
                **item,
                "media_kind": self._task_reference_media_kind(item),
                "display_name": self._task_public_text(display_name),
                "preview_url": self._task_asset_preview_url(asset) if asset is not None else "",
            })
        return result

    @classmethod
    def build_input_prompt_payload(cls, obj) -> dict:
        """Resolve the exact persisted text submitted by this generated workflow."""
        workflow = obj.generated_workflow
        snapshot, _injected, execution_metadata, resolved_semantics = (
            cls._task_execution_contract(obj)
        )
        prompt_artifact = workflow.prompt_artifact
        prompt_text = resolved_semantics.get("prompt.positive")
        if not isinstance(prompt_text, str) or not prompt_text:
            raise serializers.ValidationError({
                "input_prompt": "冻结 resolved_semantics 缺少实际提交的正向提示词。",
            })
        prompt_source = (
            "generated_workflow.injected_values.resolved_semantics.prompt.positive"
        )
        if isinstance(resolved_semantics.get("prompt.negative"), str):
            negative_prompt = str(resolved_semantics["prompt.negative"])
            negative_prompt_source = (
                "generated_workflow.injected_values.resolved_semantics.prompt.negative"
            )
            negative_prompt_submitted = True
        else:
            negative_prompt = ""
            negative_prompt_source = "not_submitted"
            negative_prompt_submitted = False
        conditioning_mode = (
            cls._task_public_text(snapshot["conditioning_mode"])
            if obj.kind == GenerationTask.Kind.VIDEO else ""
        )
        effective_conditioning_mode = cls._task_public_text(
            snapshot["effective_conditioning_mode"],
        )
        return {
            "task_id": str(obj.id),
            "prompt_artifact_id": str(prompt_artifact.id),
            "prompt_type": obj.kind,
            "model": cls._task_public_text(execution_metadata["model_identifier"]),
            "conditioning_mode": conditioning_mode,
            "effective_conditioning_mode": effective_conditioning_mode,
            "prompt_template_key": cls._task_public_text(snapshot["prompt_template_key"]),
            "content_hash": cls._task_public_text(snapshot["prompt_content_hash"]),
            "prompt": prompt_text,
            "negative_prompt": negative_prompt,
            "prompt_character_count": len(prompt_text),
            "negative_prompt_character_count": len(negative_prompt),
            "negative_prompt_submitted": negative_prompt_submitted,
            "prompt_source": prompt_source,
            "negative_prompt_source": negative_prompt_source,
        }

    @extend_schema_field(GenerationTaskInputParametersSerializer)
    def get_input_parameters(self, obj) -> dict:
        snapshot, _injected, execution_metadata, semantics = (
            self._task_execution_contract(obj)
        )
        is_image = obj.kind == GenerationTask.Kind.IMAGE

        workflow_parameters = {}
        for key, value in semantics.items():
            if not isinstance(key, str) or key.startswith("prompt."):
                continue
            if isinstance(value, bool):
                workflow_parameters[key] = value
            elif isinstance(value, int):
                workflow_parameters[key] = value
            elif isinstance(value, float):
                workflow_parameters[key] = value if value == value else None
            elif value is not None:
                workflow_parameters[key] = self._task_public_text(value)

        reference_assets = self._task_reference_assets_for_request(
            snapshot["asset_bindings"],
        )
        continuity_assets = self._task_reference_assets_for_request(
            snapshot["continuity_assets"],
        )
        staged_by_asset = {
            str(row.get("asset_id")): row
            for row in (obj.staged_inputs or [])
            if isinstance(row, dict) and row.get("asset_id")
        }
        for summary in reference_assets:
            staged = staged_by_asset.get(str(summary.get("asset_id")))
            if staged and staged.get("media_role") == "reference_video":
                summary.update({
                    "source_fps": self._task_optional_float(staged.get("source_fps")),
                    "effective_fps": self._task_optional_int(staged.get("effective_fps")),
                    "effective_duration_ms": self._task_optional_int(
                        staged.get("effective_duration_ms"),
                    ),
                    "cfr_guaranteed": staged.get("cfr_guaranteed") is True,
                })
            paired_audio = staged_by_asset.get(str(summary.get("audio_asset_id")))
            if paired_audio and paired_audio.get("media_role") == "paired_reference_audio":
                summary["paired_audio_duration_aligned"] = (
                    paired_audio.get("duration_aligned") is True
                )
        image_count, video_count, audio_count = self._task_reference_counts(reference_assets)
        shot_ids = []
        for value in snapshot["shot_ids"]:
            normalized = self._task_uuid_text(value)
            if normalized and normalized not in shot_ids:
                shot_ids.append(normalized)
        if len(shot_ids) != snapshot["shot_count"]:
            raise serializers.ValidationError({
                "input_parameters": "任务冻结 shot_ids 与 shot_count 不一致。",
            })
        input_prompt = self.build_input_prompt_payload(obj)
        template_metadata = execution_metadata["template"]
        parameters = {
            "model": self._task_public_text(execution_metadata["model_identifier"]),
            "model_profile": self._task_public_text(obj.model_profile),
            "conditioning_mode": self._task_public_text(snapshot["conditioning_mode"]),
            "effective_conditioning_mode": self._task_public_text(
                snapshot["effective_conditioning_mode"],
            ),
            "workflow_mode": self._task_public_text(execution_metadata["workflow_mode"]),
            "template_slug": self._task_public_text(template_metadata["slug"]),
            "source_workflow": self._task_public_text(execution_metadata["source_workflow"]),
            "workflow_parameters": workflow_parameters,
            "prompt_template_key": self._task_public_text(snapshot["prompt_template_key"]),
            "resolution_profile": self._task_public_text(snapshot["resolution_profile"]),
            "width": snapshot["width"],
            "height": snapshot["height"],
            "resolution_multiple": execution_metadata["required_dimension_multiple"],
            "seed": obj.seed,
            "reference_assets": reference_assets,
            "reference_asset_count": len(reference_assets),
            "reference_input_count": image_count + video_count + audio_count,
            "reference_image_count": image_count,
            "reference_video_count": video_count,
            "reference_audio_count": audio_count,
            "continuity_assets": continuity_assets,
            "continuity_asset_count": len(continuity_assets),
            "prompt_available": bool(input_prompt["prompt"]),
            "prompt_character_count": input_prompt["prompt_character_count"],
            "negative_prompt_character_count": input_prompt["negative_prompt_character_count"],
            "shot_count": snapshot["shot_count"],
            "shot_ids": shot_ids,
            "batch_id": self._task_public_text(snapshot.get("batch_id")) or None,
            "batch_kind": self._task_public_text(snapshot.get("batch_kind")) or None,
            "batch_role": self._task_public_text(snapshot.get("batch_role")) or None,
            "batch_index": self._task_optional_int(snapshot.get("batch_index")),
            "batch_size": self._task_optional_int(snapshot.get("batch_size")),
            "whole_episode": snapshot["whole_episode"],
            "idempotency_key": (
                self._task_public_text(snapshot.get("idempotency_key")) or None
            ),
        }
        if not is_image:
            parameters.update({
                "fps": snapshot["fps"],
                "requested_duration_ms": snapshot["requested_duration_ms"],
                "effective_duration_ms": obj.effective_duration_ms,
                "effective_frames": obj.effective_frames,
                "include_audio": snapshot["include_audio"],
            })
        return parameters

    def to_representation(self, instance):
        representation = super().to_representation(instance)
        # Worker staging records can contain ComfyUI routing identifiers and
        # legacy absolute paths.  The public input_parameters field is the
        # whitelisted audit snapshot; raw staging state never leaves the API.
        representation["staged_inputs"] = []
        view = self.context.get("view")
        if getattr(view, "action", None) == "list":
            # Polling responses expose the stable, path-free input_parameters
            # summary instead of raw worker payloads and verbose detail logs.
            representation.update({
                "staged_inputs": [],
                "outputs": [],
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

    def get_can_retry(self, obj) -> bool:
        if obj.status != GenerationTask.Status.FAILED:
            return False
        if obj.outputs:
            return False
        safe_retry_boundary = (
            (
                obj.submission_state == GenerationTask.SubmissionState.UNPREPARED
                and not obj.submission_token
                and not obj.comfy_prompt_id
            )
            or obj.submission_state == GenerationTask.SubmissionState.TERMINAL_FAILED
        )
        if not safe_retry_boundary:
            return False
        return not GenerationTask.objects.filter(
            capability_snapshot__retry_of=str(obj.id),
        ).exists()

    @staticmethod
    def get_retry_requires_reconciliation(obj) -> bool:
        return bool(
            obj.status in {GenerationTask.Status.FAILED, GenerationTask.Status.CANCELLED}
            and not obj.outputs
            and obj.submission_token
            and obj.submission_state not in {
                GenerationTask.SubmissionState.TERMINAL_FAILED,
                GenerationTask.SubmissionState.TERMINAL_CANCELLED,
            }
        )

    @staticmethod
    def get_can_cancel(obj) -> bool:
        return obj.status in {
            GenerationTask.Status.QUEUED,
            GenerationTask.Status.RUNNING,
            GenerationTask.Status.CANCEL_REQUESTED,
        }

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
        media.extend(self._derived_frame_output_media(obj, len(media)))
        return media

    def _derived_frame_output_media(self, obj, start_index: int) -> list[dict]:
        """Expose extracted continuity frames as source-task image outputs on detail reads."""
        request = self.context.get("request")
        view = self.context.get("view")
        user = getattr(request, "user", None)
        if getattr(view, "action", None) not in {"retrieve", "list"} or not getattr(user, "is_authenticated", False):
            return []
        queryset = (
            DeferredGenerationRequest.objects
            .select_related("derived_asset")
            .filter(source_task=obj, derived_asset__isnull=False)
            .order_by("created_at", "id")
        )
        if not getattr(user, "is_staff", False):
            queryset = queryset.filter(owner=user)
        result: list[dict] = []
        for offset, deferred in enumerate(queryset):
            asset = deferred.derived_asset
            if asset is None:
                continue
            preview_url = self._task_asset_preview_url(asset)
            filename = ""
            if asset.file:
                filename = Path(str(asset.file.name)).name
            filename = filename or asset.name or f"deferred-frame-{deferred.id}.png"
            public_label = (
                "尾帧抽取图"
                if deferred.frame_slot == DeferredGenerationRequest.FrameSlot.FIRST_FRAME
                else "首帧抽取图"
            )
            result.append({
                "index": start_index + offset,
                "kind": "image",
                "filename": self._task_public_text(filename),
                "preview_url": preview_url,
                "download_url": f"{preview_url}?download=1" if preview_url else "",
                "ai_generated": True,
                "provenance_url": "",
                "artifact_sha256": self._task_public_text(asset.checksum),
                "manifest_sha256": self._task_public_text(deferred.source_manifest_sha256),
                "distribution_status": "not_prepared",
                "distribution_error": "",
                "public_provenance_url": "",
                "public_artifact_sha256": "",
                "public_manifest_sha256": "",
                "public_label": public_label,
                "public_version": "deferred-frame-v1",
                "promoted_asset_id": str(asset.id),
            })
        return result

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
        backed by an existing timestamp or retry reference; it must not turn UI
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
                # ``timestamp`` is the canonical public key.  ``occurred_at``
                # remains as a compatibility alias for clients built while
                # this endpoint was being introduced.
                "timestamp": represented,
                "occurred_at": represented,
                "label": label,
                "details": details or {},
            }))

        retry_of = self.get_retry_of(obj)
        if retry_of:
            append_event(
                obj.created_at,
                0,
                event="retry_created",
                status=GenerationTask.Status.QUEUED,
                label="已创建重试任务",
                details={"source_task_id": retry_of},
            )

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

    @staticmethod
    def _snapshot(obj) -> dict:
        return obj.capability_snapshot if isinstance(obj.capability_snapshot, dict) else {}

    @staticmethod
    def _batch_snapshot(obj) -> dict:
        return effective_batch_snapshot_for_task(obj)

    @extend_schema_field(serializers.ChoiceField(
        choices=(RETIRED_BIBLE_IMAGE_CANDIDATE_PURPOSE,),
        allow_null=True,
    ))
    def get_generation_purpose(self, obj) -> str | None:
        snapshot = self._snapshot(obj)
        if snapshot.get("generation_purpose") == RETIRED_BIBLE_IMAGE_CANDIDATE_PURPOSE:
            return RETIRED_BIBLE_IMAGE_CANDIDATE_PURPOSE
        return None

    @extend_schema_field(GenerationTaskBibleTargetSerializer(allow_null=True))
    def get_bible_target(self, obj) -> dict | None:
        snapshot = self._snapshot(obj)
        if snapshot.get("generation_purpose") != RETIRED_BIBLE_IMAGE_CANDIDATE_PURPOSE:
            return None
        target = GenerationTaskBibleTargetSerializer(data=snapshot.get("bible_target"))
        if not target.is_valid():
            return None
        validated = target.validated_data
        return {
            "schema_version": validated["schema_version"],
            "reference_category": validated["reference_category"],
            "entity_id": str(validated["entity_id"]),
            "entity_name": validated["entity_name"],
            "source_fingerprint": validated["source_fingerprint"],
        }

    @staticmethod
    def _episode(obj):
        if obj.episode_id:
            return obj.episode
        return obj.generated_workflow.prompt_artifact.episode

    @staticmethod
    def _shot(obj):
        return obj.generated_workflow.prompt_artifact.shot

    def get_batch_id(self, obj) -> str | None:
        return self._batch_snapshot(obj).get("batch_id") or None

    def get_batch_kind(self, obj) -> str | None:
        return self._batch_snapshot(obj).get("batch_kind") or None

    def get_batch_role(self, obj) -> str | None:
        return self._batch_snapshot(obj).get("batch_role") or None

    def get_batch_index(self, obj) -> int | None:
        return self._batch_snapshot(obj).get("batch_index")

    def get_batch_size(self, obj) -> int | None:
        return self._batch_snapshot(obj).get("batch_size")

    @extend_schema_field(GenerationTaskBatchSlotSerializer(many=True))
    def get_batch_slots(self, obj) -> list[dict]:
        snapshot = self._batch_snapshot(obj)
        batch_id = snapshot.get("batch_id")
        batch_size = self._task_optional_int(snapshot.get("batch_size")) or 0
        if not batch_id or batch_size <= 1:
            return []
        cache = self.context.setdefault("_production_batch_slots_cache", {})
        cache_key = (str(obj.owner_id), str(obj.project_id), str(batch_id))
        if cache_key not in cache:
            cache[cache_key] = build_production_batch_slots(obj)
        return cache[cache_key]

    @extend_schema_field(serializers.UUIDField(allow_null=True))
    def get_production_run_id(self, obj) -> str | None:
        """Expose only the validated immutable run identity used for UI grouping."""

        return self._task_uuid_text(self._snapshot(obj).get("production_run_id"))

    @extend_schema_field(serializers.IntegerField(min_value=1, allow_null=True))
    def get_production_variant_number(self, obj) -> int | None:
        snapshot = self._snapshot(obj)
        execution_identity = self._task_mapping(snapshot.get("execution_identity"))
        raw_value = execution_identity.get("variant_number")
        if isinstance(raw_value, bool):
            return None
        value = self._task_optional_int(raw_value)
        return value if value is not None and value > 0 else None

    def get_episode_id(self, obj) -> str | None:
        snapshot = self._snapshot(obj)
        episode = self._episode(obj)
        return snapshot.get("episode_id") or (str(episode.id) if episode else None)

    def get_episode_number(self, obj) -> int | None:
        snapshot = self._snapshot(obj)
        episode = self._episode(obj)
        return snapshot.get("episode_number", episode.number if episode else None)

    def get_episode_title(self, obj) -> str:
        snapshot = self._snapshot(obj)
        episode = self._episode(obj)
        return snapshot.get("episode_title", episode.title if episode else "")

    def get_shot_ids(self, obj) -> list[str]:
        snapshot = self._snapshot(obj)
        if isinstance(snapshot.get("shot_ids"), list):
            return [str(value) for value in snapshot["shot_ids"]]
        prompt = obj.generated_workflow.prompt_artifact
        request_ids = (prompt.request_options or {}).get("shot_ids")
        if isinstance(request_ids, list):
            return [str(value) for value in request_ids]
        return [str(prompt.shot_id)] if prompt.shot_id else []

    def get_shot_id(self, obj) -> str | None:
        snapshot = self._snapshot(obj)
        shot = self._shot(obj)
        return snapshot.get("shot_id") or (str(shot.id) if shot else None)

    def get_shot_number(self, obj) -> int | None:
        snapshot = self._snapshot(obj)
        shot = self._shot(obj)
        return snapshot.get("shot_number", shot.number if shot else None)

    def get_shot_order(self, obj) -> int | None:
        return self.get_shot_number(obj)

    def get_shot_title(self, obj) -> str:
        snapshot = self._snapshot(obj)
        shot = self._shot(obj)
        return snapshot.get("shot_title", shot.title if shot else "")

    def get_retry_of(self, obj) -> str | None:
        return self._snapshot(obj).get("retry_of") or None

    def validate_generated_workflow(self, workflow):
        user = self.context["request"].user
        if workflow.project.owner_id != user.id and not user.is_staff:
            raise serializers.ValidationError("不能使用其他用户的工作流")
        return workflow


class GenerationTaskListSerializer(GenerationTaskSerializer):
    """List projection that preserves audit rows without inventing old inputs.

    Current frozen contracts keep the same safe ``input_parameters`` projection
    as task detail.  Unsupported or damaged contracts remain visible as task
    audit facts, while their runtime inputs are explicitly unavailable.  Detail
    and input-prompt endpoints continue to fail closed through
    ``GenerationTaskSerializer``.
    """

    INPUT_CONTRACT_AVAILABLE = "available"
    INPUT_CONTRACT_UNAVAILABLE = "unavailable"
    INPUT_CONTRACT_LEGACY_SCHEMA_UNSUPPORTED = "legacy_schema_unsupported"
    INPUT_CONTRACT_FROZEN_INVALID = "frozen_contract_invalid"

    input_parameters = serializers.SerializerMethodField(
        help_text=(
            "当前冻结合同可验证时返回安全输入投影；历史或损坏合同返回 null。 "
            "Returns the safe input projection only for a verifiable frozen contract; "
            "legacy or invalid contracts return null."
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
            injected = self._task_mapping(obj.generated_workflow.injected_values)
            reason_code = (
                self.INPUT_CONTRACT_LEGACY_SCHEMA_UNSUPPORTED
                if injected.get("schema_version") != 2
                else self.INPUT_CONTRACT_FROZEN_INVALID
            )
            projection = (
                self.INPUT_CONTRACT_UNAVAILABLE,
                reason_code,
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
            INPUT_CONTRACT_LEGACY_SCHEMA_UNSUPPORTED,
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
    generated_workflow_id = serializers.UUIDField(read_only=True)
    format = serializers.ChoiceField(choices=("comfyui_api", "comfyui_ui"), read_only=True)
    filename = serializers.CharField(read_only=True)
    content_hash = serializers.CharField(read_only=True)
    template_slug = serializers.CharField(read_only=True)
    template_version = serializers.IntegerField(read_only=True)
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


class ScriptBibleEvidenceSerializer(serializers.Serializer):
    line = serializers.IntegerField(min_value=1)
    kind = serializers.ChoiceField(choices=(
        "body_mention", "dialogue_speaker", "shot_setting",
    ))
    excerpt = serializers.CharField(max_length=240)


class ScriptBibleDiagnosticSerializer(serializers.Serializer):
    line = serializers.IntegerField(min_value=1, required=False, allow_null=True)
    severity = serializers.ChoiceField(choices=("info", "warning", "error"))
    code = serializers.CharField(max_length=64)
    detail = serializers.CharField(required=False)
    entity_type = serializers.ChoiceField(
        choices=("character", "scene", "prop"), required=False,
    )
    candidate_id = serializers.RegexField(r"^[0-9a-f]{64}$", required=False)


class ScriptBibleCandidateSerializer(serializers.Serializer):
    candidate_id = serializers.RegexField(r"^[0-9a-f]{64}$")
    entity_type = serializers.ChoiceField(choices=("character", "scene", "prop"))
    name = serializers.CharField(max_length=160)
    description = serializers.CharField(max_length=2_000, allow_blank=True)
    visual_identity_en = serializers.CharField(max_length=3_000, allow_blank=True)
    declaration_line = serializers.IntegerField(min_value=1)
    occurrence = serializers.IntegerField(min_value=0)
    evidence = ScriptBibleEvidenceSerializer(many=True)
    recommended = serializers.BooleanField()
    matched_bible_id = serializers.UUIDField(allow_null=True)
    existing = serializers.BooleanField()
    confirmed = serializers.BooleanField()
    conflict_code = serializers.ChoiceField(
        choices=(
            "script_bible_cross_type_name_conflict",
            "script_bible_content_conflict",
            "script_bible_reviewed_content_conflict",
            "script_bible_provenance_conflict",
        ),
        allow_null=True,
    )


class ScriptBibleCandidatesResponseSerializer(serializers.Serializer):
    schema_version = serializers.IntegerField(min_value=1, max_value=1)
    project_id = serializers.UUIDField()
    source_fingerprint = serializers.RegexField(r"^[0-9a-f]{64}$")
    has_block = serializers.BooleanField()
    candidates = ScriptBibleCandidateSerializer(many=True)
    diagnostics = ScriptBibleDiagnosticSerializer(many=True)


class ScriptBibleReviewedSelectionSerializer(StrictSerializer):
    candidate_id = serializers.RegexField(r"^[0-9a-f]{64}$")
    reviewed = serializers.BooleanField()
    name = serializers.CharField(max_length=160, trim_whitespace=False)
    description = serializers.CharField(
        max_length=2_000, allow_blank=True, trim_whitespace=False,
    )
    visual_identity_en = serializers.CharField(
        max_length=3_000, allow_blank=True, trim_whitespace=False,
    )

    def to_internal_value(self, data):
        if not isinstance(data, dict) or data.get("reviewed") is not True:
            raise serializers.ValidationError({
                "reviewed": "只有字面 JSON true 才表示已人工确认。",
            })
        return super().to_internal_value(data)

    def validate_reviewed(self, value):
        if value is not True:
            raise serializers.ValidationError("只有人工确认的候选才能应用。")
        return value










class ScriptBibleErrorDetailsSerializer(serializers.Serializer):
    detail = serializers.CharField()
    line = serializers.IntegerField(min_value=1, required=False, allow_null=True)
    diagnostic = ScriptBibleDiagnosticSerializer(required=False)
    source_fingerprint = serializers.RegexField(r"^[0-9a-f]{64}$", required=False)
    current_source_fingerprint = serializers.RegexField(
        r"^[0-9a-f]{64}$", required=False,
    )
    pending_count = serializers.IntegerField(min_value=0, required=False)
    conflict_count = serializers.IntegerField(min_value=0, required=False)
    candidates = ScriptBibleCandidateSerializer(many=True, required=False)


class ScriptBibleErrorSerializer(serializers.Serializer):
    status_code = serializers.IntegerField()
    code = serializers.CharField(max_length=64)
    details = ScriptBibleErrorDetailsSerializer()


class ScriptBibleErrorEnvelopeSerializer(serializers.Serializer):
    error = ScriptBibleErrorSerializer()


class PromptFieldTranslationSerializer(serializers.ModelSerializer):
    runtime_metadata = serializers.SerializerMethodField()
    revision_history = serializers.SerializerMethodField()
    context_current = serializers.SerializerMethodField()
    promoted_fields = serializers.SerializerMethodField()

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

    @extend_schema_field(serializers.ListField(child=serializers.DictField()))
    def get_promoted_fields(self, obj: PromptFieldTranslation) -> list[dict]:
        promotion = (obj.runtime_metadata or {}).get("promotion") or {}
        if not promotion.get("promoted"):
            return []
        dialogue_index = promotion.get("dialogue_index")
        return [{
            "field_key": obj.field_key,
            "value": obj.translated_text,
            "dialogue_index": dialogue_index if isinstance(dialogue_index, int) else None,
        }]

    class Meta:
        model = PromptFieldTranslation
        fields = (
            "id", "project", "scope_type", "scope_id", "field_key",
            "source_text", "translated_text", "status", "revision",
            "provider", "model", "error_message", "runtime_metadata",
            "revision_history", "context_current", "promoted_fields",
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


class PromptTranslationBatchRequestSerializer(serializers.Serializer):
    entries = PromptTranslationEntryRequestSerializer(many=True, allow_empty=False)
    model_deployment_id = serializers.UUIDField()
    source_lang = serializers.ChoiceField(choices=("Chinese",), default="Chinese")
    target_lang = serializers.ChoiceField(choices=("English",), default="English")

    def validate_entries(self, entries):
        maximum = translation_request_max_entries()
        if len(entries) > maximum:
            raise serializers.ValidationError(f"单次最多翻译 {maximum} 个字段")
        seen = set()
        for item in entries:
            key = (item["scope_type"], item.get("scope_id"), item["field_key"])
            if key in seen:
                raise serializers.ValidationError("同一批次不能重复提交同一字段")
            seen.add(key)
        return entries


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


class CanonicalGlossaryEntryRequestSerializer(serializers.Serializer):
    """Select one current Bible entity without trusting a browser-supplied name."""

    scope_type = serializers.ChoiceField(choices=("character", "scene", "prop"))
    scope_id = serializers.UUIDField()


class CanonicalGlossaryEnsureSerializer(serializers.Serializer):
    entries = CanonicalGlossaryEntryRequestSerializer(
        many=True, required=False, allow_empty=True,
    )
    include_all_missing = serializers.BooleanField(default=False)
    ensure_visual_identity_translation = serializers.BooleanField(default=False)

    def validate(self, attrs):
        entries = attrs.get("entries", [])
        if not entries and not attrs["include_all_missing"]:
            raise serializers.ValidationError(
                "entries 至少选择一个 Bible 实体，或将 include_all_missing 设为 true",
            )
        maximum = canonical_glossary_max_entities()
        if len(entries) > maximum:
            raise serializers.ValidationError(
                f"单次最多处理 {maximum} 个 Bible 实体",
            )
        keys = [(item["scope_type"], item["scope_id"]) for item in entries]
        if len(keys) != len(set(keys)):
            raise serializers.ValidationError("同一 Bible 实体不能重复提交")
        return attrs


class CanonicalGlossaryResultEntrySerializer(serializers.Serializer):
    scope_type = serializers.ChoiceField(choices=("character", "scene", "prop"))
    scope_id = serializers.UUIDField()
    name = serializers.CharField(allow_blank=True)
    canonical_english = serializers.CharField(allow_blank=True)
    status = serializers.ChoiceField(choices=("generated", "reused", "conflict", "failed"))
    origin = serializers.ChoiceField(choices=("model", "existing", "source_english", "none"))
    retryable = serializers.BooleanField()
    error_code = serializers.CharField(allow_blank=True)
    message = serializers.CharField(allow_blank=True)


class CanonicalGlossarySummarySerializer(serializers.Serializer):
    requested = serializers.IntegerField(min_value=0)
    generated = serializers.IntegerField(min_value=0)
    reused = serializers.IntegerField(min_value=0)
    conflict = serializers.IntegerField(min_value=0)
    failed = serializers.IntegerField(min_value=0)


class CanonicalGlossaryVisualSummarySerializer(serializers.Serializer):
    requested = serializers.IntegerField(min_value=0)
    generated = serializers.IntegerField(min_value=0)
    reused = serializers.IntegerField(min_value=0)
    not_required = serializers.IntegerField(min_value=0)
    conflict = serializers.IntegerField(min_value=0)
    failed = serializers.IntegerField(min_value=0)


class CanonicalGlossaryVisualTranslationSerializer(serializers.Serializer):
    scope_type = serializers.ChoiceField(choices=("character", "scene", "prop"))
    scope_id = serializers.UUIDField()
    field_key = serializers.CharField()
    source_text = serializers.CharField(allow_blank=True)
    translated_text = serializers.CharField(allow_blank=True)
    translation_id = serializers.UUIDField(allow_null=True)
    translation_status = serializers.CharField(allow_blank=True)
    revision = serializers.IntegerField(min_value=1, allow_null=True)
    status = serializers.ChoiceField(choices=(
        "generated", "reused", "not_required", "conflict", "failed",
    ))
    origin = serializers.ChoiceField(choices=(
        "model", "existing", "source_english", "none",
    ))
    context_current = serializers.BooleanField()
    retryable = serializers.BooleanField()
    error_code = serializers.CharField(allow_blank=True)
    message = serializers.CharField(allow_blank=True)


class CanonicalGlossaryEnsureResponseSerializer(serializers.Serializer):
    project_id = serializers.UUIDField()
    updated_at = serializers.DateTimeField()
    translation_glossary = serializers.DictField(child=serializers.CharField())
    summary = CanonicalGlossarySummarySerializer()
    entries = CanonicalGlossaryResultEntrySerializer(many=True)
    visual_summary = CanonicalGlossaryVisualSummarySerializer()
    visual_translations = CanonicalGlossaryVisualTranslationSerializer(many=True)
    runtime = serializers.DictField()


class PromptTranslationRetranslateSerializer(serializers.Serializer):
    model_deployment_id = serializers.UUIDField()
    source_text = serializers.CharField(
        trim_whitespace=False, required=False,
    )
    source_lang = serializers.ChoiceField(choices=("Chinese",), default="Chinese")
    target_lang = serializers.ChoiceField(choices=("English",), default="English")

    def validate_source_text(self, value):
        maximum = translation_text_max_characters()
        if len(value) > maximum:
            raise serializers.ValidationError(
                f"不能超过当前安全边界 {maximum} 个字符",
            )
        return value


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


class PromptTranslationConflictErrorSerializer(serializers.Serializer):
    status_code = serializers.IntegerField()
    code = serializers.CharField()
    details = serializers.DictField()


class PromptTranslationConflictErrorResponseSerializer(serializers.Serializer):
    error = PromptTranslationConflictErrorSerializer()


class PromptTranslationPromotionEntrySerializer(serializers.Serializer):
    entry_id = serializers.UUIDField()
    expected_revision = serializers.IntegerField(min_value=1)


class PromptTranslationPromoteShotSerializer(serializers.Serializer):
    shot_id = serializers.UUIDField()
    entries = PromptTranslationPromotionEntrySerializer(many=True, allow_empty=True)

    def validate(self, attrs):
        entries = attrs["entries"]
        if not entries:
            raise serializers.ValidationError({
                "entries": "至少选择一个已确认译文",
            })
        maximum = translation_promotion_max_entries()
        if len(entries) > maximum:
            raise serializers.ValidationError({
                "entries": f"单次最多同步 {maximum} 个镜头导演字段",
            })
        entry_ids = [item["entry_id"] for item in entries]
        if len(entry_ids) != len(set(entry_ids)):
            raise serializers.ValidationError({"entries": "同一翻译条目不能重复提交"})
        return attrs


class PromptTranslationPromoteShotResponseSerializer(serializers.Serializer):
    shot_id = serializers.UUIDField()
    promoted_fields = serializers.ListField(child=serializers.DictField())
    entries = PromptFieldTranslationSerializer(many=True)


class AssemblyJobSerializer(serializers.ModelSerializer):
    output_url = serializers.SerializerMethodField()
    download_url = serializers.SerializerMethodField()
    provenance_url = serializers.SerializerMethodField()
    ai_generated = serializers.SerializerMethodField()

    class Meta:
        model = AssemblyJob
        fields = "__all__"
        read_only_fields = tuple(field.name for field in AssemblyJob._meta.fields) + ("source_tasks",)

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
    def get_ai_generated(obj) -> bool:
        return True

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





class ReleaseBundleSerializer(serializers.ModelSerializer):
    verification_status = serializers.SerializerMethodField()

    class Meta:
        model = ReleaseBundle
        fields = (
            "id", "owner", "project", "created_by", "version", "title", "notes",
            "status", "selection", "manifest", "manifest_sha256", "sealed_at",
            "released_at", "verification_status", "created_at", "updated_at",
        )
        read_only_fields = (
            "id", "owner", "project", "created_by", "version", "status", "manifest",
            "manifest_sha256", "sealed_at", "released_at", "verification_status",
            "created_at", "updated_at",
        )

    @staticmethod
    def get_verification_status(obj) -> str:
        if obj.status == ReleaseBundle.Status.DRAFT:
            return "draft"
        try:
            from studio.services.release_bundles import verified_release_bundle

            verified_release_bundle(obj)
            return "verified"
        except (ValueError, TypeError):
            return "invalid"
