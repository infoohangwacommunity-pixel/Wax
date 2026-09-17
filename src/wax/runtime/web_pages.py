"""Interaction sessions — ephemeral human-in-the-loop (Phase G, Parts 16-17/37-41).

NOT "web pages." Interaction sessions.

The AI says: "I need the human to do something outside WhatsApp."
The runtime creates an interaction session:
- session ID
- random secret token (for URL access)
- purpose (OAuth, upload, secret input, confirmation, form)
- principal, context, execution
- expiration
- maximum submissions
- state (pending, submitted, expired, cancelled)
- allowed input type
- result (captured after submission)

The AI generates the HTML interface. The runtime hosts it through the
main FastAPI application (NOT a separate http.server). The user opens
the URL, interacts, the runtime captures the result, emits an event,
and the AI continues. The session expires.

Security:
- Each session has a random secret token in the URL
- Sessions are scoped to a principal + execution
- Secrets are NEVER stored in plain text in the result
- The workspace is NOT served — only the specific HTML the AI generated
- POST handling is real (multipart, form-encoded, JSON)
- Sessions expire automatically
"""

from __future__ import annotations

import secrets
from dataclasses import dataclass, field
from datetime import UTC, datetime, timedelta
from typing import Any

from wax.runtime.logging import get_logger

log = get_logger(__name__)

# In-memory session store (for MVP; can move to DB later)
# Keyed by session_id
_sessions: dict[str, InteractionSession] = {}
# Keyed by secret token (for URL lookup)
_token_index: dict[str, str] = {}


@dataclass
class InteractionSession:
    """A temporary interaction session between the AI and the human."""

    session_id: str
    secret_token: str  # random, in the URL
    purpose: str  # "oauth", "upload", "secret_input", "confirmation", "form"
    principal_id: str
    context_id: str | None = None
    execution_id: str | None = None
    html_content: str = ""
    created_at: datetime = field(default_factory=lambda: datetime.now(UTC))
    expires_at: datetime = field(default_factory=lambda: datetime.now(UTC) + timedelta(minutes=30))
    max_submissions: int = 1
    submission_count: int = 0
    state: str = "pending"  # pending, submitted, expired, cancelled
    result: dict[str, Any] | None = None
    # The event to emit when the session is submitted (wakes the AI)
    wake_event: str | None = None

    @property
    def is_expired(self) -> bool:
        return datetime.now(UTC) > self.expires_at

    @property
    def url_path(self) -> str:
        return f"/i/{self.secret_token}"


def create_session(
    *,
    purpose: str,
    principal_id: str,
    html_content: str,
    context_id: str | None = None,
    execution_id: str | None = None,
    expires_in_minutes: int = 30,
    max_submissions: int = 1,
    wake_event: str | None = None,
) -> InteractionSession:
    """Create a new interaction session.

    Returns the session. The URL is `session.url_path` — the AI sends
    this to the user via WhatsApp.
    """
    session_id = secrets.token_hex(8)
    secret_token = secrets.token_urlsafe(32)

    session = InteractionSession(
        session_id=session_id,
        secret_token=secret_token,
        purpose=purpose,
        principal_id=principal_id,
        context_id=context_id,
        execution_id=execution_id,
        html_content=html_content,
        expires_at=datetime.now(UTC) + timedelta(minutes=expires_in_minutes),
        max_submissions=max_submissions,
        wake_event=wake_event,
    )

    _sessions[session_id] = session
    _token_index[secret_token] = session_id

    log.info(
        "interaction.session_created",
        session_id=session_id,
        purpose=purpose,
        principal_id=principal_id,
        expires_at=session.expires_at.isoformat(),
    )

    return session


def get_session_by_token(secret_token: str) -> InteractionSession | None:
    """Look up a session by its secret token (from the URL)."""
    session_id = _token_index.get(secret_token)
    if session_id is None:
        return None
    session = _sessions.get(session_id)
    if session is None:
        return None
    if session.is_expired and session.state == "pending":
        session.state = "expired"
    return session


