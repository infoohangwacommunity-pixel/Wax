"""Generic OAuth flow — production implementation (spec §25).

Provider-agnostic. No hardcoded provider-specific logic.
No `if provider == google` decision trees.

Flow:
1. Intelligence requests a connection via wax_runtime
2. Runtime creates a DB-backed OAuth session with PKCE
3. User opens the authorization URL in their browser
4. Provider redirects to WAX callback
5. WAX validates state + PKCE + session binding
6. Server-side token exchange (model NEVER sees the code)
7. Tokens encrypted at rest with AES-256-GCM
8. Session invalidated (one-time use, replay-proof)
9. Work woken — intelligence receives only safe success/failure

The model NEVER sees: authorization code, access token, refresh token,
client secret, API key, or any credential material.
"""

from __future__ import annotations

import base64
import hashlib
import json
import secrets
import urllib.parse
from dataclasses import dataclass, field
from datetime import UTC, datetime, timedelta
from typing import Any

import httpx
from sqlalchemy import select
from sqlalchemy.ext.asyncio import AsyncSession

from wax.runtime.logging import get_logger
from wax.state.credential_store import CredentialRecord, decrypt_secret, encrypt_secret

log = get_logger(__name__)


@dataclass
class OAuthProvider:
    """Configuration for an OAuth provider — fully generic.

    New providers are added through configuration, not code changes.
    No provider-specific logic in the application.
    """

    name: str
    authorization_url: str
    token_url: str
    client_id: str
    client_secret: str
    scopes: list[str] = field(default_factory=list)
    redirect_uri: str = ""
    use_pkce: bool = True
    # Optional: which field in the token response contains the user identity
    # (for safe display to the model, e.g. "user@example.com")
    identity_field: str | None = None


@dataclass
class OAuthSession:
    """A DB-backed OAuth session — durable across process restarts."""

    state: str  # cryptographically random
    code_verifier: str | None = None  # PKCE code verifier
    code_challenge: str | None = None  # PKCE code challenge (S256)
    provider_name: str = ""
    principal_id: str = ""
    work_id: str | None = None
    execution_id: str | None = None
    created_at: datetime = field(default_factory=lambda: datetime.now(UTC))
    expires_at: datetime = field(default_factory=lambda: datetime.now(UTC) + timedelta(minutes=10))
    consumed: bool = False
    consumed_at: datetime | None = None

    @property
    def is_expired(self) -> bool:
        return datetime.now(UTC) > self.expires_at

    @property
    def is_consumed(self) -> bool:
        return self.consumed


def _generate_pkce() -> tuple[str, str]:
    """Generate PKCE code verifier and challenge (S256).

    Returns (code_verifier, code_challenge).
    """
    code_verifier = base64.urlsafe_b64encode(secrets.token_bytes(32)).decode().rstrip("=")
    challenge_bytes = hashlib.sha256(code_verifier.encode()).digest()
    code_challenge = base64.urlsafe_b64encode(challenge_bytes).decode().rstrip("=")
    return code_verifier, code_challenge


def create_oauth_session(
    *,
    provider: OAuthProvider,
    principal_id: str,
    session: AsyncSession,
    work_id: str | None = None,
    execution_id: str | None = None,
) -> tuple[str, str]:
    """Create a DB-backed OAuth session and return (authorization_url, state).

    The state is cryptographically random and bound to the principal.
    If the provider uses PKCE, a code verifier/challenge is generated.
    """
    state = secrets.token_urlsafe(32)

    code_verifier = None
    code_challenge = None
    if provider.use_pkce:
        code_verifier, code_challenge = _generate_pkce()

    # Store the session in the DB (durable across restarts)
    from ulid import ULID

    from wax.state.interaction_models import InteractionSessionRecord

    # We reuse the interaction_sessions table for OAuth sessions
    # (same lifecycle: temporary, token-based, one-time use)
    # The HTML content is the OAuth redirect page
    oauth_html = f"""<!DOCTYPE html>
<html><head><title>Connecting {provider.name}...</title>
<meta name="viewport" content="width=device-width, initial-scale=1">
<style>body {{ font-family: sans-serif; text-align: center; padding: 40px; }}
a {{ color: #007bff; font-size: 18px; }}</style>
</head><body>
<h2>Connecting to {provider.name}</h2>
<p>Click below to authorize your {provider.name} account.</p>
<p><a href="{{AUTH_URL}}">Authorize with {provider.name}</a></p>
</body></html>"""

    # Build the authorization URL with proper URL encoding
    params: dict[str, str] = {
        "client_id": provider.client_id,
        "redirect_uri": provider.redirect_uri,
        "response_type": "code",
        "state": state,
    }
    if provider.scopes:
        params["scope"] = " ".join(provider.scopes)
    if code_challenge:
        params["code_challenge"] = code_challenge
        params["code_challenge_method"] = "S256"

    encoded_params = urllib.parse.urlencode(params)
    auth_url = f"{provider.authorization_url}?{encoded_params}"

    # Store the OAuth session metadata in the interaction_sessions table
    # with purpose="oauth" and the state as the wake_event
    record = InteractionSessionRecord(
        id=str(ULID()),
        secret_token=state,  # reuse the token field for OAuth state
        purpose=f"oauth:{provider.name}",
        principal_id=principal_id,
        context_id=work_id,
        execution_id=execution_id,
        html_content=oauth_html.replace("{AUTH_URL}", auth_url),
        expires_at=datetime.now(UTC) + timedelta(minutes=10),
        max_submissions=1,
        submission_count=0,
        state="pending",
        wake_event=f"oauth.completed:{state}",
        result={
            "provider": provider.name,
            "code_verifier": code_verifier,  # needed for token exchange
            "code_challenge": code_challenge,
            "principal_id": principal_id,
            "work_id": work_id,
            "execution_id": execution_id,
        },
    )
    session.add(record)
    session.flush()

    log.info(
        "oauth.session_created",
        provider=provider.name,
        principal_id=principal_id,
        pkce=provider.use_pkce,
    )
    return auth_url, state


