from __future__ import annotations

import base64
import hashlib
import os
import re
from dataclasses import dataclass

from cryptography.hazmat.primitives.ciphers.aead import AESGCM
from django.core.exceptions import ValidationError
from django.db import transaction
from django.db.models import Max

from studio.models import SecretAuditEvent, SecretBinding, SecretVersion
from studio.services.security_boundaries import boundary_int


KEYRING_ENV = "AUTOPROMPT_CONFIG_MASTER_KEYS"
ACTIVE_KEY_ENV = "AUTOPROMPT_CONFIG_ACTIVE_KEY_ID"


class SecretStoreError(RuntimeError):
    def __init__(self, message: str, *, code: str):
        super().__init__(message)
        self.code = code


@dataclass(frozen=True)
class Keyring:
    active_key_id: str
    keys: dict[str, bytes]


def _decode_key(value: str) -> bytes:
    try:
        padding = "=" * (-len(value) % 4)
        decoded = base64.b64decode(
            f"{value}{padding}".encode("ascii"), altchars=b"-_", validate=True,
        )
    except (ValueError, UnicodeEncodeError) as exc:
        raise SecretStoreError("数据库密钥环格式无效", code="secret_keyring_invalid") from exc
    if len(decoded) != 32:
        raise SecretStoreError("数据库加密密钥必须为 32 bytes", code="secret_keyring_invalid")
    return decoded


def load_keyring() -> Keyring:
    raw = str(os.environ.get(KEYRING_ENV) or "").strip()
    active = str(os.environ.get(ACTIVE_KEY_ENV) or "").strip()
    if not raw or not active:
        raise SecretStoreError(
            "数据库敏感配置密钥环未配置",
            code="secret_keyring_unconfigured",
        )
    keys: dict[str, bytes] = {}
    for item in raw.split(","):
        item = item.strip()
        if not item:
            continue
        key_id, separator, encoded = item.partition(":")
        key_id = key_id.strip()
        encoded = encoded.strip()
        if (
            not separator
            or re.fullmatch(r"[A-Za-z0-9][A-Za-z0-9_.-]{0,63}", key_id) is None
            or key_id in keys
        ):
            raise SecretStoreError("数据库密钥环格式无效", code="secret_keyring_invalid")
        keys[key_id] = _decode_key(encoded)
    if active not in keys:
        raise SecretStoreError("当前数据库加密 key id 不在密钥环中", code="secret_active_key_missing")
    return Keyring(active_key_id=active, keys=keys)


def _aad(binding: SecretBinding, version: int, purpose: str, *, aad_version: int = 1) -> bytes:
    if aad_version != 1:
        raise SecretStoreError("不支持的 secret AAD 版本", code="secret_aad_version_unsupported")
    return f"autoprompt-secret:v1|{binding.id}|{version}|{purpose}".encode("utf-8")


def _normalize_secret(value: str) -> bytes:
    if not isinstance(value, str) or not value:
        raise ValidationError({"secret_value": "秘密值不能为空"})
    material = value.encode("utf-8")
    max_plaintext_bytes = boundary_int(
        "security.managed_value.max_plaintext_bytes",
    )
    if len(material) > max_plaintext_bytes:
        raise ValidationError({
            "secret_value": f"秘密值不能超过 {max_plaintext_bytes} bytes",
        })
    return material


def _safe_reason(reason: str, *, secret_value: str = "") -> str:
    reason = str(reason or "").strip()
    if len(reason) > 500:
        raise ValidationError({"reason": "说明不能超过 500 个字符"})
    if secret_value and secret_value in reason:
        raise ValidationError({"reason": "说明不能包含秘密值"})
    return reason


@transaction.atomic
def create_secret_binding(
    *, key: str, label: str, purpose: str, secret_value: str, actor, reason: str = "",
) -> SecretBinding:
    keyring = load_keyring()
    binding = SecretBinding(
        key=key, label=label, purpose=purpose, created_by=actor,
    )
    binding.full_clean()
    binding.save()
    _create_version(
        binding=binding, secret_value=secret_value, actor=actor, keyring=keyring,
        action=SecretAuditEvent.Action.CREATE,
        reason=_safe_reason(reason, secret_value=secret_value),
    )
    return binding


@transaction.atomic
def rotate_secret_binding(
    binding: SecretBinding, *, secret_value: str, actor, reason: str = "",
) -> SecretBinding:
    keyring = load_keyring()
    binding = SecretBinding.objects.select_for_update().get(pk=binding.pk)
    if not binding.is_active:
        raise ValidationError("已停用凭据不能轮换；请先启用")
    _create_version(
        binding=binding, secret_value=secret_value, actor=actor, keyring=keyring,
        action=SecretAuditEvent.Action.ROTATE,
        reason=_safe_reason(reason, secret_value=secret_value),
    )
    return binding


