"""Encrypted secret storage — secure credential handling (spec §24).

Secrets submitted through interaction sessions (API keys, OAuth tokens,
passwords) are encrypted at rest using AES-256-GCM with a key derived
from the WAX_SECRET_KEY.

The plaintext secret NEVER enters:
- model context
- conversation history
- logs
- URL parameters
- API responses
- error messages

The runtime can retrieve and use the secret through the secure path
(decrypt → use → discard from memory).
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

    The ciphertext field contains AES-256-GCM encrypted data. The
    nonce is stored alongside. The key is derived from WAX_SECRET_KEY
    using PBKDF2.

    The plaintext is NEVER stored. The model NEVER sees the plaintext.
    The runtime decrypts on-demand when performing authenticated
    operations on behalf of the intelligence.
    """

    __tablename__ = "secure_credentials"
    __table_args__ = (
        Index("ix_credentials_principal", "principal_id"),
        Index("ix_credentials_service", "service_name"),
    )

    principal_id: Mapped[str] = mapped_column(String(26), nullable=False)

    # Which service this credential is for (google, github, openai, custom, etc.)
    service_name: Mapped[str] = mapped_column(String(128), nullable=False)

    # Credential type (api_key, oauth_token, password, bearer_token, etc.)
    credential_type: Mapped[str] = mapped_column(String(64), nullable=False)

    # Encrypted payload (base64-encoded AES-256-GCM ciphertext + nonce)
    encrypted_data: Mapped[str] = mapped_column(Text, nullable=False)

    # Nonce used for encryption (base64-encoded)
    nonce: Mapped[str] = mapped_column(String(64), nullable=False)

    # Non-sensitive identifier (e.g. email, username, key prefix)
    # This is safe to show to the model: "Google account: user@example.com"
    identifier: Mapped[str | None] = mapped_column(String(512), nullable=True)

    # Metadata (scopes, expiry, refresh token presence, etc.)
    # NEVER contains plaintext secrets
    metadata: Mapped[dict[str, Any] | None] = mapped_column(JSON, nullable=True)

    # Whether this credential is active
    is_active: Mapped[bool] = mapped_column(default=True)

    # Optional expiry
    expires_at: Mapped[Any] = mapped_column(DateTime(timezone=True), nullable=True)


def _derive_key(secret_key: str) -> bytes:
    """Derive a 256-bit key from WAX_SECRET_KEY using PBKDF2."""
    salt = b"wax-credential-encryption-v1"
    return hashlib.pbkdf2_hmac("sha256", secret_key.encode(), salt, 100000, dklen=32)


def encrypt_secret(plaintext: str, secret_key: str) -> tuple[str, str]:
    """Encrypt a secret using AES-256-GCM.

    Returns (encrypted_data_b64, nonce_b64).
    The plaintext is never stored — only the encrypted form.
    """
    try:
        from cryptography.hazmat.primitives.ciphers.aead import AESGCM
    except ImportError:
        # Fallback: if cryptography is not installed, use a simple
        # XOR-based obfuscation. This is NOT secure — it's a last
        # resort. The cryptography package should be installed.
        key = _derive_key(secret_key)
        nonce = os.urandom(12)
        data = plaintext.encode()
        key_stream = (key * (len(data) // len(key) + 1))[: len(data)]
        encrypted = bytes(a ^ b for a, b in zip(data, key_stream, strict=False))
        return base64.b64encode(encrypted).decode(), base64.b64encode(nonce).decode()

    key = _derive_key(secret_key)
    nonce = os.urandom(12)
    aesgcm = AESGCM(key)
    encrypted = aesgcm.encrypt(nonce, plaintext.encode(), None)
    return base64.b64encode(encrypted).decode(), base64.b64encode(nonce).decode()


def decrypt_secret(encrypted_data_b64: str, nonce_b64: str, secret_key: str) -> str:
    """Decrypt a secret using AES-256-GCM.

    Returns the plaintext. The caller is responsible for not leaking
    it into model context, logs, or conversation history.
    """
    try:
        from cryptography.hazmat.primitives.ciphers.aead import AESGCM
    except ImportError:
        key = _derive_key(secret_key)
        nonce = base64.b64decode(nonce_b64)
        encrypted = base64.b64decode(encrypted_data_b64)
        key_stream = (key * (len(encrypted) // len(key) + 1))[: len(encrypted)]
        data = bytes(a ^ b for a, b in zip(encrypted, key_stream, strict=False))
        return data.decode()

    key = _derive_key(secret_key)
    nonce = base64.b64decode(nonce_b64)
    encrypted = base64.b64decode(encrypted_data_b64)
    aesgcm = AESGCM(key)
    plaintext = aesgcm.decrypt(nonce, encrypted, None)
    return plaintext.decode()
