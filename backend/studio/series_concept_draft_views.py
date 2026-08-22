from __future__ import annotations

import logging
import re
from typing import Any
from uuid import uuid4

from drf_spectacular.utils import extend_schema
from rest_framework import status
from rest_framework.exceptions import APIException, ValidationError
from rest_framework.permissions import IsAuthenticated
from rest_framework.response import Response
from rest_framework.views import APIView

from studio.series_concept_draft_serializers import (
    SeriesConceptDraftGenerationErrorEnvelopeSerializer,
    SeriesConceptDraftRequestSerializer,
    SeriesConceptDraftResponseSerializer,
)
from studio.services.creative_drafts import (
    CreativeDraftError,
    CreativeResourceBusy,
    CreativeRuntimeUnavailable,
    CreativeValidationError,
)
from studio.services.series_concept_drafts import generate_series_concept_draft


logger = logging.getLogger(__name__)
_SAFE_ERROR_CODE_RE = re.compile(r"^[a-z][a-z0-9_]{0,63}$")
_SAFE_SHA256_RE = re.compile(r"^[0-9a-f]{64}$")
_SAFE_VALIDATION_PATH_RE = re.compile(
    r"^(?:result\.)?(?:title|logline|bible\.(?:characters|scenes|props)\[\d+\]"
    r"(?:\.(?:name|description|visual_identity_en))?|episodes\[\d+\]"
    r"(?:\.(?:title|shots(?:\[\d+\])?"
    r"(?:\.(?:setting|action|duration_ms|dialogue"
    r"(?:\.(?:speaker|delivery|text))?))?))?)$"
)
_SAFE_SHAPE_TOKEN_RE = re.compile(
    r'\s+|[{}\[\]:,?]|-?\d+(?:\.\d+)?(?:[eE][+-]?\d+)?|'
    r'true|false|null|<redacted_middle>|'
    r'"(?:title|logline|bible|characters|scenes|props|name|description|visual_identity_en|'
    r'episodes|shots|setting|action|dialogue|speaker|delivery|text|duration_ms|schema_version)"|'
    r'"<(?:redacted_text|unterminated_text|unknown_key)>"'
)
_DEEPSEEK_UNAVAILABLE_CODES = {
    "deepseek_api_authentication_failed",
    "deepseek_api_disabled",
    "deepseek_api_misconfigured",
    "deepseek_api_unavailable",
    "deepseek_protocol_unavailable",
    "deepseek_provider_unavailable",
}
_RUNTIME_ERROR_MAPPINGS = {
    "creative_parent_timeout": (
        "series_concept_parent_timeout", "parent_process",
    ),
    "creative_invalid_runtime_json": (
        "series_concept_runtime_protocol_failed", "transport_decode",
    ),
    "creative_runtime_exit": (
        "series_concept_runtime_exit", "child_process_exit",
    ),
    "creative_runtime_failed": (
        "series_concept_runtime_failed", "runtime",
    ),
    "deepseek_api_network_failed": (
        "series_concept_provider_network_failed", "provider_transport",
    ),
    "deepseek_api_rate_limited": (
        "series_concept_provider_rate_limited", "provider_transport",
    ),
    "deepseek_api_request_rejected": (
        "series_concept_provider_request_rejected", "provider_transport",
    ),
    "deepseek_api_invalid_response": (
        "series_concept_provider_invalid_response", "provider_decode",
    ),
    "series_concept_draft_invalid_request": (
        "series_concept_draft_invalid_request", "request_protocol",
    ),
    "series_concept_draft_empty_output": (
        "series_concept_draft_empty_output", "provider_output",
    ),
    "series_concept_draft_output_invalid_json": (
        "series_concept_draft_output_invalid_json", "provider_decode",
    ),
    "series_concept_draft_output_truncated": (
        "series_concept_provider_output_truncated", "provider_output",
    ),
    "series_concept_draft_validation_failed": (
        "series_concept_draft_validation_failed", "provider_validation",
    ),
}
class SeriesConceptDraftEndpointError(APIException):
    def __init__(self, *, status_code: int, code: str, detail: Any) -> None:
        self.status_code = status_code
        self.default_code = code
        super().__init__(detail=detail, code=code)
        # Preserve the allow-listed numeric and boolean diagnostic types.
        # APIException otherwise coerces every nested leaf to ErrorDetail text.
        self.detail = detail


def _safe_error_code(value: object, *, fallback: str) -> str:
    candidate = str(value or "").strip().casefold()
    return candidate if _SAFE_ERROR_CODE_RE.fullmatch(candidate) else fallback


def _safe_runtime_label(value: object) -> str:
    candidate = str(value or "").strip().casefold()
    return candidate if _SAFE_ERROR_CODE_RE.fullmatch(candidate) else ""


def _safe_usage(value: object) -> dict[str, int]:
    if not isinstance(value, dict):
        return {}
    return {
        key: item
        for key in ("prompt_tokens", "completion_tokens", "total_tokens")
        if type((item := value.get(key))) is int and item >= 0
    }


