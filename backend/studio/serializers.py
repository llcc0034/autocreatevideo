from __future__ import annotations

from fractions import Fraction
from pathlib import Path, PurePosixPath, PureWindowsPath
import hashlib
import json
import re
import subprocess
import tempfile
import unicodedata
from uuid import UUID

from django.conf import settings
from django.contrib.auth import get_user_model
from django.contrib.auth.password_validation import validate_password
from django.db.models import Q
from django.urls import reverse
from django.utils.dateparse import parse_datetime
from django.utils import timezone
from PIL import Image, UnidentifiedImageError
from drf_spectacular.utils import extend_schema_field
from rest_framework import serializers

from studio.models import (
    Asset,
    AssemblyJob,
    CharacterBible,
    ConsistencyLock,
    Episode,
    EpisodeCreativeDraft,
    GeneratedWorkflow,
    GenerationTask,
    PromptFieldTranslation,
    PromptArtifact,
    PropBible,
    ReleaseBundle,
    SceneBible,
    SeriesProject,
    Shot,
    ShotDetailDraft,
    Style,
    WorkflowTemplate,
)
from studio.media_signing import classify_task_output, signed_media_token
from studio.services.provenance import ProvenanceError, assembly_manifest_path
from studio.services.reference_bindings import (
    AUDIO_REFERENCE_PURPOSES,
    DEFAULT_AUDIO_REFERENCE_PURPOSE,
    DEFAULT_VIDEO_REFERENCE_PURPOSE,
    ReferenceBindingError,
    VIDEO_REFERENCE_PURPOSES,
    normalize_shot_reference_bindings,
)
from studio.services.dialogue import DialogueContractError, normalize_dialogue
from studio.services.h3_prompts import PROMPT_COMPILER_VERSION
from studio.services.prompt_translation import (
    CanonicalTargetCollisionError,
    TranslationValidationError,
    ensure_no_canonical_target_collisions,
    resolve_current_source,
    translation_entry_context_current,
)
from studio.services.shot_prompt_recovery import legacy_negative_prompt_repair_evidence
from studio.services.resolution import (
    H3_RESOLUTION_MULTIPLE_DEFAULT,
    H3_RESOLUTION_MULTIPLE_MAX,
    H3_RESOLUTION_MULTIPLE_MIN,
    H3_RESOLUTION_PROFILES,
    ResolutionProfileError,
    validate_image_resolution_multiple,
    validate_resolution_multiple as validate_h3_resolution_multiple,
)

User = get_user_model()

VIDEO_SHOT_MIN_DURATION_MS = 5000
VIDEO_SHOT_MAX_DURATION_MS = 15000


def _validate_video_shot_duration(media_type: str, duration_ms: int) -> None:
    if (
        media_type == Shot.MediaType.VIDEO
        and not VIDEO_SHOT_MIN_DURATION_MS <= duration_ms <= VIDEO_SHOT_MAX_DURATION_MS
    ):
        raise serializers.ValidationError({
            "duration_ms": "视频分镜时长必须在 5000–15000ms",
        })


class UserSerializer(serializers.ModelSerializer):
    class Meta:
        model = User
        fields = ("id", "username", "email", "first_name", "last_name", "is_staff", "date_joined")
        read_only_fields = ("id", "is_staff", "date_joined")


