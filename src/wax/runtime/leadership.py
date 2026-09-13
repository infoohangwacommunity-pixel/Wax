"""Multi-instance leadership for the runtime maintenance loop.

WAX is designed to run as multiple instances behind a load balancer
(gunicorn workers, replicas). Durable-work claiming is already
multi-instance safe (FOR UPDATE SKIP LOCKED + lease fencing). The
maintenance loop (approval expiry, ledger retention) is IDEMPOTENT —
concurrent passes produce the same end state — but N instances running
the same sweeps wastes work and multiplies contention.

This module makes ONE instance the per-pass leader using Postgres
advisory locks: a session-scoped lock keyed to this runtime. Instances
that fail to acquire become followers for that pass: they skip the
sweeps and report the decision (log + metric) instead of silently
doing redundant work.

Dialect honesty:
- postgresql — real leader election via pg_try_advisory_lock. The lock
  is held on a dedicated session for the duration of one pass and
  released (or dropped, when the session closes) afterwards.
- sqlite — single-writer database: concurrent instances are impossible
  by construction, so every instance is the leader. This is documented
  semantics, not a missing feature.
- any other dialect — fail OPEN (act as leader). Sweeps are idempotent;
  skipping them because of an unknown dialect would be worse than
  double-running them.
"""

from __future__ import annotations

from typing import Any

from sqlalchemy import text

from wax.runtime.logging import get_logger

log = get_logger(__name__)

# Fixed, documented advisory-lock key for the maintenance loop. Arbitrary
# but stable; this constant is the single convention for it.
# 0x5741_5852 == 'WAXR' in ASCII.
MAINTENANCE_ADVISORY_LOCK_KEY = 0x5741_5852


def _mode_for(database_url: str) -> str:
    """Leader-election mode for a database URL (pure, testable)."""
    url = (database_url or "").lower()
    if url.startswith("postgres"):
        return "postgres_advisory_lock"
    if url.startswith("sqlite"):
        return "single_writer"
    return "fail_open"


class MaintenanceLeadership:
    """One maintenance pass's leadership token.

    When the database supports session-scoped advisory locks, the token
    holds a dedicated session open for the whole pass; `release()`
    closes it, which also releases the lock. Without such locks the
    token holds nothing (documented per-dialect semantics above).
    """

    def __init__(self, mode: str, is_leader: bool) -> None:
        self.mode = mode
        self.is_leader = is_leader
        self._cm: Any | None = None  # held db_session context manager, if any

    @classmethod
    async def acquire(cls, settings: Any) -> MaintenanceLeadership:
        """Try to become the leader for one maintenance pass."""
        from wax.state.engine import db_session

        mode = _mode_for(settings.database_url)
        if mode != "postgres_advisory_lock":
            # single_writer: always leader. fail_open: act as leader.
            return cls(mode=mode, is_leader=True)

        cm = db_session()
        session = await cm.__aenter__()
        try:
            result = await session.execute(
                text("SELECT pg_try_advisory_lock(:key)"),
                {"key": MAINTENANCE_ADVISORY_LOCK_KEY},
            )
            got = bool(result.scalar())
        except BaseException:
            await cm.__aexit__(None, None, None)
            raise

        if not got:
            await cm.__aexit__(None, None, None)
            log.info("runtime.maintenance.follower", mode=mode)
            return cls(mode=mode, is_leader=False)

        # End the lock-acquiring transaction but KEEP the session (and
        # the session-scoped lock) alive until release().
        await session.commit()
        token = cls(mode=mode, is_leader=True)
        token._cm = cm
        log.debug("runtime.maintenance.leader", mode=mode)
        return token

    async def release(self) -> None:
        """Release the advisory lock (if held) by closing its session."""
        if self._cm is not None:
            try:
                await self._cm.__aexit__(None, None, None)
            except Exception:  # pragma: no cover - close is best-effort
                log.warning(
                    "runtime.maintenance.leadership_release_error",
                    detail="advisory-lock session close failed; lock drops "
                    "with the connection regardless",
                )
            self._cm = None