def _safe_redacted_json_shape(value: object) -> str:
    candidate = str(value or "")[:900]
    position = 0
    while position < len(candidate):
        matched = _SAFE_SHAPE_TOKEN_RE.match(candidate, position)
        if matched is None:
            return ""
        position = matched.end()
    return candidate


def _safe_positive_number(value: object, *, maximum: float) -> int | float | None:
    if isinstance(value, bool) or not isinstance(value, (int, float)):
        return None
    if value <= 0 or value > maximum:
        return None
    return value


def _build_generation_diagnostic(
    *,
    provider: str,
    error_code: str,
    failure_phase: str,
    runtime_metadata: dict[str, Any] | None = None,
) -> dict[str, Any]:
    metadata = runtime_metadata if isinstance(runtime_metadata, dict) else {}
    safe_phase = _safe_runtime_label(
        metadata.get("failure_phase") or failure_phase
    ) or "runtime"
    diagnostic: dict[str, Any] = {
        "diagnostic_id": uuid4().hex,
        "provider": (
            provider if provider in {"local_qwen", "deepseek_api"} else "unknown"
        ),
        "error_code": _safe_error_code(
            error_code, fallback="series_concept_generation_failed",
        ),
        "failure_phase": safe_phase,
    }
    finish_reason = _safe_runtime_label(metadata.get("finish_reason"))
    if finish_reason:
        diagnostic["finish_reason"] = finish_reason
    usage = _safe_usage(metadata.get("usage"))
    if usage:
        diagnostic["usage"] = usage

    details: dict[str, Any] = {}
    for key in ("attempts", "elapsed_ms"):
        value = metadata.get(key)
        if type(value) is int and value >= 0:
            details[key] = value
    http_status = metadata.get("http_status")
    if type(http_status) is int and 100 <= http_status <= 599:
        details["http_status"] = http_status
    if type(metadata.get("incomplete")) is bool:
        details["incomplete"] = metadata["incomplete"]
    for key in ("requested_max_tokens", "configured_max_tokens"):
        value = metadata.get(key)
        if type(value) is int and value >= 1:
            details[key] = value
    timeout_seconds = _safe_positive_number(
        metadata.get("configured_timeout_seconds"), maximum=3600,
    )
    if timeout_seconds is not None:
        details["configured_timeout_seconds"] = timeout_seconds
    for key in ("validation_error_code", "repair_reason_code"):
        value = _safe_runtime_label(metadata.get(key))
        if value:
            details[key] = value
    validation_path = str(metadata.get("validation_path") or "")[:160]
    if _SAFE_VALIDATION_PATH_RE.fullmatch(validation_path):
        details["validation_path"] = validation_path
    for key in ("repair_attempted", "repair_succeeded"):
        if type(metadata.get(key)) is bool:
            details[key] = metadata[key]

    output: dict[str, Any] = {}
    for source_key, public_key in (
        ("output_chars", "character_count"),
        ("output_utf8_bytes", "utf8_byte_count"),
    ):
        value = metadata.get(source_key)
        if type(value) is int and value >= 0:
            output[public_key] = value
    output_sha256 = str(metadata.get("output_sha256") or "").lower()
    if _SAFE_SHA256_RE.fullmatch(output_sha256):
        output["sha256"] = output_sha256
    redacted_shape = _safe_redacted_json_shape(
        metadata.get("redacted_output_shape")
    )
    if redacted_shape:
        output["redacted_json_shape"] = redacted_shape
    for source_key, public_key, minimum in (
        ("json_decode_line", "json_decode_line", 1),
        ("json_decode_column", "json_decode_column", 1),
        ("json_decode_position", "json_decode_position", 0),
    ):
        value = metadata.get(source_key)
        if type(value) is int and value >= minimum:
            output[public_key] = value
    if output:
        details["output"] = output

    diagnostic["details_available"] = bool(details)
    diagnostic["details"] = details
    return diagnostic


def _log_generation_failure(
    *,
    provider: str,
    error_code: str,
    failure_phase: str,
    runtime_metadata: dict[str, Any] | None = None,
) -> dict[str, Any]:
    """Log only bounded operational facts; never provider/user content."""

    diagnostic = _build_generation_diagnostic(
        provider=provider,
        error_code=error_code,
        failure_phase=failure_phase,
        runtime_metadata=runtime_metadata,
    )
    logger.warning(
        "series_concept_generation_failed diagnostic_id=%s provider=%s error_code=%s "
        "failure_phase=%s finish_reason=%s usage=%s",
        diagnostic["diagnostic_id"],
        diagnostic["provider"],
        diagnostic["error_code"],
        diagnostic["failure_phase"],
        diagnostic.get("finish_reason", ""),
        diagnostic.get("usage", {}),
        extra={"series_concept_diagnostic": diagnostic},
    )
    return diagnostic