def get_session(session_id: str) -> InteractionSession | None:
    """Look up a session by ID."""
    session = _sessions.get(session_id)
    if session and session.is_expired and session.state == "pending":
        session.state = "expired"
    return session


def submit_session(
    secret_token: str,
    *,
    form_data: dict[str, Any],
    files: dict[str, bytes] | None = None,
) -> InteractionSession | None:
    """Process a submission for a session.

    Captures the result, marks the session as submitted, and returns it.
    The caller is responsible for emitting the wake event.
    """
    session = get_session_by_token(secret_token)
    if session is None:
        return None
    if session.state != "pending":
        return session  # already submitted or expired
    if session.submission_count >= session.max_submissions:
        session.state = "expired"
        return session

    # Capture the result — NEVER store secrets in plain text
    result: dict[str, Any] = {
        "submitted_at": datetime.now(UTC).isoformat(),
        "purpose": session.purpose,
    }

    # For secret_input purpose, mark that a secret was received but don't
    # store the value in the session result. The AI reads it from the
    # execution's secure storage.
    if session.purpose == "secret_input":
        result["secret_received"] = True
        result["secret_field"] = next(iter(form_data.keys())) if form_data else None
        # The actual secret value is NOT stored in the session — it goes
        # to a secure path (env var, encrypted storage, etc.)
    elif session.purpose == "upload":
        result["files"] = {}
        if files:
            for filename, content in files.items():
                result["files"][filename] = {
                    "size_bytes": len(content),
                    "received": True,
                }
    else:
        # General form data — store non-secret fields
        result["form_data"] = {
            k: v for k, v in form_data.items() if not k.lower().endswith("_secret")
        }

    session.result = result
    session.submission_count += 1
    if session.submission_count >= session.max_submissions:
        session.state = "submitted"

    log.info(
        "interaction.session_submitted",
        session_id=session.session_id,
        purpose=session.purpose,
        submission_count=session.submission_count,
    )

    return session


def cancel_session(session_id: str) -> bool:
    """Cancel a session (e.g., the AI decided it no longer needs it)."""
    session = _sessions.get(session_id)
    if session is None:
        return False
    session.state = "cancelled"
    log.info("interaction.session_cancelled", session_id=session_id)
    return True


def cleanup_expired() -> int:
    """Remove expired sessions. Returns the count removed."""
    expired_ids = [sid for sid, s in _sessions.items() if s.is_expired and s.state != "submitted"]
    for sid in expired_ids:
        session = _sessions.pop(sid, None)
        if session:
            _token_index.pop(session.secret_token, None)
    if expired_ids:
        log.info("interaction.sessions_cleaned", count=len(expired_ids))
    return len(expired_ids)


def list_active_for_principal(principal_id: str) -> list[InteractionSession]:
    """List active sessions for a principal."""
    return [
        s
        for s in _sessions.values()
        if s.principal_id == principal_id and s.state == "pending" and not s.is_expired
    ]


# ---------------------------------------------------------------------------
# HTML generators — the AI can call these or write its own HTML
# ---------------------------------------------------------------------------


def upload_page_html(*, title: str = "Upload File", field_name: str = "file") -> str:
    """Generate a file upload page."""
    return f"""<!DOCTYPE html>
<html>
<head><title>{title}</title>
<meta name="viewport" content="width=device-width, initial-scale=1">
<style>
body {{ font-family: sans-serif; max-width: 500px; margin: 40px auto; padding: 20px; }}
h1 {{ color: #333; }}
form {{ display: flex; flex-direction: column; gap: 16px; }}
button {{ padding: 12px; background: #007bff; color: white; border: none; border-radius: 6px; font-size: 16px; cursor: pointer; }}
button:hover {{ background: #0056b3; }}
input[type=file] {{ padding: 8px; }}
</style>
</head>
<body>
<h1>{title}</h1>
<form method="POST" enctype="multipart/form-data">
  <input type="file" name="{field_name}" required>
  <button type="submit">Upload</button>
</form>
</body>
</html>"""


