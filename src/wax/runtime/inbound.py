"""Durable inbound message acceptance — the webhook-to-worker boundary.

The webhook's ONLY job is to durably accept the message. It does NOT
run intelligence, does NOT call the LLM, does NOT execute terminal
commands, does NOT send a WhatsApp reply. Those happen in the worker.

This module provides the atomic, race-free idempotency primitive that
makes the webhook fast AND safe:

  accept_inbound_message(session, runtime_request) -> AcceptResult

The primitive uses ``INSERT ... ON CONFLICT DO NOTHING`` so that:

  - The FIRST delivery of a message ID inserts a row → schedule work.
  - The SECOND delivery (Meta retry, race condition, duplicate webhook)
    inserts nothing → acknowledge, do nothing.
  - Two concurrent deliveries race: the database's unique constraint
    + ON CONFLICT arbitrates exactly one winner. No UniqueViolationError
    ever reaches the application.

This replaces the old pattern of ``try: insert() except UniqueViolationError:
…`` which leaked the constraint violation as an application error and
required transaction rollback to recover.
"""

from __future__ import annotations

from dataclasses import dataclass
from datetime import UTC, datetime

from sqlalchemy import select
from sqlalchemy.dialects.postgresql import insert as pg_insert
from sqlalchemy.dialects.sqlite import insert as sqlite_insert
from sqlalchemy.ext.asyncio import AsyncSession

from wax.runtime.logging import get_logger
from wax.runtime.work.repository import WorkRepository
from wax.state.bridge_models import ProcessedMessageRecord

log = get_logger(__name__)


@dataclass
class AcceptResult:
    """The outcome of accepting an inbound message.

    `accepted=True` means this is a brand-new message — the caller
    should schedule durable work to process it.

    `accepted=False` means the message ID was already seen — the
    caller should acknowledge the webhook and do nothing else.
    """

    accepted: bool
    principal_id: str | None
    processed_message_id: str | None
    execution_id: str | None  # set if a previous run already started


