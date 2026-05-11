"""Symmetric encryption for broker credentials at rest.

We use Fernet (AES-128-CBC + HMAC-SHA256). The key lives in CREDENTIAL_ENCRYPTION_KEY.
Rotating that key invalidates every stored credential — handle rotation by reading,
re-encrypting under the new key, and deploying both transitionally if needed.
"""
import json
from functools import lru_cache
from typing import Any

from cryptography.fernet import Fernet, InvalidToken

from app.config import get_settings


@lru_cache
def _fernet() -> Fernet:
    key = get_settings().credential_encryption_key
    return Fernet(key.encode() if isinstance(key, str) else key)


def encrypt_json(data: dict[str, Any]) -> str:
    return _fernet().encrypt(json.dumps(data, separators=(",", ":")).encode()).decode()


def decrypt_json(token: str) -> dict[str, Any]:
    try:
        return json.loads(_fernet().decrypt(token.encode()).decode())
    except InvalidToken as exc:
        raise ValueError("credential_decrypt_failed") from exc