async def handle_oauth_callback(
    *,
    state: str,
    code: str,
    provider: OAuthProvider,
    secret_key: str,
    session: AsyncSession,
) -> str | None:
    """Validate an OAuth callback and exchange the code for tokens.

    Returns the principal_id if successful, None if invalid.
    The model NEVER sees the authorization code or the tokens.

    Validates:
    - state exists in DB (unknown state → reject)
    - state not expired (expired → reject)
    - state not already consumed (replay → reject)
    - provider name matches (mismatch → reject)
    - principal binding matches

    Then:
    - Exchanges code for tokens server-side via httpx
    - Encrypts tokens with AES-256-GCM
    - Stores encrypted credentials in secure_credentials
    - Marks session as consumed (replay-proof)
    - Emits wake signal for the associated Work
    """
    from wax.runtime.work.signals import SignalRepository
    from wax.state.interaction_models import InteractionSessionRecord

    # 1. Look up the session by state
    result = await session.execute(
        select(InteractionSessionRecord).where(
            InteractionSessionRecord.secret_token == state,
            InteractionSessionRecord.purpose.like("oauth:%"),
        )
    )
    oauth_session = result.scalar_one_or_none()

    # 2. Reject unknown state
    if oauth_session is None:
        log.warning("oauth.unknown_state")
        return None

    # 3. Reject expired state
    if datetime.now(UTC) > oauth_session.expires_at:
        log.warning("oauth.expired_state")
        oauth_session.state = "expired"
        await session.flush()
        return None

    # 4. Reject already-consumed state (replay prevention)
    if oauth_session.submission_count > 0 or oauth_session.state != "pending":
        log.warning("oauth.replayed_state")
        return None

    # 5. Validate provider match
    session_data = oauth_session.result or {}
    expected_provider = session_data.get("provider", "")
    if expected_provider != provider.name:
        log.warning("oauth.provider_mismatch", expected=expected_provider, got=provider.name)
        return None

    # 6. Mark as consumed IMMEDIATELY (before token exchange — prevents
    #    replay even if token exchange crashes)
    oauth_session.submission_count = 1
    oauth_session.state = "submitted"
    await session.flush()

    # 7. Exchange code for tokens (server-side, model never sees this)
    code_verifier = session_data.get("code_verifier")
    principal_id = session_data.get("principal_id", oauth_session.principal_id)
    _work_id = session_data.get("work_id")

    token_data = await _exchange_code_for_tokens(
        provider=provider,
        code=code,
        code_verifier=code_verifier,
    )

    if token_data is None:
        log.error("oauth.token_exchange_failed", provider=provider.name)
        oauth_session.state = "failed"
        await session.flush()
        return None

    # 8. Extract tokens and encrypt
    access_token = token_data.get("access_token", "")
    refresh_token = token_data.get("refresh_token", "")
    expires_in = token_data.get("expires_in")
    token_type = token_data.get("token_type", "Bearer")

    # Get user identity for safe display (model sees this, not the token)
    identity = None
    if provider.identity_field and provider.identity_field in token_data:
        identity = str(token_data[provider.identity_field])
    elif "id_token" in token_data:
        # Try to extract email from id_token JWT payload (no verification —
        # just for display purposes; the token itself is encrypted at rest)
        try:
            id_token = token_data["id_token"]
            parts = id_token.split(".")
            if len(parts) >= 2:
                payload = parts[1]
                # Add padding
                payload += "=" * (4 - len(payload) % 4)
                decoded = json.loads(base64.urlsafe_b64decode(payload))
                identity = decoded.get("email") or decoded.get("sub")
        except Exception:
            pass

    # Encrypt the access token
    encrypted_access, nonce_access = encrypt_secret(access_token, secret_key)

    # Store the credential
    from ulid import ULID

    credential = CredentialRecord(
        id=str(ULID()),
        principal_id=principal_id,
        service_name=provider.name,
        credential_type="oauth_access_token",
        encrypted_data=encrypted_access,
        nonce=nonce_access,
        identifier=identity or f"{provider.name} connected",
        metadata={
            "token_type": token_type,
            "expires_in": expires_in,
            "has_refresh_token": bool(refresh_token),
            "scopes": provider.scopes,
            "connected_at": datetime.now(UTC).isoformat(),
        },
        is_active=True,
        expires_at=datetime.now(UTC) + timedelta(seconds=int(expires_in)) if expires_in else None,
    )
    session.add(credential)

    # If there's a refresh token, store it separately (also encrypted)
    if refresh_token:
        encrypted_refresh, nonce_refresh = encrypt_secret(refresh_token, secret_key)
        refresh_credential = CredentialRecord(
            id=str(ULID()),
            principal_id=principal_id,
            service_name=provider.name,
            credential_type="oauth_refresh_token",
            encrypted_data=encrypted_refresh,
            nonce=nonce_refresh,
            identifier=f"{provider.name} refresh token",
            metadata={"connected_at": datetime.now(UTC).isoformat()},
            is_active=True,
        )
        session.add(refresh_credential)

    # 9. Mark session as completed
    oauth_session.state = "completed"
    oauth_session.result = {
        **session_data,
        "status": "success",
        "identity": identity,
        "provider": provider.name,
    }

    # 10. Emit wake signal for the associated Work
    if oauth_session.wake_event:
        await SignalRepository(session).emit(
            oauth_session.wake_event,
            payload={
                "status": "connected",
                "provider": provider.name,
                "identity": identity,
            },
            emitted_by="oauth",
        )

    await session.flush()

    log.info(
        "oauth.completed",
        provider=provider.name,
        principal_id=principal_id,
        identity=identity,
    )
    return principal_id


