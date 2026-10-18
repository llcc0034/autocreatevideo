"""Authoritative restricted REST scalar vocabulary and schema projection.

This is not general JSON Schema or a new provider protocol. Authoring and
execution use the same scalar validator; examples never become defaults.
"""
from copy import deepcopy

from django.core.exceptions import ValidationError

ASYNC_MEDIA_OUTPUT_KIND = {
    "image.text_to_image": "image", "video.text_to_video": "video", "audio.text_to_audio": "audio",
}
ASYNC_MEDIA_FIELDS = (
    ("model", "模型标识", ("string",), "所选模型版本的 model_identifier；Smoke 样例不是生产模型选择器"),
    ("prompt", "正向提示词", ("string",), "策略 positive_prompt 通道组装的实际正文"),
    ("negative_prompt", "负向提示词", ("string",), "策略 negative_prompt 通道组装的实际正文"),
    ("width", "宽度（像素）", ("integer",), "任务准备参数，与模型尺寸合同取交集"),
    ("height", "高度（像素）", ("integer",), "任务准备参数，与模型尺寸合同取交集"),
    ("duration_seconds", "时长（秒）", ("integer", "number"), "任务准备参数，与模型时长合同取交集"),
    ("fps", "帧率", ("integer", "number"), "任务准备参数，与模型帧率合同取交集"),
    ("seed", "随机种子", ("integer",), "任务准备参数，种子策略在执行实例声明"),
    ("audio", "生成音频", ("boolean",), "布尔开关，不是音频文件或素材引用"),
)
STANDARD_MEDIA_FIELDS = frozenset(row[0] for row in ASYNC_MEDIA_FIELDS)
FIELD_TYPES = {row[0]: row[2] for row in ASYNC_MEDIA_FIELDS}
SCALAR_SCHEMA_MAPPING = {
    "type": "type", "minimum": "minimum", "maximum": "maximum",
    "minLength": "min_length", "maxLength": "max_length", "enum": "enum",
}


def api_parameter_rule(key, definition, *, required):
    """Closed scalar schema -> shared execution rule, without adding defaults."""
    from studio.services.execution_parameters import compile_execution_parameter_contract

    if not isinstance(definition, dict) or set(definition) - SCALAR_SCHEMA_MAPPING.keys():
        raise ValidationError({f"parameters.{key}": "API 参数包含未注册的 Schema 字段"})
    rule = {SCALAR_SCHEMA_MAPPING[name]: deepcopy(value) for name, value in definition.items()}
    rule["required"] = required
    compile_execution_parameter_contract({key: [{"source": "api.request_schema", "rule": rule}]})
    return rule


def async_media_editor_options():
    from studio.services.media_request_limits import media_prompt_absolute_max_chars

    return {
        "schema_version": 1,
        "maximum_string_length": media_prompt_absolute_max_chars(),
        "operations": [{"key": key, "output_kind": kind} for key, kind in ASYNC_MEDIA_OUTPUT_KIND.items()],
        "fields": [{"key": key, "label": label, "types": list(types), "source_description": source} for key, label, types, source in ASYNC_MEDIA_FIELDS],
        "description": "仅支持封闭对象中的标准标量字段；Smoke 样例仅为测试输入，不是生产默认值。标准字段经服务连接的 request_field_map 映射到远程单层字段；缺失映射会阻断执行实例。",
    }