async def accept_inbound_message(
    session: AsyncSession,
    *,
    interface_kind: str,
    interface_message_id: str,
    sender_interface_id: str,
    sender_display_name: str | None,
    text: str,
    received_at: datetime,
) -> AcceptResult:
    """Idempotently accept an inbound message.

    Uses INSERT ... ON CONFLICT DO NOTHING (PostgreSQL) or the SQLite
    equivalent, so concurrent deliveries of the same message ID never
    raise UniqueViolationError. Exactly one delivery wins; the rest
    are silently acknowledged as duplicates.

    On acceptance, schedules a durable intelligence Work item. The
    worker (already running) picks it up and runs bridge.process().

    Returns AcceptResult describing whether this call accepted the
    message or observed a duplicate.
    """
    # 1. Resolve the principal FIRST (auto-create on first contact).
    # We need the principal_id to schedule work for it.
    #
    # CONCURRENT RACE HANDLING: Two webhook deliveries of the same message
    # ID arrive simultaneously. Both call accept_inbound_message. Both
    # find no existing principal for the sender. Both try to create one.
    # The first wins; the second hits a UniqueViolation on the credential.
    #
    # We handle this by catching the integrity error and retrying the
    # resolution — by the time we retry, the winning transaction has
    # committed (or we get a fresh lookup that finds the new credential).
    from wax.identity.normalize import normalize_phone
    from wax.identity.repository import PrincipalRepository

    normalized_sender_id = (
        normalize_phone(sender_interface_id)
        if interface_kind == "whatsapp"
        else sender_interface_id
    )
    cred_kind = f"{interface_kind}_phone" if interface_kind == "whatsapp" else interface_kind

    principal = await PrincipalRepository(session).resolve_principal_by_credential(
        kind=cred_kind,
        value=normalized_sender_id,
    )
    if principal is None:
        try:
            principal = await PrincipalRepository(session).create_principal(
                display_name=sender_display_name or normalized_sender_id,
            )
            await PrincipalRepository(session).add_credential(
                principal.id,
                kind=cred_kind,
                value=normalized_sender_id,
                is_verified=True,
            )
            await session.flush()
        except Exception as cred_err:
            # Race condition: another concurrent request created the
            # credential first. Roll back our partial work and re-resolve.
            # The UniqueViolation on (kind, value) is the expected case.
            from sqlalchemy.exc import IntegrityError

            if not isinstance(cred_err, IntegrityError) and "already attached" not in str(cred_err):
                raise  # unrelated error — let it propagate
            await session.rollback()
            principal = await PrincipalRepository(session).resolve_principal_by_credential(
                kind=cred_kind,
                value=normalized_sender_id,
            )
            if principal is None:
                # Truly unexpected — the credential should exist now
                raise RuntimeError(
                    f"Principal resolution race: credential {cred_kind}={normalized_sender_id} "
                    f"could not be resolved after race rollback"
                ) from cred_err
    principal_id = principal.id

    # 2. Idempotently insert the processed_messages row.
    # Use dialect-specific INSERT ... ON CONFLICT DO NOTHING.
    from ulid import ULID

    # Determine the dialect from the session's bind.
    dialect_name = session.bind.dialect.name if session.bind else "sqlite"

    new_id_row_id = None
    new_pm_id = str(ULID())

    if dialect_name == "postgresql":
        stmt = (
            pg_insert(ProcessedMessageRecord)
            .values(
                id=new_pm_id,
                interface_kind=interface_kind,
                interface_message_id=interface_message_id,
                principal_id=principal_id,
                execution_id=None,
                outcome="pending",
                request_text=text[:2000],
                received_at=received_at,
            )
            .on_conflict_do_nothing(constraint="uq_processed_messages_interface_id")
            .returning(ProcessedMessageRecord.id)
        )
        result = await session.execute(stmt)
        new_id_row_id = result.scalar_one_or_none()
    else:
        # SQLite (test suite)
        stmt = (
            sqlite_insert(ProcessedMessageRecord)
            .values(
                id=new_pm_id,
                interface_kind=interface_kind,
                interface_message_id=interface_message_id,
                principal_id=principal_id,
                execution_id=None,
                outcome="pending",
                request_text=text[:2000],
                received_at=received_at,
            )
            .on_conflict_do_nothing(index_elements=["interface_kind", "interface_message_id"])
            .returning(ProcessedMessageRecord.id)
        )
        result = await session.execute(stmt)
        new_id_row_id = result.scalar_one_or_none()

    if new_id_row_id is None:
        # Conflict — the message was already accepted. Look up the
        # existing record so we can return its execution_id (if any).
        existing = await session.execute(
            select(ProcessedMessageRecord).where(
                ProcessedMessageRecord.interface_kind == interface_kind,
                ProcessedMessageRecord.interface_message_id == interface_message_id,
            )
        )
        existing_record = existing.scalar_one_or_none()
        log.info(
            "whatsapp.duplicate_message.acknowledged",
            message_id=interface_message_id,
            interface=interface_kind,
            principal_id=principal_id,
            existing_outcome=getattr(existing_record, "outcome", None),
        )
        return AcceptResult(
            accepted=False,
            principal_id=principal_id,
            processed_message_id=(existing_record.id if existing_record is not None else None),
            execution_id=(existing_record.execution_id if existing_record is not None else None),
        )

    # 3. Accepted — schedule durable intelligence work.
    # The worker picks this up and runs bridge.process(). The bridge
    # does NOT re-check idempotency (we already did) — it just runs
    # the intelligence loop and produces a response.
    work_repo = WorkRepository(session)
    work_item = await work_repo.schedule(
        kind="intelligence",
        payload={
            "prompt": text[:4000],
            "observation": {
                "source": "interface",
                "event": "inbound_message",
                "interface_kind": interface_kind,
                "interface_message_id": interface_message_id,
                "sender_interface_id": normalized_sender_id,
                "sender_display_name": sender_display_name,
                "received_at": received_at.isoformat(),
            },
        },
        wake_at=datetime.now(UTC),  # due immediately
        principal_id=principal_id,
        execution_id=None,  # bridge.process will create the execution
        max_attempts=3,
    )
    await session.flush()

    log.info(
        "whatsapp.message.accepted",
        message_id=interface_message_id,
        interface=interface_kind,
        principal_id=principal_id,
        work_id=work_item.id,
    )

    return AcceptResult(
        accepted=True,
        principal_id=principal_id,
        processed_message_id=new_id_row_id,
        execution_id=None,
    )