def _raise_generation_error(
    exc: CreativeDraftError, *, provider: str,
) -> None:
    raw_error_code = _safe_error_code(
        getattr(exc, "code", ""), fallback="series_concept_generation_failed",
    )
    if isinstance(exc, CreativeValidationError):
        error_code = raw_error_code
        failure_phase = "request_validation"
    elif isinstance(exc, CreativeResourceBusy):
        error_code = "series_concept_resource_busy"
        failure_phase = "resource_busy"
    elif isinstance(exc, CreativeRuntimeUnavailable):
        error_code = "series_concept_runtime_unavailable"
        failure_phase = "runtime_availability"
    elif raw_error_code in _DEEPSEEK_UNAVAILABLE_CODES:
        error_code = raw_error_code
        failure_phase = "runtime_availability"
    elif raw_error_code in _RUNTIME_ERROR_MAPPINGS:
        error_code, failure_phase = _RUNTIME_ERROR_MAPPINGS[raw_error_code]
    elif raw_error_code.startswith("series_concept_"):
        error_code = raw_error_code
        failure_phase = "provider_runtime"
    else:
        error_code = "series_concept_generation_failed"
        failure_phase = "provider_runtime"
    diagnostic = _log_generation_failure(
        provider=provider,
        error_code=error_code,
        failure_phase=failure_phase,
        runtime_metadata=dict(getattr(exc, "runtime_metadata", {}) or {}),
    )
    if isinstance(exc, CreativeValidationError):
        public_context = dict(exc.public_context)
        raise SeriesConceptDraftEndpointError(
            status_code=status.HTTP_400_BAD_REQUEST,
            code=_safe_error_code(
                exc.code, fallback="series_concept_invalid_request",
            ),
            detail={
                "detail": str(exc),
                **public_context,
            },
        ) from exc
    if isinstance(exc, CreativeResourceBusy):
        raise SeriesConceptDraftEndpointError(
            status_code=status.HTTP_409_CONFLICT,
            code="series_concept_resource_busy",
            detail={
                "detail": "本地 Qwen 或视频生成正在占用共享计算资源，请稍后重试。",
                "diagnostic": diagnostic,
            },
        ) from exc

    if isinstance(exc, CreativeRuntimeUnavailable) or raw_error_code in (
        _DEEPSEEK_UNAVAILABLE_CODES
    ):
        raise SeriesConceptDraftEndpointError(
            status_code=status.HTTP_503_SERVICE_UNAVAILABLE,
            code=(
                raw_error_code
                if raw_error_code in _DEEPSEEK_UNAVAILABLE_CODES
                else "series_concept_runtime_unavailable"
            ),
            detail={
                "detail": "所选创作通道当前未正确配置或不可用。",
                "diagnostic": diagnostic,
            },
        ) from exc

    public_detail = (
        "DeepSeek 已达到当前连续剧模型输出能力并停止生成，返回内容不完整。"
        "本次完整候选仍按单次请求提交，未截断、未分批；请重试或由管理员检查模型能力配置。"
        if error_code == "series_concept_provider_output_truncated"
        else "所选创作通道未返回可用候选，请稍后重试。"
    )
    raise SeriesConceptDraftEndpointError(
        status_code=status.HTTP_502_BAD_GATEWAY,
        code=error_code,
        detail={"detail": public_detail, "diagnostic": diagnostic},
    ) from exc


class SeriesConceptDraftGenerateView(APIView):
    permission_classes = [IsAuthenticated]

    @extend_schema(
        request=SeriesConceptDraftRequestSerializer,
        responses={
            200: SeriesConceptDraftResponseSerializer,
            400: dict,
            409: dict,
            502: SeriesConceptDraftGenerationErrorEnvelopeSerializer,
            503: SeriesConceptDraftGenerationErrorEnvelopeSerializer,
        },
        description=(
            "Generate a detached project title, logline and parser-compatible master-script "
            "candidate. The response is review-only and never creates a SeriesProject."
        ),
    )
    def post(self, request):
        input_serializer = SeriesConceptDraftRequestSerializer(data=request.data)
        input_serializer.is_valid(raise_exception=True)
        try:
            result = generate_series_concept_draft(
                **input_serializer.validated_data,
            )
        except CreativeDraftError as exc:
            _raise_generation_error(
                exc,
                provider=input_serializer.validated_data["provider"],
            )

        response_serializer = SeriesConceptDraftResponseSerializer(data=result)
        try:
            response_serializer.is_valid(raise_exception=True)
        except ValidationError as exc:
            diagnostic = _log_generation_failure(
                provider=input_serializer.validated_data["provider"],
                error_code="series_concept_invalid_response",
                failure_phase="response_validation",
            )
            raise SeriesConceptDraftEndpointError(
                status_code=status.HTTP_502_BAD_GATEWAY,
                code="series_concept_invalid_response",
                detail={
                    "detail": "创作通道返回了无法安全展示的候选，请稍后重试。",
                    "diagnostic": diagnostic,
                },
            ) from exc
        return Response(response_serializer.data, status=status.HTTP_200_OK)