class RegisterSerializer(serializers.ModelSerializer):
    password = serializers.CharField(write_only=True, min_length=8, validators=[validate_password])

    class Meta:
        model = User
        fields = ("id", "username", "email", "first_name", "last_name", "password")
        read_only_fields = ("id",)

    def create(self, validated_data):
        return User.objects.create_user(**validated_data)


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
    metadata_sha256 = serializers.SerializerMethodField()
    is_active = serializers.BooleanField(required=False, default=True)

    class Meta:
        model = Asset
        fields = "__all__"
        read_only_fields = (
            "id", "owner", "mime_type", "checksum",
            "created_at", "updated_at",
        )

    def validate_style(self, style):
        user = self.context["request"].user
        if style and style.owner_id not in (None, user.id) and not user.is_staff:
            raise serializers.ValidationError("不能使用其他用户的风格")
        return style

    def validate_kind(self, value):
        if self.instance is not None and value != self.instance.kind:
            raise serializers.ValidationError(
                "素材类别创建后不可修改；请新建正确类别的素材，避免现有 Bible 引用发生语义漂移。"
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
        kind = attrs.get("kind", getattr(self.instance, "kind", None))
        if self.instance is None:
            metadata = attrs.get("metadata") or {}
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
                if not str(attrs.get("description") or "").strip():
                    raise serializers.ValidationError({
                        "description": "分类图片素材必须填写基础描述；自动设定草稿不会读取图片像素。",
                    })
                if not str(attrs.get("prompt_hint") or "").strip():
                    raise serializers.ValidationError({
                        "prompt_hint": "分类图片素材必须填写视觉锚点；自动设定草稿只使用已保存元数据。",
                    })
        file_value = attrs.get("file", getattr(self.instance, "file", None))
        local_path = attrs.get("local_path", getattr(self.instance, "local_path", ""))
        if file_value and local_path:
            raise serializers.ValidationError("file 与 local_path 只能使用一种来源")
        uploaded = attrs.get("file")
        if uploaded:
            kind = attrs.get("kind", getattr(self.instance, "kind", None))
            mime_type, verified_metadata = self._validate_upload(uploaded, kind)
            attrs["mime_type"] = mime_type
            attrs["checksum"] = self._uploaded_checksum(uploaded)
            attrs["metadata"] = {
                **(getattr(self.instance, "metadata", {}) or {}),
                **(attrs.get("metadata") or {}),
                **verified_metadata,
            }
            self.enforce_tenant_quota(uploaded)
        return attrs

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
                    "生成产物类型不能通过该入口上传"
                ),
            })
        if uploaded.size > settings.ASSET_UPLOAD_MAX_BYTES:
            raise serializers.ValidationError({
                "file": f"单文件不能超过 {settings.ASSET_UPLOAD_MAX_BYTES} bytes",
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
                    or width > settings.ASSET_IMAGE_MAX_WIDTH
                    or height > settings.ASSET_IMAGE_MAX_HEIGHT
                    or width * height > settings.ASSET_IMAGE_MAX_PIXELS
                ):
                    raise serializers.ValidationError({
                        "file": (
                            f"图片尺寸超限；最大 {settings.ASSET_IMAGE_MAX_WIDTH}x"
                            f"{settings.ASSET_IMAGE_MAX_HEIGHT} / {settings.ASSET_IMAGE_MAX_PIXELS} pixels"
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
        specs = {
            "video": {
                "max_bytes": settings.ASSET_VIDEO_MAX_BYTES,
                "extensions": {
                    ".mp4": {"video/mp4"}, ".mov": {"video/quicktime"},
                    ".webm": {"video/webm"},
                },
                "min_seconds": 2.0, "max_seconds": 15.0,
            },
            "audio": {
                "max_bytes": settings.ASSET_AUDIO_MAX_BYTES,
                "extensions": {
                    ".wav": {"audio/wav", "audio/x-wav"}, ".mp3": {"audio/mpeg"},
                    ".flac": {"audio/flac", "audio/x-flac"},
                    ".m4a": {"audio/mp4", "audio/x-m4a"}, ".ogg": {"audio/ogg"},
                },
                "min_seconds": 2.0, "max_seconds": 15.0,
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
                    capture_output=True, text=True, timeout=30, check=False,
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
            if not specs["min_seconds"] <= duration <= specs["max_seconds"]:
                raise serializers.ValidationError({
                    "file": (
                        f"参考{media_label}时长必须在 {specs['min_seconds']:g}–"
                        f"{specs['max_seconds']:g} 秒"
                    ),
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
                if source_rate is None:
                    raise serializers.ValidationError({
                        "file": (
                            "无法可靠确定参考视频源 FPS；该素材不能安全用于 MiniMax H3 Ref2VA"
                        ),
                    })
                video_metadata = {
                    "source_fps": round(float(source_rate), 6),
                    "source_fps_fraction": str(source_rate),
                    "source_avg_frame_rate": average_rate,
                    "source_r_frame_rate": nominal_rate,
                    "ref2va_effective_fps": 24,
                    "requires_cfr24_staging": True,
                }
        except serializers.ValidationError:
            raise
        except (OSError, subprocess.SubprocessError, json.JSONDecodeError, ValueError) as exc:
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
        if used + uploaded.size > settings.ASSET_TENANT_QUOTA_BYTES:
            raise serializers.ValidationError({
                "file": (
                    f"租户素材配额不足：已使用 {used} bytes，"
                    f"上限 {settings.ASSET_TENANT_QUOTA_BYTES} bytes"
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

    @staticmethod
    def get_metadata_sha256(obj) -> str:
        from studio.services.asset_bible_drafts import _snapshot_hash

        return _snapshot_hash([obj])

class SeriesProjectSerializer(serializers.ModelSerializer):
    owner = serializers.PrimaryKeyRelatedField(read_only=True)
    episode_count = serializers.IntegerField(read_only=True)
    character_count = serializers.IntegerField(read_only=True)
    scene_count = serializers.IntegerField(read_only=True)
    prop_count = serializers.IntegerField(read_only=True)

    class Meta:
        model = SeriesProject
        fields = "__all__"
        read_only_fields = ("id", "owner", "completed_at", "created_at", "updated_at")

    def validate_style(self, style):
        user = self.context["request"].user
        if style and style.owner_id not in (None, user.id) and not user.is_staff:
            raise serializers.ValidationError("不能使用其他用户的风格")
        return style

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
        width = attrs.get("width", getattr(self.instance, "width", 768))
        height = attrs.get("height", getattr(self.instance, "height", 1344))
        model = attrs.get("target_model", getattr(self.instance, "target_model", "minimax-h3"))
        fps = attrs.get("fps", getattr(self.instance, "fps", 24))
        default_duration = attrs.get(
            "default_shot_duration_ms", getattr(self.instance, "default_shot_duration_ms", 5000),
        )
        multiple = 32 if "minimax-h3" in model.lower() else 8
        if width % multiple or height % multiple:
            raise serializers.ValidationError(f"width 和 height 必须是 {multiple} 的倍数")
        if "minimax-h3" in model.lower() and fps != 24:
            raise serializers.ValidationError("MiniMax H3 本地工作流固定使用 24 fps")
        if "minimax-h3" in model.lower() and (min(width, height) > 768 or max(width, height) > 1344):
            raise serializers.ValidationError("当前 MiniMax H3 profile 限制短边不超过 768、长边不超过 1344；未配置 1080p/4K 放大流程")
        if "minimax-h3" in model.lower() and not 5000 <= default_duration <= 15000:
            raise serializers.ValidationError({
                "default_shot_duration_ms": "MiniMax H3 默认分镜时长必须在 5000–15000ms",
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
        if not user.is_staff and any(asset.owner_id not in (None, user.id) for asset in assets):
            raise serializers.ValidationError({"reference_assets": "不能使用其他用户的素材"})
        if self.Meta.model in {CharacterBible, SceneBible, PropBible}:
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
            from studio.services.asset_bible_drafts import provenance_snapshots_equal

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
                from studio.services.asset_bible_drafts import _snapshot_hash

                current_hash = _snapshot_hash([source_asset])
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
    class Meta:
        model = CharacterBible
        fields = "__all__"
        read_only_fields = ("id", "created_at", "updated_at")

    def validate_anchor_asset(self, asset):
        user = self.context["request"].user
        if asset and asset.owner_id not in (None, user.id) and not user.is_staff:
            raise serializers.ValidationError("不能使用其他用户的素材")
        return asset


class SceneBibleSerializer(ProjectOwnedSerializer):
    class Meta:
        model = SceneBible
        fields = "__all__"
        read_only_fields = ("id", "created_at", "updated_at")


class PropBibleSerializer(ProjectOwnedSerializer):
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
        requested_status = attrs.get("status", getattr(self.instance, "status", Episode.Status.DRAFT))
        if requested_status == Episode.Status.COMPLETED and (
            not self.instance
            or not self.instance.assembly_jobs.filter(status=AssemblyJob.Status.SUCCEEDED).exists()
        ):
            raise serializers.ValidationError({"status": "只有成功完成单集组装后才能标记 completed"})
        return attrs


class H3ReferenceEntityBindingSchema(serializers.Serializer):
    entity_id = serializers.UUIDField()
    asset_id = serializers.UUIDField()


class H3ReferenceVideoBindingSchema(serializers.Serializer):
    asset_id = serializers.UUIDField()
    purpose = serializers.ChoiceField(
        choices=sorted(VIDEO_REFERENCE_PURPOSES),
        default=DEFAULT_VIDEO_REFERENCE_PURPOSE,
    )
    use_embedded_audio = serializers.BooleanField(default=False)
    audio_asset_id = serializers.UUIDField(required=False)
    audio_purpose = serializers.ChoiceField(
        choices=sorted(AUDIO_REFERENCE_PURPOSES),
        required=False,
        default=DEFAULT_AUDIO_REFERENCE_PURPOSE,
    )


class H3ReferenceAudioBindingSchema(serializers.Serializer):
    asset_id = serializers.UUIDField()
    purpose = serializers.ChoiceField(
        choices=sorted(AUDIO_REFERENCE_PURPOSES),
        default=DEFAULT_AUDIO_REFERENCE_PURPOSE,
    )


class H3ReferenceBindingsSchema(serializers.Serializer):
    """OpenAPI shape only; runtime validation remains the strict canonical service."""

    first_frame_asset_id = serializers.UUIDField(required=False)
    last_frame_asset_id = serializers.UUIDField(required=False)
    characters = H3ReferenceEntityBindingSchema(many=True, required=False)
    scene = H3ReferenceEntityBindingSchema(required=False)
    props = H3ReferenceEntityBindingSchema(many=True, required=False)
    videos = H3ReferenceVideoBindingSchema(many=True, required=False)
    audios = H3ReferenceAudioBindingSchema(many=True, required=False)


@extend_schema_field(H3ReferenceBindingsSchema)
class H3ReferenceBindingsJSONField(serializers.JSONField):
    """Preserve raw JSON so unknown keys still reach strict service validation."""


class ShotSerializer(serializers.ModelSerializer):
    reference_bindings = H3ReferenceBindingsJSONField(required=False)
    legacy_negative_prompt_repair_eligible = serializers.SerializerMethodField()

    @extend_schema_field(serializers.BooleanField())
    def get_legacy_negative_prompt_repair_eligible(self, obj: Shot) -> bool:
        return legacy_negative_prompt_repair_evidence(obj) is not None

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
        media_type = attrs.get(
            "media_type", getattr(self.instance, "media_type", Shot.MediaType.VIDEO),
        )
        duration_ms = attrs.get(
            "duration_ms", getattr(self.instance, "duration_ms", 5000),
        )
        _validate_video_shot_duration(media_type, duration_ms)
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
        if reference_assets is not None and not user.is_staff:
            if any(asset.owner_id not in (None, user.id) for asset in reference_assets):
                raise serializers.ValidationError({"reference_assets": "不能使用其他用户的素材"})
        characters = attrs.get("characters")
        if characters is None and self.instance:
            characters = self.instance.characters.all()
        speaker_ids = {item.stable_speaker_id for item in (characters or [])}
        dialogue = attrs.get("dialogue", getattr(self.instance, "dialogue", []))
        unknown = {item["speaker_id"] for item in dialogue if item.get("speaker_id") not in speaker_ids}
        if unknown:
            raise serializers.ValidationError({"dialogue": f"未绑定的人物 speaker_id: {', '.join(sorted(unknown))}"})
        mode = attrs.get("h3_mode", getattr(self.instance, "h3_mode", Shot.H3Mode.T2VA))
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
        try:
            attrs["reference_bindings"] = normalize_shot_reference_bindings(
                mode=mode, bindings=bindings, project=episode.project,
                characters=list(characters), scene=scene, props=list(props),
                shot_reference_assets=list(shot_assets), user=user,
            )
        except ReferenceBindingError as exc:
            raise serializers.ValidationError({"reference_bindings": str(exc)}) from exc
        return attrs


class ShotSyncItemSerializer(serializers.Serializer):
    """One item in an episode's authoritative, ordered shot snapshot."""

    id = serializers.UUIDField(required=False)
    number = serializers.IntegerField(min_value=1)
    title = serializers.CharField(max_length=200, required=False, allow_blank=True)
    narrative = serializers.CharField(required=False, allow_blank=True)
    detailed_description = serializers.CharField(required=False, allow_blank=True)
    camera = serializers.CharField(required=False, allow_blank=True)
    action = serializers.CharField(required=False, allow_blank=True)
    dialogue = serializers.ListField(child=serializers.JSONField(), required=False)
    sound_effects = serializers.CharField(required=False, allow_blank=True)
    music = serializers.CharField(required=False, allow_blank=True)
    duration_ms = serializers.IntegerField(min_value=100, required=False)
    media_type = serializers.ChoiceField(choices=Shot.MediaType.choices, required=False)
    h3_mode = serializers.ChoiceField(choices=Shot.H3Mode.choices, required=False)
    include_audio = serializers.BooleanField(required=False)
    scene = serializers.PrimaryKeyRelatedField(
        queryset=SceneBible.objects.all(), required=False, allow_null=True,
    )
    characters = serializers.PrimaryKeyRelatedField(
        queryset=CharacterBible.objects.all(), many=True, required=False,
    )
    props = serializers.PrimaryKeyRelatedField(
        queryset=PropBible.objects.all(), many=True, required=False,
    )
    reference_assets = serializers.PrimaryKeyRelatedField(
        queryset=Asset.objects.all(), many=True, required=False,
    )
    reference_bindings = H3ReferenceBindingsJSONField(required=False)
    continuity_notes = serializers.CharField(required=False, allow_blank=True)
    metadata = serializers.JSONField(required=False)

    def validate_dialogue(self, value):
        try:
            return normalize_dialogue(value)
        except DialogueContractError as exc:
            raise serializers.ValidationError(str(exc)) from exc

    def validate_metadata(self, value):
        if not isinstance(value, dict):
            raise serializers.ValidationError("metadata 必须是 JSON 对象")
        return value

    def validate_reference_bindings(self, value):
        if not isinstance(value, dict):
            raise serializers.ValidationError("reference_bindings 必须是 JSON 对象")
        return value

    def validate(self, attrs):
        current = None
        episode = self.context.get("episode")
        item_id = attrs.get("id")
        if episode is not None and item_id:
            current = episode.shots.only("media_type", "duration_ms").filter(pk=item_id).first()
            # The parent serializer reports foreign or unknown IDs.
            if current is None:
                return attrs
        media_type = attrs.get(
            "media_type", getattr(current, "media_type", Shot.MediaType.VIDEO),
        )
        duration_ms = attrs.get("duration_ms", getattr(current, "duration_ms", 5000))
        _validate_video_shot_duration(media_type, duration_ms)
        return attrs


class EpisodeShotSyncSerializer(serializers.Serializer):
    shots = ShotSyncItemSerializer(many=True, allow_empty=True)

    def validate_shots(self, shots):
        ids = [str(item["id"]) for item in shots if item.get("id")]
        numbers = [item["number"] for item in shots]
        if len(ids) != len(set(ids)):
            raise serializers.ValidationError("id 不能重复")
        if len(numbers) != len(set(numbers)):
            raise serializers.ValidationError("number 不能重复")
        for index, item in enumerate(shots):
            if not item.get("id") and not item.get("narrative", "").strip():
                raise serializers.ValidationError(f"第 {index + 1} 项新分镜必须提供 narrative")
        return shots


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
    metadata = serializers.JSONField(required=False)
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


class ProjectBibleSyncSerializer(serializers.Serializer):
    characters = CharacterBibleSyncItemSerializer(many=True, required=False, allow_empty=True)
    scenes = SceneBibleSyncItemSerializer(many=True, required=False, allow_empty=True)
    props = PropBibleSyncItemSerializer(many=True, required=False, allow_empty=True)

    def validate(self, attrs):
        if not any(key in attrs for key in ("characters", "scenes", "props")):
            raise serializers.ValidationError("characters、scenes、props 至少提供一个完整列表")
        for key in ("characters", "scenes", "props"):
            if key not in attrs:
                continue
            items = attrs[key]
            ids = [str(item["id"]) for item in items if item.get("id")]
            slugs = [item["slug"] for item in items]
            if len(ids) != len(set(ids)):
                raise serializers.ValidationError({key: "id 不能重复"})
            if len(slugs) != len(set(slugs)):
                raise serializers.ValidationError({key: "slug 不能重复"})
        characters = attrs.get("characters")
        if characters is not None:
            speaker_ids = [item["stable_speaker_id"] for item in characters]
            if len(speaker_ids) != len(set(speaker_ids)):
                raise serializers.ValidationError({"characters": "stable_speaker_id 不能重复"})
        normalized_names: dict[str, str] = {}
        for key in ("characters", "scenes", "props"):
            for item in attrs.get(key, []):
                normalized = unicodedata.normalize(
                    "NFKC", str(item.get("name") or ""),
                ).strip().casefold()
                previous = normalized_names.get(normalized)
                if normalized and previous is not None:
                    raise serializers.ValidationError({
                        key: (
                            f"名称“{item['name']}”与 {previous} 重复；"
                            "项目内人物、场景和道具名称必须唯一。"
                        ),
                    })
                if normalized:
                    normalized_names[normalized] = f"{key}/{item['name']}"
        return attrs


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
    duration_ms = serializers.IntegerField(min_value=100)


class PromptGenerateSerializer(serializers.Serializer):
    project_id = serializers.UUIDField()
    episode_id = serializers.UUIDField(required=False)
    shot_ids = serializers.ListField(child=serializers.UUIDField(), required=False, allow_empty=False)
    prompt_type = serializers.ChoiceField(choices=PromptArtifact.PromptType.choices, default=PromptArtifact.PromptType.VIDEO)
    h3_mode = serializers.ChoiceField(
        choices=Shot.H3Mode.choices, default=Shot.H3Mode.T2VA,
        help_text=(
            "用户工作流为 T2VA、FL2VA（首尾帧槽位可为 0/1/2）和 Ref2VA；"
            "I2VA/L2VA 是首尾帧工作流按显式槽位得到的有效模式及历史兼容值。"
        ),
    )
    multi_shot = serializers.BooleanField(default=False)
    include_audio = serializers.BooleanField(default=True)
    total_duration_ms = serializers.IntegerField(min_value=100, required=False)
    shot_durations = DurationOverrideSerializer(many=True, required=False, default=list)
    overall_soundscape = serializers.CharField(required=False, allow_blank=True, default="")
    non_diegetic_music = serializers.CharField(required=False, allow_blank=True, default="")
    extra_direction = serializers.CharField(required=False, allow_blank=True, default="")
    reference_bindings = H3ReferenceBindingsJSONField(required=False)
    reference_asset_ids = serializers.ListField(
        child=serializers.UUIDField(), required=False, allow_empty=True,
    )

    def validate(self, attrs):
        if not attrs.get("episode_id") and not attrs.get("shot_ids"):
            raise serializers.ValidationError("episode_id 和 shot_ids 至少提供一个")
        if attrs["prompt_type"] == PromptArtifact.PromptType.IMAGE and attrs["multi_shot"]:
            raise serializers.ValidationError("图片提示词不支持 multi_shot")
        ids = [str(item) for item in attrs.get("shot_ids", [])]
        if len(ids) != len(set(ids)):
            raise serializers.ValidationError("shot_ids 不能重复")
        override_ids = [str(item["shot_id"]) for item in attrs.get("shot_durations", [])]
        if len(override_ids) != len(set(override_ids)):
            raise serializers.ValidationError("shot_durations 中 shot_id 不能重复")
        reference_asset_ids = [str(value) for value in attrs.get("reference_asset_ids", [])]
        if len(reference_asset_ids) != len(set(reference_asset_ids)):
            raise serializers.ValidationError("reference_asset_ids 不能重复")
        if "reference_bindings" in attrs and not isinstance(attrs["reference_bindings"], dict):
            raise serializers.ValidationError({"reference_bindings": "必须是 JSON 对象"})
        return attrs


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


class ProductionWorkflowTemplateSerializer(serializers.ModelSerializer):
    """Path-free, non-executable template metadata for production selectors."""

    class Meta:
        model = WorkflowTemplate
        fields = (
            "id", "name", "slug", "version", "output_kind", "description",
            "input_schema", "is_system", "is_active",
        )
        read_only_fields = fields

    def to_representation(self, instance):
        representation = super().to_representation(instance)
        schema = instance.input_schema if isinstance(instance.input_schema, dict) else {}
        representation["input_schema"] = {
            key: schema[key]
            for key in (
                "h3_modes", "is_default", "maturity", "user_parameters",
                "model_profile", "source_workflow", "required_custom_nodes",
            )
            if key in schema
        }
        source_workflow = representation["input_schema"].get("source_workflow")
        if isinstance(source_workflow, str):
            representation["input_schema"]["source_workflow"] = min(
                PurePosixPath(source_workflow).name,
                PureWindowsPath(source_workflow).name,
                key=len,
            )
        return representation


class GeneratedWorkflowSerializer(serializers.ModelSerializer):
    class Meta:
        model = GeneratedWorkflow
        fields = "__all__"
        read_only_fields = tuple(field.name for field in GeneratedWorkflow._meta.fields)


class WorkflowGenerateSerializer(serializers.Serializer):
    prompt_artifact_id = serializers.UUIDField()
    template_id = serializers.UUIDField()
    overrides = serializers.JSONField(required=False, default=dict)

    def validate_overrides(self, value):
        if not isinstance(value, dict):
            raise serializers.ValidationError("overrides must be a JSON object")
        return value


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
    h3_mode = serializers.CharField(allow_blank=True, read_only=True)
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
        help_text="Legacy continuity-Bible assets associated with, but not submitted to, T2VA.",
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
    h3_mode = serializers.CharField(allow_blank=True, read_only=True)
    effective_conditioning_mode = serializers.CharField(allow_blank=True, read_only=True)
    prompt_template_key = serializers.CharField(allow_blank=True, read_only=True)
    content_hash = serializers.CharField(allow_blank=True, read_only=True)
    prompt = serializers.CharField(allow_blank=True, read_only=True)
    negative_prompt = serializers.CharField(allow_blank=True, read_only=True)
    prompt_character_count = serializers.IntegerField(read_only=True)
    negative_prompt_character_count = serializers.IntegerField(read_only=True)
    prompt_source = serializers.ChoiceField(
        choices=(
            (
                "generated_workflow.injected_values.PROMPT",
                "generated_workflow.injected_values.PROMPT",
            ),
            ("prompt_artifact.prompt_text", "prompt_artifact.prompt_text"),
        ),
        read_only=True,
    )
    negative_prompt_source = serializers.ChoiceField(
        choices=(
            (
                "generated_workflow.injected_values.NEGATIVE_PROMPT",
                "generated_workflow.injected_values.NEGATIVE_PROMPT",
            ),
            ("prompt_artifact.negative_prompt", "prompt_artifact.negative_prompt"),
        ),
        read_only=True,
    )


class GenerationTaskSerializer(serializers.ModelSerializer):
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
        fields = "__all__"
        read_only_fields = (
            "id", "owner", "project", "episode", "kind", "status", "progress", "attempts",
            "comfy_prompt_id", "client_id", "outputs", "error_message", "queued_at", "started_at",
            "finished_at", "created_at", "updated_at",
            "backend", "model_profile", "workflow_version", "seed", "effective_frames",
            "effective_duration_ms", "capability_snapshot", "staged_inputs",
            "submission_token", "submission_state", "submission_prepared_at", "submission_submitted_at",
            "heartbeat_at", "stalled_at", "remote_state", "remote_error",
            "progress_source", "comfy_progress", "progress_node_id", "progress_updated_at",
            "progress_telemetry_state", "progress_telemetry_error",
        )

    @staticmethod
    def _task_mapping(value) -> dict:
        return value if isinstance(value, dict) else {}

    @staticmethod
    def _task_value(primary: dict, key: str, *fallbacks):
        """Prefer a persisted capability value without treating false/zero as missing."""
        if key in primary and primary[key] is not None:
            return primary[key]
        return next((value for value in fallbacks if value is not None), None)

    @staticmethod
    def _task_int(value, default: int = 0) -> int:
        try:
            return int(value)
        except (TypeError, ValueError, OverflowError):
            return default

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
        for raw in raw_assets:
            if isinstance(raw, str):
                asset_id = cls._task_uuid_text(raw)
                if not asset_id:
                    continue
                raw = {"asset_id": asset_id}
            if not isinstance(raw, dict):
                continue
            media_metadata = raw.get("media_metadata")
            if not isinstance(media_metadata, dict):
                media_metadata = {}
            summaries.append({
                "asset_id": cls._task_uuid_text(raw.get("asset_id") or raw.get("id")),
                "name": cls._task_public_text(raw.get("name")),
                "category": cls._task_public_text(raw.get("category")),
                "input_slot": cls._task_optional_int(raw.get("input_slot")),
                "input_label": cls._task_public_text(raw.get("input_label")),
                "project_slot": cls._task_optional_int(raw.get("project_slot")),
                "entity_id": cls._task_uuid_text(raw.get("entity_id")),
                "entity_name": cls._task_public_text(raw.get("entity_name")),
                "entity_label": cls._task_public_text(raw.get("entity_label")),
                "checksum": cls._task_public_text(raw.get("checksum")),
                "use_embedded_audio": cls._task_bool(raw.get("use_embedded_audio")),
                "audio_asset_id": cls._task_uuid_text(raw.get("audio_asset_id")),
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
                    raw.get("effective_fps", media_metadata.get("ref2va_effective_fps")),
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
            category = str(item.get("category") or "").lower()
            input_label = str(item.get("input_label") or "").lower()
            if category in {"video", "reference_video"} or input_label.startswith("<video"):
                video_count += 1
                if (
                    item.get("audio_asset_id")
                    or item.get("audio_input_label")
                    or item.get("use_embedded_audio")
                ):
                    audio_count += 1
            elif category in {"audio", "reference_audio"} or input_label.startswith("<audio"):
                audio_count += 1
            else:
                image_count += 1
        return image_count, video_count, audio_count

    @staticmethod
    def _task_reference_media_kind(item: dict, asset: Asset | None = None) -> str:
        # A task snapshot describes the media that was actually bound.  Live
        # Asset metadata may fill a historical gap but must not rewrite it.
        category = str(item.get("category") or "").casefold()
        label = str(item.get("input_label") or "").casefold()
        if category in {"video", "reference_video"} or label.startswith("<video"):
            return "video"
        if category in {"audio", "reference_audio"} or label.startswith("<audio"):
            return "audio"
        if category or label.startswith("<picture"):
            return "image"
        if asset is not None:
            if asset.kind in {Asset.Kind.REFERENCE_VIDEO, Asset.Kind.OUTPUT_VIDEO}:
                return "video"
            if asset.kind in {Asset.Kind.REFERENCE_AUDIO, Asset.Kind.AUDIO}:
                return "audio"
            mime_type = str(asset.mime_type or "").casefold()
            if mime_type.startswith("video/"):
                return "video"
            if mime_type.startswith("audio/"):
                return "audio"
        return "image"

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
                "media_kind": self._task_reference_media_kind(item, asset),
                "display_name": self._task_public_text(display_name),
                "preview_url": self._task_asset_preview_url(asset) if asset is not None else "",
            })
        return result

    @classmethod
    def _task_shot_ids(cls, snapshot: dict, prompt) -> list[str]:
        raw_ids = snapshot.get("shot_ids") if isinstance(snapshot.get("shot_ids"), list) else None
        if raw_ids is None:
            request_options = cls._task_mapping(prompt.request_options)
            raw_ids = request_options.get("shot_ids") if isinstance(request_options.get("shot_ids"), list) else None
        if raw_ids is None:
            raw_ids = [prompt.shot_id] if prompt.shot_id else []
        result = []
        for value in raw_ids:
            normalized = cls._task_uuid_text(value)
            if normalized and normalized not in result:
                result.append(normalized)
        return result

    @classmethod
    def build_input_prompt_payload(cls, obj) -> dict:
        """Resolve the exact persisted text submitted by this generated workflow."""
        workflow = obj.generated_workflow
        injected = cls._task_mapping(workflow.injected_values)
        prompt_artifact = workflow.prompt_artifact
        structured_payload = cls._task_mapping(prompt_artifact.structured_payload)
        snapshot = cls._task_mapping(obj.capability_snapshot)

        if "PROMPT" in injected and isinstance(injected["PROMPT"], str):
            prompt_text = injected["PROMPT"]
            prompt_source = "generated_workflow.injected_values.PROMPT"
        else:
            prompt_text = (
                prompt_artifact.prompt_text
                if isinstance(prompt_artifact.prompt_text, str) else ""
            )
            prompt_source = "prompt_artifact.prompt_text"
        if "NEGATIVE_PROMPT" in injected and isinstance(injected["NEGATIVE_PROMPT"], str):
            negative_prompt = injected["NEGATIVE_PROMPT"]
            negative_prompt_source = "generated_workflow.injected_values.NEGATIVE_PROMPT"
        else:
            negative_prompt = (
                prompt_artifact.negative_prompt
                if isinstance(prompt_artifact.negative_prompt, str) else ""
            )
            negative_prompt_source = "prompt_artifact.negative_prompt"

        prompt_template_key = cls._task_value(
            snapshot,
            "prompt_template_key",
            injected.get("PROMPT_TEMPLATE_KEY"),
            structured_payload.get("prompt_template_key"),
            "",
        )
        h3_mode = (
            cls._task_public_text(prompt_artifact.h3_mode)
            if obj.kind == GenerationTask.Kind.VIDEO else ""
        )
        effective_conditioning_mode = cls._task_public_text(cls._task_value(
            snapshot,
            "effective_conditioning_mode",
            injected.get("EFFECTIVE_CONDITIONING_MODE"),
            structured_payload.get("effective_conditioning_mode"),
            h3_mode if obj.kind == GenerationTask.Kind.VIDEO else "TEXT_ONLY",
        ))
        return {
            "task_id": str(obj.id),
            "prompt_artifact_id": str(prompt_artifact.id),
            "prompt_type": prompt_artifact.prompt_type,
            "model": cls._task_public_text(prompt_artifact.model),
            "h3_mode": h3_mode,
            "effective_conditioning_mode": effective_conditioning_mode,
            "prompt_template_key": cls._task_public_text(prompt_template_key),
            "content_hash": cls._task_public_text(prompt_artifact.content_hash),
            "prompt": prompt_text,
            "negative_prompt": negative_prompt,
            "prompt_character_count": len(prompt_text),
            "negative_prompt_character_count": len(negative_prompt),
            "prompt_source": prompt_source,
            "negative_prompt_source": negative_prompt_source,
        }

    @extend_schema_field(GenerationTaskInputParametersSerializer)
    def get_input_parameters(self, obj) -> dict:
        snapshot = self._task_mapping(obj.capability_snapshot)
        workflow = obj.generated_workflow
        injected = self._task_mapping(workflow.injected_values)
        prompt = workflow.prompt_artifact
        payload = self._task_mapping(prompt.structured_payload)
        template = workflow.template
        schema = self._task_mapping(template.input_schema)
        project = obj.project
        is_image = (
            obj.kind == GenerationTask.Kind.IMAGE
            or prompt.prompt_type == PromptArtifact.PromptType.IMAGE
        )

        def injected_value(key: str):
            return injected[key] if key in injected else None

        h3_mode = self._task_public_text(self._task_value(
            snapshot, "h3_mode", injected_value("REQUESTED_H3_MODE"),
            injected_value("H3_MODE"), prompt.h3_mode,
        ))
        effective_conditioning_mode = self._task_public_text(self._task_value(
            snapshot, "effective_conditioning_mode",
            injected_value("EFFECTIVE_CONDITIONING_MODE"),
            payload.get("effective_conditioning_mode"), h3_mode,
        ))
        workflow_mode = self._task_public_text(self._task_value(
            snapshot, "workflow_mode", injected_value("WORKFLOW_MODE"),
            schema.get("workflow_mode"), "legacy",
        ))
        parameter_definitions = self._task_mapping(schema.get("user_parameters"))
        workflow_parameters = {}
        for key, definition in parameter_definitions.items():
            if not isinstance(key, str) or not isinstance(definition, dict):
                continue
            value = self._task_value(
                self._task_mapping(snapshot.get("workflow_parameters")),
                key,
                injected_value(key),
                definition.get("default"),
            )
            if isinstance(value, bool):
                workflow_parameters[key] = value
            elif isinstance(value, int):
                workflow_parameters[key] = value
            elif isinstance(value, float):
                workflow_parameters[key] = value if value == value else None
            elif value is not None:
                workflow_parameters[key] = self._task_public_text(value)
        lora_model = self._task_public_text(schema.get("lora_model"))
        if lora_model:
            workflow_parameters["LORA_MODEL"] = lora_model

        reference_raw = None
        for mapping, key in (
            (snapshot, "reference_assets"),
            (injected, "REFERENCE_BINDINGS"),
            (injected, "REFERENCE_IMAGES"),
            (payload, "reference_bindings"),
            (payload, "reference_assets"),
        ):
            if isinstance(mapping.get(key), list) and mapping[key]:
                reference_raw = mapping[key]
                break
        persisted_assets = self._task_reference_assets_for_request(reference_raw or [])
        continuity_raw = payload.get("continuity_assets")
        persisted_continuity_assets = self._task_reference_assets_for_request(
            continuity_raw if isinstance(continuity_raw, list) else []
        )
        staged_by_asset = {
            str(row.get("asset_id")): row
            for row in (obj.staged_inputs or [])
            if isinstance(row, dict) and row.get("asset_id")
        }
        for summary in persisted_assets:
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
        # Early T2VA tasks copied continuity-Bible material into their task
        # snapshots even though the text-to-video graph had no image/reference
        # inputs.  Keep that useful historical association, but never describe
        # it as conditioning that was submitted to ComfyUI.
        is_text_only_conditioning = (
            effective_conditioning_mode.casefold() == Shot.H3Mode.T2VA.casefold()
            or workflow_mode.casefold() == "text_to_video"
            or workflow_mode.casefold() == "text_to_image"
        )
        if is_text_only_conditioning:
            reference_assets = []
            continuity_assets = persisted_continuity_assets or persisted_assets
        else:
            reference_assets = persisted_assets
            continuity_assets = []
        image_count, video_count, audio_count = self._task_reference_counts(reference_assets)
        shot_ids = self._task_shot_ids(snapshot, prompt)
        input_prompt = self.build_input_prompt_payload(obj)

        batch_kind = self._task_value(snapshot, "batch_kind")
        whole_episode = self._task_bool(
            self._task_value(
                snapshot,
                "whole_episode",
                self._task_mapping(prompt.request_options).get("whole_episode"),
                batch_kind == "episode",
            )
        )
        parameters = {
            "model": self._task_public_text(self._task_value(
                snapshot, "model", injected_value("MODEL"), prompt.model, template.model,
            )),
            "model_profile": self._task_public_text(self._task_value(
                snapshot, "model_profile", obj.model_profile,
            )),
            "h3_mode": h3_mode,
            "effective_conditioning_mode": effective_conditioning_mode,
            "workflow_mode": workflow_mode,
            "template_slug": self._task_public_text(self._task_value(
                snapshot, "template_slug", injected_value("WORKFLOW_TEMPLATE_SLUG"), template.slug,
            )),
            "source_workflow": self._task_public_text(self._task_value(
                snapshot, "source_workflow", injected_value("SOURCE_WORKFLOW"),
                schema.get("source_workflow"), "",
            )),
            "workflow_parameters": workflow_parameters,
            "prompt_template_key": self._task_public_text(self._task_value(
                snapshot, "prompt_template_key", injected_value("PROMPT_TEMPLATE_KEY"),
                payload.get("prompt_template_key"), "",
            )),
            "resolution_profile": self._task_public_text(self._task_value(
                snapshot, "resolution_profile", injected_value("RESOLUTION_PROFILE"), "project",
            )),
            "width": self._task_int(self._task_value(
                snapshot, "width", injected_value("WIDTH"), project.width,
            )),
            "height": self._task_int(self._task_value(
                snapshot, "height", injected_value("HEIGHT"), project.height,
            )),
            "resolution_multiple": self._task_int(self._task_value(
                snapshot, "resolution_multiple", injected_value("RESOLUTION_MULTIPLE"),
                H3_RESOLUTION_MULTIPLE_DEFAULT,
            ), H3_RESOLUTION_MULTIPLE_DEFAULT),
            "seed": self._task_int(self._task_value(
                snapshot, "seed", injected_value("SEED"), obj.seed,
            )),
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
            "shot_count": self._task_int(self._task_value(
                snapshot, "shot_count", len(shot_ids),
            ), len(shot_ids)),
            "shot_ids": shot_ids,
            "batch_id": self._task_public_text(self._task_value(snapshot, "batch_id")) or None,
            "batch_kind": self._task_public_text(batch_kind) or None,
            "batch_role": self._task_public_text(self._task_value(snapshot, "batch_role")) or None,
            "batch_index": self._task_optional_int(self._task_value(snapshot, "batch_index")),
            "batch_size": self._task_optional_int(self._task_value(snapshot, "batch_size")),
            "whole_episode": whole_episode,
            "idempotency_key": (
                self._task_public_text(self._task_value(snapshot, "idempotency_key")) or None
            ),
        }
        if not is_image:
            parameters.update({
                "fps": self._task_int(self._task_value(
                    snapshot, "fps", injected_value("FPS"), project.fps,
                )),
                "requested_duration_ms": self._task_int(self._task_value(
                    snapshot, "requested_duration_ms", injected_value("REQUESTED_DURATION_MS"),
                    prompt.total_duration_ms,
                )),
                "effective_duration_ms": self._task_int(self._task_value(
                    snapshot, "effective_duration_ms", injected_value("EFFECTIVE_DURATION_MS"),
                    obj.effective_duration_ms,
                )),
                "effective_frames": self._task_int(self._task_value(
                    snapshot, "effective_frames", injected_value("FRAMES"), obj.effective_frames,
                )),
                "include_audio": self._task_bool(self._task_value(
                    snapshot, "include_audio", injected_value("AUDIO_ENABLED"),
                    prompt.include_audio,
                )),
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
                "capability_snapshot": {},
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
        threshold = float(getattr(settings, "TASK_STALL_THRESHOLD_SECONDS", 30))
        return (timezone.now() - last_activity).total_seconds() > threshold

    def get_can_retry(self, obj) -> bool:
        if obj.status not in {GenerationTask.Status.FAILED, GenerationTask.Status.CANCELLED}:
            return False
        if obj.outputs:
            return False
        terminal_states = {
            GenerationTask.SubmissionState.TERMINAL_FAILED,
            GenerationTask.SubmissionState.TERMINAL_CANCELLED,
        }
        if obj.submission_token and obj.submission_state not in terminal_states:
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
        if obj.status != GenerationTask.Status.SUCCEEDED or not isinstance(obj.outputs, list):
            return []
        media = []
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
                "publish_url": reverse("task-publish-output", kwargs={"pk": obj.id}),
                "promoted_asset_id": (
                    self._task_public_text(item.get("promoted_asset_id"))
                    if kind == "image" and isinstance(item, dict) else ""
                ),
                "promote_to_asset_url": (
                    reverse("task-promote-output-to-asset", kwargs={"pk": obj.id})
                    if kind == "image" else ""
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
    def _episode(obj):
        if obj.episode_id:
            return obj.episode
        return obj.generated_workflow.prompt_artifact.episode

    @staticmethod
    def _shot(obj):
        return obj.generated_workflow.prompt_artifact.shot

    def get_batch_id(self, obj) -> str | None:
        return self._snapshot(obj).get("batch_id") or None

    def get_batch_kind(self, obj) -> str | None:
        return self._snapshot(obj).get("batch_kind") or None

    def get_batch_role(self, obj) -> str | None:
        return self._snapshot(obj).get("batch_role") or None

    def get_batch_index(self, obj) -> int | None:
        return self._snapshot(obj).get("batch_index")

    def get_batch_size(self, obj) -> int | None:
        return self._snapshot(obj).get("batch_size")

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


class ComfyUIWorkflowSyncSerializer(serializers.Serializer):
    """Result of explicitly saving a task canvas to ComfyUI user data."""

    task_id = serializers.UUIDField(read_only=True)
    generated_workflow_id = serializers.UUIDField(read_only=True)
    synced = serializers.BooleanField(read_only=True)
    workflow_path = serializers.CharField(read_only=True)
    filename = serializers.CharField(read_only=True)
    comfyui_url = serializers.URLField(read_only=True)
    open_reason = serializers.CharField(read_only=True)


class TaskOutputPromoteSerializer(serializers.Serializer):
    output_index = serializers.IntegerField(min_value=0)
    name = serializers.CharField(max_length=160, required=False, allow_blank=True)
    reference_category = serializers.ChoiceField(
        choices=(Asset.Kind.CHARACTER, Asset.Kind.SCENE, Asset.Kind.PROP),
        required=False,
        allow_null=True,
        default=None,
    )
    entity_id = serializers.UUIDField(required=False, allow_null=True, default=None)

    def validate(self, attrs):
        category = attrs.get("reference_category")
        entity_id = attrs.get("entity_id")
        if bool(category) != bool(entity_id):
            raise serializers.ValidationError({
                "reference_category": "分类参考必须同时选择人物、场景或道具实体",
                "entity_id": "分类参考必须同时选择人物、场景或道具实体",
            })
        return attrs


class GenerationTaskCreateSerializer(serializers.Serializer):
    generated_workflow_id = serializers.UUIDField()
    priority = serializers.IntegerField(min_value=-100, max_value=100, default=0)
    max_attempts = serializers.IntegerField(min_value=1, max_value=10, default=3)


class NextEpisodeSerializer(serializers.Serializer):
    title = serializers.CharField(max_length=200)
    synopsis = serializers.CharField(required=False, allow_blank=True, default="")
    script = serializers.CharField(required=False, allow_blank=True, default="")


class ProductionWorkflowSelectionSerializer(serializers.Serializer):
    output_kind = serializers.CharField(help_text="当前固定为 video")
    h3_mode = serializers.ChoiceField(choices=Shot.H3Mode.choices)
    template_id = serializers.UUIDField()
    parameters = serializers.DictField(
        child=serializers.FloatField(), required=False, default=dict,
    )


@extend_schema_field(ProductionWorkflowSelectionSerializer(many=True))
class ProductionWorkflowSelectionsField(serializers.JSONField):
    pass


class ProductionBuildSerializer(serializers.Serializer):
    template_id = serializers.UUIDField(required=False, allow_null=True, default=None)
    workflow_selections = ProductionWorkflowSelectionsField(
        required=False, default=list,
    )
    episode_ids = serializers.ListField(
        child=serializers.UUIDField(), required=False, allow_empty=True, default=list,
    )
    shot_ids = serializers.ListField(
        child=serializers.UUIDField(), required=False, allow_empty=True, default=list,
    )
    multi_shot = serializers.BooleanField(default=True)
    # Retained for backward-compatible clients. Production now always respects
    # each Shot's own include_audio and h3_mode values.
    include_audio = serializers.BooleanField(
        default=True, help_text="兼容旧客户端；生产任务实际使用每个已保存 Shot 的 include_audio。",
    )
    h3_mode = serializers.ChoiceField(
        choices=Shot.H3Mode.choices, default=Shot.H3Mode.T2VA,
        help_text="兼容旧客户端；生产任务实际使用每个已保存 Shot 的 h3_mode。",
    )
    enqueue = serializers.BooleanField(default=True)
    whole_episode = serializers.BooleanField(default=False)
    idempotency_key = serializers.CharField(
        required=False, allow_blank=True, default="", max_length=120, trim_whitespace=True,
    )
    priority = serializers.IntegerField(min_value=-100, max_value=100, default=0)
    max_attempts = serializers.IntegerField(min_value=1, max_value=10, default=3)
    resolution_profile = serializers.ChoiceField(
        choices=[(value, value) for value in H3_RESOLUTION_PROFILES], default="project",
    )
    resolution_multiple = serializers.IntegerField(
        min_value=H3_RESOLUTION_MULTIPLE_MIN,
        max_value=H3_RESOLUTION_MULTIPLE_MAX,
        default=H3_RESOLUTION_MULTIPLE_DEFAULT,
        help_text="ComfyUI 官方 ResolutionSelector 倍数，范围 8–128，且必须为 4 的倍数。",
    )
    image_resolution_profile = serializers.ChoiceField(
        choices=[(value, value) for value in H3_RESOLUTION_PROFILES],
        required=False,
        default="0.98",
        help_text=(
            "图片任务独立使用的分辨率档位；默认 0.98 MP 以匹配 SDXL 的高分辨率工作区，"
            "不会被视频烟测档位 0.1/0.2/0.3 降质。"
        ),
    )
    image_resolution_multiple = serializers.IntegerField(
        min_value=H3_RESOLUTION_MULTIPLE_MIN,
        max_value=H3_RESOLUTION_MULTIPLE_MAX,
        required=False,
        allow_null=True,
        default=None,
        help_text="图片尺寸取整倍数，范围 8–128 且必须为 8 的倍数；不填写时默认 32。",
    )
    workflow_overrides = serializers.JSONField(required=False, default=dict)
    def validate_episode_ids(self, value):
        if len(value) != len(set(value)):
            raise serializers.ValidationError("episode_ids 不能重复")
        return value

    def validate_shot_ids(self, value):
        if len(value) != len(set(value)):
            raise serializers.ValidationError("shot_ids 不能重复")
        return value

    def validate_workflow_overrides(self, value):
        if not isinstance(value, dict):
            raise serializers.ValidationError("workflow_overrides must be a JSON object")
        return value

    def validate_workflow_selections(self, value):
        if not isinstance(value, list):
            raise serializers.ValidationError("workflow_selections 必须是数组")
        if len(value) > len(Shot.H3Mode.values):
            raise serializers.ValidationError("workflow_selections 数量超出 H3 模式上限")
        normalized = []
        seen = set()
        allowed_modes = set(Shot.H3Mode.values)
        for index, item in enumerate(value):
            if not isinstance(item, dict):
                raise serializers.ValidationError(f"第 {index + 1} 项必须是对象")
            unknown = set(item) - {"output_kind", "h3_mode", "template_id", "parameters"}
            if unknown:
                raise serializers.ValidationError(
                    f"第 {index + 1} 项包含不支持字段: {', '.join(sorted(unknown))}"
                )
            output_kind = item.get("output_kind")
            mode = item.get("h3_mode")
            if output_kind != WorkflowTemplate.OutputKind.VIDEO:
                raise serializers.ValidationError("当前仅支持按 H3 模式选择视频工作流")
            if mode not in allowed_modes:
                raise serializers.ValidationError(f"不支持的 H3 模式: {mode!r}")
            try:
                template_id = UUID(str(item.get("template_id")))
            except (TypeError, ValueError, AttributeError):
                raise serializers.ValidationError(f"第 {index + 1} 项 template_id 无效")
            parameters = item.get("parameters", {})
            if not isinstance(parameters, dict):
                raise serializers.ValidationError(f"第 {index + 1} 项 parameters 必须是对象")
            if any(not isinstance(key, str) for key in parameters):
                raise serializers.ValidationError("工作流参数名必须是字符串")
            match_key = (output_kind, mode)
            if match_key in seen:
                raise serializers.ValidationError(f"{output_kind}/{mode} 只能选择一个工作流")
            seen.add(match_key)
            normalized.append({
                "output_kind": output_kind,
                "h3_mode": mode,
                "template_id": template_id,
                "parameters": parameters,
            })
        return normalized

    def validate_resolution_multiple(self, value):
        try:
            return validate_h3_resolution_multiple(value)
        except ResolutionProfileError as exc:
            raise serializers.ValidationError(str(exc)) from exc

    def validate(self, attrs):
        profile = attrs.get("resolution_profile", "project")
        image_profile = attrs.get("image_resolution_profile", "0.98")
        image_multiple = attrs.get("image_resolution_multiple")
        if image_multiple is None:
            image_multiple = H3_RESOLUTION_MULTIPLE_DEFAULT
        try:
            attrs["image_resolution_multiple"] = validate_image_resolution_multiple(
                image_multiple,
            )
        except ResolutionProfileError as exc:
            raise serializers.ValidationError({
                "image_resolution_multiple": str(exc),
            }) from exc
        overrides = attrs.get("workflow_overrides", {})
        if attrs.get("template_id") and attrs.get("workflow_selections"):
            raise serializers.ValidationError({
                "workflow_selections": (
                    "旧版 template_id 与按模式 workflow_selections 不能同时使用"
                ),
            })
        controlled_override_keys = {"RESOLUTION_PROFILE", "RESOLUTION_MULTIPLE"} & set(overrides)
        if controlled_override_keys:
            raise serializers.ValidationError({
                "workflow_overrides": (
                    "RESOLUTION_PROFILE/RESOLUTION_MULTIPLE must be supplied through their "
                    "top-level controlled fields"
                ),
            })
        if (
            profile != "project" or image_profile != "project"
        ) and ({"WIDTH", "HEIGHT"} & set(overrides)):
            raise serializers.ValidationError({
                "workflow_overrides": (
                    "WIDTH/HEIGHT cannot be supplied together with a controlled video or image "
                    "resolution profile; select project for both profiles to use custom dimensions"
                ),
            })
        if attrs.get("whole_episode"):
            if len(attrs.get("episode_ids", [])) != 1:
                raise serializers.ValidationError({
                    "episode_ids": "whole_episode=true 时必须且只能选择一个剧集",
                })
            if attrs.get("shot_ids"):
                raise serializers.ValidationError({
                    "shot_ids": "whole_episode=true 时会选择该集全部分镜，不能再传 shot_ids",
                })
            if not attrs.get("enqueue", True):
                raise serializers.ValidationError({
                    "enqueue": "whole_episode=true 会创建可独立追踪的分镜任务，enqueue 必须为 true",
                })
            if not attrs.get("idempotency_key"):
                raise serializers.ValidationError({
                    "idempotency_key": "整集生成必须提供非空 idempotency_key",
                })
            # A whole-episode request is always independently retryable per
            # shot, even if an older client still sends multi_shot=true.
            attrs["multi_shot"] = False
        elif attrs.get("idempotency_key"):
            raise serializers.ValidationError({
                "idempotency_key": "idempotency_key 当前仅用于 whole_episode 整集生成",
            })
        return attrs


class ScriptBreakdownSerializer(serializers.Serializer):
    script = serializers.CharField(required=False, allow_blank=True, default="", max_length=1_000_000)
    episode_count_hint = serializers.IntegerField(min_value=1, max_value=1000, default=1)
    default_shot_duration_ms = serializers.IntegerField(min_value=5000, max_value=15000, default=5000)
    multi_shot = serializers.BooleanField(default=True)
    include_audio = serializers.BooleanField(default=True)
    replace_existing = serializers.BooleanField(default=False)


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


_CJK_RE = re.compile(r"[\u3400-\u4dbf\u4e00-\u9fff]")
_ENGLISH_DIRECTOR_FORBIDDEN_RE = re.compile(
    r"[\u3040-\u30ff\u31f0-\u31ff\uac00-\ud7af\u0400-\u052f"
    r"\u200b-\u200f\u202a-\u202e\u2060\ufeff]"
)


def _english_director_text(value: str) -> str:
    value = str(value or "")
    if (
        _CJK_RE.search(value)
        or _ENGLISH_DIRECTOR_FORBIDDEN_RE.search(value)
        or any(
            (character.isalpha() and "LATIN" not in unicodedata.name(character, ""))
            or (
                character not in "\n\r\t"
                and unicodedata.category(character) in {"Cc", "Cf"}
            )
            for character in value
        )
    ):
        raise serializers.ValidationError(
            "英文导演字段只能包含安全的拉丁英文文字，"
            "不能含中文、日文、韩文、西里尔文或不可见控制字符"
        )
    return value


def _chinese_detail_text(value: str) -> str:
    if not _CJK_RE.search(str(value or "")):
        raise serializers.ValidationError("高细节中文草稿必须包含中文正文")
    return value


class CreativeDialogueDraftSerializer(StrictSerializer):
    # Creative output is a reviewable proposal.  Keep only broad transport
    # ceilings here; speaker/Bible membership, language purity and the normal
    # per-field authoring budgets are reported as review issues by the service.
    speaker_id = serializers.CharField(max_length=80, allow_blank=True)
    text_zh = serializers.CharField(max_length=2_000, allow_blank=True, trim_whitespace=False)
    delivery_zh = serializers.CharField(
        max_length=1_000, required=False, allow_blank=True, default="",
        trim_whitespace=False,
    )
    delivery_en = serializers.CharField(
        max_length=1_500, required=False, allow_blank=True, default="",
        trim_whitespace=False,
    )
    voiceover = serializers.BooleanField(default=False)


class CreativeShotDraftSerializer(StrictSerializer):
    client_key = serializers.RegexField(r"^[a-z0-9][a-z0-9_-]{0,63}$")
    order = serializers.IntegerField(min_value=1, max_value=999)
    title_zh = serializers.CharField(max_length=500, allow_blank=True)
    title_en = serializers.CharField(max_length=500, allow_blank=True)
    narrative_zh = serializers.CharField(max_length=4_000, allow_blank=True, trim_whitespace=False)
    # Qwen's primary response grammar remains tighter (280/180).  The draft
    # review envelope leaves bounded room for a service-owned local repair
    # such as a verified body-side appositive, and for a reviewer to retain
    # that already-validated correction without truncation.
    narrative_en = serializers.CharField(
        max_length=6_000, allow_blank=True, trim_whitespace=False,
    )
    camera_zh = serializers.CharField(max_length=2_000, allow_blank=True, trim_whitespace=False)
    camera_en = serializers.CharField(
        max_length=3_000, allow_blank=True, trim_whitespace=False,
    )
    action_zh = serializers.CharField(max_length=2_000, allow_blank=True, trim_whitespace=False)
    action_en = serializers.CharField(
        max_length=3_000, allow_blank=True, trim_whitespace=False,
    )
    continuity_zh = serializers.CharField(
        max_length=2_000, required=False, allow_blank=True, default="", trim_whitespace=False,
    )
    continuity_en = serializers.CharField(
        max_length=3_000, required=False, allow_blank=True, default="",
        trim_whitespace=False,
    )
    soundscape_zh = serializers.CharField(
        max_length=2_000, required=False, allow_blank=True, default="", trim_whitespace=False,
    )
    soundscape_en = serializers.CharField(
        max_length=3_000, required=False, allow_blank=True, default="",
        trim_whitespace=False,
    )
    music_zh = serializers.CharField(
        max_length=2_000, required=False, allow_blank=True, default="", trim_whitespace=False,
    )
    music_en = serializers.CharField(
        max_length=3_000, required=False, allow_blank=True, default="",
        trim_whitespace=False,
    )
    duration_ms = serializers.IntegerField(min_value=1, max_value=600_000)
    media_type = serializers.ChoiceField(choices=(Shot.MediaType.VIDEO,), default=Shot.MediaType.VIDEO)
    h3_mode = serializers.ChoiceField(choices=Shot.H3Mode.choices)
    include_audio = serializers.BooleanField(default=True)
    scene_id = serializers.UUIDField(required=False, allow_null=True, default=None)
    character_ids = serializers.ListField(
        child=serializers.UUIDField(), required=False, allow_empty=True, default=list,
    )
    prop_ids = serializers.ListField(
        child=serializers.UUIDField(), required=False, allow_empty=True, default=list,
    )
    dialogue = CreativeDialogueDraftSerializer(many=True, required=False, default=list)

    def validate(self, attrs):
        # Duplicate bindings, audio conflicts and dense dialogue are all
        # editable content risks.  Preserve the proposal exactly and let the
        # review service describe them instead of rejecting the whole draft.
        return attrs


class CreativeEpisodeDraftSerializer(StrictSerializer):
    title_zh = serializers.CharField(max_length=8_000, allow_blank=True)
    title_en = serializers.CharField(
        max_length=2_000, required=False, allow_blank=True, default="",
    )
    # The formal Episode synopsis is source-of-truth context rather than a
    # local-Qwen output field. Keep the draft envelope aligned with the
    # creative runtime's 8,000-character input ceiling.
    synopsis_zh = serializers.CharField(max_length=8_000, allow_blank=True, trim_whitespace=False)
    synopsis_en = serializers.CharField(
        max_length=4_000, required=False, allow_blank=True, default="",
        trim_whitespace=False,
    )


class CreativeDraftDataSerializer(StrictSerializer):
    schema_version = serializers.IntegerField(min_value=1, max_value=1)
    episode = CreativeEpisodeDraftSerializer()
    shots = CreativeShotDraftSerializer(many=True, allow_empty=False)

    def validate_shots(self, value):
        if not value:
            raise serializers.ValidationError("草稿至少需要一个可编辑镜头")
        return value


MODEL_PROVIDER_CHOICES = ("local_qwen", "deepseek_api")


class CreativeDraftGenerateSerializer(StrictSerializer):
    episode_id = serializers.UUIDField()
    provider = serializers.ChoiceField(
        choices=MODEL_PROVIDER_CHOICES, default="local_qwen",
    )
    target_shot_count = serializers.IntegerField(min_value=1, max_value=4, default=4)
    creativity = serializers.ChoiceField(
        choices=EpisodeCreativeDraft.Creativity.choices,
        default=EpisodeCreativeDraft.Creativity.BALANCED,
    )
    variation_note = serializers.CharField(
        max_length=4000, required=False, allow_blank=True, default="",
        trim_whitespace=False,
    )


class CreativeDraftPatchSerializer(StrictSerializer):
    expected_revision = serializers.IntegerField(min_value=1)
    draft_data = CreativeDraftDataSerializer()


class CreativeDraftRegenerateSerializer(StrictSerializer):
    expected_revision = serializers.IntegerField(min_value=1)
    provider = serializers.ChoiceField(choices=MODEL_PROVIDER_CHOICES, required=False)
    target_shot_count = serializers.IntegerField(min_value=1, max_value=4, required=False)
    creativity = serializers.ChoiceField(
        choices=EpisodeCreativeDraft.Creativity.choices, required=False,
    )
    variation_note = serializers.CharField(
        max_length=4000, required=False, allow_blank=True, trim_whitespace=False,
    )
    shot_keys = serializers.ListField(
        child=serializers.RegexField(r"^[a-z0-9][a-z0-9_-]{0,63}$"),
        required=False, allow_empty=False,
    )
    preserve_locked_content = serializers.BooleanField(default=True)

    def validate_shot_keys(self, value):
        if len(value) != len(set(value)):
            raise serializers.ValidationError("shot_keys 不能重复")
        return value


class CreativeDraftConfirmSerializer(StrictSerializer):
    expected_revision = serializers.IntegerField(min_value=1)
    review_acknowledged = serializers.BooleanField(default=False)


class CreativeDraftApplySerializer(StrictSerializer):
    expected_revision = serializers.IntegerField(min_value=1)
    review_acknowledged = serializers.BooleanField(default=False)
    language_mode = serializers.ChoiceField(
        choices=("chinese_only", "bilingual_confirmed"), default="chinese_only",
    )
    english_review_acknowledged = serializers.BooleanField(default=False)
    placement = serializers.ChoiceField(choices=("append", "replace"), default="append")
    shot_keys = serializers.ListField(
        child=serializers.RegexField(r"^[a-z0-9][a-z0-9_-]{0,63}$"),
        required=False, allow_empty=False,
    )

    def validate(self, attrs):
        if (
            attrs["language_mode"] == "bilingual_confirmed"
            and not attrs["english_review_acknowledged"]
        ):
            raise serializers.ValidationError({
                "english_review_acknowledged": "应用并确认英文前必须显式确认已人工审核",
            })
        return attrs


class EpisodeCreativeDraftSerializer(serializers.ModelSerializer):
    context_current = serializers.SerializerMethodField()
    can_edit = serializers.SerializerMethodField()
    can_regenerate = serializers.SerializerMethodField()
    can_confirm = serializers.SerializerMethodField()
    can_apply = serializers.SerializerMethodField()

    class Meta:
        model = EpisodeCreativeDraft
        fields = (
            "id", "project", "episode", "status", "revision", "generation_no",
            "parent_draft", "context_hash", "context_current", "input_settings",
            "creativity", "variation_note", "draft_data", "validation", "warnings",
            "error_message", "provider", "model", "runtime_metadata",
            "revision_history", "confirmed_by", "confirmed_at", "applied_by",
            "applied_at", "applied_shot_ids", "can_edit", "can_regenerate",
            "can_confirm", "can_apply", "created_at", "updated_at",
        )
        read_only_fields = fields

    def get_context_current(self, obj) -> bool:
        from studio.services.creative_drafts import creative_context_hash

        return obj.context_hash == creative_context_hash(obj.project, obj.episode)

    def get_can_edit(self, obj) -> bool:
        return obj.status in {
            EpisodeCreativeDraft.Status.DRAFT,
            EpisodeCreativeDraft.Status.FAILED,
        }

    def get_can_regenerate(self, obj) -> bool:
        return obj.status in {
            EpisodeCreativeDraft.Status.DRAFT,
            EpisodeCreativeDraft.Status.CONFIRMED,
            EpisodeCreativeDraft.Status.FAILED,
        }

    def get_can_confirm(self, obj) -> bool:
        return bool(
            obj.status == EpisodeCreativeDraft.Status.DRAFT
            and (obj.validation or {}).get("status") in {
                "passed", "ready", "needs_review",
            }
            and self.get_context_current(obj)
        )

    def get_can_apply(self, obj) -> bool:
        review_issues = (obj.validation or {}).get("review_issues", [])
        requires_edit = bool(
            isinstance(review_issues, list)
            and any(
                isinstance(item, dict) and item.get("severity") == "requires_edit"
                for item in review_issues
            )
        )
        return bool(
            obj.status == EpisodeCreativeDraft.Status.CONFIRMED
            and (obj.validation or {}).get("status") in {
                "passed", "ready", "needs_review",
            }
            and not requires_edit
            and self.get_context_current(obj)
        )


class ShotDetailDraftDataSerializer(StrictSerializer):
    schema_version = serializers.IntegerField(min_value=1, max_value=1)
    detail_zh = serializers.CharField(
        # 500/750 are generation recommendations.  Reviewers may temporarily
        # keep a longer or imperfect bilingual candidate while correcting it.
        max_length=3_000, allow_blank=True, trim_whitespace=False,
    )
    detail_en = serializers.CharField(
        max_length=4_500, allow_blank=True, trim_whitespace=False,
    )


class ShotDetailDraftGenerateSerializer(StrictSerializer):
    shot_id = serializers.UUIDField()
    provider = serializers.ChoiceField(
        choices=MODEL_PROVIDER_CHOICES, default="local_qwen",
    )
    variation_note = serializers.CharField(
        max_length=2_000, required=False, allow_blank=True, default="", trim_whitespace=False,
    )


class ShotDetailDraftPatchSerializer(StrictSerializer):
    expected_revision = serializers.IntegerField(min_value=1)
    draft_data = ShotDetailDraftDataSerializer()


class ShotDetailDraftRegenerateSerializer(StrictSerializer):
    expected_revision = serializers.IntegerField(min_value=1)
    provider = serializers.ChoiceField(choices=MODEL_PROVIDER_CHOICES, required=False)
    variation_note = serializers.CharField(
        max_length=2_000, required=False, allow_blank=True, default="", trim_whitespace=False,
    )


class ShotDetailDraftConfirmSerializer(StrictSerializer):
    expected_revision = serializers.IntegerField(min_value=1)
    review_acknowledged = serializers.BooleanField(default=False)


class ShotDetailDraftApplySerializer(StrictSerializer):
    expected_revision = serializers.IntegerField(min_value=1)
    review_acknowledged = serializers.BooleanField(default=False)
    language_mode = serializers.ChoiceField(
        choices=("chinese_only", "bilingual_confirmed"), default="bilingual_confirmed",
    )
    english_review_acknowledged = serializers.BooleanField(default=False)

    def validate(self, attrs):
        if (
            attrs["language_mode"] == "bilingual_confirmed"
            and not attrs["english_review_acknowledged"]
        ):
            raise serializers.ValidationError({
                "english_review_acknowledged": "应用英文高细节前必须显式确认已人工审核译文",
            })
        return attrs


class ShotDetailDraftSerializer(serializers.ModelSerializer):
    context_current = serializers.SerializerMethodField()
    can_edit = serializers.SerializerMethodField()
    can_regenerate = serializers.SerializerMethodField()
    can_confirm = serializers.SerializerMethodField()
    can_apply = serializers.SerializerMethodField()

    class Meta:
        model = ShotDetailDraft
        fields = (
            "id", "project", "episode", "shot", "status", "revision", "generation_no",
            "parent_draft", "context_hash", "context_current", "context_snapshot", "input_settings",
            "variation_note", "draft_data", "validation", "warnings", "error_message",
            "provider", "model", "runtime_metadata", "revision_history", "confirmed_by",
            "confirmed_at", "applied_by", "applied_at", "can_edit", "can_regenerate",
            "can_confirm", "can_apply", "created_at", "updated_at",
        )
        read_only_fields = fields

    def get_context_current(self, obj) -> bool:
        from studio.services.shot_detail_drafts import shot_detail_context_hash

        try:
            return obj.context_hash == shot_detail_context_hash(obj.project, obj.episode, obj.shot)
        except Exception:
            return False

    def get_can_edit(self, obj) -> bool:
        return obj.status in {ShotDetailDraft.Status.DRAFT, ShotDetailDraft.Status.FAILED}

    def get_can_regenerate(self, obj) -> bool:
        return obj.status in {
            ShotDetailDraft.Status.DRAFT, ShotDetailDraft.Status.CONFIRMED,
            ShotDetailDraft.Status.FAILED,
        }

    def get_can_confirm(self, obj) -> bool:
        return bool(
            obj.status == ShotDetailDraft.Status.DRAFT
            and (obj.validation or {}).get("status") in {"passed", "ready", "needs_review"}
            and self.get_context_current(obj)
        )

    def get_can_apply(self, obj) -> bool:
        review_issues = (obj.validation or {}).get("review_issues", [])
        requires_edit = bool(
            isinstance(review_issues, list)
            and any(
                isinstance(item, dict) and item.get("severity") == "requires_edit"
                for item in review_issues
            )
        )
        return bool(
            obj.status == ShotDetailDraft.Status.CONFIRMED
            and (obj.validation or {}).get("status") in {"passed", "ready", "needs_review"}
            and not requires_edit
            and self.get_context_current(obj)
        )


class PromptFieldTranslationSerializer(serializers.ModelSerializer):
    context_current = serializers.SerializerMethodField()
    promoted_fields = serializers.SerializerMethodField()

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


class PromptTranslationEntryRequestSerializer(serializers.Serializer):
    field_key = serializers.CharField(max_length=160)
    source_text = serializers.CharField(max_length=30_000, trim_whitespace=False)
    scope_type = serializers.ChoiceField(
        choices=PromptFieldTranslation.ScopeType.choices,
        default=PromptFieldTranslation.ScopeType.PROJECT,
    )
    scope_id = serializers.UUIDField(required=False, allow_null=True)

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
    provider = serializers.ChoiceField(
        choices=MODEL_PROVIDER_CHOICES, default="local_qwen",
    )
    source_lang = serializers.ChoiceField(choices=("Chinese",), default="Chinese")
    target_lang = serializers.ChoiceField(choices=("English",), default="English")

    def validate_entries(self, entries):
        if len(entries) > 100:
            raise serializers.ValidationError("单次最多翻译 100 个字段")
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
        if len(entries) > 100:
            raise serializers.ValidationError("单次最多处理 100 个 Bible 实体")
        if attrs["ensure_visual_identity_translation"] and len(entries) > 50:
            raise serializers.ValidationError(
                "同时生成视觉锚点英文候选时，单次最多处理 50 个 Bible 实体"
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
    origin = serializers.ChoiceField(choices=("local_qwen", "existing", "source_english", "none"))
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
        "local_qwen", "existing", "source_english", "none",
    ))
    context_current = serializers.BooleanField()
    retryable = serializers.BooleanField()
    error_code = serializers.CharField(allow_blank=True)
    message = serializers.CharField(allow_blank=True)


class CanonicalGlossaryEnsureResponseSerializer(serializers.Serializer):
    project_id = serializers.UUIDField()
    translation_glossary = serializers.DictField(child=serializers.CharField())
    summary = CanonicalGlossarySummarySerializer()
    entries = CanonicalGlossaryResultEntrySerializer(many=True)
    visual_summary = CanonicalGlossaryVisualSummarySerializer()
    visual_translations = CanonicalGlossaryVisualTranslationSerializer(many=True)
    runtime = serializers.DictField()


class PromptTranslationRetranslateSerializer(serializers.Serializer):
    provider = serializers.ChoiceField(
        choices=MODEL_PROVIDER_CHOICES, default="local_qwen",
    )
    source_text = serializers.CharField(
        max_length=30_000, trim_whitespace=False, required=False,
    )
    source_lang = serializers.ChoiceField(choices=("Chinese",), default="Chinese")
    target_lang = serializers.ChoiceField(choices=("English",), default="English")


class PromptTranslationConfirmSerializer(serializers.Serializer):
    expected_revision = serializers.IntegerField(min_value=1)
    translation = serializers.CharField(
        max_length=30_000, trim_whitespace=False, required=False,
    )


class PromptTranslationPromotionEntrySerializer(serializers.Serializer):
    entry_id = serializers.UUIDField()
    expected_revision = serializers.IntegerField(min_value=1)


class PromptTranslationPromoteShotSerializer(serializers.Serializer):
    shot_id = serializers.UUIDField()
    entries = PromptTranslationPromotionEntrySerializer(many=True, allow_empty=True)
    repair_legacy_negative_prompt = serializers.BooleanField(default=False)

    def validate(self, attrs):
        entries = attrs["entries"]
        if not entries and not attrs["repair_legacy_negative_prompt"]:
            raise serializers.ValidationError({
                "entries": "至少选择一个已确认译文，或显式请求修复旧负面提示污染",
            })
        if len(entries) > 20:
            raise serializers.ValidationError({"entries": "单次最多同步 20 个镜头导演字段"})
        entry_ids = [item["entry_id"] for item in entries]
        if len(entry_ids) != len(set(entry_ids)):
            raise serializers.ValidationError({"entries": "同一翻译条目不能重复提交"})
        return attrs


class PromptTranslationPromoteShotResponseSerializer(serializers.Serializer):
    shot_id = serializers.UUIDField()
    promoted_fields = serializers.ListField(child=serializers.DictField())
    entries = PromptFieldTranslationSerializer(many=True)
    shot_patch = serializers.DictField()


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

class AssembleEpisodeSerializer(serializers.Serializer):
    episode_id = serializers.UUIDField()
    dry_run = serializers.BooleanField(default=True)
    output_name = serializers.CharField(max_length=160, required=False, allow_blank=True, default="")


class AssembleSeriesSerializer(serializers.Serializer):
    project_id = serializers.UUIDField()
    dry_run = serializers.BooleanField(default=True)
    output_name = serializers.CharField(max_length=160, required=False, allow_blank=True, default="")


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


class ReleaseBundleCreateSerializer(serializers.Serializer):
    project_id = serializers.UUIDField()
    title = serializers.CharField(max_length=200, required=False, allow_blank=True, default="")
    notes = serializers.CharField(required=False, allow_blank=True, default="", max_length=10_000)
    selection = serializers.JSONField(required=False)


class ReleaseBundleProposeSerializer(serializers.Serializer):
    project_id = serializers.UUIDField()
