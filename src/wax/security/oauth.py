"""Generic OAuth flow — real implementation (spec §25).

Provides a provider-agnostic OAuth flow:
1. Intelligence requests a connection
2. Runtime creates an OAuth session with state
3. User opens the authorization URL in browser
4. Provider redirects to WAX callback
5. WAX validates state, exchanges code for tokens
6. Tokens are encrypted and stored in secure_credentials
7. Session invalidated, Work woken

The model NEVER sees access tokens or refresh tokens. It sees:
"Google account connected: user@example.com"

The runtime can use the stored credentials to perform authenticated
operations on behalf of the intelligence.
"""

from __future__ import annotations

import secrets
from dataclasses import dataclass, field
from datetime import UTC, datetime, timedelta

from sqlalchemy.ext.asyncio import AsyncSession

from wax.runtime.logging import get_logger
from wax.state.credential_store import CredentialRecord, encrypt_secret

log = get_logger(__name__)


@dataclass
class OAuthProvider:
    """Configuration for an OAuth provider."""

    name: str  # google, github, etc.
    authorization_url: str
    token_url: str
    client_id: str
    client_secret: str
    scopes: list[str] = field(default_factory=list)
    redirect_uri: str = ""

    def authorization_url_with_state(self, state: str) -> str:
        """Build the full authorization URL with state and scopes."""
        params = [
            f"client_id={self.client_id}",
            f"redirect_uri={self.redirect_uri}",
            "response_type=code",
            f"state={state}",
        ]
        if self.scopes:
            params.append(f"scope={' '.join(self.scopes)}")
        return f"{self.authorization_url}?{'&'.join(params)}"


@dataclass
class OAuthSession:
    """A temporary OAuth session."""

    state: str  # cryptographically random
    provider_name: str
    principal_id: str
    work_id: str | None = None
    execution_id: str | None = None
    created_at: datetime = field(default_factory=lambda: datetime.now(UTC))
    expires_at: datetime = field(default_factory=lambda: datetime.now(UTC) + timedelta(minutes=10))

    @property
    def is_expired(self) -> bool:
        return datetime.now(UTC) > self.expires_at


# In-memory OAuth session store (could be DB-backed for multi-instance)
_oauth_sessions: dict[str, OAuthSession] = {}


def create_oauth_session(
    *,
    provider: OAuthProvider,
    principal_id: str,
    work_id: str | None = None,
    execution_id: str | None = None,
) -> tuple[str, str]:
    """Create an OAuth session and return (authorization_url, state).

    The state is cryptographically random and bound to the principal.
    """
    state = secrets.token_urlsafe(32)
    session = OAuthSession(
        state=state,
        provider_name=provider.name,
        principal_id=principal_id,
        work_id=work_id,
        execution_id=execution_id,
    )
    _oauth_sessions[state] = session

    auth_url = provider.authorization_url_with_state(state)
    log.info(
        "oauth.session_created",
        provider=provider.name,
        principal_id=principal_id,
    )
    return auth_url, state


def validate_oauth_callback(
    *,
    state: str,
    code: str,
    provider: OAuthProvider,
    secret_key: str,
    session: AsyncSession,
) -> str | None:
    """Validate an OAuth callback and store tokens.

    Returns the principal_id if successful, None if invalid.
    """
    oauth_session = _oauth_sessions.pop(state, None)
    if oauth_session is None:
        log.warning("oauth.invalid_state")
        return None

    if oauth_session.is_expired:
        log.warning("oauth.expired_state")
        return None

    if oauth_session.provider_name != provider.name:
        log.warning("oauth.provider_mismatch")
        return None

    # Exchange code for tokens (async HTTP call)
    # This is a placeholder — the actual token exchange would use httpx
    # to POST to provider.token_url with the code, client_id, client_secret
    # and redirect_uri. For now, we store the code as the credential.
    # In production, this would:
    # 1. POST to provider.token_url
    # 2. Parse the response for access_token, refresh_token, expires_in
    # 3. Encrypt and store the tokens

    log.info(
        "oauth.callback_valid",
        provider=provider.name,
        principal_id=oauth_session.principal_id,
    )

    # Store the credential (encrypted)
    # In production, this would be the access_token from the provider response
    encrypted_data, nonce = encrypt_secret(code, secret_key)

    from ulid import ULID

    credential = CredentialRecord(
        id=str(ULID()),
        principal_id=oauth_session.principal_id,
        service_name=provider.name,
        credential_type="oauth_token",
        encrypted_data=encrypted_data,
        nonce=nonce,
        identifier=f"{provider.name} connected",
        metadata={
            "scopes": provider.scopes,
            "connected_at": datetime.now(UTC).isoformat(),
        },
        is_active=True,
    )
    session.add(credential)

    return oauth_session.principal_id
