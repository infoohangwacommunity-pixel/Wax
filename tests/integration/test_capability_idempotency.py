"""Capability-invocation idempotency (CV-19): the failure matrix.

Mission §29 forbids "adding an idempotency column and declaring victory" —
the semantics must be tested against the failure modes the contract
promises to survive:

- identical request replay → RECORDED outcome, no re-execution
- concurrent duplicate → exactly one execution, loser refused
- crash after effect but before acknowledgement → the claim lease
  expires; an identical request may take the claim over and
  re-execute (honest at-least-once, same as the durable-work queue)
- duplicate while the claim is live → refused (outcome=duplicate)
- retry after failure → re-execution is honest (no outcome recorded)
- scheduled execution and live-bridge calls share ONE mechanism: the
  declared transport field is lifted from inputs before the authority
  gate (unit-tested here; both callers use the same lift function)
- approval replay is a separate ledger (CV-11) with the same shape —
  claim-before-effect, database-owned uniqueness
- partial execution cannot be made at-most-once by any ledger; the
  runtime is honest about it: takeover = at-least-once

The ledger is append-only evidence: no code path deletes rows.
"""

from __future__ import annotations

import asyncio
from datetime import UTC, datetime, timedelta

import pytest
from sqlalchemy import select, update

from wax.authority.service import AuthorizationService
from wax.capabilities.contracts import (
    CapabilityDescriptor,
    CapabilityInvocationRequest,
)
from wax.capabilities.idempotency import (
    claim_invocation,
    complete_success,
)
from wax.capabilities.invoker import (
    CapabilityInvoker,
    lift_idempotency_key,
)
from wax.identity.repository import PrincipalRepository
from wax.state.authority_models import Role
from wax.state.capability_models import CapabilityInvocationRecord
from wax.state.engine import db_session, dispose_engine, init_engine
from wax.state.models import Base


@pytest.fixture
async def fresh_db(test_settings):
    test_settings.__dict__["database_url"] = "sqlite+aiosqlite:///:memory:"
    init_engine(test_settings)
    engine = init_engine.__globals__["_engine"]
    async with engine.begin() as conn:
        await conn.run_sync(Base.metadata.create_all)
    yield
    await dispose_engine()


@pytest.fixture
def registry() -> CapabilityRegistry:
    from wax.capabilities.registry import CapabilityRegistry

    return CapabilityRegistry()


ROLE_ID = "01HXY" + "0" * 21


async def _make_authorized_principal() -> str:
    async with db_session() as session:
        role = await session.get(Role, ROLE_ID)
        if role is None:
            role = Role(
                id=ROLE_ID, name="member", description="Member role"
            )
            role.add_permission("capability.invoke:built_in")
            session.add(role)
        principal = await PrincipalRepository(session).create_principal()
        await AuthorizationService(session).assign_role(principal.id, role.id)
        await session.commit()
        return principal.id


def _counting_impl(counter: dict, behavior: str):
    """An effect that records executions and can fail on demand.

    behavior: "ok" always succeeds; "fail" raises on every call;
    "fail_once" raises on the first call only.
    """

    async def impl(inputs: dict, ctx) -> dict:
        counter["calls"] += 1
        if behavior == "fail" or (behavior == "fail_once" and counter["calls"] == 1):
            raise RuntimeError("effect blew up")
        return {"n": counter["calls"], "echo": inputs.get("message", "")}

    return impl


def _register_effect(registry, name: str, behavior: str, counter: dict) -> None:
    registry.register(
        CapabilityDescriptor(
            name=name,
            description="counted effect",
            required_permission="capability.invoke:built_in",
            timeout_seconds=5.0,
        ),
        _counting_impl(counter, behavior),
    )


async def _invoke(registry, principal_id: str, key: str | None, name: str = "eff"):
    async with db_session() as session:
        invoker = CapabilityInvoker(
            registry, AuthorizationService(session)
        )
        result = await invoker.invoke(
            CapabilityInvocationRequest(
                capability_name=name,
                principal_id=principal_id,
                inputs={"message": "m"},
                idempotency_key=key,
            )
        )
        await session.commit()
        return result


async def _ledger_rows() -> list[CapabilityInvocationRecord]:
    async with db_session() as session:
        result = await session.execute(select(CapabilityInvocationRecord))
        return list(result.scalars().all())


