"""Encrypted secret storage — AES-256-GCM ONLY (spec §24).

No XOR fallback. If the cryptography package is not available, fail
closed with a clear operational error.

Plaintext secrets NEVER enter model context, conversation history,
logs, URL parameters, API responses, or error messages.
"""

from __future__ import annotations

import base64
import hashlib
import os
from typing import Any

from sqlalchemy import DateTime, Index, String, Text
from sqlalchemy.orm import Mapped, mapped_column
from sqlalchemy.types import JSON

from wax.state.models import Base, TimestampMixin, ULIDPrimaryKeyMixin


class CredentialRecord(Base, ULIDPrimaryKeyMixin, TimestampMixin):
    """An encrypted credential associated with a principal/service.

    The ciphertext field contains AES-256-GCM encrypted data.
    The plaintext is NEVER stored, NEVER in model context, NEVER logged.
    """

    __tablename__ = "secure_credentials"
    __table_args__ = (
        Index("ix_credentials_principal", "principal_id"),
        Index("ix_credentials_service", "service_name"),
    )

    principal_id: Mapped[str] = mapped_column(String(26), nullable=False)
    service_name: Mapped[str] = mapped_column(String(128), nullable=False)
    credential_type: Mapped[str] = mapped_column(String(64), nullable=False)

    # Encrypted payload (base64-encoded AES-256-GCM ciphertext)
    encrypted_data: Mapped[str] = mapped_column(Text, nullable=False)
    nonce: Mapped[str] = mapped_column(String(64), nullable=False)

    # Non-sensitive identifier (safe to show to the model)
    identifier: Mapped[str | None] = mapped_column(String(512), nullable=True)

    # Metadata (scopes, expiry, refresh presence — NEVER plaintext secrets)
    metadata: Mapped[dict[str, Any] | None] = mapped_column(JSON, nullable=True)
    is_active: Mapped[bool] = mapped_column(default=True)
    expires_at: Mapped[Any] = mapped_column(DateTime(timezone=True), nullable=True)


def _derive_key(secret_key: str) -> bytes:
    """Derive a 256-bit key from WAX_SECRET_KEY using PBKDF2."""
    salt = b"wax-credential-encryption-v1"
    return hashlib.pbkdf2_hmac("sha256", secret_key.encode(), salt, 100000, dklen=32)


def encrypt_secret(plaintext: str, secret_key: str) -> tuple[str, str]:
    """Encrypt a secret using AES-256-GCM.

    Returns (encrypted_data_b64, nonce_b64).
    Fails closed if cryptography package is not available.
    """
    try:
        from cryptography.hazmat.primitives.ciphers.aead import AESGCM
    except ImportError as e:
        raise RuntimeError(
            "The 'cryptography' package is required for secret encryption. "
            "Install it with: pip install cryptography"
        ) from e

    key = _derive_key(secret_key)
    nonce = os.urandom(12)
    aesgcm = AESGCM(key)
    encrypted = aesgcm.encrypt(nonce, plaintext.encode(), None)
    return base64.b64encode(encrypted).decode(), base64.b64encode(nonce).decode()


def decrypt_secret(encrypted_data_b64: str, nonce_b64: str, secret_key: str) -> str:
    """Decrypt a secret using AES-256-GCM.

    Fails closed if cryptography package is not available.
    """
    try:
        from cryptography.hazmat.primitives.ciphers.aead import AESGCM
    except ImportError as e:
        raise RuntimeError(
            "The 'cryptography' package is required for secret decryption. "
            "Install it with: pip install cryptography"
        ) from e

    key = _derive_key(secret_key)
    nonce = base64.b64decode(nonce_b64)
    encrypted = base64.b64decode(encrypted_data_b64)
    aesgcm = AESGCM(key)
    plaintext = aesgcm.decrypt(nonce, encrypted, None)
    return plaintext.decode()
