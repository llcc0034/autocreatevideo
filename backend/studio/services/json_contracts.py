from __future__ import annotations

import hashlib
import json
import re
from dataclasses import dataclass
from typing import Any

from django.core.exceptions import ValidationError


DEFAULT_JSON_LIMIT_BYTES = 64 * 1024
SENSITIVE_KEY_PARTS = (
    "api_key",
    "apikey",
    "authorization",
    "bearer",
    "password",
    "private_key",
    "secret",
    "token",
)
FORBIDDEN_SECRET_SINGLE_TOKENS = frozenset({
    "accesskey",
    "accesstoken",
    "apikey",
    "apisecret",
    "authtoken",
    "authorization",
    "bearer",
    "bearertoken",
    "clientsecret",
    "credential",
    "credentials",
    "idtoken",
    "password",
    "privatekey",
    "refreshtoken",
    "secret",
    "secretkey",
    "secretvalue",
    "token",
})
FORBIDDEN_SECRET_TOKEN_PAIRS = frozenset({
    ("access", "key"),
    ("access", "token"),
    ("api", "key"),
    ("api", "secret"),
    ("auth", "token"),
    ("bearer", "token"),
    ("client", "secret"),
    ("credential", "ref"),
    ("id", "token"),
    ("private", "key"),
    ("refresh", "token"),
    ("secret", "key"),
    ("secret", "value"),
})
_CAMEL_ACRONYM_BOUNDARY = re.compile(r"([A-Z]+)([A-Z][a-z])")
_CAMEL_WORD_BOUNDARY = re.compile(r"([a-z0-9])([A-Z])")
_FIELD_TOKEN = re.compile(r"[a-z0-9]+")


@dataclass(frozen=True)
class JsonContract:
    value: Any
    schema_version: int
    size_bytes: int
    sha256: str


def canonical_json_bytes(value: Any) -> bytes:
    try:
        encoded = json.dumps(
            value,
            ensure_ascii=False,
            sort_keys=True,
            separators=(",", ":"),
            allow_nan=False,
        )
    except (TypeError, ValueError) as exc:
        raise ValidationError("JSON 载荷必须可确定序列化，且不能包含 NaN/Infinity") from exc
    return encoded.encode("utf-8")


def json_contract(
    value: Any,
    *,
    schema_version: int = 1,
    max_bytes: int = DEFAULT_JSON_LIMIT_BYTES,
) -> JsonContract:
    if isinstance(schema_version, bool) or not isinstance(schema_version, int) or schema_version < 1:
        raise ValidationError("schema_version 必须是正整数")
    payload = canonical_json_bytes(value)
    if len(payload) > max_bytes:
        raise ValidationError(f"JSON 载荷超过 {max_bytes} 字节上限")
    return JsonContract(
        value=value,
        schema_version=schema_version,
        size_bytes=len(payload),
        sha256=hashlib.sha256(payload).hexdigest(),
    )


def verify_json_contract(
    value: Any,
    *,
    schema_version: int,
    size_bytes: int,
    sha256: str,
    max_bytes: int = DEFAULT_JSON_LIMIT_BYTES,
) -> JsonContract:
    contract = json_contract(
        value,
        schema_version=schema_version,
        max_bytes=max_bytes,
    )
    if contract.size_bytes != size_bytes or contract.sha256 != sha256:
        raise ValidationError("JSON 载荷的 size/checksum 与正文不一致")
    return contract


def assign_json_contract(
    instance: Any,
    field_name: str,
    value: Any,
    *,
    schema_version: int = 1,
    max_bytes: int = DEFAULT_JSON_LIMIT_BYTES,
    size_field_name: str | None = None,
    sha256_field_name: str | None = None,
    schema_version_field_name: str | None = None,
) -> JsonContract:
    contract = json_contract(
        value,
        schema_version=schema_version,
        max_bytes=max_bytes,
    )
    setattr(instance, field_name, value)
    setattr(
        instance,
        size_field_name or f"{field_name}_size_bytes",
        contract.size_bytes,
    )
    setattr(
        instance,
        sha256_field_name or f"{field_name}_sha256",
        contract.sha256,
    )
    setattr(
        instance,
        schema_version_field_name or f"{field_name}_schema_version",
        contract.schema_version,
    )
    return contract


def find_sensitive_json_keys(value: Any, *, prefix: str = "") -> list[str]:
    found: list[str] = []
    if isinstance(value, dict):
        for raw_key, child in value.items():
            key = str(raw_key)
            path = f"{prefix}.{key}" if prefix else key
            normalized = key.casefold().replace("-", "_")
            if any(part in normalized for part in SENSITIVE_KEY_PARTS):
                found.append(path)
            found.extend(find_sensitive_json_keys(child, prefix=path))
    elif isinstance(value, list):
        for index, child in enumerate(value):
            path = f"{prefix}[{index}]"
            found.extend(find_sensitive_json_keys(child, prefix=path))
    return found


def find_forbidden_secret_fields(value: Any, *, prefix: str = "") -> list[str]:
    """Find explicit plaintext-secret fields without matching safe token limits.

    Versioned model/workflow/recipe contracts legitimately contain names such
    as ``max_output_tokens``, ``token_limit`` and ``token_usage``.  Split field
    names across snake_case, kebab-case and camelCase boundaries, then reject
    only unmistakable credential names.  This catches nested ``clientSecret``
    and ``access-key`` fields without treating every occurrence of the word
    ``token`` as secret material.
    """

    found: list[str] = []
    if isinstance(value, dict):
        for raw_key, child in value.items():
            key = str(raw_key)
            path = f"{prefix}.{key}" if prefix else key
            separated = _CAMEL_ACRONYM_BOUNDARY.sub(r"\1_\2", key)
            separated = _CAMEL_WORD_BOUNDARY.sub(r"\1_\2", separated)
            tokens = tuple(_FIELD_TOKEN.findall(separated.casefold()))
            token_pairs = set(zip(tokens, tokens[1:]))
            if (
                len(tokens) == 1
                and tokens[0] in FORBIDDEN_SECRET_SINGLE_TOKENS
            ) or bool(token_pairs & FORBIDDEN_SECRET_TOKEN_PAIRS):
                found.append(path)
            found.extend(find_forbidden_secret_fields(child, prefix=path))
    elif isinstance(value, list):
        for index, child in enumerate(value):
            found.extend(
                find_forbidden_secret_fields(child, prefix=f"{prefix}[{index}]"),
            )
    return found