class TestReplaySemantics:
    async def test_replay_returns_recorded_outcome_without_reexecution(
        self, fresh_db, registry
    ) -> None:
        counter = {"calls": 0}
        _register_effect(registry, "eff", "ok", counter)
        principal = await _make_authorized_principal()

        first = await _invoke(registry, principal, "job-1")
        second = await _invoke(registry, principal, "job-1")

        assert first.outcome == "success"
        assert first.idempotent_replay is False
        assert second.outcome == "success"
        assert second.idempotent_replay is True
        # The replay carries the RECORDED outputs of the first execution.
        assert second.outputs == first.outputs
        assert counter["calls"] == 1, "replay must not re-execute the effect"

    async def test_no_key_means_no_ledger_and_both_calls_execute(
        self, fresh_db, registry
    ) -> None:
        counter = {"calls": 0}
        _register_effect(registry, "eff", "ok", counter)
        principal = await _make_authorized_principal()

        await _invoke(registry, principal, None)
        await _invoke(registry, principal, None)

        assert counter["calls"] == 2
        assert await _ledger_rows() == []

    async def test_different_principals_do_not_collide(
        self, fresh_db, registry
    ) -> None:
        """The ledger is keyed by (principal, capability, key): principals
        are independent authorities — one principal's key never blocks
        another's identical-shaped request."""
        counter = {"calls": 0}
        _register_effect(registry, "eff", "ok", counter)
        principal_a = await _make_authorized_principal()
        principal_b = await _make_authorized_principal()

        first = await _invoke(registry, principal_a, "shared-key")
        second = await _invoke(registry, principal_b, "shared-key")

        assert first.outcome == "success"
        assert second.outcome == "success"
        assert second.idempotent_replay is False
        assert counter["calls"] == 2


class TestConcurrency:
    async def test_concurrent_duplicates_execute_exactly_once(
        self, fresh_db, registry
    ) -> None:
        counter = {"calls": 0}
        _register_effect(registry, "eff", "ok", counter)
        principal = await _make_authorized_principal()

        results = await asyncio.gather(
            _invoke(registry, principal, "race-1"),
            _invoke(registry, principal, "race-1"),
        )
        outcomes = sorted(r.outcome for r in results)

        assert outcomes == ["duplicate", "success"], (
            "exactly one concurrent claimant may execute"
        )
        assert counter["calls"] == 1

        # After the winner completed, an identical request is a replay
        # of the recorded outcome — never a second execution.
        third = await _invoke(registry, principal, "race-1")
        assert third.outcome == "success"
        assert third.idempotent_replay is True
        assert counter["calls"] == 1

    async def test_live_claim_refuses_duplicate_until_completion(
        self, fresh_db, registry
    ) -> None:
        """Simulate a claim held by another live worker: the identical
        request is refused while the lease is live, then replays the
        recorded outcome once the owner completes."""
        counter = {"calls": 0}
        _register_effect(registry, "eff", "ok", counter)
        principal = await _make_authorized_principal()

        claim = await claim_invocation(
            principal_id=principal,
            capability_name="eff",
            idempotency_key="held-1",
            inputs={"message": "m"},
            claim_seconds=900.0,
        )
        assert claim.verdict.value == "claimed"

        refused = await _invoke(registry, principal, "held-1")
        assert refused.outcome == "duplicate"
        assert counter["calls"] == 0

        await complete_success({"done": True}, claim.record.id)
        replay = await _invoke(registry, principal, "held-1")
        assert replay.outcome == "success"
        assert replay.idempotent_replay is True
        assert replay.outputs == {"done": True}
        assert counter["calls"] == 0, "replay never re-executes"


