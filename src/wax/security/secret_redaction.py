"""Secret redaction for terminal output (spec §51).

Terminal commands can output secrets (env, cat config.json, curl
responses with tokens). Before terminal output enters model context,
high-risk credential patterns are redacted.

This is defense-in-depth — the primary boundary is secret isolation
in the environment, not regex filtering of output.
"""

from __future__ import annotations

import re

_SECRET_PATTERNS = [
    (re.compile(r"sk-[a-zA-Z0-9]{20,}"), "sk-[REDACTED]"),
    (re.compile(r"ghp_[a-zA-Z0-9]{36,}"), "ghp_[REDACTED]"),
    (re.compile(r"github_pat_[a-zA-Z0-9_]{22}_[a-zA-Z0-9]{59}"), "github_pat_[REDACTED]"),
    (re.compile(r"gsk_[a-zA-Z0-9]{30,}"), "gsk_[REDACTED]"),
    (re.compile(r"sk-ant-[a-zA-Z0-9-_]{30,}"), "sk-ant-[REDACTED]"),
    (
        re.compile(r"eyJ[a-zA-Z0-9_-]{10,}\.[a-zA-Z0-9_-]{10,}\.[a-zA-Z0-9_-]{10,}"),
        "eyJ[REDACTED_JWT]",
    ),
    (re.compile(r"[Bb]earer\s+[a-zA-Z0-9._-]{20,}"), "Bearer [REDACTED]"),
    (re.compile(r"AKIA[A-Z0-9]{16}"), "AKIA[REDACTED]"),
    (
        re.compile(r"-----BEGIN [A-Z ]+PRIVATE KEY-----[\s\S]*?-----END [A-Z ]+PRIVATE KEY-----"),
        "-----BEGIN [REDACTED PRIVATE KEY]-----",
    ),
    (
        re.compile(r"(postgresql|postgres|mysql|redis|mongodb)://[^:]+:([^@]+)@"),
        r"\1://\1user:[REDACTED]@",
    ),
    (
        re.compile(
            r'(?i)(api[_-]?key|access[_-]?token|secret[_-]?key|auth[_-]?token)\s*[=:]\s*["\']?([a-zA-Z0-9_\-\.]{20,})'
        ),
        r"\1=[REDACTED]",
    ),
    (
        re.compile(
            r"(WAX_SECRET_KEY|WAX_DATABASE_URL|WAX_LLM_API_KEY|WAX_WHATSAPP_ACCESS_TOKEN|WAX_WHATSAPP_APP_SECRET)\s*=\s*\S+"
        ),
        r"\1=[REDACTED]",
    ),
]


def redact_secrets(text: str) -> str:
    """Redact known secret patterns from terminal output."""
    if not text:
        return text
    redacted = text
    for pattern, replacement in _SECRET_PATTERNS:
        redacted = pattern.sub(replacement, redacted)
    return redacted


def redact_env_vars(env: dict[str, str]) -> dict[str, str]:
    """Filter sensitive env vars from the terminal subprocess environment."""
    SENSITIVE_PREFIXES = (
        "WAX_SECRET_KEY",
        "WAX_DATABASE_URL",
        "WAX_LLM_API_KEY",
        "WAX_OPENAI_API_KEY",
        "WAX_ANTHROPIC_API_KEY",
        "WAX_WHATSAPP_ACCESS_TOKEN",
        "WAX_WHATSAPP_APP_SECRET",
        "WAX_LLM_FALLBACK_",
        "DATABASE_URL",
    )
    filtered = {}
    for key, value in env.items():
        if any(key.startswith(prefix) for prefix in SENSITIVE_PREFIXES):
            continue
        filtered[key] = value
    return filtered