def _create_version(
    *, binding: SecretBinding, secret_value: str, actor, keyring: Keyring,
    action: str, reason: str,
) -> SecretVersion:
    material = _normalize_secret(secret_value)
    next_version = (binding.versions.aggregate(value=Max("version"))["value"] or 0) + 1
    nonce = os.urandom(12)
    ciphertext = AESGCM(keyring.keys[keyring.active_key_id]).encrypt(
        nonce, material, _aad(binding, next_version, binding.purpose),
    )
    version = SecretVersion.objects.create(
        binding=binding,
        version=next_version,
        key_id=keyring.active_key_id,
        nonce_b64=base64.urlsafe_b64encode(nonce).decode("ascii"),
        ciphertext_b64=base64.urlsafe_b64encode(ciphertext).decode("ascii"),
        ciphertext_sha256=hashlib.sha256(ciphertext).hexdigest(),
        created_by=actor,
    )
    SecretAuditEvent.objects.create(
        binding=binding, version=version.version, actor=actor,
        action=action, reason=str(reason or "")[:500],
    )
    binding.active_version = version
    binding.save(update_fields=("active_version", "updated_at"))
    return version


def resolve_secret(binding: SecretBinding | None) -> str:
    if binding is None:
        raise SecretStoreError("模型部署缺少受管凭据", code="credential_missing")
    binding = SecretBinding.objects.select_related("active_version").get(pk=binding.pk)
    if not binding.is_active:
        raise SecretStoreError("受管凭据已停用", code="credential_inactive")
    if binding.active_version_id is None:
        raise SecretStoreError("受管凭据尚未写入秘密值", code="credential_material_missing")
    if binding.active_version.binding_id != binding.id:
        raise SecretStoreError(
            "受管凭据当前版本不属于该绑定",
            code="credential_version_binding_mismatch",
        )
    return resolve_secret_version(binding.active_version, require_active_binding=True)


def resolve_secret_version(
    version: SecretVersion | None, *, require_active_binding: bool = False,
) -> str:
    """Decrypt one immutable revision without silently following a rotated binding."""

    if version is None:
        raise SecretStoreError("受管凭据版本不存在", code="credential_material_missing")
    version = SecretVersion.objects.select_related("binding").get(pk=version.pk)
    binding = version.binding
    if require_active_binding and not binding.is_active:
        raise SecretStoreError("受管凭据已停用", code="credential_inactive")
    if version.algorithm != "AES-256-GCM":
        raise SecretStoreError("受管凭据算法不受支持", code="credential_algorithm_unsupported")
    keyring = load_keyring()
    key = keyring.keys.get(version.key_id)
    if key is None:
        raise SecretStoreError("解密所需 key id 不在密钥环中", code="credential_key_missing")
    try:
        nonce = base64.b64decode(
            version.nonce_b64.encode("ascii"), altchars=b"-_", validate=True,
        )
        ciphertext = base64.b64decode(
            version.ciphertext_b64.encode("ascii"), altchars=b"-_", validate=True,
        )
        if len(nonce) != 12 or len(ciphertext) < 16:
            raise SecretStoreError("受管凭据密文结构无效", code="credential_ciphertext_invalid")
        if hashlib.sha256(ciphertext).hexdigest() != version.ciphertext_sha256:
            raise SecretStoreError("受管凭据密文校验和不一致", code="credential_ciphertext_mismatch")
        plaintext = AESGCM(key).decrypt(
            nonce, ciphertext,
            _aad(binding, version.version, binding.purpose, aad_version=version.aad_version),
        )
        return plaintext.decode("utf-8")
    except SecretStoreError:
        raise
    except Exception as exc:
        raise SecretStoreError("受管凭据解密校验失败", code="credential_decryption_failed") from exc


@transaction.atomic
def set_secret_binding_active(
    binding: SecretBinding, *, is_active: bool, actor, reason: str = "",
) -> SecretBinding:
    binding = SecretBinding.objects.select_for_update().get(pk=binding.pk)
    binding.is_active = is_active
    binding.save(update_fields=("is_active", "updated_at"))
    latest = binding.versions.order_by("-version").first()
    SecretAuditEvent.objects.create(
        binding=binding,
        version=latest.version if latest else None,
        actor=actor,
        action=(SecretAuditEvent.Action.ACTIVATE if is_active else SecretAuditEvent.Action.DEACTIVATE),
        reason=_safe_reason(reason),
    )
    return binding
