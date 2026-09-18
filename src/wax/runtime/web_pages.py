"""Interaction sessions — DB-backed (Bug 3 fix).

The in-memory dict couldn't cross process boundaries. The terminal
subprocess that calls serve_page() is a different process from the
FastAPI server. Now sessions are stored in PostgreSQL — they survive
process boundaries, Railway restarts, and multiple workers.

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
from datetime import UTC, datetime, timedelta
from typing import Any

from sqlalchemy import select
from sqlalchemy.ext.asyncio import AsyncSession

from wax.runtime.logging import get_logger
from wax.state.interaction_models import InteractionSessionRecord

log = get_logger(__name__)


async def create_session(
    *,
    session: AsyncSession,
    purpose: str,
    principal_id: str,
    html_content: str,
    context_id: str | None = None,
    execution_id: str | None = None,
    expires_in_minutes: int = 30,
    max_submissions: int = 1,
    wake_event: str | None = None,
) -> InteractionSessionRecord:
    """Create a new interaction session in the DB."""
    from ulid import ULID

    record = InteractionSessionRecord(
        id=str(ULID()),
        secret_token=secrets.token_urlsafe(32),
        purpose=purpose,
        principal_id=principal_id,
        context_id=context_id,
        execution_id=execution_id,
        html_content=html_content,
        expires_at=datetime.now(UTC) + timedelta(minutes=expires_in_minutes),
        max_submissions=max_submissions,
        submission_count=0,
        state="pending",
        wake_event=wake_event,
    )
    session.add(record)
    await session.flush()

    log.info(
        "interaction.session_created",
        session_id=record.id,
        purpose=purpose,
        principal_id=principal_id,
    )
    return record


async def get_session_by_token(
    session: AsyncSession, secret_token: str
) -> InteractionSessionRecord | None:
    """Look up a session by its secret token."""
    result = await session.execute(
        select(InteractionSessionRecord).where(
            InteractionSessionRecord.secret_token == secret_token
        )
    )
    record = result.scalar_one_or_none()
    if (
        record and record.is_expired
        if hasattr(record, "is_expired")
        else (record and datetime.now(UTC) > record.expires_at and record.state == "pending")
    ):
        record.state = "expired"
        await session.flush()
    return record


async def get_session(session: AsyncSession, session_id: str) -> InteractionSessionRecord | None:
    """Look up a session by ID."""
    return await session.get(InteractionSessionRecord, session_id)


async def submit_session(
    session: AsyncSession,
    secret_token: str,
    *,
    form_data: dict[str, Any],
    files: dict[str, bytes] | None = None,
) -> InteractionSessionRecord | None:
    """Process a submission for a session."""
    record = await get_session_by_token(session, secret_token)
    if record is None:
        return None
    if record.state != "pending":
        return record
    if record.submission_count >= record.max_submissions:
        record.state = "expired"
        await session.flush()
        return record

    result: dict[str, Any] = {
        "submitted_at": datetime.now(UTC).isoformat(),
        "purpose": record.purpose,
    }

    if record.purpose == "secret_input":
        result["secret_received"] = True
        result["secret_field"] = next(iter(form_data.keys())) if form_data else None
    elif record.purpose == "upload":
        result["files"] = {}
        if files:
            for filename, content in files.items():
                result["files"][filename] = {"size_bytes": len(content), "received": True}
    else:
        result["form_data"] = {
            k: v for k, v in form_data.items() if not k.lower().endswith("_secret")
        }

    record.result = result
    record.submission_count += 1
    if record.submission_count >= record.max_submissions:
        record.state = "submitted"
    await session.flush()

    log.info(
        "interaction.session_submitted",
        session_id=record.id,
        purpose=record.purpose,
    )
    return record


async def cancel_session(session: AsyncSession, session_id: str) -> bool:
    """Cancel a session."""
    record = await session.get(InteractionSessionRecord, session_id)
    if record is None:
        return False
    record.state = "cancelled"
    await session.flush()
    return True


async def list_active_for_principal(
    session: AsyncSession, principal_id: str
) -> list[InteractionSessionRecord]:
    """List active sessions for a principal."""
    result = await session.execute(
        select(InteractionSessionRecord)
        .where(
            InteractionSessionRecord.principal_id == principal_id,
            InteractionSessionRecord.state == "pending",
        )
        .order_by(InteractionSessionRecord.created_at.desc())
    )
    return list(result.scalars().all())


# ---------------------------------------------------------------------------
# HTML generators
# ---------------------------------------------------------------------------


def upload_page_html(*, title: str = "Upload File", field_name: str = "file") -> str:
    return f"""<!DOCTYPE html>
<html><head><title>{title}</title>
<meta name="viewport" content="width=device-width, initial-scale=1">
<style>
body {{ font-family: sans-serif; max-width: 500px; margin: 40px auto; padding: 20px; }}
h1 {{ color: #333; }}
form {{ display: flex; flex-direction: column; gap: 16px; }}
button {{ padding: 12px; background: #007bff; color: white; border: none; border-radius: 6px; font-size: 16px; cursor: pointer; }}
input[type=file] {{ padding: 8px; }}
</style></head><body>
<h1>{title}</h1>
<form method="POST" enctype="multipart/form-data">
  <input type="file" name="{field_name}" required>
  <button type="submit">Upload</button>
</form></body></html>"""


def secret_input_html(
    *, title: str = "Enter Secret", label: str = "Secret", field_name: str = "secret"
) -> str:
    return f"""<!DOCTYPE html>
<html><head><title>{title}</title>
<meta name="viewport" content="width=device-width, initial-scale=1">
<style>
body {{ font-family: sans-serif; max-width: 500px; margin: 40px auto; padding: 20px; }}
h1 {{ color: #333; }}
form {{ display: flex; flex-direction: column; gap: 16px; }}
input[type=password] {{ padding: 12px; font-size: 16px; border: 1px solid #ccc; border-radius: 6px; }}
button {{ padding: 12px; background: #007bff; color: white; border: none; border-radius: 6px; font-size: 16px; cursor: pointer; }}
.note {{ color: #666; font-size: 14px; }}
</style></head><body>
<h1>{title}</h1>
<p class="note">This is a secure one-time input. Your secret is transmitted encrypted and never stored in chat history.</p>
<form method="POST">
  <input type="password" name="{field_name}" id="{field_name}" required>
  <button type="submit">Submit Securely</button>
</form></body></html>"""


def confirmation_page_html(
    *,
    title: str = "Confirm",
    message: str = "Do you want to proceed?",
    confirm_label: str = "Confirm",
    cancel_label: str = "Cancel",
) -> str:
    return f"""<!DOCTYPE html>
<html><head><title>{title}</title>
<meta name="viewport" content="width=device-width, initial-scale=1">
<style>
body {{ font-family: sans-serif; max-width: 500px; margin: 40px auto; padding: 20px; }}
.buttons {{ display: flex; gap: 12px; }}
button {{ padding: 12px 24px; border: none; border-radius: 6px; font-size: 16px; cursor: pointer; flex: 1; }}
.confirm {{ background: #28a745; color: white; }}
.cancel {{ background: #dc3545; color: white; }}
</style></head><body>
<h1>{title}</h1>
<div>{message}</div>
<form method="POST">
  <div class="buttons">
    <button type="submit" name="action" value="confirm" class="confirm">{confirm_label}</button>
    <button type="submit" name="action" value="cancel" class="cancel">{cancel_label}</button>
  </div>
</form></body></html>"""
