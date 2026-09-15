"""Multi-instance shared state (ADR-0044, Phase 11).

Declares which runtime mechanisms MUST be shared across processes for
multi-instance correctness, and which are legitimately process-local.

Provides DB-backed implementations of rate limiting and cost tracking
that work across processes. The existing in-memory implementations
(RateLimiter, CostProtector) remain for single-process dev mode.
"""

from __future__ import annotations

from datetime import UTC, datetime

from sqlalchemy import DateTime, Index, Integer, String, text
from sqlalchemy.ext.asyncio import AsyncSession
from sqlalchemy.orm import Mapped, mapped_column
from ulid import ULID

from wax.runtime.logging import get_logger
from wax.state.models import Base, TimestampMixin, ULIDPrimaryKeyMixin

log = get_logger(__name__)


# The shared-state boundary declaration. This is documentation that
# operators can audit: "what MUST be shared for multi-instance correctness?"
SHARED_MECHANISMS = [
    "rate_limit_counters",
    "cost_tracking",
    "execution_leases",  # already DB-backed (work_items.lease_owner)
    "work_item_fencing",  # already DB-backed (mark_succeeded expected_owner)
    "approval_consumption",  # already DB-backed (pending_approvals.status)
    "idempotency_ledger",  # already DB-backed (capability_invocations)
]

PROCESS_LOCAL_MECHANISMS = [
    "provider_circuit_breaker",  # per-process observation is fine
    "inflight_asyncio_tasks",  # process-local by definition
    "mock_llm_script",  # test-only
]


class RateLimitCounterRecord(Base, ULIDPrimaryKeyMixin, TimestampMixin):
    """DB-backed rate limit counter for multi-instance correctness."""

    __tablename__ = "rate_limit_counters"
    __table_args__ = (
        Index("ix_rlc_principal_window", "principal_id", "window_start", unique=True),
    )

    principal_id: Mapped[str] = mapped_column(String(26), nullable=False)
    window_start: Mapped[datetime] = mapped_column(DateTime(timezone=True), nullable=False)
    count: Mapped[int] = mapped_column(Integer, nullable=False, default=0, server_default="0")
    window_seconds: Mapped[int] = mapped_column(
        Integer, nullable=False, default=60, server_default="60"
    )


class CostTrackingRecord(Base, ULIDPrimaryKeyMixin, TimestampMixin):
    """DB-backed cost tracking for multi-instance correctness."""

    __tablename__ = "cost_tracking"
    __table_args__ = (Index("ix_cost_principal_day", "principal_id", "day", unique=True),)

    principal_id: Mapped[str] = mapped_column(String(26), nullable=False)
    day: Mapped[str] = mapped_column(String(10), nullable=False)  # YYYY-MM-DD
    total_tokens: Mapped[int] = mapped_column(
        Integer, nullable=False, default=0, server_default="0"
    )
    total_messages: Mapped[int] = mapped_column(
        Integer, nullable=False, default=0, server_default="0"
    )
    total_cost_cents: Mapped[int] = mapped_column(
        Integer, nullable=False, default=0, server_default="0"
    )


class DbBackedRateLimiter:
    """Rate limiter backed by the DB for multi-instance correctness.

    Each process reads + writes atomically via conditional UPDATE.
    Falls back to no-op (allow) when the DB is not available.
    """

    def __init__(self, max_per_minute: int = 60) -> None:
        self._max = max_per_minute

    async def check(self, session: AsyncSession, principal_id: str) -> bool:
        """Returns True if allowed, False if rate-limited."""
        now = datetime.now(UTC)
        window_start = now.replace(second=0, microsecond=0)

        from sqlalchemy import select as sa_select
        from sqlalchemy import update as sa_update

        # Read existing counter
        result = await session.execute(
            sa_select(RateLimitCounterRecord)
            .where(RateLimitCounterRecord.principal_id == principal_id)
            .where(RateLimitCounterRecord.window_start == window_start)
        )
        record = result.scalar_one_or_none()

        if record is None:
            # No counter yet — create one with count=1
            try:
                new_record = RateLimitCounterRecord(
                    id=str(ULID()),
                    principal_id=principal_id,
                    window_start=window_start,
                    count=1,
                    window_seconds=60,
                )
                session.add(new_record)
                await session.flush()
                return True
            except Exception:
                # Race: another process created it — re-read and update
                await session.rollback()
                result = await session.execute(
                    sa_select(RateLimitCounterRecord)
                    .where(RateLimitCounterRecord.principal_id == principal_id)
                    .where(RateLimitCounterRecord.window_start == window_start)
                )
                record = result.scalar_one_or_none()
                if record is None:
                    return True  # edge case; allow

        current = record.count if record else 0
        if current >= self._max:
            return False

        # Atomic increment with conditional WHERE (count < max)
        result = await session.execute(
            sa_update(RateLimitCounterRecord)
            .where(RateLimitCounterRecord.principal_id == principal_id)
            .where(RateLimitCounterRecord.window_start == window_start)
            .where(RateLimitCounterRecord.count < self._max)
            .values(count=RateLimitCounterRecord.count + 1)
            .execution_options(synchronize_session=False)
        )
        return result.rowcount == 1


class DbBackedCostProtector:
    """Cost protector backed by the DB for multi-instance correctness.

    Tracks per-principal daily token/message totals. Rejects when
    the daily cap is exceeded.
    """

    def __init__(self, daily_token_cap: int = 1_000_000) -> None:
        self._cap = daily_token_cap

    async def check_and_record(
        self,
        session: AsyncSession,
        principal_id: str,
        *,
        tokens: int = 0,
        messages: int = 0,
    ) -> bool:
        """Returns True if within budget, False if exceeded."""
        now = datetime.now(UTC)
        day = now.strftime("%Y-%m-%d")

        result = await session.execute(
            text("SELECT total_tokens FROM cost_tracking WHERE principal_id = :pid AND day = :day"),
            {"pid": principal_id, "day": day},
        )
        row = result.first()

        if row is None:
            record = CostTrackingRecord(
                id=str(ULID()),
                principal_id=principal_id,
                day=day,
                total_tokens=tokens,
                total_messages=messages,
                total_cost_cents=0,
            )
            session.add(record)
            try:
                await session.flush()
                return tokens < self._cap
            except Exception:
                await session.rollback()
                result = await session.execute(
                    text(
                        "SELECT total_tokens FROM cost_tracking "
                        "WHERE principal_id = :pid AND day = :day"
                    ),
                    {"pid": principal_id, "day": day},
                )
                row = result.first()

        current_tokens = row[0] if row else 0
        if current_tokens + tokens > self._cap:
            return False

        result = await session.execute(
            text(
                "UPDATE cost_tracking "
                "SET total_tokens = total_tokens + :tokens, "
                "    total_messages = total_messages + :messages "
                "WHERE principal_id = :pid AND day = :day "
                "AND total_tokens + :tokens <= :cap"
            ),
            {
                "pid": principal_id,
                "day": day,
                "tokens": tokens,
                "messages": messages,
                "cap": self._cap,
            },
        )
        return result.rowcount == 1