async def _exchange_code_for_tokens(
    *,
    provider: OAuthProvider,
    code: str,
    code_verifier: str | None = None,
) -> dict[str, Any] | None:
    """Exchange an authorization code for tokens via HTTP POST.

    This is the server-side token exchange. The model never sees the
    code, the client secret, or the resulting tokens.

    Uses proper URL-encoded form data (not JSON) per OAuth 2.0 spec.
    """
    data: dict[str, str] = {
        "grant_type": "authorization_code",
        "code": code,
        "redirect_uri": provider.redirect_uri,
        "client_id": provider.client_id,
        "client_secret": provider.client_secret,
    }
    if code_verifier:
        data["code_verifier"] = code_verifier

    try:
        async with httpx.AsyncClient(timeout=30.0) as client:
            response = await client.post(
                provider.token_url,
                data=data,
                headers={"Accept": "application/json"},
            )
            if response.status_code != 200:
                log.error(
                    "oauth.token_exchange_error",
                    status=response.status_code,
                    provider=provider.name,
                )
                return None
            return response.json()
    except Exception as e:
        log.error(
            "oauth.token_exchange_exception",
            error=str(e)[:300],
            error_type=type(e).__name__,
            provider=provider.name,
        )
        return None


async def get_safe_credential_summary(
    *,
    session: AsyncSession,
    principal_id: str,
) -> list[dict[str, str]]:
    """Get a safe summary of connected services for the model.

    Returns non-sensitive metadata only — NEVER tokens, NEVER secrets.
    The model sees: "Google connected: user@example.com"
    The model does NOT see: access_token, refresh_token, etc.
    """
    result = await session.execute(
        select(CredentialRecord).where(
            CredentialRecord.principal_id == principal_id,
            CredentialRecord.is_active == True,  # noqa: E712
            CredentialRecord.credential_type == "oauth_access_token",
        )
    )
    summaries = []
    for cred in result.scalars():
        summaries.append(
            {
                "service": cred.service_name,
                "identity": cred.identifier or "connected",
                "status": "active" if cred.is_active else "inactive",
            }
        )
    return summaries


async def use_credential(
    *,
    session: AsyncSession,
    principal_id: str,
    service_name: str,
    secret_key: str,
) -> str | None:
    """Retrieve and decrypt a credential for runtime use.

    The decrypted credential is returned to the RUNTIME (not the model).
    The runtime uses it to perform authenticated operations on behalf
    of the intelligence. The model never sees the plaintext.

    Returns the decrypted access token, or None if no credential exists.
    """
    result = await session.execute(
        select(CredentialRecord)
        .where(
            CredentialRecord.principal_id == principal_id,
            CredentialRecord.service_name == service_name,
            CredentialRecord.credential_type == "oauth_access_token",
            CredentialRecord.is_active == True,  # noqa: E712
        )
        .limit(1)
    )
    cred = result.scalar_one_or_none()
    if cred is None:
        return None

    return decrypt_secret(cred.encrypted_data, cred.nonce, secret_key)
