"""API Key 加解密。使用 Fernet（对称加密）。

密钥从 JWT_SECRET 派生（SHA-256 → 32 字节 url-safe base64）。
注意：JWT_SECRET 变更后，已加密的 API Key 将无法解密，需要重新配置。
"""
import base64
import hashlib
import logging
import os

from cryptography.fernet import Fernet

logger = logging.getLogger(__name__)


def _get_fernet() -> Fernet:
    secret = os.getenv("JWT_SECRET", "")
    if not secret:
        raise RuntimeError("JWT_SECRET 未配置，无法加密 API Key")
    key_bytes = hashlib.sha256(secret.encode("utf-8")).digest()
    key = base64.urlsafe_b64encode(key_bytes)
    return Fernet(key)


def encrypt_api_key(plain: str) -> str:
    f = _get_fernet()
    return f.encrypt(plain.encode("utf-8")).decode("utf-8")


def decrypt_api_key(enc: str) -> str:
    f = _get_fernet()
    return f.decrypt(enc.encode("utf-8")).decode("utf-8")