class TestCrashRecovery:
    async def test_failed_attempt_allows_identical_retry(
        self, fresh_db, registry
    ) -> None:
        """A failed attempt recorded NO outcome, so an identical retry
        re-executing is honest (at-least-once). After the retry succeeds,
        the recorded success replays."""
        counter = {"calls": 0}
        _register_effect(registry, "eff", "fail_once", counter)
        principal = await _make_authorized_principal()

        failed = await _invoke(registry, principal, "retry-1")
        assert failed.outcome == "failure"
        assert counter["calls"] == 1

        recovered = await _invoke(registry, principal, "retry-1")
        assert recovered.outcome == "success"
        assert recovered.idempotent_replay is False, (
            "retry after failure is a real execution, not a replay"
        )
        assert counter["calls"] == 2

        replay = await _invoke(registry, principal, "retry-1")
        assert replay.outcome == "success"
        assert replay.idempotent_replay is True
        assert replay.outputs == recovered.outputs
        assert counter["calls"] == 2

    async def test_expired_lease_takeover_reexecutes(
        self, fresh_db, registry
    ) -> None:
        """A claim abandoned mid-execution (process died between claim
        and completion) holds an `executing` row whose lease expires.
        The effect state is UNKNOWN, so an identical request may take
        the claim over and re-execute — honest at-least-once."""
        counter = {"calls": 0}
        _register_effect(registry, "eff", "ok", counter)
        principal = await _make_authorized_principal()

        claim = await claim_invocation(
            principal_id=principal,
            capability_name="eff",
            idempotency_key="crash-1",
            inputs={"message": "m"},
            claim_seconds=900.0,
        )
        assert claim.verdict.value == "claimed"

        # The claiming process dies here (no complete_success/failure).
        # Backdate the lease to simulate time passing.
        async with db_session() as session:
            await session.execute(
                update(CapabilityInvocationRecord)
                .where(CapabilityInvocationRecord.id == claim.record.id)
                .values(
                    claim_expires_at=datetime.now(UTC) - timedelta(seconds=1)
                )
            )
            await session.commit()

        takeover = await _invoke(registry, principal, "crash-1")
        assert takeover.outcome == "success"
        assert takeover.idempotent_replay is False
        assert counter["calls"] == 1, "takeover re-executes the effect"

        replay = await _invoke(registry, principal, "crash-1")
        assert replay.outcome == "success"
        assert replay.idempotent_replay is True
        assert counter["calls"] == 1

    async def test_live_lease_is_not_takeable(self, fresh_db, registry) -> None:
        """A lease with time remaining means the owner may still be
        executing — the honest answer is 'refuse now', not 'take over'."""
        counter = {"calls": 0}
        _register_effect(registry, "eff", "ok", counter)
        principal = await _make_authorized_principal()

        claim = await claim_invocation(
            principal_id=principal,
            capability_name="eff",
            idempotency_key="live-1",
            inputs={"message": "m"},
            claim_seconds=900.0,
        )
        assert claim.verdict.value == "claimed"

        refused = await _invoke(registry, principal, "live-1")
        assert refused.outcome == "duplicate"
        assert counter["calls"] == 0


class TestEvidenceAndBoundaries:
    async def test_ledger_is_append_only_evidence(
        self, fresh_db, registry
    ) -> None:
        """Failure and replay never delete rows — replay evidence is
        audit evidence."""
        counter = {"calls": 0}
        _register_effect(registry, "eff", "fail", counter)
        principal = await _make_authorized_principal()

        await _invoke(registry, principal, "ev-1")
        rows = await _ledger_rows()
        assert len(rows) == 1
        assert rows[0].status == "failed"
        assert rows[0].error is not None

        await _invoke(registry, principal, "ev-1")  # retry fails again
        rows = await _ledger_rows()
        assert len(rows) == 1, "takeover reuses the row, never adds one"
        assert rows[0].status == "failed"

    async def test_denied_request_does_not_consume_key(
        self, fresh_db, registry
    ) -> None:
        """The claim happens AFTER authorization: a denied request must
        not burn the caller's idempotency key."""
        counter = {"calls": 0}
        _register_effect(registry, "eff", "ok", counter)
        denied_principal = await _make_authorized_principal()
        # A principal with NO roles is denied.
        async with db_session() as session:
            stranger = await PrincipalRepository(session).create_principal()
            await session.commit()
            stranger_id = stranger.id

        refused = await _invoke(registry, stranger_id, "key-1")
        assert refused.outcome == "denied"
        assert await _ledger_rows() == []

        allowed = await _invoke(registry, denied_principal, "key-1")
        assert allowed.outcome == "success"
        assert counter["calls"] == 1

    async def test_lift_idempotency_key_is_metadata_not_semantics(self) -> None:
        """The declared transport field is request METADATA: lifted before
        the authority gate so approval fingerprints stay about the
        operation. The original dict is execution-step evidence and is
        never mutated."""
        original = {"message": "m", "idempotency_key": " job-9 "}
        frozen = dict(original)

        cleaned, key = lift_idempotency_key(original)

        assert key == "job-9"
        assert cleaned == {"message": "m"}
        assert original == frozen, "caller's dict must not be mutated"

        # Absent / empty / whitespace-only / non-string → no key.
        for inputs in (
            {"message": "m"},
            {"message": "m", "idempotency_key": ""},
            {"message": "m", "idempotency_key": "   "},
            {"message": "m", "idempotency_key": 123},
        ):
            cleaned, key = lift_idempotency_key(inputs)
            assert key is None
            assert cleaned is inputs or cleaned == inputs

        # Overlong keys are clamped, not rejected (bounded evidence).
        cleaned, key = lift_idempotency_key(
            {"idempotency_key": "k" * 600}
        )
        assert key is not None and len(key) == 512

        # Non-dict inputs pass through untouched.
        passthrough, key = lift_idempotency_key("not-a-dict")  # type: ignore[arg-type]
        assert passthrough == "not-a-dict" and key is None