def secret_input_html(
    *, title: str = "Enter Secret", label: str = "Secret", field_name: str = "secret"
) -> str:
    """Generate a secret input page (for API keys, tokens, etc.)."""
    return f"""<!DOCTYPE html>
<html>
<head><title>{title}</title>
<meta name="viewport" content="width=device-width, initial-scale=1">
<style>
body {{ font-family: sans-serif; max-width: 500px; margin: 40px auto; padding: 20px; }}
h1 {{ color: #333; }}
form {{ display: flex; flex-direction: column; gap: 16px; }}
label {{ font-weight: bold; }}
input[type=password] {{ padding: 12px; font-size: 16px; border: 1px solid #ccc; border-radius: 6px; }}
button {{ padding: 12px; background: #007bff; color: white; border: none; border-radius: 6px; font-size: 16px; cursor: pointer; }}
button:hover {{ background: #0056b3; }}
.note {{ color: #666; font-size: 14px; }}
</style>
</head>
<body>
<h1>{title}</h1>
<p class="note">This is a secure one-time input. Your secret is transmitted encrypted and never stored in chat history.</p>
<form method="POST">
  <label for="{field_name}">{label}:</label>
  <input type="password" name="{field_name}" id="{field_name}" required>
  <button type="submit">Submit Securely</button>
</form>
</body>
</html>"""


def confirmation_page_html(
    *,
    title: str = "Confirm",
    message: str = "Do you want to proceed?",
    confirm_label: str = "Confirm",
    cancel_label: str = "Cancel",
) -> str:
    """Generate a confirmation page."""
    return f"""<!DOCTYPE html>
<html>
<head><title>{title}</title>
<meta name="viewport" content="width=device-width, initial-scale=1">
<style>
body {{ font-family: sans-serif; max-width: 500px; margin: 40px auto; padding: 20px; }}
h1 {{ color: #333; }}
.message {{ font-size: 18px; margin: 24px 0; }}
.buttons {{ display: flex; gap: 12px; }}
button {{ padding: 12px 24px; border: none; border-radius: 6px; font-size: 16px; cursor: pointer; flex: 1; }}
.confirm {{ background: #28a745; color: white; }}
.cancel {{ background: #dc3545; color: white; }}
</style>
</head>
<body>
<h1>{title}</h1>
<div class="message">{message}</div>
<form method="POST">
  <div class="buttons">
    <button type="submit" name="action" value="confirm" class="confirm">{confirm_label}</button>
    <button type="submit" name="action" value="cancel" class="cancel">{cancel_label}</button>
  </div>
</form>
</body>
</html>"""


def generic_form_html(*, title: str = "Form", fields: list[dict[str, str]]) -> str:
    """Generate a generic form page.

    `fields` is a list of {name, label, type, required} dicts.
    """
    field_html = ""
    for f in fields:
        name = f.get("name", "")
        label = f.get("label", name)
        ftype = f.get("type", "text")
        required = "required" if f.get("required", False) else ""
        field_html += f"""
  <label for="{name}">{label}:</label>
  <input type="{ftype}" name="{name}" id="{name}" {required}>
"""
    return f"""<!DOCTYPE html>
<html>
<head><title>{title}</title>
<meta name="viewport" content="width=device-width, initial-scale=1">
<style>
body {{ font-family: sans-serif; max-width: 500px; margin: 40px auto; padding: 20px; }}
h1 {{ color: #333; }}
form {{ display: flex; flex-direction: column; gap: 12px; }}
label {{ font-weight: bold; }}
input {{ padding: 10px; font-size: 16px; border: 1px solid #ccc; border-radius: 6px; }}
button {{ padding: 12px; background: #007bff; color: white; border: none; border-radius: 6px; font-size: 16px; cursor: pointer; margin-top: 8px; }}
</style>
</head>
<body>
<h1>{title}</h1>
<form method="POST">
{field_html}
  <button type="submit">Submit</button>
</form>
</body>
</html>"""
