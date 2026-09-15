"""Authenticated encryption for the credential vault (ADR-0047, P0-Vault).

Replaces XOR cipher with AES-GCM (Authenticated Encryption with
Associated Data). Uses envelope encryption: a master key encrypts
a per-record data key, which encrypts the secret.

Properties:
- Random nonce per secret (never reused)
- Authentication tag (tamper detection)
- Key version (for rotation)
- Associated data (record_id + principal_id bound to ciphertext)
- Fail-closed on tamper/wrong key
- No plaintext logging
"""

from __future__ import annotations

import base64
import hashlib
import json
import os
from dataclasses import dataclass

from wax.runtime.logging import get_logger

log = get_logger(__name__)


@dataclass(frozen=True)
class EncryptedSecret:
    """Envelope-encrypted secret with per-record data key."""

    ciphertext: str  # base64-encoded
    ciphertext_nonce: str  # base64-encoded (12 bytes for AES-GCM)
    wrapped_data_key: str  # base64-encoded (data key encrypted with master key)
    wrapped_key_nonce: str  # base64-encoded (12 bytes for AES-GCM)
    key_version: int
    encryption_version: str = "aes-gcm-1"


def _get_master_key() -> bytes:
    """Get the 32-byte master key from WAX_VAULT_KEY.

    Production: raises if WAX_VAULT_KEY is missing and WAX_ENV=production.
    Dev/staging: falls back to a dev key with a loud warning.
    """
    env_key = os.environ.get("WAX_VAULT_KEY")
    if env_key:
        return hashlib.sha256(env_key.encode("utf-8")).digest()

    wax_env = os.environ.get("WAX_ENV", "development").lower()
    if wax_env == "production":
        raise RuntimeError(
            "WAX_VAULT_KEY is not set and WAX_ENV=production. "
            "The credential vault requires an encryption key in production."
        )

    log.warning(
        "vault.dev_key_in_use",
        detail="WAX_VAULT_KEY not set; using development-mode key. "
        "Production deployments MUST set WAX_VAULT_KEY.",
    )
    return hashlib.sha256(b"WAX_DEV_VAULT_KEY_DO_NOT_USE_IN_PRODUCTION").digest()


def encrypt_secret(
    plaintext: str,
    *,
    record_id: str = "",
    principal_id: str = "",
) -> EncryptedSecret:
    """Encrypt a secret using AES-GCM with envelope encryption.

    1. Generate a random 32-byte data key
    2. Encrypt the secret with the data key (AES-GCM, random nonce)
    3. Wrap (encrypt) the data key with the master key (AES-GCM, random nonce)
    4. Return the envelope (ciphertext + nonces + wrapped key + version)
    """
    from cryptography.hazmat.primitives.ciphers.aead import AESGCM

    master_key = _get_master_key()

    # Generate a random 32-byte data key
    data_key = os.urandom(32)

    # Encrypt the secret with the data key
    secret_nonce = os.urandom(12)
    associated_data = f"{record_id}:{principal_id}".encode()
    aesgcm_data = AESGCM(data_key)
    ciphertext = aesgcm_data.encrypt(secret_nonce, plaintext.encode("utf-8"), associated_data)

    # Wrap the data key with the master key
    wrap_nonce = os.urandom(12)
    aesgcm_master = AESGCM(master_key)
    wrapped_key = aesgcm_master.encrypt(wrap_nonce, data_key, associated_data)

    return EncryptedSecret(
        ciphertext=base64.b64encode(ciphertext).decode("ascii"),
        ciphertext_nonce=base64.b64encode(secret_nonce).decode("ascii"),
        wrapped_data_key=base64.b64encode(wrapped_key).decode("ascii"),
        wrapped_key_nonce=base64.b64encode(wrap_nonce).decode("ascii"),
        key_version=1,
    )


def decrypt_secret(
    envelope: EncryptedSecret, *, record_id: str = "", principal_id: str = ""
) -> str:
    """Decrypt a secret using AES-GCM.

    1. Unwrap the data key with the master key
    2. Decrypt the secret with the data key
    3. Verify the authentication tag (tamper detection)
    4. Return the plaintext

    Raises on tamper, wrong key, or corruption.
    """
    from cryptography.hazmat.primitives.ciphers.aead import AESGCM

    master_key = _get_master_key()

    # Unwrap the data key
    wrap_nonce = base64.b64decode(envelope.wrapped_key_nonce)
    wrapped_key = base64.b64decode(envelope.wrapped_data_key)
    associated_data = f"{record_id}:{principal_id}".encode()
    aesgcm_master = AESGCM(master_key)
    data_key = aesgcm_master.decrypt(wrap_nonce, wrapped_key, associated_data)

    # Decrypt the secret
    secret_nonce = base64.b64decode(envelope.ciphertext_nonce)
    ciphertext = base64.b64decode(envelope.ciphertext)
    aesgcm_data = AESGCM(data_key)
    plaintext_bytes = aesgcm_data.decrypt(secret_nonce, ciphertext, associated_data)

    return plaintext_bytes.decode("utf-8")


def serialize_envelope(envelope: EncryptedSecret) -> str:
    """Serialize an envelope to a JSON string for storage."""
    return json.dumps(
        {
            "ciphertext": envelope.ciphertext,
            "ciphertext_nonce": envelope.ciphertext_nonce,
            "wrapped_data_key": envelope.wrapped_data_key,
            "wrapped_key_nonce": envelope.wrapped_key_nonce,
            "key_version": envelope.key_version,
            "encryption_version": envelope.encryption_version,
        }
    )


def deserialize_envelope(json_str: str) -> EncryptedSecret:
    """Deserialize an envelope from a JSON string."""
    data = json.loads(json_str)
    return EncryptedSecret(
        ciphertext=data["ciphertext"],
        ciphertext_nonce=data["ciphertext_nonce"],
        wrapped_data_key=data["wrapped_data_key"],
        wrapped_key_nonce=data["wrapped_key_nonce"],
        key_version=data["key_version"],
        encryption_version=data.get("encryption_version", "aes-gcm-1"),
    )
