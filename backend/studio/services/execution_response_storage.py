"""Private retained response objects, read only through an authorized detail."""
import hashlib
import uuid

from django.core.exceptions import ValidationError
from django.core.files.base import ContentFile
from django.core.files.storage import default_storage


class ExecutionResponseStorageError(ValidationError):
    """A received response could not be retained; not a model content error."""


def store_response(text):
    data = text.encode("utf-8")
    from studio.services.security_boundaries import boundary_int
    if len(data) > boundary_int("security.snapshot.text_max_bytes"):
        raise ExecutionResponseStorageError("响应超出正式文本存储边界，不能截断或伪装已保留")
    size, digest = len(data), hashlib.sha256(data).hexdigest()
    key = None
    try:
        key = default_storage.save(f"execution-responses/{uuid.uuid4()}.txt", ContentFile(data))
        read_response(key, size, digest)
    except (OSError, ValidationError) as exc:
        if key is not None:
            default_storage.delete(key)
        raise ExecutionResponseStorageError("模型已经返回，但原始正文存储失败；不会伪装已保留或自动重跑。") from exc
    return key, size, digest


def read_response(key, size, sha256):
    if not key or not key.startswith("execution-responses/") or ".." in key:
        raise ValidationError("响应存储标识无效")
    try:
        with default_storage.open(key, "rb") as stream:
            data = stream.read(size + 1)
    except OSError as exc:
        raise ExecutionResponseStorageError("保留的响应文件暂时无法读取，请检查正式文件存储；不会用空内容代替") from exc
    if len(data) != size or hashlib.sha256(data).hexdigest() != sha256:
        raise ExecutionResponseStorageError("保留的响应文件与摘要不一致")
    try:
        return data.decode("utf-8")
    except UnicodeError as exc:
        raise ExecutionResponseStorageError("保留的响应不是有效UTF-8正文，不能替换或伪装成功") from exc
