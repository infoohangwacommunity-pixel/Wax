"""Credential Vault package (ADR-0040, Phase 7).

The vault NEVER exposes secrets to the model. Provider-neutral;
understands resource types, not brands.
"""

from __future__ import annotations

from wax.runtime.vault.service import (
    UNIVERSAL_CONNECTORS,
    CredentialVault,
    decrypt_secret,
    encrypt_secret,
    seed_builtin_connectors,
)

__all__ = [
    "UNIVERSAL_CONNECTORS",
    "CredentialVault",
    "decrypt_secret",
    "encrypt_secret",
    "seed_builtin_connectors",
]
